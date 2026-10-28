"""Main entry point for TopGoalPost bot."""

import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bot.db import init_db
from bot.config import Config
from bot.services.audit import current_actor
from bot.utils.errors import (
    PERMISSION_MESSAGE,
    acknowledge_interaction,
    consume_permission_denial,
    describe_app_command_error,
    describe_command_error,
    error_message,
    log_error,
    send_interaction,
)

# Setup basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("TopGoalPost")

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")


COGS = [
    "bot.cogs.admin.season_cog",
    "bot.cogs.admin.admin_cog",
    "bot.cogs.admin.player_cog",
    "bot.cogs.signup.signup_cog",
    "bot.cogs.matches.match_cog",
    "bot.cogs.tournament.tournament_cog",
    "bot.cogs.scheduling.schedule_cog",
    "bot.cogs.standings.standings_cog",
    "bot.cogs.standings.stats_cog",
    "bot.ui.dashboard",
]
DEV_COGS = ["bot.cogs.admin.dev_cog"]


def dev_mode(env=os.environ) -> bool:
    """Dummy-data commands load only with ENV=dev or LOAD_DEV_COGS=1."""
    return env.get("ENV", "").lower() == "dev" or env.get("LOAD_DEV_COGS", "") in ("1", "true")


def select_cogs(env=os.environ) -> list[str]:
    return COGS + (DEV_COGS if dev_mode(env) else [])


class TopGoalPostBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        super().__init__(
            command_prefix=commands.when_mentioned_or(COMMAND_PREFIX),
            intents=intents,
            help_command=commands.DefaultHelpCommand(),
        )

    async def setup_hook(self):
        """Called automatically when the bot starts up. Use for async initialization."""
        self.tree.on_error = self.on_app_command_error
        self.tree.interaction_check = self._track_app_actor
        self.before_invoke(self._track_prefix_actor)

        logger.info("Initializing database...")
        await init_db()
        
        logger.info("Ensuring configuration exists...")
        config = Config()
        await config.init_defaults()
        
        logger.info("Loading cogs...")
        cogs = select_cogs()
        if dev_mode():
            logger.warning("Dev mode: loading dummy-data commands. Use a separate database.")
        for cog in cogs:
            await self.load_extension(cog)

        from bot.ui.signup_button import SignupView
        from bot.ui.report import ReportActionButton
        self.add_view(SignupView())
        self.add_dynamic_items(ReportActionButton)

        logger.info("Syncing app commands...")
        guild_id = os.getenv("SERVER_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync(guild=None)
            await self.tree.sync(guild=guild)
            logger.info(f"Synced commands to guild {guild_id} and cleared global.")
        else:
            await self.tree.sync()
            logger.info("Synced commands globally")

    @staticmethod
    async def _track_app_actor(interaction: discord.Interaction) -> bool:
        current_actor.set(interaction.user.id)
        return True

    @staticmethod
    async def _track_prefix_actor(ctx: commands.Context) -> None:
        current_actor.set(ctx.author.id)

    async def on_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            if not consume_permission_denial(ctx.author.id):
                return
            try:
                await ctx.author.send(PERMISSION_MESSAGE)
            except discord.HTTPException:
                try:
                    await ctx.send(PERMISSION_MESSAGE, delete_after=10)
                except discord.HTTPException:
                    pass
            return
        message = describe_command_error(error)
        if message is None:
            error_id = log_error(f"Command '{ctx.command}'", getattr(error, "original", error))
            message = error_message(error_id)
        await ctx.send(message)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await send_interaction(
                interaction,
                f"Slow down — try again in {error.retry_after:.0f}s.",
            )
            return
        if isinstance(error, app_commands.CheckFailure):
            if not consume_permission_denial(interaction.user.id):
                await acknowledge_interaction(interaction)
                return
            await send_interaction(interaction, PERMISSION_MESSAGE)
            return
        message = describe_app_command_error(error)
        if message is None:
            name = interaction.command.qualified_name if interaction.command else "?"
            error_id = log_error(f"App command '{name}'", getattr(error, "original", error))
            message = error_message(error_id)
        await send_interaction(interaction, message)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user.name} (ID: {self.user.id})")
        from bot.utils.announce import refresh_presence
        from bot.utils.emojis import bind

        bind(self)
        await refresh_presence(self)
        logger.info("Bot is ready!")


async def main():
    if not TOKEN:
        logger.error("DISCORD_TOKEN not found in environment variables.")
        return

    bot = TopGoalPostBot()
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot manually stopped.")
