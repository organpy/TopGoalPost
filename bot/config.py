"""Configuration manager backed by the settings table."""

from typing import Optional

from bot.db import get_db_manager, SettingRepository

ADMIN_ROLES_KEY = "admin_roles"


def parse_role_ids(value: Optional[str]) -> list[int]:
    """Parse a comma/space separated list of Discord role IDs."""
    if not value:
        return []
    ids = []
    for part in value.replace(",", " ").split():
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


class Config:
    def __init__(self):
        self.db = get_db_manager()

    async def init_defaults(self):
        async with self.db.get_session() as session:
            repo = SettingRepository(session)
            if await repo.get("stage") is None:
                await repo.set("stage", "OFFSEASON")
            await session.commit()

    async def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self.db.get_session() as session:
            repo = SettingRepository(session)
            val = await repo.get(key)
            return val if val is not None else default

    async def set(self, key: str, value: str) -> None:
        async with self.db.get_session() as session:
            repo = SettingRepository(session)
            await repo.set(key, value)
            await session.commit()

    async def remove(self, key: str) -> bool:
        """Remove a config key. Returns True if it existed."""
        async with self.db.get_session() as session:
            repo = SettingRepository(session)
            removed = await repo.remove(key)
            await session.commit()
            return removed

    async def get_season_state(self) -> str:
        return await self.get("stage", "OFFSEASON")

    async def set_season_state(self, state: str) -> None:
        await self.set("stage", state)

    async def get_int(self, key: str, default: int = 0) -> int:
        val = await self.get(key)
        if val is None:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    async def get_bool(self, key: str, default: bool = False) -> bool:
        val = await self.get(key)
        if val is None:
            return default
        return val.lower() in ("1", "true", "yes")

    async def get_role_id(self, key: str) -> Optional[int]:
        val = await self.get(key)
        if not val:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    async def get_channel_id(self, key: str) -> Optional[int]:
        return await self.get_role_id(key)

    async def get_admin_role_ids(self) -> list[int]:
        """Role IDs allowed to use admin/host commands (admins always pass)."""
        return parse_role_ids(await self.get(ADMIN_ROLES_KEY))
