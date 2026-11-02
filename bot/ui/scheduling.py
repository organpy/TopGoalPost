"""Scheduling-related UI components (views)."""

from datetime import datetime, timedelta

import discord

from bot.utils.content_filter import find_vulgarity
from bot.utils.embeds import BLUE, GREEN, GREY, ORANGE, RED, card, quote, subtext, time_block
from bot.utils.emojis import get as emoji
from bot.utils.errors import ReportingView
from bot.utils.rate_limit import COUNTER_OFFER_LIMITER


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

OFFER_COLOR = ORANGE
COUNTER_COLOR = BLUE
CONFIRMED_COLOR = GREEN
CANCELLED_COLOR = RED
EXPIRED_COLOR = GREY

# Matches MatchConfirmView's timeout, so the countdown on the DM is honest.
OFFER_TIMEOUT_SECONDS = 840

OFFER_NEXT_STEPS = {
    "confirmed": "`/report` the score after the match.",
    "cancelled": "Send another time with `/offer_time`.",
    "expired": "Send a new offer with `/offer_time`.",
    "limit": "Send a new offer with `/offer_time`.",
}


def _safe_note(note: str | None) -> str | None:
    """Last-resort display guard for any note that reaches an embed."""
    if note and find_vulgarity(note):
        return "Message hidden by the safety filter."
    return note


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


def _mention(user) -> str:
    return f"<@{getattr(user, 'id', 0)}>"


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
    title = "🔁 Counter-offer" if counter else f"{emoji('OFFER')} Match time offer"
    action = (
        "suggested a different time:"
        if counter
        else "wants to play at:"
    )
    expires = int(datetime.now().timestamp()) + OFFER_TIMEOUT_SECONDS
    embed = card(
        title,
        (
            f"**{_display_name(proposer)}** {action}\n"
            f"{time_block(when)}"
        ),
        color=COUNTER_COLOR if counter else OFFER_COLOR,
        section=_match_value(match_id),
        timestamp=False,
    )
    embed.set_author(name=_display_name(proposer), icon_url=_avatar_url(proposer))
    if note:
        embed.add_field(name="Message", value=quote(_safe_note(note)), inline=False)
    embed.add_field(
        name="\u200b",
        value=(
            "**Confirm** if it works, **Counter offer** to suggest another "
            f"time, or **Cancel**.\n{subtext(f'This offer expires <t:{expires}:R>')}"
        ),
        inline=False,
    )
    return embed


def offer_sent_embed(
    *,
    recipient_id: int,
    when: datetime,
    match_id: int | None,
) -> discord.Embed:
    """Small ephemeral receipt for the player who sent the offer."""
    return card(
        "📨 Offer sent",
        (
            f"Sent to <@{recipient_id}>.\n"
            f"{time_block(when)}"
        ),
        color=OFFER_COLOR,
        section=_match_value(match_id),
        timestamp=False,
    )


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
        "expired": ("⌛ Offer expired", EXPIRED_COLOR, "didn't settle on a time"),
        "countered": ("🔁 Counter-offer sent", COUNTER_COLOR, "sent a counter-offer"),
        "limit": ("🛑 Counter-offer limit reached", EXPIRED_COLOR, "went back and forth too many times"),
    }
    title, color, verb = states.get(status, states["cancelled"])
    if status == "countered":
        headline = f"{_mention(player_b)} suggested a new time to {_mention(player_a)}."
    else:
        headline = f"{_mention(player_a)} vs {_mention(player_b)}: {verb}."
    embed = card(
        title,
        f"{headline}\n{time_block(when)}",
        color=color,
        section=_match_value(match_id),
        timestamp=False,
    )
    if note:
        embed.add_field(name="Message", value=quote(_safe_note(note)), inline=False)
    if next_step := OFFER_NEXT_STEPS.get(status):
        embed.description += "\n" + subtext(next_step)
    return embed


def match_channel_embed(
    *,
    player_a,
    player_b,
    when: datetime,
    match_id: int | None,
) -> discord.Embed:
    """Public confirmation posted in the configured match channel/thread."""
    embed = card(
        f"{emoji('SCHEDULE')} Match scheduled",
        (
            f"## {_mention(player_a)} vs {_mention(player_b)}\n"
            f"{time_block(when)}\n"
            + subtext("`/report` the score after the match")
        ),
        color=CONFIRMED_COLOR,
        section=_match_value(match_id),
        timestamp=False,
    )
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
        if self.view.value is not None or self.view.counter_submitted:
            await interaction.response.send_message(
                "This offer was already answered.", ephemeral=True
            )
            return

        note = self.note.value.strip() or None
        if note and find_vulgarity(note):
            await interaction.response.send_message(
                "Please keep counter-offer messages respectful. "
                "That message contains language that isn't allowed.",
                ephemeral=True,
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

        retry_after = COUNTER_OFFER_LIMITER.consume(interaction.user.id)
        if retry_after:
            await interaction.response.send_message(
                "You're sending counter-offers too quickly. "
                f"Try again in {retry_after}s.",
                ephemeral=True,
            )
            return

        self.view.counter_submitted = True
        self.view.value = "counter"
        self.view.counter_time = when
        self.view.counter_note = note
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
        super().__init__(timeout=OFFER_TIMEOUT_SECONDS)
        self.value: bool | str | None = None
        self.target = target
        self.counter_time: datetime | None = None
        self.counter_note: str | None = None
        self.counter_submitted = False

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
        if self.value is not None or self.counter_submitted:
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
