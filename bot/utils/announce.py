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
        "description": (
            "A brand-new **PBLC** season is on the way, and we'd love to have "
            "you in it. Grab your spot before the bracket locks!"
        ),
        "color": discord.Color.from_rgb(52, 152, 219),
        "action": "Type `/signup` to join. It takes about ten seconds.",
        "extra": (
            "Set your timezone with `/set_timezone` so your opponents know "
            "when you're around."
        ),
    },
    "OPEN": {
        "title": "🎲 Open stage is live",
        "description": (
            "Signups are closed and the first matches are out. Everyone gets "
            "**four games** to show what they've got. Good luck!"
        ),
        "color": discord.Color.from_rgb(230, 126, 34),
        "action": (
            "Check where you stand with `/open`, pick a time with your "
            "opponent using `/offer_time`, and `/report` the score once you've "
            "played."
        ),
        "extra": "Not sure when your opponent is free? `/check_times` shows the overlap.",
    },
    "PLAYIN": {
        "title": "⚔️ Playins are live",
        "description": (
            "The Open stage is done, and now it's the climb. Win your playin "
            "matches to earn a place in **Swiss**."
        ),
        "color": discord.Color.from_rgb(155, 89, 182),
        "action": (
            "See the ladder with `/playins`, set up your match with "
            "`/offer_time`, and `/report` the result when you're done."
        ),
        "extra": "New rounds open up on their own as soon as the current one wraps.",
    },
    "SWISS": {
        "title": "🇨🇭 Swiss is live",
        "description": (
            "Thirty-two players remain. Each round pairs you with someone on "
            "the same record, so every game counts."
        ),
        "color": discord.Color.from_rgb(46, 204, 113),
        "action": (
            "Follow the bracket with `/swiss`, agree a time using "
            "`/offer_time`, and `/report` your score after the match."
        ),
        "extra": "Look at the bracket after each round to find your next opponent.",
    },
    "PLAYOFF": {
        "title": "🏆 Playoffs are live",
        "description": (
            "This is it. The double-elimination bracket is set and only one "
            "player walks away with the title."
        ),
        "color": discord.Color.from_rgb(241, 196, 15),
        "action": (
            "Find your series with `/playoff`, lock in a time using "
            "`/offer_time`, and `/report` each game as soon as it ends."
        ),
        "extra": "Every series is best of three, so one bad game won't knock you out.",
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
    lines = [f"Congratulations to **{champion}**, our Season {season} champion! 🎉"]
    if runner_up:
        lines.append(f"Huge respect to **{runner_up}** for a great run to the final.")
    lines.append("Thanks to everyone who played. You made this season what it was.")
    embed.description = "\n\n".join(lines)
    embed.add_field(
        name="Full recap",
        value="Relive the season with `/season_summary`.",
        inline=False,
    )
    embed.add_field(
        name="Next season",
        value=(
            f"Season {season + 1} is coming. Keep an eye on this channel for "
            "signups."
        ),
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
