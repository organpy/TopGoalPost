"""Bulk per-player match actions: forfeit_all and remove_from_matches."""

from sqlalchemy import select

from bot.db import EventRepository, MatchPlayerRepository, MatchRepository
from bot.db.models import AuditLog, EventType, Match, Season, SeasonStatus
from bot.services.match import MatchService


async def _match(db, event_id, p1, p2):
    async with db.get_session() as session:
        m = await MatchRepository(session).create(event_id)
        mp_repo = MatchPlayerRepository(session)
        await mp_repo.create(m.id, 0, p1)
        await mp_repo.create(m.id, 1, p2)
        await session.commit()
        return m.id


async def _old_season_match(db, seed_data):
    """A season-0 Open match for the same players, left unplayed."""
    players = seed_data["players"]
    async with db.get_session() as session:
        session.add(Season(number=0, status=SeasonStatus.ARCHIVED))
        old = await EventRepository(session).create("S0 Open", EventType.OPEN, 0)
        await session.commit()
    return await _match(db, old.id, players[0].id, players[2].id)


async def _completed(db, match_id):
    async with db.get_session() as session:
        return (await session.get(Match, match_id)).completed_at is not None


class TestForfeitAll:
    async def test_uses_forfeit_and_stays_in_season(self, db, seed_data, open_event):
        players = seed_data["players"]
        current = await _match(db, open_event.id, players[0].id, players[1].id)
        old = await _old_season_match(db, seed_data)

        msg = await MatchService().forfeit_all_player_matches(
            players[0].discord_id, season=1
        )

        assert "forfeited 1" in msg
        assert await _completed(db, current)
        assert not await _completed(db, old)
        async with db.get_session() as session:
            rows = (await session.execute(select(AuditLog))).scalars().all()
        assert sorted(r.action for r in rows) == ["player.withdraw", "score.forfeit"]

    async def test_refuses_archived_season(self, db, seed_data):
        old = await _old_season_match(db, seed_data)

        msg = await MatchService().forfeit_all_player_matches(
            seed_data["players"][0].discord_id, season=0
        )

        assert "archived" in msg
        assert not await _completed(db, old)


class TestRemoveFromMatches:
    async def test_only_touches_given_season(self, db, seed_data, open_event):
        players = seed_data["players"]
        current = await _match(db, open_event.id, players[0].id, players[1].id)
        old = await _old_season_match(db, seed_data)

        await MatchService().remove_player_from_matches(players[0].discord_id, 1)

        async with db.get_session() as session:
            repo = MatchPlayerRepository(session)
            current_ids = {mp.player_id for mp in await repo.get_by_match(current)}
            old_ids = {mp.player_id for mp in await repo.get_by_match(old)}
        assert players[0].id not in current_ids
        assert players[0].id in old_ids


class TestWithdraw:
    async def _chain(self, db, event_id, a, b, c):
        """m1: a vs b; m2: winner(m1) vs c, fed by propagation."""
        m1 = await _match(db, event_id, a, b)
        async with db.get_session() as session:
            m2 = await MatchRepository(session).create(event_id)
            m2.p1_prereq_id = m1
            m2.p1_prereq_type = "winner"
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(m2.id, 0, None)
            await mp_repo.create(m2.id, 1, c)
            await session.commit()
            return m1, m2.id

    async def _winner(self, db, match_id):
        async with db.get_session() as session:
            match = await MatchRepository(session).get_by_id(match_id)
            w = match.get_winner()
            return w.player_id if w else None

    async def test_withdrawn_winner_does_not_advance(self, db, seed_data):
        a, b, c = (p for p in seed_data["players"][:3])
        async with db.get_session() as session:
            event = await EventRepository(session).create("S1 Playin", EventType.PLAYIN, 1)
            await session.commit()
        m1, m2 = await self._chain(db, event.id, a.id, b.id, c.id)
        svc = MatchService()

        # b withdraws first, then a withdraws: a's forfeit hands b the win,
        # but b is withdrawn so m2 must resolve to c without more commands.
        await svc.withdraw_player(b.discord_id, season=1)
        await svc.withdraw_player(a.discord_id, season=1)

        assert await _completed(db, m1)
        assert await _completed(db, m2)
        assert await self._winner(db, m2) == c.id

    async def test_withdraw_players_flags_everyone_first(self, db, seed_data):
        a, b, c = (p for p in seed_data["players"][:3])
        async with db.get_session() as session:
            event = await EventRepository(session).create("S1 Playin", EventType.PLAYIN, 1)
            await session.commit()
        m1, m2 = await self._chain(db, event.id, a.id, b.id, c.id)

        msg = await MatchService().withdraw_players(
            [a.discord_id, b.discord_id, 999999999999999999], season=1
        )

        assert "Withdrew 2" in msg and "Skipped" in msg
        assert await _completed(db, m2)
        assert await self._winner(db, m2) == c.id

    async def test_win_all_advances_player_through_chain(self, db, seed_data):
        a, b, c = (p for p in seed_data["players"][:3])
        async with db.get_session() as session:
            event = await EventRepository(session).create("S1 Playin", EventType.PLAYIN, 1)
            await session.commit()
        m1, m2 = await self._chain(db, event.id, a.id, b.id, c.id)

        msg = await MatchService().win_all_player_matches(a.discord_id, season=1)

        assert "2 match" in msg
        assert await self._winner(db, m1) == a.id
        assert await self._winner(db, m2) == a.id
