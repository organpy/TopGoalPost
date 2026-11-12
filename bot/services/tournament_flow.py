"""Discord-first tournament progression: status, sync, and staged actions."""

from typing import Any, Optional, Sequence

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
from bot.services import audit
from bot.services.season import SeasonService, next_state
from bot.services.season_results import get_season_result
from bot.services.match import MatchService
from bot.services.playin import (
    PlayinService, clear_redraw, compute_playin_math, generate_redraw, get_redraw,
    plan_redraw,
)
from bot.services.pairing import PairingService, SWISS_FIELD_SIZE
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

        redraw = await get_redraw(season)
        readiness = await self.readiness(season)
        next_action = self._recommend_next(
            state, stage_counts, layout, playin_rounds, playin_started,
            ready=readiness["ready"],
        )

        return {
            "season": season,
            "state": state,
            "readiness": readiness,
            "stages": stage_counts,
            "playin_layout": layout,
            "playin_cut_notice": "" if redraw else cut_notice(layout),
            "playin_redraw": redraw,
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
            if state == "PLAYIN" and not blockers:
                async with self.db.get_session() as session:
                    field = len(await self.pairing_service._swiss_eligible(session, season))
                if field < SWISS_FIELD_SIZE:
                    blockers.append(
                        f"Only {field}/{SWISS_FIELD_SIZE} Swiss players. "
                        "Fill the rest with dashboard **Swiss Preview**, or "
                        "`/tourney redraw_playins`."
                    )
        elif state == "PLAYOFF":
            action = "close_season"
            counts = await self._event_match_counts(season, EventType.PLAYOFF)
            if counts["total"] == 0:
                blockers.append("No PLAYOFF matches generated yet.")
            elif counts["unplayed"]:
                blockers.append(f"{counts['unplayed']} playoff match(es) unplayed.")
            else:
                # Same rule `/season close` applies: the grand final decides it.
                async with self.db.get_session() as session:
                    result = await get_season_result(session, season)
                if result.champion is None:
                    blockers.append("The grand final is not decided yet.")
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

    async def start_open(self, season: int, regenerate: bool = False) -> dict[str, Any]:
        """Generate the Open stage, then move SIGNUP → OPEN.

        ``regenerate`` deletes every existing Open match (played ones too)
        and builds the pairings again.
        """
        if err := await self._require_state(("SIGNUP", "OPEN")):
            return err

        existing = await self._event_match_counts(season, EventType.OPEN)
        if existing["total"] and regenerate:
            await self.pairing_service.delete_open(season)
            # Old results fed open_wins/hp live; reset them so nobody is left out.
            await self.sync_open_stats(season)
            await audit.record(
                "stage.open_regenerate", "season", season,
                deleted=existing["total"], played=existing["completed"],
            )
        elif existing["total"]:
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

    async def playin_shortfall(self, season: int) -> Optional[dict[str, Any]]:
        """Numbers for a regular Playin ladder that cannot fill Swiss, or None.

        Withdrawn players are counted out (they would only forfeit).
        """
        async with self.db.get_session() as session:
            all_ps = (
                await session.execute(
                    select(PlayerSeason).where(PlayerSeason.season_number == season)
                )
            ).scalars().all()
        math = compute_playin_math(list(all_ps))
        produced = min(math["base_qualifiers"], math["target"])
        short_by = math["target"] - produced
        if short_by <= 0:
            return None
        active = [ps for ps in all_ps if not ps.withdrawn and not (ps.swiss_seed or 0) > 0]
        wins = {w: sum(1 for ps in active if min(ps.open_wins or 0, 4) == w) for w in range(5)}
        return {
            "seeds": math["returning_seeds"],
            "target": math["target"],
            "produced": produced,
            "field": math["returning_seeds"] + produced,
            "short_by": short_by,
            "wins": wins,
            "withdrawn": sum(1 for ps in all_ps if ps.withdrawn),
        }

    @staticmethod
    def shortfall_text(short: dict[str, Any]) -> str:
        w = short["wins"]
        return (
            f"⚠️ Regular Playins would only fill **{short['field']}/{SWISS_FIELD_SIZE}** "
            f"Swiss (short by **{short['short_by']}**).\n"
            f"Seeds {short['seeds']} · 4W {w[4]} · 3W {w[3]} · 2W {w[2]} · "
            f"1W {w[1]} · 0W {w[0]} · withdrawn {short['withdrawn']}\n"
            "**Regular anyway**: survival ladder (3 rounds + CUT); fill the gap "
            "later with dashboard **Swiss Preview**.\n"
            "**Single-elim redraw**: 2+ win players go straight through and the "
            "1-win players play a knockout for the rest."
        )

    async def start_playins(self, season: int, allow_short: bool = False) -> dict[str, Any]:
        """Sync Open stats, verify Open is done, create the Playin graph, then advance.

        If the regular ladder cannot fill Swiss, nothing is created unless
        ``allow_short``; the result carries ``short`` numbers and a redraw
        preview so the host can choose.
        """
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

        playin_counts = await self._event_match_counts(season, EventType.PLAYIN)
        if not allow_short and not playin_counts["total"] and not await get_redraw(season):
            short = await self.playin_shortfall(season)
            if short:
                redraw = await self.preview_playin_redraw(season)
                text = self.shortfall_text(short)
                return {
                    "error": (
                        text + "\nUse `/tourney start_playins` to choose, or "
                        "`/tourney redraw_playins`."
                    ),
                    "short": short,
                    "short_text": text,
                    "redraw_preview": redraw,
                    "state": await self.season_service.get_state(),
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

    async def finish_playins(self, season: int) -> dict[str, Any]:
        """Manual Playin finalization; only meaningful during PLAYIN."""
        if err := await self._require_state(("PLAYIN",)):
            return err
        try:
            await self.playin_service.finish_playins(season)
        except ValueError as e:
            return {"error": str(e)}
        return {
            "ok": True,
            "message": "Playins finalized. Start Swiss with `/tourney swiss_round`.",
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

    async def _redraw_plan(
        self,
        season: int,
        min_wins: int = 2,
        pull_back: Sequence[str] = (),
        pull_back_worst: int = 0,
    ) -> dict[str, Any]:
        """Validate state and build a redraw plan (nothing is written except
        an Open stats sync)."""
        if err := await self._require_state(("OPEN", "PLAYIN")):
            return err
        state = await self.season_service.get_state()
        if (await self._event_match_counts(season, EventType.SWISS))["total"]:
            return {"error": "The Swiss bracket already exists.", "state": state}
        open_counts = await self._event_match_counts(season, EventType.OPEN)
        if open_counts["total"] == 0:
            return {"error": "No Open matches yet; nothing to draw Playins from.", "state": state}
        if open_counts["unplayed"]:
            return {
                "error": f"Open not finished: {open_counts['unplayed']} match(es) unplayed.",
                "state": state,
            }
        sync = await self.sync_open_stats(season)
        if sync.get("error"):
            return {**sync, "state": state}

        from bot.services.signup import find_player

        async with self.db.get_session() as session:
            all_ps = (
                await session.execute(
                    select(PlayerSeason)
                    .options(selectinload(PlayerSeason.player))
                    .where(PlayerSeason.season_number == season)
                )
            ).scalars().all()
            errors: list[str] = []
            pull_ids: list[int] = []
            for raw in pull_back:
                if not raw.strip():
                    continue
                player = await find_player(session, raw)
                if player is None:
                    errors.append(f"`{raw}`: player not found.")
                else:
                    pull_ids.append(player.id)
        if errors:
            return {"error": "\n".join(errors), "state": state}

        plan = plan_redraw(
            list(all_ps),
            min_wins=min_wins,
            pull_back_ids=pull_ids,
            pull_back_worst=max(0, pull_back_worst),
        )
        names = {
            ps.player_id: (ps.player.name if ps.player else f"Player {ps.player_id}")
            for ps in all_ps
        }
        stats = {ps.player_id: ps for ps in all_ps}
        playin = await self._event_match_counts(season, EventType.PLAYIN)
        return {**plan, "state": state, "names": names, "stats": stats, "existing": playin}

    @staticmethod
    def _redraw_signature(plan: dict[str, Any]) -> list:
        return [plan["direct"], plan["contestants"], plan["pulled_back"]]

    @staticmethod
    def _redraw_text(plan: dict[str, Any]) -> str:
        names, stats = plan["names"], plan["stats"]

        def who(pid: int) -> str:
            ps = stats.get(pid)
            rec = f"{ps.open_wins or 0}W {ps.open_gd or 0:+d}" if ps else ""
            return f"{names.get(pid, pid)} ({rec})"

        def side(s: list, nodes: list) -> str:
            if s[0] == "p":
                return names.get(s[1], str(s[1]))
            slot = nodes[s[1]]["slot"].replace("P-", "").replace("-M", "-")
            return f"W {slot}"

        lines = [
            f"Swiss: {plan['seeds']} seeds + {len(plan['direct'])} direct "
            f"({plan['min_wins']}+ Open wins) · **{plan['slots']}** place(s) to play for.",
        ]
        if plan["pulled_back"]:
            lines.append("Pulled back: " + ", ".join(names.get(p, str(p)) for p in plan["pulled_back"]))
        lines.append(
            f"Contestants ({len(plan['contestants'])}): "
            + ", ".join(who(p) for p in plan["contestants"])
        )
        if plan.get("free"):
            lines.append("Through without playing: " + ", ".join(names.get(p, str(p)) for p in plan["free"]))
        nodes = plan["nodes"]
        lines.append(f"**{plan['rounds']}** round(s), **{len(nodes)}** match(es):")
        for node in nodes:
            label = node["slot"].replace("P-", "").replace("-M", "-")
            final = " → Swiss" if node["round"] == plan["rounds"] else ""
            lines.append(f"`{label}` {side(node['sides'][0], nodes)} vs {side(node['sides'][1], nodes)}{final}")
        existing = plan.get("existing") or {}
        if existing.get("total"):
            lines.append(
                f"⚠️ Deletes the current Playin ladder: {existing['total']} match(es), "
                f"{existing['completed']} already played."
            )
        text = "\n".join(lines)
        return text if len(text) <= 1800 else text[:1790] + "\n…"

    async def preview_playin_redraw(
        self,
        season: int,
        min_wins: int = 2,
        pull_back: Sequence[str] = (),
        pull_back_worst: int = 0,
    ) -> dict[str, Any]:
        """Describe the redraw ladder without creating it."""
        plan = await self._redraw_plan(season, min_wins, pull_back, pull_back_worst)
        if plan.get("error"):
            return {k: plan[k] for k in ("error", "state", "needed") if k in plan}
        return {
            "ok": True,
            "state": plan["state"],
            "signature": self._redraw_signature(plan),
            "slots": plan["slots"],
            "rounds": plan["rounds"],
            "matches": len(plan["nodes"]),
            "message": self._redraw_text(plan),
        }

    async def redraw_playins(
        self,
        season: int,
        min_wins: int = 2,
        pull_back: Sequence[str] = (),
        pull_back_worst: int = 0,
        expected: Optional[list] = None,
    ) -> dict[str, Any]:
        """Replace Playins with a redraw ladder (see ``plan_redraw``).

        Deletes any existing Playin matches, sends the direct players through,
        and creates the knockout paths for the remaining places. From OPEN it
        also advances to PLAYIN. ``expected`` is the preview's signature; a
        mismatch means the field changed and nothing is done.
        """
        plan = await self._redraw_plan(season, min_wins, pull_back, pull_back_worst)
        if plan.get("error"):
            return {k: plan[k] for k in ("error", "state", "needed") if k in plan}
        if expected is not None and self._redraw_signature(plan) != expected:
            return {
                "error": "The field changed since the preview. Run the redraw again.",
                "state": plan["state"],
            }

        existing = plan["existing"]
        await clear_redraw(season)
        await self.match_service._delete_event_matches(season, EventType.PLAYIN)
        await self.match_service._clear_playin_ranks(season)
        try:
            by_round = await generate_redraw(season, plan)
        except ValueError as e:
            return {"error": str(e), "state": plan["state"]}
        await audit.record(
            "stage.playin_redraw", "season", season,
            deleted=existing["total"], played=existing["completed"],
            direct=len(plan["direct"]), slots=plan["slots"],
            pulled_back=plan["pulled_back"], contestants=plan["contestants"],
        )

        advanced_from = await self._advance("PLAYIN")
        if advanced_from:
            await prerender("OPEN", season)
        if not plan["nodes"]:
            # Everyone got a free pass; nothing to play.
            await self.playin_service.finish_playins(season)
        else:
            await prerender("PLAYIN", season)
        rounds_text = ", ".join(f"R{r}: {n}" for r, n in sorted(by_round.items()))
        return {
            "ok": True,
            "state": "PLAYIN",
            "advanced_from": advanced_from,
            "matches_by_round": by_round,
            "message": (
                f"Playins redrawn: {len(plan['direct'])} direct, "
                f"**{plan['slots']}** place(s) from {len(plan['contestants'])} contestants "
                f"({rounds_text or 'no matches'}). View: `/playins`."
                + (" State → PLAYIN." if advanced_from else "")
            ),
        }

    async def _swiss_fill_context(self, season: int) -> dict[str, Any]:
        """Check that a Swiss field can be previewed/filled right now.

        From OPEN this means skipping Playins (Open must be finished); from
        PLAYIN every Playin match must be scored. Returns ``{"error"}`` or
        ``{"state", "skip_playins"}``.
        """
        if err := await self._require_state(("OPEN", "PLAYIN")):
            return err
        state = await self.season_service.get_state()
        if (await self._event_match_counts(season, EventType.SWISS))["total"]:
            return {"error": "The Swiss bracket already exists.", "state": state}
        if state == "OPEN":
            open_counts = await self._event_match_counts(season, EventType.OPEN)
            if open_counts["total"] == 0:
                return {"error": "No Open matches yet; nothing to seed Swiss from.", "state": state}
            if open_counts["unplayed"]:
                return {
                    "error": f"Open not finished: {open_counts['unplayed']} match(es) unplayed.",
                    "state": state,
                }
            sync = await self.sync_open_stats(season)
            if sync.get("error"):
                return {**sync, "state": state}
        else:
            unplayed = (await self._event_match_counts(season, EventType.PLAYIN))["unplayed"]
            if unplayed:
                return {
                    "error": (
                        f"{unplayed} Playin match(es) unplayed. Score them "
                        "(or Finish Playins) before filling Swiss."
                    ),
                    "state": state,
                }
        return {"state": state, "skip_playins": state == "OPEN"}

    async def _swiss_pool(self, session, season: int) -> tuple[list[int], list[PlayerSeason]]:
        """(already-qualified player IDs in seed order, every registered PlayerSeason)."""
        qualified = await self.pairing_service._swiss_eligible(session, season)
        all_ps = (
            await session.execute(
                select(PlayerSeason)
                .options(selectinload(PlayerSeason.player))
                .where(PlayerSeason.season_number == season)
            )
        ).scalars().all()
        return qualified, list(all_ps)

    @staticmethod
    def _fill_order(ps: PlayerSeason) -> tuple:
        """Best Open finishers first: wins, then goal difference."""
        return (-(ps.open_wins or 0), -(ps.open_gd or 0), -(ps.hp or 0), ps.player_id)

    async def preview_swiss(self, season: int, picks: Sequence[str] = ()) -> dict[str, Any]:
        """Show the 32-player Swiss field before it is created.

        Seeds and Playin qualifiers come first, then ``picks`` (names, IDs or
        mentions chosen by the host), then the best remaining Open finishers
        until the field is full. From OPEN this skips Playins entirely.
        Nothing is written; pass ``fill_ids`` to :meth:`start_swiss_filled`.
        """
        ctx = await self._swiss_fill_context(season)
        if ctx.get("error"):
            return ctx

        from bot.services.signup import find_player

        async with self.db.get_session() as session:
            qualified, all_ps = await self._swiss_pool(session, season)
            by_id = {ps.player_id: ps for ps in all_ps}
            taken = set(qualified)
            errors: list[str] = []
            pick_ids: list[int] = []
            for raw in picks:
                if not raw.strip():
                    continue
                player = await find_player(session, raw)
                ps = by_id.get(player.id) if player else None
                if player is None:
                    errors.append(f"`{raw}`: player not found.")
                elif ps is None:
                    errors.append(f"`{raw}`: **{player.name}** is not registered this season.")
                elif ps.withdrawn:
                    errors.append(f"`{raw}`: **{player.name}** has withdrawn.")
                elif player.id in taken:
                    errors.append(f"`{raw}`: **{player.name}** is already in the field.")
                else:
                    taken.add(player.id)
                    pick_ids.append(player.id)

        slots = max(0, SWISS_FIELD_SIZE - len(qualified))
        if not errors and len(pick_ids) > slots:
            errors.append(
                f"{len(pick_ids)} players picked but only {slots} open slot(s) remain."
            )
        if errors:
            return {"error": "\n".join(errors), "state": ctx["state"]}

        candidates = sorted(
            (ps for ps in all_ps if ps.player_id not in taken and not ps.withdrawn),
            key=self._fill_order,
        )
        auto_ids = [ps.player_id for ps in candidates[: slots - len(pick_ids)]]

        sources = (
            [(pid, "seed" if (by_id[pid].swiss_seed or 0) > 0 else "playin") for pid in qualified]
            + [(pid, "pick") for pid in pick_ids]
            + [(pid, "auto") for pid in auto_ids]
        )
        field = [
            {
                "seed": n,
                "player_id": pid,
                "name": by_id[pid].player.name if by_id[pid].player else f"Player {pid}",
                "source": source,
                "open_wins": by_id[pid].open_wins or 0,
                "open_gd": by_id[pid].open_gd or 0,
            }
            for n, (pid, source) in enumerate(sources, start=1)
        ]
        short = SWISS_FIELD_SIZE - len(field)
        return {
            "ok": True,
            "state": ctx["state"],
            "skip_playins": ctx["skip_playins"],
            "field": field,
            "qualified": len(qualified),
            "fill_ids": pick_ids + auto_ids,
            "short": short,
            "message": (
                f"Swiss field: {len(qualified)} qualified + {len(pick_ids)} picked + "
                f"{len(auto_ids)} auto-filled = **{len(field)}/{SWISS_FIELD_SIZE}**."
                + (f" Still **{short}** short; nobody left to fill." if short else "")
            ),
        }

    async def start_swiss_filled(self, season: int, fill_ids: Sequence[int]) -> dict[str, Any]:
        """Create Swiss with ``fill_ids`` topping the field up to 32.

        Fillers get Playin ranks after the existing qualifiers, so their
        seeds follow the preview order. From OPEN this skips Playins (forced
        OPEN → SWISS, audited). Generation must succeed before the stage moves.
        """
        ctx = await self._swiss_fill_context(season)
        if ctx.get("error"):
            return ctx
        fill_ids = list(fill_ids)

        async with self.db.get_session() as session:
            qualified, all_ps = await self._swiss_pool(session, season)
            by_id = {ps.player_id: ps for ps in all_ps}
            stale = (
                len(qualified) + len(fill_ids) != SWISS_FIELD_SIZE
                or len(set(fill_ids)) != len(fill_ids)
                or any(pid in qualified for pid in fill_ids)
                or any(pid not in by_id or by_id[pid].withdrawn for pid in fill_ids)
            )
            if stale:
                return {
                    "error": "The Swiss field changed since the preview. Open the preview again.",
                    "state": ctx["state"],
                }
            next_rank = max((ps.playin_rank or 0) for ps in all_ps) + 1
            previous = {pid: by_id[pid].playin_rank for pid in fill_ids}
            for offset, pid in enumerate(fill_ids):
                by_id[pid].playin_rank = next_rank + offset
            await session.commit()

        try:
            matches = await self.pairing_service.generate_swiss_round(season, 1)
        except ValueError as e:
            async with self.db.get_session() as session:
                for pid, rank in previous.items():
                    ps = await session.get(PlayerSeason, (pid, season))
                    if ps:
                        ps.playin_rank = rank
                await session.commit()
            return {"error": str(e), "state": ctx["state"]}

        await audit.record(
            "stage.swiss_fill", "season", season,
            filled=fill_ids, skip_playins=ctx["skip_playins"],
        )
        if ctx["skip_playins"]:
            await self.season_service.advance_state("SWISS", force=True)
            advanced_from = "OPEN"
            await prerender("OPEN", season)
        else:
            advanced_from = await self._advance("SWISS")
            if advanced_from:
                await prerender("PLAYIN", season)
        return {
            "ok": True,
            "state": "SWISS",
            "advanced_from": advanced_from,
            "matches": matches,
            "filled": len(fill_ids),
            "message": (
                f"Swiss bracket ready (66 matches). R1: **{len(matches)}** matches. "
                f"{len(fill_ids)} player(s) filled in."
                + (" Playins skipped." if ctx["skip_playins"] else "")
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

    async def unplayed_matchups(
        self, season: int, stage: str
    ) -> Optional[list[tuple[tuple[int, str], tuple[int, str]]]]:
        """Unplayed matches in ``stage`` with both players known, as
        ``((discord_id, name), (discord_id, name))`` pairs in match order.

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
        pairs = []
        for match in sorted(matches, key=lambda m: m.id):
            players = [mp.player for mp in sorted(match.match_players, key=lambda mp: mp.side)]
            if len(players) == 2 and all(players):
                pairs.append(tuple((p.discord_id, p.name) for p in players))
        return pairs

    async def stage_discord_ids(self, season: int, stage: str) -> Optional[set[int]]:
        """Everyone taking part in ``stage`` (played or not), for stage roles.

        SIGNUP means everyone registered this season. None when the stage has
        no event yet. Name-only placeholders (negative IDs) are skipped.
        """
        async with self.db.get_session() as session:
            if stage == "SIGNUP":
                stmt = (
                    select(Player.discord_id)
                    .join(PlayerSeason, PlayerSeason.player_id == Player.id)
                    .where(PlayerSeason.season_number == season)
                )
            else:
                etype = STAGE_EVENTS.get(stage)
                if not etype:
                    return None
                event = await EventRepository(session).get_by_type_and_season(etype, season)
                if not event:
                    return None
                stmt = (
                    select(Player.discord_id)
                    .join(MatchPlayer, MatchPlayer.player_id == Player.id)
                    .join(Match, Match.id == MatchPlayer.match_id)
                    .where(Match.event_id == event.id)
                )
            ids = (await session.execute(stmt)).scalars().all()
        return {discord_id for discord_id in ids if discord_id > 0}

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
        ready: bool = False,
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
                "`/playins` updates live. To skip Playins, use dashboard "
                "**Swiss Preview**."
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
            if p.get("total", 0) == 0:
                return "Generate playoffs: `/tourney generate_playoffs`."
            if p.get("unplayed", 0) > 0:
                return f"Score playoffs ({p['unplayed']} unplayed). View: `/playoff`."
            if not ready:
                return "The grand final is not decided yet. View: `/playoff`."
            return "Grand final decided — close it with `/season close`."
        return "Check `/tourney status`."
