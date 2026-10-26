"""Shared permission checks for admin/host commands.

Access is granted to:
- guild administrators (always), or
- members holding any role listed in the `admin_roles` config setting
  (set with: !set_config admin_roles <role_id1,role_id2,...>)
"""

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import Config


async def _is_host(member: Optional[discord.abc.User], guild: Optional[discord.Guild]) -> bool:
    if guild is None or member is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    role_ids = {r.id for r in getattr(member, "roles", []) or []}
    if not role_ids:
        return False
    admin_roles = set(await Config().get_admin_role_ids())
    return bool(role_ids & admin_roles)


async def is_moderator(member: Optional[discord.abc.User], guild: Optional[discord.Guild]) -> bool:
    """Hosts, plus members holding the configured `moderator_role`."""
    if await _is_host(member, guild):
        return True
    mod_role_id = await Config().get_role_id("moderator_role")
    if not mod_role_id:
        return False
    return any(r.id == mod_role_id for r in getattr(member, "roles", []) or [])


async def host_predicate(ctx: commands.Context) -> bool:
    """Prefix-command predicate: administrator or a configured host role."""
    ok = await _is_host(ctx.author, ctx.guild)
    if not ok:
        raise commands.MissingAnyRole(await Config().get_admin_role_ids())
    return True


async def host_predicate_app(interaction: discord.Interaction) -> bool:
    """Slash-command predicate: administrator or a configured host role."""
    ok = await _is_host(interaction.user, interaction.guild)
    if not ok:
        raise app_commands.CheckFailure("Requires an administrator or host role.")
    return True


def require_host():
    return commands.check(host_predicate)


def require_host_app():
    return app_commands.check(host_predicate_app)
