"""Pairing service for all tournament stages."""

import random
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    Event,
    Match,
    MatchPlayer,
    EventType,
    EventStatus,
    PlayerSeasonRepository,
    EventRepository,
    MatchRepository,
    MatchPlayerRepository,
)

SWISS_FIELD_SIZE = 32

PLAYOFF_BRACKET: list[tuple[str, int | None, int | None, str | None, str | None, str, str]] = [
    # ── Winners Bracket ──────────────────────────────────────
    # Winners Round 1 (8 matches, seeded)
    ("W-R1-M1", 0, 15, None, None, "winner", "winner"),
    ("W-R1-M2", 7, 8,  None, None, "winner", "winner"),
    ("W-R1-M3", 3, 12, None, None, "winner", "winner"),
    ("W-R1-M4", 4, 11, None, None, "winner", "winner"),
    ("W-R1-M5", 1, 14, None, None, "winner", "winner"),
    ("W-R1-M6", 6, 9,  None, None, "winner", "winner"),
    ("W-R1-M7", 2, 13, None, None, "winner", "winner"),
    ("W-R1-M8", 5, 10, None, None, "winner", "winner"),
    # Winners Round 2 (Quarterfinals, 4 matches)
    ("W-R2-M1", None, None, "W-R1-M1", "W-R1-M2", "winner", "winner"),
    ("W-R2-M2", None, None, "W-R1-M3", "W-R1-M4", "winner", "winner"),
    ("W-R2-M3", None, None, "W-R1-M5", "W-R1-M6", "winner", "winner"),
    ("W-R2-M4", None, None, "W-R1-M7", "W-R1-M8", "winner", "winner"),
    # Winners Round 3 (Semifinals, 2 matches)
    ("W-R3-M1", None, None, "W-R2-M1", "W-R2-M2", "winner", "winner"),
    ("W-R3-M2", None, None, "W-R2-M3", "W-R2-M4", "winner", "winner"),
    # Winners Round 4 (Final, 1 match)
    ("W-R4-M1", None, None, "W-R3-M1", "W-R3-M2", "winner", "winner"),
    # ── Losers Bracket ───────────────────────────────────────
    # Losers Round 1 (4 matches: W-R1 losers paired)
    ("L-R1-M1", None, None, "W-R1-M1", "W-R1-M2", "loser", "loser"),
    ("L-R1-M2", None, None, "W-R1-M3", "W-R1-M4", "loser", "loser"),
    ("L-R1-M3", None, None, "W-R1-M5", "W-R1-M6", "loser", "loser"),
    ("L-R1-M4", None, None, "W-R1-M7", "W-R1-M8", "loser", "loser"),
    # Losers Round 2 (4 matches: L-R1 winners vs W-R2 losers)
    ("L-R2-M1", None, None, "L-R1-M1", "W-R2-M1", "winner", "loser"),
    ("L-R2-M2", None, None, "L-R1-M2", "W-R2-M2", "winner", "loser"),
    ("L-R2-M3", None, None, "L-R1-M3", "W-R2-M3", "winner", "loser"),
    ("L-R2-M4", None, None, "L-R1-M4", "W-R2-M4", "winner", "loser"),
    # Losers Round 3 (2 matches: L-R2 winners face each other)
    ("L-R3-M1", None, None, "L-R2-M1", "L-R2-M2", "winner", "winner"),
    ("L-R3-M2", None, None, "L-R2-M3", "L-R2-M4", "winner", "winner"),
    # Losers Round 4 (2 matches: L-R3 winners vs W-R3 losers)
    ("L-R4-M1", None, None, "L-R3-M1", "W-R3-M1", "winner", "loser"),
    ("L-R4-M2", None, None, "L-R3-M2", "W-R3-M2", "winner", "loser"),
    # Losers Round 5 (Semifinal: 2 L-R4 winners face each other)
    ("L-R5-M1", None, None, "L-R4-M1", "L-R4-M2", "winner", "winner"),
    # Losers Round 6 (Final: L-R5 winner vs W-R4 loser)
    ("L-R6-M1", None, None, "L-R5-M1", "W-R4-M1", "winner", "loser"),
    # ── Grand Final ──────────────────────────────────────────
    ("GF-M1", None, None, "W-R4-M1", "L-R6-M1", "winner", "winner"),
    # GF-M2 (bracket reset) is created dynamically
]


# ── SWISS_BRACKET ───────────────────────────────────────────
# Fixed 66-match survival ladder. Every slot is pre-created with its feeder
# links, so the board can label future slots ("Winner of 266") before the
# matches are played. Records per bucket:
#   R1 16  (0-0)
#   R2 16  U (1-0) / L (0-1)
#   R3 16  U (2-0) / M (1-1) / L (0-2)
#   R4 12  U (2-1) / L (1-2)
#   R5  6  M (2-2)  -> 3-2 qualifies, 2-3 eliminated
# 3 wins qualifies, 3 losses eliminates.
def _build_swiss_bracket() -> list[
    tuple[str, int, Optional[str], Optional[str], Optional[str], Optional[str]]
]:
    """(slot, round_num, p1_source, p1_type, p2_source, p2_type)."""
    bracket: list[
        tuple[str, int, Optional[str], Optional[str], Optional[str], Optional[str]]
    ] = []

    # Round 1 — 16 seeded matches.
    for i in range(1, 17):
        bracket.append((f"SW-R1-M{i}", 1, None, None, None, None))

    # Round 2 — R1 winners (U) and losers (L) paired inside their record.
    for i in range(1, 9):
        a, b = 2 * i - 1, 2 * i
        bracket.append(
            (f"SW-R2-U{i}", 2, f"SW-R1-M{a}", "winner", f"SW-R1-M{b}", "winner")
        )
        bracket.append(
            (f"SW-R2-L{i}", 2, f"SW-R1-M{a}", "loser", f"SW-R1-M{b}", "loser")
        )

    # Round 3 — 2-0 (U), 1-1 (M: R2U losers vs R2L winners), 0-2 (L).
    for i in range(1, 5):
        bracket.append(
            (
                f"SW-R3-U{i}",
                3,
                f"SW-R2-U{2 * i - 1}",
                "winner",
                f"SW-R2-U{2 * i}",
                "winner",
            )
        )
        bracket.append(
            (
                f"SW-R3-L{i}",
                3,
                f"SW-R2-L{2 * i - 1}",
                "loser",
                f"SW-R2-L{2 * i}",
                "loser",
            )
        )
    for i in range(1, 9):
        bracket.append(
            (f"SW-R3-M{i}", 3, f"SW-R2-U{i}", "loser", f"SW-R2-L{i}", "winner")
        )

    # Round 4 — 2-1 (U: R3U losers + R3M winners), 1-2 (L: R3M losers + R3L winners).
    for i in range(1, 5):
        bracket.append(
            (f"SW-R4-U{i}", 4, f"SW-R3-U{i}", "loser", f"SW-R3-M{i}", "winner")
        )
        bracket.append(
            (f"SW-R4-L{i}", 4, f"SW-R3-M{i}", "loser", f"SW-R3-L{i}", "winner")
        )
    bracket.append(
        ("SW-R4-U5", 4, "SW-R3-M5", "winner", "SW-R3-M6", "winner")
    )
    bracket.append(
        ("SW-R4-U6", 4, "SW-R3-M7", "winner", "SW-R3-M8", "winner")
    )
    bracket.append(
        ("SW-R4-L5", 4, "SW-R3-M5", "loser", "SW-R3-M6", "loser")
    )
    bracket.append(
        ("SW-R4-L6", 4, "SW-R3-M7", "loser", "SW-R3-M8", "loser")
    )

    # Round 5 — 2-2 deciders: R4U losers vs R4L winners.
    for i in range(1, 7):
        bracket.append(
            (f"SW-R5-M{i}", 5, f"SW-R4-U{i}", "loser", f"SW-R4-L{i}", "winner")
        )

    return bracket


SWISS_BRACKET = _build_swiss_bracket()


# ── ADVANCEMENT_MAP ─────────────────────────────────────────
# For each source match, maps where winner and loser advance.
# Each value is (destination_match_id, side) or None (eliminated).
ADVANCEMENT_MAP: dict[str, dict[str, tuple[str, int] | None]] = {
    # Winners R1
    "W-R1-M1": {"winner": ("W-R2-M1", 0), "loser": ("L-R1-M1", 0)},
    "W-R1-M2": {"winner": ("W-R2-M1", 1), "loser": ("L-R1-M1", 1)},
    "W-R1-M3": {"winner": ("W-R2-M2", 0), "loser": ("L-R1-M2", 0)},
    "W-R1-M4": {"winner": ("W-R2-M2", 1), "loser": ("L-R1-M2", 1)},
    "W-R1-M5": {"winner": ("W-R2-M3", 0), "loser": ("L-R1-M3", 0)},
    "W-R1-M6": {"winner": ("W-R2-M3", 1), "loser": ("L-R1-M3", 1)},
    "W-R1-M7": {"winner": ("W-R2-M4", 0), "loser": ("L-R1-M4", 0)},
    "W-R1-M8": {"winner": ("W-R2-M4", 1), "loser": ("L-R1-M4", 1)},
    # Winners QF
    "W-R2-M1": {"winner": ("W-R3-M1", 0), "loser": ("L-R2-M1", 1)},
    "W-R2-M2": {"winner": ("W-R3-M1", 1), "loser": ("L-R2-M2", 1)},
    "W-R2-M3": {"winner": ("W-R3-M2", 0), "loser": ("L-R2-M3", 1)},
    "W-R2-M4": {"winner": ("W-R3-M2", 1), "loser": ("L-R2-M4", 1)},
    # Winners SF
    "W-R3-M1": {"winner": ("W-R4-M1", 0), "loser": ("L-R4-M1", 1)},
    "W-R3-M2": {"winner": ("W-R4-M1", 1), "loser": ("L-R4-M2", 1)},
    # Winners Final
    "W-R4-M1": {"winner": ("GF-M1", 0), "loser": ("L-R6-M1", 0)},
    # Losers R1
    "L-R1-M1": {"winner": ("L-R2-M1", 0), "loser": None},
    "L-R1-M2": {"winner": ("L-R2-M2", 0), "loser": None},
    "L-R1-M3": {"winner": ("L-R2-M3", 0), "loser": None},
    "L-R1-M4": {"winner": ("L-R2-M4", 0), "loser": None},
    # Losers R2
    "L-R2-M1": {"winner": ("L-R3-M1", 0), "loser": None},
    "L-R2-M2": {"winner": ("L-R3-M1", 1), "loser": None},
    "L-R2-M3": {"winner": ("L-R3-M2", 0), "loser": None},
    "L-R2-M4": {"winner": ("L-R3-M2", 1), "loser": None},
    # Losers R3
    "L-R3-M1": {"winner": ("L-R4-M1", 0), "loser": None},
    "L-R3-M2": {"winner": ("L-R4-M2", 0), "loser": None},
    # Losers R4
    "L-R4-M1": {"winner": ("L-R5-M1", 0), "loser": None},
    "L-R4-M2": {"winner": ("L-R5-M1", 1), "loser": None},
    # Losers R5 (Semifinal)
    "L-R5-M1": {"winner": ("L-R6-M1", 1), "loser": None},
    # Losers R6 (Final)
    "L-R6-M1": {"winner": ("GF-M1", 1), "loser": None},
    # Grand Final — handled by bracket reset logic
    "GF-M1": {"winner": None, "loser": None},
}


# ── ADVANCEMENT REVERSE MAP ─────────────────────────────────
# Given a destination slot, tells us which source matches feed into it
# and which side/prereq_type they occupy.
ADVANCEMENT_REVERSE_MAP: dict[str, list[tuple[str, str, int]]] = {}
for src, adv in ADVANCEMENT_MAP.items():
    for outcome, dest in adv.items():
        if dest is not None:
            dest_slot, side = dest
            ADVANCEMENT_REVERSE_MAP.setdefault(dest_slot, []).append((src, outcome, side))


class PairingService:
    def __init__(self):
        self.db = get_db_manager()

    async def _get_or_create_event(self, event_type: EventType, season: int) -> Event:
        async with self.db.get_session() as session:
            repo = EventRepository(session)
            event = await repo.get_by_type_and_season(event_type, season)
            if not event:
                event_names = {
                    EventType.OPEN: f"Season {season} Open",
                    EventType.PLAYIN: f"Season {season} Playin",
                    EventType.SWISS: f"Season {season} Swiss",
                    EventType.PLAYOFF: f"Season {season} Playoffs",
                }
                event = await repo.create(
                    name=event_names.get(event_type, f"Season {season} Event"),
                    event_type=event_type,
                    season=season,
                )
                event.status = EventStatus.ACTIVE
                await session.commit()
            return event

    async def pair_open(self, event_id: int, force: bool = False) -> tuple[int, Optional[int], Optional[str]]:
        """Single-round open pairing."""
        if force:
            async with self.db.get_session() as session:
                match_repo = MatchRepository(session)
                unfinished = await match_repo.get_unplayed_by_event(event_id)
                for m in unfinished:
                    await session.delete(m)
                await session.commit()
        created, _, bye, err = await self._pair_open_round(event_id, set())
        return created, bye, err

    async def _pair_open_round(
        self, event_id: int, history: set[tuple[int, int]]
    ) -> tuple[int, set[tuple[int, int]], Optional[int], Optional[str]]:
        """Pair one round of open matches. Pairs eligible players, avoids rematches in `history`.

        Returns (matches_created, updated_history, bye_player_id, error_message).
        """
        async with self.db.get_session() as session:
            match_repo = MatchRepository(session)

            event = await EventRepository(session).get_by_id(event_id)
            if not event:
                return 0, history, None, "Event not found."
            season = event.season
            ps_repo = PlayerSeasonRepository(session)
            all_ps = await ps_repo.get_by_season(season)
            active = [ps for ps in all_ps if ps.hp > 0 and (ps.swiss_seed is None or ps.swiss_seed == 0)]
            active_ids = [ps.player_id for ps in active]

            if len(active_ids) < 2:
                return 0, history, None, "Not enough active players in open stage."

            existing_matches = await match_repo.get_by_event(event_id)
            existing_pairs: set[tuple[int, int]] = set()
            for m in existing_matches:
                if len(m.match_players) >= 2:
                    mps = sorted(m.match_players, key=lambda mp: mp.side)
                    p1, p2 = mps[0].player_id, mps[1].player_id
                    if p1 and p2:
                        existing_pairs.add(tuple(sorted((p1, p2))))

            candidates = active_ids[:]
            random.shuffle(candidates)
            used: set[int] = set()
            pairings: list[tuple[int, int]] = []

            while True:
                pool = [p for p in candidates if p not in used]
                if len(pool) < 2:
                    break
                p1 = pool[0]
                p2 = None
                for c in pool[1:]:
                    pair = tuple(sorted((p1, c)))
                    if pair not in history and pair not in existing_pairs:
                        p2 = c
                        break
                if p2 is None:
                    p2 = pool[1]
                pairings.append((p1, p2))
                history.add(tuple(sorted((p1, p2))))
                used.add(p1)
                used.add(p2)

            for p1, p2 in pairings:
                match = Match(event_id=event_id)
                session.add(match)
                await session.flush()
                session.add(MatchPlayer(match_id=match.id, side=0, player_id=p1))
                session.add(MatchPlayer(match_id=match.id, side=1, player_id=p2))

            await session.commit()
            leftovers = [p for p in candidates if p not in used]
            bye = leftovers[0] if leftovers else None
            return len(pairings), history, bye, None

    async def generate_open(self, season: int) -> tuple[int, int, Optional[str]]:
        """Generate all Open stage matches: 4 matches per player, all at once.

        Each player is paired against 4 different opponents using repeated
        rounds of random pairing with rematch avoidance.

        Returns (total_matches_created, total_players, error_message).
        """
        event = await self._get_or_create_event(EventType.OPEN, season)
        eid = event.id

        async with self.db.get_session() as session:
            match_repo = MatchRepository(session)
            unfinished = await match_repo.get_unplayed_by_event(eid)
            if unfinished:
                return len(unfinished), 0, None  # already generated, just report what exists

        history: set[tuple[int, int]] = set()
        total_matches = 0
        for _ in range(4):
            created, history, bye, err = await self._pair_open_round(eid, history)
            if created == 0:
                break
            total_matches += created

        if total_matches == 0:
            return 0, 0, "Could not create any open matches."

        async with self.db.get_session() as session:
            ps_repo = PlayerSeasonRepository(session)
            all_ps = await ps_repo.get_by_season(season)
            active = len([ps for ps in all_ps if ps.hp > 0 and (ps.swiss_seed is None or ps.swiss_seed == 0)])
            await session.commit()

        return total_matches, active, None

    @staticmethod
    def _swiss_round(match: Match) -> int:
        return match.series_game or 1

    async def _swiss_eligible(self, session, season: int) -> list[int]:
        all_ps = await PlayerSeasonRepository(session).get_by_season(season)
        eligible = [
            ps
            for ps in all_ps
            if (ps.swiss_seed and ps.swiss_seed > 0)
            or (ps.playin_rank and ps.playin_rank > 0)
        ]
        eligible.sort(
            key=lambda ps: (
                0 if ps.swiss_seed and ps.swiss_seed > 0 else 1,
                ps.swiss_seed if ps.swiss_seed and ps.swiss_seed > 0 else 999,
                ps.playin_rank or 999,
                ps.player_id,
            )
        )
        ids: list[int] = []
        seen: set[int] = set()
        for ps in eligible:
            if ps.player_id in seen:
                continue
            seen.add(ps.player_id)
            ids.append(ps.player_id)
            if len(ids) == SWISS_FIELD_SIZE:
                break
        return ids

    async def _create_swiss_graph(self, season: int, eid: int) -> list[Match]:
        """Create the fixed 66-match Swiss ladder, feeder links included."""
        async with self.db.get_session() as session:
            eligible_ids = await self._swiss_eligible(session, season)
            if len(eligible_ids) != SWISS_FIELD_SIZE:
                raise ValueError(
                    f"Swiss needs exactly {SWISS_FIELD_SIZE} players "
                    f"(returning seeds + Playin qualifiers); found "
                    f"{len(eligible_ids)}."
                )

            # Seed pairing: 1 vs 32, 2 vs 31, ...
            seeds: list[tuple[int, int]] = []
            i, j = 0, len(eligible_ids) - 1
            while i < j:
                seeds.append((eligible_ids[i], eligible_ids[j]))
                i += 1
                j -= 1

            created: list[Match] = []
            by_slot: dict[str, Match] = {}

            for slot, round_num, p1_src, p1_type, p2_src, p2_type in SWISS_BRACKET:
                match = Match(
                    event_id=eid,
                    series_game=round_num,
                    bracket_slot=slot,
                    p1_prereq_type=p1_type or "winner",
                    p2_prereq_type=p2_type or "winner",
                )
                if p1_src:
                    match.p1_prereq_id = by_slot[p1_src].id
                if p2_src:
                    match.p2_prereq_id = by_slot[p2_src].id
                session.add(match)
                await session.flush()
                by_slot[slot] = match

                p1_id = p2_id = None
                if round_num == 1:
                    idx = int(slot.rsplit("M", 1)[1]) - 1
                    if idx < len(seeds):
                        p1_id, p2_id = seeds[idx]
                session.add(
                    MatchPlayer(match_id=match.id, side=0, player_id=p1_id)
                )
                session.add(
                    MatchPlayer(match_id=match.id, side=1, player_id=p2_id)
                )
                created.append(match)

            await session.commit()
            return created

    async def generate_swiss_round(
        self, season: int, round_num: Optional[int] = None
    ) -> list[Match]:
        """Ensure the fixed Swiss ladder exists and return one of its rounds.

        The whole 66-match graph is created on the first call; later calls
        simply read the requested round back out of the database.
        """
        event = await self._get_or_create_event(EventType.SWISS, season)
        eid = event.id

        async with self.db.get_session() as session:
            existing = await MatchRepository(session).get_by_event(eid)

        if not existing:
            await self._create_swiss_graph(season, eid)

        if round_num is None:
            round_num = 1
        if round_num < 1 or round_num > 5:
            raise ValueError("Swiss rounds are numbered 1 through 5.")

        async with self.db.get_session() as session:
            all_matches = await MatchRepository(session).get_by_event(eid)
        return [m for m in all_matches if self._swiss_round(m) == round_num]

    async def seed_playoff(self, event_id: int, seeded_ids: list[int]) -> list[int]:
        """Seed players into the playoff bracket.

        Returns the list of seeded match IDs created.
        """
        match_ids = []
        for slot, p1_idx, p2_idx, p1s, p2s, p1t, p2t in PLAYOFF_BRACKET:
            p1_id = seeded_ids[p1_idx] if p1_idx is not None else None
            p2_id = seeded_ids[p2_idx] if p2_idx is not None else None
            async with self.db.get_session() as session:
                slot_match = await self._get_match_by_bracket_slot(session, event_id, slot)
                if slot_match:
                    continue

                p1_prereq_id = None
                p2_prereq_id = None
                if p1s:
                    pm = await self._get_match_by_bracket_slot(session, event_id, p1s)
                    p1_prereq_id = pm.id if pm else None
                if p2s:
                    pm = await self._get_match_by_bracket_slot(session, event_id, p2s)
                    p2_prereq_id = pm.id if pm else None

                match = Match(
                    event_id=event_id,
                    p1_prereq_id=p1_prereq_id,
                    p2_prereq_id=p2_prereq_id,
                    p1_prereq_type=p1t,
                    p2_prereq_type=p2t,
                    bracket_slot=slot,
                    series_game=1,
                )
                session.add(match)
                await session.flush()
                session.add(MatchPlayer(match_id=match.id, side=0, player_id=p1_id))
                session.add(MatchPlayer(match_id=match.id, side=1, player_id=p2_id))
                await session.commit()
                match_ids.append(match.id)
        return match_ids

    async def _get_match_by_bracket_slot(self, session, event_id: int, slot: str) -> Optional[Match]:
        stmt = (
            select(Match)
            .options(selectinload(Match.match_players))
            .where(and_(Match.event_id == event_id, Match.bracket_slot == slot))
        )
        result = await session.execute(stmt)
        return result.scalars().first()

    async def generate_open_pools(self, season: int) -> dict:
        """Generate Open stage matches for a season.

        Each player gets 4 matches against different opponents.
        Returns dict of match_id -> list of MatchPlayer.
        """
        created, players, err = await self.generate_open(season)
        if err:
            raise ValueError(err)

        event = await self._get_or_create_event(EventType.OPEN, season)
        async with self.db.get_session() as session:
            match_repo = MatchRepository(session)
            matches = await match_repo.get_by_event(event.id)
            unplayed = [m for m in matches if not m.completed_at]

            pools = {}
            for m in unplayed:
                mps = sorted(m.match_players, key=lambda mp: mp.side)
                if len(mps) >= 2:
                    pools[m.id] = mps
        return pools 

    async def generate_playoff_bracket(self, season: int) -> list[int]:
        """Select the top 16 by Swiss record and seed the playoff bracket."""
        async with self.db.get_session() as session:
            swiss_event = await EventRepository(session).get_by_type_and_season(
                EventType.SWISS, season
            )
            if not swiss_event:
                raise ValueError("No Swiss event found for this season.")

            match_repo = MatchRepository(session)
            matches = await match_repo.get_by_event(swiss_event.id)

            stats: dict[int, dict[str, int]] = {}
            for match in matches:
                if not match.completed_at:
                    continue
                mps = [
                    mp for mp in sorted(match.match_players, key=lambda mp: mp.side)
                    if mp.player_id is not None
                ]
                if len(mps) == 1:
                    # A Swiss bye counts as a 1-0 win.
                    pid = mps[0].player_id
                    stats.setdefault(pid, {"wins": 0, "losses": 0, "gd": 0})
                    stats[pid]["wins"] += 1
                    stats[pid]["gd"] += 1
                    continue
                if len(mps) < 2:
                    continue
                for mp in mps[:2]:
                    if mp.player_id not in stats:
                        stats[mp.player_id] = {"wins": 0, "losses": 0, "gd": 0}
                    opp = mps[1] if mp.side == 0 else mps[0]
                    if mp.is_winner:
                        stats[mp.player_id]["wins"] += 1
                    else:
                        stats[mp.player_id]["losses"] += 1
                    stats[mp.player_id]["gd"] += (mp.goals or 0) - (opp.goals or 0)

            if len(stats) < 16:
                raise ValueError(
                    "Need at least 16 players with completed Swiss matches; "
                    f"found {len(stats)}."
                )

            ps_repo = PlayerSeasonRepository(session)
            all_ps = await ps_repo.get_by_season(season)
            ps_by_id = {ps.player_id: ps for ps in all_ps}

            ranked = sorted(
                stats,
                key=lambda pid: (
                    -stats[pid]["wins"],
                    stats[pid]["losses"],
                    -stats[pid]["gd"],
                    (ps_by_id[pid].swiss_seed or 999) if pid in ps_by_id else 999,
                    pid,
                ),
            )
            top_16 = ranked[:16]

        event = await self._get_or_create_event(EventType.PLAYOFF, season)
        return await self.seed_playoff(event.id, top_16)
