"""Content-addressed cache for generated images.

Only cache metadata lives in the database. Rendered files stay on disk and are
always written atomically by the renderers. Clearing the cache therefore never
touches match data, player avatars, or files that are currently being sent.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from bot.branding import BRANDING
from bot.db import ImageCacheRepository, get_db_manager

CACHE_VERSION = 1


def render_cache_key(kind: str, payload: Any) -> str:
    """Stable key for one kind of render and its input data."""
    raw = repr(
        (kind, CACHE_VERSION, BRANDING.footer, BRANDING.circuit_label, payload)
    )
    return hashlib.sha3_256(raw.encode("utf-8")).hexdigest()


async def get_cached_render(key: str) -> str | None:
    """Return a cached file only when it still exists.

    URL entries are returned as-is for compatibility with older cache rows.
    Missing local files are removed so the caller re-renders instead of
    serving a broken path.
    """
    async with get_db_manager().get_session() as session:
        repo = ImageCacheRepository(session)
        cached = await repo.get(key)
        if not cached:
            return None
        if cached.startswith(("http://", "https://")):
            return cached
        if os.path.exists(cached):
            return cached
        await repo.delete(key)
        await session.commit()
        return None


async def store_render(key: str, filename: str) -> None:
    """Remember that ``key`` produced ``filename``."""
    async with get_db_manager().get_session() as session:
        await ImageCacheRepository(session).set(key, filename)
        await session.commit()


async def clear_render_cache() -> int:
    """Drop every cache row and return how many entries were removed.

    Generated files are intentionally left in place. The next render overwrites
    them atomically, and deleting them could race with a message being sent.
    """
    async with get_db_manager().get_session() as session:
        repo = ImageCacheRepository(session)
        count = await repo.count()
        await repo.delete_all()
        await session.commit()
        return count
