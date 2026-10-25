"""Standings rendering cogs. Separate commands per phase."""

import io
import os
import time
import discord
from discord.ext import commands
from discord import app_commands
from PIL import Image

from bot.rendering import StandingsMaker
from bot.rendering.playoff import get_playoff
from bot.services.player_stats import PlayerStatsService
from bot.services.season import SeasonService
from bot.ui.autocomplete import season_autocomplete
from bot.utils.errors import report_interaction_error


class StandingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.season_service = SeasonService()
        self.stats_service = PlayerStatsService()
        self._bracket_cooldowns: dict[tuple[int, str], float] = {}

    async def _send_standings(
        self,
        interaction: discord.Interaction,
        phase: str,
        view: str = "full",
        season: int = None,
    ):
        now = time.monotonic()
        key = (interaction.user.id, phase)
        last = self._bracket_cooldowns.get(key)
        if last is not None and now - last < 5:
            wait = 5 - (now - last)
            msg = f"⏳ Try again in {wait:.1f}s."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        self._bracket_cooldowns[key] = now

        await interaction.response.defer()

        season_num = season or await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send("No active season.", ephemeral=True)
            return

        if season is not None:
            if season not in await self.stats_service.available_seasons():
                await interaction.followup.send(
                    f"Season {season} has no tournament data.", ephemeral=True
                )
                return

        try:
            makers = {
                "OPEN": StandingsMaker.get_open,
                "PLAYIN": StandingsMaker.get_playin,
                "SWISS": StandingsMaker.get_swiss,
                "PLAYOFF": get_playoff,
            }
            func = makers.get(phase)
            if not func:
                await interaction.followup.send("No standings for this phase.", ephemeral=True)
                return

            if phase == "PLAYOFF":
                result = await func(view=view, season=season_num)
            else:
                result = await func(season=season_num)
            cached, filename, *extra = result

            if cached and cached.startswith(("http://", "https://")):
                await interaction.followup.send(cached)
                return

            image_path = cached or filename
            if not os.path.exists(image_path):
                await interaction.followup.send(
                    "Standings image not found. Generate some matches first.",
                    ephemeral=True,
                )
                return

            img = Image.open(image_path)
            with io.BytesIO() as image_binary:
                img.save(image_binary, 'PNG')
                image_binary.seek(0)
                file = discord.File(
                    fp=image_binary,
                    filename=f"{phase.lower()}_s{season_num}.png",
                )
                await interaction.followup.send(file=file)

        except Exception as e:
            await report_interaction_error(interaction, e, f"{phase} standings")

    async def show_open(self, interaction: discord.Interaction, season: int = None):
        await self._send_standings(interaction, "OPEN", season=season)

    async def show_playins(self, interaction: discord.Interaction, season: int = None):
        await self._send_standings(interaction, "PLAYIN", season=season)

    async def show_swiss(self, interaction: discord.Interaction, season: int = None):
        await self._send_standings(interaction, "SWISS", season=season)

    async def show_playoff(
        self, interaction: discord.Interaction, view: str = "full", season: int = None
    ):
        await self._send_standings(interaction, "PLAYOFF", view=view, season=season)

    @app_commands.command(name="open", description="Show Open stage standings")
    @app_commands.describe(season="Season number (defaults to the current one)")
    @app_commands.autocomplete(season=season_autocomplete)
    async def open_cmd(self, interaction: discord.Interaction, season: int = None):
        await self.show_open(interaction, season=season)

    @app_commands.command(name="playins", description="Show Playin ladder standings")
    @app_commands.describe(season="Season number (defaults to the current one)")
    @app_commands.autocomplete(season=season_autocomplete)
    async def playins_cmd(self, interaction: discord.Interaction, season: int = None):
        await self.show_playins(interaction, season=season)

    @app_commands.command(name="swiss", description="Show Swiss stage standings")
    @app_commands.describe(season="Season number (defaults to the current one)")
    @app_commands.autocomplete(season=season_autocomplete)
    async def swiss_cmd(self, interaction: discord.Interaction, season: int = None):
        await self.show_swiss(interaction, season=season)

    @app_commands.command(name="playoff", description="Show Playoff bracket")
    @app_commands.describe(
        view="Full bracket, winners side, or losers side",
        season="Season number (defaults to the current one)",
    )
    @app_commands.choices(
        view=[
            app_commands.Choice(name="Full bracket", value="full"),
            app_commands.Choice(name="Winners bracket", value="winners"),
            app_commands.Choice(name="Losers bracket", value="losers"),
        ]
    )
    @app_commands.autocomplete(season=season_autocomplete)
    async def playoff_cmd(
        self,
        interaction: discord.Interaction,
        view: app_commands.Choice[str] = None,
        season: int = None,
    ):
        await self.show_playoff(
            interaction, view.value if view else "full", season=season
        )


async def setup(bot):
    await bot.add_cog(StandingsCog(bot))
