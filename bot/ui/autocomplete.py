"""Shared slash-command autocompletes."""

import discord
from discord import app_commands

from bot.services.player_stats import PlayerStatsService


async def season_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """Seasons that actually have tournament data, newest first."""
    seasons = await PlayerStatsService().available_seasons()
    return [
        app_commands.Choice(name=f"Season {number}", value=number)
        for number in seasons
        if current in str(number)
    ][:25]
