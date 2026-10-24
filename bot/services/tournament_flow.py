"""Discord-first tournament progression: status, sync, and staged actions."""

from typing import Any, Optional

from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    EventType,
    Match,
    MatchPlayer,
    PlayerSeason,
    EventRepository,
    MatchRepository,
)
from bot.services.season import SeasonService, STATE_ORDER
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
            "stages": stage_counts,
            "playin_layout": layout,
            "playin_cut_notice": cut_notice(layout),
            "playin_rounds": playin_rounds,
            "playin_started": playin_started,
            "next_action": next_action,
        }

    async def sync_open_stats(self, season: int) -> dict[str, Any]:
        """Rebuild open_wins/losses/gd/hp from completed OPEN matches."""
        return await self.match_service.sync_open_stats(season)

    async def start_playins(self, season: int) -> dict[str, Any]:
        """Sync Open stats, verify Open is done, and create the Playin graph."""
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

        state = await self.season_service.get_state()
        if state == "OPEN":
            await self.season_service.advance_state("PLAYIN")
        elif state != "PLAYIN":
            return {
                "error": f"Cannot start playins from state {state}. Need OPEN or PLAYIN.",
            }

        try:
            gen = await self.playin_service.generate_playins(season)
            matches_created = gen["round1_matches"]
            byes = gen["round1_byes"]
        except ValueError as e:
            return {"error": str(e), "sync": sync}

        # Open is finished now; keep its final table ready for `/open`.
        await prerender("OPEN", season)

        if not any(gen.get("matches_by_round", {}).values()) and not any(
            gen.get("byes_by_round", {}).values()
        ):
            layout = gen.get("layout") or {}
            auto_count = layout.get("qualifiers", 0)
            notice = cut_notice(layout)
            return {
                "ok": True,
                "state": "PLAYIN",
                "matches_created": 0,
                "byes": 0,
                "matches_by_round": {},
                "byes_by_round": {},
                "sync": sync,
                "cut_notice": notice,
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
        notice = cut_notice(gen.get("layout") or {})
        return {
            "ok": True,
            "state": "PLAYIN",
            "matches_created": matches_created,
            "byes": byes,
            "matches_by_round": match_counts,
            "byes_by_round": bye_counts,
            "sync": sync,
            "cut_notice": notice,
            "message": (
                f"Playins ladder created ({rounds_text}). "
                "Known players are pre-seeded and future cards show placeholders. "
                "Just score the matches; Playins finalize automatically. "
                "A CUT round is added only if R3 overproduces. View: `/playins`."
                + (f"\n{notice}" if notice else "")
            ),
        }

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
