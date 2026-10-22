"""End-to-end test that runs the full tournament pipeline using an in-memory DB."""

import os
from collections import Counter

import pytest
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"

from bot.db import init_db, get_db_manager, PlayerSeason, MatchRepository
from bot.services.season import SeasonService
from bot.services.signup import SignupService
from bot.services.pairing import PairingService
from bot.services.match import MatchService
from bot.services.playin import PlayinService
from bot.config import Config

FLAG_POOL = ["us", "fr", "jp", "gb", "de", "ca", "br", "ar"]


def _play_game(p1, p2):
    """Simulate a random game outcome."""
    import random
    s1, s2 = 0, 0
    while s1 == s2:
        s1 = random.randint(0, 5)
        s2 = random.randint(0, 5)
    return p1 if s1 > s2 else p2, s1, s2


async def _play_all_matches(event_id, db):
    """Play all uncompleted matches in an event."""
    from sqlalchemy import select, and_
    from sqlalchemy.orm import selectinload
    from bot.db import Match, MatchPlayer, MatchRepository
    from bot.services.match import MatchService

    ms = MatchService()
    for _ in range(200):
        async with db.get_session() as session:
            repo = MatchRepository(session)
            unplayed = await repo.get_unplayed_by_event(event_id)
        if not unplayed:
            break
        played = 0
        for match in unplayed:
            mps = sorted(match.match_players, key=lambda mp: mp.side)
            if len(mps) < 2 or not mps[0].player_id or not mps[1].player_id:
                continue
            p1, p2 = mps[0].player_id, mps[1].player_id
            winner, s1, s2 = _play_game(p1, p2)
            winner_side = 0 if winner == p1 else 1
            await ms.report_score(match.id, s1, s2, winner_side)
            played += 1
        if played == 0:
            break


@pytest.mark.asyncio
async def test_full_tournament_pipeline():
    """64 players → Open → Playin → Swiss → Playoff, verifies counts."""
    import random
    random.seed(42)

    await init_db()
    db = get_db_manager()

    config = Config()
    await config.init_defaults()

    season_service = SeasonService()
    signup_service = SignupService()
    pairing_service = PairingService()
    playin_service = PlayinService()

    NUM_PLAYERS = 64
    NUM_SEEDS = 8

    await season_service.create_season(1)
    await season_service.advance_state("SIGNUP")

    base_id = 1000000
    for i in range(NUM_PLAYERS):
        name = f"P{i+1:03d}"
        country = FLAG_POOL[i % len(FLAG_POOL)]
        await signup_service.register(base_id + i + 1, name, country)

    # Set returning seeds
    async with db.get_session() as session:
        from sqlalchemy import select
        stmt = select(PlayerSeason).where(PlayerSeason.season_number == 1)
        res = await session.execute(stmt)
        ps_list = res.scalars().all()
        for i, ps in enumerate(ps_list[:NUM_SEEDS]):
            ps.swiss_seed = i + 1
        await session.commit()

    # OPEN
    await season_service.advance_state("OPEN")
    matches, players, err = await pairing_service.generate_open(1)
    assert err is None
    assert matches > 0
    assert players == NUM_PLAYERS - NUM_SEEDS

    # Get open event_id
    from bot.db import EventType, EventRepository
    async with db.get_session() as session:
        ev = await EventRepository(session).get_by_type_and_season(EventType.OPEN, 1)
        assert ev is not None
        event_id = ev.id

    await _play_all_matches(event_id, db)

    # Open stats are synced automatically when scores are reported.
    sync = await MatchService().sync_open_stats(1)
    assert "error" not in sync

    # PLAYIN
    await season_service.advance_state("PLAYIN")
    gen = await playin_service.generate_playins(1)
    for _ in range(50):
        async with db.get_session() as session:
            ev = await EventRepository(session).get_by_type_and_season(EventType.PLAYIN, 1)
            if ev:
                await _play_all_matches(ev.id, db)
        advance = await playin_service.try_advance_after_score(1)
        if advance is None:
            break

    # Playins finalize automatically once the last ladder match is scored.
    async with db.get_session() as session:
        ev = await EventRepository(session).get_by_type_and_season(EventType.PLAYIN, 1)
        playin_matches = await MatchRepository(session).get_by_event(ev.id)
        assert playin_matches
        assert all(m.completed_at is not None for m in playin_matches)
        ps_all = (
            await session.execute(
                select(PlayerSeason).where(PlayerSeason.season_number == 1)
            )
        ).scalars().all()
    participants = [
        ps for ps in ps_all
        if (ps.open_wins or 0) >= 1 and not ps.swiss_seed
    ]
    assert participants
    assert all(
        ps.playin_rank is not None or ps.playin_eliminated
        for ps in participants
    )

    # SWISS — each player advances as soon as their match is scored.
    await season_service.advance_state("SWISS")
    await pairing_service.generate_swiss_round(1, None)
    for _ in range(6):
        async with db.get_session() as session:
            ev = await EventRepository(session).get_by_type_and_season(EventType.SWISS, 1)
            if not ev:
                break
            before = await MatchRepository(session).get_by_event(ev.id)
        await _play_all_matches(ev.id, db)
        async with db.get_session() as session:
            after = await MatchRepository(session).get_by_event(ev.id)
        if not before and not after:
            break
        if (
            before
            and after
            and max((m.series_game or 1) for m in before)
            == max((m.series_game or 1) for m in after)
            and not any(m.completed_at is None for m in after)
        ):
            break

    async with db.get_session() as session:
        ev = await EventRepository(session).get_by_type_and_season(EventType.SWISS, 1)
        swiss_matches = await MatchRepository(session).get_by_event(ev.id)
    assert swiss_matches
    assert max((m.series_game or 1) for m in swiss_matches) == 5
    assert all(m.completed_at is not None for m in swiss_matches)
    counts = Counter(m.series_game or 1 for m in swiss_matches)
    assert dict(sorted(counts.items())) == {1: 16, 2: 16, 3: 16, 4: 12, 5: 6}

    stats: dict[int, list[int]] = {}
    for match in swiss_matches:
        mps = [
            mp for mp in match.match_players if mp.player_id is not None
        ]
        if len(mps) < 2:
            continue
        for mp in mps:
            stats.setdefault(mp.player_id, [0, 0])
            if mp.is_winner:
                stats[mp.player_id][0] += 1
            else:
                stats[mp.player_id][1] += 1
    assert sum(1 for wins, _ in stats.values() if wins >= 3) == 16

    # PLAYOFF
    bracket = await pairing_service.generate_playoff_bracket(1)
    await season_service.advance_state("PLAYOFF")
    assert len(bracket) > 0

    async with db.get_session() as session:
        ev = await EventRepository(session).get_by_type_and_season(EventType.PLAYOFF, 1)
        assert ev is not None
        await _play_all_matches(ev.id, db)

    # Verify something happened — at least some matches completed
    async with db.get_session() as session:
        from bot.db import Match
        from sqlalchemy import select, func
        stmt = select(func.count(Match.id)).where(Match.completed_at.isnot(None))
        result = await session.execute(stmt)
        total_completed = result.scalar()
        assert total_completed > 0, "No matches were completed in the pipeline"
