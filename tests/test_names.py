"""Player names must never be able to ping (@everyone, <@id>, ...)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import Player, PlayerRepository
from bot.utils.names import clean_player_name, unsafe_name_reason


@pytest.mark.parametrize(
    "name",
    ["@everyone", "@here", "hi @here", "<@123456>", "<@&42>", "<#99>", "a`b`c"],
)
def test_mention_capable_names_are_rejected(name):
    _, error = clean_player_name(name)
    assert error and "can't contain" in error


@pytest.mark.parametrize("name", ["xX_Pro_Xx", "Team#1", "José", "Mr. Goal"])
def test_ordinary_names_are_accepted(name):
    assert clean_player_name(name) == (name, None)


def test_names_are_normalised_before_length_check():
    assert clean_player_name("  Big​   Boss \n") == ("Big Boss", None)
    assert clean_player_name("​ab​")[1].startswith("Name must be")


async def _signup_season():
    from bot.config import Config
    from bot.services.season import SeasonService

    await Config().init_defaults()
    await SeasonService().create_season(1)
    await SeasonService().advance_state("SIGNUP")


async def test_register_rejects_everyone(db):
    from bot.services.signup import SignupService

    await _signup_season()
    ok, msg = await SignupService().register(1, "@everyone", "fr")
    assert not ok and "can't contain" in msg
    async with db.get_session() as session:
        assert await PlayerRepository(session).get_by_discord_id(1) is None


async def test_edit_name_rejects_even_when_forced(db):
    from bot.services.signup import SignupService

    await _signup_season()
    svc = SignupService()
    assert (await svc.register(1, "Goodname", "fr"))[0]

    ok, msg = await svc.edit_name(1, "<@&123>", force=True)
    assert not ok and "can't contain" in msg
    async with db.get_session() as session:
        assert (await PlayerRepository(session).get_by_discord_id(1)).name == "Goodname"


async def test_model_refuses_unsafe_names(db):
    with pytest.raises(ValueError):
        Player(discord_id=1, name="@here")
    async with db.get_session() as session:
        with pytest.raises(ValueError):
            await PlayerRepository(session).create(discord_id=2, name="<@1>")


async def test_signup_modal_rejects_before_touching_nickname(db):
    import discord

    from bot.ui import signup_button as module

    modal = module.SignupModal("1")
    modal.signup_service = MagicMock(register=AsyncMock())
    interaction = MagicMock()
    interaction.response = AsyncMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.edit = AsyncMock()

    modal.name = SimpleNamespace(value="@everyone")
    modal.country = SimpleNamespace(value="")

    with patch.object(module, "signups_open", AsyncMock(return_value=True)):
        await modal.on_submit(interaction)

    interaction.user.edit.assert_not_awaited()
    modal.signup_service.register.assert_not_awaited()
    assert "can't contain" in interaction.response.send_message.call_args[0][0]


def test_substitute_falls_back_to_a_safe_discord_name():
    from bot.cogs.admin.player_cog import _safe_user_name

    member = SimpleNamespace(display_name="@everyone", name="realuser")
    assert _safe_user_name(member, "1") == "realuser"
    member = SimpleNamespace(display_name="<@1>", name="@here")
    assert _safe_user_name(member, "1") == "1"


async def test_substitute_placeholder_rejects_unsafe_name(db):
    from bot.cogs.admin.player_cog import PlayerCog

    cog = PlayerCog(MagicMock())
    cog.stats_service.resolve_player = AsyncMock(return_value=None)
    interaction = MagicMock()
    interaction.guild.get_member_named.return_value = None
    interaction.guild.members = []

    player, name, discord_id, error = await cog._prepare_new_player(
        interaction, "@everyone", 1
    )
    assert player is None and name is None and "can't contain" in error


def test_unsafe_reason_names_the_characters():
    assert unsafe_name_reason("a@b<c") == "Names can't contain `<` `@`."
