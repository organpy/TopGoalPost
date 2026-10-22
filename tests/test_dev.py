"""Tests for DevService and PlayerRepository.update."""

import pytest

from bot.db import (
    PlayerRepository,
    MatchRepository,
    MatchPlayerRepository,
    EventRepository,
    PlayerSeasonRepository,
    SettingRepository,
    EventType,
    EventStatus,
)
from bot.db.models import Season, PlayerSeason, Match, MatchPlayer
from bot.services.dev import DevService


class TestPlayerRepositoryUpdate:
    async def test_update_name(self, db):
        async with db.get_session() as session:
            repo = PlayerRepository(session)
            player = await repo.create(discord_id=1111, name="OldName")
            updated = await repo.update(player.id, name="NewName")
            assert updated is not None
            assert updated.name == "NewName"

    async def test_update_discord_id(self, db):
        async with db.get_session() as session:
            repo = PlayerRepository(session)
            player = await repo.create(discord_id=1111, name="Test")
            updated = await repo.update(player.id, discord_id=2222)
            assert updated is not None
            assert updated.discord_id == 2222

    async def test_update_nonexistent_returns_none(self, db):
        async with db.get_session() as session:
            repo = PlayerRepository(session)
            updated = await repo.update(99999, name="Ghost")
            assert updated is None


class TestDevService:
    async def test_dummy_register(self, db):
        async with db.get_session() as session:
            season = Season(number=1)
            session.add(season)
            sr = SettingRepository(session)
            await sr.set("season", "1")
            await session.commit()

        svc = DevService()
        ok, msg = await svc.dummy_register(1, 9000001, "Dummy001", "us")
        assert ok
        assert "Registered" in msg

    async def test_dummy_register_duplicate(self, db):
        async with db.get_session() as session:
            season = Season(number=1)
            session.add(season)
            sr = SettingRepository(session)
            await sr.set("season", "1")
            await session.commit()

        svc = DevService()
        ok1, _ = await svc.dummy_register(1, 9000001, "Dummy001", "us")
        assert ok1
        ok2, msg2 = await svc.dummy_register(1, 9000001, "Dummy001", "us")
        assert not ok2
        assert "already registered" in msg2

    async def test_dummy_register_no_active_season_repo_call(self, db):
        async with db.get_session() as session:
            season = Season(number=1)
            session.add(season)
            sr = SettingRepository(session)
            await sr.set("season", "1")
            await session.commit()

        svc = DevService()
        ok, _ = await svc.dummy_register(1, 9000005, "Dummy005", "jp")
        assert ok

        async with db.get_session() as session:
            repo = PlayerRepository(session)
            player = await repo.get_by_discord_id(9000005)
            assert player is not None
            assert player.name == "Dummy005"
            assert player.country == "jp"

    async def test_dummy_score_all(self, db):
        from bot.services.match import MatchService

        async with db.get_session() as session:
            season = Season(number=1)
            session.add(season)
            sr = SettingRepository(session)
            await sr.set("season", "1")
            prepo = PlayerRepository(session)
            p1 = await prepo.create(discord_id=1001, name="P1")
            p2 = await prepo.create(discord_id=1002, name="P2")
            ps_repo = PlayerSeasonRepository(session)
            await ps_repo.create(p1.id, 1)
            await ps_repo.create(p2.id, 1)
            er = EventRepository(session)
            event = await er.create("Open", EventType.OPEN, 1)
            mr = MatchRepository(session)
            mp_repo = MatchPlayerRepository(session)
            m1 = await mr.create(event.id)
            await mp_repo.create(m1.id, 0, p1.id)
            await mp_repo.create(m1.id, 1, p2.id)
            m2 = await mr.create(event.id)
            await mp_repo.create(m2.id, 0, p1.id)
            await mp_repo.create(m2.id, 1, p2.id)
            await session.commit()

        svc = DevService()
        scored = await svc.dummy_score_all()
        assert scored == 2

        async with db.get_session() as session:
            mr = MatchRepository(session)
            import sqlalchemy as sa
            stmt = sa.select(Match).where(Match.completed_at.is_not(None))
            result = await session.execute(stmt)
            completed = result.scalars().all()
            assert len(completed) == 2

    async def test_dummy_score_all_no_unplayed(self, db):
        svc = DevService()
        scored = await svc.dummy_score_all()
        assert scored == 0

    async def test_dummy_score_all_completes_bo3(self, db):
        """The loop must score the second/third BO3 game it creates."""
        async with db.get_session() as session:
            prepo = PlayerRepository(session)
            p1 = await prepo.create(discord_id=2001, name="P1")
            p2 = await prepo.create(discord_id=2002, name="P2")
            er = EventRepository(session)
            event = await er.create("Playoff", EventType.PLAYOFF, 1)
            mr = MatchRepository(session)
            match = await mr.create(event.id)
            match.bracket_slot = "W-R1-M1"
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(match.id, 0, p1.id)
            await mp_repo.create(match.id, 1, p2.id)
            await session.commit()

        svc = DevService()
        scored = await svc.dummy_score_all()
        assert scored in (2, 3)

        async with db.get_session() as session:
            mr = MatchRepository(session)
            matches = await mr.get_by_event(event.id)
            assert len(matches) == scored
            assert all(m.completed_at is not None for m in matches)

    async def test_next_dummy_start_ignores_real_ids(self, db):
        from bot.services.dev import DUMMY_ID_BASE

        async with db.get_session() as session:
            repo = PlayerRepository(session)
            await repo.create(discord_id=DUMMY_ID_BASE + 5, name="Dummy")
            await repo.create(discord_id=10_000_000, name="RealLooking")
            await session.commit()

        svc = DevService()
        assert await svc.next_dummy_start() == DUMMY_ID_BASE + 6

    async def test_dummy_replace_id(self, db):
        async with db.get_session() as session:
            repo = PlayerRepository(session)
            await repo.create(discord_id=1001, name="TestPlayer")
            await session.commit()

        svc = DevService()
        ok, msg = await svc.dummy_replace_id(1001, 9999)
        assert ok
        assert "1001" in msg and "9999" in msg

        async with db.get_session() as session:
            repo = PlayerRepository(session)
            player = await repo.get_by_discord_id(9999)
            assert player is not None
            assert player.name == "TestPlayer"
            old = await repo.get_by_discord_id(1001)
            assert old is None

    async def test_dummy_replace_id_not_found(self, db):
        svc = DevService()
        ok, msg = await svc.dummy_replace_id(99999, 1)
        assert not ok
        assert "No player found" in msg

    async def test_dummy_replace_id_collision(self, db):
        async with db.get_session() as session:
            repo = PlayerRepository(session)
            await repo.create(discord_id=1001, name="A")
            await repo.create(discord_id=1002, name="B")
            await session.commit()

        svc = DevService()
        ok, msg = await svc.dummy_replace_id(1001, 1002)
        assert not ok
        assert "already in use" in msg
