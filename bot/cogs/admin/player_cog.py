"""Admin player management.

Inspect and edit any player's identity, availability, season stats and
matches so the database never has to be touched by hand.
"""

from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

from bot.cogs.checks import require_host_app
from bot.cogs.scheduling.schedule_cog import (
    _format_minutes,
    day_autocomplete,
    parse_hhmm,
    resolve_local_datetime,
    slot_autocomplete,
    time_autocomplete,
    timezone_autocomplete,
)
from bot.db import (
    get_db_manager,
    PlayerRepository,
    PlayerSeasonRepository,
)
from bot.services.match import MatchService
from bot.services.scheduler import SchedulerService, WEEKDAYS, parse_weekday
from bot.services.season import SeasonService
from bot.services.signup import SignupService
from bot.ui.confirm import confirm
from bot.utils.countries import country_choices, resolve_country
from bot.utils.threads import sync_player_threads

CLEAR_TOKENS = {"none", "clear", "-"}


def _is_clear(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in CLEAR_TOKENS


class PlayerCog(commands.Cog):
    """Host-only tools for editing player records."""

    player = app_commands.Group(
        name="player", description="Inspect and edit any player (admin)"
    )

    def __init__(self, bot):
        self.bot = bot
        self.db = get_db_manager()
        self.scheduler = SchedulerService()
        self.season_service = SeasonService()
        self.signup_service = SignupService()
        self.match_service = MatchService()

    # ── helpers ───────────────────────────────────────────────

    async def _load_player(self, session, target: discord.User):
        return await PlayerRepository(session).get_by_discord_id(target.id)

    async def _target_tz(self, target: discord.User):
        timezone_str = await self.scheduler.get_timezone(target.id)
        return self.scheduler.resolve_timezone(timezone_str) if timezone_str else None

    @staticmethod
    def _slot_lines(dated, weekly, tz=None) -> list[str]:
        lines = []
        for slot in weekly:
            lines.append(
                f"**{WEEKDAYS[slot.weekday]}** "
                f"{_format_minutes(slot.start_minute)}–"
                f"{_format_minutes(slot.finish_minute)}"
            )
        for slot in dated:
            start = datetime.fromtimestamp(slot.start, tz)
            finish = datetime.fromtimestamp(slot.finish, tz)
            line = f"{start.strftime('%b %d %H:%M')}–{finish.strftime('%H:%M')}"
            if slot.prio:
                line += " (preferred)"
            lines.append(line)
        return lines

    # ── info ──────────────────────────────────────────────────

    @player.command(name="info", description="Show everything stored for a player")
    @app_commands.describe(target="The player to inspect")
    @require_host_app()
    async def info_cmd(
        self, interaction: discord.Interaction, target: discord.User
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = await self.season_service.get_current_season_number()

        timezone_str = await self.scheduler.get_timezone(target.id)
        weekly = await self.scheduler.get_weekly_timeslots(target.id)
        dated = await self.scheduler.get_user_timeslots(target.id)
        tz = self.scheduler.resolve_timezone(timezone_str) if timezone_str else None
        slots = self._slot_lines(dated, weekly, tz)

        async with self.db.get_session() as session:
            player = await self._load_player(session, target)
            if not player:
                await interaction.followup.send(
                    f"{target.mention} is not registered.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title=f"Player: {player.name}", color=discord.Color.blurple()
            )
            embed.add_field(name="Discord ID", value=f"`{player.discord_id}`")
            embed.add_field(name="Country", value=(player.country or "—").upper())
            embed.add_field(name="Timezone", value=timezone_str or "—")

            if season_num:
                ps = await PlayerSeasonRepository(session).get(
                    player_id=player.id, season_number=season_num
                )
                if ps:
                    if ps.swiss_seed and ps.swiss_seed > 0:
                        record = f"Skipped Open (seed #{ps.swiss_seed})"
                    else:
                        record = f"{ps.open_wins} - {ps.open_losses} (GD {ps.open_gd:+d})"
                    embed.add_field(
                        name=f"Season {season_num}",
                        value=(
                            f"**Open:** {record}\n"
                            f"**HP:** {ps.hp}\n"
                            f"**Playin Rank:** {ps.playin_rank or '—'}"
                        ),
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name=f"Season {season_num}",
                        value="Not registered for this season",
                        inline=False,
                    )

            result = await session.execute(
                text(
                    "SELECT COUNT(*), SUM(is_winner) FROM match_players "
                    "WHERE player_id = :pid"
                ),
                {"pid": player.id},
            )
            total, wins = result.first() or (0, 0)
            embed.add_field(
                name="All-Time",
                value=f"**Matches:** {total or 0}\n**Wins:** {wins or 0}",
                inline=False,
            )

        embed.add_field(
            name=f"Availability ({len(slots)})",
            value="\n".join(slots)[:1024] if slots else "No timeslots",
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── identity ──────────────────────────────────────────────

    @player.command(name="set", description="Edit a player's name, country or timezone")
    @app_commands.describe(
        target="The player to edit",
        name="New display name (3-50 characters)",
        country="Country name or 2-letter code, or 'none' to clear",
        timezone="IANA timezone, e.g. Europe/Berlin",
    )
    @app_commands.autocomplete(
        country=country_choices, timezone=timezone_autocomplete
    )
    @require_host_app()
    async def set_cmd(
        self,
        interaction: discord.Interaction,
        target: discord.User,
        name: Optional[str] = None,
        country: Optional[str] = None,
        timezone: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if name is None and country is None and timezone is None:
            await interaction.followup.send(
                "Nothing to change — pass at least one of name, country or timezone.",
                ephemeral=True,
            )
            return

        async with self.db.get_session() as session:
            player = await self._load_player(session, target)
            if not player:
                await interaction.followup.send(
                    f"{target.mention} is not registered.", ephemeral=True
                )
                return
            before = (player.name, player.country)
            player_id = player.id

        changes: list[str] = []
        errors: list[str] = []

        if name is not None:
            ok, msg = await self.signup_service.edit_name(
                target.id, name, force=True
            )
            if ok:
                changes.append(f"name: `{before[0]}` → `{name}`")
            else:
                errors.append(msg)

        if country is not None:
            clearing = _is_clear(country)
            code = None if clearing else resolve_country(country)
            if code is None and not clearing:
                errors.append(f"Unknown country `{country}`.")
            else:
                async with self.db.get_session() as session:
                    await PlayerRepository(session).update(player_id, country=code)
                    await session.commit()
                changes.append(
                    f"country: `{(before[1] or '—').upper()}` → "
                    f"`{code.upper() if code else '—'}`"
                )

        if timezone is not None:
            if self.scheduler.resolve_timezone(timezone) is None:
                errors.append(f"`{timezone}` is not a valid IANA timezone.")
            else:
                await self.scheduler.set_timezone(target.id, timezone)
                changes.append(f"timezone: `{timezone}`")

        lines = []
        if changes:
            lines.append(f"Updated {target.mention}:")
            lines.extend(f"• {change}" for change in changes)
        if errors:
            lines.append("**Errors:**")
            lines.extend(f"• {error}" for error in errors)
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ── stats ─────────────────────────────────────────────────

    @player.command(
        name="stats", description="Edit a player's season stats (0 clears seed/rank)"
    )
    @app_commands.describe(
        target="The player to edit",
        seed="Swiss seed (skips Open); 0 clears",
        playin_rank="Playin rank; 0 clears",
        hp="Open HP",
        open_wins="Open wins",
        open_losses="Open losses",
        open_gd="Open goal difference",
    )
    @require_host_app()
    async def stats_cmd(
        self,
        interaction: discord.Interaction,
        target: discord.User,
        seed: Optional[int] = None,
        playin_rank: Optional[int] = None,
        hp: Optional[int] = None,
        open_wins: Optional[int] = None,
        open_losses: Optional[int] = None,
        open_gd: Optional[int] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        supplied = {
            "swiss_seed": seed,
            "playin_rank": playin_rank,
            "hp": hp,
            "open_wins": open_wins,
            "open_losses": open_losses,
            "open_gd": open_gd,
        }
        if all(value is None for value in supplied.values()):
            await interaction.followup.send(
                "Nothing to change — pass at least one stat.", ephemeral=True
            )
            return

        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send("No active season.", ephemeral=True)
            return

        async with self.db.get_session() as session:
            player = await self._load_player(session, target)
            if not player:
                await interaction.followup.send(
                    f"{target.mention} is not registered.", ephemeral=True
                )
                return

            ps_repo = PlayerSeasonRepository(session)
            ps = await ps_repo.get(player_id=player.id, season_number=season_num)
            if not ps:
                ps = await ps_repo.create(
                    player_id=player.id, season_number=season_num
                )

            changes = []
            for field, value in supplied.items():
                if value is None:
                    continue
                # A zero seed or rank means "not seeded / not ranked".
                clears = value == 0 and field in ("swiss_seed", "playin_rank")
                new_value = None if clears else value
                setattr(ps, field, new_value)
                shown = "—" if new_value is None else new_value
                changes.append(f"• {field}: `{shown}`")

            session.add(ps)
            await session.commit()

        await interaction.followup.send(
            f"Updated {target.mention} for season {season_num}:\n"
            + "\n".join(changes),
            ephemeral=True,
        )

    # ── availability ──────────────────────────────────────────

    @player.command(
        name="add_weekly", description="Add a recurring weekly slot for a player"
    )
    @app_commands.describe(
        target="The player to edit",
        weekday="Weekday name or number (1 = Monday)",
        start="Start time in the player's timezone, e.g. 19:00",
        finish="Finish time in the player's timezone, e.g. 21:30",
    )
    @app_commands.autocomplete(start=time_autocomplete, finish=time_autocomplete)
    @require_host_app()
    async def add_weekly_cmd(
        self,
        interaction: discord.Interaction,
        target: discord.User,
        weekday: str,
        start: str,
        finish: str,
    ):
        await interaction.response.defer(ephemeral=True)
        weekday_index = parse_weekday(weekday)
        if weekday_index is None:
            await interaction.followup.send(
                f"Unknown weekday `{weekday}`.", ephemeral=True
            )
            return

        start_parsed = parse_hhmm(start)
        finish_parsed = parse_hhmm(finish)
        if start_parsed is None or finish_parsed is None:
            await interaction.followup.send(
                f"Could not read `{start}`–`{finish}` as times.", ephemeral=True
            )
            return

        start_minute = start_parsed[0] * 60 + start_parsed[1]
        finish_minute = finish_parsed[0] * 60 + finish_parsed[1]
        try:
            await self.scheduler.add_weekly_timeslot(
                target.id, weekday_index, start_minute, finish_minute
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        timezone_str = await self.scheduler.get_timezone(target.id)
        await interaction.followup.send(
            f"Added **{WEEKDAYS[weekday_index]}** "
            f"{_format_minutes(start_minute)}–{_format_minutes(finish_minute)} "
            f"for {target.mention}"
            + (f" ({timezone_str})." if timezone_str else "."),
            ephemeral=True,
        )

    @player.command(
        name="add_date", description="Add a one-off slot for a player"
    )
    @app_commands.describe(
        target="The player to edit",
        day="Day of month",
        start_time="24h format: 19:45, 19 45 or 1945",
        finish_time="24h format: 19:45, 19 45 or 1945",
        preferred="Mark as a preferred timeslot",
    )
    @app_commands.autocomplete(
        day=day_autocomplete,
        start_time=time_autocomplete,
        finish_time=time_autocomplete,
    )
    @require_host_app()
    async def add_date_cmd(
        self,
        interaction: discord.Interaction,
        target: discord.User,
        day: int,
        start_time: str,
        finish_time: str,
        preferred: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)
        tz = await self._target_tz(target)
        if tz is None:
            await interaction.followup.send(
                f"{target.mention} has no valid timezone — set one first "
                "with `/player set`.",
                ephemeral=True,
            )
            return

        start = parse_hhmm(start_time)
        finish = parse_hhmm(finish_time)
        if start is None or finish is None:
            await interaction.followup.send(
                f"Could not read `{start_time}`–`{finish_time}` as times.",
                ephemeral=True,
            )
            return

        hour_start, minute_start = start
        hour_finish, minute_finish = finish
        now = datetime.now(tz)
        prospective_start = resolve_local_datetime(
            tz, now, day, hour_start, minute_start
        )
        if prospective_start is None:
            await interaction.followup.send(
                f"Invalid time {day} {hour_start}:{minute_start}", ephemeral=True
            )
            return

        if (hour_start, minute_start) < (hour_finish, minute_finish):
            delta = timedelta(
                hours=hour_finish - hour_start,
                minutes=minute_finish - minute_start,
            )
        else:
            delta = timedelta(
                days=1,
                hours=hour_finish - hour_start,
                minutes=minute_finish - minute_start,
            )
        prospective_finish = prospective_start + delta

        await self.scheduler.add_timeslot(
            target.id,
            int(prospective_start.timestamp()),
            int(prospective_finish.timestamp()),
            1 if preferred else 0,
        )
        await interaction.followup.send(
            f"Added <t:{int(prospective_start.timestamp())}:F> to "
            f"<t:{int(prospective_finish.timestamp())}:F> for {target.mention}.",
            ephemeral=True,
        )

    @player.command(name="remove_slot", description="Remove one of a player's slots")
    @app_commands.describe(
        target="The player to edit", slot="The slot to remove"
    )
    @app_commands.autocomplete(slot=slot_autocomplete)
    @require_host_app()
    async def remove_slot_cmd(
        self, interaction: discord.Interaction, target: discord.User, slot: str
    ):
        await interaction.response.defer(ephemeral=True)
        kind, _, raw_id = slot.partition(":")
        if kind not in ("date", "weekly") or not raw_id.isdigit():
            await interaction.followup.send(
                "Pick a slot from the autocomplete list.", ephemeral=True
            )
            return

        slot_id = int(raw_id)
        if kind == "weekly":
            await self.scheduler.remove_weekly_timeslot(target.id, slot_id)
        else:
            await self.scheduler.remove_timeslot_by_id(target.id, slot_id)

        await interaction.followup.send(
            f"Removed {kind} slot `{slot_id}` for {target.mention}.",
            ephemeral=True,
        )

    @player.command(name="clear_slots", description="Delete all of a player's slots")
    @app_commands.describe(target="The player to edit")
    @require_host_app()
    async def clear_slots_cmd(
        self, interaction: discord.Interaction, target: discord.User
    ):
        await interaction.response.defer(ephemeral=True)
        weekly = await self.scheduler.get_weekly_timeslots(target.id)
        dated = await self.scheduler.get_user_timeslots(target.id)
        total = len(weekly) + len(dated)
        if total == 0:
            await interaction.followup.send(
                f"{target.mention} has no timeslots.", ephemeral=True
            )
            return

        answer = await confirm(
            interaction,
            f"Delete all {total} timeslots for {target.mention}?",
        )
        if not answer:
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return

        await self.scheduler.clear_timeslots(target.id)
        for slot in weekly:
            await self.scheduler.remove_weekly_timeslot(target.id, slot.id)

        await interaction.followup.send(
            f"Deleted {total} timeslots for {target.mention}.", ephemeral=True
        )

    # ── matches ───────────────────────────────────────────────

    async def _season_or_reply(self, interaction: discord.Interaction):
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send("No active season.", ephemeral=True)
        return season_num

    @player.command(
        name="remove_from_matches",
        description="Detach a player from their uncompleted matches",
    )
    @app_commands.describe(target="The player to detach")
    @require_host_app()
    async def remove_from_matches_cmd(
        self, interaction: discord.Interaction, target: discord.User
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = await self._season_or_reply(interaction)
        if not season_num:
            return
        answer = await confirm(
            interaction,
            f"Detach {target.mention} from all uncompleted matches? "
            "The matches stay but lose their player.",
        )
        if not answer:
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return

        msg = await self.match_service.remove_player_from_matches(
            target.id, season_num
        )
        if interaction.guild:
            await sync_player_threads(interaction.guild, target.id, add=False)
        await interaction.followup.send(msg, ephemeral=True)

    @player.command(
        name="delete_matches",
        description="Delete a player's uncompleted Open/Playin matches",
    )
    @app_commands.describe(target="The player whose matches to delete")
    @require_host_app()
    async def delete_matches_cmd(
        self, interaction: discord.Interaction, target: discord.User
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = await self._season_or_reply(interaction)
        if not season_num:
            return
        answer = await confirm(
            interaction,
            f"Delete all uncompleted Open/Playin matches for {target.mention}?",
        )
        if not answer:
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return

        msg = await self.match_service.delete_player_matches(
            target.id, season_num
        )
        if interaction.guild:
            await sync_player_threads(interaction.guild, target.id, add=False)
        await interaction.followup.send(msg, ephemeral=True)

    @player.command(
        name="forfeit_all", description="Forfeit all of a player's uncompleted matches"
    )
    @app_commands.describe(target="The player to forfeit for")
    @require_host_app()
    async def forfeit_all_cmd(
        self, interaction: discord.Interaction, target: discord.User
    ):
        await interaction.response.defer(ephemeral=True)
        answer = await confirm(
            interaction,
            f"Forfeit every uncompleted match for {target.mention}? "
            "Opponents get a 1-0 win.",
        )
        if not answer:
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return

        msg = await self.match_service.forfeit_all_player_matches(target.id)
        await interaction.followup.send(msg, ephemeral=True)

    @player.command(
        name="substitute", description="Replace a player in all uncompleted matches"
    )
    @app_commands.describe(old="The player being replaced", new="The replacement")
    @require_host_app()
    async def substitute_cmd(
        self,
        interaction: discord.Interaction,
        old: discord.User,
        new: discord.User,
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = await self._season_or_reply(interaction)
        if not season_num:
            return
        answer = await confirm(
            interaction,
            f"Replace {old.mention} with {new.mention} in all uncompleted matches?",
        )
        if not answer:
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return

        msg = await self.match_service.substitute_player(
            old.id, new.id, season_num
        )
        if interaction.guild:
            await sync_player_threads(interaction.guild, old.id, add=False)
            await sync_player_threads(interaction.guild, new.id, add=True)
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(PlayerCog(bot))
