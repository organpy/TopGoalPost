"""Scheduling group threads: grouping, creation and the unthreaded report."""

from unittest.mock import AsyncMock, MagicMock

from bot.utils.threads import create_scheduling_threads, players_without_thread, split_groups


def test_small_field_is_one_group():
    assert split_groups([1, 2, 3, 4, 5, 6]) == [[1, 2, 3, 4, 5, 6]]


def test_groups_are_deterministic_and_cover_everyone():
    ids = list(range(1, 22))
    groups = split_groups(ids)
    assert groups == split_groups(ids)
    assert len(groups) == 7
    assert {pid for group in groups for pid in group} == set(ids)
    # Each group overlaps the previous one by three players.
    assert groups[1][:3] == groups[0][-3:]


async def test_create_threads_adds_each_group():
    channel = MagicMock()
    thread = MagicMock(add_user=AsyncMock())
    channel.create_thread = AsyncMock(return_value=thread)

    created = await create_scheduling_threads(channel, "OPEN", list(range(1, 13)))

    assert created == 4
    assert channel.create_thread.await_args_list[0].kwargs["name"] == "OPEN scheduling #1"
    assert thread.add_user.await_count == sum(len(g) for g in split_groups(list(range(1, 13))))


async def test_players_without_thread_only_checks_stage_threads():
    in_thread = MagicMock(id=1)
    stage_thread = MagicMock(fetch_members=AsyncMock(return_value=[in_thread]))
    stage_thread.name = "SWISS scheduling #1"
    other = MagicMock(fetch_members=AsyncMock(return_value=[MagicMock(id=2)]))
    other.name = "OPEN scheduling #1"
    channel = MagicMock(threads=[stage_thread, other])

    assert await players_without_thread(channel, "SWISS", [1, 2, 3]) == [2, 3]


async def test_unthreadedgames_reports_missing_stage_threads(db, monkeypatch):
    from bot.cogs.admin.admin_cog import AdminCog
    from bot.config import Config
    from bot.services.tournament_flow import TournamentFlowService

    await Config().set_season_state("OPEN")
    cog = AdminCog(MagicMock())
    cog.season_service.get_current_season_number = AsyncMock(return_value=1)
    monkeypatch.setattr(TournamentFlowService, "unplayed_discord_ids", AsyncMock(return_value=[1, 2]))
    monkeypatch.setattr(
        "bot.utils.threads.get_match_thread_channel", AsyncMock(return_value=MagicMock(threads=[]))
    )
    ctx = MagicMock(send=AsyncMock())

    await cog.unthreadedgames_cmd.callback(cog, ctx)

    assert "OPEN has no scheduling threads yet" in ctx.send.call_args[0][0]
