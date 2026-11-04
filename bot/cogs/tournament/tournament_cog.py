"""Tournament management cog."""

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands
from discord import app_commands

from bot.cogs.checks import require_host, require_host_app
from bot.services import audit
from bot.services.season import SeasonService
from bot.services.pairing import PairingService
from bot.services.playin import PlayinService
from bot.services.match import MatchService
from bot.services.signup import SignupService
from bot.services.tournament_flow import TournamentFlowService, STAGE_EVENTS
from bot.db import get_db_manager, MatchRepository, EventType, EventRepository
from bot.ui.autocomplete import player_list_autocomplete
from bot.ui.confirm import confirm
from bot.utils.announce import announce_stage
from bot.utils.roles import STATE_ROLE_KEYS, get_configured_role, stage_roles, sync_role
from bot.utils.threads import cleanse_scheduling_threads, create_scheduling_threads

logger = logging.getLogger(__name__)

STAGE_EVENT_TYPES = STAGE_EVENTS


class TournamentCog(commands.Cog):
    tourney = app_commands.Group(name="tourney", description="Tournament progression (admin)")

    def __init__(self, bot):
        self.bot = bot
        self.db = get_db_manager()
        self.season_service = SeasonService()
        self.pairing_service = PairingService()
        self.playin_service = PlayinService()
        self.match_service = MatchService()
        self.signup_service = SignupService()
        self.flow_service = TournamentFlowService()
        # Keep background role syncs referenced until they finish.
        self._role_tasks: set[asyncio.Task] = set()

    @commands.group(name="tourney", invoke_without_command=True)
    @require_host()
    async def tourney_group(self, ctx: commands.Context):
        """Tournament progression commands."""
        await ctx.send_help(ctx.command)

    async def _get_parent_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Get the configured match thread parent channel."""
        from bot.config import Config
        cfg = Config()
        cid = await cfg.get_channel_id("match_channel")
        if cid:
            channel = guild.get_channel(cid)
            if channel and isinstance(channel, discord.TextChannel):
                return channel
        return None

    @tourney_group.command(name="makethreads", help="Create scheduling group threads for a stage")
    @require_host()
    async def makethreads_cmd(self, ctx: commands.Context, stage: Optional[str] = None):
        if not ctx.guild:
            await ctx.send("Guild only.")
            return

        channel = await self._get_parent_channel(ctx.guild)
        if not channel:
            await ctx.send(
                "No match thread channel configured. An admin must set `match_channel` via `!set_config match_channel <channel_id>`"
            )
            return

        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return

        state = stage.upper() if stage else await self.season_service.get_state()
        etype = STAGE_EVENT_TYPES.get(state)
        if not etype:
            await ctx.send(f"No threads to create for state {state}.")
            return

        message = await ctx.send("Creating scheduling threads...")
        matchups = await self.flow_service.unplayed_matchups(season_num, state)
        if matchups is None:
            await message.edit(content=f"No {state} event found.")
            return
        if not matchups:
            await message.edit(content="No players with unplayed matches.")
            return

        created = await create_scheduling_threads(channel, state, matchups)
        await message.edit(content=f"Created {created} scheduling threads for {state}.")

    @tourney_group.command(name="cleansethreads", help="Remove non-participants from scheduling threads")
    @require_host()
    async def cleansethreads_cmd(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("Guild only.")
            return

        channel = await self._get_parent_channel(ctx.guild)
        if not channel:
            await ctx.send("No match channel configured.")
            return

        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return

        state = await self.season_service.get_state()
        etype = STAGE_EVENT_TYPES.get(state)
        if not etype:
            await ctx.send(f"No threads for state {state}.")
            return

        message = await ctx.send("Cleaning up threads...")
        active = await self.flow_service.unplayed_discord_ids(season_num, state)
        if active is None:
            await message.edit(content=f"No {state} event.")
            return

        cleaned = await cleanse_scheduling_threads(
            channel,
            set(active),
            await self.flow_service.registered_discord_ids(),
            self.bot.user.id,
        )
        await message.edit(content=f"Cleaned {cleaned} users from threads.")

    async def _sync_stage_roles(self, guild: discord.Guild, state: str, season: int) -> None:
        """Give the new stage's role to its players and strip old stage roles."""
        try:
            key = STATE_ROLE_KEYS.get(state)
            role = await get_configured_role(guild, key) if key else None
            eligible = await self.flow_service.stage_discord_ids(season, state) or set()
            result = await sync_role(
                guild,
                role,
                eligible,
                strip=await stage_roles(guild),
                reason=f"Stage advanced to {state}",
            )
            logger.info("Stage role sync for %s: %s", state, result.summary())
        except Exception:
            logger.exception("Stage role sync for %s failed", state)

    def _start_stage_role_sync(self, guild: discord.Guild, state: str, season: int) -> None:
        task = asyncio.create_task(self._sync_stage_roles(guild, state, season))
        self._role_tasks.add(task)
        task.add_done_callback(self._role_tasks.discard)

    async def _run_stage(self, guild, action, season_num: int, *args) -> dict:
        """Run a flow-service stage action and clean stage roles if it advanced."""
        result = await action(season_num, *args)
        if result.get("advanced_from"):
            if guild:
                self._start_stage_role_sync(guild, result["state"], season_num)
            await announce_stage(self.bot, result["state"])
        return result

    async def _stage_prefix(self, ctx, action, *args) -> None:
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return
        result = await self._run_stage(ctx.guild, action, season_num, *args)
        await ctx.send(result.get("error") or result["message"])

    async def _stage_slash(self, interaction: discord.Interaction, action, *args) -> None:
        await interaction.response.defer(ephemeral=True)
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        result = await self._run_stage(interaction.guild, action, season, *args)
        await interaction.followup.send(result.get("error") or result["message"], ephemeral=True)

    async def _open_impl(self, ctx):
        await self._stage_prefix(ctx, self.flow_service.start_open)

    @tourney_group.command(name="generate_open", help="Generate Open stage matches (4 per player)")
    @require_host()
    async def open_cmd(self, ctx: commands.Context):
        await self._open_impl(ctx)

    async def _skip_impl(self, season_num: int, raw: str) -> tuple[list[str], list[str], Optional[str]]:
        state = await self.season_service.get_state()
        if state != "SIGNUP":
            return [], [], f"Skip players must be set during SIGNUP (currently {state})."
        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(
                EventType.OPEN, season_num
            )
            if event:
                existing = await MatchRepository(session).get_by_event(event.id)
                if existing:
                    return [], [], "Open matches already exist. Skip setup must happen before Open generation."
        tokens = [part.strip() for part in raw.split(",") if part.strip()]
        success, errors = await self.signup_service.set_skip_open(season_num, tokens)
        return success, errors, None

    @tourney_group.command(name="skip_open", help="Set players who skip Open and enter Swiss seeded (comma-separated names/IDs)")
    @require_host()
    async def skip_open_cmd(self, ctx: commands.Context, *, players: str = ""):
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return
        success, errors, error = await self._skip_impl(season_num, players)
        if error:
            await ctx.send(error)
            return
        lines = ["Open-skip list updated."]
        if success:
            lines.append("\n".join(success))
        else:
            lines.append("No skips set (Open-skip list cleared).")
        if errors:
            lines.append("\n".join(errors))
        await ctx.send("\n".join(lines))

    async def _playin_impl(self, ctx):
        await self._stage_prefix(ctx, self.flow_service.start_playins)

    @tourney_group.command(
        name="generate_playin",
        help="Finish Open and create the complete Playin ladder (all rounds at once)",
    )
    @require_host()
    async def playin_cmd(self, ctx: commands.Context):
        await self._playin_impl(ctx)

    @tourney_group.command(name="finish_playins", help="Finalize Playins qualifiers")
    @require_host()
    async def finish_playins_cmd(self, ctx: commands.Context):
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return
        
        result = await self.flow_service.finish_playins(season_num)
        await ctx.send(result.get("error") or result["message"])

    async def _swiss_impl(self, ctx, round_num=None):
        await self._stage_prefix(ctx, self.flow_service.start_swiss, round_num)

    @tourney_group.command(name="generate_swiss", help="Generate the next Swiss round (or a specific one)")
    @require_host()
    async def swiss_cmd(
        self, ctx: commands.Context, round_num: Optional[int] = None
    ):
        await self._swiss_impl(ctx, round_num)

    async def _playoff_impl(self, ctx):
        await self._stage_prefix(ctx, self.flow_service.start_playoffs)

    @tourney_group.command(name="generate_playoffs", help="Generate Playoff Bracket")
    @require_host()
    async def playoff_cmd(self, ctx: commands.Context):
        await self._playoff_impl(ctx)

    @tourney_group.command(name="import_seeds", help="Bulk import returning seeds (1-16) by mention order")
    @require_host()
    async def import_seeds_cmd(self, ctx: commands.Context, *, seeds: str):
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return

        import re
        discord_ids = [int(m) for m in re.findall(r"(\d{17,20})", seeds)]
        if not discord_ids:
            await ctx.send("No valid Discord mentions found. Mention users like `@User1 @User2`.")
            return

        message = await ctx.send("Importing seeds...")
        success_msgs, error_msgs = await self.signup_service.import_seeds(discord_ids, season_num)
        lines = [f"Imported {len(success_msgs)} seeds:"]
        lines.extend(success_msgs)
        if error_msgs:
            lines.append(f"\nErrors ({len(error_msgs)}):")
            lines.extend(error_msgs)
        await message.edit(content="\n".join(lines))

    # Generation commands are only under /tourney group and /dashboard buttons.
    # Standings are separate commands (see standings_cog.py).

    # ── Slash commands (Discord-first progression) ─────────────────

    @tourney.command(name="status", description="Season stage summary and recommended next step")
    @require_host_app()
    async def slash_status(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        info = await self.flow_service.get_status()
        embed = discord.Embed(title="Tournament status", color=discord.Color.blue())
        if not info.get("season"):
            embed.description = info.get("next_action", "No season.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        embed.add_field(name="Season", value=str(info["season"]), inline=True)
        embed.add_field(name="State", value=info["state"], inline=True)
        for stage in ("OPEN", "PLAYIN", "SWISS", "PLAYOFF"):
            c = info["stages"].get(stage, {})
            if c.get("total", 0):
                embed.add_field(
                    name=stage,
                    value=f"{c['completed']}/{c['total']} done ({c['unplayed']} left)",
                    inline=False,
                )
        layout = info.get("playin_layout", {})
        if layout:
            exp = layout.get("expected_matches", {})
            value = (
                f"Qualifiers {layout.get('qualifiers')} · "
                f"Returning {layout.get('returning_seeds')} · "
                f"4W auto {len(layout.get('four_wins', []))} · "
                f"R1/R2/R3 matches: {exp.get(1, 0)}/{exp.get(2, 0)}/{exp.get(3, 0)} · "
                f"target {layout.get('target', 0)}"
            )
            if info.get("playin_cut_notice"):
                value += f"\n{info['playin_cut_notice']}"
            embed.add_field(name="Playin plan", value=value, inline=False)
        embed.add_field(name="Next step", value=info["next_action"], inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @tourney.command(name="sync_open", description="Rebuild Open win/loss stats from completed matches")
    @require_host_app()
    async def slash_sync_open(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        result = await self.flow_service.sync_open_stats(season)
        if result.get("error"):
            await interaction.followup.send(result["error"], ephemeral=True)
            return
        dist = result.get("win_distribution", {})
        await interaction.followup.send(
            f"Synced **{result['players_synced']}** players. Open wins: `{dist}`",
            ephemeral=True,
        )

    @tourney.command(
        name="start_playins",
        description="Finish Open and create the complete preseeded Playin ladder",
    )
    @require_host_app()
    async def slash_start_playins(self, interaction: discord.Interaction):
        await self._stage_slash(interaction, self.flow_service.start_playins)

    @tourney.command(
        name="playin_round",
        description="Report an existing Playin round's match and bye counts",
    )
    @app_commands.describe(round_num="Playin round number")
    @require_host_app()
    async def slash_playin_round(self, interaction: discord.Interaction, round_num: app_commands.Range[int, 1, 3]):
        await interaction.response.defer(ephemeral=True)
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        state = await self.season_service.get_state()
        if state != "PLAYIN":
            await interaction.followup.send(
                f"Must be PLAYIN (now {state}). Use `/tourney start_playins` first.",
                ephemeral=True,
            )
            return
        try:
            m, b = await self.playin_service.generate_round(season, round_num)
            msg = f"Playin R{round_num}: **{m}** matches, **{b}** byes."
            if round_num == 3:
                msg += " Playins finalize automatically after the last score."
            await interaction.followup.send(msg, ephemeral=True)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)

    @tourney.command(name="finish_playins", description="Manually retry Playin finalization")
    @require_host_app()
    async def slash_finish_playins(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        result = await self.flow_service.finish_playins(season)
        await interaction.followup.send(result.get("error") or result["message"], ephemeral=True)

    @tourney.command(
        name="skip_open",
        description="Set players who skip Open and enter Swiss seeded (comma-separated)",
    )
    @app_commands.describe(players="Names, Discord IDs, or mentions separated by commas; leave empty to clear")
    @app_commands.autocomplete(players=player_list_autocomplete)
    @require_host_app()
    async def slash_skip_open(
        self, interaction: discord.Interaction, players: str = ""
    ):
        await interaction.response.defer(ephemeral=True)
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        success, errors, error = await self._skip_impl(season, players)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return
        lines = ["Open-skip list updated."]
        if success:
            lines.append("\n".join(success))
        else:
            lines.append("No skips set (Open-skip list cleared).")
        if errors:
            lines.append("\n".join(errors))
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @tourney.command(name="generate_open", description="Create / regenerate Open matches (4 per player)")
    @app_commands.describe(
        regenerate="Delete all Open matches (including played ones) and pair again",
    )
    @require_host_app()
    async def slash_generate_open(self, interaction: discord.Interaction, regenerate: bool = False):
        if not regenerate:
            await self._stage_slash(interaction, self.flow_service.start_open)
            return
        await interaction.response.defer(ephemeral=True)
        if not await confirm(
            interaction,
            "Delete **every** Open match this season, including played results, "
            "and generate new pairings?",
        ):
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        result = await self._run_stage(interaction.guild, self.flow_service.start_open, season, True)
        await interaction.followup.send(result.get("error") or result["message"], ephemeral=True)

    @tourney.command(name="swap_players", description="Swap two players' Open matchups")
    @app_commands.describe(
        player1="Takes over player2's Open opponents",
        player2="Takes over player1's Open opponents",
    )
    @require_host_app()
    async def slash_swap_players(
        self, interaction: discord.Interaction, player1: discord.User, player2: discord.User
    ):
        await interaction.response.defer(ephemeral=True)
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        result = await self.pairing_service.swap_open_players(season, player1.id, player2.id)
        if "error" in result:
            await interaction.followup.send(result["error"], ephemeral=True)
            return
        await audit.record(
            "open.swap_players", "player", player1.id,
            other=player2.id, matches=result["matches"],
        )
        a, b = result["names"]
        await interaction.followup.send(
            f"Swapped **{a}** and **{b}** across {result['matches']} Open matches.",
            ephemeral=True,
        )

    @tourney.command(name="swiss_round", description="Create the Swiss bracket / show a round")
    @app_commands.describe(
        round_num="Optional round number to display; the whole ladder is created at once",
    )
    @require_host_app()
    async def slash_swiss_round(
        self,
        interaction: discord.Interaction,
        round_num: app_commands.Range[int, 1, 5] = None,
    ):
        await self._stage_slash(interaction, self.flow_service.start_swiss, round_num)

    @tourney.command(name="generate_playoffs", description="Seed / regenerate playoff bracket from Swiss results")
    @require_host_app()
    async def slash_generate_playoffs(self, interaction: discord.Interaction):
        await self._stage_slash(interaction, self.flow_service.start_playoffs)


async def setup(bot):
    cog = TournamentCog(bot)
    await bot.add_cog(cog)
