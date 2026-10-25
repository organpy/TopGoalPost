"""Season management service.

Handles season lifecycle: creation, advancing stages, archiving.
"""

import logging
from typing import Optional

from bot.db import (
    get_db_manager,
    Season,
    SeasonStatus,
    SeasonRepository,
    SettingRepository,
    ImageCacheRepository,
)
from bot.config import Config
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

    async def create_season(self, season_number: int) -> bool:
        """Create a new season and archive the previous one.

        History is kept: archived seasons keep their events, matches,
        registrations and players so brackets and stats stay viewable. Archived
        seasons cannot be reopened; create a new number instead.
        Also resets the config state to OFFSEASON so it can be manually advanced.
        """
        async with self.db.get_session() as session:
            repo = SeasonRepository(session)

            # Never reuse a season number: archived data is read-only.
            if await repo.get_by_number(season_number):
                return False

            active = await repo.get_active()
            if active:
                await repo.archive(active.number)

            session.add(Season(number=season_number, status=SeasonStatus.ACTIVE))
            await session.commit()

        await self._clear_generated_assets()

        await self.advance_state("OFFSEASON", force=True)
        return True

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
        return True

    async def get_state(self) -> str:
        """Get the current season state."""
        config = Config()
        return await config.get_season_state()
