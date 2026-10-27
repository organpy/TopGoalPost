"""Signup and registration cog."""

import discord
from discord.ext import commands
from discord import app_commands

from bot.rendering import ASSETS_ROOT
from bot.services.signup import SignupService
from bot.utils.countries import country_choices


class SignupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.signup_service = SignupService()

    async def country_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await country_choices(interaction, current)

    @app_commands.command(name="signup", description="Register for the current season")
    @app_commands.describe(
        username="Your preferred display name",
        country="Your country (for the flag on standings)"
    )
    @app_commands.autocomplete(country=country_autocomplete)
    async def signup_cmd(self, interaction: discord.Interaction, username: str, country: str):
        if len(country) != 2:
            await interaction.response.send_message("Invalid country selection.", ephemeral=True)
            return

        try:
            pfp_dir = ASSETS_ROOT / "pfps"
            pfp_dir.mkdir(parents=True, exist_ok=True)
            await interaction.user.display_avatar.save(
                fp=str(pfp_dir / f"{interaction.user.id}.png")
            )
        except Exception:
            pass

        success, msg = await self.signup_service.register(interaction.user.id, username, country)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="unregister", description="Unregister from the current season")
    async def unregister_cmd(self, interaction: discord.Interaction):
        success, msg = await self.signup_service.unregister(interaction.user.id)
        await interaction.response.send_message(msg, ephemeral=True)
        
    @app_commands.command(name="status", description="Check your registration status")
    async def status_cmd(self, interaction: discord.Interaction):
        is_reg = await self.signup_service.is_registered(interaction.user.id)
        if is_reg:
            await interaction.response.send_message("You are currently registered for the active season.", ephemeral=True)
        else:
            await interaction.response.send_message("You are not registered for the active season.", ephemeral=True)

    @app_commands.command(name="editname", description="Change your display name")
    @app_commands.describe(name="New display name (3-50 characters)")
    async def editname_cmd(self, interaction: discord.Interaction, name: str):
        success, msg = await self.signup_service.edit_name(interaction.user.id, name)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="editcountry", description="Change your country")
    @app_commands.describe(
        country="Your country (for the flag on standings)"
    )
    @app_commands.autocomplete(country=country_autocomplete)
    async def editcountry_cmd(
        self, interaction: discord.Interaction, country: str
    ):
        success, msg = await self.signup_service.edit_country(
            interaction.user.id, country
        )
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(SignupCog(bot))
