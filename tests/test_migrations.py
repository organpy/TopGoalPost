"""Alembic migrations run cleanly on a throwaway SQLite file."""

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


def _alembic(db_path: Path) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_upgrade_and_downgrade_round_trip(tmp_path):
    db_path = tmp_path / "migrate.db"
    cfg = _alembic(db_path)

    command.upgrade(cfg, "head")
    assert "match_reports" in _tables(db_path)
    assert {"scheduled_at", "scheduled_by"} <= _columns(db_path, "matches")
    assert "audit_log" in _tables(db_path)

    command.downgrade(cfg, "e7a1c3d9f210")
    assert "scheduled_at" not in _columns(db_path, "matches")

    command.downgrade(cfg, "d1e4f7a92b30")
    assert "match_reports" not in _tables(db_path)

    command.upgrade(cfg, "head")
    assert "match_reports" in _tables(db_path)
