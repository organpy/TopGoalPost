"""Season management service.

Handles season lifecycle: creation, advancing stages, archiving.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select, update

from bot.db import (
    get_db_manager,
    Event,
    EventStatus,
    Season,
    SeasonStatus,
    SeasonRepository,
    SettingRepository,
    ImageCacheRepository,
)
from bot.config import Config
from bot.services import audit
from bot.services.season_results import get_season_result
from bot.rendering import ASSETS_ROOT

logger = logging.getLogger(__name__)

STATE_ORDER = ["OFFSEASON", "SIGNUP", "OPEN", "PLAYIN", "SWISS", "PLAYOFF"]

# Legal forward moves. Anything else (skips, backward jumps) needs force=True.
TRANSITIONS = {
    "OFFSEASON": "SIGNUP",
    "SIGNUP": "OPEN",
    "OPEN": "PLAYIN",
    "PLAYIN": "SWISS",
    "SWISS": "PLAYOFF",
    "PLAYOFF": "OFFSEASON",
}


def next_state(state: str) -> Optional[str]:
    """The only state reachable from ``state`` without force."""
    return TRANSITIONS.get(state.upper())


def transition_error(current: str, new_state: str) -> Optional[str]:
    """Why ``current -> new_state`` is illegal, or None when it is allowed."""
    new_upper = new_state.upper()
    if new_upper not in STATE_ORDER:
        return f"Unknown state `{new_state}`. Valid: {', '.join(STATE_ORDER)}."
    if new_upper == current or TRANSITIONS.get(current) == new_upper:
        return None
    allowed = TRANSITIONS.get(current)
    return (
        f"Cannot move from {current} to {new_upper}. "
        f"Next allowed state is {allowed}; use force to override."
    )


class SeasonService:
    def __init__(self):
        self.db = get_db_manager()

    async def get_current_season_number(self) -> Optional[int]:
        """Get the current active season number.

        Older databases may only have the ``season`` setting, so fall back to
        it when no active Season row exists. Never assume season 1.
        """
        async with self.db.get_session() as session:
            repo = SeasonRepository(session)
            season = await repo.get_active()
            if season:
                return season.number
            stored = await SettingRepository(session).get("season")
            if not stored:
                return None
            try:
                return int(stored)
            except (TypeError, ValueError):
                return None

    async def get_current_season(self) -> Optional[Season]:
        """Get the current active season object."""
        async with self.db.get_session() as session:
            repo = SeasonRepository(session)
            season = await repo.get_active()
            if season:
                return season
            stored = await SettingRepository(session).get("season")
            if not stored:
                return None
            try:
                return await repo.get_by_number(int(stored))
            except (TypeError, ValueError):
                return None

    async def create_blocker(self) -> Optional[str]:
        """Why creating a new season would archive unfinished work, if it would."""
        async with self.db.get_session() as session:
            active = await SeasonRepository(session).get_active()
            if not active or active.ended_at is not None:
                return None
            has_events = (
                await session.execute(
                    select(func.count(Event.id)).where(Event.season == active.number)
                )
            ).scalar()
        if not has_events:
            return None
        return (
            f"Season {active.number} is still running. Close it with `/season close` "
            "first, or pass force to archive it unfinished."
        )

    async def create_season(self, season_number: int, force: bool = False) -> bool:
        """Create a new season and archive the previous one.

        History is kept: archived seasons keep their events, matches,
        registrations and players so brackets and stats stay viewable. Archived
        seasons cannot be reopened; create a new number instead.
        Refuses to archive a season that has events but was never closed,
        unless ``force``. Resets the state to OFFSEASON.
        """
        if not force and await self.create_blocker():
            return False

        async with self.db.get_session() as session:
            repo = SeasonRepository(session)

            # Never reuse a season number: archived data is read-only.
            if await repo.get_by_number(season_number):
                return False

            active = await repo.get_active()
            if active:
                if active.ended_at is None:
                    logger.warning("Archiving unfinished season %s", active.number)
                await repo.archive(active.number)

            session.add(Season(number=season_number, status=SeasonStatus.ACTIVE))
            await session.commit()
            archived = active.number if active else None

        await audit.record("season.create", "season", season_number, archived=archived, forced=force)

        await self._clear_generated_assets()
        await self.advance_state("OFFSEASON", force=True)
        return True

    async def close_season(self, season: int, force: bool = False) -> dict:
        """Finish a season: record the champion, complete events, go OFFSEASON.

        The season stays active (and viewable) until the next one is created.
        """
        state = await self.get_state()
        if state != "PLAYOFF" and not force:
            return {"error": f"Seasons close from PLAYOFF (currently {state})."}

        async with self.db.get_session() as session:
            row = await SeasonRepository(session).get_by_number(season)
            if not row:
                return {"error": f"Season {season} not found."}
            if row.ended_at is not None:
                return {"error": f"Season {season} is already closed."}

            result = await get_season_result(session, season)
            if result.champion is None or result.deciding_slot is None:
                return {"error": "The grand final is not complete yet."}

            row.champion_id = result.champion.id
            row.ended_at = datetime.utcnow()
            await session.execute(
                update(Event).where(Event.season == season).values(status=EventStatus.COMPLETED)
            )
            await session.commit()
            champion, runner_up = result.champion.name, (
                result.runner_up.name if result.runner_up else None
            )

        await audit.record("season.close", "season", season, champion=champion, forced=force)
        await self.advance_state("OFFSEASON", force=force)
        return {"ok": True, "season": season, "champion": champion, "runner_up": runner_up}

    async def is_active(self, season: int) -> bool:
        """Only the current season may be mutated; archived ones are read-only.

        With no season configured at all there is nothing to protect.
        """
        current = await self.get_current_season_number()
        if current is not None:
            return season == current
        row = await self.get_season(season)
        return row is None or row.status != SeasonStatus.ARCHIVED

    async def require_active(self, season: int) -> Optional[str]:
        """An error message when ``season`` is archived, otherwise None."""
        if await self.is_active(season):
            return None
        return f"Season {season} is archived and read-only."

    async def get_season(self, number: int) -> Optional[Season]:
        """Return a season row whether it is active or archived."""
        async with self.db.get_session() as session:
            return await SeasonRepository(session).get_by_number(number)

    async def _clear_generated_assets(self) -> None:
        """Drop cached tables so the new season starts with fresh art.

        Player avatars are keyed by Discord ID and shared across seasons, so
        they are kept — deleting them would strip archived brackets of faces.
        """
        async with self.db.get_session() as session:
            await ImageCacheRepository(session).delete_all()
            await session.commit()

        tables = ASSETS_ROOT / "tables"
        if tables.exists():
            for path in tables.glob("*.png"):
                try:
                    path.unlink()
                except OSError:
                    pass

    async def advance_state(self, new_state: str, force: bool = False) -> bool:
        """Move the season state along the legal path.

        OFFSEASON → SIGNUP → OPEN → PLAYIN → SWISS → PLAYOFF → OFFSEASON.
        Re-entering the current state is a no-op. ``force`` allows any valid
        state and is reserved for explicit admin overrides and reset paths.
        """
        state_upper = new_state.upper()
        if state_upper not in STATE_ORDER:
            return False

        config = Config()
        current = await config.get_season_state()
        error = transition_error(current, state_upper)
        if error and not force:
            return False
        if error:
            logger.warning("Forced season state %s -> %s", current, state_upper)

        await config.set_season_state(state_upper)
        if state_upper != current:
            await audit.record(
                "stage.force" if error else "stage.advance",
                "stage", state_upper, previous=current,
            )
        return True

    async def get_state(self) -> str:
        """Get the current season state."""
        config = Config()
        return await config.get_season_state()
