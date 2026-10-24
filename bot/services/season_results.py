"""Resolve a season's champion and runner-up from stored results.

The ``Season.champion_id`` column is authoritative when it is set, but older
seasons were often archived without it. In that case the grand final is read
directly from the playoff bracket so summaries and player cards can still show
who won.
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.db import (
    Event,
    EventType,
    Match,
    MatchPlayer,
    Player,
    Season,
)


@dataclass
class SeasonResult:
    champion: Optional[Player] = None
    runner_up: Optional[Player] = None
    deciding_slot: Optional[str] = None


async def _grand_final_matches(session, season_number: int) -> list[Match]:
    stmt = (
        select(Match)
        .join(Event, Match.event_id == Event.id)
        .options(
            selectinload(Match.match_players).selectinload(MatchPlayer.player)
        )
        .where(
            Event.season == season_number,
            Event.type == EventType.PLAYOFF,
            Match.bracket_slot.in_(("GF-M1", "GF-M2")),
            Match.completed_at.isnot(None),
        )
        .order_by(Match.id)
    )
    return list((await session.execute(stmt)).scalars().unique().all())


def _deciding_winner(
    matches: list[Match],
) -> tuple[Optional[int], Optional[str]]:
    """Return (player_id, slot) for the deciding grand final."""
    by_slot: dict[str, list[Match]] = {}
    for match in matches:
        if match.bracket_slot:
            by_slot.setdefault(match.bracket_slot, []).append(match)

    for slot in ("GF-M2", "GF-M1"):
        rows = by_slot.get(slot) or []
        if not rows:
            continue
        wins: dict[int, int] = {}
        for match in rows:
            winner = match.get_winner()
            if winner and winner.player_id is not None:
                wins[winner.player_id] = wins.get(winner.player_id, 0) + 1
        for player_id, count in wins.items():
            if count >= 2:
                return player_id, slot
        # A populated reset is the deciding series. If it is still in
        # progress, the earlier GF-M1 result is not the championship.
        return None, None
    return None, None


def _opponent(matches: list[Match], player_id: int) -> Optional[Player]:
    for match in matches:
        for mp in match.match_players:
            if mp.player_id and mp.player_id != player_id:
                if mp.player is not None:
                    return mp.player
                return None
    return None


async def get_season_result(session, season_number: int) -> SeasonResult:
    """Return the champion and runner-up for one season."""
    season = await session.get(Season, season_number)
    stored_champion = None
    if season and season.champion_id:
        stored_champion = await session.get(Player, season.champion_id)

    matches = await _grand_final_matches(session, season_number)
    derived_id, deciding_slot = _deciding_winner(matches)

    champion = stored_champion
    if derived_id is not None:
        derived = await session.get(Player, derived_id)
        if derived is not None:
            champion = derived

    runner_up = None
    if champion is not None:
        deciding = [
            match
            for match in matches
            if match.bracket_slot == deciding_slot
        ]
        runner_up = _opponent(deciding or matches, champion.id)

    return SeasonResult(
        champion=champion,
        runner_up=runner_up,
        deciding_slot=deciding_slot,
    )
