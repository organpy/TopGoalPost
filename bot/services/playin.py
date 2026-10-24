"""Stepped Playin ladder.

Open results decide where a player enters the ladder:

* 4 wins  -> automatic Swiss qualifier
* 3 wins  -> best by Open GD may auto-qualify; the rest start in R3
* 2 wins  -> starts in R2
* 1 win   -> starts in R1
* 0 wins  -> eliminated

R1 winners join the 2-win players in R2. R2 winners join the 3-win players
in R3. R3 winners normally qualify for Swiss; if they overproduce, the worst
R3 winners play a conditional CUT round. Byes count as a win and are always
given to the highest-ranked entrant first.

The complete R1/R2/R3 graph is created when Playins start. Later rounds are
never generated manually: scoring a match fills its winner into the already
existing next-round card.

The target is ``32 - returning_seeds``. If R3 produces too many qualifiers, a
single conditional CUT round is created between the lowest-ranked R3 winners.
It only exists when overproduction actually happens.
"""

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select, and_

from bot.db import (
    get_db_manager,
    PlayerSeason,
    Match,
    MatchPlayer,
    EventType,
    EventStatus,
    PlayerSeasonRepository,
    EventRepository,
    MatchRepository,
)
from bot.rendering import prerender

TIER_KEYS = {1: "one_wins", 2: "two_wins", 3: "three_wins"}
SWISS_FIELD_SIZE = 32
CUT_ROUND = 4


def _ceil_half(n: int) -> int:
    return (n + 1) // 2


def compute_playin_math(
    all_ps: list[PlayerSeason], target: Optional[int] = None
) -> dict[str, Any]:
    """Return pool sizes and the pre-computed ladder shape.

    ``target`` defaults to ``32 - returning_seeds``. The best three-win
    players are taken directly until the base ladder reaches the target;
    the worst three-win players enter R3.
    """
    returning_seeds = sum(
        1 for ps in all_ps if ps.swiss_seed is not None and ps.swiss_seed > 0
    )
    if target is None:
        target = max(0, SWISS_FIELD_SIZE - returning_seeds)
    unseeded = [
        ps
        for ps in all_ps
        if ps.swiss_seed is None or ps.swiss_seed == 0
    ]

    def sort_pool(ps_list: list[PlayerSeason]) -> list[PlayerSeason]:
        return sorted(
            ps_list,
            key=lambda ps: (-(ps.open_gd or 0), ps.player_id),
        )

    one_wins = sort_pool([ps for ps in unseeded if (ps.open_wins or 0) == 1])
    two_wins = sort_pool([ps for ps in unseeded if (ps.open_wins or 0) == 2])
    three_wins = sort_pool([ps for ps in unseeded if (ps.open_wins or 0) == 3])
    four_wins = sort_pool([ps for ps in unseeded if (ps.open_wins or 0) >= 4])
    auto_eliminated = [ps for ps in unseeded if (ps.open_wins or 0) <= 0]

    r2_advancers = _ceil_half(len(one_wins))
    r3_advancers = _ceil_half(len(two_wins) + r2_advancers)

    direct_three_wins: list[PlayerSeason] = []
    base_qualifiers = len(four_wins) + _ceil_half(
        len(three_wins) + r3_advancers
    )
    for count in range(len(three_wins) + 1):
        remaining = len(three_wins) - count
        total = (
            len(four_wins)
            + count
            + _ceil_half(remaining + r3_advancers)
        )
        if total <= target:
            direct_three_wins = three_wins[:count]
            base_qualifiers = total
        else:
            # Adding a direct three-win player never lowers the total by more
            # than one, so the first over-target count is the end.
            break

    remaining_three_wins = three_wins[len(direct_three_wins):]
    r3_entrants = len(remaining_three_wins) + r3_advancers
    r3_winners = _ceil_half(r3_entrants)
    entrants = {
        1: len(one_wins),
        2: len(two_wins) + r2_advancers,
        3: r3_entrants,
    }
    expected_matches = {rnd: entrants[rnd] // 2 for rnd in TIER_KEYS}
    byes = {rnd: entrants[rnd] % 2 for rnd in TIER_KEYS}
    over_by = max(0, base_qualifiers - target)
    available = sum(1 for ps in unseeded if (ps.open_wins or 0) >= 1)

    return {
        "returning_seeds": returning_seeds,
        "target": target,
        "four_wins": four_wins,
        "direct_three_wins": direct_three_wins,
        "remaining_three_wins": remaining_three_wins,
        "auto_eliminated": auto_eliminated,
        "one_wins": one_wins,
        "two_wins": two_wins,
        "three_wins": three_wins,
        "pools": {1: one_wins, 2: two_wins, 3: remaining_three_wins},
        "entrants": entrants,
        "expected_matches": expected_matches,
        "byes": byes,
        "base_qualifiers": base_qualifiers,
        "qualifiers": min(target, available),
        "over_by": over_by,
        "short_by": max(0, target - base_qualifiers),
        "cut_matches": over_by if r3_winners >= 2 * over_by else 0,
        "cut_players": 2 * over_by if r3_winners >= 2 * over_by else 0,
    }


@dataclass(frozen=True)
class _Entrant:
    player_id: Optional[int] = None
    prereq_id: Optional[int] = None


class PlayinService:
    def __init__(self):
        self.db = get_db_manager()

    async def _load_math(self, season: int) -> dict[str, Any]:
        async with self.db.get_session() as session:
            all_ps = await PlayerSeasonRepository(session).get_by_season(season)
        return compute_playin_math(all_ps)

    async def generate_playins(self, season: int) -> dict[str, Any]:
        """Create the complete Playin graph and return its shape."""
        math = await self._load_math(season)
        event = await self._get_or_create_playin_event(season)
        eid = event.id

        async with self.db.get_session() as session:
            existing = await MatchRepository(session).get_by_event(eid)
            if existing:
                by_round = defaultdict(int)
                bye_by_round = defaultdict(int)
                for match in existing:
                    by_round[match.series_game] += 1
                    if self._is_bye(match):
                        bye_by_round[match.series_game] += 1
                return {
                    "round1_matches": by_round.get(1, 0),
                    "round1_byes": bye_by_round.get(1, 0),
                    "first_round": 1 if by_round.get(1, 0) else None,
                    "matches_by_round": dict(by_round),
                    "byes_by_round": dict(bye_by_round),
                    "expected_matches": math["expected_matches"],
                    "byes": math["byes"],
                    "layout": math,
                    "existing": True,
                }

            known_byes: list[PlayerSeason] = []
            result_tokens: list[_Entrant] = []
            matches_by_round: dict[int, int] = {}
            byes_by_round: dict[int, int] = {}

            for rnd in (1, 2, 3):
                pool = list(math["pools"][rnd]) + known_byes
                # Higher win groups always enter first; byes go to the top of
                # the highest group, then by goal difference.
                pool.sort(
                    key=lambda ps: (
                        -(ps.open_wins or 0),
                        -(ps.open_gd or 0),
                        ps.player_id,
                    )
                )
                entrants = [
                    _Entrant(player_id=ps.player_id) for ps in pool
                ] + result_tokens

                if len(entrants) != math["entrants"][rnd]:
                    raise ValueError(
                        f"Playin R{rnd} entrant mismatch: "
                        f"expected {math['entrants'][rnd]}, got {len(entrants)}."
                    )

                next_byes: list[PlayerSeason] = []
                next_tokens: list[_Entrant] = []
                created = 0
                byes_created = 0

                if len(entrants) % 2:
                    bye_entrant = entrants[0]
                    entrants = entrants[1:]
                    bye_match = await self._add_bye(
                        session, eid, rnd, f"P-R{rnd}-B1", bye_entrant
                    )
                    byes_created += 1
                    if bye_entrant.player_id is not None:
                        ps = next(
                            p for p in pool if p.player_id == bye_entrant.player_id
                        )
                        next_byes.append(ps)
                    else:
                        next_tokens.append(_Entrant(prereq_id=bye_match.id))

                for i in range(len(entrants) // 2):
                    side0 = entrants[i]
                    side1 = entrants[-1 - i]
                    match = await self._add_match(
                        session,
                        eid,
                        rnd,
                        f"P-R{rnd}-M{i + 1}",
                        side0,
                        side1,
                    )
                    next_tokens.append(_Entrant(prereq_id=match.id))
                    created += 1

                matches_by_round[rnd] = created
                byes_by_round[rnd] = byes_created
                known_byes = next_byes
                result_tokens = next_tokens

            await session.commit()

        if not any(matches_by_round.values()) and not any(byes_by_round.values()):
            await self.finish_playins(season)

        return {
            "round1_matches": matches_by_round.get(1, 0),
            "round1_byes": byes_by_round.get(1, 0),
            "first_round": 1 if matches_by_round.get(1, 0) or byes_by_round.get(1, 0) else None,
            "matches_by_round": matches_by_round,
            "byes_by_round": byes_by_round,
            "expected_matches": math["expected_matches"],
            "byes": math["byes"],
            "layout": math,
            "existing": False,
        }

    async def generate_round(self, season: int, round_num: int) -> tuple[int, int]:
        """Compatibility wrapper.

        Playin rounds are all created by :meth:`generate_playins`. Calling this
        after that simply reports the existing round rather than creating a
        second batch.
        """
        if round_num not in TIER_KEYS:
            raise ValueError("Playin round must be 1, 2, or 3.")
        event = await self._get_or_create_playin_event(season)
        async with self.db.get_session() as session:
            existing = await MatchRepository(session).get_by_event(event.id)
        if not existing:
            await self.generate_playins(season)
            async with self.db.get_session() as session:
                existing = await MatchRepository(session).get_by_event(event.id)
        round_matches = [m for m in existing if m.series_game == round_num]
        return len(round_matches), sum(1 for m in round_matches if self._is_bye(m))

    async def try_advance_after_score(self, season: int) -> Optional[dict[str, Any]]:
        """Create the conditional cut round, then finalize when complete."""
        event = await self._get_or_create_playin_event(season)
        async with self.db.get_session() as session:
            matches = await MatchRepository(session).get_by_event(event.id)

        if matches and any(match.completed_at is None for match in matches):
            return None

        has_cut = any(
            (match.series_game or 0) >= CUT_ROUND for match in matches
        )
        if not has_cut:
            created = await self._create_cut_round(season)
            if created:
                return {
                    "action": "cut",
                    "round": CUT_ROUND,
                    "matches": created,
                }

        if not await self._playins_finalized(season):
            await self.finish_playins(season)
            return {"action": "finished"}
        return None

    async def _create_cut_round(self, season: int) -> int:
        """Create cut matches only when R3 produces too many qualifiers.

        Returns the number of cut matches created. A zero result means either
        there is no overproduction or the rare case where one cut round cannot
        resolve it (the caller then falls back to ranking).
        """
        async with self.db.get_session() as session:
            all_ps = await PlayerSeasonRepository(session).get_by_season(season)
            math = compute_playin_math(all_ps)
            event = await self._get_or_create_playin_event(season)
            matches = await MatchRepository(session).get_by_event(event.id)

            if any((m.series_game or 0) >= CUT_ROUND for m in matches):
                return 0

            ps_by_id = {ps.player_id: ps for ps in all_ps}
            r3_winners = [
                ps_by_id[winner.player_id]
                for match in matches
                if match.series_game == 3
                for winner in [match.get_winner()]
                if winner and winner.player_id in ps_by_id
            ]
            direct_ids = {
                ps.player_id for ps in math["four_wins"]
            } | {
                ps.player_id for ps in math["direct_three_wins"]
            }
            candidates = direct_ids | {
                ps.player_id for ps in r3_winners
            }
            target = math["target"]
            excess = len(candidates) - target
            if excess <= 0 or len(r3_winners) < 2 * excess:
                return 0

            playin_wins: dict[int, int] = defaultdict(int)
            for match in matches:
                winner = match.get_winner()
                if winner and winner.player_id is not None:
                    playin_wins[winner.player_id] += 1

            r3_winners.sort(
                key=lambda ps: (
                    -(ps.open_wins or 0),
                    -playin_wins.get(ps.player_id, 0),
                    -(ps.open_gd or 0),
                    ps.player_id,
                )
            )
            cut_pool = r3_winners[len(r3_winners) - 2 * excess:]
            random.shuffle(cut_pool)

            for index in range(excess):
                match = Match(
                    event_id=event.id,
                    series_game=CUT_ROUND,
                    bracket_slot=f"P-CUT-M{index + 1}",
                )
                session.add(match)
                await session.flush()
                session.add(
                    MatchPlayer(
                        match_id=match.id,
                        side=0,
                        player_id=cut_pool[2 * index].player_id,
                    )
                )
                session.add(
                    MatchPlayer(
                        match_id=match.id,
                        side=1,
                        player_id=cut_pool[2 * index + 1].player_id,
                    )
                )

            await session.commit()
            return excess

    async def _playins_finalized(self, season: int) -> bool:
        async with self.db.get_session() as session:
            all_ps = await PlayerSeasonRepository(session).get_by_season(season)
            participants = [
                ps
                for ps in all_ps
                if (ps.open_wins or 0) >= 1
                and (ps.swiss_seed is None or ps.swiss_seed == 0)
            ]
            if not participants:
                return True
            return all(
                ps.playin_rank is not None or ps.playin_eliminated
                for ps in participants
            )

    async def finish_playins(self, season: int) -> None:
        """Rank exactly the target number of qualifiers and eliminate the rest."""
        # Manual finalization must not bypass the conditional cut.
        created = await self._create_cut_round(season)
        if created:
            raise ValueError(
                f"Playin overproduction: {created} cut match(es) created. "
                "Score them, then finish again."
            )

        async with self.db.get_session() as session:
            all_ps = await PlayerSeasonRepository(session).get_by_season(season)
            math = compute_playin_math(all_ps)
            event = await self._get_or_create_playin_event(season)
            matches = await MatchRepository(session).get_by_event(event.id)

            incomplete = [m for m in matches if m.completed_at is None]
            if incomplete:
                raise ValueError(
                    f"Playins are not complete: {len(incomplete)} match(es) remain."
                )

            ps_by_id = {ps.player_id: ps for ps in all_ps}
            participants = [
                ps
                for ps in all_ps
                if (ps.open_wins or 0) >= 1
                and (ps.swiss_seed is None or ps.swiss_seed == 0)
            ]
            participant_ids = {ps.player_id for ps in participants}
            target = min(math["target"], len(participant_ids))

            qualifier_ids: set[int] = {
                ps.player_id for ps in math["four_wins"]
            } | {
                ps.player_id for ps in math["direct_three_wins"]
            }
            has_cut = any(
                (match.series_game or 0) >= CUT_ROUND for match in matches
            )
            r3_winner_ids: set[int] = set()
            cut_player_ids: set[int] = set()
            cut_winner_ids: set[int] = set()
            for match in matches:
                winner = match.get_winner()
                winner_id = (
                    winner.player_id if winner and winner.player_id else None
                )
                if match.series_game == 3 and winner_id is not None:
                    r3_winner_ids.add(winner_id)
                elif match.series_game == CUT_ROUND:
                    for mp in match.match_players:
                        if mp.player_id is not None:
                            cut_player_ids.add(mp.player_id)
                    if winner_id is not None:
                        cut_winner_ids.add(winner_id)

            if has_cut:
                # R3 winners who skipped the cut advance directly; the cut
                # pool is represented only by its winners.
                qualifier_ids |= r3_winner_ids - cut_player_ids
                qualifier_ids |= cut_winner_ids
            else:
                qualifier_ids |= r3_winner_ids

            # Count Playin wins so a bye and a played win rank the same.
            playin_wins: dict[int, int] = defaultdict(int)
            for match in matches:
                winner = match.get_winner()
                if winner and winner.player_id is not None:
                    playin_wins[winner.player_id] += 1

            four_ids = {ps.player_id for ps in math["four_wins"]}
            direct_ids = {
                ps.player_id for ps in math["direct_three_wins"]
            }

            def rank_key(ps: PlayerSeason) -> tuple:
                if ps.player_id in four_ids:
                    tier = 0
                elif ps.player_id in direct_ids:
                    tier = 1
                else:
                    tier = 2
                return (
                    tier,
                    -(ps.open_wins or 0),
                    -playin_wins.get(ps.player_id, 0),
                    -(ps.open_gd or 0),
                    ps.player_id,
                )

            ordered = [
                ps_by_id[pid]
                for pid in qualifier_ids
                if pid in ps_by_id
            ]
            ordered.sort(key=rank_key)

            # A single cut round normally resolves overproduction. If it could
            # not, fall back to ranking the candidates.
            if len(ordered) > target:
                ordered = ordered[:target]

            if len(ordered) < target:
                already = {ps.player_id for ps in ordered}
                fillers = [
                    ps for ps in participants
                    if ps.player_id not in already
                ]
                fillers.sort(
                    key=lambda ps: (
                        -(ps.open_wins or 0),
                        -playin_wins.get(ps.player_id, 0),
                        -(ps.open_gd or 0),
                        ps.player_id,
                    )
                )
                ordered.extend(fillers[: target - len(ordered)])

            qualifier_set = {ps.player_id for ps in ordered}
            for ps in participants:
                if ps.player_id in qualifier_set:
                    ps.playin_eliminated = False
                else:
                    ps.playin_eliminated = True
                    ps.playin_rank = None

            # Zero-win players are out before the ladder starts.
            for ps in math["auto_eliminated"]:
                ps.playin_eliminated = True
                ps.playin_rank = None

            for rank, ps in enumerate(ordered, start=1):
                ps.playin_rank = rank
                ps.playin_eliminated = False

            await session.commit()

        await prerender("PLAYIN", season)

    # ── graph helpers ───────────────────────────────────────

    async def _add_match(
        self,
        session,
        event_id: int,
        round_num: int,
        slot: str,
        side0: _Entrant,
        side1: _Entrant,
    ) -> Match:
        match = Match(
            event_id=event_id,
            series_game=round_num,
            bracket_slot=slot,
        )
        if side0.prereq_id is not None:
            match.p1_prereq_id = side0.prereq_id
            match.p1_prereq_type = "winner"
        if side1.prereq_id is not None:
            match.p2_prereq_id = side1.prereq_id
            match.p2_prereq_type = "winner"

        session.add(match)
        await session.flush()
        session.add(
            MatchPlayer(match_id=match.id, side=0, player_id=side0.player_id)
        )
        session.add(
            MatchPlayer(match_id=match.id, side=1, player_id=side1.player_id)
        )
        return match

    async def _add_bye(
        self,
        session,
        event_id: int,
        round_num: int,
        slot: str,
        entrant: _Entrant,
    ) -> Match:
        match = Match(
            event_id=event_id,
            series_game=round_num,
            bracket_slot=slot,
        )
        if entrant.prereq_id is not None:
            match.p1_prereq_id = entrant.prereq_id
            match.p1_prereq_type = "winner"

        session.add(match)
        await session.flush()

        mp = MatchPlayer(
            match_id=match.id,
            side=0,
            player_id=entrant.player_id,
            is_winner=entrant.player_id is not None,
        )
        session.add(mp)
        session.add(MatchPlayer(match_id=match.id, side=1, player_id=None))
        if entrant.player_id is not None:
            match.completed_at = datetime.utcnow()
        return match

    @staticmethod
    def _is_bye(match: Match) -> bool:
        return bool(match.bracket_slot and "-B" in match.bracket_slot)

    async def _get_or_create_playin_event(self, season: int):
        async with self.db.get_session() as session:
            repo = EventRepository(session)
            event = await repo.get_by_type_and_season(EventType.PLAYIN, season)
            if not event:
                event = await repo.create(
                    name=f"Season {season} Playin",
                    event_type=EventType.PLAYIN,
                    season=season,
                )
                event.status = EventStatus.ACTIVE
                await session.commit()
            return event
