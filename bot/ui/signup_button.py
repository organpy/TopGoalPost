"""Persistent signup button and modal."""

import discord

from bot.utils.embeds import BLUE, card, season_track, subtext
from bot.utils.emojis import get as emoji
from bot.utils.errors import ReportingView
from bot.utils.names import clean_player_name
from bot.utils.roles import set_member_role

from bot.rendering import ASSETS_ROOT
from bot.services.season import SeasonService
from bot.services.signup import SignupService

SIGNUPS_CLOSED = "Signups are closed — the season has already started."


def signup_panel_embed(season: int | None = None) -> discord.Embed:
    """Public panel posted by ``!signupbutton``."""
    title = (
        f"{emoji('SIGNUP')} Signups are open"
        if season is None
        else f"{emoji('SIGNUP')} Season {season} signups are open"
    )
    embed = card(
        title,
        (
            "Fancy a shot at the title? Hit **Sign up** below, pick your "
            "nickname, and you're in.\n\n"
            + subtext(season_track("SIGNUP"))
        ),
        color=BLUE,
        section="Player signup",
        timestamp=False,
    )
    embed.add_field(name="1️⃣ Sign up", value="Tap the button below", inline=True)
    embed.add_field(name="2️⃣ Timezone", value="`/set_timezone`", inline=True)
    embed.add_field(name="3️⃣ Availability", value="`/add_timeslot`", inline=True)
    embed.add_field(
        name="👋 Already registered?",
        value="You're all set! Change your details any time with `/editname` or `/editcountry`.",
        inline=False,
    )
    return embed


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

        name, error = clean_player_name(self.name.value)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
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

        success, msg = await self.signup_service.register(
            interaction.user.id, name, country
        )
        await interaction.response.send_message(content=msg, ephemeral=True)

        if success and isinstance(interaction.user, discord.Member):
            try:
                await interaction.user.edit(nick=name)
            except discord.HTTPException:
                pass
            await set_member_role(
                interaction.user, "signup_role", True, "Signed up via button"
            )


class SignupView(ReportingView):
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
