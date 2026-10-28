"""League custom emojis with standard fallbacks.

Emoji names, IDs, and fallbacks live in ``branding.json``. This module resolves
them against both guild emojis and application-owned emojis, so a missing custom
emoji never breaks an embed.
"""

import logging

import discord

from bot.branding import BRANDING, EmojiSpec

logger = logging.getLogger(__name__)

LeagueEmoji = EmojiSpec

EMOJIS: dict[str, EmojiSpec] = dict(BRANDING.emojis)

_BOUND_BOT: discord.Client | None = None
_APPLICATION_EMOJIS: dict[int, discord.Emoji] = {}


def bind(bot: discord.Client) -> None:
    """Remember the bot whose emoji cache should be checked."""
    global _BOUND_BOT
    _BOUND_BOT = bot


def unbind() -> None:
    """Clear the bound bot (primarily useful in tests)."""
    global _BOUND_BOT
    _BOUND_BOT = None
    _APPLICATION_EMOJIS.clear()


async def load_application_emojis(bot: discord.Client | None = None) -> None:
    """Cache emojis owned by the application, which are not in guild caches."""
    bot = bot or _BOUND_BOT
    if bot is None:
        return

    try:
        emojis = await bot.fetch_application_emojis()
    except (discord.DiscordException, AttributeError) as exc:
        logger.warning("Could not load application emojis: %s", exc)
        return

    _APPLICATION_EMOJIS.clear()
    _APPLICATION_EMOJIS.update({emoji.id: emoji for emoji in emojis})
    if emojis:
        logger.info("Loaded %s application emoji(s).", len(emojis))


def _resolve(spec: EmojiSpec) -> discord.Emoji | None:
    if _BOUND_BOT is None or spec.emoji_id is None:
        return None
    emoji = _BOUND_BOT.get_emoji(spec.emoji_id)
    if emoji is not None:
        return emoji
    return _APPLICATION_EMOJIS.get(spec.emoji_id)


def get(key: str) -> str:
    """Return the custom emoji when available, otherwise its fallback."""
    spec = EMOJIS[key]
    if emoji := _resolve(spec):
        return str(emoji)
    return spec.fallback


def get_url(key: str) -> str | None:
    """Return the emoji image URL for places that do not render emoji markup."""
    spec = EMOJIS[key]
    if emoji := _resolve(spec):
        return str(emoji.url)
    return None
