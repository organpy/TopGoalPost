"""Signup service.

Handles player registration and unregistration for the active season.
"""

from typing import Optional, Tuple

from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    Player,
    PlayerSeason,
    Match,
    MatchPlayer,
    PlayerRepository,
    PlayerSeasonRepository,
    MatchPlayerRepository,
)
from bot.services.season import SeasonService
from bot.utils.countries import resolve_country


class SignupService:
    def __init__(self):
        self.db = get_db_manager()
        self.season_service = SeasonService()

    async def register(self, discord_id: int, name: str, country: Optional[str] = None) -> Tuple[bool, str]:
        """Register a user for the current season.

        Args:
            discord_id: User's Discord ID
            name: User's chosen display name
            country: Optional 2-letter country code

        Returns:
            Tuple of (success: bool, message: str)
        """
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            return False, "No active season exists."

        state = await self.season_service.get_state()
        if state != "SIGNUP":
            return False, "Signups are currently closed."

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            ps_repo = PlayerSeasonRepository(session)

            # Get or create player
            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                player = await player_repo.create(discord_id=discord_id, name=name, country=country)
            else:
                # Update name/country if provided
                player.name = name
                if country:
                    player.country = country
                session.add(player)

            # Check if already registered for season
            ps = await ps_repo.get(player_id=player.id, season_number=season_num)
            if ps:
                return False, "You are already registered for this season."

            # Register
            await ps_repo.create(player_id=player.id, season_number=season_num)
            await session.commit()

        return True, (
            "Successfully registered for the season!\n\n"
            "Next: `/set_timezone` so times show in your local time, then "
            "`/add_timeslot` to add your weekly availability."
        )

    async def unregister(self, discord_id: int) -> Tuple[bool, str]:
        """Unregister a user from the current season.

        Args:
            discord_id: User's Discord ID

        Returns:
            Tuple of (success: bool, message: str)
        """
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            return False, "No active season exists."
            
        state = await self.season_service.get_state()
        if state not in ("SIGNUP", "OPEN"):
            return False, "It's too late to unregister from this season."

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            ps_repo = PlayerSeasonRepository(session)
            mp_repo = MatchPlayerRepository(session)

            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                return False, "You are not registered."

            ps = await ps_repo.get(player_id=player.id, season_number=season_num)
            if not ps:
                return False, "You are not registered for this season."
                
            # Check for uncompleted matches in OPEN stage
            if state == "OPEN":
                unplayed = await mp_repo.get_unplayed_player_matches(
                    player.id, season=season_num
                )
                if unplayed:
                    return False, (
                        f"Cannot unregister: you still have {len(unplayed)} unplayed match(es). "
                        f"Play or forfeit them first using `/forfeit match_id:X`."
                    )
            
            await ps_repo.delete(player_id=player.id, season_number=season_num)
            await session.commit()
            
        return True, "Successfully unregistered from the season."
        
    async def edit_name(
        self, discord_id: int, new_name: str, force: bool = False
    ) -> Tuple[bool, str]:
        """Change a player's display name.

        Args:
            discord_id: User's Discord ID
            new_name: New display name (3-50 chars)
            force: Admin override; skips the SIGNUP state check

        Returns:
            Tuple of (success: bool, message: str)
        """
        if len(new_name) < 3 or len(new_name) > 50:
            return False, "Name must be between 3 and 50 characters."

        if not force:
            season_num = await self.season_service.get_current_season_number()
            if not season_num:
                return False, "No active season exists."
            state = await self.season_service.get_state()
            if state != "SIGNUP":
                return False, "Names can only be changed while signups are open."

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                return False, "You are not registered. Use `/signup` first."

            player.name = new_name
            session.add(player)
            await session.commit()

        return True, f"Your display name has been changed to **{new_name}**."

    async def edit_country(
        self, discord_id: int, new_country: str
    ) -> Tuple[bool, str]:
        """Change a player's country at any time.

        Country only affects display and flags, so unlike names it is not
        restricted to the signup window. The previous value may be NULL.
        """
        code = resolve_country(new_country)
        if code is None:
            return False, f"Unknown country `{new_country}`."

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                return False, "You are not registered. Use `/signup` first."

            old_country = player.country
            if old_country and old_country.lower() == code:
                return False, f"Your country is already **{code.upper()}**."

            player.country = code
            session.add(player)
            await session.commit()

        old_label = old_country.upper() if old_country else "—"
        return True, (
            f"Your country has been changed from **{old_label}** "
            f"to **{code.upper()}**."
        )

    async def import_seeds(self, discord_ids: list[int], season_number: int) -> Tuple[list[str], list[str]]:
        """Assign swiss_seed 1-N to players by discord_id order.

        Args:
            discord_ids: Ordered list of Discord user IDs (seed 1 = first)
            season_number: Season number

        Returns:
            Tuple of (success_messages, error_messages)
        """
        success_msgs = []
        error_msgs = []

        async with self.db.get_session() as session:
            ps_repo = PlayerSeasonRepository(session)
            for i, did in enumerate(discord_ids, start=1):
                player_repo = PlayerRepository(session)
                player = await player_repo.get_by_discord_id(did)
                if not player:
                    error_msgs.append(f"Seed {i}: <@{did}> not found in database")
                    continue
                ps = await ps_repo.get(player_id=player.id, season_number=season_number)
                if not ps:
                    error_msgs.append(f"Seed {i}: **{player.name}** is not registered for this season")
                    continue
                ps.swiss_seed = i
                session.add(ps)
                success_msgs.append(f"Seed {i}: **{player.name}**")
            await session.commit()

        return success_msgs, error_msgs

    async def get_skips(self, season_number: int) -> list[tuple[int, str, int]]:
        """Return (seed, name, player_id) for players skipping Open."""
        async with self.db.get_session() as session:
            all_ps = await PlayerSeasonRepository(session).get_by_season(
                season_number
            )
            skips = [
                ps for ps in all_ps
                if ps.swiss_seed is not None and ps.swiss_seed > 0
            ]
            skips.sort(key=lambda ps: ps.swiss_seed)
            out = []
            for ps in skips:
                player = await PlayerRepository(session).get_by_id(ps.player_id)
                out.append(
                    (ps.swiss_seed, player.name if player else f"Player {ps.player_id}", ps.player_id)
                )
            return out

    async def set_skip_open(
        self,
        season_number: int,
        tokens: list[str],
        country: str = "XX",
    ) -> Tuple[list[str], list[str]]:
        """Replace the Open-skip list for a season.

        Tokens may be Discord IDs, mentions, or player names. Players must
        already have an account; a PlayerSeason row is created when needed.
        Seeds are assigned in the order supplied.
        """
        success: list[str] = []
        errors: list[str] = []

        async with self.db.get_session() as session:
            ps_repo = PlayerSeasonRepository(session)
            player_repo = PlayerRepository(session)

            all_ps = await ps_repo.get_by_season(season_number)
            for ps in all_ps:
                if ps.swiss_seed is not None:
                    ps.swiss_seed = None

            seen_players: set[int] = set()
            for raw in tokens:
                index = len(success) + 1
                token = raw.strip()
                if not token:
                    continue
                token = token.strip("<@!>")
                player = None
                if token.isdigit():
                    player = await player_repo.get_by_discord_id(int(token))
                if player is None:
                    player = await player_repo.get_by_name(token)
                if player is None:
                    result = await session.execute(
                        select(Player).where(
                            func.lower(Player.name) == token.lower()
                        )
                    )
                    player = result.scalars().first()
                if player is None:
                    errors.append(f"`{raw}`: player not found.")
                    continue
                if player.id in seen_players:
                    errors.append(f"`{raw}`: player listed more than once.")
                    continue
                seen_players.add(player.id)

                ps = await ps_repo.get(player.id, season_number)
                if not ps:
                    ps = await ps_repo.create(
                        player_id=player.id, season_number=season_number
                    )
                ps.swiss_seed = index
                ps.open_wins = 0
                ps.open_losses = 0
                ps.open_gd = 0
                ps.hp = 4
                ps.playin_rank = None
                ps.playin_eliminated = False
                session.add(ps)
                success.append(f"Seed {index}: **{player.name}**")

            await session.commit()

        return success, errors

    async def is_registered(self, discord_id: int) -> bool:
        """Check if a user is registered for the current season."""
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            return False
            
        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                return False
                
            ps_repo = PlayerSeasonRepository(session)
            ps = await ps_repo.get(player_id=player.id, season_number=season_num)
            return ps is not None
