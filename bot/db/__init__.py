from .models import (
    Base,
    Player,
    PlayerSeason,
    Season,
    Setting,
    Event,
    Match,
    MatchPlayer,
    MatchReport,
    ImageCache,
    Timezone,
    Timeslot,
    WeeklyTimeslot,
    EventType,
    EventStatus,
    SeasonStatus,
    ReportStatus,
)
from .session import DatabaseManager, get_db_manager, init_db_manager
from .repositories import (
    PlayerRepository,
    SeasonRepository,
    PlayerSeasonRepository,
    SettingRepository,
    EventRepository,
    MatchRepository,
    MatchPlayerRepository,
    MatchReportRepository,
    ImageCacheRepository,
    TimezoneRepository,
    TimeslotRepository,
)

import os
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

SQLITE_PREFIX = "sqlite+aiosqlite:///"


def resolve_database_url(db_url: str) -> str:
    """Resolve relative sqlite URLs against the project root, independent of CWD."""
    if db_url.startswith(SQLITE_PREFIX):
        path = db_url[len(SQLITE_PREFIX):]
        if path and not pathlib.PurePath(path).is_absolute():
            return SQLITE_PREFIX + str(PROJECT_ROOT / path)
    return db_url


async def init_db():
    db_url = resolve_database_url(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db"))
    if db_url.startswith(SQLITE_PREFIX):
        path = db_url[len(SQLITE_PREFIX):]
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)

    db = init_db_manager(db_url)
    await db.init()
    from .schema import sync_schema

    await sync_schema(db.engine)
    return db

__all__ = [
    "Base",
    "Player",
    "PlayerSeason",
    "Season",
    "Setting",
    "Event",
    "Match",
    "MatchPlayer",
    "MatchReport",
    "ReportStatus",
    "ImageCache",
    "Timezone",
    "Timeslot",
    "WeeklyTimeslot",
    "EventType",
    "EventStatus",
    "SeasonStatus",
    "DatabaseManager",
    "get_db_manager",
    "init_db_manager",
    "init_db",
    "PlayerRepository",
    "SeasonRepository",
    "PlayerSeasonRepository",
    "SettingRepository",
    "EventRepository",
    "MatchRepository",
    "MatchPlayerRepository",
    "MatchReportRepository",
    "ImageCacheRepository",
    "TimezoneRepository",
    "TimeslotRepository",
]
