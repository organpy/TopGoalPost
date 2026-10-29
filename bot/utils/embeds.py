"""Shared look for the bot's embeds: palette, footer, and small layout helpers."""

from datetime import datetime, timezone

import discord

from bot.branding import BRANDING

BRAND = BRANDING.bot_name

BLUE = discord.Color.from_rgb(88, 141, 255)
ORANGE = discord.Color.from_rgb(255, 146, 72)
PURPLE = discord.Color.from_rgb(171, 112, 255)
GREEN = discord.Color.from_rgb(67, 214, 132)
GOLD = discord.Color.from_rgb(255, 196, 64)
RED = discord.Color.from_rgb(255, 92, 92)
GREY = discord.Color.from_rgb(128, 136, 150)

STAGE_TRACK = [
    ("SIGNUP", "Signup"),
    ("OPEN", "Open"),
    ("PLAYIN", "Playins"),
    ("SWISS", "Swiss"),
    ("PLAYOFF", "Playoffs"),
]


def card(
    title: str | None = None,
    description: str | None = None,
    *,
    color: discord.Color,
    section: str | None = None,
    timestamp: datetime | bool = True,
) -> discord.Embed:
    """An embed with the house footer (bot name · section) and timestamp."""
    if timestamp is True:
        timestamp = datetime.now(timezone.utc)
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=timestamp or None,
    )
    embed.set_footer(text=BRANDING.footer_text(section))
    return embed


def subtext(text: str) -> str:
    """Small grey line under the main text."""
    return f"-# {text}"


def quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines() or [""])


def time_block(when: datetime) -> str:
    """Big local time with a relative countdown underneath."""
    stamp = int(when.timestamp())
    return f"### <t:{stamp}:F>\n{subtext(f'<t:{stamp}:R>')}"


def season_track(state: str) -> str:
    """One-line progress bar through the season's stages."""
    keys = [key for key, _ in STAGE_TRACK]
    current = keys.index(state) if state in keys else -1
    parts = []
    for index, (_, label) in enumerate(STAGE_TRACK):
        if index < current:
            parts.append(f"~~{label}~~")
        elif index == current:
            parts.append(f"**▸ {label}**")
        else:
            parts.append(label)
    return " · ".join(parts)
