"""Admin utility cog: hot-reload, avatars, roles, threads."""

import importlib
import os

from datetime import timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.errors import error_message, log_error
from bot.cogs.checks import require_host, require_host_app

from bot.db import get_db_manager, ImageCacheRepository
from bot.services import audit
from bot.services.health import Check, HealthService
from bot.services.season import SeasonService
from bot.config import Config
from bot.rendering import StandingsMaker, ASSETS_ROOT


STAGE_EVENT_TYPES = {
    "OPEN": "open_role",
    "PLAYIN": "playin_role",
    "SWISS": "swiss_role",
    "PLAYOFF": "playoff_role",
}


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = get_db_manager()
        self.season_service = SeasonService()

    @commands.command(name="reimport", help="Hot-reload the StandingsMaker module")
    @require_host()
    async def reimport_cmd(self, ctx: commands.Context):
        importlib.reload(StandingsMaker)
        await ctx.send("StandingsMaker reloaded.")

    @commands.command(name="resetimages", help="Clear the standings image cache")
    @require_host()
    async def resetimages_cmd(self, ctx: commands.Context):
        async with self.db.get_session() as session:
            repo = ImageCacheRepository(session)
            await repo.delete_all()
            await session.commit()
        await ctx.send("Image cache cleared.")

    @commands.command(name="sync_commands", hidden=True)
    async def sync_commands_prefix(self, ctx: commands.Context):
        """Force-resync all application commands (prefix fallback)."""
        if not ctx.author.guild_permissions.administrator:
            await ctx.send("You don't have permissions to do that.")
            return
        try:
            await ctx.send("Syncing application commands (clearing global cache and copying to guild)...")
            guild_id = os.getenv("SERVER_ID")
            
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                # 1. Copy global commands to this specific guild
                self.bot.tree.copy_global_to(guild=guild)
                # 2. Clear global commands from the local tree so we don't sync them globally
                self.bot.tree.clear_commands(guild=None)
                # 3. Sync empty global list to Discord to delete the duplicates
                await self.bot.tree.sync(guild=None)
                # 4. Sync the guild commands
                synced_guild = await self.bot.tree.sync(guild=guild)
                await ctx.send(f"Sync complete. Cleared global commands, synced {len(synced_guild)} commands to testing guild.")
            else:
                # If no server ID, just sync globally
                synced = await self.bot.tree.sync()
                await ctx.send(f"Sync complete. {len(synced)} global commands synced.")
        except discord.HTTPException as e:
            error_id = log_error("Command sync", e)
            await ctx.send(f"Sync failed. {error_message(error_id)}")

    @commands.command(name="getpfps", help="Download all guild member avatars to pfps/")
    @require_host()
    async def getpfps_cmd(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("Guild only.")
            return
        
        message = await ctx.send("Downloading avatars...")
        count = 0
        pfp_dir = ASSETS_ROOT / "pfps"
        pfp_dir.mkdir(parents=True, exist_ok=True)
        async for member in ctx.guild.fetch_members():
            try:
                await member.display_avatar.save(fp=str(pfp_dir / f"{member.id}.png"))
                count += 1
            except Exception:
                continue
        await message.edit(content=f"Downloaded {count} avatars to {pfp_dir}.")

    @commands.command(name="giveroles", help="Assign current stage role to eligible players")
    @require_host()
    async def giveroles_cmd(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("Guild only.")
            return

        state = await self.season_service.get_state()
        role_key = STAGE_EVENT_TYPES.get(state)
        if not role_key:
            await ctx.send(f"No stage role defined for state {state}.")
            return

        cfg = Config()
        target_role_id = await cfg.get_role_id(role_key)
        if not target_role_id:
            await ctx.send(f"`{role_key}` not configured. Set it via `!set_config`.")
            return

        # Clear all stage roles (but NOT the signup_role)
        stage_role_ids = set()
        for rk in STAGE_EVENT_TYPES.values():
            rid = await cfg.get_role_id(rk)
            if rid:
                stage_role_ids.add(rid)

        # Collect eligible player discord IDs from unplayed matches of the current event
        from bot.db import EventRepository, MatchRepository, PlayerRepository, EventType
        from bot.cogs.tournament.tournament_cog import STAGE_EVENT_TYPES as STAGE_MAP

        etype = STAGE_MAP.get(state)
        if not etype:
            await ctx.send(f"No event type for state {state}.")
            return

        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return

        eligible_discord_ids = set()
        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(etype, season_num)
            if event:
                matches = await MatchRepository(session).get_unplayed_by_event(event.id)
                player_repo = PlayerRepository(session)
                for m in matches:
                    for mp in m.match_players:
                        if mp.player_id:
                            p = await player_repo.get_by_id(mp.player_id)
                            if p:
                                eligible_discord_ids.add(p.discord_id)

        if not eligible_discord_ids:
            await ctx.send("No eligible players found for current stage.")
            return

        target_role = ctx.guild.get_role(target_role_id)
        if not target_role:
            await ctx.send(f"Role {target_role_id} not found.")
            return

        stage_roles = [r for r in [ctx.guild.get_role(rid) for rid in stage_role_ids] if r is not None]
        added = 0
        message = await ctx.send(f"Assigning {role_key} to eligible players...")
        async for member in ctx.guild.fetch_members():
            if member.id in eligible_discord_ids:
                to_remove = [r for r in stage_roles if r != target_role and r in member.roles]
                if to_remove:
                    try:
                        await member.remove_roles(*to_remove, reason="giveroles: clearing old stage roles")
                    except discord.Forbidden:
                        pass
                if target_role not in member.roles:
                    try:
                        await member.add_roles(target_role, reason="giveroles: assigned current stage role")
                        added += 1
                    except discord.Forbidden:
                        continue

        await message.edit(content=f"Assigned {role_key} to {added} players. Old stage roles cleared.")

    @commands.command(name="signupbutton", hidden=True, help="Post the persistent signup button")
    @require_host()
    async def signupbutton_cmd(self, ctx: commands.Context):
        from bot.ui.signup_button import SignupView

        if await self.season_service.get_state() != "SIGNUP":
            await ctx.send("Signups are closed — the season has already started.")
            return

        await ctx.send(view=SignupView())
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    @commands.command(name="sync_hp", hidden=True, help="Recalculate Open HP/stats from completed matches")
    @require_host()
    async def sync_hp_cmd(self, ctx: commands.Context):
        from bot.services.match import MatchService

        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return
        result = await MatchService().sync_open_stats(season_num)
        if result.get("error"):
            await ctx.send(result["error"])
            return
        await ctx.send(f"Synced HP/stats for {result['players_synced']} players.")

    @commands.command(name="getleaderboard", hidden=True, help="Mention the top N open-stage players")
    @require_host()
    async def getleaderboard_cmd(self, ctx: commands.Context, amount: int):
        async with self.db.get_session() as session:
            repo = ImageCacheRepository(session)
            await repo.delete_all()
            await session.commit()

        result = await StandingsMaker.get_open_with_players(sort_by_rank=True)
        players = result[3]
        ids = [f"<@{p.id}>" for p in players[:amount]]
        await ctx.send("\n".join(ids) if ids else "No players found.")

    @commands.command(
        name="unthreadedgames",
        hidden=True,
        help="List players with unplayed matches who are in no scheduling thread",
    )
    @require_host()
    async def unthreadedgames_cmd(self, ctx: commands.Context):
        from bot.services.tournament_flow import TournamentFlowService
        from bot.utils.threads import get_match_thread_channel, players_without_thread, stage_threads

        season_num = await self.season_service.get_current_season_number()
        state = await self.season_service.get_state()
        discord_ids = (
            await TournamentFlowService().unplayed_discord_ids(season_num, state)
            if season_num else None
        )
        if not discord_ids:
            await ctx.send(f"No unplayed {state} matches.")
            return
        channel = await get_match_thread_channel(ctx.guild) if ctx.guild else None
        if not channel:
            await ctx.send("No match channel configured.")
            return
        if not stage_threads(channel, state):
            await ctx.send(f"{state} has no scheduling threads yet. Run `!tourney makethreads`.")
            return

        missing = await players_without_thread(channel, state, discord_ids)
        if not missing:
            await ctx.send(f"Every player with an unplayed {state} match is in a scheduling thread.")
            return
        await ctx.send(
            f"Players with unplayed {state} matches but no scheduling thread:\n"
            + " ".join(f"<@{d}>" for d in missing)
        )

    @app_commands.command(name="audit", description="Recent admin and score changes (host)")
    @app_commands.describe(
        limit="How many entries (max 25)",
        action="Filter by action prefix, e.g. score, stage, season, config, reset",
    )
    @require_host_app()
    async def audit_cmd(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 15,
        action: Optional[str] = None,
    ):
        entries = await audit.recent(limit, action)
        if not entries:
            await interaction.response.send_message("No audit entries.", ephemeral=True)
            return
        lines = []
        for e in entries:
            ts = int(e.created_at.replace(tzinfo=timezone.utc).timestamp())
            who = f"<@{e.actor_id}>" if e.actor_id else "system"
            target = f" {e.target_type}:{e.target_id}" if e.target_type else ""
            details = f" `{e.details}`" if e.details else ""
            lines.append(f"<t:{ts}:f> {who} **{e.action}**{target}{details}")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @app_commands.command(name="doctor", description="Check configuration, permissions and database health (host)")
    @require_host_app()
    async def doctor_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        checks = await HealthService().check()
        if interaction.guild:
            checks += await self._discord_checks(interaction.guild)
        failed = [c for c in checks if not c.ok]
        lines = [f"{c.icon} {c.name}" + (f" — {c.detail}" if c.detail else "") for c in checks]
        header = "All checks passed." if not failed else f"{len(failed)} issue(s) found."
        await interaction.followup.send(f"**Doctor:** {header}\n" + "\n".join(lines), ephemeral=True)

    async def _discord_checks(self, guild: discord.Guild) -> list[Check]:
        cfg = Config()
        checks: list[Check] = []
        for key in ("match_channel", "score_channel", "agreements_channel", "announcements_channel"):
            cid = await cfg.get_channel_id(key)
            if cid:
                found = guild.get_channel(cid) is not None
                checks.append(Check(f"channel `{key}` exists", found, "" if found else f"{cid} not found"))
        for key in ("signup_role", "moderator_role"):
            rid = await cfg.get_role_id(key)
            if rid:
                found = guild.get_role(rid) is not None
                checks.append(Check(f"role `{key}` exists", found, "" if found else f"{rid} not found"))
        for rid in await cfg.get_admin_role_ids():
            found = guild.get_role(rid) is not None
            checks.append(Check(f"host role {rid} exists", found))

        me = guild.me
        if me is not None:
            checks.append(Check("bot can Manage Roles", me.guild_permissions.manage_roles))
            match_channel = guild.get_channel(await cfg.get_channel_id("match_channel") or 0)
            if match_channel is not None:
                perms = match_channel.permissions_for(me)
                for perm in ("send_messages", "embed_links", "attach_files",
                             "create_public_threads", "manage_threads"):
                    checks.append(Check(f"match channel: {perm}", getattr(perms, perm)))
        return checks

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        try:
            pfp_dir = ASSETS_ROOT / "pfps"
            pfp_dir.mkdir(parents=True, exist_ok=True)
            await after.display_avatar.save(fp=str(pfp_dir / f"{after.id}.png"))
        except Exception:
            pass


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
