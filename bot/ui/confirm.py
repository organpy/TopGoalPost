"""Generic confirm/cancel prompt for destructive admin actions."""

import discord

from bot.utils.errors import ReportingView


class ConfirmActionView(ReportingView):
    """A yes/no prompt; `value` stays None if the prompt times out."""

    def __init__(self, author_id: int, timeout: float = 60):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None

    def _disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _respond(self, interaction: discord.Interaction, value: bool) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This confirmation belongs to someone else.", ephemeral=True
            )
            return
        self.value = value
        self._disable_all()
        self.stop()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        self._disable_all()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._respond(interaction, False)


async def confirm(
    interaction: discord.Interaction, prompt: str
) -> bool | None:
    """Send an ephemeral confirm prompt and wait for the author's answer."""
    view = ConfirmActionView(interaction.user.id)
    await interaction.followup.send(prompt, view=view, ephemeral=True)
    await view.wait()
    return view.value
