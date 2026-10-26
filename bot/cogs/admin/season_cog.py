"""Admin cog for season management."""

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord.ext import commands
from discord import app_commands
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.utils.errors import error_message, log_error
from bot.cogs.checks import require_host, require_host_app
from bot.services.season import SeasonService, STATE_ORDER, next_state, transition_error
from bot.db import get_db_manager, PlayerSeason, SeasonStatus
from bot.config import Config
from bot.utils.announce import announce, announce_stage, refresh_presence


STATE_CHOICES = [app_commands.Choice(name=state, value=state) for state in STATE_ORDER]


class SeasonAdminCog(commands.Cog):
    season = app_commands.Group(name="season", description="Season lifecycle (host)")

    def __init__(self, bot):
        self.bot = bot
        self.db = get_db_manager()
        self.season_service = SeasonService()

    @commands.group(name="season", invoke_without_command=True)
    @require_host()
    async def season_group(self, ctx: commands.Context):
        """Season management commands."""
        await ctx.send_help(ctx.command)

    @season_group.command(name="status", help="Check current season status")
    @require_host()
    async def status_cmd(self, ctx: commands.Context):
        await ctx.send(embed=await self.status_embed())

    async def status_embed(self) -> discord.Embed:
        season_num = await self.season_service.get_current_season_number()
        state = await self.season_service.get_state()

        embed = discord.Embed(title="Season Status", color=discord.Color.blue())
        if season_num:
            embed.add_field(name="Current Season", value=f"Season {season_num}", inline=False)
            embed.add_field(name="State", value=state, inline=False)
            nxt = next_state(state)
            if nxt:
                embed.add_field(name="Next state", value=nxt, inline=False)
        else:
            embed.description = "No active season. Run `/season create <N>`."
        return embed

    @season_group.command(
        name="create",
        help="Create a new season (archives the current one, history is kept). "
        "Add `force` to archive an unclosed season.",
    )
    @require_host()
    async def create_cmd(self, ctx: commands.Context, number: int, force: str = ""):
        await ctx.send(await self.create_message(number, force.lower() == "force"))

    async def create_message(self, number: int, force: bool = False) -> str:
        existing = await self.season_service.get_season(number)
        if existing:
            if existing.status == SeasonStatus.ARCHIVED:
                return f"Season {number} is archived and read-only. Pick a new season number."
            return f"Season {number} is already active."
        if not force and (blocker := await self.season_service.create_blocker()):
            return blocker

        if await self.season_service.create_season(number, force=force):
            return (
                f"Created Season {number} and set state to OFFSEASON. "
                "Previous seasons stay available in `/seasons`. "
                "Next: `/season advance SIGNUP`."
            )
        return f"Season {number} already exists."

    @season_group.command(
        name="advance",
        help="Advance to the next season state. Add `force` to jump or rewind.",
    )
    @require_host()
    async def advance_cmd(self, ctx: commands.Context, state: str, force: str = ""):
        await ctx.send(await self.advance_message(state, force.lower() == "force"))

    async def advance_message(self, state: str, force: bool = False) -> str:
        """Apply a state change and describe the result (shared by prefix/slash)."""
        target = state.upper()
        current = await self.season_service.get_state()
        error = transition_error(current, target)
        if error and (not force or target not in STATE_ORDER):
            return error

        await self.season_service.advance_state(target, force=force)
        if target != current:
            await announce_stage(self.bot, target)
        msg = f"Season state: **{current} → {target}**" + (" (forced)" if error else "")
        if target == "PLAYIN":
            msg += (
                "\n⚠️ This does **not** create playin matches. "
                "Run `/tourney start_playins` or dashboard **Start Playins** after Open is scored."
            )
        elif target == "OPEN":
            msg += "\nGenerate matches with `/tourney generate_open` if you have not already."
        return msg

    @season.command(name="status", description="Current season, state and next state")
    @require_host_app()
    async def slash_status(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=await self.status_embed(), ephemeral=True)

    @season.command(name="create", description="Create a new season (archives the current one)")
    @app_commands.describe(
        number="New season number (never reuse an archived one)",
        force="Archive the current season even if it was never closed",
    )
    @require_host_app()
    async def slash_create(
        self,
        interaction: discord.Interaction,
        number: app_commands.Range[int, 1],
        force: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.create_message(number, force), ephemeral=True)

    @season.command(name="close", description="Close the season after the grand final")
    @require_host_app()
    async def slash_close(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(await self.close_message(), ephemeral=True)

    @season.command(name="advance", description="Move to the next season state")
    @app_commands.describe(
        state="Target state (normally the next one)",
        force="Allow skipping or rewinding stages (logged)",
    )
    @app_commands.choices(state=STATE_CHOICES)
    @require_host_app()
    async def slash_advance(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str],
        force: bool = False,
    ):
        await interaction.response.send_message(
            await self.advance_message(state.value, force), ephemeral=True
        )

    @season_group.command(name="close", help="Close the season after the grand final")
    @require_host()
    async def close_cmd(self, ctx: commands.Context):
        await ctx.send(await self.close_message())

    async def close_message(self) -> str:
        season = await self.season_service.get_current_season_number()
        if not season:
            return "No active season."
        result = await self.season_service.close_season(season)
        if "error" in result:
            return result["error"]
        recap = f"🏆 **Season {season} is over!** Champion: **{result['champion']}**"
        if result["runner_up"]:
            recap += f" · Runner-up: **{result['runner_up']}**"
        recap += ". Full recap: `/season_summary`."
        await announce(self.bot, recap)
        await refresh_presence(self.bot)
        return recap + f"\nNext: `/season create {season + 1}`."

    @commands.command(name="playerlist", help="View all registered players")
    @require_host()
    async def playerlist_cmd(self, ctx: commands.Context):
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season.")
            return

        async with self.db.get_session() as session:
            stmt = select(PlayerSeason).options(selectinload(PlayerSeason.player)).where(PlayerSeason.season_number == season_num)
            result = await session.execute(stmt)
            players = result.scalars().all()

        if not players:
            await ctx.send("No players registered.")
            return

        lines = []
        for ps in sorted(players, key=lambda x: (x.swiss_seed is None, x.swiss_seed, x.player.name)):
            seed_str = f"[Seed {ps.swiss_seed}] " if ps.swiss_seed else ""
            lines.append(f"• {seed_str}**{ps.player.name}** (HP: {ps.hp}, Open: {ps.open_wins}-{ps.open_losses})")

        # Quick pagination/chunking
        chunks = []
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 1900:
                chunks.append(chunk)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            chunks.append(chunk)

        await ctx.send(f"**Registered Players ({len(players)}) - Page 1/{len(chunks)}**\n{chunks[0]}")
        for c in chunks[1:]:
            await ctx.send(c)

    CONFIG_KEYS = {
        "match_channel": "Channel for match scheduling threads",
        "score_channel": "Channel for score confirmation messages",
        "agreements_channel": "Channel where agreed match times are announced",
        "announcements_channel": "Channel for public stage and season announcements",
        "admin_id": "Discord ID of the head admin (contact for errors)",
        "admin_roles": "Comma-separated role IDs allowed to use admin/host commands",
        "signup_role": "Role given to signed-up players (never removed by giveroles)",
        "moderator_role": "Role that can confirm/reject scores",
        "open_role": "Role for Open stage participants",
        "playin_role": "Role for Playin stage participants",
        "swiss_role": "Role for Swiss stage participants",
        "playoff_role": "Role for Playoff stage participants",
    }

    @commands.command(name="set_config", help="Set a bot configuration value")
    @require_host()
    async def set_config_cmd(self, ctx: commands.Context, key: str, *, value: str):
        if key not in self.CONFIG_KEYS:
            await ctx.send(f"Unknown config key. Valid keys: {', '.join(self.CONFIG_KEYS.keys())}")
            return

        cfg = Config()
        if key == "match_channel":
            # Validate it's a channel ID
            try:
                cid = int(value)
                ch = ctx.guild.get_channel(cid)
                if not ch:
                    await ctx.send("Invalid channel ID.")
                    return
            except ValueError:
                await ctx.send("Channel ID must be a number.")
                return
        await cfg.set(key, value)
        await ctx.send(f"Set `{key}` to `{value}`.")

    @commands.command(name="remove_config", help="Remove a bot configuration value")
    @require_host()
    async def remove_config_cmd(self, ctx: commands.Context, key: str):
        cfg = Config()
        removed = await cfg.remove(key)
        if removed:
            await ctx.send(f"Removed `{key}`.")
        else:
            await ctx.send(f"`{key}` was not set.")

    @commands.command(name="backup", help="Backup the database")
    @require_host()
    async def backup_cmd(self, ctx: commands.Context):
        from bot.db import PROJECT_ROOT, resolve_database_url

        backup_dir = PROJECT_ROOT / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        db_url = resolve_database_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db"))
        sqlite_prefix = "sqlite+aiosqlite:///"
        if not db_url.startswith(sqlite_prefix):
            await ctx.send("Backup is only supported for sqlite databases.")
            return
        db_path = Path(db_url[len(sqlite_prefix):])
        if not db_path.exists():
            await ctx.send(f"No database file found at `{db_path}`.")
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{db_path.stem}_{timestamp}.db"
        try:
            shutil.copy2(str(db_path), str(backup_path))
            size_mb = backup_path.stat().st_size / (1024 * 1024)
            await ctx.send(f"Backup created: `{backup_path}` ({size_mb:.1f} MB)")
        except OSError as e:
            error_id = log_error("Database backup", e)
            await ctx.send(f"Backup failed. {error_message(error_id)}")


async def setup(bot):
    await bot.add_cog(SeasonAdminCog(bot))
