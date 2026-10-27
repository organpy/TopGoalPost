"""Startup schema sync upgrades create_all-era databases additively."""

import sqlite3

from bot.db.schema import alembic_head, sync_schema
from bot.db.session import DatabaseManager


async def _sync(db_path):
    manager = DatabaseManager(f"sqlite+aiosqlite:///{db_path}")
    await manager.init()
    try:
        return await sync_schema(manager.engine)
    finally:
        await manager.close()


async def test_legacy_database_gets_missing_pieces(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE players (id INTEGER PRIMARY KEY, discord_id INTEGER NOT NULL, name VARCHAR(255) NOT NULL)")

    changes = await _sync(db_path)

    assert "added column players.country" in changes
    assert f"stamped alembic {alembic_head()}" in changes
    with sqlite3.connect(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert "match_reports" in tables
    assert version == alembic_head()


async def test_sync_is_idempotent(tmp_path):
    db_path = tmp_path / "fresh.db"
    await _sync(db_path)
    assert await _sync(db_path) == []


async def test_incomplete_sync_is_not_stamped(tmp_path):
    db_path = tmp_path / "broken.db"
    with sqlite3.connect(db_path) as conn:
        # `name` is NOT NULL in the model, so sync cannot add it.
        conn.execute("CREATE TABLE players (id INTEGER PRIMARY KEY, discord_id INTEGER NOT NULL)")

    changes = await _sync(db_path)

    assert not any(c.startswith("stamped alembic") for c in changes)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() is None
