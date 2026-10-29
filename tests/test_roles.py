"""Signup/stage role assignment: who gets which role, with minimal API calls."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.config import Config
from bot.db.models import Match, MatchPlayer
from bot.utils import roles as roles_module
from bot.utils.roles import role_problem, set_member_role, sync_role


class FakeRole:
    def __init__(self, role_id, position=1, managed=False, default=False):
        self.id = role_id
        self.position = position
        self.managed = managed
        self._default = default
        self.members = []
        self.mention = f"<@&{role_id}>"

    def is_default(self):
        return self._default

    def __ge__(self, other):
        return self.position >= other.position

    def __hash__(self):
        return self.id

    def __eq__(self, other):
        return isinstance(other, FakeRole) and other.id == self.id


EVERYONE = FakeRole(1, position=0, default=True)


def make_member(member_id, *roles, guild=None):
    member = SimpleNamespace(id=member_id, mention=f"<@{member_id}>", guild=guild)
    member.roles = [EVERYONE, *roles]
    for role in roles:
        role.members.append(member)

    async def edit(roles, reason):
        member.roles = [EVERYONE, *roles]

    member.edit = AsyncMock(side_effect=edit)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    return member


def make_guild(*members):
    by_id = {m.id: m for m in members}
    guild = MagicMock()
    guild.get_member = MagicMock(side_effect=by_id.get)
    guild.fetch_member = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "unknown member")
    )
    return guild


class TestSyncRole:
    async def test_swaps_roles_in_one_edit_and_skips_correct_members(self):
        open_role, swiss = FakeRole(10), FakeRole(30)
        moving = make_member(1, open_role)          # Open -> Swiss
        already = make_member(2, swiss)             # already correct
        eliminated = make_member(3, open_role)      # not in Swiss
        newcomer = make_member(4)                   # no role yet
        guild = make_guild(moving, already, eliminated, newcomer)

        with patch.object(roles_module.asyncio, "sleep", AsyncMock()) as sleep:
            result = await sync_role(
                guild, swiss, {1, 2, 4, 99}, strip=[open_role, swiss], reason="t"
            )

        assert moving.roles == [EVERYONE, swiss]
        assert eliminated.roles == [EVERYONE]
        assert newcomer.roles == [EVERYONE, swiss]
        already.edit.assert_not_awaited()
        assert moving.edit.await_count == 1
        assert (result.added, result.removed, result.unchanged) == (2, 2, 1)
        assert result.missing == [99]
        # Paced: a pause between each of the 3 edits, none before the first.
        assert sleep.await_count == 2

    async def test_never_sends_the_everyone_role(self):
        swiss = FakeRole(30)
        member = make_member(1)
        guild = make_guild(member)

        await sync_role(guild, swiss, {1}, reason="t", delay=0)

        sent = member.edit.await_args.kwargs["roles"]
        assert EVERYONE not in sent and swiss in sent

    async def test_records_failures_and_continues(self):
        swiss = FakeRole(30)
        blocked, fine = make_member(1), make_member(2)
        blocked.edit = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "no")
        )
        guild = make_guild(blocked, fine)

        result = await sync_role(guild, swiss, {1, 2}, reason="t", delay=0)

        assert result.added == 1
        assert result.failed == ["<@1>: Forbidden"]

    async def test_no_role_just_strips(self):
        open_role = FakeRole(10)
        holder = make_member(1, open_role)
        guild = make_guild(holder)

        result = await sync_role(guild, None, {1}, strip=[open_role], reason="t", delay=0)

        assert holder.roles == [EVERYONE]
        assert result.removed == 1


def test_role_problem_detects_hierarchy_and_permissions():
    guild = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = FakeRole(5, position=5)

    assert role_problem(guild, FakeRole(10, position=2)) is None
    assert "above the bot" in role_problem(guild, FakeRole(10, position=5))
    assert "integration" in role_problem(guild, FakeRole(10, managed=True))
    guild.me.guild_permissions.manage_roles = False
    assert "Manage Roles" in role_problem(guild, FakeRole(10, position=2))


async def test_set_member_role_adds_and_removes(db):
    role = FakeRole(77)
    await Config().set("signup_role", "77")
    guild = MagicMock()
    guild.get_role = MagicMock(return_value=role)
    member = make_member(1, guild=guild)

    assert await set_member_role(member, "signup_role", True, "t")
    member.add_roles.assert_awaited_once_with(role, reason="t")

    member.roles.append(role)
    assert await set_member_role(member, "signup_role", False, "t")
    member.remove_roles.assert_awaited_once_with(role, reason="t")


async def test_stage_discord_ids_includes_finished_matches(db, seed_data, open_event):
    from bot.services.tournament_flow import TournamentFlowService

    players = seed_data["players"]
    async with db.get_session() as session:
        done = Match(event_id=open_event.id, completed_at=datetime.utcnow())
        pending = Match(event_id=open_event.id)
        session.add_all([done, pending])
        await session.flush()
        session.add_all([
            MatchPlayer(match_id=done.id, player_id=players[0].id, side=0),
            MatchPlayer(match_id=done.id, player_id=players[1].id, side=1),
            MatchPlayer(match_id=pending.id, player_id=players[2].id, side=0),
            MatchPlayer(match_id=pending.id, player_id=None, side=1),
        ])
        await session.commit()

    flow = TournamentFlowService()
    assert await flow.stage_discord_ids(1, "OPEN") == {1000, 1001, 1002}
    assert await flow.stage_discord_ids(1, "SIGNUP") == {1000 + i for i in range(8)}
    assert await flow.stage_discord_ids(1, "SWISS") is None


class TestGiveRolesCommand:
    def _ctx(self):
        ctx = MagicMock()
        ctx.author.id = 9
        ctx.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))
        return ctx

    async def test_refuses_when_bot_cannot_manage_role(self, db):
        from bot.cogs.admin import admin_cog as module

        ctx = self._ctx()
        cog = module.AdminCog(MagicMock())
        with patch.object(module, "get_configured_role", AsyncMock(return_value=FakeRole(30))), \
             patch.object(module, "stage_roles", AsyncMock(return_value=[])), \
             patch.object(module, "role_problem", return_value="bot role too low"), \
             patch.object(module, "sync_role", AsyncMock()) as sync:
            await cog.giveroles_cmd.callback(cog, ctx, "swiss")

        sync.assert_not_awaited()
        assert ctx.send.await_args.args[0] == "bot role too low"

    async def test_syncs_named_stage_for_all_participants(self, db):
        from bot.cogs.admin import admin_cog as module
        from bot.utils.roles import RoleSyncResult

        ctx = self._ctx()
        cog = module.AdminCog(MagicMock())
        cog.season_service.get_current_season_number = AsyncMock(return_value=1)
        swiss, open_role = FakeRole(30), FakeRole(10)
        flow = MagicMock(stage_discord_ids=AsyncMock(return_value={1, 2}))
        with patch.object(module, "get_configured_role", AsyncMock(return_value=swiss)), \
             patch.object(module, "stage_roles", AsyncMock(return_value=[open_role, swiss])), \
             patch.object(module, "role_problem", return_value=None), \
             patch.object(module, "TournamentFlowService", return_value=flow), \
             patch.object(module, "sync_role", AsyncMock(return_value=RoleSyncResult(added=2))) as sync:
            await cog.giveroles_cmd.callback(cog, ctx, "Swiss")

        flow.stage_discord_ids.assert_awaited_once_with(1, "SWISS")
        args, kwargs = sync.await_args
        assert args[1:] == (swiss, {1, 2})
        assert kwargs["strip"] == [open_role, swiss]
        final = ctx.send.return_value.edit.await_args.kwargs["content"]
        assert "synced for SWISS" in final and "**2**" in final

    async def test_unknown_stage_lists_options(self, db):
        from bot.cogs.admin.admin_cog import AdminCog

        ctx = self._ctx()
        cog = AdminCog(MagicMock())
        await cog.giveroles_cmd.callback(cog, ctx, "finals")
        assert "signup, open, playin, swiss, playoff" in ctx.send.await_args.args[0]
