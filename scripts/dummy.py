#!/usr/bin/env python3
import argparse
import asyncio
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload

from bot.db import init_db, get_db_manager, EventType, Match, MatchPlayer, Event, PlayerSeason, EventStatus
from bot.services.season import SeasonService
from bot.services.signup import SignupService
from bot.services.pairing import PairingService
from bot.services.match import MatchService
from bot.services.playin import PlayinService
from bot.services.dev import DUMMY_ID_BASE
from bot.config import Config

FLAG_POOL = ["us", "fr", "jp", "gb", "de", "ca", "br", "ar"]


def play_game(p1, p2):
    s1, s2 = 0, 0
    while s1 == s2:
        s1 = random.randint(0, 5)
        s2 = random.randint(0, 5)
    return p1 if s1 > s2 else p2, s1, s2

def log(msg, verbose=False, debug=False, level="info"):
    if level == "debug" and not debug:
        return
    if level == "verbose" and not verbose:
        return
    print(msg)

def parse_args():
    parser = argparse.ArgumentParser(description="Generate dummy BL tournament data.")
    parser.add_argument("--db", type=str, default="data/dummy.db")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--players", type=int, default=64, help="Total players (unseeded + seeded)")
    parser.add_argument("--manual-seeds", type=int, default=8, help="Returning seeds (skip OPEN/PLAYIN)")
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--until", choices=["open", "playin", "swiss", "playoff"], default="playoff")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


async def _play_all_matches(match_service: MatchService, event_id: int, verbose=False, debug=False):
    db = get_db_manager()
    for outer_iter in range(200):
        async with db.get_session() as session:
            stmt = select(Match).options(selectinload(Match.match_players)).where(and_(Match.event_id == event_id, Match.completed_at.is_(None)))
            result = await session.execute(stmt)
            unplayed = result.scalars().all()

        if not unplayed:
            break

        played = 0
        for match in unplayed:
            mps = sorted(match.match_players, key=lambda mp: mp.side)
            if len(mps) < 2 or not mps[0].player_id or not mps[1].player_id:
                log(f"[WARN] Match {match.id} incomplete players", verbose=verbose, debug=debug, level="verbose")
                continue
            p1, p2 = mps[0].player_id, mps[1].player_id
            winner, s1, s2 = play_game(p1, p2)
            await match_service.report_score(match.id, s1, s2, 0 if winner == p1 else 1)
            played += 1
            log(f"[MATCH] {match.id}: {p1} vs {p2} -> {winner} {s1}-{s2}", verbose=verbose, debug=debug, level="debug")

        if played == 0:
            break

async def main_async():
    args = parse_args()
    random.seed(args.seed)
    if args.debug:
        args.verbose = True

    # Setup DB
    import os
    db_path = Path(args.db).resolve()
    print(f"[DB] {db_path}")
    if db_path.exists():
        if not args.overwrite:
            print("[ABORT] Database already exists. Pass --overwrite to replace it.")
            raise SystemExit(1)
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

    await init_db()
    
    config = Config()
    await config.init_defaults()
    
    season_service = SeasonService()
    signup_service = SignupService()
    pairing_service = PairingService()
    match_service = MatchService()
    playin_service = PlayinService()
    db = get_db_manager()
    
    await season_service.create_season(1)
    await season_service.advance_state("SIGNUP")
    
    log(f"[INIT] Creating {args.players} players ({args.manual_seeds} returning seeds)...", verbose=True)
    
    # Register players
    base_id = DUMMY_ID_BASE
    for i in range(args.players):
        discord_id = base_id + i + 1
        name = f"Player{i + 1:03d}"
        country = FLAG_POOL[i % len(FLAG_POOL)]
        await signup_service.register(discord_id, name, country)
        
    # Set returning seeds
    async with db.get_session() as session:
        stmt = select(PlayerSeason).where(PlayerSeason.season_number == 1)
        res = await session.execute(stmt)
        ps_list = res.scalars().all()
        for i, ps in enumerate(ps_list[:args.manual_seeds]):
            ps.swiss_seed = i + 1
        await session.commit()
        
    await season_service.advance_state("OPEN")
    
    # OPEN - generate all 4 matches per player upfront
    log("[OPEN] Generating Open matches (4 per player)...", verbose=True)

    async with db.get_session() as session:
        stmt = select(Event).where(Event.type == EventType.OPEN)
        ev = (await session.execute(stmt)).scalars().first()
        if not ev:
            ev = Event(name="S1 Open", type=EventType.OPEN, season=1, status=EventStatus.ACTIVE)
            session.add(ev)
            await session.commit()
        event_id = ev.id

    created, players, err = await pairing_service.generate_open(1)
    log(f"[OPEN] Created {created} matches for {players} players", verbose=args.verbose)

    await _play_all_matches(match_service, event_id, verbose=args.verbose, debug=args.debug)

    # Rebuild Open stats from the completed matches (also done per score).
    await match_service.sync_open_stats(1)

    # Log OPEN win distribution
    async with db.get_session() as session:
        stmt = select(PlayerSeason).where(PlayerSeason.season_number == 1)
        ps_all = (await session.execute(stmt)).scalars().all()
        unseeded = [ps for ps in ps_all if not ps.swiss_seed]
        win_counts = {w: sum(1 for ps in unseeded if ps.open_wins == w) for w in range(5)}
        log(f"[OPEN] Unseeded: {len(unseeded)} players, wins: {win_counts}", verbose=True)

    if args.until == "open":
        log("Done.")
        return
        
    await season_service.advance_state("PLAYIN")
    log("[PLAYIN] Starting Playins (prelim→main loop)...", verbose=True)

    gen = await playin_service.generate_playins(1)
    log(f"[PLAYIN] Initial: {gen.get('round1_matches', 0)} matches, {gen.get('round1_byes', 0)} byes", verbose=args.verbose)

    async with db.get_session() as session:
        stmt = select(Event).where(Event.type == EventType.PLAYIN)
        playin_ev = (await session.execute(stmt)).scalars().first()
        playin_event_id = playin_ev.id if playin_ev else None

    for _ in range(15):
        if playin_event_id:
            await _play_all_matches(match_service, playin_event_id, verbose=args.verbose, debug=args.debug)
        result = await playin_service.try_advance_after_score(1)
        if not result:
            break
        log(f"[PLAYIN] Advance: {result['action']} r{result.get('round', '?')} m:{result.get('matches', 0)} b:{result.get('byes', 0)}", verbose=args.verbose)
        if result.get("action") == "finished":
            break

    # Log PLAYIN qualification
    async with db.get_session() as session:
        stmt = select(PlayerSeason).where(PlayerSeason.season_number == 1)
        ps_all = (await session.execute(stmt)).scalars().all()
        qualified = [ps for ps in ps_all if ps.playin_rank and ps.playin_rank > 0]
        seeded = [ps for ps in ps_all if ps.swiss_seed and ps.swiss_seed > 0]
        log(f"[PLAYIN] Qualified: {len(qualified)}, Returning seeds: {len(seeded)}", verbose=True)

    if args.until == "playin":
        log("Done.")
        return
        
    await season_service.advance_state("SWISS")
    log("[SWISS] Starting Swiss Stage...", verbose=True)

    await pairing_service.generate_swiss_round(1, None)
    async with db.get_session() as session:
        stmt = select(Event).where(Event.type == EventType.SWISS)
        swiss_ev = (await session.execute(stmt)).scalars().first()
        swiss_event_id = swiss_ev.id if swiss_ev else None
    if swiss_event_id:
        # Each fully-scored round auto-creates the next one.
        await _play_all_matches(
            match_service, swiss_event_id, verbose=args.verbose, debug=args.debug
        )

    if args.until == "swiss":
        log("Done.")
        return
        
    await season_service.advance_state("PLAYOFF")
    log("[PLAYOFF] Starting Playoffs...", verbose=True)
    
    await pairing_service.generate_playoff_bracket(1)
    async with db.get_session() as session:
        stmt = select(Event).where(Event.type == EventType.PLAYOFF, Event.season == 1)
        playoff_ev = (await session.execute(stmt)).scalars().first()
        playoff_event_id = playoff_ev.id

    log("[PLAYOFF] Seeded bracket from Swiss record.", verbose=True)
    await _play_all_matches(match_service, playoff_event_id, verbose=args.verbose, debug=args.debug)
    
    log("[FINISH] Simulation complete.", verbose=True)


if __name__ == "__main__":
    asyncio.run(main_async())
