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

        assert "Forfeited 1" in msg
        assert await _completed(db, current)
        assert not await _completed(db, old)
        async with db.get_session() as session:
            rows = (await session.execute(select(AuditLog))).scalars().all()
        assert [r.action for r in rows] == ["score.forfeit"]

    async def test_refuses_archived_season(self, db, seed_data):
        old = await _old_season_match(db, seed_data)

        msg = await MatchService().forfeit_all_player_matches(
            seed_data["players"][0].discord_id, season=0
        )

        assert "archived" in msg
        assert not await _completed(db, old)


