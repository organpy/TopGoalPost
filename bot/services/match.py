"""Match service for score reporting, BO3 advancement, and bracket propagation."""

from collections import defaultdict
from datetime import datetime
from typing import Optional

from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    Match,
    MatchPlayer,
    PlayerSeason,
    EventType,
    MatchRepository,
    MatchPlayerRepository,
    EventRepository,
    PlayerSeasonRepository,
)
from bot.services.pairing import ADVANCEMENT_MAP


class MatchService:
    def __init__(self):
        self.db = get_db_manager()

    async def report_score(self, match_id: int, p1_goals: int, p2_goals: int, winner_side: int) -> dict:
        """Report a match score and handle post-match logic.

        Args:
            match_id: Match ID
            p1_goals: Goals for player 1 (side 0)
            p2_goals: Goals for player 2 (side 1)
            winner_side: 0 for player 1, 1 for player 2

        Returns:
            dict with match result info
        """
        async with self.db.get_session() as session:
            mp_repo = MatchPlayerRepository(session)
            match_repo = MatchRepository(session)

            p1_mp = await mp_repo.get_by_match_and_side(match_id, 0)
            p2_mp = await mp_repo.get_by_match_and_side(match_id, 1)
            if not p1_mp or not p2_mp:
                return {"error": "Match players not found"}
            if p1_mp.player_id is None or p2_mp.player_id is None:
                return {"error": "Both players must be known before scoring this match."}

            match = await match_repo.get_by_id(match_id)
            if not match:
                return {"error": "Match not found"}

            was_completed = match.completed_at is not None
            old_winner_side = None
            old_winner = match.get_winner()
            if old_winner:
                old_winner_side = old_winner.side

            if winner_side not in (0, 1):
                return {"error": "Winner side must be 0 or 1."}
            if p1_goals < 0 or p2_goals < 0:
                return {"error": "Goals cannot be negative."}
            if p1_goals == p2_goals:
                return {"error": "Ties are not allowed."}
            if (winner_side == 0 and p1_goals < p2_goals) or (
                winner_side == 1 and p2_goals < p1_goals
            ):
                return {"error": "Winner must have at least as many goals as the opponent."}

            await mp_repo.update_score(p1_mp.id, p1_goals)
            await mp_repo.update_score(p2_mp.id, p2_goals)
            await mp_repo.set_winner(match_id, winner_side)
            match.completed_at = datetime.utcnow()
            await session.commit()

        winner_id = p1_mp.player_id if winner_side == 0 else p2_mp.player_id
        loser_id = p2_mp.player_id if winner_side == 0 else p1_mp.player_id

        event = await self._get_event_for_match(match_id)
        playin_advance = None

        if was_completed and event:
            if event.type == EventType.OPEN:
                await self._reset_after_open_edit(event.season)
            elif event.type == EventType.PLAYIN:
                await self._reset_after_playin_edit(
                    event.season, match.series_game or 1
                )
                await self._clear_prereq_descendants(match_id)
            elif event.type == EventType.SWISS:
                await self._reset_after_swiss_edit(event.season, match_id)
            elif event.type == EventType.PLAYOFF:
                await self._reset_playoff_after_edit(match)

        if event and event.type == EventType.OPEN:
            await self.sync_open_stats(event.season)
        elif event and event.type == EventType.PLAYOFF:
            await self._advance_playoff_bo3(match_id)
        elif event and event.type == EventType.PLAYIN:
            from bot.services.playin import PlayinService
            from bot.services.season import SeasonService

            await self._propagate(match_id)
            season = await SeasonService().get_current_season_number()
            if season:
                playin_advance = await PlayinService().try_advance_after_score(season)
        elif event and event.type == EventType.SWISS:
            await self._propagate(match_id)
        else:
            await self._propagate(match_id)

        result = {
            "match_id": match_id,
            "winner_id": winner_id,
            "loser_id": loser_id,
            "score": (p1_goals, p2_goals),
            "edited": was_completed,
            "old_winner_side": old_winner_side,
        }
        if playin_advance:
            result["playin_advance"] = playin_advance
        return result

    async def forfeit(self, match_id: int, loser_id: int) -> Optional[dict]:
        """Forfeit a match. The other player gets a 1-0 win."""
        async with self.db.get_session() as session:
            mp_repo = MatchPlayerRepository(session)
            match_repo = MatchRepository(session)

            match = await match_repo.get_by_id(match_id)
            if not match:
                return None
            if match.completed_at:
                return None

            mps = sorted(match.match_players, key=lambda mp: mp.side)
            if len(mps) < 2:
                return None
            p1_mp, p2_mp = mps[0], mps[1]
            if p1_mp.player_id is None or p2_mp.player_id is None:
                return None

            if p1_mp.player_id == loser_id:
                winner_side = 1
                await mp_repo.update_score(p1_mp.id, 0)
                await mp_repo.update_score(p2_mp.id, 1)
            elif p2_mp.player_id == loser_id:
                winner_side = 0
                await mp_repo.update_score(p1_mp.id, 1)
                await mp_repo.update_score(p2_mp.id, 0)
            else:
                return None

            await mp_repo.set_winner(match_id, winner_side)
            match.completed_at = datetime.utcnow()
            await session.commit()

        event = await self._get_event_for_match(match_id)
        playin_advance = None
        if event and event.type == EventType.PLAYOFF:
            await self._advance_playoff_bo3(match_id)
        elif event and event.type == EventType.PLAYIN:
            from bot.services.playin import PlayinService
            from bot.services.season import SeasonService

            await self._propagate(match_id)
            season = await SeasonService().get_current_season_number()
            if season:
                playin_advance = await PlayinService().try_advance_after_score(season)
        elif event and event.type == EventType.SWISS:
            await self._propagate(match_id)
        elif event and event.type == EventType.OPEN:
            await self.sync_open_stats(event.season)
        else:
            await self._propagate(match_id)

        result = {"match_id": match_id, "forfeited_by": loser_id}
        if playin_advance:
            result["playin_advance"] = playin_advance
        return result

    async def _propagate(
        self, completed_match_id: int, _visited: Optional[set[int]] = None
    ) -> None:
        """Update matches that depend on this completed match.

        Playin byes are represented by one-sided matches. When a prereq fills
        a bye, the bye is completed automatically and its winner is propagated
        one round further.
        """
        visited = _visited or set()
        if completed_match_id in visited:
            return
        visited.add(completed_match_id)

        completed_byes: list[int] = []
        async with self.db.get_session() as session:
            stmt = select(Match).options(
                selectinload(Match.match_players)
            ).where(
                or_(
                    Match.p1_prereq_id == completed_match_id,
                    Match.p2_prereq_id == completed_match_id,
                )
            )
            result = await session.execute(stmt)
            dependent_matches = result.scalars().all()

            completed_match = (
                await session.execute(
                    select(Match)
                    .options(selectinload(Match.match_players))
                    .where(Match.id == completed_match_id)
                )
            ).scalars().first()
            if not completed_match:
                return

            winner_mp = completed_match.get_winner()
            loser_mp = completed_match.get_loser()
            if not winner_mp:
                return

            for dep_match in dependent_matches:
                slots = {mp.side: mp for mp in dep_match.match_players}
                if dep_match.p1_prereq_id == completed_match.id:
                    slot = slots.get(0)
                    if slot and slot.player_id is None:
                        source = (
                            winner_mp
                            if dep_match.p1_prereq_type == "winner"
                            else loser_mp
                        )
                        if source:
                            slot.player_id = source.player_id

                if dep_match.p2_prereq_id == completed_match.id:
                    slot = slots.get(1)
                    if slot and slot.player_id is None:
                        source = (
                            winner_mp
                            if dep_match.p2_prereq_type == "winner"
                            else loser_mp
                        )
                        if source:
                            slot.player_id = source.player_id

                if (
                    dep_match.completed_at is None
                    and dep_match.bracket_slot
                    and "-B" in dep_match.bracket_slot
                ):
                    players = [
                        mp for mp in dep_match.match_players
                        if mp.player_id is not None
                    ]
                    if len(players) == 1:
                        players[0].is_winner = True
                        dep_match.completed_at = datetime.utcnow()
                        completed_byes.append(dep_match.id)

            await session.commit()

        for bye_id in completed_byes:
            await self._propagate(bye_id, visited)

    async def _clear_prereq_descendants(self, match_id: int) -> None:
        """Reset every match fed by ``match_id``, recursively.

        The cards and their bracket slots are kept so Playin can be rescored
        without regenerating the graph.
        """
        visited: set[int] = set()
        frontier = {match_id}

        while frontier:
            async with self.db.get_session() as session:
                stmt = (
                    select(Match)
                    .options(selectinload(Match.match_players))
                    .where(
                        or_(
                            Match.p1_prereq_id.in_(frontier),
                            Match.p2_prereq_id.in_(frontier),
                        )
                    )
                )
                deps = (await session.execute(stmt)).scalars().unique().all()
                next_frontier: set[int] = set()

                for dep in deps:
                    if dep.id in visited:
                        continue
                    visited.add(dep.id)
                    for mp in dep.match_players:
                        if mp.side == 0 and dep.p1_prereq_id in frontier:
                            mp.player_id = None
                        if mp.side == 1 and dep.p2_prereq_id in frontier:
                            mp.player_id = None
                        mp.goals = 0
                        mp.is_winner = False
                    dep.completed_at = None
                    next_frontier.add(dep.id)

                await session.commit()
            frontier = next_frontier

    async def _delete_event_matches(self, season: int, event_type: EventType) -> None:
        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(
                event_type, season
            )
            if not event:
                return
            matches = await MatchRepository(session).get_by_event(event.id)
            for match in matches:
                match.p1_prereq_id = None
                match.p2_prereq_id = None
            await session.flush()
            for match in matches:
                await session.delete(match)
            await session.commit()

    async def _clear_playin_ranks(self, season: int) -> None:
        async with self.db.get_session() as session:
            all_ps = await PlayerSeasonRepository(session).get_by_season(season)
            for ps in all_ps:
                if ps.swiss_seed is None or ps.swiss_seed == 0:
                    ps.playin_rank = None
                    ps.playin_eliminated = False
            await session.commit()

    async def _set_state(self, state: str) -> None:
        from bot.config import Config

        await Config().set_season_state(state)

    async def _reset_after_open_edit(self, season: int) -> None:
        """Open edits can change Playin pools and everything downstream."""
        await self._delete_event_matches(season, EventType.PLAYIN)
        await self._delete_event_matches(season, EventType.SWISS)
        await self._delete_event_matches(season, EventType.PLAYOFF)
        await self._clear_playin_ranks(season)
        await self._set_state("OPEN")

    async def _reset_after_playin_edit(
        self, season: int, round_num: int = 1
    ) -> None:
        # An R1/R2/R3 correction changes who belongs in the conditional cut.
        # Correcting a cut match itself keeps the rest of the cut round intact.
        if round_num < 4:
            async with self.db.get_session() as session:
                event = await EventRepository(session).get_by_type_and_season(
                    EventType.PLAYIN, season
                )
                if event:
                    matches = await MatchRepository(session).get_by_event(event.id)
                    for match in matches:
                        if (match.series_game or 0) >= 4:
                            await session.delete(match)
                    await session.commit()
        await self._delete_event_matches(season, EventType.SWISS)
        await self._delete_event_matches(season, EventType.PLAYOFF)
        await self._clear_playin_ranks(season)
        await self._set_state("PLAYIN")

    async def _reset_after_swiss_edit(self, season: int, match_id: int) -> None:
        """Clear every Swiss slot fed by this match, plus the playoff bracket.

        The fixed ladder is never deleted — its feeder links are what keep the
        board readable — so rescoring just empties the downstream slots.
        """
        await self._clear_prereq_descendants(match_id)
        await self._delete_event_matches(season, EventType.PLAYOFF)
        await self._set_state("SWISS")

    async def _reset_playoff_after_edit(self, match: Match) -> None:
        """Clear the parts of the playoff bracket that depend on this series."""
        slot = match.bracket_slot
        if not slot:
            return

        cleared_slots: set[str] = set()

        async def clear_slot(session, dest_slot: str, incoming_side: Optional[int]):
            if dest_slot in cleared_slots:
                return
            cleared_slots.add(dest_slot)

            matches = (
                await session.execute(
                    select(Match)
                    .options(selectinload(Match.match_players))
                    .where(
                        and_(
                            Match.event_id == match.event_id,
                            Match.bracket_slot == dest_slot,
                        )
                    )
                    .order_by(Match.series_game)
                )
            ).scalars().unique().all()
            if not matches:
                return

            first = matches[0]
            for extra in matches[1:]:
                await session.delete(extra)
            for mp in first.match_players:
                if mp.side == incoming_side:
                    mp.player_id = None
                mp.goals = 0
                mp.is_winner = False
            first.completed_at = None

            adv = ADVANCEMENT_MAP.get(dest_slot, {})
            for outcome in ("winner", "loser"):
                target = adv.get(outcome)
                if target:
                    await clear_slot(session, target[0], target[1])

        async with self.db.get_session() as session:
            current = (
                await session.execute(
                    select(Match)
                    .options(selectinload(Match.match_players))
                    .where(
                        and_(
                            Match.event_id == match.event_id,
                            Match.bracket_slot == slot,
                        )
                    )
                    .order_by(Match.series_game)
                )
            ).scalars().unique().all()
            for extra in current:
                if extra.id != match.id and extra.series_game > match.series_game:
                    await session.delete(extra)

            adv = ADVANCEMENT_MAP.get(slot, {})
            for outcome in ("winner", "loser"):
                target = adv.get(outcome)
                if target:
                    await clear_slot(session, target[0], target[1])

            if slot == "GF-M1":
                gf2 = (
                    await session.execute(
                        select(Match).where(
                            and_(
                                Match.event_id == match.event_id,
                                Match.bracket_slot == "GF-M2",
                            )
                        )
                    )
                ).scalars().all()
                for reset_match in gf2:
                    await session.delete(reset_match)

            await session.commit()

    async def _series_wins(self, slot_letter: str, event_id: int, session) -> tuple[dict[int, int], list[Match]]:
        """Count wins per player across all completed games in a bracket slot."""
        stmt = (
            select(Match)
            .options(selectinload(Match.match_players))
            .where(
                and_(
                    Match.event_id == event_id,
                    Match.bracket_slot == slot_letter,
                    Match.completed_at.isnot(None),
                )
            )
            .order_by(Match.series_game.asc())
        )
        result = await session.execute(stmt)
        matches = result.scalars().all()

        wins: dict[int, int] = defaultdict(int)
        for m in matches:
            winner_mp = m.get_winner()
            if winner_mp and winner_mp.player_id:
                wins[winner_mp.player_id] += 1
        return dict(wins), matches

    async def _next_series_game(self, event_id: int, slot_letter: str, session) -> None:
        """Create the next game in a BO3 series, copying players from previous game."""
        stmt = (
            select(Match)
            .options(selectinload(Match.match_players))
            .where(
                and_(
                    Match.event_id == event_id,
                    Match.bracket_slot == slot_letter,
                )
            )
            .order_by(Match.series_game.desc())
        )
        result = await session.execute(stmt)
        last_match = result.scalars().first()
        if not last_match or len(last_match.match_players) < 2:
            return
        if last_match.series_game >= 3:
            return

        mps = sorted(last_match.match_players, key=lambda mp: mp.side)
        mp0, mp1 = mps[0], mps[1]
        if not mp0 or not mp1 or not mp0.player_id or not mp1.player_id:
            return

        new_match = Match(
            event_id=event_id,
            p1_prereq_id=last_match.p1_prereq_id,
            p2_prereq_id=last_match.p2_prereq_id,
            p1_prereq_type=last_match.p1_prereq_type,
            p2_prereq_type=last_match.p2_prereq_type,
            bracket_slot=slot_letter,
            series_game=last_match.series_game + 1,
        )
        session.add(new_match)
        await session.flush()
        session.add(MatchPlayer(match_id=new_match.id, side=0, player_id=mp0.player_id))
        session.add(MatchPlayer(match_id=new_match.id, side=1, player_id=mp1.player_id))

    async def _advance_playoff_bo3(self, completed_match_id: int) -> None:
        """Handle BO3 playoff bracket advancement.

        1. Count series wins for the slot.
        2. If a player has 2 wins → series decided: propagate winner & loser
           to destination matches using ADVANCEMENT_MAP.
        3. If no one has 2 wins and < 3 games exist → create next game.

        Grand Final (GF-M1) special case: if LR bracket winner (side 1) wins,
        create GF-M2 bracket reset match.
        """
        async with self.db.get_session() as session:
            stmt = (
                select(Match)
                .options(selectinload(Match.match_players))
                .where(Match.id == completed_match_id)
            )
            result = await session.execute(stmt)
            match = result.scalars().first()
            if not match or not match.bracket_slot:
                return

            event_id = match.event_id
            slot = match.bracket_slot
            wins, series_matches = await self._series_wins(slot, event_id, session)

            winner_id: Optional[int] = None
            loser_id: Optional[int] = None
            for pid, wc in wins.items():
                if wc >= 2:
                    winner_id = pid
                    for m in series_matches:
                        for mp in m.match_players:
                            if mp.player_id and mp.player_id != pid:
                                loser_id = mp.player_id
                                break
                        if loser_id is not None:
                            break
                    break

            if winner_id is not None and loser_id is not None:
                adv = ADVANCEMENT_MAP.get(slot)
                if adv is None:
                    await session.commit()
                    return

                # ── Place winner in destination ────────────────
                winner_dest = adv.get("winner")
                if winner_dest is not None:
                    dest_slot, dest_side = winner_dest
                    await self._fill_slot(
                        session, event_id, dest_slot, dest_side, winner_id,
                    )

                # ── Place loser in destination ─────────────────
                loser_dest = adv.get("loser")
                if loser_dest is not None:
                    dest_slot, dest_side = loser_dest
                    await self._fill_slot(
                        session, event_id, dest_slot, dest_side, loser_id,
                    )

                # ── Grand Final bracket reset (GF-M1) ──────────
                if slot == "GF-M1" and series_matches:
                    await self._handle_gf_bracket_reset(
                        session, event_id, winner_id, series_matches,
                    )

                await session.commit()
                return

            max_game = max((m.series_game for m in series_matches), default=0)
            if max_game < 3:
                await self._next_series_game(event_id, slot, session)
                await session.commit()

    async def _fill_slot(
        self, session, event_id: int, dest_slot: str, dest_side: int, player_id: int,
    ) -> None:
        """Fill a player into an unfilled MatchPlayer slot for a given bracket slot."""
        stmt = (
            select(Match)
            .options(selectinload(Match.match_players))
            .where(
                and_(
                    Match.event_id == event_id,
                    Match.bracket_slot == dest_slot,
                    Match.series_game == 1,
                )
            )
        )
        result = await session.execute(stmt)
        dest_match = result.scalars().first()
        if not dest_match:
            return
        for mp in dest_match.match_players:
            if mp.side == dest_side and mp.player_id is None:
                mp.player_id = player_id
                return

    async def _handle_gf_bracket_reset(
        self, session, event_id: int, winner_id: int, series_matches: list[Match],
    ) -> None:
        """If LR bracket champ (side 1) won GF-M1, create GF-M2 bracket reset match."""
        gf_last = series_matches[-1]
        if len(gf_last.match_players) < 2:
            return
        gf_mps = sorted(gf_last.match_players, key=lambda mp: mp.side)
        gf_p1_id = gf_mps[0].player_id  # WB champ
        gf_p2_id = gf_mps[1].player_id  # LB champ
        if winner_id == gf_p2_id:
            gf2_match = Match(
                event_id=event_id,
                p1_prereq_id=gf_last.p1_prereq_id,
                p2_prereq_id=gf_last.p2_prereq_id,
                p1_prereq_type="winner",
                p2_prereq_type="winner",
                bracket_slot="GF-M2",
                series_game=1,
            )
            session.add(gf2_match)
            await session.flush()
            session.add(MatchPlayer(match_id=gf2_match.id, side=0, player_id=gf_p1_id))
            session.add(MatchPlayer(match_id=gf2_match.id, side=1, player_id=gf_p2_id))

    async def _get_event_for_match(self, match_id: int) -> Optional:
        async with self.db.get_session() as session:
            stmt = (
                select(Match)
                .options(selectinload(Match.event))
                .where(Match.id == match_id)
            )
            result = await session.execute(stmt)
            match = result.scalars().first()
            return match.event if match else None

    async def _track_open_stats(
        self,
        match_id: int,
        winner_id: int,
        loser_id: int,
        p1_goals: int,
        p2_goals: int,
        winner_side: int,
        season: int,
    ) -> None:
        """Update PlayerSeason open_wins/open_losses/open_gd/hp after an open match."""
        from bot.db import PlayerSeasonRepository

        async with self.db.get_session() as session:
            ps_repo = PlayerSeasonRepository(session)

            winner_ps = await ps_repo.get(winner_id, season)
            loser_ps = await ps_repo.get(loser_id, season)

            if winner_ps:
                winner_ps.open_wins = (winner_ps.open_wins or 0) + 1
                winner_gd = p1_goals - p2_goals if winner_side == 0 else p2_goals - p1_goals
                winner_ps.open_gd = (winner_ps.open_gd or 0) + winner_gd
                session.add(winner_ps)

            if loser_ps:
                loser_ps.open_losses = (loser_ps.open_losses or 0) + 1
                loser_ps.hp = max(0, 4 - loser_ps.open_losses)
                loser_gd = p2_goals - p1_goals if winner_side == 0 else p1_goals - p2_goals
                loser_ps.open_gd = (loser_ps.open_gd or 0) + loser_gd
                session.add(loser_ps)

            await session.commit()

    async def substitute_player(self, old_discord_id: int, new_discord_id: int, season: int) -> str:
        """Swap a player in all uncompleted matches in a season.

        Args:
            old_discord_id: Discord ID of the player being replaced
            new_discord_id: Discord ID of the player taking over
            season: Season number to scope the substitution

        Returns:
            Status message describing what was done.
        """
        from bot.db import PlayerRepository, PlayerSeasonRepository

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            old_player = await player_repo.get_by_discord_id(old_discord_id)
            new_player = await player_repo.get_by_discord_id(new_discord_id)

            if not old_player:
                return f"No player found with Discord ID {old_discord_id}."
            if not new_player:
                return f"No player found with Discord ID {new_discord_id}."

            mp_repo = MatchPlayerRepository(session)
            old_mps = await mp_repo.get_player_matches(old_player.id)

            swapped = 0
            for mp in old_mps:
                stmt = select(Match).options(selectinload(Match.match_players)).where(Match.id == mp.match_id)
                result = await session.execute(stmt)
                match = result.scalars().first()
                if match and not match.completed_at:
                    mp.player_id = new_player.id
                    session.add(mp)
                    swapped += 1

            if swapped == 0:
                return f"{old_player.name} has no uncompleted matches to substitute."

            ps_repo = PlayerSeasonRepository(session)
            old_ps = await ps_repo.get(player_id=old_player.id, season_number=season)
            new_ps = await ps_repo.get(player_id=new_player.id, season_number=season)
            if old_ps and new_ps:
                new_ps.open_wins = old_ps.open_wins
                new_ps.open_losses = old_ps.open_losses
                new_ps.open_gd = old_ps.open_gd
                new_ps.hp = old_ps.hp
                new_ps.playin_rank = old_ps.playin_rank
                new_ps.swiss_seed = old_ps.swiss_seed
                session.add(new_ps)

            await session.commit()

        return (
            f"Substituted **{old_player.name}** → **{new_player.name}** "
            f"in {swapped} uncompleted match(es)."
        )

    async def remove_player_from_matches(self, discord_id: int, season: int) -> str:
        """Remove a player from all uncompleted matches in a season.

        Clears the player_id from MatchPlayer rows so the match is left without
        that participant. Does NOT modify threads — caller handles that.

        Returns:
            A human-readable result message.
        """
        from bot.db import PlayerRepository

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                return f"No player found with Discord ID {discord_id}."

            mp_repo = MatchPlayerRepository(session)
            mps = await mp_repo.get_player_matches(player.id)
            removed = 0
            for mp in mps:
                stmt = select(Match).options(selectinload(Match.match_players)).where(Match.id == mp.match_id)
                result = await session.execute(stmt)
                match = result.scalars().first()
                if match and not match.completed_at:
                    mp.player_id = None
                    session.add(mp)
                    removed += 1

            if removed == 0:
                return f"{player.name} has no uncompleted matches to remove."

            await session.commit()

        return f"Removed **{player.name}** from {removed} uncompleted match(es)."

    async def delete_player_matches(self, discord_id: int, season: int) -> str:
        """Delete all uncompleted matches involving a player (used by `/player delete_matches`).

        Only matches in OPEN and PLAYIN events are deleted; Swiss/Playoff matches
        are part of bracket or pairing structures and must be handled differently.

        Returns:
            A human-readable result message.
        """
        from bot.db import PlayerRepository

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                return f"No player found with Discord ID {discord_id}."

            mp_repo = MatchPlayerRepository(session)
            mps = await mp_repo.get_unplayed_player_matches(player.id, season=season)

            deleted = 0
            skipped = 0
            for mp in mps:
                stmt = (
                    select(Match)
                    .options(selectinload(Match.event))
                    .where(Match.id == mp.match_id)
                )
                match = (await session.execute(stmt)).scalars().first()
                if not match or match.completed_at:
                    continue
                if match.event and match.event.season != season:
                    continue
                if match.event and match.event.type not in (EventType.OPEN, EventType.PLAYIN):
                    skipped += 1
                    continue
                await session.delete(match)
                deleted += 1

            if deleted == 0 and skipped == 0:
                return f"{player.name} has no uncompleted Open/Playin matches to delete."

            await session.commit()

        msg = f"Deleted {deleted} uncompleted match(es) for **{player.name}**."
        if skipped:
            msg += f" Skipped {skipped} Swiss/Playoff match(es)."
        return msg

    async def forfeit_all_player_matches(
        self, discord_id: int, season: Optional[int] = None
    ) -> str:
        """Forfeit all uncompleted matches for a player.

        Each match gets a 1-0 default score in favor of the opponent. When a
        season is given, only that season's matches are touched.

        Returns:
            A human-readable result message.
        """
        from bot.db import PlayerRepository

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                return f"No player found with Discord ID {discord_id}."

            mp_repo = MatchPlayerRepository(session)
            mps = await mp_repo.get_unplayed_player_matches(player.id, season=season)

            if not mps:
                return f"{player.name} has no uncompleted matches to forfeit."

            forfeited = 0
            for mp in mps:
                match_id = mp.match_id
                stmt = select(Match).options(selectinload(Match.match_players)).where(Match.id == match_id)
                result = await session.execute(stmt)
                match = result.scalars().first()
                if not match or match.completed_at:
                    continue

                opponent_mp = None
                for other in match.match_players:
                    if other.player_id and other.player_id != player.id:
                        opponent_mp = other
                        break

                if not opponent_mp:
                    continue

                winner_side = opponent_mp.side
                p1_goals = 1 if winner_side == 0 else 0
                p2_goals = 1 if winner_side == 1 else 0

                await mp_repo.update_score(mp.id, 0)
                await mp_repo.update_score(opponent_mp.id, 1)
                await mp_repo.set_winner(match_id, winner_side)
                match.completed_at = datetime.utcnow()
                session.add(match)
                forfeited += 1

            if forfeited == 0:
                return f"Could not forfeit any matches for {player.name}."

            await session.commit()

        return f"Forfeited {forfeited} match(es) for <@{discord_id}>."

    async def get_match_history(self, player_id: int, season: int) -> list[dict]:
        """Get completed match history for a player in a season.

        Returns:
            List of dicts with match_id, opponent_name, score, won, round info.
        """
        from bot.db import PlayerRepository

        async with self.db.get_session() as session:
            stmt = (
                select(Match)
                .options(
                    selectinload(Match.match_players)
                    .selectinload(MatchPlayer.player),
                    selectinload(Match.event),
                )
                .where(
                    and_(
                        Match.completed_at.isnot(None),
                        Match.event.has(season=season),
                    )
                )
                .order_by(Match.completed_at.desc())
            )
            result = await session.execute(stmt)
            all_matches = result.scalars().unique().all()

            matches = []
            for match in all_matches:
                mps = sorted(match.match_players, key=lambda mp: mp.side)
                if len(mps) < 2:
                    continue
                mp_entry = next((mp for mp in mps if mp.player_id == player_id), None)
                if not mp_entry:
                    continue
                opponent_mp = mps[1] if mps[0].player_id == player_id else mps[0]
                opponent_name = opponent_mp.player.name if opponent_mp.player and opponent_mp.player_id else "Unknown"
                my_score = mp_entry.goals
                opp_score = opponent_mp.goals
                won = mp_entry.is_winner
                stage = match.event.type.value if match.event else "?"
                slot = match.bracket_slot or ""
                series = match.series_game

                matches.append({
                    "match_id": match.id,
                    "stage": stage.upper(),
                    "slot": slot,
                    "series_game": series,
                    "opponent": opponent_name,
                    "my_score": my_score,
                    "opp_score": opp_score,
                    "won": won,
                })

            return matches

    async def sync_open_stats(self, season: int) -> dict:
        """Rebuild PlayerSeason open_* fields from completed OPEN matches."""
        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(EventType.OPEN, season)
            if not event:
                return {"error": "No OPEN event for this season."}

            ps_all = (
                await session.execute(
                    select(PlayerSeason).where(PlayerSeason.season_number == season)
                )
            ).scalars().all()

            for ps in ps_all:
                if ps.swiss_seed:
                    continue
                ps.open_wins = 0
                ps.open_losses = 0
                ps.open_gd = 0
                ps.hp = 4

            stmt = (
                select(MatchPlayer)
                .join(Match)
                .where(
                    and_(
                        Match.event_id == event.id,
                        Match.completed_at.isnot(None),
                    )
                )
            )
            mps = (await session.execute(stmt)).scalars().all()

            for mp in mps:
                if not mp.player_id:
                    continue
                ps = next((p for p in ps_all if p.player_id == mp.player_id), None)
                if not ps or ps.swiss_seed:
                    continue
                if mp.is_winner:
                    ps.open_wins = (ps.open_wins or 0) + 1
                else:
                    ps.open_losses = (ps.open_losses or 0) + 1
                    ps.hp = max(0, 4 - ps.open_losses)

            for mp in mps:
                if not mp.player_id:
                    continue
                ps = next((p for p in ps_all if p.player_id == mp.player_id), None)
                if not ps or ps.swiss_seed:
                    continue
                opp_goals = 0
                for omp in mps:
                    if omp.match_id == mp.match_id and omp.side != mp.side:
                        opp_goals = omp.goals or 0
                ps.open_gd = (ps.open_gd or 0) + (mp.goals or 0) - opp_goals

            await session.commit()

            unseeded = [ps for ps in ps_all if not ps.swiss_seed]
            win_dist = {w: sum(1 for ps in unseeded if (ps.open_wins or 0) == w) for w in range(5)}

        return {
            "ok": True,
            "players_synced": len(unseeded),
            "win_distribution": win_dist,
        }

    async def list_incomplete_matches(
        self,
        season: Optional[int] = None,
        include_completed: bool = False,
        *,
        stage: Optional[str] = None,
        status: Optional[str] = None,
        round_num: Optional[int] = None,
        player: Optional[str] = None,
    ) -> list[dict]:
        """List matches for admin tooling.

        No fixed cap is used, so old completed matches are reachable from
        autocomplete and ``/list_matches``.
        """
        from bot.services.season import SeasonService

        if season is None:
            season = await SeasonService().get_current_season_number()
        if not season:
            return []

        status_key = (status or "").lower()
        if status_key in ("completed", "complete", "done", "finished"):
            include_completed = True
        elif status_key in ("active", "pending", "unplayed", "open"):
            include_completed = False

        stage_key = (stage or "").upper()
        player_key = (player or "").lower()

        async with self.db.get_session() as session:
            stmt = (
                select(Match)
                .options(
                    selectinload(Match.match_players).selectinload(MatchPlayer.player),
                    selectinload(Match.event),
                )
                .where(Match.event.has(season=season))
                .order_by(Match.id.desc())
            )
            result = await session.execute(stmt)
            matches = result.scalars().unique().all()

            out = []
            for match in matches:
                if not include_completed and match.completed_at is not None:
                    continue
                if status_key in ("completed", "complete", "done", "finished"):
                    if match.completed_at is None:
                        continue
                if stage_key:
                    event_type = (
                        match.event.type.value.upper() if match.event else ""
                    )
                    if event_type != stage_key:
                        continue
                if round_num is not None and (match.series_game or 1) != round_num:
                    continue

                mps = sorted(match.match_players, key=lambda mp: mp.side)
                if len(mps) < 2:
                    continue
                if not mps[0].player_id or not mps[1].player_id:
                    continue
                label = self._format_match_label(match, mps)
                if match.completed_at:
                    label += " [completed — edit]"
                if player_key and player_key not in label.lower():
                    continue
                out.append({"match_id": match.id, "label": label})

            # Active matches stay first; completed matches follow newest-first.
            active_ids = {m.id for m in matches if m.completed_at is None}
            out.sort(
                key=lambda item: (
                    item["match_id"] not in active_ids,
                    -item["match_id"],
                )
            )
            return out

    @staticmethod
    def _format_match_label(match: Match, mps: list[MatchPlayer]) -> str:
        """Build a human-readable label for autocomplete / list commands."""
        p1_name = mps[0].player.name if mps[0].player else f"P{mps[0].player_id}"
        p2_name = mps[1].player.name if mps[1].player else f"P{mps[1].player_id}"
        stage = match.event.type.value.upper() if match.event else "?"
        slot = match.bracket_slot or ""
        game = f" G{match.series_game}" if match.series_game and match.series_game > 1 else ""
        slot_part = f" {slot}{game}" if slot else ""
        if match.completed_at:
            p1_goals = mps[0].goals or 0
            p2_goals = mps[1].goals or 0
            players = f"{p1_name} {p1_goals}-{p2_goals} {p2_name}"
        else:
            players = f"{p1_name} vs {p2_name}"
        return f"#{match.id} {stage}{slot_part}: {players}"

    async def format_result_summary(self, match_id: int) -> str:
        """One-line summary of a scored match, with BO3 series detail.

        Example:
            Match 361 · Playoff W-R1-M3 · organ 5-0 dummy · Series 1-0 · G1 5-0, G2 3-2
        """
        async with self.db.get_session() as session:
            stmt = (
                select(Match)
                .options(
                    selectinload(Match.match_players).selectinload(MatchPlayer.player),
                    selectinload(Match.event),
                )
                .where(Match.id == match_id)
            )
            match = (await session.execute(stmt)).scalars().first()
            if not match:
                return f"Match {match_id}"

            mps = sorted(match.match_players, key=lambda mp: mp.side)
            if len(mps) < 2:
                return f"Match {match.id}"
            p1_name = mps[0].player.name if mps[0].player else "TBA"
            p2_name = mps[1].player.name if mps[1].player else "TBA"
            p1_goals = mps[0].goals or 0
            p2_goals = mps[1].goals or 0

            stage = match.event.type.value.title() if match.event else "?"
            header = f"{stage} {match.bracket_slot}" if match.bracket_slot else stage
            parts = [
                f"Match {match.id}",
                header,
                f"{p1_name} {p1_goals}-{p2_goals} {p2_name}",
            ]

            if not match.event or match.event.type != EventType.PLAYOFF:
                return " · ".join(parts)

            games = (
                await session.execute(
                    select(Match)
                    .options(selectinload(Match.match_players))
                    .where(
                        and_(
                            Match.event_id == match.event_id,
                            Match.bracket_slot == match.bracket_slot,
                            Match.completed_at.isnot(None),
                        )
                    )
                    .order_by(Match.series_game.asc())
                )
            ).scalars().unique().all()

        if not games:
            return " · ".join(parts)

        player_ids = [mp.player_id for mp in mps]
        wins = {pid: 0 for pid in player_ids if pid}
        game_parts = []
        for game in games:
            gmps = sorted(game.match_players, key=lambda mp: mp.side)
            if len(gmps) < 2:
                continue
            winner_mp = next((mp for mp in gmps if mp.is_winner), None)
            if winner_mp and winner_mp.player_id in wins:
                wins[winner_mp.player_id] += 1
            game_parts.append(f"G{game.series_game or 1} {gmps[0].goals or 0}-{gmps[1].goals or 0}")

        if len(player_ids) == 2 and all(player_ids):
            parts.append(f"Series {wins[player_ids[0]]}-{wins[player_ids[1]]}")
        if game_parts:
            parts.append(", ".join(game_parts))
        return " · ".join(parts)
