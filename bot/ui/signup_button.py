"""Persistent signup button and modal."""

import discord

from bot.config import Config
from bot.rendering import ASSETS_ROOT
from bot.services.season import SeasonService
from bot.services.signup import SignupService

SIGNUPS_CLOSED = "Signups are closed — the season has already started."


async def signups_open() -> bool:
    """True while the current season is still in the SIGNUP state."""
    season_service = SeasonService()
    if not await season_service.get_current_season_number():
        return False
    return await season_service.get_state() == "SIGNUP"


class SignupModal(discord.ui.Modal, title="Sign up"):
    def __init__(self, season: str):
        super().__init__(title=f"Season {season} Sign up", timeout=None)
        self.season = season
        self.signup_service = SignupService()

    name = discord.ui.TextInput(
        label="Your nickname",
        required=True,
        min_length=3,
        max_length=50,
    )
    country = discord.ui.TextInput(
        label="Your country (2-letter code, e.g. US)",
        required=False,
        max_length=2,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await signups_open():
            await interaction.response.send_message(SIGNUPS_CLOSED, ephemeral=True)
            return

        country = (self.country.value or "").strip().upper() or None

        try:
            pfp_dir = ASSETS_ROOT / "pfps"
            pfp_dir.mkdir(parents=True, exist_ok=True)
            await interaction.user.display_avatar.save(
                fp=str(pfp_dir / f"{interaction.user.id}.png")
            )
        except Exception:
            pass

        if isinstance(interaction.user, discord.Member):
            try:
                await interaction.user.edit(nick=self.name.value)
            except discord.Forbidden:
                pass

            role_id = await Config().get_role_id("signup_role")
            if role_id:
                role = interaction.guild.get_role(role_id) if interaction.guild else None
                if role:
                    try:
                        await interaction.user.add_roles(role, reason="Signed up via button")
                    except discord.Forbidden:
                        pass

        success, msg = await self.signup_service.register(
            interaction.user.id, self.name.value, country
        )
        await interaction.response.send_message(content=msg, ephemeral=True)


class SignupView(discord.ui.View):
    """Persistent view — register with bot.add_view() on startup."""

    def __init__(self):
        super().__init__(timeout=None)
        self.signup_service = SignupService()

    @discord.ui.button(
        label="Sign up",
        style=discord.ButtonStyle.blurple,
        custom_id="tgp-signup-button",
    )
    async def open_signup(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await signups_open():
            await interaction.response.send_message(SIGNUPS_CLOSED, ephemeral=True)
            return

        if await self.signup_service.is_registered(interaction.user.id):
            await interaction.response.send_message(
                "You have already signed up. Use `/editname` to change your submission.",
                ephemeral=True,
            )
            return

        season = await SeasonService().get_current_season_number()
        await interaction.response.send_modal(SignupModal(season=str(season or "?")))
