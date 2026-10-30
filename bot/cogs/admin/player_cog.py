"""Admin player management.

Inspect and edit any player's identity, availability, season stats and
matches so the database never has to be touched by hand.
"""

from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

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
from bot.ui.autocomplete import season_autocomplete, username_autocomplete
from bot.db import (
    get_db_manager,
    PlayerRepository,
    PlayerSeasonRepository,
)
from bot.services.match import MatchService
from bot.services.player_stats import PlayerStatsService
from bot.services.scheduler import SchedulerService, WEEKDAYS, parse_weekday
from bot.services.season import SeasonService
from bot.services.signup import SignupService
from bot.ui.confirm import confirm
from bot.utils.countries import country_choices, resolve_country
from bot.utils.names import clean_player_name, unsafe_name_reason
from bot.utils.threads import sync_player_threads

CLEAR_TOKENS = {"none", "clear", "-"}


def _is_clear(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in CLEAR_TOKENS


def _safe_user_name(user, fallback: str) -> str:
    """Display name, else username, else fallback — first one that can't ping."""
    for candidate in (
        getattr(user, "display_name", None),
        getattr(user, "name", None),
    ):
        if candidate and not unsafe_name_reason(candidate):
            return candidate
    return fallback


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
        self.stats_service = PlayerStatsService()

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
    @app_commands.describe(
        target="The player to inspect",
        season="Season number (defaults to the current one)",
    )
    @app_commands.autocomplete(season=season_autocomplete)
    @require_host_app()
    async def info_cmd(
        self,
        interaction: discord.Interaction,
        target: discord.User,
        season: int = None,
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = season or await self.season_service.get_current_season_number()

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
            player_id = player.id
            name = player.name
            discord_id = player.discord_id
            country = player.country

        embed = discord.Embed(title=f"Player: {name}", color=discord.Color.blurple())
        embed.add_field(name="Discord ID", value=f"`{discord_id}`")
        embed.add_field(name="Country", value=(country or "—").upper())
        embed.add_field(name="Timezone", value=timezone_str or "—")

        stats = (
            await self.stats_service.get_stats(player_id, season_num)
            if season_num
            else None
        )
        if stats and stats.has_data:
            lines = [
                f"**Open:** {stats.open_summary}",
                f"**Playin:** {stats.record_summary(stats.playin)}",
                f"**Swiss:** {stats.record_summary(stats.swiss)}",
                f"**Playoff:** {stats.record_summary(stats.playoff)}",
            ]
            if stats.playin_rank:
                lines.append(f"**Playin Rank:** #{stats.playin_rank}")
            if stats.hp is not None:
                lines.append(f"**HP:** {stats.hp}")
            if stats.champion:
                lines.append("**Champion**")
            total = stats.total
            lines.append(
                f"**Totals:** {total.games} games · {total.wins}-{total.losses} · "
                f"goals {total.goals_for}-{total.goals_against}"
            )
            embed.add_field(
                name=f"Season {stats.season}", value="\n".join(lines), inline=False
            )
        elif season_num:
            embed.add_field(
                name=f"Season {season_num}",
                value="Not registered for this season",
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
        reset_name="Revert the name to the player's Discord username",
        country="Country name or 2-letter code, or 'none' to clear",
        timezone="IANA timezone or UTC offset, e.g. Europe/Berlin or UTC+1",
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
        reset_name: Optional[bool] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        if name is not None and reset_name:
            await interaction.followup.send(
                "Pass either name or reset_name, not both.", ephemeral=True
            )
            return
        if reset_name:
            name = target.name
        if name is None and country is None and timezone is None:
            await interaction.followup.send(
                "Nothing to change — pass at least one of name, reset_name, "
                "country or timezone.",
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
                changes.append(
                    f"name: `{before[0]}` → `{clean_player_name(name)[0]}`"
                )
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
            normalized = self.scheduler.normalize_timezone(timezone)
            if normalized is None:
                errors.append(
                    f"`{timezone}` is not a valid IANA timezone or UTC offset."
                )
            else:
                timezone = normalized
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

    @staticmethod
    def _clean_player_token(token: str) -> str:
        token = (token or "").strip()
        if token.startswith("<@") and token.endswith(">"):
            token = token[2:-1].lstrip("!")
        return token

    async def _resolve_player_token(self, token: str, season: int):
        return await self.stats_service.resolve_player(
            self._clean_player_token(token), season
        )

    @staticmethod
    def _find_member(guild, token: str):
        if guild is None:
            return None
        try:
            member = guild.get_member_named(token)
        except Exception:
            member = None
        if member:
            return member

        try:
            members = list(getattr(guild, "members", []) or [])
        except TypeError:
            members = []
        wanted = token.lower()
        for member in members:
            names = {
                getattr(member, attr, None)
                for attr in ("name", "display_name", "nick", "global_name")
            }
            if any(
                isinstance(name, str) and name.lower() == wanted
                for name in names
            ):
                return member
        return None

    async def _discord_name(
        self, interaction: discord.Interaction, discord_id: int
    ) -> Optional[str]:
        guild = interaction.guild
        member = guild.get_member(discord_id) if guild else None
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(discord_id)
            except Exception:
                member = None
        if member is not None:
            return _safe_user_name(member, str(discord_id))
        try:
            user = await self.bot.fetch_user(discord_id)
        except Exception:
            return None
        return _safe_user_name(user, str(discord_id))

    async def _prepare_new_player(
        self, interaction: discord.Interaction, token: str, season: int
    ) -> tuple[Optional[object], Optional[str], Optional[int], Optional[str]]:
        """Resolve a substitute or describe the player that must be created."""
        token = self._clean_player_token(token)
        player = await self.stats_service.resolve_player(token, season)
        if player:
            return player, player.name, None, None

        if token.isdigit():
            discord_id = int(token)
            name = await self._discord_name(interaction, discord_id)
            if not name:
                return (
                    None,
                    None,
                    None,
                    f"No Discord user found with ID {token}.",
                )
            return None, name, discord_id, None

        member = self._find_member(interaction.guild, token)
        if member is not None:
            return None, _safe_user_name(member, str(member.id)), member.id, None

        # Not in the DB or the server: create a name-only placeholder.
        error = unsafe_name_reason(token)
        if error:
            return None, None, None, error
        return None, token, None, None

    async def _create_substitute_player(
        self,
        name: str,
        discord_id: Optional[int],
        country: Optional[str] = None,
    ):
        async with self.db.get_session() as session:
            repo = PlayerRepository(session)
            if discord_id is not None:
                existing = await repo.get_by_discord_id(discord_id)
                if existing:
                    if country is not None:
                        existing.country = country
                        await session.commit()
                    return existing
                player = await repo.create(
                    discord_id=discord_id, name=name[:255], country=country
                )
            else:
                player = await repo.create_placeholder(
                    name=name[:255], country=country
                )
            await session.commit()
            return player

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
        season_num = await self._season_or_reply(interaction)
        if not season_num:
            return
        answer = await confirm(
            interaction,
            f"Forfeit every uncompleted match for {target.mention}? "
            "Opponents get a 1-0 win.",
        )
        if not answer:
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return

        msg = await self.match_service.forfeit_all_player_matches(
            target.id, season=season_num
        )
        await interaction.followup.send(msg, ephemeral=True)

    @player.command(
        name="substitute",
        description="Replace a player in the current season (name or ID)",
    )
    @app_commands.describe(
        old="Player being replaced (name, player ID or Discord ID)",
        new="Replacement (name, player ID or Discord ID; may be new)",
        country=(
            "Country for the replacement (name or 2-letter code; "
            "fills a missing country from the old player)"
        ),
    )
    @app_commands.autocomplete(
        old=username_autocomplete,
        new=username_autocomplete,
        country=country_choices,
    )
    @require_host_app()
    async def substitute_cmd(
        self,
        interaction: discord.Interaction,
        old: str,
        new: str,
        country: Optional[str] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = await self._season_or_reply(interaction)
        if not season_num:
            return

        country_code = None
        if country is not None:
            country_code = resolve_country(country)
            if country_code is None:
                await interaction.followup.send(
                    f"Unknown country `{country}`.", ephemeral=True
                )
                return

        old_player = await self._resolve_player_token(old, season_num)
        if not old_player:
            await interaction.followup.send(
                f"No player found matching **{old}**.", ephemeral=True
            )
            return

        (
            new_player,
            new_name,
            new_discord_id,
            error,
        ) = await self._prepare_new_player(interaction, new, season_num)
        if error:
            await interaction.followup.send(error, ephemeral=True)
            return

        new_label = (
            f"**{new_player.name}**"
            if new_player
            else f"**{new_name}** (new player)"
        )
        answer = await confirm(
            interaction,
            f"Replace **{old_player.name}** with {new_label} in season "
            f"{season_num}? Unplayed matches and completed Open results move "
            "to the replacement.",
        )
        if not answer:
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return

        if new_player is None:
            new_player = await self._create_substitute_player(
                new_name, new_discord_id, country_code
            )
        elif country_code and new_player.id != old_player.id:
            async with self.db.get_session() as session:
                await PlayerRepository(session).update(
                    new_player.id, country=country_code
                )
                await session.commit()
            new_player.country = country_code

        msg = await self.match_service.substitute_player(
            old_player.id, new_player.id, season_num
        )
        if interaction.guild:
            if old_player.discord_id > 0:
                await sync_player_threads(
                    interaction.guild, old_player.discord_id, add=False
                )
            if new_player.discord_id > 0:
                await sync_player_threads(
                    interaction.guild, new_player.discord_id, add=True
                )
        await interaction.followup.send(msg, ephemeral=True)

    @player.command(
        name="reset_matches",
        description="Reset a player's completed Open matches (current season)",
    )
    @app_commands.describe(target="Player name, player ID or Discord ID")
    @app_commands.autocomplete(target=username_autocomplete)
    @require_host_app()
    async def reset_matches_cmd(
        self, interaction: discord.Interaction, target: str
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = await self._season_or_reply(interaction)
        if not season_num:
            return

        player = await self._resolve_player_token(target, season_num)
        if not player:
            await interaction.followup.send(
                f"No player found matching **{target}**.", ephemeral=True
            )
            return

        answer = await confirm(
            interaction,
            f"Reset all completed Open matches for **{player.name}** in "
            f"season {season_num}? Scores and winners will be cleared.",
        )
        if not answer:
            await interaction.followup.send("Cancelled.", ephemeral=True)
            return

        msg = await self.match_service.reset_open_matches(
            player.id, season_num
        )
        await interaction.followup.send(msg, ephemeral=True)

    @player.command(
        name="switch_id",
        description="Change a player's Discord ID",
    )
    @app_commands.describe(
        target="Player name, player ID or current Discord ID",
        new_id="New Discord ID",
    )
    @app_commands.autocomplete(target=username_autocomplete)
    @require_host_app()
    async def switch_id_cmd(
        self,
        interaction: discord.Interaction,
        target: str,
        new_id: str,
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = await self._season_or_reply(interaction)
        if not season_num:
            return

        player = await self._resolve_player_token(target, season_num)
        if not player:
            await interaction.followup.send(
                f"No player found matching **{target}**.", ephemeral=True
            )
            return

        cleaned = new_id.strip()
        if not cleaned.isdigit() or int(cleaned) <= 0:
            await interaction.followup.send(
                "New ID must be a positive Discord ID.", ephemeral=True
            )
            return
        new_discord_id = int(cleaned)
        if player.discord_id == new_discord_id:
            await interaction.followup.send(
                f"**{player.name}** already has that Discord ID.",
                ephemeral=True,
            )
            return

        async with self.db.get_session() as session:
            repo = PlayerRepository(session)
            existing = await repo.get_by_discord_id(new_discord_id)
            if existing and existing.id != player.id:
                await interaction.followup.send(
                    f"That Discord ID already belongs to **{existing.name}**.",
                    ephemeral=True,
                )
                return
            old_discord_id = player.discord_id
            await repo.update(player.id, discord_id=new_discord_id)
            await session.commit()

        if interaction.guild:
            if old_discord_id > 0:
                await sync_player_threads(
                    interaction.guild, old_discord_id, add=False
                )
            await sync_player_threads(
                interaction.guild, new_discord_id, add=True
            )
        await interaction.followup.send(
            f"Switched **{player.name}** from Discord ID "
            f"`{old_discord_id}` to `{new_discord_id}`.",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(PlayerCog(bot))
