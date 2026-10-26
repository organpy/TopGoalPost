"""Public stage announcements and the bot's status line."""

import logging

import discord

from bot.config import Config

logger = logging.getLogger(__name__)

STAGE_ANNOUNCEMENTS = {
    "SIGNUP": "📝 Signups are open! Use `/signup` to join.",
    "OPEN": "🎲 The **Open** stage has started. Check `/open` and report scores with `/report`.",
    "PLAYIN": "⚔️ **Playins** have started. See the ladder with `/playins`.",
    "SWISS": "🇨🇭 **Swiss** has started. See the bracket with `/swiss`.",
    "PLAYOFF": "🏆 **Playoffs** have started. See the bracket with `/playoff`.",
}


async def announce(bot: discord.Client, content: str) -> None:
    """Post to `announcements_channel` if configured; failures are logged."""
    channel_id = await Config().get_channel_id("announcements_channel")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        logger.warning("announcements_channel %s not found", channel_id)
        return
    try:
        await channel.send(content)
    except discord.HTTPException:
        logger.warning("Could not post announcement", exc_info=True)


async def announce_stage(bot: discord.Client, state: str) -> None:
    if text := STAGE_ANNOUNCEMENTS.get(state):
        await announce(bot, text)
    await refresh_presence(bot)


async def refresh_presence(bot: discord.Client) -> None:
    state = await Config().get_season_state()
    activity_name = "/signup" if state in ("OFFSEASON", "SIGNUP") else "/report"
    try:
        await bot.change_presence(activity=discord.Game(name=activity_name))
    except (discord.HTTPException, AttributeError):
        logger.warning("Could not set presence", exc_info=True)
