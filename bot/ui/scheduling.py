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
    """DM view letting the opponent confirm or deny a proposed match time."""

    def __init__(self, target: int):
        super().__init__(timeout=840)
        self.value: bool | None = None
        self.target = target

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

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, False)
