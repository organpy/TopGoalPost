#!/usr/bin/env python3
"""Seed dummy players into a dedicated test DB."""
import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select

from bot.db import init_db, get_db_manager, Player, PlayerSeason
from bot.services.season import SeasonService
from bot.services.signup import SignupService
from bot.services.dev import DUMMY_ID_BASE, DUMMY_ID_END
from bot.config import Config

FLAG_POOL = ["us", "fr", "jp", "gb", "de", "ca", "br", "ar"]


def parse_args():
    parser = argparse.ArgumentParser(description="Seed dummy players into a test DB.")
    parser.add_argument("--db", type=str, default="data/dummy.db")
    return parser.parse_args()


async def main():
    args = parse_args()
    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    print(f"[DB] {db_path}")

    await init_db()

    config = Config()
    await config.init_defaults()

    season_service = SeasonService()
    signup_service = SignupService()
    db = get_db_manager()

    # Create season if not exists
    await season_service.create_season(1)
    await season_service.advance_state("SIGNUP")

    # Delete only the reserved dummy-ID range, never real Discord IDs.
    async with db.get_session() as session:
        dummy_ps = await session.execute(
            select(PlayerSeason)
            .join(Player)
            .where(Player.discord_id.between(DUMMY_ID_BASE, DUMMY_ID_END))
        )
        for ps in dummy_ps.scalars().all():
            await session.delete(ps)
        await session.execute(
            delete(Player).where(
                Player.discord_id.between(DUMMY_ID_BASE, DUMMY_ID_END)
            )
        )
        await session.commit()

    # Register 64 players
    for i in range(64):
        discord_id = DUMMY_ID_BASE + i + 1
        name = f"Player{i + 1:03d}"
        country = FLAG_POOL[i % len(FLAG_POOL)]
        success, msg = await signup_service.register(discord_id, name, country)
        print(f"  {name}: {msg}")

    # Set 13 returning seeds
    async with db.get_session() as session:
        stmt = select(PlayerSeason).where(PlayerSeason.season_number == 1)
        res = await session.execute(stmt)
        ps_list = res.scalars().all()
        for i, ps in enumerate(ps_list[:13]):
            ps.swiss_seed = i + 1
        await session.commit()

    print("\nSeeded 64 dummy players. State is SIGNUP.")
    print("13 returning seeds (swiss_seed 1-13), 51 open-stage competitors.")
    print(
        f"Dummy Discord IDs: {DUMMY_ID_BASE + 1} through {DUMMY_ID_BASE + 64}"
    )


if __name__ == "__main__":
    asyncio.run(main())
