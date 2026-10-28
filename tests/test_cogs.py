"""Mock-based tests for cog-layer command logic.

Uses unittest.mock to simulate discord.py objects so we can test
cog command handlers without a live bot connection.

NOTE: Tests bypass discord.py command wrappers (decorators) by calling
the underlying _impl methods or .callback directly, since the decorators
require a live bot connection to bind properly.
"""

import pytest

try:
    import discord
except ImportError:
    pytest.skip("discord.py not installed — skipping cog tests", allow_module_level=True)

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

import pytest_asyncio

from bot.cogs.tournament.tournament_cog import TournamentCog
from bot.cogs.signup.signup_cog import SignupCog
from bot.cogs.admin.season_cog import SeasonAdminCog
from bot.cogs.matches.match_cog import MatchCog
from bot.cogs.standings.stats_cog import StatsCog
from bot.cogs.standings.standings_cog import StandingsCog
from bot.db import init_db_manager, get_db_manager, PlayerRepository
from bot.db.repositories import PlayerSeasonRepository
from bot.ui.dashboard import DashboardView
from bot.config import Config


@pytest_asyncio.fixture(autouse=True)
async def mock_db():
    """Initialize in-memory SQLite database so cogs can construct without error."""
    init_db_manager("sqlite+aiosqlite://")
    manager = get_db_manager()
    await manager.init()
    await manager.create_tables()
    config = Config()
    await config.init_defaults()
    yield
    await manager.drop_tables()
    await manager.close()


@pytest.fixture
def mock_interaction():
    """Create a minimal mock discord.Interaction."""
    interaction = MagicMock()
    interaction.response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.user = MagicMock()
    interaction.user.id = 123456789
    interaction.user.name = "TestUser"
    interaction.guild = MagicMock()
    interaction.guild.id = 999
    interaction.guild.get_channel = MagicMock(return_value=None)
    interaction.guild.get_member = MagicMock(return_value=None)
    return interaction


@pytest.fixture
def mock_ctx():
    """Create a minimal mock commands.Context."""
    ctx = MagicMock()
    ctx.send = AsyncMock()
    ctx.guild = MagicMock()
    ctx.guild.id = 999
    ctx.guild.get_channel = MagicMock(return_value=None)
    ctx.guild.get_member = MagicMock(return_value=None)
    return ctx


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = 111111
    bot.change_presence = AsyncMock()
    return bot


class TestTournamentCog:
    """Verify tournament cog delegates to services correctly."""

    async def test_open_command_delegates(self, mock_ctx, mock_bot):
        cog = TournamentCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        cog.flow_service.start_open = AsyncMock(
            return_value={"ok": True, "state": "OPEN", "advanced_from": "SIGNUP", "message": "done"}
        )
        cog._cleanup_stage_roles = AsyncMock()

        await cog._open_impl(mock_ctx)

        cog.flow_service.start_open.assert_awaited_once_with(1)
        cog._cleanup_stage_roles.assert_awaited_once_with(mock_ctx.guild, "SIGNUP", "OPEN")
        mock_ctx.send.assert_awaited_with("done")

    async def test_open_wrong_state(self, mock_ctx, mock_bot):
        cog = TournamentCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        cog.flow_service.season_service.get_state = AsyncMock(return_value="PLAYOFF")
        cog.flow_service.pairing_service.generate_open = AsyncMock()

        await cog._open_impl(mock_ctx)

        cog.flow_service.pairing_service.generate_open.assert_not_awaited()
        msg = mock_ctx.send.call_args[0][0]
        assert "Cannot do that from state PLAYOFF" in msg

    async def test_open_no_season(self, mock_ctx, mock_bot):
        cog = TournamentCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=None)

        await cog._open_impl(mock_ctx)

        mock_ctx.send.assert_awaited_with("No active season.")

    async def test_playin_command(self, mock_ctx, mock_bot):
        cog = TournamentCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        cog.flow_service.start_playins = AsyncMock(
            return_value={"ok": True, "state": "PLAYIN", "advanced_from": None, "message": "ok"}
        )

        await cog._playin_impl(mock_ctx)

        cog.flow_service.start_playins.assert_awaited_once_with(1)

    async def test_finish_playins(self, mock_ctx, mock_bot):
        cog = TournamentCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        cog.flow_service.finish_playins = AsyncMock(return_value={"ok": True, "message": "ok"})

        await cog.finish_playins_cmd.callback(cog, mock_ctx)

        cog.flow_service.finish_playins.assert_awaited_once_with(1)


class TestSignupCog:
    """Verify signup cog validates and delegates."""

    async def test_editname_valid(self, mock_interaction, mock_bot):
        cog = SignupCog(mock_bot)
        cog.signup_service.edit_name = AsyncMock(return_value=(True, "Name changed!"))

        await cog.editname_cmd.callback(cog, mock_interaction, "NewName")

        cog.signup_service.edit_name.assert_awaited_once_with(123456789, "NewName")

    async def test_editcountry_delegates(self, mock_interaction, mock_bot):
        cog = SignupCog(mock_bot)
        cog.signup_service.edit_country = AsyncMock(
            return_value=(True, "Country changed!")
        )

        await cog.editcountry_cmd.callback(cog, mock_interaction, "Germany")

        cog.signup_service.edit_country.assert_awaited_once_with(
            123456789, "Germany"
        )
        mock_interaction.response.send_message.assert_awaited_once_with(
            "Country changed!", ephemeral=True
        )

    async def test_signup_delegates(self, mock_interaction, mock_bot):
        cog = SignupCog(mock_bot)
        cog.signup_service.register = AsyncMock(return_value=(True, "Registered!"))

        await cog.signup_cmd.callback(cog, mock_interaction, "PlayerName", "us")

        cog.signup_service.register.assert_awaited_once_with(123456789, "PlayerName", "us")


class TestSeasonAdminCog:
    """Verify season admin cog delegates."""

    async def test_backup_creates_readable_copy(self, mock_ctx, mock_bot, tmp_path, monkeypatch):
        import sqlite3

        real_db = tmp_path / "bot.db"
        with sqlite3.connect(real_db) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{real_db}")
        monkeypatch.setattr("bot.services.health.PROJECT_ROOT", tmp_path)

        cog = SeasonAdminCog(mock_bot)
        await cog.backup_cmd.callback(cog, mock_ctx)

        [backup] = (tmp_path / "data" / "backups").glob("bot_*.db")
        with sqlite3.connect(backup) as conn:
            assert conn.execute("SELECT x FROM t").fetchone() == (42,)
        assert "Backup created" in mock_ctx.send.call_args[0][0]


    def test_slash_season_group_has_subcommands(self, mock_bot):
        cog = SeasonAdminCog(mock_bot)
        group = next(c for c in cog.__cog_app_commands__ if c.name == "season")
        assert {c.name for c in group.commands} == {"status", "create", "advance", "close"}
        assert group.default_permissions is None

    async def test_slash_advance_illegal_transition(self, mock_interaction, mock_bot, db):
        from discord import app_commands

        cog = SeasonAdminCog(mock_bot)
        await cog.slash_advance.callback(
            cog, mock_interaction, app_commands.Choice(name="SWISS", value="SWISS")
        )
        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "Next allowed state is SIGNUP" in msg

    async def test_slash_advance_force(self, mock_interaction, mock_bot, db):
        from discord import app_commands

        cog = SeasonAdminCog(mock_bot)
        await cog.slash_advance.callback(
            cog, mock_interaction, app_commands.Choice(name="SWISS", value="SWISS"), True
        )
        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "OFFSEASON → SWISS** (forced)" in msg
        assert await cog.season_service.get_state() == "SWISS"


class TestMatchCog:
    """Verify match cog validates participants."""

    async def test_report_requires_match_player(self, mock_interaction, mock_bot):
        cog = MatchCog(mock_bot)
        from bot.db import PlayerRepository
        with patch.object(PlayerRepository, 'get_by_discord_id', AsyncMock(return_value=None)):
            await cog.report_cmd.callback(cog, mock_interaction)
            mock_interaction.response.send_message.assert_awaited_with(
                "You are not a registered player.", ephemeral=True
            )

    async def test_report_ui_sends_score_view(self, mock_interaction, mock_bot):
        from bot.ui.report import ReportScoreView

        cog = MatchCog(mock_bot)
        player_mock = MagicMock()
        player_mock.id = 42
        from bot.db import PlayerRepository
        matches = [{"match_id": 1, "side": 0, "label": "#1 SWISS: A vs B"}]
        with patch.object(
            PlayerRepository, "get_by_discord_id", AsyncMock(return_value=player_mock)
        ), patch.object(
            MatchCog, "_active_player_matches", AsyncMock(return_value=matches)
        ):
            await cog.report_cmd.callback(cog, mock_interaction)

        view = mock_interaction.response.send_message.call_args.kwargs["view"]
        assert isinstance(view, ReportScoreView)
        assert view.match_id == 1
        assert view.reporter_side == 0

    async def test_set_score_autocomplete_shows_completed_score(
        self, mock_interaction, mock_bot
    ):
        from datetime import datetime
        from bot.db import Event, EventType, Match, MatchPlayer, Player
        from bot.db.models import Season, SeasonStatus

        async with get_db_manager().get_session() as session:
            session.add(Season(number=1, status=SeasonStatus.ACTIVE))
            await session.flush()
            p1 = Player(discord_id=1, name="Alice", country="US")
            p2 = Player(discord_id=2, name="Bob", country="US")
            session.add_all([p1, p2])
            await session.flush()
            event = Event(name="Open", type=EventType.OPEN, season=1)
            session.add(event)
            await session.flush()
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
                        player_id=p1.id,
                        goals=3,
                        is_winner=True,
                    ),
                    MatchPlayer(
                        match_id=match.id,
                        side=1,
                        player_id=p2.id,
                        goals=1,
                        is_winner=False,
                    ),
                ]
            )
            await session.commit()
            match_id = match.id

        cog = MatchCog(mock_bot)
        choices = await cog.admin_match_autocomplete(mock_interaction, "")

        assert any(
            f"#{match_id}" in choice.name and "3-1" in choice.name
            for choice in choices
        )

    async def test_report_view_maps_score_by_reporter_side(self, mock_interaction):
        from bot.ui.report import ReportScoreView

        view = ReportScoreView(
            [{"match_id": 7, "side": 1, "label": "#7 SWISS: A vs B"}],
            mock_interaction.user.id,
        )
        with patch(
            "bot.ui.report._send_confirmation", AsyncMock()
        ) as send_confirmation:
            await view.report_score(mock_interaction, 4, 2)

        mock_interaction.response.defer.assert_awaited_once()
        send_confirmation.assert_awaited_once_with(
            mock_interaction,
            7,
            mock_interaction.user.id,
            2,
            4,
            1,
            4,
            2,
        )

    async def test_report_view_preset_button_reports_score(self, mock_interaction):
        from bot.ui.report import ReportScoreView, ScoreButton

        view = ReportScoreView(
            [{"match_id": 7, "side": 1, "label": "#7 SWISS: A vs B"}],
            mock_interaction.user.id,
        )
        button = next(
            child
            for child in view.children
            if isinstance(child, ScoreButton) and child.label == "5-0"
        )

        with patch(
            "bot.ui.report._send_confirmation", AsyncMock()
        ) as send_confirmation:
            await button.callback(mock_interaction)

        send_confirmation.assert_awaited_once_with(
            mock_interaction,
            7,
            mock_interaction.user.id,
            0,
            5,
            1,
            5,
            0,
        )

    async def test_report_view_custom_opens_modal(self, mock_interaction):
        from bot.ui.report import ReportScoreView, ScoreReportModal

        view = ReportScoreView(
            [{"match_id": 7, "side": 0, "label": "#7 SWISS: A vs B"}],
            mock_interaction.user.id,
        )
        await view.custom_score(mock_interaction)

        modal = mock_interaction.response.send_modal.call_args.args[0]
        assert isinstance(modal, ScoreReportModal)
        assert modal.match_id == 7
        assert modal.reporter_side == 0

    async def test_set_score_playoff_creates_next_game(
        self, mock_interaction, mock_bot
    ):
        from discord import app_commands
        from bot.db import EventRepository, EventType, Match, MatchPlayer, PlayerRepository

        async with get_db_manager().get_session() as session:
            event = await EventRepository(session).create(
                "Playoffs", EventType.PLAYOFF, 1
            )
            p1 = await PlayerRepository(session).create(1, "P1")
            p2 = await PlayerRepository(session).create(2, "P2")
            match = Match(
                event_id=event.id, bracket_slot="W-R1-M1", series_game=1
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

        cog = MatchCog(mock_bot)
        await cog.set_score_playoff_cmd.callback(
            cog,
            mock_interaction,
            match_id,
            app_commands.Choice(name="Game 1", value="g1"),
            5,
            3,
        )

        async with get_db_manager().get_session() as session:
            from sqlalchemy import select

            matches = (
                await session.execute(
                    select(Match).where(Match.bracket_slot == "W-R1-M1")
                )
            ).scalars().all()
        assert sorted(m.series_game for m in matches) == [1, 2]
        game2_id = next(m.id for m in matches if m.series_game == 2)

        # Split the first two games; game 3 must be created.
        await cog.set_score_playoff_cmd.callback(
            cog,
            mock_interaction,
            game2_id,
            app_commands.Choice(name="Game 2", value="g2"),
            2,
            5,
        )

        async with get_db_manager().get_session() as session:
            matches = (
                await session.execute(
                    select(Match).where(Match.bracket_slot == "W-R1-M1")
                )
            ).scalars().all()
        assert sorted(m.series_game for m in matches) == [1, 2, 3]

    async def test_set_score_playoff_rejects_missing_game(
        self, mock_interaction, mock_bot
    ):
        from discord import app_commands
        from bot.db import EventRepository, EventType, Match, MatchPlayer, PlayerRepository

        async with get_db_manager().get_session() as session:
            event = await EventRepository(session).create(
                "Playoffs", EventType.PLAYOFF, 1
            )
            p1 = await PlayerRepository(session).create(1, "P1")
            p2 = await PlayerRepository(session).create(2, "P2")
            match = Match(
                event_id=event.id, bracket_slot="W-R1-M1", series_game=1
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

        cog = MatchCog(mock_bot)
        await cog.set_score_playoff_cmd.callback(
            cog,
            mock_interaction,
            match_id,
            app_commands.Choice(name="Game 2", value="g2"),
            5,
            3,
        )
        mock_interaction.response.send_message.assert_awaited_with(
            "Game 2 does not exist for this series yet.", ephemeral=True
        )

    async def test_report_autocomplete_loads_nested_players(self, mock_interaction, mock_bot):
        from bot.db import EventRepository, EventType, Match, MatchPlayer, Season, SeasonStatus

        async with get_db_manager().get_session() as session:
            session.add(Season(number=1, status=SeasonStatus.ACTIVE))
            event = await EventRepository(session).create(
                name="Autocomplete Open",
                event_type=EventType.OPEN,
                season=1,
            )
            reporter = await PlayerRepository(session).create(
                discord_id=mock_interaction.user.id,
                name="Reporter",
            )
            opponent = await PlayerRepository(session).create(
                discord_id=987654321,
                name="Opponent",
            )
            match = Match(event_id=event.id)
            session.add(match)
            await session.flush()
            session.add_all([
                MatchPlayer(match_id=match.id, side=0, player_id=reporter.id),
                MatchPlayer(match_id=match.id, side=1, player_id=opponent.id),
            ])
            await session.commit()

        cog = MatchCog(mock_bot)
        choices = await cog.match_autocomplete(mock_interaction, "")

        assert len(choices) == 1
        assert "Opponent" in choices[0].name


class TestStatsCog:
    """Discord-only regressions that don't require a live gateway."""

    @staticmethod
    async def _seed_season(session, number: int = 1):
        from bot.db.models import Season, SeasonStatus

        session.add(Season(number=number, status=SeasonStatus.ACTIVE))
        await session.flush()

    async def test_profile_sends_embed_for_registered_player(
        self, mock_interaction, mock_bot
    ):
        async with get_db_manager().get_session() as session:
            await self._seed_season(session)
            player = await PlayerRepository(session).create(
                discord_id=mock_interaction.user.id,
                name="ProfileTester",
            )
            await PlayerSeasonRepository(session).create(
                player_id=player.id, season_number=1
            )
            await session.commit()

        cog = StatsCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)

        await cog.profile_cmd.callback(cog, mock_interaction)

        mock_interaction.followup.send.assert_awaited()
        embed = mock_interaction.followup.send.call_args.kwargs.get("embed")
        assert embed is not None
        assert embed.title == "ProfileTester"
        assert mock_interaction.followup.send.call_args.kwargs["ephemeral"] is True

    async def _profile_embed(self, mock_interaction, mock_bot, **season_kwargs):
        from bot.db.repositories import PlayerSeasonRepository

        async with get_db_manager().get_session() as session:
            await self._seed_season(session)
            player = await PlayerRepository(session).create(
                discord_id=mock_interaction.user.id,
                name="ProfileTester",
            )
            ps = await PlayerSeasonRepository(session).create(
                player_id=player.id, season_number=1
            )
            for key, value in season_kwargs.items():
                setattr(ps, key, value)
            await session.commit()

        cog = StatsCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        await cog.profile_cmd.callback(cog, mock_interaction)
        return mock_interaction.followup.send.call_args.kwargs.get("embed")

    async def test_profile_marks_open_as_skipped(self, mock_interaction, mock_bot):
        embed = await self._profile_embed(
            mock_interaction, mock_bot, swiss_seed=3, hp=4
        )

        fields = {field.name: field.value for field in embed.fields}
        assert "Skipped Open" in fields["Open"]
        assert "#3" in fields["Open"]
        assert fields["HP"] == "4"
        # A zeroed Open record must never be shown as 0 - 0.
        assert not any("0 - 0" in field.value for field in embed.fields)

    async def test_profile_says_did_not_play_when_open_is_empty(
        self, mock_interaction, mock_bot
    ):
        embed = await self._profile_embed(mock_interaction, mock_bot)

        fields = {field.name: field.value for field in embed.fields}
        assert fields["Open"] == "Did not play"

    async def test_profile_shows_open_record_for_players_who_played(
        self, mock_interaction, mock_bot
    ):
        from bot.db.models import Match, MatchPlayer
        from bot.db.repositories import EventRepository, EventType

        async with get_db_manager().get_session() as session:
            await self._seed_season(session)
            player = await PlayerRepository(session).create(
                discord_id=mock_interaction.user.id,
                name="ProfileTester",
            )
            opponent = await PlayerRepository(session).create(
                discord_id=555, name="Rival"
            )
            await PlayerSeasonRepository(session).create(
                player_id=player.id, season_number=1
            )
            event = await EventRepository(session).create(
                name="Open", event_type=EventType.OPEN, season=1
            )
            for mine, theirs in ((5, 1), (3, 0)):
                match = Match(event_id=event.id, completed_at=datetime.utcnow())
                session.add(match)
                await session.flush()
                session.add_all([
                    MatchPlayer(
                        match_id=match.id, side=0, player_id=player.id,
                        goals=mine, is_winner=True,
                    ),
                    MatchPlayer(
                        match_id=match.id, side=1, player_id=opponent.id,
                        goals=theirs, is_winner=False,
                    ),
                ])
            await session.commit()

        cog = StatsCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        await cog.profile_cmd.callback(cog, mock_interaction)
        embed = mock_interaction.followup.send.call_args.kwargs.get("embed")

        fields = {field.name: field.value for field in embed.fields}
        assert fields["Open"] == "2-0 · GD +7"
        assert "Skipped Open" not in fields["Open"]
        assert fields["Record"] == "**2-0**"
        assert fields["Goals"] == "**8-1** (+7)"
        assert fields["Form"] == "🟩🟩"
        assert "vs Rival" in fields["Recent matches"]

        # Profile and history share one message: History is a tab.
        view = mock_interaction.followup.send.call_args.kwargs["view"]
        assert view.tab == "profile"
        assert view.history[0].title == "Match history · ProfileTester"
        assert "2-0 in 2 matches" in view.history[0].footer.text

        mock_interaction.followup.send.reset_mock()
        await cog.history_cmd.callback(cog, mock_interaction)
        kwargs = mock_interaction.followup.send.call_args.kwargs
        assert kwargs["embed"].title == "Match history · ProfileTester"
        assert kwargs["view"].tab == "history"
        assert kwargs["view"].profile.title == "ProfileTester"

    async def test_season_summary_command_renders_image(
        self, mock_interaction, mock_bot, tmp_path
    ):
        from bot.db.models import Event, EventStatus, EventType, Match, MatchPlayer

        async with get_db_manager().get_session() as session:
            await self._seed_season(session)
            alice = await PlayerRepository(session).create(
                discord_id=101, name="Alice"
            )
            bob = await PlayerRepository(session).create(
                discord_id=102, name="Bob"
            )
            event = Event(
                name="Open",
                type=EventType.OPEN,
                season=1,
                status=EventStatus.COMPLETED,
            )
            session.add(event)
            await session.flush()
            match = Match(event_id=event.id, completed_at=datetime.utcnow())
            session.add(match)
            await session.flush()
            session.add_all([
                MatchPlayer(
                    match_id=match.id,
                    side=0,
                    player_id=alice.id,
                    goals=5,
                    is_winner=True,
                ),
                MatchPlayer(
                    match_id=match.id,
                    side=1,
                    player_id=bob.id,
                    goals=2,
                    is_winner=False,
                ),
            ])
            await session.commit()

        cog = StatsCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        output = tmp_path / "season_summary.png"

        def fake_render(_summary, filename):
            Image.new("RGB", (20, 20)).save(filename)
            return filename

        with patch(
            "bot.cogs.standings.stats_cog.asset_path",
            return_value=str(output),
        ), patch(
            "bot.cogs.standings.stats_cog.render_season_summary",
            side_effect=fake_render,
        ):
            await cog.season_summary_cmd.callback(cog, mock_interaction, 1)

        mock_interaction.followup.send.assert_awaited()
        kwargs = mock_interaction.followup.send.call_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "file" in kwargs
        assert output.exists()

    async def test_season_summary_command_rejects_empty_season(
        self, mock_interaction, mock_bot
    ):
        async with get_db_manager().get_session() as session:
            await self._seed_season(session)
            await session.commit()

        cog = StatsCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        await cog.season_summary_cmd.callback(cog, mock_interaction, 1)

        message = mock_interaction.followup.send.call_args[0][0]
        assert "No completed matches" in message
        assert mock_interaction.followup.send.call_args.kwargs["ephemeral"] is True


class TestDashboardView:
    """Dashboard buttons must call cog methods, not app_commands.Command objects."""

    async def test_open_board_uses_public_standings_handler(self, mock_bot):
        standings_cog = MagicMock()
        standings_cog.show_open = AsyncMock()
        mock_bot.get_cog.return_value = standings_cog

        view = DashboardView(mock_bot, MagicMock())
        interaction = MagicMock()

        await view.open_board_btn.callback(interaction)

        standings_cog.show_open.assert_awaited_once_with(interaction)


class TestWeeklyTimeslotModal:
    @staticmethod
    def _modal(weekday, start, finish):
        from bot.ui.scheduling import WeeklyTimeslotModal

        modal = WeeklyTimeslotModal()
        modal.weekday._value = weekday
        modal.start_time._value = start
        modal.finish_time._value = finish
        return modal

    async def test_rejects_bad_weekday(self, mock_interaction):
        modal = self._modal("Funday", "19:00", "21:00")
        await modal.on_submit(mock_interaction)

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "weekday" in msg.lower()
        assert mock_interaction.response.send_message.call_args[1]["ephemeral"] is True

    async def test_rejects_bad_time(self, mock_interaction):
        modal = self._modal("Wednesday", "nope", "21:00")
        await modal.on_submit(mock_interaction)

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "start time" in msg.lower()

    async def test_rejects_reversed_range(self, mock_interaction):
        modal = self._modal("Wednesday", "21:00", "19:00")
        await modal.on_submit(mock_interaction)

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "after the start" in msg

    async def test_adds_weekly_slot(self, mock_interaction):
        from bot.services.scheduler import SchedulerService

        modal = self._modal("Wednesday", "19:00", "21:00")
        await modal.on_submit(mock_interaction)

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "Wednesday" in msg
        assert "19:00" in msg and "21:00" in msg

        slots = await SchedulerService().get_weekly_timeslots(
            mock_interaction.user.id
        )
        assert len(slots) == 1
        assert slots[0].weekday == 2

    async def test_duplicate_slot_is_reported(self, mock_interaction):
        from bot.services.scheduler import SchedulerService

        await SchedulerService().add_weekly_timeslot(
            mock_interaction.user.id, 2, 1140, 1260
        )
        modal = self._modal("Wed", "19:00", "21:00")
        await modal.on_submit(mock_interaction)

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "already exists" in msg


class TestRemoveTimeslotCommand:
    async def test_removes_dated_slot(self, mock_interaction, mock_bot):
        from bot.cogs.scheduling.schedule_cog import ScheduleCog
        from bot.services.scheduler import SchedulerService

        svc = SchedulerService()
        await svc.add_timeslot(mock_interaction.user.id, 1000, 2000)
        slot = (await svc.get_user_timeslots(mock_interaction.user.id))[0]

        cog = ScheduleCog(mock_bot)
        await cog.remove_timeslot.callback(
            cog, mock_interaction, f"date:{slot.id}"
        )

        assert await svc.get_user_timeslots(mock_interaction.user.id) == []

    async def test_removes_weekly_slot(self, mock_interaction, mock_bot):
        from bot.cogs.scheduling.schedule_cog import ScheduleCog
        from bot.services.scheduler import SchedulerService

        svc = SchedulerService()
        await svc.add_weekly_timeslot(mock_interaction.user.id, 4, 600, 700)
        slot = (await svc.get_weekly_timeslots(mock_interaction.user.id))[0]

        cog = ScheduleCog(mock_bot)
        await cog.remove_timeslot.callback(
            cog, mock_interaction, f"weekly:{slot.id}"
        )

        assert await svc.get_weekly_timeslots(mock_interaction.user.id) == []

    async def test_rejects_free_text(self, mock_interaction, mock_bot):
        from bot.cogs.scheduling.schedule_cog import ScheduleCog

        cog = ScheduleCog(mock_bot)
        await cog.remove_timeslot.callback(cog, mock_interaction, "garbage")

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "Pick a slot" in msg


class TestOfferTimePermissions:
    async def test_non_admin_cannot_send_text(self, mock_interaction, mock_bot):
        from bot.cogs.scheduling.schedule_cog import ScheduleCog

        cog = ScheduleCog(mock_bot)
        target = MagicMock()
        target.bot = False
        target.id = 42

        with patch(
            "bot.cogs.scheduling.schedule_cog._is_host", AsyncMock(return_value=False)
        ):
            await cog.offer_time.callback(
                cog, mock_interaction, target, 15, "19:00", "custom text"
            )

        # The command defers immediately so a slow DM cannot expire the token.
        mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        msg = mock_interaction.followup.send.call_args[0][0]
        assert "Only admins" in msg
        assert mock_interaction.followup.send.call_args[1]["ephemeral"] is True

    async def test_admin_can_target_anyone_with_text(self, mock_interaction, mock_bot):
        from bot.cogs.scheduling.schedule_cog import ScheduleCog
        from bot.services.scheduler import SchedulerService

        cog = ScheduleCog(mock_bot)
        await SchedulerService().set_timezone(mock_interaction.user.id, "UTC")

        target = MagicMock()
        target.bot = False
        target.id = 42
        target.send = AsyncMock()

        sent = {}

        async def _send(**kwargs):
            sent.update(kwargs)
            msg = MagicMock()
            msg.edit = AsyncMock()
            return msg

        target.send.side_effect = _send

        with patch(
            "bot.cogs.scheduling.schedule_cog._is_host", AsyncMock(return_value=True)
        ), patch(
            "bot.cogs.scheduling.schedule_cog.MatchConfirmView"
        ) as view_cls:
            view_cls.return_value.wait = AsyncMock()
            view_cls.return_value.value = None
            mock_interaction.user.send = AsyncMock()
            await cog.offer_time.callback(
                cog, mock_interaction, target, 15, "19:00", "Bring your A game"
            )

        target.send.assert_awaited_once()
        assert any(
            field.name == "Message" and "Bring your A game" in field.value
            for field in sent["embed"].fields
        )
        # Admins bypass the shared-match requirement entirely.
        mock_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        assert (
            mock_interaction.followup.send.call_args.kwargs["embed"].title
            == "📨 Offer sent"
        )


class TestPlayerAdmin:
    """The /player group is the single way to edit a player's record."""

    @staticmethod
    def _target(discord_id: int = 42):
        target = MagicMock()
        target.id = discord_id
        target.mention = f"<@{discord_id}>"
        return target

    async def _register(self, discord_id: int = 42, name: str = "Old", **fields):
        from bot.db.models import Season, SeasonStatus

        async with get_db_manager().get_session() as session:
            session.add(Season(number=1, status=SeasonStatus.ACTIVE))
            player = await PlayerRepository(session).create(
                discord_id=discord_id, name=name, country="US"
            )
            ps = await PlayerSeasonRepository(session).create(
                player_id=player.id, season_number=1
            )
            for key, value in fields.items():
                setattr(ps, key, value)
            await session.commit()
            return player.id

    @staticmethod
    def _sent(interaction) -> str:
        call = interaction.followup.send.call_args
        return call[0][0] if call[0] else call[1]["content"]

    def test_every_subcommand_requires_a_host(self):
        from bot.cogs.admin.player_cog import PlayerCog

        names = {cmd.name for cmd in PlayerCog.player.commands}
        assert names == {
            "info", "set", "stats", "add_weekly", "add_date", "remove_slot",
            "clear_slots", "remove_from_matches", "delete_matches",
            "forfeit_all", "substitute", "reset_matches", "switch_id",
        }
        for cmd in PlayerCog.player.commands:
            assert cmd.checks, f"{cmd.name} is missing require_host_app"

    async def test_info_shows_identity_and_availability(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog
        from bot.services.scheduler import SchedulerService

        await self._register()
        scheduler = SchedulerService()
        await scheduler.set_timezone(42, "Europe/Berlin")
        await scheduler.add_weekly_timeslot(42, 2, 19 * 60, 21 * 60)

        cog = PlayerCog(mock_bot)
        await cog.info_cmd.callback(cog, mock_interaction, self._target())

        embed = mock_interaction.followup.send.call_args[1]["embed"]
        fields = {field.name: field.value for field in embed.fields}
        assert embed.title == "Player: Old"
        assert fields["Country"] == "US"
        assert fields["Timezone"] == "Europe/Berlin"
        assert "Wednesday" in fields["Availability (1)"]
        assert mock_interaction.followup.send.call_args[1]["ephemeral"] is True

    async def test_info_marks_a_skipped_open(self, mock_interaction, mock_bot):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register(swiss_seed=4, hp=4)

        cog = PlayerCog(mock_bot)
        await cog.info_cmd.callback(cog, mock_interaction, self._target())

        fields = {
            field.name: field.value
            for field in mock_interaction.followup.send.call_args[1]["embed"].fields
        }
        assert "Skipped Open" in fields["Season 1"]
        assert "0 - 0" not in fields["Season 1"]

    async def test_set_changes_only_supplied_fields(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register()
        cog = PlayerCog(mock_bot)
        target = self._target()

        await cog.set_cmd.callback(
            cog, mock_interaction, target, "NewName", "Germany", None
        )

        async with get_db_manager().get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(42)
            assert player.name == "NewName"
            assert player.country == "de"
        body = self._sent(mock_interaction)
        assert "name:" in body and "country:" in body
        assert "timezone:" not in body

    async def test_set_accepts_a_two_letter_code_and_clears(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register()
        cog = PlayerCog(mock_bot)
        target = self._target()

        await cog.set_cmd.callback(cog, mock_interaction, target, None, "br", None)
        async with get_db_manager().get_session() as session:
            assert (await PlayerRepository(session).get_by_discord_id(42)).country == "br"

        await cog.set_cmd.callback(cog, mock_interaction, target, None, "none", None)
        async with get_db_manager().get_session() as session:
            assert (await PlayerRepository(session).get_by_discord_id(42)).country is None

    async def test_set_rejects_bad_country_and_timezone(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register()
        cog = PlayerCog(mock_bot)
        target = self._target()

        await cog.set_cmd.callback(
            cog, mock_interaction, target, None, "Atlantis", "Mars/Olympus"
        )

        body = self._sent(mock_interaction)
        assert "Unknown country" in body
        assert "not a valid IANA timezone" in body
        async with get_db_manager().get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(42)
            assert player.country == "US"

    async def test_set_without_options_changes_nothing(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register()
        cog = PlayerCog(mock_bot)
        await cog.set_cmd.callback(cog, mock_interaction, self._target())

        assert "Nothing to change" in self._sent(mock_interaction)

    async def test_set_timezone_writes_it(self, mock_interaction, mock_bot):
        from bot.cogs.admin.player_cog import PlayerCog
        from bot.services.scheduler import SchedulerService

        await self._register()
        cog = PlayerCog(mock_bot)
        await cog.set_cmd.callback(
            cog, mock_interaction, self._target(), None, None, "Asia/Tokyo"
        )

        assert await SchedulerService().get_timezone(42) == "Asia/Tokyo"

    async def test_set_rejects_unknown_player(self, mock_interaction, mock_bot):
        from bot.cogs.admin.player_cog import PlayerCog

        cog = PlayerCog(mock_bot)
        await cog.set_cmd.callback(
            cog, mock_interaction, self._target(999), "Someone", None, None
        )

        assert "not registered" in self._sent(mock_interaction)

    async def test_stats_updates_existing_row(self, mock_interaction, mock_bot):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register()
        cog = PlayerCog(mock_bot)
        await cog.stats_cmd.callback(
            cog, mock_interaction, self._target(),
            seed=3, playin_rank=2, hp=1, open_wins=5, open_losses=1, open_gd=9,
        )

        async with get_db_manager().get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(42)
            ps = await PlayerSeasonRepository(session).get(
                player_id=player.id, season_number=1
            )
            assert (ps.swiss_seed, ps.playin_rank, ps.hp) == (3, 2, 1)
            assert (ps.open_wins, ps.open_losses, ps.open_gd) == (5, 1, 9)

    async def test_stats_creates_a_missing_season_row(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog
        from bot.db.models import Season, SeasonStatus

        async with get_db_manager().get_session() as session:
            session.add(Season(number=1, status=SeasonStatus.ACTIVE))
            player = await PlayerRepository(session).create(
                discord_id=42, name="Old"
            )
            await session.commit()
            player_id = player.id

        cog = PlayerCog(mock_bot)
        await cog.stats_cmd.callback(
            cog, mock_interaction, self._target(), hp=2
        )

        async with get_db_manager().get_session() as session:
            ps = await PlayerSeasonRepository(session).get(
                player_id=player_id, season_number=1
            )
            assert ps is not None and ps.hp == 2

    async def test_stats_zero_clears_seed_and_rank(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register(swiss_seed=3, playin_rank=7)
        cog = PlayerCog(mock_bot)
        await cog.stats_cmd.callback(
            cog, mock_interaction, self._target(), seed=0, playin_rank=0
        )

        async with get_db_manager().get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(42)
            ps = await PlayerSeasonRepository(session).get(
                player_id=player.id, season_number=1
            )
            assert ps.swiss_seed is None
            assert ps.playin_rank is None

    async def test_stats_without_options_changes_nothing(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register()
        cog = PlayerCog(mock_bot)
        await cog.stats_cmd.callback(cog, mock_interaction, self._target())

        assert "Nothing to change" in self._sent(mock_interaction)

    async def test_add_weekly_writes_for_the_target(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog
        from bot.services.scheduler import SchedulerService

        await self._register()
        cog = PlayerCog(mock_bot)
        await cog.add_weekly_cmd.callback(
            cog, mock_interaction, self._target(), "Wed", "19:00", "21:30"
        )

        slots = await SchedulerService().get_weekly_timeslots(42)
        assert len(slots) == 1
        assert (slots[0].weekday, slots[0].start_minute, slots[0].finish_minute) == (
            2, 19 * 60, 21 * 60 + 30,
        )
        assert "Wednesday" in self._sent(mock_interaction)

    async def test_add_weekly_rejects_bad_input(self, mock_interaction, mock_bot):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register()
        cog = PlayerCog(mock_bot)
        await cog.add_weekly_cmd.callback(
            cog, mock_interaction, self._target(), "Someday", "19:00", "21:00"
        )
        assert "Unknown weekday" in self._sent(mock_interaction)

        await cog.add_weekly_cmd.callback(
            cog, mock_interaction, self._target(), "Wed", "21:00", "19:00"
        )
        assert "after the start" in self._sent(mock_interaction)

    async def test_add_date_uses_the_targets_timezone(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog
        from bot.services.scheduler import SchedulerService

        await self._register()
        await SchedulerService().set_timezone(42, "Asia/Tokyo")
        cog = PlayerCog(mock_bot)

        await cog.add_date_cmd.callback(
            cog, mock_interaction, self._target(), 15, "19:00", "21:00", True
        )

        slots = await SchedulerService().get_user_timeslots(42)
        assert len(slots) == 1 and slots[0].prio == 1
        assert "Added" in self._sent(mock_interaction)

    async def test_add_date_requires_a_target_timezone(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog

        await self._register()
        cog = PlayerCog(mock_bot)
        await cog.add_date_cmd.callback(
            cog, mock_interaction, self._target(), 15, "19:00", "21:00"
        )

        assert "no valid timezone" in self._sent(mock_interaction)

    async def test_remove_slot_handles_both_kinds(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog
        from bot.services.scheduler import SchedulerService

        await self._register()
        scheduler = SchedulerService()
        dated = await scheduler.add_timeslot(42, 1000, 2000)
        weekly = await scheduler.add_weekly_timeslot(42, 2, 600, 700)
        cog = PlayerCog(mock_bot)

        await cog.remove_slot_cmd.callback(
            cog, mock_interaction, self._target(), f"date:{dated.id}"
        )
        assert await scheduler.get_user_timeslots(42) == []

        await cog.remove_slot_cmd.callback(
            cog, mock_interaction, self._target(), f"weekly:{weekly.id}"
        )
        assert await scheduler.get_weekly_timeslots(42) == []

    async def test_remove_slot_rejects_a_foreign_id(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin.player_cog import PlayerCog
        from bot.services.scheduler import SchedulerService

        await self._register()
        scheduler = SchedulerService()
        await scheduler.add_weekly_timeslot(7, 2, 600, 700)
        cog = PlayerCog(mock_bot)

        # Another user's slot id must not be removable through this target.
        await cog.remove_slot_cmd.callback(
            cog, mock_interaction, self._target(), "weekly:1"
        )
        assert len(await scheduler.get_weekly_timeslots(7)) == 1

    async def test_clear_slots_needs_confirmation(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin import player_cog as module
        from bot.services.scheduler import SchedulerService

        await self._register()
        scheduler = SchedulerService()
        await scheduler.add_weekly_timeslot(42, 2, 600, 700)
        cog = module.PlayerCog(mock_bot)

        with patch.object(module, "confirm", AsyncMock(return_value=False)):
            await cog.clear_slots_cmd.callback(
                cog, mock_interaction, self._target()
            )
        assert len(await scheduler.get_weekly_timeslots(42)) == 1

        with patch.object(module, "confirm", AsyncMock(return_value=True)):
            await cog.clear_slots_cmd.callback(
                cog, mock_interaction, self._target()
            )
        assert await scheduler.get_weekly_timeslots(42) == []

    async def test_match_commands_delegate_to_the_service(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin import player_cog as module

        await self._register()
        cog = module.PlayerCog(mock_bot)
        target = self._target()
        cog.match_service = MagicMock()
        for method in (
            "remove_player_from_matches",
            "delete_player_matches",
            "forfeit_all_player_matches",
            "substitute_player",
        ):
            setattr(cog.match_service, method, AsyncMock(return_value="done"))

        old_player = MagicMock(id=1, name="Old", discord_id=42)
        new_player = MagicMock(id=2, name="New", discord_id=7)
        cog.stats_service.resolve_player = AsyncMock(
            side_effect=[old_player, new_player]
        )

        with patch.object(module, "confirm", AsyncMock(return_value=True)):
            await cog.remove_from_matches_cmd.callback(
                cog, mock_interaction, target
            )
            await cog.delete_matches_cmd.callback(cog, mock_interaction, target)
            await cog.forfeit_all_cmd.callback(cog, mock_interaction, target)
            await cog.substitute_cmd.callback(
                cog, mock_interaction, "Old", "New"
            )

        cog.match_service.remove_player_from_matches.assert_awaited_once_with(42, 1)
        cog.match_service.delete_player_matches.assert_awaited_once_with(42, 1)
        cog.match_service.forfeit_all_player_matches.assert_awaited_once_with(
            42, season=1
        )
        cog.match_service.substitute_player.assert_awaited_once_with(1, 2, 1)

    async def test_substitute_creates_a_name_only_placeholder(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin import player_cog as module

        await self._register()
        cog = module.PlayerCog(mock_bot)
        cog.match_service = MagicMock()
        cog.match_service.substitute_player = AsyncMock(return_value="done")
        old_player = MagicMock(id=1, name="Old", discord_id=42, country="br")
        newcomer = MagicMock(id=9, name="Ghost", discord_id=-1)
        cog.stats_service.resolve_player = AsyncMock(
            side_effect=[old_player, None]
        )
        cog._create_substitute_player = AsyncMock(return_value=newcomer)
        mock_interaction.guild.get_member_named.return_value = None
        mock_interaction.guild.members = []

        with patch.object(module, "confirm", AsyncMock(return_value=True)):
            await cog.substitute_cmd.callback(
                cog, mock_interaction, "Old", "Ghost"
            )

        cog._create_substitute_player.assert_awaited_once_with(
            "Ghost", None, None
        )
        cog.match_service.substitute_player.assert_awaited_once_with(1, 9, 1)

    async def test_substitute_applies_explicit_country(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin import player_cog as module

        await self._register()
        cog = module.PlayerCog(mock_bot)
        cog.match_service = MagicMock()
        cog.match_service.substitute_player = AsyncMock(return_value="done")
        old_player = MagicMock(id=1, name="Old", discord_id=42, country="br")
        newcomer = MagicMock(id=9, name="Ghost", discord_id=-1)
        cog.stats_service.resolve_player = AsyncMock(
            side_effect=[old_player, None]
        )
        cog._create_substitute_player = AsyncMock(return_value=newcomer)
        mock_interaction.guild.get_member_named.return_value = None
        mock_interaction.guild.members = []

        with patch.object(module, "confirm", AsyncMock(return_value=True)):
            await cog.substitute_cmd.callback(
                cog, mock_interaction, "Old", "Ghost", "Germany"
            )

        cog._create_substitute_player.assert_awaited_once_with(
            "Ghost", None, "de"
        )
        cog.match_service.substitute_player.assert_awaited_once_with(1, 9, 1)

    async def test_substitute_updates_existing_player_country(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin import player_cog as module

        old_id = await self._register(discord_id=42, name="Old")
        async with get_db_manager().get_session() as session:
            new = await PlayerRepository(session).create(
                discord_id=7, name="New", country="us"
            )
            await PlayerSeasonRepository(session).create(
                player_id=new.id, season_number=1
            )
            await session.commit()
            new_id = new.id
        cog = module.PlayerCog(mock_bot)
        cog.match_service = MagicMock()
        cog.match_service.substitute_player = AsyncMock(return_value="done")
        old_player = MagicMock(id=old_id, name="Old", discord_id=42, country="br")
        new_player = MagicMock(id=new_id, name="New", discord_id=7, country="us")
        cog.stats_service.resolve_player = AsyncMock(
            side_effect=[old_player, new_player]
        )
        mock_interaction.guild = None

        with patch.object(module, "confirm", AsyncMock(return_value=True)):
            await cog.substitute_cmd.callback(
                cog, mock_interaction, "Old", "New", "Germany"
            )

        async with get_db_manager().get_session() as session:
            stored = await PlayerRepository(session).get_by_id(new_id)
            assert stored.country == "de"
        cog.match_service.substitute_player.assert_awaited_once_with(
            old_id, new_id, 1
        )

    async def test_create_substitute_player_saves_country(self, mock_bot):
        from bot.cogs.admin import player_cog as module

        cog = module.PlayerCog(mock_bot)
        player = await cog._create_substitute_player("Ghost", None, "br")

        async with get_db_manager().get_session() as session:
            stored = await PlayerRepository(session).get_by_id(player.id)
            assert stored.country == "br"

    async def test_reset_matches_delegates_to_the_service(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin import player_cog as module

        await self._register()
        cog = module.PlayerCog(mock_bot)
        cog.match_service = MagicMock()
        cog.match_service.reset_open_matches = AsyncMock(return_value="reset")
        cog.stats_service.resolve_player = AsyncMock(
            return_value=MagicMock(id=1, name="Old", discord_id=42)
        )

        with patch.object(module, "confirm", AsyncMock(return_value=True)):
            await cog.reset_matches_cmd.callback(
                cog, mock_interaction, "Old"
            )

        cog.match_service.reset_open_matches.assert_awaited_once_with(1, 1)

    async def test_switch_id_updates_the_player(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin import player_cog as module

        await self._register(discord_id=42, name="Old")
        cog = module.PlayerCog(mock_bot)

        await cog.switch_id_cmd.callback(
            cog, mock_interaction, "Old", "777"
        )

        async with get_db_manager().get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(777)
            assert player is not None
            assert player.name == "Old"
            assert await PlayerRepository(session).get_by_discord_id(42) is None

    async def test_match_commands_respect_cancel(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.admin import player_cog as module

        await self._register()
        cog = module.PlayerCog(mock_bot)
        cog.match_service = MagicMock()
        cog.match_service.forfeit_all_player_matches = AsyncMock()

        with patch.object(module, "confirm", AsyncMock(return_value=False)):
            await cog.forfeit_all_cmd.callback(
                cog, mock_interaction, self._target()
            )

        cog.match_service.forfeit_all_player_matches.assert_not_awaited()
        assert "Cancelled" in self._sent(mock_interaction)

class TestSignupLock:
    async def _season(self, state: str):
        from bot.db.models import Season, SeasonStatus

        async with get_db_manager().get_session() as session:
            session.add(Season(number=1, status=SeasonStatus.ACTIVE))
            await session.commit()
        await Config().set_season_state(state)

    async def test_view_rejects_after_signup_closes(self, mock_interaction):
        from bot.ui.signup_button import SignupView

        await self._season("OPEN")
        view = SignupView()
        await view.open_signup.callback(mock_interaction)

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "closed" in msg.lower()
        mock_interaction.response.send_modal.assert_not_awaited()

    async def test_modal_rejects_after_signup_closes(self, mock_interaction):
        from bot.ui.signup_button import SignupModal

        await self._season("SWISS")
        modal = SignupModal(season="1")
        type(modal).name._value = "Someone"
        type(modal).country._value = "US"

        await modal.on_submit(mock_interaction)

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert "closed" in msg.lower()

    async def test_view_opens_modal_during_signup(self, mock_interaction):
        from bot.ui.signup_button import SignupView

        await self._season("SIGNUP")
        view = SignupView()
        await view.open_signup.callback(mock_interaction)

        mock_interaction.response.send_modal.assert_awaited_once()

    async def test_signup_message_points_at_timezone_and_timeslot(
        self, mock_interaction, mock_bot
    ):
        from bot.cogs.signup.signup_cog import SignupCog

        await self._season("SIGNUP")
        cog = SignupCog(mock_bot)

        with patch.object(cog.signup_service, "register", AsyncMock(
            return_value=(True, "ok")
        )):
            await cog.signup_cmd.callback(
                cog, mock_interaction, "Player", "US"
            )

        msg = mock_interaction.response.send_message.call_args[0][0]
        assert mock_interaction.response.send_message.call_args[1]["ephemeral"] is True
        assert msg == "ok"

    async def test_register_success_mentions_next_steps(self, db):
        from bot.services.signup import SignupService
        from bot.db.models import Season, SeasonStatus

        async with get_db_manager().get_session() as session:
            session.add(Season(number=1, status=SeasonStatus.ACTIVE))
            await session.commit()
        await Config().set_season_state("SIGNUP")

        ok, msg = await SignupService().register(555, "Player", "US")
        assert ok is True
        assert "/set_timezone" in msg
        assert "/add_timeslot" in msg

    async def test_edit_name_requires_signup_state(self, db):
        from bot.services.signup import SignupService
        from bot.db.models import Season, SeasonStatus
        from bot.db import Player

        async with get_db_manager().get_session() as session:
            session.add(Season(number=1, status=SeasonStatus.ACTIVE))
            session.add(Player(discord_id=556, name="Before", country="US"))
            await session.commit()
        await Config().set_season_state("OPEN")

        svc = SignupService()
        ok, msg = await svc.edit_name(556, "After")
        assert ok is False
        assert "signups are open" in msg.lower()

        ok, _ = await svc.edit_name(556, "After", force=True)
        assert ok is True

        async with get_db_manager().get_session() as session:
            refreshed = await PlayerRepository(session).get_by_discord_id(556)
            assert refreshed.name == "After"

    async def test_edit_country_accepts_null_old_country(self, db):
        from bot.services.signup import SignupService
        from bot.db import Player

        async with get_db_manager().get_session() as session:
            session.add(Player(discord_id=557, name="NoFlag", country=None))
            await session.commit()

        ok, msg = await SignupService().edit_country(557, "Germany")
        assert ok is True
        assert "—" in msg

        async with get_db_manager().get_session() as session:
            refreshed = await PlayerRepository(session).get_by_discord_id(557)
            assert refreshed.country == "de"

    async def test_edit_country_rejects_unknown_country(self, db):
        from bot.services.signup import SignupService
        from bot.db import Player

        async with get_db_manager().get_session() as session:
            session.add(Player(discord_id=558, name="NoFlag", country=None))
            await session.commit()

        ok, msg = await SignupService().edit_country(558, "Neverland")
        assert ok is False
        assert "Unknown country" in msg


class TestMatchConfirmView:
    @staticmethod
    def _view(target=42):
        from bot.ui.scheduling import MatchConfirmView

        return MatchConfirmView(target=target)

    @staticmethod
    def _interaction(discord_id=42):
        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.user.id = discord_id
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        return interaction

    async def test_confirm_records_answer_and_disables_buttons(self):
        view = self._view()
        interaction = self._interaction()

        await view.confirm.callback(interaction)

        assert view.value is True
        assert all(child.disabled for child in view.children)
        interaction.response.edit_message.assert_awaited_once()

    async def test_cancel_records_answer(self):
        view = self._view()
        interaction = self._interaction()

        await view.cancel.callback(interaction)

        assert view.value is False

    async def test_counter_opens_modal_without_answering(self):
        from bot.ui.scheduling import CounterOfferModal

        view = self._view()
        interaction = self._interaction()

        await view.counter.callback(interaction)

        assert view.value is None
        modal = interaction.response.send_modal.call_args.args[0]
        assert isinstance(modal, CounterOfferModal)

    async def test_stale_interaction_does_not_raise(self):
        """A 10062 from Discord must not lose the answer or spam a traceback."""
        import discord

        view = self._view()
        interaction = self._interaction()
        interaction.response.edit_message = AsyncMock(
            side_effect=discord.errors.NotFound(
                MagicMock(status=404), {"code": 10062, "message": "Unknown interaction"}
            )
        )

        await view.confirm.callback(interaction)

        assert view.value is True

    async def test_second_press_is_ignored(self):
        view = self._view()
        first = self._interaction()
        await view.confirm.callback(first)

        second = self._interaction()
        second.response.edit_message.reset_mock()
        await view.cancel.callback(second)

        assert view.value is True
        second.response.edit_message.assert_not_awaited()
        assert "already answered" in second.response.send_message.call_args[0][0]

    async def test_non_target_is_rejected(self):
        view = self._view(target=42)
        interaction = self._interaction(discord_id=7)

        await view.confirm.callback(interaction)

        assert view.value is None
        assert "permission" in interaction.response.send_message.call_args[0][0]

    async def test_timeout_disables_buttons(self):
        view = self._view()

        await view.on_timeout()

        assert all(child.disabled for child in view.children)

class TestStandingsCog:
    async def test_bracket_reply_is_public_and_cooldown_blocks_second_call(
        self, mock_interaction, mock_bot, tmp_path, monkeypatch
    ):
        target = tmp_path / "open.png"
        Image.new("RGBA", (8, 8), (0, 0, 0, 255)).save(target)
        render = AsyncMock(return_value=(0, str(target), "hash"))
        monkeypatch.setattr(
            "bot.cogs.standings.standings_cog.StandingsMaker.get_open",
            render,
        )

        cog = StandingsCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        mock_interaction.response.is_done = MagicMock(return_value=False)

        await cog._send_standings(mock_interaction, "OPEN")

        assert render.await_count == 1
        sent = mock_interaction.followup.send.await_args
        assert "file" in sent.kwargs
        assert "ephemeral" not in sent.kwargs

        mock_interaction.response.is_done = MagicMock(return_value=True)
        await cog._send_standings(mock_interaction, "OPEN")

        assert render.await_count == 1
        blocked = mock_interaction.followup.send.await_args
        assert "Try again" in blocked.args[0]
        assert blocked.kwargs["ephemeral"] is True


class TestConfirmActionView:
    @staticmethod
    def _interaction(discord_id=42):
        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.user.id = discord_id
        interaction.response = AsyncMock()
        return interaction

    async def test_confirm_records_true_and_disables_buttons(self):
        from bot.ui.confirm import ConfirmActionView

        view = ConfirmActionView(author_id=42)
        await view.confirm.callback(self._interaction())

        assert view.value is True
        assert all(child.disabled for child in view.children)

    async def test_cancel_records_false(self):
        from bot.ui.confirm import ConfirmActionView

        view = ConfirmActionView(author_id=42)
        await view.cancel.callback(self._interaction())

        assert view.value is False

    async def test_strangers_cannot_answer(self):
        from bot.ui.confirm import ConfirmActionView

        view = ConfirmActionView(author_id=42)
        interaction = self._interaction(99)

        await view.confirm.callback(interaction)

        assert view.value is None
        interaction.response.send_message.assert_awaited_once()
        assert "someone else" in interaction.response.send_message.call_args[0][0]

    async def test_timeout_leaves_value_unset(self):
        from bot.ui.confirm import ConfirmActionView

        view = ConfirmActionView(author_id=42)
        await view.on_timeout()

        assert view.value is None
        assert all(child.disabled for child in view.children)


class TestStageActionParity:
    """Prefix, slash and dashboard stage actions all call the same flow method."""

    @pytest.mark.parametrize(
        "prefix_impl, slash_name, button, flow_method",
        [
            ("_open_impl", "slash_generate_open", "gen_open", "start_open"),
            ("_playin_impl", "slash_start_playins", "start_playins", "start_playins"),
            ("_swiss_impl", "slash_swiss_round", "gen_swiss", "start_swiss"),
            ("_playoff_impl", "slash_generate_playoffs", "seed_playoff", "start_playoffs"),
        ],
    )
    async def test_surfaces_share_flow_method(
        self, mock_ctx, mock_bot, prefix_impl, slash_name, button, flow_method
    ):
        from bot.ui.dashboard import DashboardView

        result = {"ok": True, "state": "X", "advanced_from": None, "message": "m"}

        cog = TournamentCog(mock_bot)
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        setattr(cog.flow_service, flow_method, AsyncMock(return_value=result))
        await getattr(cog, prefix_impl)(mock_ctx)
        getattr(cog.flow_service, flow_method).assert_awaited_once()

        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        setattr(cog.flow_service, flow_method, AsyncMock(return_value=result))
        await getattr(cog, slash_name).callback(cog, interaction)
        getattr(cog.flow_service, flow_method).assert_awaited_once()

        mock_bot.get_cog = MagicMock(return_value=cog)
        view = DashboardView(mock_bot, MagicMock())
        view.season_service.get_current_season_number = AsyncMock(return_value=1)
        setattr(view.flow_service, flow_method, AsyncMock(return_value=result))
        await getattr(view, button).callback(interaction)
        getattr(view.flow_service, flow_method).assert_awaited_once()

    def test_generate_playin_has_no_ignored_round_arg(self, mock_bot):
        import inspect

        cog = TournamentCog(mock_bot)
        params = inspect.signature(cog.playin_cmd.callback).parameters
        assert "round_num" not in params
        assert "round" not in cog.playin_cmd.help.lower().split("(")[0]

    def test_dashboard_has_one_playins_action(self, mock_bot):
        from bot.ui.dashboard import DashboardView

        view = DashboardView(mock_bot, MagicMock())
        labels = [item.label for item in view.children]
        assert sum("Playins" in label and "Finish" not in label for label in labels) == 1


def test_bot_messages_reference_existing_commands():
    """Every `/cmd` or `!cmd` mentioned in bot copy must be a registered command."""
    import importlib
    import re
    from pathlib import Path

    from discord import app_commands
    from discord.ext import commands

    modules = [
        "bot.cogs.admin.season_cog", "bot.cogs.admin.admin_cog", "bot.cogs.admin.dev_cog",
        "bot.cogs.admin.player_cog", "bot.cogs.signup.signup_cog",
        "bot.cogs.matches.match_cog", "bot.cogs.tournament.tournament_cog",
        "bot.cogs.scheduling.schedule_cog", "bot.cogs.standings.standings_cog",
        "bot.cogs.standings.stats_cog", "bot.ui.dashboard",
    ]
    slash, prefix = set(), {"help"}
    for name in modules:
        for obj in vars(importlib.import_module(name)).values():
            if not (isinstance(obj, type) and issubclass(obj, commands.Cog)):
                continue
            for cmd in obj.__cog_app_commands__:
                slash.add(cmd.name)
                if isinstance(cmd, app_commands.Group):
                    slash.update(f"{cmd.name} {sub.name}" for sub in cmd.commands)
            prefix.update(c.qualified_name for c in obj.__cog_commands__)

    root = Path(__file__).resolve().parents[1] / "bot"
    missing = []
    for path in root.rglob("*.py"):
        for m in re.finditer(r"`([/!])([a-z_]+)( [a-z_]+)?", path.read_text()):
            kind, base, sub = m.group(1), m.group(2), (m.group(3) or "").strip()
            pool = slash if kind == "/" else prefix
            full = f"{base} {sub}".strip()
            if full not in pool and base not in pool:
                missing.append(f"{path.name}: {kind}{full}")
            elif sub and base in {"season", "tourney"} and full not in pool:
                missing.append(f"{path.name}: {kind}{full}")
    assert missing == []


class TestDashboardReadiness:
    def test_buttons_disabled_outside_their_stage(self, mock_bot):
        from bot.ui.dashboard import DashboardView

        view = DashboardView(mock_bot, MagicMock(), state="SWISS")
        assert view.gen_open.disabled
        assert view.start_playins.disabled
        assert not view.gen_swiss.disabled
        assert not view.seed_playoff.disabled
        assert view.finish_playins.disabled
        assert not view.next_step.disabled

    def test_readiness_text(self):
        from bot.ui.dashboard import readiness_text

        assert "Ready for **OPEN**" in readiness_text(
            {"action": "start_open", "ready": True, "next_state": "OPEN", "blockers": []}
        )
        assert "❌ 3 OPEN match(es) unplayed." in readiness_text(
            {"action": "start_playins", "ready": False, "next_state": "PLAYIN",
             "blockers": ["3 OPEN match(es) unplayed."]}
        )

    async def test_clean_threads_asks_first(self, mock_bot, mock_interaction):
        from bot.ui.dashboard import DashboardView

        view = DashboardView(mock_bot, MagicMock())
        with patch("bot.ui.dashboard.confirm", AsyncMock(return_value=False)) as ask, patch(
            "bot.ui.dashboard.cleanse_scheduling_threads", AsyncMock()
        ) as cleanse:
            await view.clean_threads.callback(mock_interaction)
        ask.assert_awaited_once()
        cleanse.assert_not_awaited()
        mock_interaction.followup.send.assert_awaited_with("Cancelled.", ephemeral=True)
