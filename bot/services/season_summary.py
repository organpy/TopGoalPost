"""Season summary: placements and statistical leaders.

Framework-free, like the rest of ``bot.services``. The renderer receives the
dataclasses below and does no database work of its own.
"""

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.db import (
    Event,
    Match,
    MatchPlayer,
    Player,
    Season,
    SeasonStatus,
    get_db_manager,
)
from bot.services.season_results import get_season_result


@dataclass
class SummaryPlayer:
    """One player's aggregate record for a season."""

    player_id: int
    name: str
    discord_id: int
    country: Optional[str]
    games: int = 0
    wins: int = 0
    losses: int = 0
    goals_for: int = 0
    goals_against: int = 0

    @classmethod
    def from_player(cls, player: Player) -> "SummaryPlayer":
        return cls(
            player_id=player.id,
            name=player.name,
            discord_id=player.discord_id,
            country=player.country,
        )

    @property
    def goal_diff(self) -> int:
        return self.goals_for - self.goals_against

    @property
    def win_rate(self) -> float:
        return self.wins / self.games if self.games else 0.0


@dataclass
class SeasonAward:
    """One leader tile in the rendered summary."""

    key: str
    label: str
    player: Optional[SummaryPlayer]
    value: str
    caption: str


@dataclass
class SeasonSummary:
    season: int
    active: bool
    matches: int
    players: int
    goals: int
    champion: Optional[SummaryPlayer] = None
    runner_up: Optional[SummaryPlayer] = None
    awards: list[SeasonAward] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.matches or self.champion)


class SeasonSummaryService:
    """Build a season-wide scoreboard from completed matches."""

    def __init__(self):
        self.db = get_db_manager()

    async def get_summary(self, season_number: int) -> SeasonSummary:
        async with self.db.get_session() as session:
            season = await session.get(Season, season_number)
            result = await get_season_result(session, season_number)
            matches = await self._completed_matches(session, season_number)
            players = self._aggregate(matches)

            champion = self._as_summary(result.champion, players)
            runner_up = self._as_summary(result.runner_up, players)
            goals = sum(
                mp.goals for match in matches for mp in match.match_players
            )

            return SeasonSummary(
                season=season_number,
                active=bool(
                    season and season.status == SeasonStatus.ACTIVE
                ),
                matches=len(matches),
                players=len(players),
                goals=goals,
                champion=champion,
                runner_up=runner_up,
                awards=self._awards(players),
            )

    @staticmethod
    async def _completed_matches(session, season_number: int):
        stmt = (
            select(Match)
            .join(Event, Match.event_id == Event.id)
            .options(
                selectinload(Match.match_players).selectinload(
                    MatchPlayer.player
                )
            )
            .where(
                Event.season == season_number,
                Match.completed_at.isnot(None),
            )
            .order_by(Match.id)
        )
        return list((await session.execute(stmt)).scalars().unique().all())

    @staticmethod
    def _aggregate(matches: list[Match]) -> dict[int, SummaryPlayer]:
        players: dict[int, SummaryPlayer] = {}
        for match in matches:
            for mp in match.match_players:
                if mp.player_id is None or mp.player is None:
                    continue
                player = players.get(mp.player_id)
                if player is None:
                    player = SummaryPlayer.from_player(mp.player)
                    players[mp.player_id] = player
                player.games += 1
                player.goals_for += mp.goals
                if mp.is_winner:
                    player.wins += 1
                else:
                    player.losses += 1
                opponent = next(
                    (
                        other
                        for other in match.match_players
                        if other.player_id != mp.player_id
                    ),
                    None,
                )
                if opponent is not None:
                    player.goals_against += opponent.goals
        return players

    @staticmethod
    def _as_summary(
        player: Optional[Player],
        players: dict[int, SummaryPlayer],
    ) -> Optional[SummaryPlayer]:
        if player is None:
            return None
        return players.get(player.id) or SummaryPlayer.from_player(player)

    @staticmethod
    def _awards(
        players: dict[int, SummaryPlayer],
    ) -> list[SeasonAward]:
        eligible = [player for player in players.values() if player.games]
        if not eligible:
            return [
                SeasonAward(
                    key=key,
                    label=label,
                    player=None,
                    value="—",
                    caption="No completed matches",
                )
                for key, label in _AWARD_LABELS
            ]

        max_games = max(player.games for player in eligible)
        # A 3-0 cameo should not beat a full season; require either half the
        # season's maximum workload or five games, whichever is smaller.
        win_rate_min = max(1, min(5, max_games // 2))

        def best(value, tiebreak=lambda player: ()):
            return max(eligible, key=lambda player: value(player) + tiebreak(player))

        top_scorer = best(
            lambda player: (player.goals_for,),
            lambda player: (player.wins, -player.games, player.name.lower()),
        )
        conceded = best(
            lambda player: (player.goals_against,),
            lambda player: (player.games, player.name.lower()),
        )
        games = best(
            lambda player: (player.games,),
            lambda player: (player.wins, player.goal_diff, player.name.lower()),
        )
        wins = best(
            lambda player: (player.wins,),
            lambda player: (player.goal_diff, -player.games, player.name.lower()),
        )
        diff = best(
            lambda player: (player.goal_diff,),
            lambda player: (player.wins, player.goals_for, player.name.lower()),
        )
        rate_players = [
            player for player in eligible if player.games >= win_rate_min
        ]
        rate = max(
            rate_players,
            key=lambda player: (
                player.win_rate,
                player.wins,
                player.goal_diff,
                player.name.lower(),
            ),
        )

        return [
            SeasonAward(
                key="top_scorer",
                label="TOP SCORER",
                player=top_scorer,
                value=str(top_scorer.goals_for),
                caption=f"{top_scorer.games} games played",
            ),
            SeasonAward(
                key="most_conceded",
                label="MOST CONCEDED",
                player=conceded,
                value=str(conceded.goals_against),
                caption=f"{conceded.games} games played",
            ),
            SeasonAward(
                key="most_games",
                label="MOST GAMES",
                player=games,
                value=str(games.games),
                caption=f"{games.wins}-{games.losses} record",
            ),
            SeasonAward(
                key="most_wins",
                label="MOST WINS",
                player=wins,
                value=str(wins.wins),
                caption=f"{wins.games} games played",
            ),
            SeasonAward(
                key="best_diff",
                label="BEST GOAL DIFF",
                player=diff,
                value=f"{diff.goal_diff:+d}" if diff.goal_diff else "0",
                caption=(
                    f"{diff.goals_for} scored · "
                    f"{diff.goals_against} conceded"
                ),
            ),
            SeasonAward(
                key="best_rate",
                label="BEST WIN RATE",
                player=rate,
                value=f"{rate.win_rate:.0%}",
                caption=f"{rate.wins}-{rate.losses} record",
            ),
        ]


_AWARD_LABELS = [
    ("top_scorer", "TOP SCORER"),
    ("most_conceded", "MOST CONCEDED"),
    ("most_games", "MOST GAMES"),
    ("most_wins", "MOST WINS"),
    ("best_diff", "BEST GOAL DIFF"),
    ("best_rate", "BEST WIN RATE"),
]
