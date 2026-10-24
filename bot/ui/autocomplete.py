"""Shared slash-command autocompletes, backed by the local database."""

import discord
from discord import app_commands
from sqlalchemy import String, cast, or_, select

from bot.db import Player, get_db_manager
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


async def _player_names(current: str) -> list[str]:
    """Registered names matching current, alphabetically, from the local DB.

    Numeric queries match the internal player ID or the Discord ID.
    """
    async with get_db_manager().get_session() as session:
        stmt = select(Player.name).distinct()
        if current:
            if current.isdigit():
                stmt = stmt.where(
                    or_(
                        cast(Player.id, String).like(f"{current}%"),
                        cast(Player.discord_id, String).like(f"{current}%"),
                    )
                )
            else:
                stmt = stmt.where(Player.name.ilike(f"%{current}%"))
        stmt = stmt.order_by(Player.name).limit(25)
        return list((await session.execute(stmt)).scalars())


async def username_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Player names; an empty query lists the first 25."""
    return [
        app_commands.Choice(name=name, value=name)
        for name in await _player_names(current)
    ]


async def player_list_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Comma-separated player names, appending to what is already typed."""
    head, sep, tail = current.rpartition(",")
    prefix = f"{head.strip()}, " if sep and head.strip() else ""
    already = {
        part.strip().lower()
        for part in (head if sep else "").split(",")
        if part.strip()
    }
    return [
        app_commands.Choice(name=f"{prefix}{name}"[:100], value=f"{prefix}{name}"[:100])
        for name in await _player_names(tail.strip())
        if name.lower() not in already
    ]
