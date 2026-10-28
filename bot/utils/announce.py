"""Public stage announcements and the bot's status line."""

import logging
import discord

from bot.config import Config
from bot.services.season import next_state
from bot.utils.embeds import (
    BLUE,
    GOLD,
    GREEN,
    ORANGE,
    PURPLE,
    card,
    season_track,
    subtext,
)

logger = logging.getLogger(__name__)

STAGE_LABELS = {
    "OFFSEASON": "Offseason",
    "SIGNUP": "Signup",
    "OPEN": "Open",
    "PLAYIN": "Playins",
    "SWISS": "Swiss",
    "PLAYOFF": "Playoffs",
}

STAGE_ANNOUNCEMENTS = {
    "SIGNUP": {
        "title": "📝 Signups are open",
        "description": (
            "A brand-new **PBLC** season is on the way, and we'd love to have "
            "you in it. Grab your spot before the bracket locks!"
        ),
        "color": BLUE,
        "actions": [
            ("✍️ Join", "`/signup`"),
            ("🌍 Timezone", "`/set_timezone`"),
            ("🕒 Availability", "`/add_timeslot`"),
        ],
        "tip": "Setting your timezone early makes scheduling much easier for everyone.",
    },
    "OPEN": {
        "title": "🎲 The Open stage is live",
        "description": (
            "Signups are closed and the first matches are out. Everyone gets "
            "**four games** to show what they've got. Good luck!"
        ),
        "color": ORANGE,
        "actions": [
            ("📊 Standings", "`/open`"),
            ("🗓️ Schedule", "`/offer_time`"),
            ("🎮 Report", "`/report`"),
        ],
        "tip": "Not sure when your opponent is free? `/check_times` shows the overlap.",
    },
    "PLAYIN": {
        "title": "⚔️ Playins are live",
        "description": (
            "The Open stage is done, and now it's the climb. Win your playin "
            "matches to earn a place in **Swiss**."
        ),
        "color": PURPLE,
        "actions": [
            ("🪜 Ladder", "`/playins`"),
            ("🗓️ Schedule", "`/offer_time`"),
            ("🎮 Report", "`/report`"),
        ],
        "tip": "New rounds open up on their own as soon as the current one wraps.",
    },
    "SWISS": {
        "title": "🇨🇭 Swiss is live",
        "description": (
            "Thirty-two players remain. Each round pairs you with someone on "
            "the same record, so every game counts."
        ),
        "color": GREEN,
        "actions": [
            ("📊 Bracket", "`/swiss`"),
            ("🗓️ Schedule", "`/offer_time`"),
            ("🎮 Report", "`/report`"),
        ],
        "tip": "Look at the bracket after each round to find your next opponent.",
    },
    "PLAYOFF": {
        "title": "🏆 Playoffs are live",
        "description": (
            "This is it. The double-elimination bracket is set and only one "
            "player walks away with the title."
        ),
        "color": GOLD,
        "actions": [
            ("🏆 Bracket", "`/playoff`"),
            ("🗓️ Schedule", "`/offer_time`"),
            ("🎮 Report", "`/report`"),
        ],
        "tip": "Every series is best of three. Report each game as soon as it ends.",
    },
}


def stage_announcement_embed(state: str) -> discord.Embed | None:
    """Build the public card for a season-stage change."""
    state = (state or "").upper()
    meta = STAGE_ANNOUNCEMENTS.get(state)
    if meta is None:
        return None

    embed = card(
        meta["title"],
        f"{meta['description']}\n\n{subtext(season_track(state))}",
        color=meta["color"],
        section="Season stage update",
    )
    embed.set_author(name=f"{STAGE_LABELS[state]} stage")
    for name, value in meta["actions"]:
        embed.add_field(name=name, value=value, inline=True)
    embed.add_field(name="💡 Good to know", value=meta["tip"], inline=False)
    if upcoming := next_state(state):
        embed.add_field(
            name="⏭️ Up next",
            value=STAGE_LABELS.get(upcoming, upcoming),
            inline=True,
        )
    return embed


def season_close_embed(
    *, season: int, champion: str, runner_up: str | None = None
) -> discord.Embed:
    """Build the public season-over card."""
    lines = [f"# 👑 {champion}", f"is the **Season {season}** champion! 🎉"]
    if runner_up:
        lines.append(f"\n🥈 Runner-up: **{runner_up}**, what a run to the final.")
    lines.append(
        "\nThanks to everyone who played. You made this season what it was."
    )
    embed = card(
        f"🏆 Season {season} is over",
        "\n".join(lines),
        color=GOLD,
        section="Season recap",
    )
    embed.add_field(
        name="📜 Full recap",
        value="`/season_summary`",
        inline=True,
    )
    embed.add_field(
        name="🔜 Next season",
        value=f"Season {season + 1} signups will be posted here.",
        inline=True,
    )
    return embed


async def announce(
    bot: discord.Client,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
) -> None:
    """Post to `announcements_channel` if configured; failures are logged."""
    channel_id = await Config().get_channel_id("announcements_channel")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.HTTPException, AttributeError):
            channel = None
    if channel is None:
        logger.warning("announcements_channel %s not found", channel_id)
        return

    payload = {}
    if content:
        payload["content"] = content
    if embed is not None:
        payload["embed"] = embed
    if not payload:
        return
    try:
        await channel.send(**payload)
    except discord.HTTPException:
        logger.warning("Could not post announcement", exc_info=True)


async def announce_stage(bot: discord.Client, state: str) -> None:
    if embed := stage_announcement_embed(state):
        await announce(bot, embed=embed)
    await refresh_presence(bot)


async def refresh_presence(bot: discord.Client) -> None:
    state = await Config().get_season_state()
    activity_name = "/signup" if state in ("OFFSEASON", "SIGNUP") else "/report"
    try:
        await bot.change_presence(activity=discord.Game(name=activity_name))
    except (discord.HTTPException, AttributeError):
        logger.warning("Could not set presence", exc_info=True)
