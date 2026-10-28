"""Custom PBLC emojis with standard fallbacks.

All custom emoji IDs live here. Embed builders call :func:`get` so a missing
emoji (or a bot that has not bound its cache yet) falls back to the standard
emoji that was used before the custom set was uploaded.
"""

from dataclasses import dataclass

import discord


@dataclass(frozen=True)
class LeagueEmoji:
    name: str
    emoji_id: int
    fallback: str

    @property
    def markup(self) -> str:
        return f"<:{self.name}:{self.emoji_id}>"


EMOJIS = {
    "PBLC": LeagueEmoji("pb_pblc", 1565055569981874397, "🏟️"),
    "SIGNUP": LeagueEmoji("pb_signup", 1565055323645943889, "📝"),
    "OPEN": LeagueEmoji("pb_open", 1565055325189578945, "🎲"),
    "PLAYINS": LeagueEmoji("pb_playins", 1565055326632411138, "⚔️"),
    "SWISS": LeagueEmoji("pb_swiss", 1565055327982977024, "🇨🇭"),
    "PLAYOFFS": LeagueEmoji("pb_playoffs", 1565055541120598026, "🏆"),
    "CHAMPION": LeagueEmoji("pb_champion", 1565055336560201728, "👑"),
    "MATCH": LeagueEmoji("pb_match", 1565055335159566356, "🎮"),
    "REPORT": LeagueEmoji("pb_report", 1565055333368598568, "🎮"),
    # The old "Score sent" receipt used an envelope rather than a controller.
    "REPORT_RECEIPT": LeagueEmoji("pb_report", 1565055333368598568, "📨"),
    "SCHEDULE": LeagueEmoji("pb_schedule", 1565055331799671064, "🗓️"),
    # Same emoji, but the old offer-card fallback was a clock rather than a calendar.
    "OFFER": LeagueEmoji("pb_schedule", 1565055331799671064, "⏰"),
}

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
    if _BOUND_BOT is not None:
        emoji = _BOUND_BOT.get_emoji(spec.emoji_id)
        if emoji is not None:
            return str(emoji)
    return spec.fallback
