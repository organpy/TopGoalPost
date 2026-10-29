"""Tests for role-based command gating and season archiving."""

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


class TestSeasonArchiving:
    async def test_create_new_season_keeps_old_data(self, db, old_season_data):
        svc = SeasonService()
        assert await svc.create_season(2, force=True) is True

        async with db.get_session() as session:
            players = (await session.execute(Player.__table__.select())).fetchall()
            seasons = (await session.execute(Season.__table__.select())).fetchall()
            events = (await session.execute(Event.__table__.select())).fetchall()
            matches = (await session.execute(Match.__table__.select())).fetchall()
            mps = (await session.execute(MatchPlayer.__table__.select())).fetchall()
            pss = (await session.execute(PlayerSeason.__table__.select())).fetchall()

        assert len(players) == 4
        assert len(matches) == 1
        assert len(mps) == 2
        assert len(pss) == 4
        assert [s.number for s in seasons] == [1, 2]  # archived + new
        assert [s.status.value for s in seasons] == ["archived", "active"]
        assert len(events) == 1 and events[0].season == 1

    async def test_archived_season_cannot_be_reopened(self, db, old_season_data):
        svc = SeasonService()
        await svc.create_season(2, force=True)

        assert await svc.create_season(1) is False
        assert (await svc.get_season(1)).status.value == "archived"
        assert (await svc.get_season(2)).status.value == "active"

    async def test_old_season_stats_survive_rollover(self, db, old_season_data):
        from datetime import datetime

        from sqlalchemy import select

        from bot.services.player_stats import OpenState, PlayerStatsService

        async with db.get_session() as session:
            match = await session.get(Match, old_season_data["match"].id)
            match.completed_at = datetime.utcnow()
            mps = (
                await session.execute(
                    select(MatchPlayer)
                    .where(MatchPlayer.match_id == match.id)
                    .order_by(MatchPlayer.side)
                )
            ).scalars().all()
            mps[0].goals = 3
            mps[0].is_winner = True
            mps[1].goals = 1
            await session.commit()

            player = await PlayerRepository(session).get_by_discord_id(2000)
            player_id = player.id

        await SeasonService().create_season(2, force=True)

        stats = await PlayerStatsService().get_stats(player_id, 1)
        assert stats.open_state is OpenState.PLAYED
        assert stats.open.games == 1
        assert stats.open.wins == 1
        assert stats.open.goals_for == 3
        assert stats.open.goals_against == 1

    async def test_state_reset_on_create(self, db, old_season_data):
        from bot.config import Config
        await Config().set("stage", "PLAYOFF")

        svc = SeasonService()
        await svc.create_season(2, force=True)
        assert await svc.get_state() == "OFFSEASON"

    async def test_same_number_returns_false_unchanged(self, db, old_season_data):
        svc = SeasonService()
        assert await svc.create_season(1) is False

    async def test_dummy_ids_continue_after_archived_dummies(self, db, old_season_data):
        """Archived players keep their IDs, so new dummies continue after them."""
        dev = DevService()
        ok, _ = await dev.dummy_register(1, DUMMY_ID_BASE, "Dummy0001", "us")
        assert ok

        await SeasonService().create_season(2)

        assert await dev.next_dummy_start() == DUMMY_ID_BASE + 1

    async def test_unplayed_matches_are_season_scoped(self, db, old_season_data):
        """An abandoned old season must not block the current one."""
        async with db.get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(2000)
            repo = MatchPlayerRepository(session)
            assert await repo.get_unplayed_player_matches(player.id, season=1)
            assert await repo.get_unplayed_player_matches(player.id, season=2) == []


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


def test_host_commands_do_not_require_administrator():
    """Host roles come from `admin_roles`; Discord admin-only defaults would hide them."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "bot"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "require_host_app" in (text := path.read_text())
        and "default_permissions(administrator=True)" in text
    ]
    assert offenders == []


def _assert_host_checked(command):
    checks = {getattr(check, "__name__", "") for check in command.checks}
    assert "host_predicate" in checks or "host_predicate_app" in checks, (
        f"{command.qualified_name} is missing a host check"
    )


def test_all_season_and_admin_commands_require_host():
    """Season/admin commands must reject non-hosts even if invoked directly."""
    from bot.cogs.admin.admin_cog import AdminCog
    from bot.cogs.admin.dev_cog import DevCog
    from bot.cogs.admin.player_cog import PlayerCog
    from bot.cogs.admin.season_cog import SeasonAdminCog
    from bot.cogs.matches.match_cog import MatchCog
    from bot.cogs.tournament.tournament_cog import TournamentCog
    from bot.ui.dashboard import DashboardCog

    prefix_cogs = (SeasonAdminCog, AdminCog, DevCog, TournamentCog)
    for cog in prefix_cogs:
        for command in cog.__cog_commands__:
            _assert_host_checked(command)

    app_groups = (SeasonAdminCog.season, PlayerCog.player, TournamentCog.tourney)
    for group in app_groups:
        for command in group.commands:
            _assert_host_checked(command)

    for command in AdminCog.__cog_app_commands__:
        _assert_host_checked(command)

    admin_match_commands = {"list_matches", "set_score", "set_score_playoff"}
    for command in MatchCog.__cog_app_commands__:
        if command.name in admin_match_commands:
            _assert_host_checked(command)

    _assert_host_checked(DashboardCog.dashboard_cmd)
