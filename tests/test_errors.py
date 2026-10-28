"""Global error replies: users always get an answer, never raw exception text."""

from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.errors import (
    PERMISSION_DENIAL_LIMITER,
    PERMISSION_MESSAGE,
    describe_app_command_error,
    error_message,
    new_error_id,
    report_interaction_error,
)


def test_error_message_contains_id():
    error_id = new_error_id()
    assert len(error_id) == 8
    assert f"`{error_id}`" in error_message(error_id)


def test_check_failures_keep_permission_message():
    assert describe_app_command_error(app_commands.CheckFailure()) == PERMISSION_MESSAGE


def _interaction(done: bool):
    interaction = MagicMock()
    interaction.response.is_done = MagicMock(return_value=done)
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


async def test_generic_error_replies_with_id_and_hides_details():
    interaction = _interaction(done=False)
    error_id = await report_interaction_error(
        interaction, RuntimeError("/secret/path.db locked"), "test"
    )
    content = interaction.response.send_message.call_args[0][0]
    assert error_id in content
    assert "secret" not in content


async def test_deferred_interaction_uses_followup():
    interaction = _interaction(done=True)
    await report_interaction_error(interaction, RuntimeError("boom"), "test")
    interaction.followup.send.assert_awaited_once()


async def test_bot_handlers_reply(monkeypatch):
    import main

    PERMISSION_DENIAL_LIMITER.reset()
    bot = main.TopGoalPostBot.__new__(main.TopGoalPostBot)

    ctx = MagicMock()
    ctx.author = MagicMock()
    ctx.author.id = 901
    ctx.author.send = AsyncMock()
    ctx.send = AsyncMock()
    await bot.on_command_error(ctx, commands.CommandInvokeError(RuntimeError("boom")))
    assert "Error ID" in ctx.send.call_args[0][0]

    await bot.on_command_error(ctx, commands.CheckFailure())
    ctx.author.send.assert_awaited_with(PERMISSION_MESSAGE)

    # Repeated permission failures are silently ignored inside the cooldown.
    ctx.author.send.reset_mock()
    await bot.on_command_error(ctx, commands.CheckFailure())
    ctx.author.send.assert_not_awaited()

    interaction = _interaction(done=True)
    interaction.command = None
    await bot.on_app_command_error(
        interaction, app_commands.CommandInvokeError(MagicMock(), RuntimeError("boom"))
    )
    assert "Error ID" in interaction.followup.send.call_args[0][0]


async def test_app_permission_denial_is_ephemeral_and_rate_limited():
    import main

    PERMISSION_DENIAL_LIMITER.reset()
    bot = main.TopGoalPostBot.__new__(main.TopGoalPostBot)

    first = _interaction(done=False)
    first.user = MagicMock()
    first.user.id = 902
    await bot.on_app_command_error(first, app_commands.CheckFailure())

    first.response.send_message.assert_awaited_once()
    assert first.response.send_message.call_args.kwargs["ephemeral"] is True
    assert first.response.send_message.call_args.args[0] == PERMISSION_MESSAGE

    second = _interaction(done=False)
    second.user = MagicMock()
    second.user.id = 902
    second.response.defer = AsyncMock()
    await bot.on_app_command_error(second, app_commands.CheckFailure())

    second.response.send_message.assert_not_awaited()
    second.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)


async def test_prefix_permission_falls_back_to_public_when_dm_is_closed():
    import main

    PERMISSION_DENIAL_LIMITER.reset()
    bot = main.TopGoalPostBot.__new__(main.TopGoalPostBot)

    ctx = MagicMock()
    ctx.author = MagicMock()
    ctx.author.id = 903
    ctx.author.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(status=403), "closed")
    )
    ctx.send = AsyncMock()

    await bot.on_command_error(ctx, commands.CheckFailure())

    ctx.send.assert_awaited_once_with(PERMISSION_MESSAGE, delete_after=10)
