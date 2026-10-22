"""Season management service.

Handles season lifecycle: creation, advancing stages, archiving.
"""

from typing import Optional

from sqlalchemy import delete, select

from bot.db import (
    get_db_manager,
    Event,
    Match,
    MatchPlayer,
    Player,
    PlayerSeason,
    Season,
    SeasonStatus,
    SeasonRepository,
    SettingRepository,
    ImageCacheRepository,
)
from bot.config import Config
from bot.rendering import ASSETS_ROOT

STATE_ORDER = ["OFFSEASON", "SIGNUP", "OPEN", "PLAYIN", "SWISS", "PLAYOFF"]


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
        """Create a new season and archive the previous one if it exists.

        Wipes all data belonging to other seasons (events, matches, player
        registrations, players) so each season starts with a clean roster.
        Also resets the config state to OFFSEASON so it can be manually advanced.
        """
        async with self.db.get_session() as session:
            repo = SeasonRepository(session)

            # Archive existing active season
            active = await repo.get_active()
            if active:
                if active.number == season_number:
                    return False  # Already exists and is active
                active.status = SeasonStatus.ARCHIVED

            # Check if requested season already exists in db (maybe archived)
            existing = await repo.get_by_number(season_number)
            if existing:
            # If it already exists, just make it active again
                existing.status = SeasonStatus.ACTIVE
                session.add(existing)
            else:
                season = Season(number=season_number, status=SeasonStatus.ACTIVE)
                session.add(season)

            await session.commit()

        await self._purge_other_seasons(season_number)
        await self._clear_generated_assets()

        # Reset state to offseason
        config = Config()
        await config.set_season_state("OFFSEASON")
        return True

    async def _clear_generated_assets(self) -> None:
        """Drop cached images so a new season cannot reuse last season's art."""
        async with self.db.get_session() as session:
            await ImageCacheRepository(session).delete_all()
            await session.commit()

        pfps = ASSETS_ROOT / "pfps"
        if pfps.exists():
            for path in pfps.glob("*.png"):
                if path.name in {"default.png", "circle.png"}:
                    continue
                try:
                    path.unlink()
                except OSError:
                    pass

        tables = ASSETS_ROOT / "tables"
        if tables.exists():
            for path in tables.glob("*.png"):
                try:
                    path.unlink()
                except OSError:
                    pass

    async def _purge_other_seasons(self, keep_number: int) -> int:
        """Delete every row tied to a season other than keep_number.

        Cascades: match_players -> matches -> events -> player_seasons ->
        orphaned players. Global user prefs (timezones, timeslots, settings)
        are preserved. Returns number of seasons purged.
        """
        async with self.db.get_session() as session:
            old_event_ids = select(Event.id).where(Event.season != keep_number)
            old_match_ids = select(Match.id).where(Match.event_id.in_(old_event_ids))

            await session.execute(
                delete(MatchPlayer).where(MatchPlayer.match_id.in_(old_match_ids))
            )
            await session.execute(delete(Match).where(Match.id.in_(old_match_ids)))
            await session.execute(delete(Event).where(Event.season != keep_number))
            await session.execute(
                delete(PlayerSeason).where(PlayerSeason.season_number != keep_number)
            )

            surviving_players = select(PlayerSeason.player_id).where(
                PlayerSeason.season_number == keep_number
            )
            await session.execute(
                delete(Player).where(Player.id.not_in(surviving_players))
            )
            await session.commit()

    async def advance_state(self, new_state: str) -> bool:
        """Advance the season state.
        
        Typically OFFSEASON → SIGNUP → OPEN → PLAYIN → SWISS → PLAYOFF.
        Currently allows jumping to any valid state to prevent locking the admin.
        """
        state_upper = new_state.upper()
        if state_upper not in STATE_ORDER:
            return False

        config = Config()
        await config.set_season_state(state_upper)
        return True
    
    async def get_state(self) -> str:
        """Get the current season state."""
        config = Config()
        return await config.get_season_state()
