"""User-facing error replies with a short ID that admins can find in the logs."""

import logging
import uuid

import discord
from discord.ext import commands

from bot.services.audit import current_actor

logger = logging.getLogger("TopGoalPost.errors")

PERMISSION_MESSAGE = "⛔ You need an administrator or host role to use this command."


def new_error_id() -> str:
    return uuid.uuid4().hex[:8]


def error_message(error_id: str) -> str:
    return (
        f"Something went wrong. Error ID: `{error_id}`. "
        "An admin can search the logs for it."
    )


def log_error(where: str, error: BaseException) -> str:
    """Log the full stack under a fresh error ID and return the ID."""
    error_id = new_error_id()
    logger.error("[%s] %s failed", error_id, where, exc_info=error)
    return error_id


async def send_interaction(interaction: discord.Interaction, content: str) -> None:
    """Reply ephemerally whether or not the interaction was already answered."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Could not send error reply")


async def report_interaction_error(
    interaction: discord.Interaction, error: BaseException, where: str
) -> str:
    error_id = log_error(where, error)
    await send_interaction(interaction, error_message(error_id))
    return error_id


def describe_command_error(error: commands.CommandError) -> str | None:
    """A safe message for expected prefix-command errors, or None if unexpected."""
    if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
        return PERMISSION_MESSAGE
    if isinstance(error, commands.MissingRequiredArgument):
        return f"Missing argument `{error.param.name}`. See `!help` for usage."
    if isinstance(error, commands.BadArgument):
        return "Invalid argument. See `!help` for usage."
    if isinstance(error, commands.CommandOnCooldown):
        return f"Slow down — try again in {error.retry_after:.0f}s."
    return None


def describe_app_command_error(error: discord.app_commands.AppCommandError) -> str | None:
    """A safe message for expected slash-command errors, or None if unexpected."""
    if isinstance(error, discord.app_commands.CommandOnCooldown):
        return f"Slow down — try again in {error.retry_after:.0f}s."
    if isinstance(error, discord.app_commands.CheckFailure):
        return PERMISSION_MESSAGE
    return None


class ReportingView(discord.ui.View):
    """View base class that answers failed button/select callbacks.

    Also records the clicking user as the audit actor for the callback.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        current_actor.set(interaction.user.id)
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await report_interaction_error(interaction, error, f"{type(self).__name__} item {item}")
