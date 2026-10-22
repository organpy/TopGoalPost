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
        logger.info("Initializing database...")
        await init_db()
        
        logger.info("Ensuring configuration exists...")
        config = Config()
        await config.init_defaults()
        
        logger.info("Loading cogs...")
        cogs = [
            "bot.cogs.admin.season_cog",
            "bot.cogs.admin.admin_cog",
            "bot.cogs.admin.dev_cog",
            "bot.cogs.admin.player_cog",
            "bot.cogs.signup.signup_cog",
            "bot.cogs.matches.match_cog",
            "bot.cogs.tournament.tournament_cog",
            "bot.cogs.scheduling.schedule_cog",
            "bot.cogs.standings.standings_cog",
            "bot.cogs.standings.stats_cog",
            "bot.ui.dashboard",
        ]
        for cog in cogs:
            await self.load_extension(cog)

        from bot.ui.signup_button import SignupView
        self.add_view(SignupView())

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

    async def on_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            await ctx.send("⛔ You need an administrator or host role to use this command.")
            return
        logger.error(f"Command '{ctx.command}' failed: {error}", exc_info=error)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            msg = "⛔ You need an administrator or host role to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        logger.error(f"App command '{interaction.command.qualified_name if interaction.command else '?'}' failed: {error}", exc_info=error)

    async def on_ready(self):
        logger.info(f"Logged in as {self.user.name} (ID: {self.user.id})")
        try:
            from bot.config import Config
            state = await Config().get_season_state()
            activity_name = "/signup" if state in ("OFFSEASON", "SIGNUP") else "/report"
            await self.change_presence(activity=discord.Game(name=activity_name))
        except Exception as e:
            logger.warning(f"Could not set presence: {e}")
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
