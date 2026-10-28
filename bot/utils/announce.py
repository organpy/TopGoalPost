"""Public stage announcements and the bot's status line."""

import logging
from datetime import datetime, timezone

import discord

from bot.config import Config
from bot.services.season import next_state

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
        "description": "A new **PBLC** season is taking entries.",
        "color": discord.Color.from_rgb(52, 152, 219),
        "action": "Use `/signup` to join the tournament.",
        "extra": (
            "Set your timezone with `/set_timezone` so opponents can schedule "
            "with you."
        ),
    },
    "OPEN": {
        "title": "🎲 Open stage is live",
        "description": "The **Open** stage has begun. Every player has four matches to play.",
        "color": discord.Color.from_rgb(230, 126, 34),
        "action": (
            "Use `/open` for standings, `/offer_time` to schedule, and "
            "`/report` when a match is finished."
        ),
        "extra": "Use `/check_times` to find shared availability with an opponent.",
    },
    "PLAYIN": {
        "title": "⚔️ Playins are live",
        "description": "The **Playin** ladder is set. Score matches to climb toward Swiss.",
        "color": discord.Color.from_rgb(155, 89, 182),
        "action": (
            "Use `/playins` for the ladder, `/offer_time` to schedule, and "
            "`/report` when a match is finished."
        ),
        "extra": "Later rounds appear automatically as the current round finishes.",
    },
    "SWISS": {
        "title": "🇨🇭 Swiss is live",
        "description": "The **Swiss** stage is underway. Every round is paired from the current standings.",
        "color": discord.Color.from_rgb(46, 204, 113),
        "action": (
            "Use `/swiss` for the bracket, `/offer_time` to schedule, and "
            "`/report` when a match is finished."
        ),
        "extra": "Check the bracket after each round to see your next opponent.",
    },
    "PLAYOFF": {
        "title": "🏆 Playoffs are live",
        "description": "The **Playoff** double-elimination bracket is set.",
        "color": discord.Color.from_rgb(241, 196, 15),
        "action": (
            "Use `/playoff` for the bracket, `/offer_time` to schedule, and "
            "`/report` when a series game is finished."
        ),
        "extra": "Playoff series are best-of-three; report each game as it ends.",
    },
}


def stage_announcement_embed(state: str) -> discord.Embed | None:
    """Build the public card for a season-stage change."""
    meta = STAGE_ANNOUNCEMENTS.get((state or "").upper())
    if meta is None:
        return None

    embed = discord.Embed(
        title=meta["title"],
        description=meta["description"],
        color=meta["color"],
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="What to do", value=meta["action"], inline=False)
    if extra := meta.get("extra"):
        embed.add_field(name="Good to know", value=extra, inline=False)
    if upcoming := next_state(state):
        embed.add_field(
            name="Next stage",
            value=STAGE_LABELS.get(upcoming, upcoming),
            inline=True,
        )
    embed.set_footer(text="TopGoalPost · Season stage update")
    return embed


def season_close_embed(
    *, season: int, champion: str, runner_up: str | None = None
) -> discord.Embed:
    """Build the public season-over card."""
    embed = discord.Embed(
        title=f"🏆 Season {season} is over",
        color=discord.Color.from_rgb(241, 196, 15),
        timestamp=datetime.now(timezone.utc),
    )
    lines = [f"**Champion:** **{champion}**"]
    if runner_up:
        lines.append(f"**Runner-up:** **{runner_up}**")
    embed.description = "\n".join(lines)
    embed.add_field(
        name="Full recap",
        value="Use `/season_summary` to see the season in full.",
        inline=False,
    )
    embed.add_field(
        name="Next season",
        value=f"Hosts can start Season {season + 1} with `/season create {season + 1}`.",
        inline=False,
    )
    embed.set_footer(text="TopGoalPost · Season recap")
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
