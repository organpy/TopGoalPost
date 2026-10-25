"""SQLite connections get WAL, foreign keys and a busy timeout."""

from sqlalchemy import text

from bot.db.session import BUSY_TIMEOUT_MS, DatabaseManager, is_file_sqlite


async def _pragmas(manager: DatabaseManager) -> dict[str, object]:
    async with manager.engine.connect() as conn:
        return {
            name: (await conn.execute(text(f"PRAGMA {name}"))).scalar()
            for name in ("journal_mode", "foreign_keys", "busy_timeout")
        }


async def test_file_database_pragmas(tmp_path):
    manager = DatabaseManager(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    await manager.init()
    try:
        pragmas = await _pragmas(manager)
    finally:
        await manager.close()
    assert pragmas == {
        "journal_mode": "wal",
        "foreign_keys": 1,
        "busy_timeout": BUSY_TIMEOUT_MS,
    }


async def test_memory_database_skips_wal(db):
    pragmas = await _pragmas(db)
    assert pragmas["foreign_keys"] == 1
    assert pragmas["journal_mode"] == "memory"


def test_is_file_sqlite():
    assert is_file_sqlite("sqlite+aiosqlite:///data/bot.db")
    assert not is_file_sqlite("sqlite+aiosqlite://")
    assert not is_file_sqlite("sqlite+aiosqlite:///:memory:")
    assert not is_file_sqlite("postgresql+asyncpg://localhost/db")
