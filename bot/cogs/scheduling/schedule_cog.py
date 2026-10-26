"""Scheduling and timezone cog.

set_timezone, timestamp, add_timeslot (weekly), add_date_timeslot (one-off),
check_times, remove_timeslot and offer_time.
"""

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, available_timezones

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.checks import _is_host
from bot.config import Config
from bot.db import get_db_manager, PlayerRepository
from bot.rendering import ASSETS_ROOT
from bot.services.match import MatchService
from bot.services.scheduler import SchedulerService, WEEKDAYS
from bot.services.season import SeasonService
from bot.ui.scheduling import MatchConfirmView, TimestampView, WeeklyTimeslotModal

TIMEZONE_LIST_URL = "https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568"

COMMON_TIMEZONES = (
    "UTC",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Paris",
    "Europe/Moscow",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
)


def parse_hhmm(text: str) -> Optional[tuple[int, int]]:
    """Parse loose 24h input ('19:45', '19 45', '1945') into (hour, minute)."""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 1 or len(digits) > 4:
        return None
    if len(digits) < 3:
        return int(digits), 0
    return int(digits[:-2]), int(digits[-2:])


def resolve_local_datetime(
    tz: ZoneInfo, now: datetime, day: int, hour: int, minute: int
) -> Optional[datetime]:
    """Resolve day/hour/minute in the user's timezone, rolling to next month
    if the day already passed."""
    month = now.month
    year = now.year
    if day < now.day:
        month += 1
    if month > 12:
        month -= 12
        year += 1
    try:
        return datetime(year=year, month=month, day=day, hour=hour, minute=minute, tzinfo=tz)
    except ValueError:
        return None


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"

def target_discord_id(interaction: discord.Interaction) -> int:
    """Resolve a `/player` target option, falling back to the invoking user.

    Discord fills options top-down, so `target` is populated whenever the
    autocomplete belongs to an option declared after it.
    """
    target = getattr(getattr(interaction, "namespace", None), "target", None)
    discord_id = getattr(target, "id", None)
    return discord_id if isinstance(discord_id, int) else interaction.user.id


async def timezone_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest valid IANA timezones."""
    query = current.strip()
    if not query:
        return [app_commands.Choice(name=zone, value=zone) for zone in COMMON_TIMEZONES]

    lowered = query.lower()
    matches = [
        zone
        for zone in COMMON_TIMEZONES
        if lowered in zone.lower()
    ]
    matches.extend(
        sorted(
            zone
            for zone in available_timezones()
            if lowered in zone.lower() and zone not in COMMON_TIMEZONES
        )
    )
    return [app_commands.Choice(name=zone, value=zone) for zone in matches[:25]]


async def day_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest the next 14 days in the target's timezone."""
    timezone_str = await SchedulerService().get_timezone(
        target_discord_id(interaction)
    )
    tz = SchedulerService.resolve_timezone(timezone_str) if timezone_str else None
    now = datetime.now(tz) if tz else datetime.now()
    choices: list[app_commands.Choice[str]] = []
    for offset in range(14):
        day = now + timedelta(days=offset)
        label = f"{day.day} ({day.strftime('%a')})"
        if current in label or current == str(day.day):
            choices.append(app_commands.Choice(name=label, value=str(day.day)))
    return choices[:25]


async def time_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest half-hour times."""
    choices = []
    for hour in range(24):
        for minute in (0, 30):
            value = f"{hour:02d}:{minute:02d}"
            if current in value:
                choices.append(app_commands.Choice(name=value, value=value))
    return choices[:25]


async def slot_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest dated and weekly slots for the caller, or for a `/player` target."""
    scheduler = SchedulerService()
    discord_id = target_discord_id(interaction)
    choices: list[app_commands.Choice[str]] = []
    for slot in await scheduler.get_user_timeslots(discord_id):
        label = (
            f"{datetime.fromtimestamp(slot.start).strftime('%b %d %H:%M')}"
            f"–{datetime.fromtimestamp(slot.finish).strftime('%H:%M')}"
        )
        if slot.prio:
            label += " (preferred)"
        if current.lower() in label.lower():
            choices.append(
                app_commands.Choice(name=label[:100], value=f"date:{slot.id}")
            )
    for slot in await scheduler.get_weekly_timeslots(discord_id):
        label = (
            f"{WEEKDAYS[slot.weekday]} "
            f"{_format_minutes(slot.start_minute)}–{_format_minutes(slot.finish_minute)}"
        )
        if current.lower() in label.lower():
            choices.append(
                app_commands.Choice(name=label[:100], value=f"weekly:{slot.id}")
            )
    return choices[:25]


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = get_db_manager()
        self.scheduler = SchedulerService()
        self.season_service = SeasonService()

    async def _reply(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        deferred: bool = False,
    ) -> None:
        """Reply ephemerally, respecting an earlier `interaction.response.defer()`."""
        if deferred:
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)

    async def _get_user_tz(
        self, interaction: discord.Interaction, *, deferred: bool = False
    ) -> Optional[ZoneInfo]:
        """Fetch and validate the invoking user's timezone, replying on failure."""
        timezone_str = await self.scheduler.get_timezone(interaction.user.id)
        if not timezone_str:
            await self._reply(
                interaction,
                "You need to set your timezone with /set_timezone!",
                deferred=deferred,
            )
            return None
        tz = self.scheduler.resolve_timezone(timezone_str)
        if tz is None:
            admin_id = await Config().get_int("admin_id")
            contact = f"<@{admin_id}>" if admin_id else "an admin"
            await self._reply(
                interaction,
                f"Your timezone is glitched, contact {contact}",
                deferred=deferred,
            )
            return None
        return tz

    @app_commands.command(name="set_timezone", description="Set your timezone")
    @app_commands.describe(timezone="e.g. UTC, Europe/Berlin, America/New_York")
    @app_commands.autocomplete(timezone=timezone_autocomplete)
    async def set_timezone(self, interaction: discord.Interaction, timezone: str):
        try:
            pfp_dir = ASSETS_ROOT / "pfps"
            pfp_dir.mkdir(parents=True, exist_ok=True)
            await interaction.user.display_avatar.save(fp=str(pfp_dir / f"{interaction.user.id}.png"))
        except Exception:
            pass

        tz = self.scheduler.resolve_timezone(timezone)
        if tz is None:
            await interaction.response.send_message(
                f'Invalid timezone "{timezone}", list of available timezones: {TIMEZONE_LIST_URL}',
                ephemeral=True,
            )
            return

        await self.scheduler.set_timezone(interaction.user.id, timezone)
        await interaction.response.send_message(
            f'Timezone set: "{timezone}", current time: '
            f"{datetime.now(tz).strftime('%Y.%m.%d %H:%M:%S %Z %z')}",
            ephemeral=True,
        )

    @app_commands.command(
        name="timestamp",
        description="Get a copyable timestamp that adjusts to other users' timezones",
    )
    @app_commands.describe(day="Day of month", hour="Hour (0-23)", minute="Minute (0-59)")
    async def timestamp(self, interaction: discord.Interaction, day: int, hour: int, minute: int):
        tz = await self._get_user_tz(interaction)
        if tz is None:
            return

        now = datetime.now(tz)
        prospective = resolve_local_datetime(tz, now, day, hour, minute)
        if prospective is None:
            await interaction.response.send_message(
                f"Invalid time {day} {hour}:{minute}", ephemeral=True
            )
            return

        view = TimestampView(time=prospective)
        await interaction.response.send_message(
            content=view.update_text(),
            ephemeral=True,
            view=view,
        )

    @app_commands.command(
        name="add_timeslot",
        description="Add your recurring weekly availability",
    )
    async def add_timeslot(self, interaction: discord.Interaction):
        await interaction.response.send_modal(WeeklyTimeslotModal())

    @app_commands.command(
        name="add_date_timeslot",
        description="Add a one-off availability window for a specific date",
    )
    @app_commands.describe(
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
    async def add_date_timeslot(
        self,
        interaction: discord.Interaction,
        day: int,
        start_time: str,
        finish_time: str,
        preferred: bool = False,
    ):
        tz = await self._get_user_tz(interaction)
        if tz is None:
            return

        start = parse_hhmm(start_time)
        if start is None:
            await interaction.response.send_message(
                f"Could not recognize start time: {start_time}", ephemeral=True
            )
            return
        finish = parse_hhmm(finish_time)
        if finish is None:
            await interaction.response.send_message(
                f"Could not recognize finish time: {finish_time}", ephemeral=True
            )
            return

        hour_start, minute_start = start
        hour_finish, minute_finish = finish

        now = datetime.now(tz)
        prospective_start = resolve_local_datetime(tz, now, day, hour_start, minute_start)
        if prospective_start is None:
            await interaction.response.send_message(
                f"Invalid time {day} {hour_start}:{minute_start}", ephemeral=True
            )
            return

        if (hour_start, minute_start) < (hour_finish, minute_finish):
            delta = timedelta(hours=hour_finish - hour_start, minutes=minute_finish - minute_start)
        else:
            delta = timedelta(days=1, hours=hour_finish - hour_start, minutes=minute_finish - minute_start)
        prospective_finish = prospective_start + delta

        await self.scheduler.add_timeslot(
            interaction.user.id,
            int(prospective_start.timestamp()),
            int(prospective_finish.timestamp()),
            1 if preferred else 0,
        )
        await interaction.response.send_message(
            content=(
                f"Success: <t:{int(prospective_start.timestamp())}:F> to "
                f"<t:{int(prospective_finish.timestamp())}:F>"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="check_times", description="Check available times for another player")
    @app_commands.describe(target="The player to check")
    async def check_times(self, interaction: discord.Interaction, target: discord.Member):
        dated = await self.scheduler.get_user_timeslots(target.id)
        weekly = await self.scheduler.get_weekly_timeslots(target.id)
        if not dated and not weekly:
            await interaction.response.send_message(
                f"{target.name} did not set any times yet", ephemeral=True
            )
            return

        lines = [f"Available times for {target.name}:"]
        if dated:
            lines.append("**One-off slots**")
            for timeslot in dated:
                bold = "**" if timeslot.prio else ""
                lines.append(
                    f"{bold}<t:{timeslot.start}:F> to <t:{timeslot.finish}:F>{bold}"
                )
        if weekly:
            lines.append("**Weekly availability**")
            for slot in weekly:
                lines.append(
                    f"{WEEKDAYS[slot.weekday]} "
                    f"{_format_minutes(slot.start_minute)}–"
                    f"{_format_minutes(slot.finish_minute)}"
                )
        if not await self.scheduler.get_timezone(target.id):
            lines.append("_(timezone not set)_")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="remove_timeslot", description="Remove one of your timeslots")
    @app_commands.describe(slot="Slot to remove (pick from the list)")
    @app_commands.autocomplete(slot=slot_autocomplete)
    async def remove_timeslot(self, interaction: discord.Interaction, slot: str):
        kind, _, raw_id = slot.partition(":")
        if not raw_id.isdigit():
            await interaction.response.send_message(
                "Pick a slot from the list.", ephemeral=True
            )
            return

        slot_id = int(raw_id)
        if kind == "date":
            await self.scheduler.remove_timeslot_by_id(interaction.user.id, slot_id)
        elif kind == "weekly":
            await self.scheduler.remove_weekly_timeslot(interaction.user.id, slot_id)
        else:
            await interaction.response.send_message(
                "Pick a slot from the list.", ephemeral=True
            )
            return

        await interaction.response.send_message("Timeslot removed.", ephemeral=True)

    async def offer_match_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """The caller's active matches, narrowed to the chosen opponent if any."""
        matches = await self._caller_matches(interaction.user.id)
        target = getattr(interaction.namespace, "target", None)
        target_name = getattr(target, "display_name", None) or getattr(target, "name", None)
        choices = []
        for m in matches:
            if target_name and target_name.lower() not in m["label"].lower():
                continue
            if current.lower() in m["label"].lower():
                choices.append(app_commands.Choice(name=m["label"][:100], value=str(m["match_id"])))
        return choices[:25]

    async def _caller_matches(self, discord_id: int) -> list[dict]:
        season = await self.season_service.get_current_season_number()
        if not season:
            return []
        async with self.db.get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(discord_id)
        if not player:
            return []
        return await MatchService().active_matches_for_player(player.id, season)

    async def _overlap_hint(self, a: int, b: int) -> str:
        """Up to three shared one-off availability windows, as a reply hint."""
        now = int(datetime.now().timestamp())
        overlaps = [
            (start, finish)
            for start, finish in await self.scheduler.get_overlapping_slots(a, b)
            if finish > now
        ][:3]
        if not overlaps:
            return ""
        windows = ", ".join(f"<t:{s}:f>–<t:{f}:t>" for s, f in overlaps)
        return f"\nShared availability: {windows}"

    @app_commands.command(name="offer_time", description="Offer a match time to another player")
    @app_commands.describe(
        target="Your opponent (admins may target anyone)",
        day="Day of month",
        time="24h format: 19:45, 19 45 or 1945",
        text="Optional message for the target (admins only)",
        match="Which match this time is for (defaults to your match with the target)",
    )
    @app_commands.autocomplete(
        day=day_autocomplete, time=time_autocomplete, match=offer_match_autocomplete
    )
    async def offer_time(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        day: int,
        time: str,
        text: Optional[str] = None,
        match: Optional[str] = None,
    ):
        # This command does DB lookups plus a DM send before it can answer, so
        # acknowledge immediately and answer through the followup webhook.
        await interaction.response.defer(ephemeral=True)

        is_host = await _is_host(interaction.user, interaction.guild)
        if text and not is_host:
            await self._reply(
                interaction,
                "Only admins can include a custom message.",
                deferred=True,
            )
            return
        if text and len(text) > 500:
            await self._reply(
                interaction,
                "Message must be 500 characters or fewer.",
                deferred=True,
            )
            return
        if target.bot:
            await self._reply(
                interaction, "You cannot offer a time to a bot.", deferred=True
            )
            return

        tz = await self._get_user_tz(interaction, deferred=True)
        if tz is None:
            return

        parsed = parse_hhmm(time)
        if parsed is None:
            await self._reply(
                interaction,
                f"Could not recognize start time: {time}",
                deferred=True,
            )
            return
        hour_start, minute_start = parsed

        now = datetime.now(tz)
        prospective_start = resolve_local_datetime(tz, now, day, hour_start, minute_start)
        if prospective_start is None:
            await self._reply(
                interaction,
                f"Invalid time {day} {hour_start}:{minute_start}",
                deferred=True,
            )
            return

        async with self.db.get_session() as session:
            repo = PlayerRepository(session)
            player = await repo.get_by_discord_id(interaction.user.id)
            opponent = await repo.get_by_discord_id(target.id)

        match_id = None
        if match:
            if not match.isdigit():
                await self._reply(interaction, "Pick a match from the list.", deferred=True)
                return
            match_id = int(match)
            participants = await self.scheduler.match_participants(match_id)
            if sorted(participants) != sorted({interaction.user.id, target.id}) and not (
                is_host and target.id in participants
            ):
                await self._reply(
                    interaction,
                    f"Match {match_id} is not between you and <@{target.id}>.",
                    deferred=True,
                )
                return
        elif player and opponent:
            season = await self.season_service.get_current_season_number()
            shared = None
            if season:
                shared = await self.scheduler.find_shared_match(
                    player.id, opponent.id, season
                )
            match_id = shared.id if shared else None

        if not is_host:
            if not player or not opponent:
                await self._reply(
                    interaction,
                    "Player not found in the tournament.",
                    deferred=True,
                )
                return
            if match_id is None:
                await self._reply(
                    interaction, "You don't have a game to schedule!", deferred=True
                )
                return

        timestamp = int(prospective_start.timestamp())
        view = MatchConfirmView(target=target.id)
        content = (
            f"<@{interaction.user.id}> wants to play your game"
            f"{f' (Match {match_id})' if match_id else ''} at <t:{timestamp}:F>"
        )
        if text:
            content += f"\n\n{text}"
        try:
            msg = await target.send(content=content, view=view)
        except discord.HTTPException:
            await self._reply(
                interaction,
                f"Could not send a DM to <@{target.id}> (DMs disabled?).",
                deferred=True,
            )
            return

        match_note = f" for Match {match_id}" if match_id else ""
        await self._reply(
            interaction,
            f"Offered time{match_note}: <t:{timestamp}:F>"
            + await self._overlap_hint(interaction.user.id, target.id),
            deferred=True,
        )

        await view.wait()
        if view.value is None:
            try:
                await msg.edit(
                    content=(
                        f"Offer timed out: game at <t:{timestamp}:F> against "
                        f"<@{interaction.user.id}>."
                    ),
                    view=None,
                )
                await interaction.user.send(
                    content=f"<@{target.id}> didn't respond to <t:{timestamp}:F>"
                )
            except discord.HTTPException:
                pass
        elif view.value:
            if match_id:
                await self.scheduler.set_match_time(
                    match_id, prospective_start, interaction.user.id
                )
            try:
                await msg.edit(
                    content=f"Confirmed: game at <t:{timestamp}:F> against <@{interaction.user.id}>.",
                    view=None,
                )
                await interaction.user.send(
                    content=f"<@{target.id}> accepted <t:{timestamp}:F>"
                )
            except discord.HTTPException:
                pass
            cfg = Config()
            channel_id = await cfg.get_channel_id("agreements_channel")
            channel = self.bot.get_channel(channel_id) if channel_id else interaction.channel
            if channel is not None:
                try:
                    await channel.send(
                        f"<@{interaction.user.id}> and <@{target.id}> agreed to play"
                        f"{f' Match {match_id}' if match_id else ''} at <t:{timestamp}:F>"
                    )
                except discord.HTTPException:
                    pass
        else:
            try:
                await msg.edit(
                    content=f"Denied: game at <t:{timestamp}:F> against <@{interaction.user.id}>.",
                    view=None,
                )
                await interaction.user.send(
                    content=f"<@{target.id}> rejected <t:{timestamp}:F>"
                )
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ScheduleCog(bot))
