"""Season summary aggregation and placement tests."""

from datetime import datetime

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
from bot.services.season_summary import SeasonSummaryService


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


async def _season_players(session, season: int = 1):
    players = [
        Player(discord_id=101, name="Alice", country="US"),
        Player(discord_id=102, name="Bob", country="BR"),
        Player(discord_id=103, name="Carol", country="US"),
        Player(discord_id=104, name="Dave", country="CA"),
    ]
    session.add_all(players)
    await session.flush()
    session.add_all(
        PlayerSeason(player_id=player.id, season_number=season)
        for player in players
    )
    await session.flush()
    return players


async def test_summary_derives_champion_and_stat_leaders(db):
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ARCHIVED))
        await session.flush()
        alice, bob, carol, dave = await _season_players(session)

        open_event = await _event(session, 1, EventType.OPEN)
        playoff_event = await _event(session, 1, EventType.PLAYOFF)

        await _match(session, open_event, alice, bob, 5, 1)
        await _match(session, open_event, alice, carol, 4, 2)
        await _match(session, open_event, bob, carol, 6, 0)
        await _match(session, open_event, bob, dave, 3, 2)
        await _match(
            session, playoff_event, alice, bob, 4, 3, slot="GF-M1", game=1
        )
        await _match(
            session, playoff_event, alice, bob, 5, 2, slot="GF-M1", game=2
        )
        await session.commit()

    summary = await SeasonSummaryService().get_summary(1)

    assert summary.active is False
    assert summary.has_data is True
    assert summary.matches == 6
    assert summary.players == 4
    assert summary.goals == 37
    assert summary.champion.name == "Alice"
    assert summary.runner_up.name == "Bob"
    assert summary.champion.games == 4
    assert summary.champion.wins == 4
    assert summary.champion.goals_for == 18
    assert summary.champion.goals_against == 8
    assert summary.champion.goal_diff == 10
    assert summary.champion.win_rate == 1.0
    assert summary.runner_up.games == 5
    assert summary.runner_up.wins == 2
    assert summary.runner_up.losses == 3
    assert summary.runner_up.goals_for == 15
    assert summary.runner_up.goals_against == 16
    assert summary.runner_up.goal_diff == -1

    awards = {award.key: award for award in summary.awards}
    assert set(awards) == {
        "top_scorer",
        "most_conceded",
        "most_games",
        "most_wins",
        "best_diff",
        "best_rate",
    }
    assert awards["top_scorer"].player.name == "Alice"
    assert awards["top_scorer"].value == "18"
    assert awards["top_scorer"].caption == "4 games played"
    assert awards["most_conceded"].player.name == "Bob"
    assert awards["most_conceded"].value == "16"
    assert awards["most_conceded"].caption == "5 games played"
    assert awards["most_games"].player.name == "Bob"
    assert awards["most_games"].value == "5"
    assert awards["most_games"].caption == "2-3 record"
    assert awards["most_wins"].player.name == "Alice"
    assert awards["most_wins"].value == "4"
    assert awards["best_diff"].player.name == "Alice"
    assert awards["best_diff"].value == "+10"
    assert awards["best_rate"].player.name == "Alice"
    assert awards["best_rate"].value == "100%"


async def test_grand_final_reset_decides_champion_and_runner_up(db):
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ARCHIVED))
        await session.flush()
        alice, bob, _carol, _dave = await _season_players(session)
        playoff = await _event(session, 1, EventType.PLAYOFF)

        await _match(
            session, playoff, bob, alice, 3, 1, slot="GF-M1", game=1
        )
        await _match(
            session, playoff, bob, alice, 3, 2, slot="GF-M2", game=1
        )
        await _match(
            session, playoff, bob, alice, 4, 1, slot="GF-M2", game=2
        )
        await session.commit()

    summary = await SeasonSummaryService().get_summary(1)

    assert summary.champion.name == "Bob"
    assert summary.runner_up.name == "Alice"
    assert summary.matches == 3
    assert summary.players == 2


async def test_empty_season_has_no_placements_or_awards(db):
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.commit()

    summary = await SeasonSummaryService().get_summary(1)

    assert summary.has_data is False
    assert summary.matches == 0
    assert summary.players == 0
    assert summary.goals == 0
    assert summary.champion is None
    assert summary.runner_up is None
    assert len(summary.awards) == 6
    assert all(award.player is None for award in summary.awards)
    assert all(award.value == "—" for award in summary.awards)
