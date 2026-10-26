"""Discord-first tournament progression: status, sync, and staged actions."""

from typing import Any, Optional

from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    EventType,
    Match,
    MatchPlayer,
    Player,
    PlayerSeason,
    EventRepository,
    MatchRepository,
)
from bot.services.season import SeasonService, next_state
from bot.services.match import MatchService
from bot.services.playin import PlayinService
from bot.services.pairing import PairingService
from bot.rendering import prerender
from bot.rendering.StandingsMaker import _compute_playin_layout


def cut_notice(layout: dict) -> str:
    """Predicted-CUT warning for the Playin ladder, or an empty string."""
    cut = layout.get("cut_matches", 0)
    if not cut:
        return ""
    players = layout.get("cut_players") or cut * 2
    return (
        f"Predicted CUT: {cut} match(es) ({players} players) — "
        "R3 would overproduce the 32-player Swiss field."
    )


STAGE_EVENTS = {
    "OPEN": EventType.OPEN,
    "PLAYIN": EventType.PLAYIN,
    "SWISS": EventType.SWISS,
    "PLAYOFF": EventType.PLAYOFF,
}


class TournamentFlowService:
    def __init__(self):
        self.db = get_db_manager()
        self.season_service = SeasonService()
        self.match_service = MatchService()
        self.playin_service = PlayinService()
        self.pairing_service = PairingService()

    async def get_status(self) -> dict[str, Any]:
        """Summarize season state and the recommended next Discord action."""
        season = await self.season_service.get_current_season_number()
        state = await self.season_service.get_state()
        if not season:
            return {
                "season": None,
                "state": state,
                "next_action": "Run `/season create <N>` then `/season advance SIGNUP`.",
            }

        stage_counts: dict[str, dict[str, int]] = {}
        for stage, etype in (
            ("OPEN", EventType.OPEN),
            ("PLAYIN", EventType.PLAYIN),
            ("SWISS", EventType.SWISS),
            ("PLAYOFF", EventType.PLAYOFF),
        ):
            stage_counts[stage] = await self._event_match_counts(season, etype)

        async with self.db.get_session() as session:
            ps_all = (
                await session.execute(
                    select(PlayerSeason).where(PlayerSeason.season_number == season)
                )
            ).scalars().all()

        layout = _compute_playin_layout(ps_all)
        playin_rounds = await self._playin_round_counts(season)
        playin_started = await self._event_exists(season, EventType.PLAYIN)

        next_action = self._recommend_next(
            state, stage_counts, layout, playin_rounds, playin_started
        )

        return {
            "season": season,
            "state": state,
            "readiness": await self.readiness(season),
            "stages": stage_counts,
            "playin_layout": layout,
            "playin_cut_notice": cut_notice(layout),
            "playin_rounds": playin_rounds,
            "playin_started": playin_started,
            "next_action": next_action,
        }

    async def readiness(self, season: int) -> dict[str, Any]:
        """What blocks the next stage action. Shared by status, dashboard and gates."""
        state = await self.season_service.get_state()
        blockers: list[str] = []
        action = None
        if state == "SIGNUP":
            action = "start_open"
            async with self.db.get_session() as session:
                registered = (
                    await session.execute(
                        select(func.count()).select_from(PlayerSeason).where(
                            PlayerSeason.season_number == season
                        )
                    )
                ).scalar() or 0
            if registered < 2:
                blockers.append(f"Only {registered} player(s) registered.")
        elif state in ("OPEN", "PLAYIN", "SWISS"):
            action = {"OPEN": "start_playins", "PLAYIN": "start_swiss", "SWISS": "start_playoffs"}[state]
            counts = await self._event_match_counts(season, STAGE_EVENTS[state])
            if counts["total"] == 0 and state != "PLAYIN":
                blockers.append(f"No {state} matches generated yet.")
            if counts["unplayed"]:
                blockers.append(f"{counts['unplayed']} {state} match(es) unplayed.")
            if state == "PLAYIN" and not await self._event_exists(season, EventType.PLAYIN):
                blockers.append("Playin ladder not created yet.")
        elif state == "PLAYOFF":
            action = "close_season"
            counts = await self._event_match_counts(season, EventType.PLAYOFF)
            if counts["unplayed"]:
                blockers.append(f"{counts['unplayed']} playoff match(es) unplayed.")
        return {
            "state": state,
            "next_state": next_state(state),
            "action": action,
            "ready": action is not None and not blockers,
            "blockers": blockers,
        }

    async def sync_open_stats(self, season: int) -> dict[str, Any]:
        """Rebuild open_wins/losses/gd/hp from completed OPEN matches."""
        return await self.match_service.sync_open_stats(season)

    async def _advance(self, target: str) -> Optional[str]:
        """Advance to ``target`` after a successful generation.

        Returns the previous state when the stage actually changed, so callers
        know to clean up Discord roles; None when already in ``target``.
        """
        current = await self.season_service.get_state()
        if current == target:
            return None
        await self.season_service.advance_state(target)
        return current

    async def _require_state(self, allowed: tuple[str, ...]) -> Optional[dict[str, Any]]:
        state = await self.season_service.get_state()
        if state in allowed:
            return None
        return {
            "error": f"Cannot do that from state {state}. Need {' or '.join(allowed)}.",
            "state": state,
        }

    async def start_open(self, season: int) -> dict[str, Any]:
        """Generate the Open stage, then move SIGNUP → OPEN."""
        if err := await self._require_state(("SIGNUP", "OPEN")):
            return err

        existing = await self._event_match_counts(season, EventType.OPEN)
        if existing["total"]:
            return {
                "ok": True,
                "state": await self.season_service.get_state(),
                "advanced_from": await self._advance("OPEN"),
                "message": (
                    f"Open already has **{existing['total']}** matches "
                    f"({existing['unplayed']} unplayed); nothing regenerated."
                ),
            }

        matches, players, gen_err = await self.pairing_service.generate_open(season)
        if gen_err:
            return {"error": gen_err, "state": await self.season_service.get_state()}

        advanced_from = await self._advance("OPEN")
        return {
            "ok": True,
            "state": "OPEN",
            "advanced_from": advanced_from,
            "matches_created": matches,
            "players": players,
            "message": (
                f"Generated **{matches}** Open matches across **{players}** players. "
                "Each player has 4 opponents to play."
                + (" State → OPEN." if advanced_from else "")
            ),
        }

    async def start_playins(self, season: int) -> dict[str, Any]:
        """Sync Open stats, verify Open is done, create the Playin graph, then advance."""
        if err := await self._require_state(("OPEN", "PLAYIN")):
            return err

        sync = await self.sync_open_stats(season)
        if sync.get("error"):
            return sync

        open_counts = await self._event_match_counts(season, EventType.OPEN)
        if open_counts["total"] == 0:
            return {
                "error": "No Open matches. Run `/tourney generate_open` (or dashboard **Open**) first.",
            }
        if open_counts["unplayed"] > 0:
            return {
                "error": (
                    f"Open not finished: {open_counts['unplayed']} match(es) still unplayed. "
                    "Use `/list_matches` + `/set_score`, then try again."
                ),
                "open": open_counts,
                "sync": sync,
            }

        try:
            gen = await self.playin_service.generate_playins(season)
        except ValueError as e:
            return {"error": str(e), "sync": sync, "state": await self.season_service.get_state()}

        advanced_from = await self._advance("PLAYIN")
        # Open is finished now; keep its final table ready for `/open`.
        await prerender("OPEN", season)

        layout = gen.get("layout") or {}
        notice = cut_notice(layout)
        base = {
            "ok": True,
            "state": "PLAYIN",
            "advanced_from": advanced_from,
            "sync": sync,
            "cut_notice": notice,
        }
        if not any(gen.get("matches_by_round", {}).values()) and not any(
            gen.get("byes_by_round", {}).values()
        ):
            auto_count = layout.get("qualifiers", 0)
            return {
                **base,
                "matches_created": 0,
                "byes": 0,
                "matches_by_round": {},
                "byes_by_round": {},
                "message": (
                    f"No Playin ladder matches needed; {auto_count} "
                    "player(s) qualified for Swiss."
                    + (f"\n{notice}" if notice else "")
                ),
            }

        match_counts = gen.get("matches_by_round", {})
        bye_counts = gen.get("byes_by_round", {})
        rounds_text = ", ".join(
            f"R{rnd}: {match_counts.get(rnd, 0)} matches/{bye_counts.get(rnd, 0)} byes"
            for rnd in (1, 2, 3)
        )
        return {
            **base,
            "matches_created": gen["round1_matches"],
            "byes": gen["round1_byes"],
            "matches_by_round": match_counts,
            "byes_by_round": bye_counts,
            "message": (
                f"Playins ladder created ({rounds_text}). "
                "Known players are pre-seeded and future cards show placeholders. "
                "Just score the matches; Playins finalize automatically. "
                "A CUT round is added only if R3 overproduces. View: `/playins`."
                + (f"\n{notice}" if notice else "")
            ),
        }

    async def start_swiss(self, season: int, round_num: Optional[int] = None) -> dict[str, Any]:
        """Create the fixed Swiss ladder, then move PLAYIN → SWISS."""
        if err := await self._require_state(("PLAYIN", "SWISS")):
            return err
        try:
            matches = await self.pairing_service.generate_swiss_round(season, round_num)
        except ValueError as e:
            return {"error": str(e), "state": await self.season_service.get_state()}

        advanced_from = await self._advance("SWISS")
        if advanced_from:
            await prerender("PLAYIN", season)
        label = matches[0].series_game if matches else (round_num or 1)
        return {
            "ok": True,
            "state": "SWISS",
            "advanced_from": advanced_from,
            "matches": matches,
            "message": (
                f"Swiss bracket ready (66 matches). R{label}: **{len(matches)}** matches."
                + (" State → SWISS." if advanced_from else "")
            ),
        }

    async def start_playoffs(self, season: int) -> dict[str, Any]:
        """Seed the playoff bracket from Swiss, then move SWISS → PLAYOFF."""
        if err := await self._require_state(("SWISS", "PLAYOFF")):
            return err
        try:
            seeded = await self.pairing_service.generate_playoff_bracket(season)
        except ValueError as e:
            return {"error": str(e), "state": await self.season_service.get_state()}

        advanced_from = await self._advance("PLAYOFF")
        return {
            "ok": True,
            "state": "PLAYOFF",
            "advanced_from": advanced_from,
            "matches": seeded,
            "message": (
                f"Playoffs seeded: **{len(seeded)}** initial matches."
                + (" State → PLAYOFF." if advanced_from else "")
            ),
        }

    async def unplayed_discord_ids(self, season: int, stage: str) -> Optional[list[int]]:
        """Discord IDs with unplayed matches in ``stage``, in match order.

        None when the stage has no event (nothing generated yet).
        """
        etype = STAGE_EVENTS.get(stage)
        if not etype:
            return None
        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(etype, season)
            if not event:
                return None
            matches = await MatchRepository(session).get_unplayed_by_event(event.id)
        seen: dict[int, None] = {}
        for match in matches:
            for mp in match.match_players:
                if mp.player:
                    seen.setdefault(mp.player.discord_id, None)
        return list(seen)

    async def registered_discord_ids(self) -> set[int]:
        async with self.db.get_session() as session:
            return set((await session.execute(select(Player.discord_id))).scalars().all())

    async def _event_exists(self, season: int, event_type: EventType) -> bool:
        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(
                event_type, season
            )
            return event is not None

    async def _event_match_counts(self, season: int, event_type: EventType) -> dict[str, int]:
        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(event_type, season)
            if not event:
                return {"total": 0, "completed": 0, "unplayed": 0}
            total = (
                await session.execute(
                    select(func.count(Match.id)).where(Match.event_id == event.id)
                )
            ).scalar() or 0
            completed = (
                await session.execute(
                    select(func.count(Match.id)).where(
                        and_(Match.event_id == event.id, Match.completed_at.isnot(None))
                    )
                )
            ).scalar() or 0
            return {"total": total, "completed": completed, "unplayed": total - completed}

    async def _playin_round_counts(self, season: int) -> dict[int, dict[str, int]]:
        out: dict[int, dict[str, int]] = {
            rnd: {"total": 0, "unplayed": 0} for rnd in (1, 2, 3, 4)
        }
        async with self.db.get_session() as session:
            event = await EventRepository(session).get_by_type_and_season(EventType.PLAYIN, season)
            if not event:
                return out
            for rnd in (1, 2, 3, 4):
                total = (
                    await session.execute(
                        select(func.count(Match.id)).where(
                            and_(Match.event_id == event.id, Match.series_game == rnd)
                        )
                    )
                ).scalar() or 0
                unplayed = (
                    await session.execute(
                        select(func.count(Match.id)).where(
                            and_(
                                Match.event_id == event.id,
                                Match.series_game == rnd,
                                Match.completed_at.is_(None),
                            )
                        )
                    )
                ).scalar() or 0
                out[rnd] = {"total": total, "unplayed": unplayed}
        return out

    def _recommend_next(
        self,
        state: str,
        stages: dict[str, dict[str, int]],
        layout: dict,
        playin_rounds: dict[int, dict[str, int]],
        playin_started: bool = False,
    ) -> str:
        if state == "OFFSEASON":
            return "Advance to SIGNUP: `/season advance SIGNUP` or dashboard **▶ SIGNUP**."
        if state == "SIGNUP":
            return "Generate Open: `/tourney generate_open` or dashboard **Open**."
        if state == "OPEN":
            o = stages.get("OPEN", {})
            if o.get("total", 0) == 0:
                return "Generate Open: `/tourney generate_open`."
            if o.get("unplayed", 0) > 0:
                return (
                    f"Finish Open ({o['unplayed']} unplayed): `/list_matches` + `/set_score`, "
                    "then `/tourney sync_open` + `/tourney start_playins`."
                )
            return (
                "Start playins: `/tourney start_playins` (syncs Open + creates the "
                "whole ladder). R2/R3 are pre-created and fill in as you score. "
                "`/playins` updates live."
            )
        if state == "PLAYIN":
            if not any(
                playin_rounds.get(rnd, {}).get("total", 0)
                for rnd in (1, 2, 3, 4)
            ):
                if playin_started:
                    return (
                        "Playins complete. Start Swiss: `/tourney swiss_round`."
                    )
                return "Start playins: `/tourney start_playins`."
            for rnd in (1, 2, 3, 4):
                pr = playin_rounds.get(rnd, {})
                unplayed = pr.get("unplayed", 0)
                if unplayed > 0:
                    label = "cut" if rnd == 4 else f"R{rnd}"
                    return (
                        f"Score Playin {label} ({unplayed} left). "
                        "The bracket fills automatically as scores arrive."
                    )
            return (
                "Playins complete. Start Swiss: `/tourney swiss_round`."
            )
        if state == "SWISS":
            s = stages.get("SWISS", {})
            if s.get("total", 0) == 0:
                return "Start Swiss: `/tourney swiss_round`."
            if s.get("unplayed", 0) > 0:
                return (
                    f"Score Swiss ({s['unplayed']} unplayed). "
                    "Players advance to the next round as soon as they finish."
                )
            return "Generate playoffs: `/tourney generate_playoffs`."
        if state == "PLAYOFF":
            p = stages.get("PLAYOFF", {})
            if p.get("unplayed", 0) > 0:
                return f"Score playoffs ({p['unplayed']} unplayed). View: `/playoff`."
            return "Season complete — `/season create <N+1>` for next season."
        return "Check `/tourney status`."
