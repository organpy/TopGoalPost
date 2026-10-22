"""Development utilities for testing the tournament pipeline.

All methods bypass state checks — intended for local / staging use only.
"""

import random
from typing import Optional, Tuple

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    Player,
    PlayerSeason,
    Match,
    MatchPlayer,
    PlayerRepository,
    PlayerSeasonRepository,
    MatchRepository,
)


FLAG_POOL = ["us", "fr", "jp", "gb", "de", "ca", "br", "ar", "it", "es", "pt", "nl"]

DUMMY_ID_BASE = 9_000_000
DUMMY_ID_END = 9_999_999


class DevService:
    def __init__(self):
        self.db = get_db_manager()

    async def next_dummy_start(self) -> int:
        """First free dummy discord_id inside the reserved dummy range."""
        async with self.db.get_session() as session:
            stmt = select(func.max(Player.discord_id)).where(
                Player.discord_id.between(DUMMY_ID_BASE, DUMMY_ID_END)
            )
            result = await session.execute(stmt)
            max_id = result.scalar()
        next_id = (max_id + 1) if max_id else DUMMY_ID_BASE
        if next_id > DUMMY_ID_END:
            raise ValueError("Dummy discord ID range is exhausted.")
        return next_id

    async def dummy_register(
        self, season_num: int, discord_id: int, name: str, country: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Register a player for a season without any state check.

        Returns (success, message).
        """
        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            ps_repo = PlayerSeasonRepository(session)

            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                player = await player_repo.create(
                    discord_id=discord_id, name=name, country=country,
                )
            else:
                player.name = name
                if country:
                    player.country = country
                session.add(player)

            ps = await ps_repo.get(player_id=player.id, season_number=season_num)
            if ps:
                return False, f"**{name}** is already registered for season {season_num}."

            await ps_repo.create(player_id=player.id, season_number=season_num)
            await session.commit()

        return True, f"Registered **{name}** for season {season_num}."

    async def dummy_score_all(self) -> int:
        """Score every playable match, including newly generated BO3/round games.

        Uses MatchService.report_score so side-effects
        (stats, BO3, playin auto-advance, bracket propagation) are applied.

        Returns the number of matches scored.
        """
        from bot.services.match import MatchService

        match_service = MatchService()
        total_scored = 0

        # Each pass scores the matches that currently have two players. A pass
        # can create the next Playin batch or BO3 game, so repeat until a pass
        # makes no progress.
        for _ in range(200):
            async with self.db.get_session() as session:
                stmt = (
                    select(Match)
                    .options(selectinload(Match.match_players))
                    .where(Match.completed_at.is_(None))
                )
                result = await session.execute(stmt)
                unplayed = result.scalars().all()

            scored_this_pass = 0
            for match in unplayed:
                mps = sorted(match.match_players, key=lambda mp: mp.side)
                if len(mps) < 2 or not mps[0].player_id or not mps[1].player_id:
                    continue
                s1 = random.randint(0, 5)
                s2 = random.randint(0, 5)
                while s1 == s2:
                    s2 = random.randint(0, 5)
                winner_side = 0 if s1 > s2 else 1
                await match_service.report_score(match.id, s1, s2, winner_side)
                scored_this_pass += 1

            total_scored += scored_this_pass
            if scored_this_pass == 0:
                break

        return total_scored

    async def dummy_replace_id(
        self, old_discord_id: int, new_discord_id: int
    ) -> Tuple[bool, str]:
        """Change a player's discord_id (e.g. for local testing).

        Returns (success, message).
        """
        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)

            player = await player_repo.get_by_discord_id(old_discord_id)
            if not player:
                return False, f"No player found with discord ID `{old_discord_id}`."

            existing = await player_repo.get_by_discord_id(new_discord_id)
            if existing and existing.id != player.id:
                return False, f"Target discord ID `{new_discord_id}` is already in use by **{existing.name}**."

            player.discord_id = new_discord_id
            session.add(player)
            await session.commit()

        return True, f"Updated **{player.name}** discord ID: `{old_discord_id}` → `{new_discord_id}`."
