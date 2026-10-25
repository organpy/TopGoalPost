"""Tournament management cog."""

from typing import Optional
import discord
from discord.ext import commands
from discord import app_commands

from bot.cogs.checks import require_host, require_host_app
from bot.services.season import SeasonService
from bot.services.pairing import PairingService
from bot.services.playin import PlayinService
from bot.services.match import MatchService
from bot.services.signup import SignupService
from bot.services.tournament_flow import TournamentFlowService, STAGE_EVENTS
from bot.db import get_db_manager, MatchRepository, PlayerRepository, EventType, EventRepository
from bot.ui.autocomplete import player_list_autocomplete


STAGE_EVENT_TYPES = STAGE_EVENTS


def _split_groups(player_ids: list[int]) -> list[list[int]]:
    """Split player IDs into scheduling groups of ~3 with overlap for connectivity."""
    n = len(player_ids)
    if n <= 6:
        return [player_ids]
    num_groups = n // 3
    k, m = divmod(n, num_groups)
    lengths = [k + (1 if i < m else 0) for i in range(num_groups)]
    result = [player_ids[-3:] + player_ids[:lengths[0]]]
    for i, l in enumerate(lengths[1:], 1):
        s = sum(lengths[:i])
        result.append(player_ids[s - 3:s + l])
    return result


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

        async with self.db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.get_by_type_and_season(etype, season_num)
            if not event:
                await message.edit(content=f"No {state} event found.")
                return
            match_repo = MatchRepository(session)
            matches = await match_repo.get_unplayed_by_event(event.id)

        seen = set()
        all_pids = []
        for match in matches:
            for mp in match.match_players:
                pid = mp.player_id
                if pid and pid not in seen:
                    seen.add(pid)
                    all_pids.append(pid)

        if not all_pids:
            await message.edit(content="No players with unplayed matches.")
            return

        groups = _split_groups(all_pids)
        created = 0
        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            for i, gids in enumerate(groups):
                try:
                    thread = await channel.create_thread(
                        name=f"{state} scheduling #{i + 1}",
                        reason=f"{state} scheduling group {i + 1}"
                    )
                    for pid in gids:
                        p = await player_repo.get_by_id(pid)
                        if p:
                            try:
                                await thread.add_user(discord.Object(p.discord_id))
                            except Exception:
                                pass
                    created += 1
                except Exception:
                    continue

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

        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(etype, season_num)
            if not event:
                await message.edit(content=f"No {state} event.")
                return
            unplayed = await MatchRepository(session).get_unplayed_by_event(event.id)

        active_player_ids = set()
        for m in unplayed:
            for mp in m.match_players:
                if mp.player_id:
                    active_player_ids.add(mp.player_id)

        threads = channel.threads
        cleaned = 0
        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            for thread in threads:
                try:
                    members = await thread.fetch_members()
                except Exception:
                    continue
                for member in members:
                    if member.id == self.bot.user.id:
                        continue
                    p = await player_repo.get_by_discord_id(member.id)
                    if not p:
                        continue
                    if p.id not in active_player_ids:
                        try:
                            await thread.remove_user(discord.Object(member.id))
                            cleaned += 1
                        except Exception:
                            pass

        await message.edit(content=f"Cleaned {cleaned} users from threads.")

    async def _cleanup_stage_roles(self, guild: discord.Guild, previous_state: str, new_state: str):
        from bot.config import Config
        cfg = Config()
        
        # Remove all stage roles. Keep only participant role.
        roles_to_clean = []
        for role_key in ["open_role", "playin_role", "swiss_role", "playoff_role"]:
            rid = await cfg.get(role_key)
            if rid:
                try:
                    roles_to_clean.append(guild.get_role(int(rid)))
                except ValueError:
                    pass
                    
        roles_to_clean = [r for r in roles_to_clean if r is not None]
        if not roles_to_clean:
            return
            
        for member in guild.members:
            to_remove = [r for r in roles_to_clean if r in member.roles]
            if to_remove:
                try:
                    await member.remove_roles(*to_remove, reason=f"Stage advanced from {previous_state} to {new_state}")
                except discord.Forbidden:
                    pass
                    
    async def _run_stage(self, guild, action, season_num: int, *args) -> dict:
        """Run a flow-service stage action and clean stage roles if it advanced."""
        result = await action(season_num, *args)
        if result.get("advanced_from") and guild:
            await self._cleanup_stage_roles(guild, result["advanced_from"], result["state"])
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
        
        message = await ctx.send("Finishing playins...")
        try:
            await self.playin_service.finish_playins(season_num)
            await message.edit(content="Playins finished! Qualifiers have been ranked.")
        except Exception as e:
            await message.edit(content=f"Error finishing playins: {e}")

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
    @app_commands.default_permissions(administrator=True)
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
    @app_commands.default_permissions(administrator=True)
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
    @app_commands.default_permissions(administrator=True)
    @require_host_app()
    async def slash_start_playins(self, interaction: discord.Interaction):
        await self._stage_slash(interaction, self.flow_service.start_playins)

    @tourney.command(
        name="playin_round",
        description="Report an existing Playin round's match and bye counts",
    )
    @app_commands.describe(round_num="Playin round number")
    @app_commands.default_permissions(administrator=True)
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
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @tourney.command(name="finish_playins", description="Manually retry Playin finalization")
    @app_commands.default_permissions(administrator=True)
    @require_host_app()
    async def slash_finish_playins(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        season = await self.season_service.get_current_season_number()
        if not season:
            await interaction.followup.send("No active season.", ephemeral=True)
            return
        try:
            await self.playin_service.finish_playins(season)
            await interaction.followup.send(
                "Playins finalized. Start Swiss with `/tourney swiss_round`.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @tourney.command(
        name="skip_open",
        description="Set players who skip Open and enter Swiss seeded (comma-separated)",
    )
    @app_commands.describe(players="Names, Discord IDs, or mentions separated by commas; leave empty to clear")
    @app_commands.autocomplete(players=player_list_autocomplete)
    @app_commands.default_permissions(administrator=True)
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
    @app_commands.default_permissions(administrator=True)
    @require_host_app()
    async def slash_generate_open(self, interaction: discord.Interaction):
        await self._stage_slash(interaction, self.flow_service.start_open)

    @tourney.command(name="swiss_round", description="Create the Swiss bracket / show a round")
    @app_commands.describe(
        round_num="Optional round number to display; the whole ladder is created at once",
    )
    @app_commands.default_permissions(administrator=True)
    @require_host_app()
    async def slash_swiss_round(
        self,
        interaction: discord.Interaction,
        round_num: app_commands.Range[int, 1, 5] = None,
    ):
        await self._stage_slash(interaction, self.flow_service.start_swiss, round_num)

    @tourney.command(name="generate_playoffs", description="Seed / regenerate playoff bracket from Swiss results")
    @app_commands.default_permissions(administrator=True)
    @require_host_app()
    async def slash_generate_playoffs(self, interaction: discord.Interaction):
        await self._stage_slash(interaction, self.flow_service.start_playoffs)


async def setup(bot):
    cog = TournamentCog(bot)
    await bot.add_cog(cog)
