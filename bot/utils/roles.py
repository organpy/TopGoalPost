"""Signup and stage role assignment.

Shared by `/signup`, the signup button, stage changes and `!giveroles`.
Bulk syncs only call Discord for members whose roles actually change, use
one edit per member, and pause between edits so large stages stay well
under Discord's per-guild member-edit rate limit.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional

import discord

from bot.config import Config

logger = logging.getLogger(__name__)

# Season state -> config key of the role its participants hold.
STATE_ROLE_KEYS = {
    "SIGNUP": "signup_role",
    "OPEN": "open_role",
    "PLAYIN": "playin_role",
    "SWISS": "swiss_role",
    "PLAYOFF": "playoff_role",
}
STAGE_ROLE_KEYS = ("open_role", "playin_role", "swiss_role", "playoff_role")

# Seconds between member edits during a bulk sync.
ROLE_EDIT_DELAY = 0.5
PROGRESS_EVERY = 10


@dataclass
class RoleSyncResult:
    added: int = 0
    removed: int = 0
    unchanged: int = 0
    missing: list[int] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Gave the role to **{self.added}**, removed old roles from "
            f"**{self.removed}**, **{self.unchanged}** already correct."
        ]
        if self.missing:
            lines.append(f"{len(self.missing)} player(s) are not in the server.")
        if self.failed:
            lines.append(f"{len(self.failed)} failed:")
            lines.extend(f"• {line}" for line in self.failed[:10])
        return "\n".join(lines)


async def get_configured_role(
    guild: discord.Guild, key: str
) -> Optional[discord.Role]:
    role_id = await Config().get_role_id(key)
    return guild.get_role(role_id) if role_id else None


async def stage_roles(guild: discord.Guild) -> list[discord.Role]:
    roles = [await get_configured_role(guild, key) for key in STAGE_ROLE_KEYS]
    return [role for role in roles if role is not None]


def role_problem(guild: discord.Guild, role: discord.Role) -> Optional[str]:
    """Why the bot can't manage ``role``, or None when it can."""
    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        return "The bot is missing the Manage Roles permission."
    if role.managed:
        return f"{role.mention} is managed by an integration."
    if role >= me.top_role:
        return (
            f"{role.mention} is above the bot's highest role — drag the bot's "
            "role above it in Server Settings → Roles."
        )
    return None


async def set_member_role(
    member: discord.Member, key: str, present: bool, reason: str
) -> bool:
    """Add or remove one configured role on one member. Never raises."""
    if member.guild is None:
        return False
    role = await get_configured_role(member.guild, key)
    if role is None or (role in member.roles) == present:
        return role is not None
    try:
        if present:
            await member.add_roles(role, reason=reason)
        else:
            await member.remove_roles(role, reason=reason)
        return True
    except discord.HTTPException as e:
        logger.warning("Could not update %s on %s: %s", key, member.id, e)
        return False


async def _resolve_member(
    guild: discord.Guild, discord_id: int
) -> Optional[discord.Member]:
    member = guild.get_member(discord_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(discord_id)
    except discord.NotFound:
        return None


async def sync_role(
    guild: discord.Guild,
    role: Optional[discord.Role],
    eligible_ids: Iterable[int],
    *,
    strip: Iterable[discord.Role] = (),
    reason: str,
    delay: float = ROLE_EDIT_DELAY,
    progress: Optional[Callable[[int, int], Awaitable[None]]] = None,
) -> RoleSyncResult:
    """Make ``role`` (if any) held by exactly ``eligible_ids``.

    Eligible members also lose the ``strip`` roles; everyone else loses
    ``role`` and ``strip``. Each member is edited at most once.
    """
    result = RoleSyncResult()
    eligible = set(eligible_ids) if role is not None else set()
    strip = {r for r in strip if r != role}
    unwanted = strip | ({role} if role is not None else set())

    holders = {m.id: m for r in unwanted for m in r.members}
    targets = sorted(eligible | set(holders))
    edits = 0

    for done, discord_id in enumerate(targets, start=1):
        if progress and done % PROGRESS_EVERY == 0:
            await progress(done, len(targets))
        try:
            member = holders.get(discord_id) or await _resolve_member(guild, discord_id)
        except discord.HTTPException as e:
            result.failed.append(f"<@{discord_id}>: {e.__class__.__name__}")
            continue
        if member is None:
            result.missing.append(discord_id)
            continue

        current = set(member.roles)
        drop = strip if discord_id in eligible else unwanted
        wanted = current - drop
        if discord_id in eligible:
            wanted.add(role)
        if wanted == current:
            result.unchanged += 1
            continue

        if edits and delay:
            await asyncio.sleep(delay)
        edits += 1
        try:
            # One PATCH per member; skip @everyone, which Discord won't accept.
            await member.edit(
                roles=[r for r in wanted if not r.is_default()], reason=reason
            )
        except discord.HTTPException as e:
            result.failed.append(f"{member.mention}: {e.__class__.__name__}")
            continue
        if role is not None and role in wanted and role not in current:
            result.added += 1
        if current - wanted:
            result.removed += 1

    return result
