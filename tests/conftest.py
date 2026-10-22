"""Shared test fixtures for service tests."""

import pytest_asyncio

from bot.db import init_db_manager, get_db_manager
from bot.db.models import (
    Player, Season, PlayerSeason, Event, Match, MatchPlayer,
    EventType, EventStatus, SeasonStatus,
)
from bot.db.repositories import (
    PlayerRepository, SeasonRepository, PlayerSeasonRepository,
    EventRepository, MatchRepository, MatchPlayerRepository,
    SettingRepository,
)
from bot.services.match import MatchService
from bot.services.pairing import PairingService
from bot.services.playin import PlayinService


@pytest_asyncio.fixture(scope="function")
async def db():
    """Initialize in-memory SQLite database per test function."""
    init_db_manager("sqlite+aiosqlite://")
    manager = get_db_manager()
    await manager.init()
    await manager.create_tables()
    yield manager
    await manager.drop_tables()
    await manager.close()


@pytest_asyncio.fixture(scope="function")
async def services(db):
    """Initialize all services (depends on db)."""
    return {
        "match": MatchService(),
        "pairing": PairingService(),
        "playin": PlayinService(),
    }


@pytest_asyncio.fixture(scope="function")
async def seed_data(db):
    """Create a minimal tournament state for testing.

    Creates: season 1, 8 players, player_season entries, and an OPEN event.
    Returns dict of created objects for use in tests.
    """
    data = {}
    async with db.get_session() as session:
        s_repo = SeasonRepository(session)
        season = Season(number=1, status=SeasonStatus.ACTIVE)
        session.add(season)
        await session.flush()
        data["season"] = season

        p_repo = PlayerRepository(session)
        players = []
        for i in range(8):
            p = await p_repo.create(
                discord_id=1000 + i,
                name=f"Player{i+1}",
                country="US",
            )
            players.append(p)
        data["players"] = players

        ps_repo = PlayerSeasonRepository(session)
        for p in players:
            ps = PlayerSeason(player_id=p.id, season_number=1, hp=4, open_wins=1)
            session.add(ps)
        await session.flush()

        setting_repo = SettingRepository(session)
        await setting_repo.set("season", "1")
        await session.commit()

    return data


@pytest_asyncio.fixture(scope="function")
async def open_event(db, seed_data):
    """Create an OPEN event with paired matches."""
    pairing = PairingService()
    event_repo = EventRepository
    async with db.get_session() as session:
        repo = EventRepository(session)
        event = await repo.create(
            name="Season 1 Open",
            event_type=EventType.OPEN,
            season=1,
        )
        event.status = EventStatus.ACTIVE

        session.add(event)
        await session.commit()
    return event
