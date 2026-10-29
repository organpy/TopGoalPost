"""Admin utility cog: hot-reload, avatars, roles, threads."""

import importlib
import logging
import os

from datetime import timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.errors import error_message, log_error
from bot.cogs.checks import require_host, require_host_app

from bot.db import get_db_manager, PlayerRepository
from bot.services import audit
from bot.services.health import Check, HealthService
from bot.services.tournament_flow import TournamentFlowService
from bot.services.season import SeasonService
from bot.config import Config
from bot.rendering import StandingsMaker, ASSETS_ROOT
from bot.rendering.cache import clear_render_cache
from bot.utils.roles import (
    STAGE_ROLE_KEYS,
    STATE_ROLE_KEYS,
    get_configured_role,
    role_problem,
    stage_roles,
    sync_role,
)

logger = logging.getLogger(__name__)

GIVEROLES_ALIASES = {
    "signup": "SIGNUP",
    "open": "OPEN",
    "playin": "PLAYIN",
    "playins": "PLAYIN",
    "swiss": "SWISS",
    "playoff": "PLAYOFF",
    "playoffs": "PLAYOFF",
}

# Config key -> default role name created by !setup_roles.
SETUP_ROLES = {
    "signup_role": "Signup",
    "open_role": "Open",
    "playin_role": "Playins",
    "swiss_role": "Swiss",
    "playoff_role": "Playoffs",
}


async def _save_avatar(user: discord.abc.User) -> bool:
    """Store a registered player's avatar as assets/pfps/<id>.png."""
    pfp_dir = ASSETS_ROOT / "pfps"
    pfp_dir.mkdir(parents=True, exist_ok=True)
    try:
        await user.display_avatar.save(fp=str(pfp_dir / f"{user.id}.png"))
        return True
    except (discord.HTTPException, OSError):
        logger.warning("Could not save avatar for %s", user.id, exc_info=True)
        return False


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

    @commands.command(name="resetimages", help="Clear the rendered-image cache")
    @require_host()
    async def resetimages_cmd(self, ctx: commands.Context):
        count = await clear_render_cache()
        await ctx.send(
            f"Rendered-image cache cleared ({count} entries). "
            "The next request will re-render."
        )

    @app_commands.command(
        name="reset_cache",
        description="Clear cached rendered images (host)",
    )
    @require_host_app()
    async def reset_cache_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        count = await clear_render_cache()
        await interaction.followup.send(
            f"Rendered-image cache cleared ({count} entries). "
            "The next request will re-render.",
            ephemeral=True,
        )

    @commands.command(name="sync_commands", hidden=True)
    @require_host()
    async def sync_commands_prefix(self, ctx: commands.Context):
        """Force-resync all application commands (prefix fallback)."""
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

    @commands.command(name="getpfps", help="Download registered players' avatars to pfps/")
    @require_host()
    async def getpfps_cmd(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("Guild only.")
            return

        message = await ctx.send("Downloading avatars...")
        count = 0
        for discord_id in await TournamentFlowService().registered_discord_ids():
            member = ctx.guild.get_member(discord_id)
            if member is None:
                continue  # left the server; keep any avatar we already have
            if await _save_avatar(member):
                count += 1
        await message.edit(content=f"Downloaded {count} registered players' avatars.")

    @commands.command(
        name="giveroles",
        help="Give a stage's role to all its players and strip old stage roles "
        "(stage: signup, open, playin, swiss, playoff; default: current)",
    )
    @require_host()
    async def giveroles_cmd(self, ctx: commands.Context, stage: Optional[str] = None):
        if not ctx.guild:
            await ctx.send("Guild only.")
            return

        state = (
            GIVEROLES_ALIASES.get(stage.strip().lower())
            if stage
            else await self.season_service.get_state()
        )
        key = STATE_ROLE_KEYS.get(state or "")
        if not key:
            await ctx.send(
                f"No role for `{stage or state}`. Use one of: "
                "signup, open, playin, swiss, playoff."
            )
            return

        role = await get_configured_role(ctx.guild, key)
        if role is None:
            await ctx.send(f"`{key}` is not set. Run `!setup_roles` first.")
            return
        strip = await stage_roles(ctx.guild) if key in STAGE_ROLE_KEYS else []
        for checked in [role, *strip]:
            problem = role_problem(ctx.guild, checked)
            if problem:
                await ctx.send(problem, allowed_mentions=discord.AllowedMentions.none())
                return

        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return
        eligible = await TournamentFlowService().stage_discord_ids(season_num, state)
        if eligible is None:
            await ctx.send(f"{state} hasn't been generated yet.")
            return

        message = await ctx.send(
            f"Syncing {role.mention} for {len(eligible)} players…",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        async def progress(done: int, total: int) -> None:
            try:
                await message.edit(content=f"Syncing {role.mention}… {done}/{total}")
            except discord.HTTPException:
                pass

        result = await sync_role(
            ctx.guild,
            role,
            eligible,
            strip=strip,
            reason=f"giveroles by {ctx.author}",
            progress=progress,
        )
        await audit.record(
            "roles.give", "role", role.id, actor_id=ctx.author.id,
            stage=state, added=result.added, removed=result.removed,
            failed=len(result.failed),
        )
        await message.edit(
            content=f"**{role.mention} synced for {state}.**\n{result.summary()}",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(
        name="setup_roles",
        help="Create the signup and stage roles and save their IDs "
        "(optional name prefix, e.g. !setup_roles S5)",
    )
    @require_host()
    async def setup_roles_cmd(self, ctx: commands.Context, *, prefix: str = ""):
        if not ctx.guild:
            await ctx.send("Guild only.")
            return

        cfg = Config()
        prefix = prefix.strip()
        lines = []
        for key, base_name in SETUP_ROLES.items():
            role_id = await cfg.get_role_id(key)
            existing = ctx.guild.get_role(role_id) if role_id else None
            if existing is not None:
                lines.append(f"• `{key}`: already set to {existing.mention}")
                continue
            name = f"{prefix} {base_name}" if prefix else base_name
            try:
                role = await ctx.guild.create_role(
                    name=name,
                    mentionable=True,
                    reason=f"setup_roles by {ctx.author}",
                )
            except discord.HTTPException as e:
                lines.append(f"• `{key}`: could not create **{name}** ({e.__class__.__name__})")
                continue
            await cfg.set(key, str(role.id))
            await audit.record(
                "roles.setup", "role", role.id, actor_id=ctx.author.id,
                key=key, name=name,
            )
            lines.append(f"• `{key}`: created {role.mention}")

        await ctx.send(
            "**Role setup**\n" + "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="signupbutton", hidden=True, help="Post the persistent signup button")
    @require_host()
    async def signupbutton_cmd(self, ctx: commands.Context):
        from bot.ui.signup_button import SignupView, signup_panel_embed

        if await self.season_service.get_state() != "SIGNUP":
            await ctx.send("Signups are closed — the season has already started.")
            return

        season = await self.season_service.get_current_season_number()
        await ctx.send(embed=signup_panel_embed(season), view=SignupView())
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
        await clear_render_cache()
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
        """Refresh avatars of registered players only; others are never stored."""
        if before.display_avatar == after.display_avatar:
            return
        async with self.db.get_session() as session:
            if not await PlayerRepository(session).get_by_discord_id(after.id):
                return
        await _save_avatar(after)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
