"""Per-season player statistics.

Framework-free: nothing here imports discord, so the numbers can be unit
tested and reused by the profile embed, the admin info panel and the player
card renderer. Every consumer reads the same dataclass, so the three views can
never disagree about a player's record.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from bot.db import (
    Event,
    EventType,
    Match,
    MatchPlayer,
    Player,
    PlayerRepository,
    PlayerSeason,
    PlayerSeasonRepository,
    Season,
    SeasonRepository,
    SeasonStatus,
    get_db_manager,
)
from bot.services.season_results import get_season_result


class OpenState(str, Enum):
    """How a player reached the Swiss stage."""

    PLAYED = "played"
    SKIPPED = "skipped"
    NOT_PLAYED = "not_played"


@dataclass
class StageRecord:
    """Completed games for one stage of a season."""

    games: int = 0
    wins: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0

    def add(self, goals_for: int, goals_against: int, won: bool) -> None:
        self.games += 1
        self.goals_for += goals_for
        self.goals_against += goals_against
        if won:
            self.wins += 1
        else:
            self.losses += 1

    def merge(self, other: "StageRecord") -> None:
        self.games += other.games
        self.wins += other.wins
        self.losses += other.losses
        self.goals_for += other.goals_for
        self.goals_against += other.goals_against

    @property
    def played(self) -> bool:
        return self.games > 0

    @property
    def goal_diff(self) -> int:
        return self.goals_for - self.goals_against

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0

    def __str__(self) -> str:
        return f"{self.wins}-{self.losses}"


@dataclass
class PlayerStats:
    """Everything a profile view needs about one player in one season."""

    player_id: int
    name: str
    country: Optional[str]
    discord_id: int
    season: int
    season_active: bool
    registered: bool
    open: StageRecord = field(default_factory=StageRecord)
    playin: StageRecord = field(default_factory=StageRecord)
    swiss: StageRecord = field(default_factory=StageRecord)
    playoff: StageRecord = field(default_factory=StageRecord)
    swiss_seed: Optional[int] = None
    playin_rank: Optional[int] = None
    hp: Optional[int] = None
    champion: bool = False
    season_champion_name: Optional[str] = None
    championship_seasons: list[int] = field(default_factory=list)

    @property
    def circuits_won(self) -> int:
        return len(self.championship_seasons)

    @property
    def total(self) -> StageRecord:
        """Combined record across every stage (computed lazily, so it cannot
        go stale when the stage records are filled in after construction)."""
        total = StageRecord()
        for record in (self.open, self.playin, self.swiss, self.playoff):
            total.merge(record)
        return total

    @property
    def open_state(self) -> OpenState:
        """Decide from actual matches first, then from the stored seed.

        A returning seed has their Open row zeroed, so the counters alone
        cannot tell "never played" apart from "skipped on purpose".
        """
        if self.open.played:
            return OpenState.PLAYED
        if self.swiss_seed and self.swiss_seed > 0:
            return OpenState.SKIPPED
        return OpenState.NOT_PLAYED

    @property
    def open_summary(self) -> str:
        state = self.open_state
        if state is OpenState.PLAYED:
            return f"{self.open.wins}-{self.open.losses} · GD {self.open.goal_diff:+d}"
        if state is OpenState.SKIPPED:
            return f"Skipped Open (seed #{self.swiss_seed})"
        return "Did not play"

    @staticmethod
    def record_summary(record: StageRecord) -> str:
        if not record.played:
            return "Did not play"
        return f"{record.wins}-{record.losses} · GD {record.goal_diff:+d}"

    @property
    def has_data(self) -> bool:
        return self.registered or self.total.played


@dataclass
class SeasonInfo:
    """One archived (or active) season for the archive overview."""

    number: int
    active: bool
    champion: Optional[str]
    ended_at: Optional[datetime]


_STAGE_FIELD = {
    EventType.OPEN: "open",
    EventType.PLAYIN: "playin",
    EventType.SWISS: "swiss",
    EventType.PLAYOFF: "playoff",
}


class PlayerStatsService:
    """Read per-season player records straight from completed matches."""

    def __init__(self):
        self.db = get_db_manager()

    async def available_seasons(self) -> list[int]:
        """Seasons that actually have tournament data, newest first."""
        async with self.db.get_session() as session:
            stmt = select(Event.season).distinct().order_by(Event.season.desc())
            seasons = list((await session.execute(stmt)).scalars())
            active = await SeasonRepository(session).get_active()
        if active and active.number not in seasons:
            seasons.insert(0, active.number)
        return sorted(set(seasons), reverse=True)

    async def season_overview(self) -> list[SeasonInfo]:
        """Seasons with data, newest first, including champion names."""
        async with self.db.get_session() as session:
            numbers = set(
                (await session.execute(select(Event.season).distinct())).scalars()
            )
            active = await SeasonRepository(session).get_active()
            if active:
                numbers.add(active.number)
            if not numbers:
                return []

            stmt = (
                select(Season)
                .options(selectinload(Season.champion))
                .where(Season.number.in_(numbers))
                .order_by(Season.number.desc())
            )
            seasons = (await session.execute(stmt)).scalars().unique().all()
            overview = []
            for season in seasons:
                result = await get_season_result(session, season.number)
                champion = result.champion or season.champion
                overview.append(
                    SeasonInfo(
                        number=season.number,
                        active=season.status == SeasonStatus.ACTIVE,
                        champion=champion.name if champion else None,
                        ended_at=season.ended_at,
                    )
                )
            return overview

    async def resolve_player(
        self, username: str, season_number: int
    ) -> Optional[Player]:
        """Find a player by name or ID, preferring that season's roster.

        Numeric input is tried as an internal player ID, then as a Discord ID.
        Names are global, so two seasons can hold different players with the
        same name; looking inside the requested season first keeps archived
        stats pointed at the right person.
        """
        token = (username or "").strip()
        async with self.db.get_session() as session:
            if token.isdigit():
                number = int(token)
                player = await session.get(Player, number)
                if player is None:
                    player = await PlayerRepository(session).get_by_discord_id(
                        number
                    )
                if player is not None:
                    return player

            stmt = (
                select(Player)
                .join(PlayerSeason, PlayerSeason.player_id == Player.id)
                .where(
                    PlayerSeason.season_number == season_number,
                    func.lower(Player.name) == token.lower(),
                )
                .limit(1)
            )
            player = (await session.execute(stmt)).scalars().first()
            if player is None:
                stmt = (
                    select(Player)
                    .where(func.lower(Player.name) == token.lower())
                    .limit(1)
                )
                player = (await session.execute(stmt)).scalars().first()
            return player

    async def get_stats(
        self, player_id: int, season_number: int
    ) -> Optional[PlayerStats]:
        """Build the stat block for one player and season, or None if unknown."""
        async with self.db.get_session() as session:
            player = await session.get(Player, player_id)
            if player is None:
                return None
            season = await session.get(Season, season_number)
            player_season = await PlayerSeasonRepository(session).get(
                player_id=player_id, season_number=season_number
            )
            matches = await self._completed_matches(session, player_id, season_number)
            season_result = await get_season_result(session, season_number)

            championship_seasons = []
            season_numbers = (
                await session.execute(select(Season.number).order_by(Season.number))
            ).scalars()
            for number in season_numbers:
                result = await get_season_result(session, number)
                if result.champion and result.champion.id == player.id:
                    championship_seasons.append(number)

            stats = PlayerStats(
                player_id=player.id,
                name=player.name,
                country=player.country,
                discord_id=player.discord_id,
                season=season_number,
                season_active=bool(season and season.status == SeasonStatus.ACTIVE),
                registered=player_season is not None,
                swiss_seed=player_season.swiss_seed if player_season else None,
                playin_rank=player_season.playin_rank if player_season else None,
                hp=player_season.hp if player_season else None,
                champion=bool(
                    season_result.champion
                    and season_result.champion.id == player.id
                ),
                season_champion_name=(
                    season_result.champion.name
                    if season_result.champion
                    else None
                ),
                championship_seasons=championship_seasons,
            )
            for match in matches:
                record = getattr(stats, _STAGE_FIELD[match.event.type])
                mine = next(
                    (mp for mp in match.match_players if mp.player_id == player_id),
                    None,
                )
                if mine is None:
                    continue
                opponent = next(
                    (mp for mp in match.match_players if mp.player_id != player_id),
                    None,
                )
                record.add(
                    goals_for=mine.goals,
                    goals_against=opponent.goals if opponent else 0,
                    won=mine.is_winner,
                )
        return stats

    async def get_stats_by_discord_id(
        self, discord_id: int, season_number: int
    ) -> Optional[PlayerStats]:
        """Convenience wrapper for the common "my own profile" case."""
        async with self.db.get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(discord_id)
        if player is None:
            return None
        return await self.get_stats(player.id, season_number)

    @staticmethod
    async def _completed_matches(session, player_id: int, season_number: int):
        stmt = (
            select(Match)
            .join(Event, Match.event_id == Event.id)
            .options(selectinload(Match.match_players), selectinload(Match.event))
            .where(
                Event.season == season_number,
                Match.completed_at.isnot(None),
                Match.match_players.any(MatchPlayer.player_id == player_id),
            )
            .order_by(Match.id)
        )
        result = await session.execute(stmt)
        return result.scalars().unique().all()
