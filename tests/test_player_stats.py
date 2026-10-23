"""Player season stats: Open participation states and stage aggregation."""

from datetime import datetime

import pytest

from bot.db.models import (
    Event,
    EventStatus,
    EventType,
    Match,
    MatchPlayer,
    Player,
    PlayerSeason,
    Season,
    SeasonStatus,
)
from bot.db.repositories import PlayerRepository
from bot.services.player_stats import OpenState, PlayerStatsService, StageRecord


async def _event(session, season: int, event_type: EventType) -> Event:
    event = Event(
        name=f"{event_type.value}-{season}",
        type=event_type,
        season=season,
        status=EventStatus.COMPLETED,
    )
    session.add(event)
    await session.flush()
    return event


async def _match(
    session,
    event: Event,
    p1: Player,
    p2: Player,
    score1: int,
    score2: int,
    *,
    slot: str | None = None,
    game: int = 1,
) -> Match:
    match = Match(
        event_id=event.id,
        completed_at=datetime.utcnow(),
        bracket_slot=slot,
        series_game=game,
    )
    session.add(match)
    await session.flush()
    session.add_all(
        [
            MatchPlayer(
                match_id=match.id,
                player_id=p1.id,
                goals=score1,
                is_winner=score1 > score2,
                side=1,
            ),
            MatchPlayer(
                match_id=match.id,
                player_id=p2.id,
                goals=score2,
                is_winner=score2 > score1,
                side=2,
            ),
        ]
    )
    await session.flush()
    return match


@pytest.fixture
async def world(db):
    """Season 2 with two players, one Open match, one Swiss match."""
    async with db.get_session() as session:
        session.add(Season(number=2, status=SeasonStatus.ACTIVE))
        await session.flush()

        repo = PlayerRepository(session)
        alice = await repo.create(discord_id=1, name="Alice", country="US")
        bob = await repo.create(discord_id=2, name="Bob", country="BR")
        carol = await repo.create(discord_id=3, name="Carol", country=None)

        session.add_all(
            [
                PlayerSeason(player_id=alice.id, season_number=2, hp=3),
                PlayerSeason(player_id=bob.id, season_number=2, swiss_seed=4),
                PlayerSeason(player_id=carol.id, season_number=2),
            ]
        )

        open_event = await _event(session, 2, EventType.OPEN)
        swiss_event = await _event(session, 2, EventType.SWISS)

        await _match(session, open_event, alice, bob, 5, 2)
        await _match(session, open_event, bob, alice, 1, 3)
        await _match(session, swiss_event, alice, carol, 2, 2)
        await session.commit()

        return {"alice": alice.id, "bob": bob.id, "carol": carol.id}


async def test_open_played_aggregates_goals_and_wins(db, world):
    stats = await PlayerStatsService().get_stats(world["alice"], 2)

    assert stats.open_state is OpenState.PLAYED
    assert stats.open.games == 2
    assert stats.open.wins == 2
    assert stats.open.losses == 0
    assert stats.open.goals_for == 8
    assert stats.open.goals_against == 3
    assert stats.open.goal_diff == 5
    assert stats.open_summary == "2-0 · GD +5"


async def test_skipped_open_is_reported_as_skipped(db, world):
    """Bob has a seed and one Open game, so he actually played."""
    stats = await PlayerStatsService().get_stats(world["bob"], 2)
    assert stats.open_state is OpenState.PLAYED
    assert stats.open.games == 2
    assert stats.open.wins == 0
    assert stats.open.losses == 2


async def test_seed_without_open_games_is_skipped(db, world):
    async with db.get_session() as session:
        carol = await PlayerRepository(session).get_by_discord_id(3)
        player_season = await session.get(PlayerSeason, (carol.id, 2))
        player_season.swiss_seed = 7
        await session.commit()

    stats = await PlayerStatsService().get_stats(world["carol"], 2)
    assert stats.open_state is OpenState.SKIPPED
    assert stats.open_summary == "Skipped Open (seed #7)"


async def test_player_without_open_games_did_not_play(db, world):
    stats = await PlayerStatsService().get_stats(world["carol"], 2)
    assert stats.open_state is OpenState.NOT_PLAYED
    assert stats.open_summary == "Did not play"


async def test_totals_span_every_stage(db, world):
    stats = await PlayerStatsService().get_stats(world["alice"], 2)

    assert stats.swiss.games == 1
    assert stats.total.games == 3
    assert stats.total.wins == 2
    assert stats.total.losses == 1
    assert stats.total.goals_for == 10
    assert stats.total.goals_against == 5


async def test_seasons_are_filtered(db, world):
    """A match from another season must not leak into this one's stats."""
    async with db.get_session() as session:
        session.add(Season(number=3, status=SeasonStatus.ARCHIVED))
        await session.flush()
        old_event = await _event(session, 3, EventType.OPEN)
        alice = await PlayerRepository(session).get_by_discord_id(1)
        bob = await PlayerRepository(session).get_by_discord_id(2)
        await _match(session, old_event, alice, bob, 9, 0)
        await session.commit()

    season_two = await PlayerStatsService().get_stats(world["alice"], 2)
    season_three = await PlayerStatsService().get_stats(world["alice"], 3)

    assert season_two.open.goals_for == 8
    assert season_three.open.goals_for == 9
    assert season_three.open.games == 1
    assert season_three.season_active is False


async def test_available_seasons_lists_data_newest_first(db, world):
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ARCHIVED))
        await session.flush()
        await _event(session, 1, EventType.OPEN)
        await session.commit()

    assert await PlayerStatsService().available_seasons() == [2, 1]


async def test_champion_flag_and_unknown_player(db, world):
    async with db.get_session() as session:
        season = await session.get(Season, 2)
        season.champion_id = world["alice"]
        await session.commit()

    stats = await PlayerStatsService().get_stats(world["alice"], 2)
    assert stats.champion is True

    assert await PlayerStatsService().get_stats(9999, 2) is None


def test_stage_record_helpers():
    record = StageRecord()
    assert not record.played
    assert record.win_rate == 0.0

    record.add(goals_for=3, goals_against=1, won=True)
    record.add(goals_for=0, goals_against=2, won=False)

    assert record.played
    assert record.games == 2
    assert record.goals_for == 3
    assert record.goals_against == 3
    assert record.goal_diff == 0
    assert record.win_rate == 0.5
