"""Tests for role-based command gating and per-season data isolation."""

from types import SimpleNamespace

import pytest
from discord.ext import commands
from discord import app_commands

from bot.config import Config, parse_role_ids
from bot.cogs.checks import host_predicate, host_predicate_app
from bot.db import (
    EventRepository,
    MatchRepository,
    MatchPlayerRepository,
    PlayerRepository,
    PlayerSeasonRepository,
    SeasonRepository,
)
from bot.db.models import Match, MatchPlayer, Player, PlayerSeason, Season, EventType, Event
from bot.services.dev import DevService, DUMMY_ID_BASE
from bot.services.season import SeasonService


def make_member(admin=False, role_ids=()):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(administrator=admin),
        roles=[SimpleNamespace(id=r) for r in role_ids],
    )


def make_ctx(member=None, guild=None):
    return SimpleNamespace(author=member, guild=guild)


def make_interaction(member=None, guild=None):
    return SimpleNamespace(user=member, guild=guild)


class TestParseRoleIds:
    def test_empty(self):
        assert parse_role_ids(None) == []
        assert parse_role_ids("") == []

    def test_comma_and_space_separated(self):
        assert parse_role_ids("111, 222 333") == [111, 222, 333]

    def test_skips_garbage(self):
        assert parse_role_ids("abc,123,junk45") == [123]


class TestConfigAdminRoles:
    async def test_roundtrip(self, db):
        cfg = Config()
        assert await cfg.get_admin_role_ids() == []
        await cfg.set("admin_roles", "1001,1002")
        assert await cfg.get_admin_role_ids() == [1001, 1002]

    async def test_remove(self, db):
        cfg = Config()
        await cfg.set("admin_roles", "555")
        assert await cfg.remove("admin_roles") is True
        assert await cfg.get_admin_role_ids() == []


class TestHostPredicate:
    async def test_admin_always_passes(self, db):
        ctx = make_ctx(member=make_member(admin=True), guild=SimpleNamespace())
        assert await host_predicate(ctx) is True

    async def test_configured_role_passes(self, db):
        await Config().set("admin_roles", "42")
        ctx = make_ctx(member=make_member(admin=False, role_ids=(42,)), guild=SimpleNamespace())
        assert await host_predicate(ctx) is True

    async def test_unconfigured_role_fails(self, db):
        ctx = make_ctx(member=make_member(admin=False, role_ids=(99,)), guild=SimpleNamespace())
        with pytest.raises(commands.MissingAnyRole):
            await host_predicate(ctx)

    async def test_no_roles_fails_even_configured(self, db):
        await Config().set("admin_roles", "42")
        ctx = make_ctx(member=make_member(admin=False, role_ids=()), guild=SimpleNamespace())
        with pytest.raises(commands.MissingAnyRole):
            await host_predicate(ctx)

    async def test_dm_fails(self, db):
        ctx = make_ctx(member=make_member(admin=True), guild=None)
        with pytest.raises(commands.CheckFailure):
            await host_predicate(ctx)

    async def test_app_predicate_mirrors_prefix(self, db):
        await Config().set("admin_roles", "7")
        ok = make_interaction(member=make_member(role_ids=(7,)), guild=SimpleNamespace())
        assert await host_predicate_app(ok) is True

        bad = make_interaction(member=make_member(), guild=SimpleNamespace())
        with pytest.raises(app_commands.CheckFailure):
            await host_predicate_app(bad)


@pytest.fixture
async def old_season_data(db):
    """Season 1 fully populated: players, registrations, event + scored match."""
    async with db.get_session() as session:
        season = Season(number=1)
        session.add(season)

        p_repo = PlayerRepository(session)
        ps_repo = PlayerSeasonRepository(session)
        pids = []
        for i in range(4):
            p = await p_repo.create(discord_id=2000 + i, name=f"Old{i}")
            await ps_repo.create(p.id, 1)
            pids.append(p.id)

        e_repo = EventRepository(session)
        event = await e_repo.create("S1 Open", EventType.OPEN, 1)

        m_repo = MatchRepository(session)
        mp_repo = MatchPlayerRepository(session)
        m = await m_repo.create(event.id)
        await mp_repo.create(m.id, 0, pids[0])
        await mp_repo.create(m.id, 1, pids[1])
        await session.commit()

    return {"season": 1, "player_discord_ids": [2000, 2001, 2002, 2003], "event": event, "match": m}


class TestSeasonPurge:
    async def test_create_new_season_wipes_old_data(self, db, old_season_data):
        svc = SeasonService()
        assert await svc.create_season(2) is True

        async with db.get_session() as session:
            players = (await session.execute(Player.__table__.select())).fetchall()
            seasons = (await session.execute(Season.__table__.select())).fetchall()
            events = (await session.execute(Event.__table__.select())).fetchall()
            matches = (await session.execute(Match.__table__.select())).fetchall()
            mps = (await session.execute(MatchPlayer.__table__.select())).fetchall()
            pss = (await session.execute(PlayerSeason.__table__.select())).fetchall()

        assert players == []
        assert matches == []
        assert mps == []
        assert pss == []
        assert [s.number for s in seasons] == [1, 2]  # archived + new
        assert events == []

    async def test_state_reset_on_create(self, db, old_season_data):
        from bot.config import Config
        await Config().set("stage", "PLAYOFF")

        svc = SeasonService()
        await svc.create_season(2)
        assert await svc.get_state() == "OFFSEASON"

    async def test_same_number_returns_false_unchanged(self, db, old_season_data):
        svc = SeasonService()
        # Reactivating season 1 itself → no purge of its own data
        assert await svc.create_season(1) is False

    async def test_purged_players_allow_dummy_reuse(self, db, old_season_data):
        """After purge, dummy IDs freed; new dummies start fresh."""
        svc = SeasonService()
        dev = DevService()
        await svc.create_season(2)

        start = await dev.next_dummy_start()
        assert start == DUMMY_ID_BASE

        ok, _ = await dev.dummy_register(2, DUMMY_ID_BASE, "Dummy0001", "us")
        assert ok

        # next call continues after the existing dummy
        assert await dev.next_dummy_start() == DUMMY_ID_BASE + 1


class TestNextDummyStart:
    async def test_empty_db_returns_base(self, db):
        assert await DevService().next_dummy_start() == DUMMY_ID_BASE

    async def test_continues_after_highest_dummy(self, db):
        async with db.get_session() as session:
            repo = PlayerRepository(session)
            await repo.create(discord_id=DUMMY_ID_BASE + 4, name="Dummy0005")
            await repo.create(discord_id=123, name="RealGuy")  # below base ignored
            await session.commit()

        assert await DevService().next_dummy_start() == DUMMY_ID_BASE + 5
