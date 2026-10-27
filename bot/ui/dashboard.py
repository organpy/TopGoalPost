"""Admin dashboard — clean, modern, includes all commands as buttons."""

import discord
from discord.ext import commands

from bot.cogs.checks import require_host_app
from discord import app_commands
from sqlalchemy import select

from bot.db import get_db_manager, PlayerSeason
from bot.services import audit
from bot.services.season import SeasonService
from bot.services.pairing import PairingService
from bot.services.playin import PlayinService
from bot.services.tournament_flow import TournamentFlowService, STAGE_EVENTS
from bot.utils.threads import cleanse_scheduling_threads, create_scheduling_threads
from bot.config import Config
from bot.ui.confirm import confirm
from bot.utils.errors import ReportingView


class SingleConfigModal(discord.ui.Modal):
    def __init__(self, key: str, label: str, placeholder: str, is_channel: bool = False, is_role: bool = False):
        super().__init__(title=f"Set {label}")
        self.key = key
        self.label = label
        self.is_channel = is_channel
        self.is_role = is_role
        self.value_input = discord.ui.TextInput(
            label=f"ID for {label}",
            placeholder=placeholder,
            required=True,
            max_length=200,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = Config()
        val = self.value_input.value.strip()

        if self.is_channel:
            try:
                cid = int(val)
                ch = interaction.guild.get_channel(cid) if interaction.guild else None
                if not ch:
                    await interaction.response.send_message(f"Channel {cid} not found.", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("Must be a number.", ephemeral=True)
                return
        elif self.is_role:
            try:
                rid = int(val)
                r = interaction.guild.get_role(rid) if interaction.guild else None
                if not r:
                    await interaction.response.send_message(f"Role {rid} not found.", ephemeral=True)
                    return
            except ValueError:
                await interaction.response.send_message("Must be a number.", ephemeral=True)
                return

        await cfg.set(self.key, val)
        await audit.record("config.set", "config", self.key, actor_id=interaction.user.id, value=val)
        await interaction.response.send_message(f"Set **{self.label}** → `{val}`", ephemeral=True)


class SkipOpenModal(discord.ui.Modal, title="Players skipping Open"):
    def __init__(self, season_service: SeasonService):
        super().__init__()
        self.season_service = season_service
        self.players = discord.ui.TextInput(
            label="Names, IDs, or mentions (comma-separated)",
            placeholder="e.g. Alice, Bob, 123456789012345678",
            required=False,
            max_length=1000,
        )
        self.add_item(self.players)

    async def on_submit(self, interaction: discord.Interaction):
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.response.send_message("No active season.", ephemeral=True)
            return
        if await self.season_service.get_state() != "SIGNUP":
            await interaction.response.send_message(
                "Skip players must be set during SIGNUP.", ephemeral=True
            )
            return
        status = await TournamentFlowService().get_status()
        if status.get("stages", {}).get("OPEN", {}).get("total", 0):
            await interaction.response.send_message(
                "Open matches already exist; the skip list can no longer be changed.",
                ephemeral=True,
            )
            return

        from bot.services.signup import SignupService

        tokens = [
            part.strip()
            for part in (self.players.value or "").split(",")
            if part.strip()
        ]
        success, errors = await SignupService().set_skip_open(season, tokens)
        lines = ["Open-skip list updated."]
        lines.extend(success or ["No skips set (Open-skip list cleared)."])
        lines.extend(errors)
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# Buttons that only make sense in some stages. Anything not listed is always on.
BUTTON_STATES = {
    "gen_open": ("SIGNUP", "OPEN"),
    "start_playins": ("OPEN", "PLAYIN"),
    "gen_swiss": ("PLAYIN", "SWISS"),
    "seed_playoff": ("SWISS", "PLAYOFF"),
    "sync_open": ("OPEN", "PLAYIN"),
    "skip_open": ("SIGNUP",),
    "finish_playins": ("PLAYIN",),
    "make_threads": ("OPEN", "PLAYIN", "SWISS", "PLAYOFF"),
    "clean_threads": ("OPEN", "PLAYIN", "SWISS", "PLAYOFF"),
}


def readiness_text(readiness: dict) -> str:
    """Checklist for the dashboard: what the next stage action still needs."""
    if not readiness.get("action"):
        return "No stage action for this state."
    if readiness["ready"]:
        return f"✅ Ready for **{readiness['next_state']}**."
    return "\n".join(f"❌ {blocker}" for blocker in readiness["blockers"])


class DashboardView(ReportingView):
    def __init__(self, bot: commands.Bot, embed: discord.Embed, state: str | None = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.original_embed = embed
        if state:
            self.apply_state(state)
        self.season_service = SeasonService()
        self.pairing_service = PairingService()
        self.playin_service = PlayinService()
        self.flow_service = TournamentFlowService()

    def apply_state(self, state: str) -> None:
        """Disable actions that the current stage would refuse anyway."""
        for name, allowed in BUTTON_STATES.items():
            getattr(self, name).disabled = state not in allowed

    async def _run_stage(self, interaction: discord.Interaction, action) -> None:
        await interaction.response.defer(ephemeral=True)
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        cog = self.bot.get_cog("TournamentCog")
        if cog:
            result = await cog._run_stage(interaction.guild, action, season_num)
        else:
            result = await action(season_num)
        await interaction.followup.send(result.get("error") or result["message"], ephemeral=True)

    # Row 0: Stage generation
    @discord.ui.button(label="Open", style=discord.ButtonStyle.primary, emoji="🎲", row=0)
    async def gen_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_stage(interaction, self.flow_service.start_open)

    @discord.ui.button(label="Start Playins", style=discord.ButtonStyle.primary, emoji="⚔️", row=0)
    async def start_playins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_stage(interaction, self.flow_service.start_playins)

    @discord.ui.button(label="Start Swiss", style=discord.ButtonStyle.primary, emoji="🇨🇭", row=0)
    async def gen_swiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_stage(interaction, self.flow_service.start_swiss)

    @discord.ui.button(label="Playoffs", style=discord.ButtonStyle.primary, emoji="🏆", row=0)
    async def seed_playoff(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_stage(interaction, self.flow_service.start_playoffs)

    # Row 1: Guided progression (prefer these over raw state jumps)
    @discord.ui.button(label="Next Step", style=discord.ButtonStyle.primary, emoji="➡️", row=1)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        info = await self.flow_service.get_status()
        await interaction.followup.send(info.get("next_action", "No guidance."), ephemeral=True)

    @discord.ui.button(label="Sync Open", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)
    async def sync_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        result = await self.flow_service.sync_open_stats(season)
        if result.get("error"):
            await interaction.followup.send(result["error"], ephemeral=True)
            return
        await interaction.followup.send(
            f"Open stats synced for {result['players_synced']} players. Wins: `{result.get('win_distribution')}`",
            ephemeral=True,
        )

    @discord.ui.button(label="Skip Open", style=discord.ButtonStyle.secondary, emoji="⏭️", row=1)
    async def skip_open(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SkipOpenModal(self.season_service))

    @discord.ui.button(label="Finish Playins", style=discord.ButtonStyle.secondary, emoji="✅", row=1)
    async def finish_playins(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._run_stage(interaction, self.flow_service.finish_playins)

    async def _thread_context(self, interaction: discord.Interaction):
        """(channel, season, state) for thread buttons, or None after replying."""
        channel_id = await Config().get_channel_id("match_channel")
        channel = interaction.guild.get_channel(channel_id) if channel_id and interaction.guild else None
        if not channel:
            await interaction.followup.send("Set match_channel first.", ephemeral=True)
            return None
        season_num = await self.season_service.get_current_season_number()
        state = await self.season_service.get_state()
        if not season_num or state not in STAGE_EVENTS:
            await interaction.followup.send(f"No scheduling threads for state {state}.", ephemeral=True)
            return None
        return channel, season_num, state

    @discord.ui.button(label="Threads", style=discord.ButtonStyle.secondary, emoji="🧵", row=2)
    async def make_threads(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ctx = await self._thread_context(interaction)
        if not ctx:
            return
        channel, season_num, state = ctx
        discord_ids = await self.flow_service.unplayed_discord_ids(season_num, state)
        if not discord_ids:
            await interaction.followup.send(f"No players with unplayed {state} matches.", ephemeral=True)
            return
        created = await create_scheduling_threads(channel, state, discord_ids)
        await interaction.followup.send(f"Created {created} scheduling threads for {state}.", ephemeral=True)

    @discord.ui.button(label="Clean Threads", style=discord.ButtonStyle.danger, emoji="🧹", row=2)
    async def clean_threads(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not await confirm(
            interaction, "Remove every registered player without an unplayed match from all threads?"
        ):
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return
        ctx = await self._thread_context(interaction)
        if not ctx:
            return
        channel, season_num, state = ctx
        active = await self.flow_service.unplayed_discord_ids(season_num, state)
        if active is None:
            await interaction.followup.send(f"No {state} event.", ephemeral=True)
            return
        cleaned = await cleanse_scheduling_threads(
            channel, set(active), await self.flow_service.registered_discord_ids(), self.bot.user.id
        )
        await interaction.followup.send(f"Cleaned {cleaned} users.", ephemeral=True)

    # Row 3: Config
    @discord.ui.button(label="Set Match Ch", style=discord.ButtonStyle.secondary, emoji="💬", row=3)
    async def set_match_ch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SingleConfigModal("match_channel", "Match Channel", "Channel ID", is_channel=True))

    @discord.ui.button(label="Set Score Ch", style=discord.ButtonStyle.secondary, emoji="🏆", row=3)
    async def set_score_ch(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SingleConfigModal("score_channel", "Score Channel", "Channel ID", is_channel=True))

    @discord.ui.button(label="Set Signup Role", style=discord.ButtonStyle.secondary, emoji="📝", row=3)
    async def set_signup_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SingleConfigModal("signup_role", "Signup Role", "Role ID", is_role=True))

    @discord.ui.button(label="Set Mod Role", style=discord.ButtonStyle.secondary, emoji="🛡️", row=3)
    async def set_mod_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SingleConfigModal("moderator_role", "Moderator Role", "Role ID", is_role=True))

    # Row 4: Standings
    @discord.ui.button(label="Open Board", style=discord.ButtonStyle.success, emoji="📊", row=4)
    async def open_board_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("StandingsCog")
        if cog:
            await cog.show_open(interaction)
        else:
            await interaction.response.send_message("Standings cog not loaded.", ephemeral=True)

    @discord.ui.button(label="Playin Board", style=discord.ButtonStyle.success, emoji="📊", row=4)
    async def playin_board_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("StandingsCog")
        if cog:
            await cog.show_playins(interaction)
        else:
            await interaction.response.send_message("Standings cog not loaded.", ephemeral=True)

    @discord.ui.button(label="Swiss Board", style=discord.ButtonStyle.success, emoji="📊", row=4)
    async def swiss_board_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("StandingsCog")
        if cog:
            await cog.show_swiss(interaction)
        else:
            await interaction.response.send_message("Standings cog not loaded.", ephemeral=True)

    @discord.ui.button(label="Playoff Board", style=discord.ButtonStyle.success, emoji="📊", row=4)
    async def playoff_board_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("StandingsCog")
        if cog:
            await cog.show_playoff(interaction)
        else:
            await interaction.response.send_message("Standings cog not loaded.", ephemeral=True)


class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = get_db_manager()
        self.season_service = SeasonService()

    @app_commands.command(name="dashboard", description="Open the admin dashboard")
    @require_host_app()
    async def dashboard_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        season_num = await self.season_service.get_current_season_number()
        state = await self.season_service.get_state()

        embed = discord.Embed(
            title=f"Dashboard — Season {season_num or '?'}",
            description=f"**State:** `{state}`",
            color=0x2b2d31
        )
        embed.set_footer(text="TopGoalPost")

        flow = TournamentFlowService()
        flow_info = await flow.get_status()
        if flow_info.get("readiness"):
            embed.add_field(
                name="Readiness", value=readiness_text(flow_info["readiness"]), inline=False
            )
        if flow_info.get("next_action"):
            embed.add_field(name="Next step", value=flow_info["next_action"], inline=False)

        if season_num:
            async with self.db.get_session() as session:
                cfg = Config()
                mc = await cfg.get_channel_id("match_channel")
                mcs = f"<#{mc}>" if mc else "❌"
                sc = await cfg.get_channel_id("score_channel")
                scs = f"<#{sc}>" if sc else "❌"

                stmt = select(PlayerSeason).where(PlayerSeason.season_number == season_num)
                rs = (await session.execute(stmt)).scalars().all()

                total = len(rs)
                seeds = sum(1 for ps in rs if ps.swiss_seed and ps.swiss_seed > 0 and ps.open_wins == 0)
                open_p = total - seeds

                embed.add_field(name="Players", value=f"**{total}** total\n{seeds} seeded\n{open_p} open", inline=True)
                embed.add_field(name="Channels", value=f"Match: {mcs}\nScore: {scs}", inline=True)
                embed.add_field(
                    name="Buttons",
                    value=(
                        "**Stage:** Open · Start Playins · Start Swiss · Playoffs\n"
                        "**Repair:** Next Step · Sync Open · Skip Open · Finish Playins\n"
                        "**Threads:** Threads · Clean Threads (asks first)\n"
                        "**Config** and **Boards** below. Buttons for other stages are disabled."
                    ),
                    inline=False,
                )

        view = DashboardView(self.bot, embed, state=state)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(DashboardCog(bot))
