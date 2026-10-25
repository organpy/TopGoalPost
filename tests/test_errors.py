"""Global error replies: users always get an answer, never raw exception text."""

from unittest.mock import AsyncMock, MagicMock

from discord import app_commands
from discord.ext import commands

from bot.utils.errors import (
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

    bot = main.TopGoalPostBot.__new__(main.TopGoalPostBot)

    ctx = MagicMock()
    ctx.send = AsyncMock()
    await bot.on_command_error(ctx, commands.CommandInvokeError(RuntimeError("boom")))
    assert "Error ID" in ctx.send.call_args[0][0]

    await bot.on_command_error(ctx, commands.CheckFailure())
    ctx.send.assert_awaited_with(PERMISSION_MESSAGE)

    interaction = _interaction(done=True)
    interaction.command = None
    await bot.on_app_command_error(
        interaction, app_commands.CommandInvokeError(MagicMock(), RuntimeError("boom"))
    )
    assert "Error ID" in interaction.followup.send.call_args[0][0]
