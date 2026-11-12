"""Buttons that let a host pick between previewed stage actions."""

from typing import Any, Awaitable, Callable

import discord

from bot.services.season import SeasonService
from bot.utils.errors import ReportingView


class StageChoiceView(ReportingView):
    """One button per option; the chosen action runs through the cog's
    ``_run_stage`` (roles + announcement), like every other stage action."""

    def __init__(
        self,
        bot,
        author_id: int,
        options: list[tuple[str, discord.ButtonStyle, Callable[..., Awaitable[dict]], tuple, bool]],
    ):
        super().__init__(timeout=300)
        self.bot = bot
        self.author_id = author_id
        for label, style, action, args, enabled in options:
            button = discord.ui.Button(label=label, style=style, disabled=not enabled)
            button.callback = self._make_callback(action, args)
            self.add_item(button)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This choice belongs to someone else.", ephemeral=True
            )
            return False
        return await super().interaction_check(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)

    def _make_callback(self, action, args: tuple):
        async def callback(interaction: discord.Interaction):
            await self._close(interaction)
            season = await SeasonService().get_current_season_number()
            if not season:
                await interaction.followup.send("No active season.", ephemeral=True)
                return
            cog = self.bot.get_cog("TournamentCog")
            if cog:
                result = await cog._run_stage(interaction.guild, action, season, *args)
            else:
                result = await action(season, *args)
            await interaction.followup.send(
                result.get("error") or result["message"], ephemeral=True
            )

        return callback

    async def _cancel(self, interaction: discord.Interaction):
        await self._close(interaction)
        await interaction.followup.send("Cancelled; nothing was created.", ephemeral=True)


def playins_choice(bot, author_id: int, flow, result: dict[str, Any]):
    """(content, embed, view) asking regular ladder vs single-elim redraw."""
    preview = result.get("redraw_preview") or {}
    can_redraw = bool(preview.get("ok"))
    embed = discord.Embed(
        title="Single-elim redraw preview",
        description=(preview.get("message") if can_redraw else
                     f"Not possible as is: {preview.get('error', 'unknown')}\n"
                     "Use `/tourney redraw_playins` with `pull_back` options.")[:4000],
        color=0x2b2d31,
    )
    view = StageChoiceView(
        bot,
        author_id,
        [
            ("Regular anyway", discord.ButtonStyle.primary, flow.start_playins, (True,), True),
            (
                "Single-elim redraw",
                discord.ButtonStyle.danger,
                flow.redraw_playins,
                (2, [], 0, preview.get("signature")),
                can_redraw,
            ),
        ],
    )
    return result["short_text"], embed, view
