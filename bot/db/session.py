from typing import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker

from .models import Base

BUSY_TIMEOUT_MS = 30_000


def is_file_sqlite(database_url: str) -> bool:
    """True for on-disk SQLite URLs; in-memory databases cannot use WAL."""
    if not database_url.startswith("sqlite"):
        return False
    path = database_url.split("///", 1)[1] if "///" in database_url else ""
    return bool(path) and path != ":memory:" and "mode=memory" not in path


def _install_sqlite_pragmas(engine, file_backed: bool) -> None:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        if file_backed:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


class DatabaseManager:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = None
        self.async_session_maker = None

    async def init(self):
        is_sqlite = self.database_url.startswith("sqlite")
        self.engine = create_async_engine(
            self.database_url,
            echo=False,
            connect_args={"timeout": BUSY_TIMEOUT_MS / 1000} if is_sqlite else {},
        )
        if is_sqlite:
            _install_sqlite_pragmas(self.engine, is_file_sqlite(self.database_url))
        self.async_session_maker = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    async def create_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_tables(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    @asynccontextmanager
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        if self.async_session_maker is None:
            raise RuntimeError("Database not initialized. Call init() first.")

        async with self.async_session_maker() as session:
            try:
                yield session
            finally:
                await session.close()

    async def close(self):
        if self.engine:
            await self.engine.dispose()


_db_manager: DatabaseManager | None = None


def get_db_manager() -> DatabaseManager:
    global _db_manager
    if _db_manager is None:
        raise RuntimeError("Database manager not initialized")
    return _db_manager


def init_db_manager(database_url: str) -> DatabaseManager:
    global _db_manager
    _db_manager = DatabaseManager(database_url)
    return _db_manager
