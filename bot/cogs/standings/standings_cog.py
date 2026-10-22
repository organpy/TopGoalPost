"""Standings rendering cogs. Separate commands per phase."""

import io
import os
import discord
from discord.ext import commands
from discord import app_commands
from PIL import Image

from bot.services.season import SeasonService
from bot.rendering import StandingsMaker
from bot.rendering.playoff import get_playoff


class StandingsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.season_service = SeasonService()

    async def _send_standings(
        self, interaction: discord.Interaction, phase: str, view: str = "full"
    ):
        await interaction.response.defer(ephemeral=True)

        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send("No active season.", ephemeral=True)
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

            result = await func(view=view) if phase == "PLAYOFF" else await func()
            link, filename, *extra = result

            if link:
                await interaction.followup.send(link, ephemeral=True)
                return

            if not os.path.exists(filename):
                await interaction.followup.send("Standings image not found. Generate some matches first.", ephemeral=True)
                return

            img = Image.open(filename)
            with io.BytesIO() as image_binary:
                img.save(image_binary, 'PNG')
                image_binary.seek(0)
                file = discord.File(fp=image_binary, filename=f'{phase.lower()}.png')
                await interaction.followup.send(file=file, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"Failed: {e}", ephemeral=True)

    async def show_open(self, interaction: discord.Interaction):
        await self._send_standings(interaction, "OPEN")

    async def show_playins(self, interaction: discord.Interaction):
        await self._send_standings(interaction, "PLAYIN")

    async def show_swiss(self, interaction: discord.Interaction):
        await self._send_standings(interaction, "SWISS")

    async def show_playoff(self, interaction: discord.Interaction, view: str = "full"):
        await self._send_standings(interaction, "PLAYOFF", view=view)

    @app_commands.command(name="open", description="Show Open stage standings")
    async def open_cmd(self, interaction: discord.Interaction):
        await self.show_open(interaction)

    @app_commands.command(name="playins", description="Show Playin ladder standings")
    async def playins_cmd(self, interaction: discord.Interaction):
        await self.show_playins(interaction)

    @app_commands.command(name="swiss", description="Show Swiss stage standings")
    async def swiss_cmd(self, interaction: discord.Interaction):
        await self.show_swiss(interaction)

    @app_commands.command(name="playoff", description="Show Playoff bracket")
    @app_commands.describe(view="Full bracket, winners side, or losers side")
    @app_commands.choices(
        view=[
            app_commands.Choice(name="Full bracket", value="full"),
            app_commands.Choice(name="Winners bracket", value="winners"),
            app_commands.Choice(name="Losers bracket", value="losers"),
        ]
    )
    async def playoff_cmd(
        self, interaction: discord.Interaction, view: app_commands.Choice[str] = None
    ):
        await self.show_playoff(interaction, view.value if view else "full")


async def setup(bot):
    await bot.add_cog(StandingsCog(bot))
