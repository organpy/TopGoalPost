"""Scheduling group threads: grouping, creation and the unthreaded report."""

from unittest.mock import AsyncMock, MagicMock

import discord

from bot.utils import threads
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


async def test_create_threads_pins_matchups_and_mentions_group(monkeypatch):
    monkeypatch.setattr(threads, "THREAD_CREATE_DELAY", 0)
    message = MagicMock(pin=AsyncMock())
    thread = MagicMock(send=AsyncMock(return_value=message), add_user=AsyncMock())
    channel = MagicMock(create_thread=AsyncMock(return_value=thread))
    ids = list(range(1, 13))
    matchups = [((a, f"p{a}"), (a + 1, f"p{a + 1}")) for a in ids[::2]]

    created = await create_scheduling_threads(channel, "OPEN", matchups)

    groups = split_groups(ids)
    assert created == len(groups) == 4
    assert channel.create_thread.await_args_list[0].kwargs["name"] == "Open · Group 1"
    first = thread.send.await_args_list[0].kwargs
    assert first["content"] == " ".join(f"<@{d}>" for d in groups[0])
    assert "<@1> vs <@2>" in first["embed"].description
    assert message.pin.await_count == 4
    thread.add_user.assert_not_awaited()


async def test_replace_player_in_threads_only_updates_threads_with_old_player(monkeypatch):
    message = MagicMock()
    message.author.bot = True
    message.content = "<@10> <@20>"
    message.embeds = [
        discord.Embed(
            title="Open · Group 1 matchups",
            description="<@10> vs <@20>\n<@30> vs <@40>",
        )
    ]
    message.edit = AsyncMock()

    async def pins():
        yield message

    old_thread = MagicMock(id=1)
    old_thread.fetch_members = AsyncMock(
        return_value=[MagicMock(id=10), MagicMock(id=20)]
    )
    old_thread.add_user = AsyncMock()
    old_thread.remove_user = AsyncMock()
    old_thread.pins = pins

    other_thread = MagicMock(id=2)
    other_thread.fetch_members = AsyncMock(return_value=[MagicMock(id=20)])
    other_thread.add_user = AsyncMock()
    other_thread.remove_user = AsyncMock()
    other_thread.pins = pins

    channel = MagicMock(threads=[old_thread, other_thread])
    monkeypatch.setattr(
        threads, "get_match_thread_channel", AsyncMock(return_value=channel)
    )

    updated = await threads.replace_player_in_threads(
        MagicMock(), 10, "Old", 99, "New"
    )

    assert updated == 1
    assert old_thread.add_user.await_args.args[0].id == 99
    assert old_thread.remove_user.await_args.args[0].id == 10
    other_thread.add_user.assert_not_awaited()
    other_thread.remove_user.assert_not_awaited()
    edit = message.edit.await_args.kwargs
    assert edit["content"] == "<@99> <@20>"
    assert edit["embed"].description == "<@99> vs <@20>\n<@30> vs <@40>"


async def test_replace_player_in_threads_updates_card_for_name_only_substitute(
    monkeypatch,
):
    message = MagicMock()
    message.author.bot = True
    message.content = "<@10>"
    message.embeds = [
        discord.Embed(
            title="Open · Group 1 matchups",
            description="<@10> vs <@20>",
        )
    ]
    message.edit = AsyncMock()

    async def pins():
        yield message

    thread = MagicMock(id=1)
    thread.fetch_members = AsyncMock(return_value=[MagicMock(id=10)])
    thread.add_user = AsyncMock()
    thread.remove_user = AsyncMock()
    thread.pins = pins
    channel = MagicMock(threads=[thread])
    monkeypatch.setattr(
        threads, "get_match_thread_channel", AsyncMock(return_value=channel)
    )

    updated = await threads.replace_player_in_threads(
        MagicMock(), 10, "Old", -1, "Ghost"
    )

    assert updated == 1
    thread.add_user.assert_not_awaited()
    assert thread.remove_user.await_args.args[0].id == 10
    edit = message.edit.await_args.kwargs
    assert edit["content"] == "Ghost"
    assert edit["embed"].description == "Ghost vs <@20>"


async def test_players_without_thread_only_checks_stage_threads():
    in_thread = MagicMock(id=1)
    stage_thread = MagicMock(fetch_members=AsyncMock(return_value=[in_thread]))
    stage_thread.name = "SWISS scheduling #1"
    other = MagicMock(fetch_members=AsyncMock(return_value=[MagicMock(id=2)]))
    other.name = "Open · Group 1"
    new_style = MagicMock(fetch_members=AsyncMock(return_value=[MagicMock(id=3)]))
    new_style.name = "Swiss · Group 2"
    channel = MagicMock(threads=[stage_thread, other, new_style])

    assert await players_without_thread(channel, "SWISS", [1, 2, 3]) == [2]


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


async def test_unplayed_matchups_skips_played_and_incomplete(db, seed_data, open_event):
    from datetime import datetime

    from bot.db.models import Match, MatchPlayer
    from bot.services.tournament_flow import TournamentFlowService

    p = seed_data["players"]
    async with db.get_session() as session:
        done = Match(event_id=open_event.id, completed_at=datetime.utcnow())
        pending = Match(event_id=open_event.id)
        tbd = Match(event_id=open_event.id)
        session.add_all([done, pending, tbd])
        await session.flush()
        session.add_all([
            MatchPlayer(match_id=done.id, player_id=p[0].id, side=0),
            MatchPlayer(match_id=done.id, player_id=p[1].id, side=1),
            MatchPlayer(match_id=pending.id, player_id=p[3].id, side=1),
            MatchPlayer(match_id=pending.id, player_id=p[2].id, side=0),
            MatchPlayer(match_id=tbd.id, player_id=p[4].id, side=0),
            MatchPlayer(match_id=tbd.id, player_id=None, side=1),
        ])
        await session.commit()

    flow = TournamentFlowService()
    assert await flow.unplayed_matchups(1, "OPEN") == [((1002, "Player3"), (1003, "Player4"))]
    assert await flow.unplayed_matchups(1, "SWISS") is None
