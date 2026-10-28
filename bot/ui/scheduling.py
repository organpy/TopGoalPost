"""Scheduling-related UI components (views)."""

from datetime import datetime, timedelta

import discord

from bot.utils.errors import ReportingView


class WeeklyTimeslotModal(discord.ui.Modal, title="Add weekly availability"):
    """Collect a recurring weekday window for the caller."""

    def __init__(self):
        super().__init__()

        self.weekday = discord.ui.TextInput(
            label="Weekday",
            placeholder="e.g. Wednesday or Wed",
            min_length=2,
            max_length=12,
            required=True,
        )
        self.start_time = discord.ui.TextInput(
            label="Start time (your timezone)",
            placeholder="e.g. 19:00",
            min_length=1,
            max_length=5,
            required=True,
        )
        self.finish_time = discord.ui.TextInput(
            label="Finish time (your timezone)",
            placeholder="e.g. 21:30",
            min_length=1,
            max_length=5,
            required=True,
        )
        self.add_item(self.weekday)
        self.add_item(self.start_time)
        self.add_item(self.finish_time)

    async def on_submit(self, interaction: discord.Interaction):
        from bot.cogs.scheduling.schedule_cog import parse_hhmm
        from bot.services.scheduler import SchedulerService, WEEKDAYS, parse_weekday

        weekday = parse_weekday(self.weekday.value)
        if weekday is None:
            await interaction.response.send_message(
                f"Could not recognize weekday: {self.weekday.value}", ephemeral=True
            )
            return

        start = parse_hhmm(self.start_time.value)
        if start is None:
            await interaction.response.send_message(
                f"Could not recognize start time: {self.start_time.value}",
                ephemeral=True,
            )
            return
        finish = parse_hhmm(self.finish_time.value)
        if finish is None:
            await interaction.response.send_message(
                f"Could not recognize finish time: {self.finish_time.value}",
                ephemeral=True,
            )
            return

        start_minute = start[0] * 60 + start[1]
        finish_minute = finish[0] * 60 + finish[1]
        try:
            await SchedulerService().add_weekly_timeslot(
                interaction.user.id, weekday, start_minute, finish_minute
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"Added weekly slot: **{WEEKDAYS[weekday]} "
            f"{_format_minutes(start_minute)}–{_format_minutes(finish_minute)}**.",
            ephemeral=True,
        )


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ── Match time offer embeds ──────────────────────────────────────────

OFFER_COLOR = discord.Color.from_rgb(230, 126, 34)
COUNTER_COLOR = discord.Color.from_rgb(52, 152, 219)
CONFIRMED_COLOR = discord.Color.from_rgb(46, 204, 113)
CANCELLED_COLOR = discord.Color.from_rgb(231, 76, 60)
EXPIRED_COLOR = discord.Color.from_rgb(149, 165, 166)


def _display_name(user) -> str:
    return (
        getattr(user, "display_name", None)
        or getattr(user, "name", None)
        or "Player"
    )


def _avatar_url(user) -> str | None:
    avatar = getattr(user, "display_avatar", None)
    url = getattr(avatar, "url", None)
    return url if isinstance(url, str) else None


def _match_value(match_id: int | None) -> str:
    return f"Match #{match_id}" if match_id else "Open scheduling"


def _time_value(when: datetime) -> str:
    stamp = int(when.timestamp())
    return f"<t:{stamp}:F>\n<t:{stamp}:R>"


def offer_embed(
    *,
    proposer,
    recipient_id: int,
    when: datetime,
    match_id: int | None,
    note: str | None = None,
    counter: bool = False,
) -> discord.Embed:
    """DM card shown to the player who has to answer a time offer."""
    title = "🔁 Counter-offer" if counter else "⏰ Match-time offer"
    action = "proposed a new time for your match" if counter else "wants to schedule your match"
    embed = discord.Embed(
        title=title,
        description=f"<@{recipient_id}>, **{_display_name(proposer)}** {action}.",
        color=COUNTER_COLOR if counter else OFFER_COLOR,
        timestamp=when,
    )
    embed.add_field(name="Match", value=_match_value(match_id), inline=True)
    embed.add_field(name="Proposed by", value=f"<@{getattr(proposer, 'id', 0)}>", inline=True)
    embed.add_field(name="Proposed time", value=_time_value(when), inline=True)
    if note:
        embed.add_field(name="Message", value=note, inline=False)
    avatar = _avatar_url(proposer)
    if avatar:
        embed.set_thumbnail(url=avatar)
    embed.set_footer(text="Confirm, counter, or cancel using the buttons below.")
    return embed


def offer_sent_embed(
    *,
    recipient_id: int,
    when: datetime,
    match_id: int | None,
) -> discord.Embed:
    """Small ephemeral receipt for the player who sent the offer."""
    embed = discord.Embed(
        title="📨 Offer sent",
        description=f"Your time offer was sent to <@{recipient_id}> by DM.",
        color=OFFER_COLOR,
        timestamp=when,
    )
    embed.add_field(name="Match", value=_match_value(match_id), inline=True)
    embed.add_field(name="Proposed time", value=_time_value(when), inline=True)
    embed.set_footer(text="You will get a DM when they answer.")
    return embed


def offer_result_embed(
    *,
    status: str,
    player_a,
    player_b,
    when: datetime,
    match_id: int | None,
    note: str | None = None,
) -> discord.Embed:
    """Shared DM/channel card for an answered or expired offer."""
    states = {
        "confirmed": ("✅ Match time confirmed", CONFIRMED_COLOR, "confirmed"),
        "cancelled": ("❌ Offer cancelled", CANCELLED_COLOR, "cancelled"),
        "expired": ("⌛ Offer expired", EXPIRED_COLOR, "did not answer in time"),
        "countered": ("🔁 Counter-offer sent", COUNTER_COLOR, "sent a counter-offer"),
        "limit": ("🛑 Counter-offer limit reached", EXPIRED_COLOR, "reached the limit"),
    }
    title, color, verb = states.get(status, states["cancelled"])
    if status == "countered":
        description = (
            f"<@{getattr(player_b, 'id', 0)}> sent a counter-offer to "
            f"<@{getattr(player_a, 'id', 0)}>."
        )
    else:
        description = (
            f"<@{getattr(player_a, 'id', 0)}> and "
            f"<@{getattr(player_b, 'id', 0)}> {verb}."
        )
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=when,
    )
    embed.add_field(name="Match", value=_match_value(match_id), inline=True)
    embed.add_field(name="Time", value=_time_value(when), inline=True)
    embed.add_field(
        name="Players",
        value=(
            f"<@{getattr(player_a, 'id', 0)}> · "
            f"<@{getattr(player_b, 'id', 0)}>"
        ),
        inline=False,
    )
    if note:
        embed.add_field(name="Message", value=note, inline=False)
    embed.set_footer(text="TopGoalPost scheduling")
    return embed


def match_channel_embed(
    *,
    player_a,
    player_b,
    when: datetime,
    match_id: int | None,
) -> discord.Embed:
    """Public confirmation posted in the configured match channel/thread."""
    embed = offer_result_embed(
        status="confirmed",
        player_a=player_a,
        player_b=player_b,
        when=when,
        match_id=match_id,
    )
    embed.title = "🗓️ Match scheduled"
    embed.description = (
        f"<@{getattr(player_a, 'id', 0)}> and "
        f"<@{getattr(player_b, 'id', 0)}> agreed on a time. Good luck!"
    )
    embed.set_footer(text="Confirmed via /offer_time")
    return embed


class CounterOfferModal(discord.ui.Modal, title="Counter-offer a match time"):
    """Ask the recipient for a new day/time in their own timezone."""

    def __init__(self, view: "MatchConfirmView"):
        super().__init__()
        self.view = view
        self.day = discord.ui.TextInput(
            label="Day of month",
            placeholder="e.g. 28",
            min_length=1,
            max_length=2,
            required=True,
        )
        self.time = discord.ui.TextInput(
            label="Time (your timezone)",
            placeholder="e.g. 19:45",
            min_length=1,
            max_length=5,
            required=True,
        )
        self.note = discord.ui.TextInput(
            label="Message (optional)",
            placeholder="Anything your opponent should know?",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.day)
        self.add_item(self.time)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != int(self.view.target):
            await interaction.response.send_message(
                "This offer is not for you.", ephemeral=True
            )
            return

        try:
            day = int(self.day.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Day must be a number between 1 and 31.", ephemeral=True
            )
            return
        if not 1 <= day <= 31:
            await interaction.response.send_message(
                "Day must be a number between 1 and 31.", ephemeral=True
            )
            return

        from bot.cogs.scheduling.schedule_cog import parse_hhmm, resolve_local_datetime
        from bot.services.scheduler import SchedulerService

        parsed = parse_hhmm(self.time.value)
        if parsed is None:
            await interaction.response.send_message(
                f"Could not recognize time: {self.time.value}", ephemeral=True
            )
            return
        hour, minute = parsed

        scheduler = SchedulerService()
        timezone_str = await scheduler.get_timezone(interaction.user.id)
        tz = scheduler.resolve_timezone(timezone_str) if timezone_str else None
        if tz is None:
            await interaction.response.send_message(
                "Set your timezone with `/set_timezone` before counter-offering.",
                ephemeral=True,
            )
            return

        now = datetime.now(tz)
        when = resolve_local_datetime(tz, now, day, hour, minute)
        if when is None:
            await interaction.response.send_message(
                f"Invalid time {day} {hour:02d}:{minute:02d}.", ephemeral=True
            )
            return
        if when <= now:
            await interaction.response.send_message(
                "Pick a time in the future.", ephemeral=True
            )
            return

        self.view.value = "counter"
        self.view.counter_time = when
        self.view.counter_note = self.note.value.strip() or None
        self.view._disable_all()
        self.view.stop()
        await interaction.response.send_message(
            "Counter-offer submitted.",
            ephemeral=True,
        )


class TimestampView(ReportingView):
    """Interactive timestamp display with adjust buttons."""

    def __init__(self, time: datetime):
        super().__init__(timeout=None)
        self.time = time
        self.bigmode = 1

    def update_text(self) -> str:
        timestamp = int(self.time.timestamp())
        if self.bigmode:
            return (
                f"<t:{timestamp}:t> `<t:{timestamp}:t>`\n"
                f"<t:{timestamp}:F> `<t:{timestamp}:F>`\n"
                f"<t:{timestamp}:R> `<t:{timestamp}:R>`\n"
            )
        return f"<t:{timestamp}:F>"

    @discord.ui.button(label="-1 Day", style=discord.ButtonStyle.secondary)
    async def minus_one_day(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.time -= timedelta(days=1)
        await interaction.response.edit_message(content=self.update_text())

    @discord.ui.button(label="-1 Hour", style=discord.ButtonStyle.secondary)
    async def minus_one_hour(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.time -= timedelta(hours=1)
        await interaction.response.edit_message(content=self.update_text())

    @discord.ui.button(label="+1 Hour", style=discord.ButtonStyle.secondary)
    async def plus_one_hour(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.time += timedelta(hours=1)
        await interaction.response.edit_message(content=self.update_text())

    @discord.ui.button(label="+1 Day", style=discord.ButtonStyle.secondary)
    async def plus_one_day(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.time += timedelta(days=1)
        await interaction.response.edit_message(content=self.update_text())

    @discord.ui.button(label="Expand/Shorten", style=discord.ButtonStyle.secondary)
    async def expand_shorten(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.bigmode = 1 - self.bigmode
        await interaction.response.edit_message(content=self.update_text())


class MatchConfirmView(ReportingView):
    """DM view letting the opponent confirm, counter, or cancel a time."""

    def __init__(self, target: int):
        super().__init__(timeout=840)
        self.value: bool | str | None = None
        self.target = target
        self.counter_time: datetime | None = None
        self.counter_note: str | None = None

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _respond(
        self, interaction: discord.Interaction, value: bool
    ) -> None:
        """Record the answer and reflect it on the DM.

        Every Discord call is best-effort: the DM may have been deleted and the
        interaction may already be stale, but the waiting `offer_time` command
        still needs `self.value` so it can notify both players.
        """
        if interaction.user.id != int(self.target):
            try:
                await interaction.response.send_message(
                    "Error: you don't have the permission to do that",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return

        if self.value is not None:
            try:
                await interaction.response.send_message(
                    "This offer was already answered.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return

        self.value = value
        self._disable_all()
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            # Stale or already-acknowledged interaction — the answer still counts.
            pass

    async def on_timeout(self) -> None:
        self._disable_all()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, True)

    @discord.ui.button(label="Counter offer", style=discord.ButtonStyle.primary)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != int(self.target):
            try:
                await interaction.response.send_message(
                    "Error: you don't have the permission to do that",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return
        if self.value is not None:
            try:
                await interaction.response.send_message(
                    "This offer was already answered.", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return
        await interaction.response.send_modal(CounterOfferModal(self))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, False)
