"""Standings image caching service.

Handles caching PIL generated images via Imgur or similar.
"""

from typing import Optional

from sqlalchemy import select

from bot.db import (
    get_db_manager,
    ImageCache,
)


class StandingsService:
    def __init__(self):
        self.db = get_db_manager()

    async def get_cached_image(self, image_hash: str) -> Optional[str]:
        """Get a cached image URL by its content hash."""
        async with self.db.get_session() as session:
            stmt = select(ImageCache).where(ImageCache.hash == image_hash)
            result = await session.execute(stmt)
            cache_entry = result.scalars().first()
            return cache_entry.link if cache_entry else None

    async def set_cached_image(self, image_hash: str, url: str) -> None:
        """Store a cached image URL by its content hash."""
        async with self.db.get_session() as session:
            stmt = select(ImageCache).where(ImageCache.hash == image_hash)
            result = await session.execute(stmt)
            cache_entry = result.scalars().first()
            
            if cache_entry:
                cache_entry.link = url
            else:
                cache_entry = ImageCache(hash=image_hash, link=url)
                session.add(cache_entry)
                
            await session.commit()
