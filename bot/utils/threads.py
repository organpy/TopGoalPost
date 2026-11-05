"""Scheduling-thread helpers shared by the tournament cog, dashboard and admin cog.

Scheduling uses *group* threads (`<Stage> · Group N`, ~3 players each with
overlap), not one thread per match. ``Match.thread_id`` is unused. Each thread
opens with a pinned message that mentions its players (which adds them to the
thread in one request) and lists their matchups.
"""

import asyncio
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


def _replace_matchup_label(
    description: str, old_label: str, new_label: str
) -> tuple[str, bool]:
    """Replace a player on one side of a pinned matchups line."""
    lines = []
    changed = False
    for line in description.splitlines():
        left, separator, right = line.partition(" vs ")
        if separator and left == old_label:
            line = f"{new_label}{separator}{right}"
            changed = True
        elif separator and right == old_label:
            line = f"{left}{separator}{new_label}"
            changed = True
        lines.append(line)
    return "\n".join(lines), changed


def _replace_mentioned_player(
    content: str, old_discord_id: int, new_discord_id: int, new_name: str
) -> tuple[str, bool]:
    """Swap the old thread mention for the substitute's mention or name."""
    old_mention = f"<@{old_discord_id}>"
    if old_discord_id <= 0 or old_mention not in content:
        return content, False

    new_label = f"<@{new_discord_id}>" if new_discord_id > 0 else new_name
    parts = content.split()
    if new_discord_id > 0:
        parts = [part for part in parts if part != new_label]
    parts = [new_label if part == old_mention else part for part in parts]
    return " ".join(parts), True


async def _update_pinned_matchups(
    thread: discord.Thread,
    old_discord_id: int,
    old_name: str,
    new_discord_id: int,
    new_name: str,
) -> None:
    """Rewrite the pinned matchup card in one scheduling thread."""
    old_label = _player_label(old_discord_id, old_name)
    new_label = _player_label(new_discord_id, new_name)

    try:
        async for message in thread.pins():
            if not message.author.bot:
                continue
            embed = next(
                (
                    item
                    for item in message.embeds
                    if item.title and item.title.endswith(" matchups")
                ),
                None,
            )
            if embed is None:
                continue

            description = embed.description or ""
            new_description, description_changed = _replace_matchup_label(
                description, old_label, new_label
            )
            content, content_changed = _replace_mentioned_player(
                message.content or "", old_discord_id, new_discord_id, new_name
            )
            if not description_changed and not content_changed:
                continue

            payload = embed.to_dict()
            payload["description"] = new_description[:4096]
            try:
                await message.edit(
                    content=content,
                    embed=discord.Embed.from_dict(payload),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                logger.warning("Could not update matchups in thread %s", thread.id)
    except discord.HTTPException:
        logger.warning("Could not read pinned messages in thread %s", thread.id)


async def replace_player_in_threads(
    guild: discord.Guild,
    old_discord_id: int,
    old_name: str,
    new_discord_id: int,
    new_name: str,
) -> int:
    """Swap a player only in scheduling threads that already contain them.

    The replacement is added before the original is removed, so the thread
    never loses its access to a participant. Pinned matchup cards are rewritten
    in place; other threads are left untouched.
    """
    if old_discord_id <= 0 or old_discord_id == new_discord_id:
        return 0

    channel = await get_match_thread_channel(guild)
    if not channel:
        return 0

    updated = 0
    for thread in channel.threads:
        try:
            members = await thread.fetch_members()
        except discord.HTTPException:
            logger.warning("Could not list members of thread %s", thread.id)
            continue
        member_ids = {member.id for member in members}
        if old_discord_id not in member_ids:
            continue

        if new_discord_id > 0 and new_discord_id not in member_ids:
            try:
                await thread.add_user(discord.Object(new_discord_id))
            except discord.HTTPException:
                logger.warning(
                    "Could not add substitute %s to thread %s",
                    new_discord_id,
                    thread.id,
                    exc_info=True,
                )
                continue

        await _update_pinned_matchups(
            thread,
            old_discord_id,
            old_name,
            new_discord_id,
            new_name,
        )

        if old_discord_id != new_discord_id:
            try:
                await thread.remove_user(discord.Object(old_discord_id))
            except discord.HTTPException:
                logger.warning(
                    "Could not remove replaced player %s from thread %s",
                    old_discord_id,
                    thread.id,
                    exc_info=True,
                )
        updated += 1
    return updated


def split_groups(player_ids: list[int]) -> list[list[int]]:
    """Split players into scheduling groups of ~3 with overlap for connectivity."""
    n = len(player_ids)
    if n <= 6:
        return [player_ids]
    num_groups = n // 3
    k, m = divmod(n, num_groups)
    lengths = [k + (1 if i < m else 0) for i in range(num_groups)]
    result = [player_ids[-3:] + player_ids[:lengths[0]]]
    for i, length in enumerate(lengths[1:], 1):
        start = sum(lengths[:i])
        result.append(player_ids[start - 3:start + length])
    return result


def thread_name(stage: str, number: int) -> str:
    return f"{stage.title()} · Group {number}"


def stage_threads(channel: discord.TextChannel, stage: str) -> list[discord.Thread]:
    # Older seasons used "<STAGE> scheduling #N".
    prefixes = (f"{stage.title()} · Group ", f"{stage} scheduling #")
    return [t for t in channel.threads if t.name.startswith(prefixes)]


# Seconds between threads during bulk creation.
THREAD_CREATE_DELAY = 1.0

Matchup = tuple[tuple[int, str], tuple[int, str]]


def _player_label(discord_id: int, name: str) -> str:
    # Negative IDs are name-only placeholders without a Discord account.
    return f"<@{discord_id}>" if discord_id > 0 else name


def matchups_embed(stage: str, number: int, matchups: list[Matchup]) -> discord.Embed:
    lines = [f"{_player_label(*a)} vs {_player_label(*b)}" for a, b in matchups]
    embed = discord.Embed(
        title=f"{stage.title()} · Group {number} matchups",
        description="\n".join(lines)[:4096] or "No unplayed matches.",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Agree on a time here, then report the score with /report.")
    return embed


async def create_scheduling_threads(
    channel: discord.TextChannel, stage: str, matchups: list[Matchup]
) -> int:
    """Create group threads for ``stage`` with a pinned matchups message.

    Players are added by mentioning them in that message rather than one
    ``add_user`` call each, and threads are created with a short pause, to
    stay clear of Discord rate limits.
    """
    discord_ids = list(dict.fromkeys(p[0] for pair in matchups for p in pair if p[0] > 0))
    created = 0
    for i, group in enumerate(split_groups(discord_ids)):
        if i and THREAD_CREATE_DELAY:
            await asyncio.sleep(THREAD_CREATE_DELAY)
        members = set(group)
        group_matchups = [m for m in matchups if m[0][0] in members or m[1][0] in members]
        try:
            thread = await channel.create_thread(
                name=thread_name(stage, i + 1),
                reason=f"{stage} scheduling group {i + 1}",
            )
            message = await thread.send(
                content=" ".join(f"<@{d}>" for d in group),
                embed=matchups_embed(stage, i + 1, group_matchups),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except discord.HTTPException:
            logger.warning("Could not create %s thread %s", stage, i + 1, exc_info=True)
            continue
        try:
            await message.pin()
        except discord.HTTPException:
            logger.warning("Could not pin matchups in thread %s", thread.id)
        created += 1
    return created


async def cleanse_scheduling_threads(
    channel: discord.TextChannel,
    active_ids: set[int],
    registered_ids: set[int],
    bot_user_id: int,
) -> int:
    """Remove registered players with no unplayed match from every thread."""
    removed = 0
    for thread in channel.threads:
        try:
            members = await thread.fetch_members()
        except discord.HTTPException:
            logger.warning("Could not list members of thread %s", thread.id)
            continue
        for member in members:
            if member.id == bot_user_id or member.id not in registered_ids:
                continue
            if member.id in active_ids:
                continue
            try:
                await thread.remove_user(discord.Object(member.id))
                removed += 1
            except discord.HTTPException:
                logger.warning("Could not remove %s from thread %s", member.id, thread.id)
    return removed


async def players_without_thread(
    channel: discord.TextChannel, stage: str, discord_ids: list[int]
) -> list[int]:
    """Players with unplayed matches who are in none of the stage's threads."""
    threaded: set[int] = set()
    for thread in stage_threads(channel, stage):
        try:
            threaded.update(m.id for m in await thread.fetch_members())
        except discord.HTTPException:
            logger.warning("Could not list members of thread %s", thread.id)
    return [d for d in discord_ids if d not in threaded]
