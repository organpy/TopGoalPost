"""Health checks and safe SQLite backups.

Only checks that need no Discord objects live here; the admin cog adds the
channel, role and permission checks on top. Nothing is repaired automatically.
"""

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from bot.config import Config
from bot.db import PROJECT_ROOT, get_db_manager, resolve_database_url
from bot.db.schema import alembic_head
from bot.rendering import ASSETS_ROOT

REQUIRED_CONFIG = ("match_channel", "score_channel")
RECOMMENDED_CONFIG = (
    "agreements_channel",
    "announcements_channel",
    "admin_roles",
    "moderator_role",
    "signup_role",
)
ASSET_DIRS = ("fonts", "flags", "pfps", "tables")
BACKUP_RETENTION = 10
SQLITE_PREFIX = "sqlite+aiosqlite:///"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True

    @property
    def icon(self) -> str:
        if self.ok:
            return "✅"
        return "❌" if self.required else "⚠️"


def database_path(database_url: Optional[str] = None) -> Optional[Path]:
    """Path of the configured SQLite file, or None for non-file databases."""
    url = resolve_database_url(
        database_url or os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db")
    )
    if not url.startswith(SQLITE_PREFIX):
        return None
    return Path(url[len(SQLITE_PREFIX):])


class HealthService:
    def __init__(self):
        self.db = get_db_manager()

    async def check(self) -> list[Check]:
        checks = [await self._db_writable(), await self._migration_head()]
        cfg = Config()
        for key in REQUIRED_CONFIG + RECOMMENDED_CONFIG:
            value = await cfg.get(key)
            checks.append(
                Check(
                    f"config `{key}`",
                    bool(value),
                    "" if value else f"`!set_config {key} ...`",
                    required=key in REQUIRED_CONFIG,
                )
            )
        for name in ASSET_DIRS:
            path = ASSETS_ROOT / name
            checks.append(Check(f"assets/{name}", path.is_dir(), "" if path.is_dir() else "missing"))
        return checks

    async def _db_writable(self) -> Check:
        try:
            async with self.db.engine.begin() as conn:
                await conn.execute(text("CREATE TEMP TABLE IF NOT EXISTS _doctor (x INTEGER)"))
                await conn.execute(text("DROP TABLE _doctor"))
            return Check("database writable", True)
        except Exception as e:  # report, never raise
            return Check("database writable", False, type(e).__name__)

    async def _migration_head(self) -> Check:
        head = alembic_head()
        try:
            async with self.db.engine.connect() as conn:
                current = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar()
        except Exception:
            current = None
        return Check(
            "migrations at head",
            current == head,
            "" if current == head else f"at {current or 'none'}, expected {head}",
        )


def _backup_sync(source: Path, target: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def backup_database(
    source: Path,
    backup_dir: Optional[Path] = None,
    keep: int = BACKUP_RETENTION,
) -> Path:
    """Copy a live SQLite DB with the backup API and prune old copies.

    Unlike a file copy this is consistent while the bot is writing (WAL mode).
    """
    backup_dir = backup_dir or PROJECT_ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    target = backup_dir / f"{source.stem}_{timestamp}.db"
    await asyncio.to_thread(_backup_sync, source, target)

    backups = sorted(backup_dir.glob(f"{source.stem}_*.db"))
    for old in backups[:-keep] if keep else []:
        old.unlink(missing_ok=True)
    return target
