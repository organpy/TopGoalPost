"""League custom emojis with standard fallbacks.

Emoji names, IDs, and fallbacks live in ``branding.json``. This module resolves
them against the bot cache so a missing custom emoji never breaks an embed.
"""

import discord

from bot.branding import BRANDING, EmojiSpec

LeagueEmoji = EmojiSpec

EMOJIS: dict[str, EmojiSpec] = dict(BRANDING.emojis)

_BOUND_BOT: discord.Client | None = None


def bind(bot: discord.Client) -> None:
    """Remember the bot whose emoji cache should be checked."""
    global _BOUND_BOT
    _BOUND_BOT = bot


def unbind() -> None:
    """Clear the bound bot (primarily useful in tests)."""
    global _BOUND_BOT
    _BOUND_BOT = None


def get(key: str) -> str:
    """Return the custom emoji when available, otherwise its fallback."""
    spec = EMOJIS[key]
    if _BOUND_BOT is not None and spec.emoji_id is not None:
        emoji = _BOUND_BOT.get_emoji(spec.emoji_id)
        if emoji is not None:
            return str(emoji)
    return spec.fallback
