"""Startup schema sync for databases created by ``create_all``.

Older installs never ran Alembic, so ``alembic upgrade head`` fails on them.
At startup we apply only *additive* changes the models define (missing tables,
missing nullable columns, missing indexes) and stamp the Alembic head so later
migrations run normally. Anything else is logged, never guessed at.
"""

import logging
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateIndex

from .models import Base

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def alembic_head() -> str:
    cfg = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _sync(conn) -> list[str]:
    changes: list[str] = []
    Base.metadata.create_all(conn)
    insp = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            if not column.nullable:
                logger.error(
                    "Column %s.%s is missing and NOT NULL; run Alembic manually.",
                    table.name, column.name,
                )
                continue
            col_type = column.type.compile(dialect=conn.dialect)
            conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {col_type}'))
            changes.append(f"added column {table.name}.{column.name}")

        indexes = {i["name"] for i in insp.get_indexes(table.name)}
        for index in table.indexes:
            if index.name not in indexes:
                conn.execute(CreateIndex(index))
                changes.append(f"added index {index.name}")

    head = alembic_head()
    if not insp.has_table("alembic_version"):
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
    current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if current is None:
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head})
        changes.append(f"stamped alembic {head}")
    elif current != head:
        logger.warning(
            "Database is at Alembic revision %s, code expects %s. "
            "Back up data/bot.db and run `alembic -c alembic.ini upgrade head`.",
            current, head,
        )
    return changes


async def sync_schema(engine) -> list[str]:
    """Apply additive schema changes; returns a description of what changed."""
    async with engine.begin() as conn:
        changes = await conn.run_sync(_sync)
    for change in changes:
        logger.info("Schema sync: %s", change)
    return changes
