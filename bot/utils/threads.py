"""Match-thread membership helpers shared by the match and admin cogs."""

import logging

import discord

from bot.config import Config

logger = logging.getLogger(__name__)


async def get_match_thread_channel(guild: discord.Guild):
    """Return the configured match-thread parent channel, if any."""
    cid = await Config().get_channel_id("match_channel")
    if not cid:
        return None
    channel = guild.get_channel(cid)
    if channel and isinstance(channel, discord.TextChannel):
        return channel
    return None


async def sync_player_threads(
    guild: discord.Guild, discord_id: int, add: bool = False
) -> None:
    """Add or remove a user from every match channel thread."""
    channel = await get_match_thread_channel(guild)
    if not channel:
        return
    for thread in channel.threads:
        try:
            members = await thread.fetch_members()
            member_ids = [m.id for m in members]
            if add and discord_id not in member_ids:
                await thread.add_user(discord.Object(discord_id))
            elif not add and discord_id in member_ids:
                await thread.remove_user(discord.Object(discord_id))
        except discord.HTTPException:
            logger.warning(
                "Could not %s user %s in thread %s",
                "add" if add else "remove", discord_id, thread.id, exc_info=True,
            )
