"""Unit tests for bot services."""

from collections import Counter
from unittest.mock import AsyncMock

import pytest

from bot.db import (
    MatchRepository, MatchPlayerRepository, PlayerRepository,
    EventRepository, PlayerSeasonRepository, SettingRepository,
    EventType, EventStatus, SeasonStatus,
)
from bot.db.models import Season, PlayerSeason, Match, MatchPlayer


# ── SeasonService ──────────────────────────────────────────

class TestSeasonService:
    """Tests for SeasonService (advance_state sequential enforcement)."""

    async def test_advance_sequential(self, db):
        from bot.services.season import SeasonService
        svc = SeasonService()

        # OFFSEASON → SIGNUP → OPEN → PLAYIN → SWISS → PLAYOFF
        assert await svc.advance_state("SIGNUP") is True
        assert await svc.advance_state("OPEN") is True
        assert await svc.advance_state("PLAYIN") is True
        assert await svc.advance_state("SWISS") is True
        assert await svc.advance_state("PLAYOFF") is True

    async def test_skip_stage_fails(self, db):
        from bot.services.season import SeasonService
        svc = SeasonService()

        assert await svc.advance_state("OPEN") is False
        assert await svc.get_state() == "OFFSEASON"

    async def test_skip_stage_with_force(self, db):
        from bot.services.season import SeasonService
        svc = SeasonService()

        assert await svc.advance_state("OPEN", force=True) is True
        assert await svc.get_state() == "OPEN"

    async def test_advance_backwards_fails(self, db):
        from bot.services.season import SeasonService
        svc = SeasonService()

        await svc.advance_state("SIGNUP")
        await svc.advance_state("OPEN")
        assert await svc.advance_state("SIGNUP") is False
        assert await svc.advance_state("OFFSEASON") is False
        assert await svc.advance_state("SIGNUP", force=True) is True
        assert await svc.get_state() == "SIGNUP"

    async def test_same_state_is_noop(self, db):
        from bot.services.season import SeasonService
        svc = SeasonService()

        assert await svc.advance_state("OFFSEASON") is True

    async def test_invalid_state_fails(self, db):
        from bot.services.season import SeasonService
        svc = SeasonService()

        assert await svc.advance_state("INVALID") is False
        assert await svc.advance_state("INVALID", force=True) is False

    async def test_playoff_closes_to_offseason(self, db):
        from bot.services.season import SeasonService
        svc = SeasonService()

        for state in ("SIGNUP", "OPEN", "PLAYIN", "SWISS", "PLAYOFF", "OFFSEASON"):
            assert await svc.advance_state(state) is True
        assert await svc.advance_state("SIGNUP") is True

    async def test_reset_path_rewinds_stage(self, db):
        from bot.services.match import MatchService
        from bot.services.season import SeasonService
        svc = SeasonService()

        await svc.advance_state("SWISS", force=True)
        await MatchService()._set_state("OPEN")
        assert await svc.get_state() == "OPEN"

    async def test_transition_error_message(self):
        from bot.services.season import transition_error

        assert transition_error("SIGNUP", "OPEN") is None
        assert "Next allowed state is OPEN" in transition_error("SIGNUP", "SWISS")
        assert "Unknown state" in transition_error("SIGNUP", "NOPE")

    async def test_current_season_prefers_active_row(self, db):
        from bot.services.season import SeasonService

        async with db.get_session() as session:
            session.add(Season(number=3, status=SeasonStatus.ACTIVE))
            await SettingRepository(session).set("season", "1")
            await session.commit()

        assert await SeasonService().get_current_season_number() == 3

    async def test_current_season_uses_stored_setting_fallback(self, db):
        from bot.services.season import SeasonService

        async with db.get_session() as session:
            await SettingRepository(session).set("season", "2")
            await session.commit()

        assert await SeasonService().get_current_season_number() == 2

    async def test_current_season_missing_does_not_default_to_one(self, db):
        from bot.services.season import SeasonService

        assert await SeasonService().get_current_season_number() is None


async def test_skip_open_replaces_and_clears(db, seed_data):
    from bot.services.signup import SignupService

    svc = SignupService()
    await svc.set_skip_open(1, ["Player1", "Player2"])

    async with db.get_session() as session:
        repo = PlayerSeasonRepository(session)
        assert (await repo.get(seed_data["players"][0].id, 1)).swiss_seed == 1
        assert (await repo.get(seed_data["players"][1].id, 1)).swiss_seed == 2

    await svc.set_skip_open(1, ["Player3"])

    async with db.get_session() as session:
        repo = PlayerSeasonRepository(session)
        assert (await repo.get(seed_data["players"][0].id, 1)).swiss_seed in (None, 0)
        assert (await repo.get(seed_data["players"][2].id, 1)).swiss_seed == 1

    await svc.set_skip_open(1, [])

    async with db.get_session() as session:
        rows = await PlayerSeasonRepository(session).get_by_season(1)
        assert all((ps.swiss_seed or 0) == 0 for ps in rows)


class TestMatchRepository:
    """Eager-loading regressions for Discord UI paths."""

    async def test_get_by_id_loads_nested_players(self, db, seed_data, open_event):
        p1, p2 = seed_data["players"][:2]
        async with db.get_session() as session:
            match = Match(event_id=open_event.id)
            session.add(match)
            await session.flush()
            session.add_all([
                MatchPlayer(match_id=match.id, side=0, player_id=p1.id),
                MatchPlayer(match_id=match.id, side=1, player_id=p2.id),
            ])
            await session.commit()
            match_id = match.id

        async with db.get_session() as session:
            loaded = await MatchRepository(session).get_by_id(match_id)

        assert loaded is not None
        assert sorted(mp.player.name for mp in loaded.match_players) == ["Player1", "Player2"]


# ── MatchService ─────────────────────────────────────────────

class TestMatchService:
    """Tests for MatchService (report_score, forfeit, substitute, history)."""

    async def test_report_score_creates_result(self, db, seed_data):
        from bot.services.match import MatchService
        svc = MatchService()

        async with db.get_session() as session:
            match_repo = MatchRepository(session)

            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.OPEN, 1)
            match = await match_repo.create(event.id)

            mp_repo = MatchPlayerRepository(session)
            p1 = seed_data["players"][0]
            p2 = seed_data["players"][1]
            await mp_repo.create(match.id, side=0, player_id=p1.id)
            await mp_repo.create(match.id, side=1, player_id=p2.id)
            await session.commit()
            match_id = match.id

        result = await svc.report_score(match_id, 3, 1, 0)

        assert result["match_id"] == match_id
        assert result["winner_id"] == p1.id
        assert result["loser_id"] == p2.id
        assert result["score"] == (3, 1)

        async with db.get_session() as session:
            repo = MatchRepository(session)
            m = await repo.get_by_id(match_id)
            assert m is not None
            assert m.completed_at is not None
            winner = m.get_winner()
            assert winner is not None
            assert winner.player_id == p1.id
            assert winner.goals == 3

    async def test_report_score_rejects_tie(self, db, seed_data):
        from bot.services.match import MatchService

        svc = MatchService()
        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "test", EventType.OPEN, 1
            )
            match = await MatchRepository(session).create(event.id)
            mp_repo = MatchPlayerRepository(session)
            p1, p2 = seed_data["players"][:2]
            await mp_repo.create(match.id, side=0, player_id=p1.id)
            await mp_repo.create(match.id, side=1, player_id=p2.id)
            await session.commit()
            match_id = match.id

        result = await svc.report_score(match_id, 2, 2, 0)

        assert result == {"error": "Ties are not allowed."}
        async with db.get_session() as session:
            match = await MatchRepository(session).get_by_id(match_id)
            assert match.completed_at is None

    async def test_report_score_can_edit_completed_match(self, db, seed_data):
        from bot.services.match import MatchService
        svc = MatchService()

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.OPEN, 1)
            match_repo = MatchRepository(session)
            match = await match_repo.create(event.id)
            mp_repo = MatchPlayerRepository(session)
            p1, p2 = seed_data["players"][:2]
            await mp_repo.create(match.id, side=0, player_id=p1.id)
            await mp_repo.create(match.id, side=1, player_id=p2.id)
            await session.commit()
            match_id = match.id

        await svc.report_score(match_id, 1, 0, 0)
        result = await svc.report_score(match_id, 0, 2, 1)
        assert "error" not in result
        assert result["edited"] is True
        assert result["winner_id"] == p2.id

        async with db.get_session() as session:
            mps = await MatchPlayerRepository(session).get_by_match(match_id)
            by_side = {mp.side: mp for mp in mps}
            assert (by_side[0].goals, by_side[1].goals) == (0, 2)
            assert by_side[1].is_winner is True

    async def test_report_score_nonexistent_match(self, db, seed_data):
        from bot.services.match import MatchService
        svc = MatchService()
        result = await svc.report_score(9999, 1, 0, 0)
        assert "error" in result

    async def test_forfeit(self, db, seed_data):
        from bot.services.match import MatchService
        svc = MatchService()

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.OPEN, 1)
            match_repo = MatchRepository(session)
            match = await match_repo.create(event.id)
            mp_repo = MatchPlayerRepository(session)
            p1, p2 = seed_data["players"][:2]
            await mp_repo.create(match.id, side=0, player_id=p1.id)
            await mp_repo.create(match.id, side=1, player_id=p2.id)
            await session.commit()
            match_id = match.id

        result = await svc.forfeit(match_id, p2.id)

        assert result is not None
        assert result["match_id"] == match_id
        assert result["forfeited_by"] == p2.id

        async with db.get_session() as session:
            repo = MatchRepository(session)
            m = await repo.get_by_id(match_id)
            assert m.completed_at is not None
            winner = m.get_winner()
            assert winner is not None
            assert winner.player_id == p1.id
            assert winner.goals == 1

    async def test_forfeit_rejects_one_sided_match(self, db, seed_data):
        from bot.services.match import MatchService

        svc = MatchService()
        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "test", EventType.OPEN, 1
            )
            match = await MatchRepository(session).create(event.id)
            mp_repo = MatchPlayerRepository(session)
            p1 = seed_data["players"][0]
            await mp_repo.create(match.id, side=0, player_id=p1.id)
            await mp_repo.create(match.id, side=1, player_id=None)
            await session.commit()
            match_id = match.id

        result = await svc.forfeit(match_id, p1.id)

        assert result is None
        async with db.get_session() as session:
            match = await MatchRepository(session).get_by_id(match_id)
            assert match.completed_at is None

    async def test_forfeit_nonexistent_match(self, db, seed_data):
        from bot.services.match import MatchService
        svc = MatchService()
        result = await svc.forfeit(9999, 1)
        assert result is None

    async def test_gf_bracket_reset_creates_gf_m2(self, db, seed_data):
        """LB champ (side 1) winning GF-M1 BO3 creates GF-M2."""
        from bot.services.match import MatchService
        from sqlalchemy import select, and_

        svc = MatchService()
        p_wb, p_lb = seed_data["players"][:2]

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("PO", EventType.PLAYOFF, 1)
            event_id = event.id
            mp_repo = MatchPlayerRepository(session)
            gf = Match(event_id=event_id, bracket_slot="GF-M1", series_game=1)
            session.add(gf)
            await session.flush()
            await mp_repo.create(gf.id, side=0, player_id=p_wb.id)
            await mp_repo.create(gf.id, side=1, player_id=p_lb.id)
            await session.commit()
            gf_id = gf.id

        await svc.report_score(gf_id, 0, 2, 1)
        async with db.get_session() as session:
            stmt = select(Match).where(
                and_(Match.event_id == event_id, Match.bracket_slot == "GF-M1")
            )
            games = (await session.execute(stmt)).scalars().all()
            game2 = next(m for m in games if m.series_game == 2)
            gf2_id = game2.id

        await svc.report_score(gf2_id, 0, 3, 1)

        async with db.get_session() as session:
            stmt = select(Match).where(
                and_(Match.event_id == event_id, Match.bracket_slot == "GF-M2")
            )
            gf_m2 = (await session.execute(stmt)).scalars().first()
            assert gf_m2 is not None

    async def test_gf_wb_win_no_bracket_reset(self, db, seed_data):
        """WB champ winning GF-M1 does not create GF-M2."""
        from bot.services.match import MatchService
        from sqlalchemy import select, and_

        svc = MatchService()
        p_wb, p_lb = seed_data["players"][:2]

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("PO2", EventType.PLAYOFF, 1)
            event_id = event.id
            mp_repo = MatchPlayerRepository(session)
            gf = Match(event_id=event_id, bracket_slot="GF-M1", series_game=1)
            session.add(gf)
            await session.flush()
            await mp_repo.create(gf.id, side=0, player_id=p_wb.id)
            await mp_repo.create(gf.id, side=1, player_id=p_lb.id)
            await session.commit()
            gf_id = gf.id

        await svc.report_score(gf_id, 3, 0, 0)
        async with db.get_session() as session:
            stmt = select(Match).where(
                and_(Match.event_id == event_id, Match.bracket_slot == "GF-M1")
            )
            games = (await session.execute(stmt)).scalars().all()
            game2 = next(m for m in games if m.series_game == 2)
            gf2_id = game2.id

        await svc.report_score(gf2_id, 2, 0, 0)

        async with db.get_session() as session:
            stmt = select(Match).where(
                and_(Match.event_id == event_id, Match.bracket_slot == "GF-M2")
            )
            gf_m2 = (await session.execute(stmt)).scalars().first()
            assert gf_m2 is None

    async def test_playoff_series_fills_next_bracket_slot(self, db, seed_data):
        """Winning a BO3 series immediately fills the next round's slot."""
        from bot.services.match import MatchService
        from sqlalchemy import select, and_

        svc = MatchService()
        p1, p2 = seed_data["players"][:2]

        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "PO-fill", EventType.PLAYOFF, 1
            )
            event_id = event.id
            mp_repo = MatchPlayerRepository(session)

            source = Match(
                event_id=event_id, bracket_slot="W-R1-M1", series_game=1
            )
            next_winner = Match(
                event_id=event_id, bracket_slot="W-R2-M1", series_game=1
            )
            next_loser = Match(
                event_id=event_id, bracket_slot="L-R1-M1", series_game=1
            )
            session.add_all([source, next_winner, next_loser])
            await session.flush()
            await mp_repo.create(source.id, side=0, player_id=p1.id)
            await mp_repo.create(source.id, side=1, player_id=p2.id)
            for match in (next_winner, next_loser):
                await mp_repo.create(match.id, side=0, player_id=None)
                await mp_repo.create(match.id, side=1, player_id=None)
            await session.commit()
            source_id = source.id
            winner_id = next_winner.id
            loser_id = next_loser.id

        await svc.report_score(source_id, 5, 2, 0)
        async with db.get_session() as session:
            games = (
                await session.execute(
                    select(Match).where(
                        and_(
                            Match.event_id == event_id,
                            Match.bracket_slot == "W-R1-M1",
                        )
                    )
                )
            ).scalars().all()
            game2 = next(m for m in games if m.series_game == 2)
            game2_id = game2.id
        await svc.report_score(game2_id, 5, 3, 0)

        async with db.get_session() as session:
            repo = MatchRepository(session)
            winner_slot = await repo.get_by_id(winner_id)
            loser_slot = await repo.get_by_id(loser_id)
            winner_players = sorted(
                winner_slot.match_players, key=lambda mp: mp.side
            )
            loser_players = sorted(
                loser_slot.match_players, key=lambda mp: mp.side
            )
            assert winner_players[0].player_id == p1.id
            assert loser_players[0].player_id == p2.id

    async def test_edit_playoff_game_resets_downstream_series(self, db, seed_data):
        """Correcting GF-M1 game 1 clears game 2 and the bracket reset."""
        from bot.services.match import MatchService
        from sqlalchemy import select, and_

        svc = MatchService()
        p_wb, p_lb = seed_data["players"][:2]

        async with db.get_session() as session:
            event = await EventRepository(session).create("PO-edit", EventType.PLAYOFF, 1)
            event_id = event.id
            mp_repo = MatchPlayerRepository(session)
            gf = Match(event_id=event_id, bracket_slot="GF-M1", series_game=1)
            session.add(gf)
            await session.flush()
            await mp_repo.create(gf.id, side=0, player_id=p_wb.id)
            await mp_repo.create(gf.id, side=1, player_id=p_lb.id)
            await session.commit()
            gf_game1_id = gf.id

        # LB player wins game 1, forcing game 2 and then the reset.
        await svc.report_score(gf_game1_id, 0, 2, 1)
        async with db.get_session() as session:
            gf_games = (
                await session.execute(
                    select(Match).where(
                        and_(Match.event_id == event_id, Match.bracket_slot == "GF-M1")
                    )
                )
            ).scalars().all()
            game2 = next(m for m in gf_games if m.series_game == 2)
            game2_id = game2.id
        await svc.report_score(game2_id, 0, 2, 1)

        async with db.get_session() as session:
            reset = (
                await session.execute(
                    select(Match).where(
                        and_(Match.event_id == event_id, Match.bracket_slot == "GF-M2")
                    )
                )
            ).scalars().first()
            assert reset is not None

        # Correct game 1 so the WB player wins. Game 2 is recreated unplayed
        # for the still-live series, while the bracket reset must disappear.
        result = await svc.report_score(gf_game1_id, 2, 0, 0)
        assert result["edited"] is True

        async with db.get_session() as session:
            gf_games = (
                await session.execute(
                    select(Match).where(
                        and_(Match.event_id == event_id, Match.bracket_slot == "GF-M1")
                    )
                )
            ).scalars().all()
            assert [m.series_game for m in sorted(gf_games, key=lambda m: m.series_game)] == [1, 2]
            replay = next(m for m in gf_games if m.series_game == 2)
            assert replay.completed_at is None
            replay_players = await MatchPlayerRepository(session).get_by_match(replay.id)
            assert all(mp.goals == 0 for mp in replay_players)
            reset = (
                await session.execute(
                    select(Match).where(
                        and_(Match.event_id == event_id, Match.bracket_slot == "GF-M2")
                    )
                )
            ).scalars().first()
            assert reset is None

    async def test_substitute_player_moves_matches_and_stats(
        self, db, seed_data
    ):
        from bot.services.match import MatchService

        svc = MatchService()
        players = seed_data["players"]

        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "test", EventType.OPEN, 1
            )
            match = await MatchRepository(session).create(event.id)
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(
                match.id, side=0, player_id=players[0].id
            )
            await mp_repo.create(
                match.id, side=1, player_id=players[1].id
            )
            old_ps = await PlayerSeasonRepository(session).get(
                players[0].id, 1
            )
            old_ps.open_wins = 3
            old_ps.hp = 1
            await session.commit()

        result = await svc.substitute_player(
            players[0].id, players[2].id, 1
        )
        assert "Substituted" in result
        assert "stats transferred" in result

        async with db.get_session() as session:
            mps = await MatchPlayerRepository(session).get_by_match(match.id)
            assert sorted(mps, key=lambda mp: mp.side)[0].player_id == (
                players[2].id
            )
            ps_repo = PlayerSeasonRepository(session)
            assert await ps_repo.get(players[0].id, 1) is None
            new_ps = await ps_repo.get(players[2].id, 1)
            assert new_ps.open_wins == 3
            assert new_ps.hp == 1

    async def test_substitute_moves_completed_open_but_not_playin(
        self, db, seed_data
    ):
        from datetime import datetime
        from bot.services.match import MatchService

        svc = MatchService()
        players = seed_data["players"]

        async with db.get_session() as session:
            open_event = await EventRepository(session).create(
                "open", EventType.OPEN, 1
            )
            playin_event = await EventRepository(session).create(
                "playin", EventType.PLAYIN, 1
            )
            match_repo = MatchRepository(session)
            mp_repo = MatchPlayerRepository(session)
            open_match = await match_repo.create(open_event.id)
            playin_match = await match_repo.create(playin_event.id)
            for match in (open_match, playin_match):
                mp1 = await mp_repo.create(
                    match.id, side=0, player_id=players[0].id
                )
                await mp_repo.create(
                    match.id, side=1, player_id=players[1].id
                )
                mp1.goals = 5
                mp1.is_winner = True
                match.completed_at = datetime.utcnow()
            await session.commit()

        await svc.substitute_player(players[0].id, players[2].id, 1)

        async with db.get_session() as session:
            mp_repo = MatchPlayerRepository(session)
            open_mps = await mp_repo.get_by_match(open_match.id)
            playin_mps = await mp_repo.get_by_match(playin_match.id)
            assert sorted(open_mps, key=lambda mp: mp.side)[0].player_id == (
                players[2].id
            )
            assert sorted(playin_mps, key=lambda mp: mp.side)[0].player_id == (
                players[0].id
            )

    async def test_substitute_is_scoped_to_the_season(self, db, seed_data):
        from bot.services.match import MatchService

        svc = MatchService()
        players = seed_data["players"]

        async with db.get_session() as session:
            session.add(Season(number=2, status=SeasonStatus.ACTIVE))
            await session.flush()
            event1 = await EventRepository(session).create(
                "s1", EventType.OPEN, 1
            )
            event2 = await EventRepository(session).create(
                "s2", EventType.OPEN, 2
            )
            match_repo = MatchRepository(session)
            mp_repo = MatchPlayerRepository(session)
            match1 = await match_repo.create(event1.id)
            match2 = await match_repo.create(event2.id)
            for match in (match1, match2):
                await mp_repo.create(
                    match.id, side=0, player_id=players[0].id
                )
                await mp_repo.create(
                    match.id, side=1, player_id=players[1].id
                )
            await session.commit()

        await svc.substitute_player(players[0].id, players[2].id, 1)

        async with db.get_session() as session:
            mp_repo = MatchPlayerRepository(session)
            s1 = await mp_repo.get_by_match(match1.id)
            s2 = await mp_repo.get_by_match(match2.id)
            assert sorted(s1, key=lambda mp: mp.side)[0].player_id == (
                players[2].id
            )
            assert sorted(s2, key=lambda mp: mp.side)[0].player_id == (
                players[0].id
            )

    async def test_substitute_creates_player_season_for_new_player(
        self, db, seed_data
    ):
        from bot.services.match import MatchService

        svc = MatchService()
        players = seed_data["players"]

        async with db.get_session() as session:
            newcomer = await PlayerRepository(session).create(
                discord_id=9999, name="Newcomer"
            )
            event = await EventRepository(session).create(
                "test", EventType.OPEN, 1
            )
            match = await MatchRepository(session).create(event.id)
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(
                match.id, side=0, player_id=players[0].id
            )
            await mp_repo.create(
                match.id, side=1, player_id=players[1].id
            )
            await session.commit()

        await svc.substitute_player(players[0].id, newcomer.id, 1)

        async with db.get_session() as session:
            ps = await PlayerSeasonRepository(session).get(newcomer.id, 1)
            assert ps is not None
            assert ps.open_wins == 1

    async def test_reset_open_matches_clears_scores_and_stats(
        self, db, seed_data
    ):
        from datetime import datetime
        from bot.services.match import MatchService

        svc = MatchService()
        players = seed_data["players"]

        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "open", EventType.OPEN, 1
            )
            match = await MatchRepository(session).create(event.id)
            mp_repo = MatchPlayerRepository(session)
            mp1 = await mp_repo.create(
                match.id, side=0, player_id=players[0].id
            )
            mp2 = await mp_repo.create(
                match.id, side=1, player_id=players[1].id
            )
            mp1.goals = 5
            mp2.goals = 2
            mp1.is_winner = True
            match.completed_at = datetime.utcnow()
            await session.commit()

        result = await svc.reset_open_matches(players[0].id, 1)
        assert "Reset 1" in result

        async with db.get_session() as session:
            match = await MatchRepository(session).get_by_id(match.id)
            assert match.completed_at is None
            mps = await MatchPlayerRepository(session).get_by_match(match.id)
            assert all(mp.goals == 0 and not mp.is_winner for mp in mps)
            ps = await PlayerSeasonRepository(session).get(players[0].id, 1)
            assert ps.open_wins == 0
            assert ps.open_losses == 0
            assert ps.hp == 4

    async def test_placeholder_players_get_unique_negative_ids(self, db):
        async with db.get_session() as session:
            repo = PlayerRepository(session)
            first = await repo.create_placeholder("Ghost One")
            second = await repo.create_placeholder("Ghost Two")
            await session.commit()

        assert first.discord_id < 0
        assert second.discord_id < first.discord_id

    async def test_get_match_history(self, db, seed_data):
        from bot.services.match import MatchService
        svc = MatchService()

        players = seed_data["players"]

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.OPEN, 1)
            match_repo = MatchRepository(session)
            match = await match_repo.create(event.id)
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(match.id, side=0, player_id=players[0].id)
            await mp_repo.create(match.id, side=1, player_id=players[1].id)
            await session.commit()

        await svc.report_score(match.id, 2, 0, 0)

        history = await svc.get_match_history(players[0].id, 1)
        assert len(history) == 1
        assert history[0]["match_id"] == match.id
        assert history[0]["won"] is True
        assert history[0]["opponent"] == "Player2"

    async def test_delete_player_matches_open(self, db, seed_data):
        from bot.services.match import MatchService
        svc = MatchService()

        players = seed_data["players"]

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.OPEN, 1)
            match_repo = MatchRepository(session)
            match = await match_repo.create(event.id)
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(match.id, side=0, player_id=players[0].id)
            await mp_repo.create(match.id, side=1, player_id=players[1].id)
            await session.commit()
            match_id = match.id

        result = await svc.delete_player_matches(players[0].discord_id, 1)
        assert "Deleted 1" in result

        async with db.get_session() as session:
            assert await MatchRepository(session).get_by_id(match_id) is None

    async def test_delete_player_matches_skips_playoff(self, db, seed_data):
        from bot.services.match import MatchService
        svc = MatchService()

        players = seed_data["players"]

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.PLAYOFF, 1)
            match_repo = MatchRepository(session)
            match = await match_repo.create(event.id)
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(match.id, side=0, player_id=players[0].id)
            await mp_repo.create(match.id, side=1, player_id=players[1].id)
            await session.commit()
            match_id = match.id

        result = await svc.delete_player_matches(players[0].discord_id, 1)
        assert "Skipped 1" in result

        async with db.get_session() as session:
            assert await MatchRepository(session).get_by_id(match_id) is not None


# ── PairingService ──────────────────────────────────────────

class TestPairingService:
    """Tests for PairingService (pair_open, seed_playoff, generate_playoff_bracket)."""

    async def test_list_matches_is_not_capped_at_100(self, db, seed_data):
        from datetime import datetime
        from bot.services.match import MatchService

        players = seed_data["players"]
        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "Season 1 Open", EventType.OPEN, 1
            )
            event.status = EventStatus.ACTIVE
            await session.flush()
            for i in range(105):
                match = Match(
                    event_id=event.id,
                    series_game=1,
                    completed_at=datetime.utcnow(),
                )
                session.add(match)
                await session.flush()
                session.add(
                    MatchPlayer(
                        match_id=match.id,
                        side=0,
                        player_id=players[0].id,
                        goals=1,
                        is_winner=True,
                    )
                )
                session.add(
                    MatchPlayer(
                        match_id=match.id,
                        side=1,
                        player_id=players[1].id,
                        goals=0,
                        is_winner=False,
                    )
                )
            await session.commit()

        svc = MatchService()
        all_matches = await svc.list_incomplete_matches(include_completed=True)
        assert len(all_matches) == 105

        completed = await svc.list_incomplete_matches(
            include_completed=True, status="completed"
        )
        assert len(completed) == 105

        active = await svc.list_incomplete_matches(
            include_completed=True, status="active"
        )
        assert active == []

        by_player = await svc.list_incomplete_matches(
            include_completed=True, player=players[0].name
        )
        assert len(by_player) == 105

        by_internal_id = await svc.list_incomplete_matches(
            include_completed=True, player=str(players[0].id)
        )
        assert len(by_internal_id) == 105

        by_discord_id = await svc.list_incomplete_matches(
            include_completed=True, player=str(players[0].discord_id)
        )
        assert len(by_discord_id) == 105

        by_stage = await svc.list_incomplete_matches(
            include_completed=True, stage="OPEN", round_num=1
        )
        assert len(by_stage) == 105

    async def test_completed_match_label_shows_score(self, db, seed_data):
        from datetime import datetime
        from bot.services.match import MatchService

        players = seed_data["players"]
        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "Season 1 Open", EventType.OPEN, 1
            )
            match = Match(
                event_id=event.id,
                series_game=1,
                completed_at=datetime.utcnow(),
            )
            session.add(match)
            await session.flush()
            session.add_all(
                [
                    MatchPlayer(
                        match_id=match.id,
                        side=0,
                        player_id=players[0].id,
                        goals=3,
                        is_winner=True,
                    ),
                    MatchPlayer(
                        match_id=match.id,
                        side=1,
                        player_id=players[1].id,
                        goals=1,
                        is_winner=False,
                    ),
                ]
            )
            await session.commit()
            match_id = match.id

        matches = await MatchService().list_incomplete_matches(
            include_completed=True
        )
        label = next(m["label"] for m in matches if m["match_id"] == match_id)
        assert f"{players[0].name} 3-1 {players[1].name}" in label

    async def test_pair_open_creates_matches(self, db, seed_data):
        from bot.services.pairing import PairingService
        svc = PairingService()

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.OPEN, 1)
            event.status = EventStatus.ACTIVE
            await session.commit()
            event_id = event.id

        created, bye, err = await svc.pair_open(event_id)
        assert created >= 1
        assert err is None

        async with db.get_session() as session:
            match_repo = MatchRepository(session)
            matches = await match_repo.get_by_event(event_id)
            assert len(matches) == created

    async def test_pair_open_no_eligible_players(self, db, seed_data):
        from bot.services.pairing import PairingService
        svc = PairingService()

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.OPEN, 1)
            event.status = EventStatus.ACTIVE
            await session.commit()
            event_id = event.id

            ps_repo = PlayerSeasonRepository(session)
            for p in seed_data["players"]:
                ps = await ps_repo.get(p.id, 1)
                if ps:
                    ps.hp = 0
            await session.commit()

        created, bye, err = await svc.pair_open(event_id)
        assert created == 0
        assert err is not None

    async def test_pair_open_force(self, db, seed_data):
        from bot.services.pairing import PairingService
        svc = PairingService()

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.OPEN, 1)
            event.status = EventStatus.ACTIVE
            match_repo = MatchRepository(session)
            match = await match_repo.create(event.id)
            mp_repo = MatchPlayerRepository(session)
            p1, p2 = seed_data["players"][:2]
            await mp_repo.create(match.id, side=0, player_id=p1.id)
            await mp_repo.create(match.id, side=1, player_id=p2.id)
            await session.commit()
            event_id = event.id

        created, bye, err = await svc.pair_open(event_id, force=True)
        if created > 0:
            assert err is None
            async with db.get_session() as session:
                match_repo = MatchRepository(session)
                matches = await match_repo.get_by_event(event_id)
                unplayed = [m for m in matches if not m.completed_at]
                assert len(unplayed) == created

    async def test_seed_playoff_creates_bracket(self, db, seed_data):
        from bot.services.pairing import PairingService
        svc = PairingService()

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("test", EventType.PLAYOFF, 1)
            event.status = EventStatus.ACTIVE
            await session.commit()
            event_id = event.id

        seeded_ids = [p.id for p in seed_data["players"]]
        while len(seeded_ids) < 16:
            seeded_ids.append(seeded_ids[-1])

        match_ids = await svc.seed_playoff(event_id, seeded_ids)
        assert len(match_ids) > 0

        async with db.get_session() as session:
            match_repo = MatchRepository(session)
            matches = await match_repo.get_by_event(event_id)
            assert len(matches) == len(match_ids)


# ── PlayinService ───────────────────────────────────────────

class TestPlayinService:
    """Tests for the stepped Playin ladder."""

    @pytest.fixture(autouse=True)
    async def setup_playin_data(self, db, seed_data):
        """Give the 8 test players 4/3/2/1 Open wins."""
        async with db.get_session() as session:
            ps_repo = PlayerSeasonRepository(session)
            for i, p in enumerate(seed_data["players"]):
                ps = await ps_repo.get(p.id, 1)
                if ps:
                    if i < 2:
                        ps.open_wins = 4
                        ps.open_gd = 5
                    elif i < 4:
                        ps.open_wins = 3
                        ps.open_gd = 3
                    elif i < 6:
                        ps.open_wins = 2
                        ps.open_gd = 1
                    else:
                        ps.open_wins = 1
                        ps.open_gd = 0
            await session.commit()
        return seed_data["players"]

    async def _play_ladder(self, db):
        from bot.services.match import MatchService

        ms = MatchService()
        for _ in range(40):
            async with db.get_session() as session:
                event = await EventRepository(session).get_by_type_and_season(
                    EventType.PLAYIN, 1
                )
                matches = await MatchRepository(session).get_by_event(event.id)
            pending = []
            for match in matches:
                if match.completed_at is not None:
                    continue
                players = [
                    mp.player_id for mp in match.match_players
                    if mp.player_id is not None
                ]
                if len(players) == 2:
                    pending.append(match)
            if not pending:
                break
            for match in pending:
                await ms.report_score(match.id, 1, 0, 0)

    async def test_all_four_wins_finalizes_without_ladder(
        self, db, seed_data, setup_playin_data
    ):
        """No R1/R2/R3 cards are needed when every entrant has four wins."""
        from bot.services.playin import PlayinService

        async with db.get_session() as session:
            ps_repo = PlayerSeasonRepository(session)
            for player in setup_playin_data:
                ps = await ps_repo.get(player.id, 1)
                ps.open_wins = 4
                ps.open_gd = 2
            await session.commit()

        gen = await PlayinService().generate_playins(1)
        assert all(count == 0 for count in gen["matches_by_round"].values())
        assert all(count == 0 for count in gen["byes_by_round"].values())

        async with db.get_session() as session:
            rows = await PlayerSeasonRepository(session).get_by_season(1)
        assert sorted(ps.playin_rank for ps in rows) == list(range(1, 9))
        assert all(ps.playin_eliminated is False for ps in rows)

    async def test_generate_full_graph(self, db, seed_data, setup_playin_data):
        from bot.services.playin import PlayinService

        svc = PlayinService()
        gen = await svc.generate_playins(1)

        assert gen["matches_by_round"] == {1: 1, 2: 1, 3: 1}
        assert gen["byes_by_round"] == {1: 0, 2: 1, 3: 0}

        async with db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(
                EventType.PLAYIN, 1
            )
            matches = await MatchRepository(session).get_by_event(event.id)

        r2 = [m for m in matches if m.bracket_slot == "P-R2-M1"][0]
        assert r2.p1_prereq_id is not None or r2.p2_prereq_id is not None
        r2_bye = [m for m in matches if m.bracket_slot == "P-R2-B1"][0]
        assert r2_bye.completed_at is not None
        bye_winner = r2_bye.get_winner()
        assert bye_winner is not None
        async with db.get_session() as session:
            bye_ps = await PlayerSeasonRepository(session).get(
                bye_winner.player_id, 1
            )
        assert bye_ps.open_wins == 2

        r3_slots = {m.bracket_slot for m in matches if m.series_game == 3}
        assert r3_slots == {"P-R3-M1"}

    async def test_generate_round_all_qualified(self, db, seed_data):
        """When everyone has 4+ wins, no playin matches are needed."""
        from bot.services.playin import PlayinService

        async with db.get_session() as session:
            ps_repo = PlayerSeasonRepository(session)
            for p in seed_data["players"]:
                ps = await ps_repo.get(p.id, 1)
                if ps:
                    ps.open_wins = 4
            await session.commit()

        svc = PlayinService()
        created, byes = await svc.generate_round(1, 1)
        assert created == 0
        assert byes == 0

    async def test_finish_playins_ranks_ladder(
        self, db, seed_data, setup_playin_data, monkeypatch
    ):
        from bot.services.playin import PlayinService, compute_playin_math

        prerender = AsyncMock()
        monkeypatch.setattr("bot.services.playin.prerender", prerender)
        svc = PlayinService()
        await svc.generate_playins(1)
        await self._play_ladder(db)
        await svc.finish_playins(1)
        prerender.assert_any_await("PLAYIN", 1)

        async with db.get_session() as session:
            all_ps = await PlayerSeasonRepository(session).get_by_season(1)
            math = compute_playin_math(all_ps)
            qualifiers = [ps for ps in all_ps if ps.playin_rank is not None]
            # This tiny fixture cannot fill a 32-player Swiss field, so the
            # available eight players are all taken.
            assert len(qualifiers) == math["qualifiers"] == 8
            assert sorted(ps.playin_rank for ps in qualifiers) == list(range(1, 9))
            assert all(
                ps.playin_eliminated or ps.playin_rank is not None
                for ps in all_ps
            )

    async def test_edit_playin_score_resets_descendants(
        self, db, seed_data, setup_playin_data
    ):
        from bot.services.match import MatchService
        from bot.services.playin import PlayinService

        svc = PlayinService()
        ms = MatchService()
        await svc.generate_playins(1)

        async with db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(
                EventType.PLAYIN, 1
            )
            matches = await MatchRepository(session).get_by_event(event.id)
        r1 = [m for m in matches if m.bracket_slot == "P-R1-M1"][0]
        r2 = [m for m in matches if m.bracket_slot == "P-R2-M1"][0]
        r1_players = {
            mp.side: mp.player_id
            for mp in r1.match_players
            if mp.player_id is not None
        }
        prereq_side = 0 if r2.p1_prereq_id == r1.id else 1

        await ms.report_score(r1.id, 1, 0, 0)
        await ms.report_score(r2.id, 1, 0, 0)

        async with db.get_session() as session:
            r2_after = await MatchRepository(session).get_by_id(r2.id)
            assert r2_after.completed_at is not None
            prereq_player = [
                mp.player_id
                for mp in r2_after.match_players
                if mp.side == prereq_side
            ][0]
            assert prereq_player == r1_players[0]

        # Flip the R1 winner. The already-played R2 match must be reset and
        # receive the new player in the prereq slot.
        await ms.report_score(r1.id, 0, 1, 1)

        async with db.get_session() as session:
            r2_after = await MatchRepository(session).get_by_id(r2.id)
            prereq_player = [
                mp.player_id
                for mp in r2_after.match_players
                if mp.side == prereq_side
            ][0]
            assert prereq_player == r1_players[1]
            assert r2_after.completed_at is None
            assert all(mp.goals == 0 for mp in r2_after.match_players)
            assert all(mp.is_winner is False for mp in r2_after.match_players)


async def test_playin_ladder_14_players(db):
    """14 one-win players collapse to 7 -> 4 -> 2, then one wildcard fills 3."""
    from bot.db.models import SeasonStatus
    from bot.db import PlayerRepository
    from bot.services.playin import PlayinService
    from bot.services.match import MatchService

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        p_repo = PlayerRepository(session)
        for i in range(29):
            p = await p_repo.create(
                discord_id=5000 + i, name=f"Seed{i}", country="US"
            )
            session.add(
                PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=0,
                    swiss_seed=i + 1,
                    hp=4,
                )
            )
        for i in range(14):
            p = await p_repo.create(
                discord_id=6000 + i, name=f"Pool{i}", country="US"
            )
            session.add(
                PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=1,
                    swiss_seed=None,
                    hp=4,
                )
            )
        await session.commit()

    svc = PlayinService()
    ms = MatchService()
    await svc.generate_playins(1)

    for _ in range(30):
        async with db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(
                EventType.PLAYIN, 1
            )
            matches = await MatchRepository(session).get_by_event(event.id)
        pending = [
            m for m in matches
            if m.completed_at is None
            and len([
                mp for mp in m.match_players if mp.player_id is not None
            ]) == 2
        ]
        if not pending:
            break
        for match in pending:
            await ms.report_score(match.id, 1, 0, 0)

    await svc.finish_playins(1)

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.PLAYIN, 1
        )
        matches = await MatchRepository(session).get_by_event(event.id)
        assert len([m for m in matches if m.series_game == 1]) == 7
        assert len([m for m in matches if m.series_game == 2]) == 4
        assert len([m for m in matches if m.series_game == 3]) == 2
        all_ps = await PlayerSeasonRepository(session).get_by_season(1)
        qualifiers = [ps for ps in all_ps if ps.playin_rank is not None]
        assert len(qualifiers) == 3
        assert sorted(ps.playin_rank for ps in qualifiers) == [1, 2, 3]


async def test_playin_overproduction_creates_cut_round(db):
    """24 seeds + 20 open players target 8; 9 candidates create one cut match."""
    from bot.db.models import SeasonStatus
    from bot.db import Player
    from bot.services.playin import PlayinService, compute_playin_math
    from bot.services.match import MatchService
    from bot.services.pairing import PairingService

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        player_id = 1
        for i in range(24):
            p = Player(discord_id=11000 + i, name=f"Seed{i}", country="US")
            session.add(p)
            await session.flush()
            session.add(
                PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=0,
                    swiss_seed=i + 1,
                    hp=4,
                )
            )
        for wins, count in ((4, 4), (3, 4), (2, 6), (1, 6)):
            for _ in range(count):
                p = Player(
                    discord_id=12000 + player_id,
                    name=f"Open{player_id}",
                    country="US",
                )
                session.add(p)
                await session.flush()
                session.add(
                    PlayerSeason(
                        player_id=p.id,
                        season_number=1,
                        open_wins=wins,
                        open_gd=0,
                        swiss_seed=None,
                        hp=4,
                    )
                )
                player_id += 1
        await session.commit()

    svc = PlayinService()
    ms = MatchService()
    await svc.generate_playins(1)

    for _ in range(20):
        async with db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(
                EventType.PLAYIN, 1
            )
            matches = await MatchRepository(session).get_by_event(event.id)
        pending = [
            m for m in matches
            if m.completed_at is None
            and len([
                mp for mp in m.match_players if mp.player_id is not None
            ]) == 2
        ]
        if not pending:
            break
        for match in pending:
            await ms.report_score(match.id, 1, 0, 0)

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.PLAYIN, 1
        )
        matches = await MatchRepository(session).get_by_event(event.id)
        cut_matches = [m for m in matches if m.series_game == 4]
        assert len(cut_matches) == 1
        assert cut_matches[0].bracket_slot == "P-CUT-M1"
        assert cut_matches[0].completed_at is not None

        all_ps = await PlayerSeasonRepository(session).get_by_season(1)
        math = compute_playin_math(all_ps)
        assert math["over_by"] == 1
        qualifiers = [ps for ps in all_ps if ps.playin_rank is not None]
        assert len(qualifiers) == 8

    # The conditional cut resolved the field to exactly 32 for Swiss.
    swiss = await PairingService().generate_swiss_round(1, 1)
    assert len(swiss) == 16


async def test_swiss_round1_uses_fixed_32_field(db):
    """Swiss is exactly 32 players, with 16 first-round matches."""
    from bot.db.models import SeasonStatus
    from bot.db import Player
    from bot.services.pairing import PairingService

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        for i in range(32):
            p = Player(discord_id=7000 + i, name=f"S{i:02d}", country="US")
            session.add(p)
            await session.flush()
            if i < 12:
                ps = PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=0,
                    swiss_seed=i + 1,
                    hp=4,
                )
            else:
                ps = PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=1,
                    playin_rank=i - 11,
                    hp=4,
                )
            session.add(ps)
        await session.commit()

    svc = PairingService()
    matches = await svc.generate_swiss_round(1, 1)
    assert len(matches) == 16

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.SWISS, 1
        )
        all_matches = await MatchRepository(session).get_by_event(event.id)
        # The whole 66-match ladder is pre-created with feeder links.
        assert len(all_matches) == 66
        ids = [
            mp.player_id
            for match in all_matches
            for mp in match.match_players
            if mp.player_id is not None
        ]
        assert len(ids) == 32
        assert len(set(ids)) == 32

        future = [m for m in all_matches if (m.series_game or 1) > 1]
        assert len(future) == 50
        assert all(m.p1_prereq_id is not None for m in future)
        assert all(m.p2_prereq_id is not None for m in future)
        assert all(
            mp.player_id is None
            for m in future
            for mp in m.match_players
        )


async def test_swiss_fixed_graph_fills_feeder_slots(db):
    from bot.db.models import SeasonStatus
    from bot.db import Player
    from bot.services.pairing import PairingService
    from bot.services.match import MatchService

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        for i in range(32):
            p = Player(discord_id=9000 + i, name=f"A{i}", country="US")
            session.add(p)
            await session.flush()
            session.add(
                PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=1,
                    playin_rank=i + 1,
                    hp=4,
                )
            )
        await session.commit()

    pairing = PairingService()
    ms = MatchService()
    r1 = await pairing.generate_swiss_round(1, 1)
    assert len(r1) == 16

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.SWISS, 1
        )
        all_matches = await MatchRepository(session).get_by_event(event.id)

    # One R1 result fills exactly one R2 winners slot and one R2 losers slot.
    first = next(m for m in all_matches if (m.series_game or 1) == 1)
    await ms.report_score(first.id, 1, 0, 0)

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.SWISS, 1
        )
        r2 = [
            m
            for m in await MatchRepository(session).get_by_event(event.id)
            if m.series_game == 2
        ]
        assert len(r2) == 16
        filled = [
            mp
            for m in r2
            for mp in m.match_players
            if mp.player_id is not None
        ]
        assert len(filled) == 2
        buckets = {
            m.bracket_slot
            for m in r2
            if any(mp.player_id is not None for mp in m.match_players)
        }
        assert len(buckets) == 2
        assert any("SW-R2-U" in b for b in buckets)
        assert any("SW-R2-L" in b for b in buckets)

    # Finishing the rest of R1 fills all 16 R2 matches.
    for match in all_matches:
        if match.id == first.id or (match.series_game or 1) != 1:
            continue
        await ms.report_score(match.id, 1, 0, 0)

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.SWISS, 1
        )
        r2 = [
            m
            for m in await MatchRepository(session).get_by_event(event.id)
            if m.series_game == 2
        ]
        assert len(r2) == 16
        assert all(
            len([mp for mp in m.match_players if mp.player_id]) == 2
            for m in r2
        )



async def test_swiss_survival_ladder_counts_and_qualifiers(db):
    from bot.db.models import SeasonStatus
    from bot.db import Player
    from bot.services.pairing import PairingService
    from bot.services.match import MatchService

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        for i in range(32):
            p = Player(discord_id=7000 + i, name=f"S{i:02d}", country="US")
            session.add(p)
            await session.flush()
            session.add(
                PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=1,
                    playin_rank=i + 1,
                    hp=4,
                )
            )
        await session.commit()

    pairing = PairingService()
    ms = MatchService()
    await pairing.generate_swiss_round(1, 1)

    for _ in range(30):
        async with db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(
                EventType.SWISS, 1
            )
            matches = await MatchRepository(session).get_by_event(event.id)
        playable = [
            m
            for m in matches
            if m.completed_at is None
            and len([mp for mp in m.match_players if mp.player_id]) == 2
        ]
        if not playable:
            break
        for match in playable:
            await ms.report_score(match.id, 1, 0, 0)

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.SWISS, 1
        )
        matches = await MatchRepository(session).get_by_event(event.id)

    counts = Counter(m.series_game or 1 for m in matches)
    assert dict(sorted(counts.items())) == {1: 16, 2: 16, 3: 16, 4: 12, 5: 6}
    assert all(m.completed_at is not None for m in matches)

    stats: dict[int, list[int]] = {}
    for match in matches:
        mps = [
            mp for mp in match.match_players if mp.player_id is not None
        ]
        if len(mps) < 2:
            continue
        for mp in mps:
            stats.setdefault(mp.player_id, [0, 0])
            if mp.is_winner:
                stats[mp.player_id][0] += 1
            else:
                stats[mp.player_id][1] += 1

    records = Counter(tuple(v) for v in stats.values())
    assert records == Counter(
        {
            (3, 0): 4,
            (3, 1): 6,
            (3, 2): 6,
            (2, 3): 6,
            (1, 3): 6,
            (0, 3): 4,
        }
    )


async def test_generate_playoff_bracket_uses_swiss_record(db, monkeypatch):
    """The top 16 must come from Swiss wins, not Playin rank."""
    from datetime import datetime
    from bot.db.models import SeasonStatus
    from bot.db import Player, PlayerRepository
    from bot.services.pairing import PairingService

    prerender = AsyncMock()
    monkeypatch.setattr("bot.services.pairing.prerender", prerender)

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        players = []
        for i in range(20):
            p = Player(discord_id=8000 + i, name=f"P{i:02d}", country="US")
            session.add(p)
            await session.flush()
            players.append(p)
            session.add(
                PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=1,
                    playin_rank=(100 + i) if i < 16 else (1 + i),
                    hp=4,
                )
            )
        await session.commit()

        swiss_event = await EventRepository(session).create(
            "Season 1 Swiss", EventType.SWISS, 1
        )
        swiss_event.status = EventStatus.ACTIVE
        await session.flush()

        top_ids = [p.id for p in players[:16]]
        low_ids = [p.id for p in players[16:]]
        for i, top_id in enumerate(top_ids):
            match = Match(
                event_id=swiss_event.id,
                completed_at=datetime.utcnow(),
            )
            session.add(match)
            await session.flush()
            session.add(
                MatchPlayer(
                    match_id=match.id, side=0, player_id=top_id, goals=2, is_winner=True
                )
            )
            session.add(
                MatchPlayer(
                    match_id=match.id,
                    side=1,
                    player_id=low_ids[i % len(low_ids)],
                    goals=0,
                    is_winner=False,
                )
            )
        await session.commit()

    svc = PairingService()
    await svc.generate_playoff_bracket(1)
    prerender.assert_awaited_once_with("SWISS", 1)

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.PLAYOFF, 1
        )
        matches = await MatchRepository(session).get_by_event(event.id)
        first_round_ids = {
            mp.player_id
            for match in matches
            if match.bracket_slot and match.bracket_slot.startswith("W-R1-")
            for mp in match.match_players
            if mp.player_id is not None
        }
        assert first_round_ids == set(top_ids)


def test_compute_playin_math_auto_qualifies_best_three_win_players():
    """With 8 seeds and a 24 target, the best 3-win players pass directly."""
    from bot.services.playin import compute_playin_math

    all_ps = []
    player_id = 1
    for seed in range(1, 9):
        all_ps.append(
            PlayerSeason(
                player_id=player_id,
                season_number=1,
                open_wins=0,
                open_gd=0,
                swiss_seed=seed,
            )
        )
        player_id += 1
    for wins, count in ((0, 8), (1, 14), (2, 14), (3, 12), (4, 8)):
        for index in range(count):
            all_ps.append(
                PlayerSeason(
                    player_id=player_id,
                    season_number=1,
                    open_wins=wins,
                    open_gd=count - index,
                    swiss_seed=None,
                )
            )
            player_id += 1

    math = compute_playin_math(all_ps)
    assert math["target"] == 24
    assert len(math["direct_three_wins"]) == 9
    assert len(math["remaining_three_wins"]) == 3
    assert math["base_qualifiers"] == 24
    assert math["over_by"] == 0


def test_compute_playin_math_targets_32():
    """78 players / 14 seeds target 18 qualifiers, with a 3-match cut."""
    from bot.services.playin import compute_playin_math

    all_ps = []
    player_id = 1
    for seed in range(1, 15):
        all_ps.append(
            PlayerSeason(
                player_id=player_id,
                season_number=1,
                open_wins=0,
                open_gd=0,
                swiss_seed=seed,
            )
        )
        player_id += 1

    win_counts = [(0, 4), (1, 16), (2, 25), (3, 14), (4, 5)]
    for wins, count in win_counts:
        for _ in range(count):
            all_ps.append(
                PlayerSeason(
                    player_id=player_id,
                    season_number=1,
                    open_wins=wins,
                    open_gd=0,
                    swiss_seed=None,
                )
            )
            player_id += 1

    math = compute_playin_math(all_ps)
    assert math["returning_seeds"] == 14
    assert len(math["one_wins"]) == 16
    assert len(math["two_wins"]) == 25
    assert len(math["three_wins"]) == 14
    assert len(math["four_wins"]) == 5
    assert len(math["auto_eliminated"]) == 4
    assert math["target"] == 18
    assert math["entrants"] == {1: 16, 2: 33, 3: 31}
    assert math["expected_matches"] == {1: 8, 2: 16, 3: 15}
    assert math["byes"] == {1: 0, 2: 1, 3: 1}
    assert math["base_qualifiers"] == 21
    assert math["qualifiers"] == 18
    assert math["over_by"] == 3
    assert math["cut_matches"] == 3
    assert sum(math["expected_matches"].values()) == 39


class TestTournamentFlow:
    async def test_start_playins_prerenders_finished_open(
        self, db, seed_data, monkeypatch
    ):
        from datetime import datetime
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        await Config().set_season_state("OPEN")
        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "Open", EventType.OPEN, 1
            )
            event.status = EventStatus.ACTIVE
            match_repo = MatchRepository(session)
            mp_repo = MatchPlayerRepository(session)
            for i in range(0, 8, 2):
                match = await match_repo.create(event.id)
                winner = await mp_repo.create(
                    match.id, side=0, player_id=seed_data["players"][i].id
                )
                loser = await mp_repo.create(
                    match.id, side=1, player_id=seed_data["players"][i + 1].id
                )
                winner.goals = 1
                winner.is_winner = True
                loser.goals = 0
                loser.is_winner = False
                match.completed_at = datetime.utcnow()
            await session.commit()

        prerender = AsyncMock()
        monkeypatch.setattr(
            "bot.services.tournament_flow.prerender", prerender
        )

        result = await TournamentFlowService().start_playins(1)

        assert result["ok"] is True
        prerender.assert_awaited_once_with("OPEN", 1)


class TestGenerateBeforeAdvance:
    async def test_start_playins_with_unplayed_open_keeps_state(self, db, seed_data):
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        await Config().set_season_state("OPEN")
        async with db.get_session() as session:
            event = await EventRepository(session).create("Open", EventType.OPEN, 1)
            match = await MatchRepository(session).create(event.id)
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(match.id, side=0, player_id=seed_data["players"][0].id)
            await mp_repo.create(match.id, side=1, player_id=seed_data["players"][1].id)
            await session.commit()

        result = await TournamentFlowService().start_playins(1)

        assert "Open not finished" in result["error"]
        assert await Config().get_season_state() == "OPEN"

    async def test_generation_error_keeps_state(self, db):
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        await Config().set_season_state("PLAYIN")
        flow = TournamentFlowService()
        flow.pairing_service.generate_swiss_round = AsyncMock(
            side_effect=ValueError("Swiss needs exactly 32 players")
        )

        result = await flow.start_swiss(1)

        assert "32 players" in result["error"]
        assert await Config().get_season_state() == "PLAYIN"

    async def test_successful_generation_advances_once(self, db):
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        await Config().set_season_state("SWISS")
        flow = TournamentFlowService()
        flow.pairing_service.generate_playoff_bracket = AsyncMock(return_value=[1, 2])

        first = await flow.start_playoffs(1)
        second = await flow.start_playoffs(1)

        assert first["advanced_from"] == "SWISS"
        assert second["advanced_from"] is None
        assert await Config().get_season_state() == "PLAYOFF"

    async def test_wrong_state_does_not_generate(self, db):
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        await Config().set_season_state("SIGNUP")
        flow = TournamentFlowService()
        flow.pairing_service.generate_swiss_round = AsyncMock()

        result = await flow.start_swiss(1)

        assert "Cannot do that from state SIGNUP" in result["error"]
        flow.pairing_service.generate_swiss_round.assert_not_awaited()


class TestReadiness:
    async def test_open_with_unplayed_is_blocked(self, db, seed_data):
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        await Config().set_season_state("OPEN")
        async with db.get_session() as session:
            event = await EventRepository(session).create("Open", EventType.OPEN, 1)
            await MatchRepository(session).create(event.id)
            await session.commit()

        ready = await TournamentFlowService().readiness(1)

        assert ready["action"] == "start_playins"
        assert ready["next_state"] == "PLAYIN"
        assert ready["ready"] is False
        assert "1 OPEN match(es) unplayed." in ready["blockers"]

    async def test_signup_with_players_is_ready(self, db, seed_data):
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        await Config().set_season_state("SIGNUP")
        ready = await TournamentFlowService().readiness(1)

        assert ready["ready"] is True
        assert ready["action"] == "start_open"


async def _swiss_with_results(db, completed: int, unplayed: int = 0):
    """Season 1 with a Swiss event: ``completed`` 1-0 matches plus open ones."""
    from datetime import datetime
    from bot.db import Player

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        players = []
        for i in range(32):
            p = Player(discord_id=9000 + i, name=f"S{i:02d}", country="US")
            session.add(p)
            await session.flush()
            session.add(PlayerSeason(player_id=p.id, season_number=1, hp=4))
            players.append(p)
        event = await EventRepository(session).create("Swiss", EventType.SWISS, 1)
        for i in range(completed + unplayed):
            match = Match(
                event_id=event.id,
                completed_at=datetime.utcnow() if i < completed else None,
            )
            session.add(match)
            await session.flush()
            winner, loser = players[i % 16], players[16 + i % 16]
            session.add(MatchPlayer(match_id=match.id, side=0, player_id=winner.id,
                                    goals=1, is_winner=i < completed))
            session.add(MatchPlayer(match_id=match.id, side=1, player_id=loser.id,
                                    goals=0, is_winner=False))
        await session.commit()


class TestPlayoffGate:
    async def test_incomplete_swiss_blocks_seeding(self, db, monkeypatch):
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        monkeypatch.setattr("bot.services.pairing.prerender", AsyncMock())
        await _swiss_with_results(db, completed=16, unplayed=1)
        await Config().set_season_state("SWISS")

        result = await TournamentFlowService().start_playoffs(1)

        assert "1 of 17 match(es) still unplayed" in result["error"]
        assert await Config().get_season_state() == "SWISS"

    async def test_wrong_qualifier_count_blocks_seeding(self, db, monkeypatch):
        from bot.services.pairing import PairingService

        monkeypatch.setattr("bot.services.pairing.prerender", AsyncMock())
        monkeypatch.setattr("bot.services.pairing.SWISS_BRACKET", [None] * 16)
        await _swiss_with_results(db, completed=16)

        with pytest.raises(ValueError, match="Expected 16 Swiss qualifiers"):
            await PairingService().generate_playoff_bracket(1)

    async def test_force_skips_gate(self, db, monkeypatch):
        from bot.services.pairing import PairingService

        monkeypatch.setattr("bot.services.pairing.prerender", AsyncMock())
        await _swiss_with_results(db, completed=16, unplayed=1)

        assert await PairingService().generate_playoff_bracket(1, force=True)

    async def test_complete_swiss_seeds_and_closes_event(self, db, monkeypatch):
        from bot.config import Config
        from bot.services.tournament_flow import TournamentFlowService

        monkeypatch.setattr("bot.services.pairing.prerender", AsyncMock())
        await _swiss_with_results(db, completed=16)
        await Config().set_season_state("SWISS")

        result = await TournamentFlowService().start_playoffs(1)

        assert result["ok"] is True
        assert await Config().get_season_state() == "PLAYOFF"
        async with db.get_session() as session:
            swiss = await EventRepository(session).get_by_type_and_season(EventType.SWISS, 1)
            assert swiss.status == EventStatus.COMPLETED


async def _archived_open_match(db, completed: bool = False) -> int:
    """Season 1 archived with one Open match, season 2 active."""
    from datetime import datetime
    from bot.db import Player

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ARCHIVED))
        session.add(Season(number=2, status=SeasonStatus.ACTIVE))
        p1 = Player(discord_id=7001, name="Old1", country="US")
        p2 = Player(discord_id=7002, name="Old2", country="US")
        session.add_all([p1, p2])
        await session.flush()
        event = await EventRepository(session).create("Open", EventType.OPEN, 1)
        match = Match(event_id=event.id, completed_at=datetime.utcnow() if completed else None)
        session.add(match)
        await session.flush()
        session.add(MatchPlayer(match_id=match.id, side=0, player_id=p1.id,
                                goals=1 if completed else None, is_winner=completed))
        session.add(MatchPlayer(match_id=match.id, side=1, player_id=p2.id,
                                goals=0 if completed else None, is_winner=False))
        await session.commit()
        return match.id


class TestArchivedSeasonGuard:
    async def test_archived_match_cannot_be_scored(self, db):
        from bot.services.match import MatchService

        match_id = await _archived_open_match(db)
        result = await MatchService().report_score(match_id, 2, 0, 0)
        assert result["error"] == "Season 1 is archived and read-only."

    async def test_archived_match_cannot_be_forfeited(self, db):
        from bot.services.match import MatchService

        match_id = await _archived_open_match(db)
        async with db.get_session() as session:
            mps = await MatchPlayerRepository(session).get_by_match(match_id)
        result = await MatchService().forfeit(match_id, mps[0].player_id)
        assert result == {"error": "Season 1 is archived and read-only."}

    async def test_rescoring_archived_match_keeps_stage(self, db):
        from bot.config import Config
        from bot.services.match import MatchService

        await Config().set_season_state("SWISS")
        match_id = await _archived_open_match(db, completed=True)
        result = await MatchService().report_score(match_id, 0, 3, 1)
        assert "archived" in result["error"]
        assert await Config().get_season_state() == "SWISS"

    async def test_report_list_excludes_old_season(self, db):
        from unittest.mock import MagicMock
        from bot.cogs.matches.match_cog import MatchCog

        match_id = await _archived_open_match(db)
        async with db.get_session() as session:
            mps = await MatchPlayerRepository(session).get_by_match(match_id)
        matches = await MatchCog(MagicMock())._active_player_matches(mps[0].player_id)
        assert matches == []


class TestResultSummary:
    async def test_bo1_summary_has_stage_players_and_score(self, db, seed_data):
        from bot.services.match import MatchService

        svc = MatchService()
        p1, p2 = seed_data["players"][:2]

        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "Swiss", EventType.SWISS, 1
            )
            event_id = event.id
            match = Match(
                event_id=event_id, bracket_slot="SW-R1-M3", series_game=1
            )
            session.add(match)
            await session.flush()
            session.add_all(
                [
                    MatchPlayer(match_id=match.id, side=0, player_id=p1.id),
                    MatchPlayer(match_id=match.id, side=1, player_id=p2.id),
                ]
            )
            await session.commit()
            match_id = match.id

        await svc.report_score(match_id, 5, 0, 0)
        summary = await svc.format_result_summary(match_id)

        assert f"Match {match_id}" in summary
        assert "Swiss" in summary
        assert "SW-R1-M3" in summary
        assert f"{p1.name} 5-0 {p2.name}" in summary
        assert "Series" not in summary

    async def test_bo3_summary_tracks_series_and_games(self, db, seed_data):
        from bot.services.match import MatchService
        from sqlalchemy import select, and_

        svc = MatchService()
        p1, p2 = seed_data["players"][:2]

        async with db.get_session() as session:
            event = await EventRepository(session).create(
                "Playoffs", EventType.PLAYOFF, 1
            )
            event_id = event.id
            source = Match(
                event_id=event_id, bracket_slot="W-R1-M3", series_game=1
            )
            next_winner = Match(
                event_id=event_id, bracket_slot="W-R2-M2", series_game=1
            )
            next_loser = Match(
                event_id=event_id, bracket_slot="L-R1-M2", series_game=1
            )
            session.add_all([source, next_winner, next_loser])
            await session.flush()
            for match in (source, next_winner, next_loser):
                session.add(
                    MatchPlayer(match_id=match.id, side=0, player_id=p1.id)
                )
                session.add(
                    MatchPlayer(match_id=match.id, side=1, player_id=p2.id)
                )
            await session.commit()
            match_id = source.id

        await svc.report_score(match_id, 5, 0, 0)
        summary = await svc.format_result_summary(match_id)
        assert "Series 1-0" in summary
        assert "G1 5-0" in summary

        async with db.get_session() as session:
            games = (
                await session.execute(
                    select(Match).where(
                        and_(
                            Match.event_id == event_id,
                            Match.bracket_slot == "W-R1-M3",
                        )
                    )
                )
            ).scalars().all()
            game2 = next(m for m in games if m.series_game == 2)
            game2_id = game2.id

        await svc.report_score(game2_id, 3, 2, 0)
        summary = await svc.format_result_summary(game2_id)
        assert "Series 2-0" in summary
        assert "G1 5-0, G2 3-2" in summary

    async def test_summary_for_unknown_match(self, db):
        from bot.services.match import MatchService

        assert await MatchService().format_result_summary(9999) == "Match 9999"


async def test_playin_render_previews_cut_round(db):
    """When R3 overproduces, /playins shows a predicted CUT column."""
    from bot.db.models import SeasonStatus
    from bot.db import Player
    from bot.rendering import StandingsMaker as standings
    from bot.services.playin import compute_playin_math

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        counter = 0
        # 24 seeds + 20 open players target 8; the open pool overproduces.
        for i in range(24):
            p = Player(discord_id=13000 + i, name=f"Seed{i}", country="US")
            session.add(p)
            await session.flush()
            session.add(
                PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=0,
                    swiss_seed=i + 1,
                    hp=4,
                )
            )
        for wins, count in ((4, 4), (3, 4), (2, 6), (1, 6)):
            for _ in range(count):
                p = Player(
                    discord_id=14000 + counter,
                    name=f"Open{counter}",
                    country="US",
                )
                session.add(p)
                await session.flush()
                session.add(
                    PlayerSeason(
                        player_id=p.id,
                        season_number=1,
                        open_wins=wins,
                        open_gd=0,
                        swiss_seed=None,
                        hp=4,
                    )
                )
                counter += 1
        await session.commit()

        all_ps = await PlayerSeasonRepository(session).get_by_season(1)
        math = compute_playin_math(all_ps)
        assert math["cut_matches"] >= 1

    layout = standings._compute_playin_layout(all_ps)
    assert layout["cut_matches"] == math["cut_matches"]
    assert layout["cut_players"] == math["cut_players"]


async def test_swiss_rescore_clears_and_refills_descendants(db):
    """Editing a Swiss result empties its downstream slots, then refills them."""
    from bot.db.models import SeasonStatus
    from bot.db import Player
    from bot.services.pairing import PairingService
    from bot.services.match import MatchService

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        for i in range(32):
            p = Player(discord_id=9200 + i, name=f"B{i}", country="US")
            session.add(p)
            await session.flush()
            session.add(
                PlayerSeason(
                    player_id=p.id,
                    season_number=1,
                    open_wins=1,
                    playin_rank=i + 1,
                    hp=4,
                )
            )
        await session.commit()

    pairing = PairingService()
    ms = MatchService()
    r1 = await pairing.generate_swiss_round(1, 1)
    first = r1[0]
    await ms.report_score(first.id, 5, 0, 0)

    def _r2_ids(matches):
        return {
            m.bracket_slot: sorted(
                mp.player_id
                for mp in m.match_players
                if mp.player_id is not None
            )
            for m in matches
            if m.series_game == 2
        }

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.SWISS, 1
        )
        before = _r2_ids(await MatchRepository(session).get_by_event(event.id))
        p1_id = next(
            mp.player_id
            for mp in first.match_players
            if mp.side == 0
        )
        p2_id = next(
            mp.player_id
            for mp in first.match_players
            if mp.side == 1
        )

    # Flip the winner: the winner slot must now hold the other player.
    result = await ms.report_score(first.id, 0, 5, 1)
    assert result["edited"] is True

    async with db.get_session() as session:
        event = await EventRepository(session).get_by_type_and_season(
            EventType.SWISS, 1
        )
        all_matches = await MatchRepository(session).get_by_event(event.id)
        after = _r2_ids(all_matches)
        assert len(all_matches) == 66

    filled_before = {slot: ids for slot, ids in before.items() if ids}
    filled_after = {slot: ids for slot, ids in after.items() if ids}
    assert len(filled_before) == 2
    assert len(filled_after) == 2
    # Same two slots stay filled, but the winner slot now holds the new winner.
    assert set(filled_before) == set(filled_after)
    assert filled_before["SW-R2-U1"] == [p1_id]
    assert filled_before["SW-R2-L1"] == [p2_id]
    assert filled_after["SW-R2-U1"] == [p2_id]
    assert filled_after["SW-R2-L1"] == [p1_id]


async def _playoff_season(db, gf_wins: int, winner_side: int = 0, pending_reset: bool = False):
    """Season 1 in PLAYOFF with ``gf_wins`` grand-final games.

    ``winner_side`` 0 is the winners-bracket champ; side 1 winning GF-M1 is
    what triggers a GF-M2 bracket reset (added unplayed with ``pending_reset``).
    """
    from datetime import datetime
    from bot.config import Config
    from bot.db import Player

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        champ = Player(discord_id=501, name="Champ", country="US")
        second = Player(discord_id=502, name="Second", country="US")
        session.add_all([champ, second])
        event = await EventRepository(session).create("Playoffs", EventType.PLAYOFF, 1)
        await session.flush()
        for game in range(1, gf_wins + 1):
            match = Match(event_id=event.id, bracket_slot="GF-M1", series_game=game,
                          completed_at=datetime.utcnow())
            session.add(match)
            await session.flush()
            session.add(MatchPlayer(match_id=match.id, side=0, player_id=champ.id,
                                    goals=3, is_winner=winner_side == 0))
            session.add(MatchPlayer(match_id=match.id, side=1, player_id=second.id,
                                    goals=1, is_winner=winner_side == 1))
        if pending_reset:
            reset = Match(event_id=event.id, bracket_slot="GF-M2", series_game=1)
            session.add(reset)
            await session.flush()
            session.add(MatchPlayer(match_id=reset.id, side=0, player_id=champ.id))
            session.add(MatchPlayer(match_id=reset.id, side=1, player_id=second.id))
        await session.commit()
    await Config().set_season_state("PLAYOFF")


class TestSeasonClose:
    async def test_close_with_incomplete_grand_final_fails(self, db):
        from bot.services.season import SeasonService

        await _playoff_season(db, gf_wins=1)
        result = await SeasonService().close_season(1)
        assert result["error"] == "The grand final is not complete yet."
        assert await SeasonService().get_state() == "PLAYOFF"

    async def test_pending_bracket_reset_blocks_close(self, db):
        from bot.services.season import SeasonService
        from bot.services.season_results import get_season_result

        await _playoff_season(db, gf_wins=2, winner_side=1, pending_reset=True)
        async with db.get_session() as session:
            result = await get_season_result(session, 1)
        assert result.champion is None and result.deciding_slot is None

        closed = await SeasonService().close_season(1)
        assert closed["error"] == "The grand final is not complete yet."
        assert (await SeasonService().get_season(1)).champion_id is None

    async def test_close_records_champion(self, db):
        from bot.services.season import SeasonService

        await _playoff_season(db, gf_wins=2)
        svc = SeasonService()
        result = await svc.close_season(1)

        assert result == {"ok": True, "season": 1, "champion": "Champ", "runner_up": "Second"}
        season = await svc.get_season(1)
        assert season.champion_id is not None and season.ended_at is not None
        assert await svc.get_state() == "OFFSEASON"
        async with db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(EventType.PLAYOFF, 1)
            assert event.status == EventStatus.COMPLETED

    async def test_create_requires_close_or_force(self, db):
        from bot.services.season import SeasonService

        await _playoff_season(db, gf_wins=2)
        svc = SeasonService()
        assert "still running" in await svc.create_blocker()
        assert await svc.create_season(2) is False
        assert await svc.create_season(2, force=True) is True

    async def test_create_after_close_succeeds(self, db):
        from bot.services.season import SeasonService

        await _playoff_season(db, gf_wins=2)
        svc = SeasonService()
        await svc.close_season(1)
        assert await svc.create_blocker() is None
        assert await svc.create_season(2) is True


class TestEventUniqueness:
    async def test_duplicate_insert_fails(self, db):
        import pytest
        from sqlalchemy.exc import IntegrityError

        async with db.get_session() as session:
            repo = EventRepository(session)
            await repo.create("Open", EventType.OPEN, 1)
            with pytest.raises(IntegrityError):
                await repo.create("Open again", EventType.OPEN, 1)

    async def test_concurrent_get_or_create_returns_one_event(self, tmp_path):
        """Uses a file DB: the in-memory fixture shares one connection."""
        import asyncio
        from sqlalchemy import func, select
        from bot.db import Event, get_db_manager, init_db_manager
        from bot.services.pairing import PairingService

        init_db_manager(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
        manager = get_db_manager()
        await manager.init()
        await manager.create_tables()
        try:
            events = await asyncio.gather(
                *(PairingService()._get_or_create_event(EventType.SWISS, 1) for _ in range(5))
            )
            async with manager.get_session() as session:
                count = (await session.execute(select(func.count(Event.id)))).scalar()
        finally:
            await manager.close()
        assert len({e.id for e in events}) == 1
        assert count == 1

    async def test_get_or_create_recovers_from_race(self, db, monkeypatch):
        from bot.db.repositories import EventRepository as Repo

        async with db.get_session() as session:
            first = await Repo(session).create("Open", EventType.OPEN, 1)
            await session.commit()

        real_get = Repo.get_by_type_and_season
        calls = {"n": 0}

        async def stale_then_real(self, event_type, season):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # another task has not committed yet, as far as we saw
            return await real_get(self, event_type, season)

        monkeypatch.setattr(Repo, "get_by_type_and_season", stale_then_real)
        async with db.get_session() as session:
            event = await Repo(session).get_or_create(EventType.OPEN, 1, "Open")
        assert event.id == first.id


class TestGrandFinalResetCleanup:
    @staticmethod
    async def _bracket(db, seed_data):
        """L-R6-M1 decided, GF-M1 won by the LB champ, GF-M2 already played."""
        from datetime import datetime

        a, b, c = seed_data["players"][:3]
        async with db.get_session() as session:
            event = await EventRepository(session).create("PO", EventType.PLAYOFF, 1)
            ids = {}
            for slot, (p0, p1), winner in (
                ("L-R6-M1", (b, c), 1),
                ("GF-M1", (a, c), 1),
                ("GF-M2", (a, c), 1),
            ):
                match = Match(event_id=event.id, bracket_slot=slot, series_game=1,
                              completed_at=datetime.utcnow())
                session.add(match)
                await session.flush()
                session.add(MatchPlayer(match_id=match.id, side=0, player_id=p0.id,
                                        goals=0, is_winner=winner == 0))
                session.add(MatchPlayer(match_id=match.id, side=1, player_id=p1.id,
                                        goals=2, is_winner=winner == 1))
                ids[slot] = match.id
            await session.commit()
            return event.id, ids

    @staticmethod
    async def _slot_count(db, event_id, slot):
        from sqlalchemy import func, select

        async with db.get_session() as session:
            return (await session.execute(
                select(func.count(Match.id)).where(
                    Match.event_id == event_id, Match.bracket_slot == slot
                )
            )).scalar()

    async def test_upstream_edit_deletes_bracket_reset(self, db, seed_data):
        from bot.services.match import MatchService

        event_id, ids = await self._bracket(db, seed_data)
        async with db.get_session() as session:
            feeder = await MatchRepository(session).get_by_id(ids["L-R6-M1"])
        await MatchService()._reset_playoff_after_edit(feeder)

        assert await self._slot_count(db, event_id, "GF-M2") == 0
        async with db.get_session() as session:
            gf1 = await MatchRepository(session).get_by_id(ids["GF-M1"])
            assert gf1.completed_at is None

    async def test_bracket_reset_creation_is_idempotent(self, db, seed_data):
        from bot.services.match import MatchService

        event_id, ids = await self._bracket(db, seed_data)
        svc = MatchService()
        async with db.get_session() as session:
            gf1 = await MatchRepository(session).get_by_id(ids["GF-M1"])
            lb_champ = next(mp.player_id for mp in gf1.match_players if mp.side == 1)
            await svc._handle_gf_bracket_reset(session, event_id, lb_champ, [gf1])
            await svc._handle_gf_bracket_reset(session, event_id, lb_champ, [gf1])
            await session.commit()

        assert await self._slot_count(db, event_id, "GF-M2") == 1
