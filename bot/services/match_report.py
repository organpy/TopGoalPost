"""Durable score reports: a player reports, the opponent or a moderator resolves.

Reports live in the database so confirmation buttons keep working after a
restart. Only ``confirm`` applies the score, through ``MatchService``.
"""

from typing import Any, Optional

from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    Match,
    MatchPlayer,
    MatchReport,
    MatchReportRepository,
    MatchRepository,
    ReportStatus,
)
from bot.db.models import OPEN_REPORT_STATUSES
from bot.services.match import MatchService


class MatchReportService:
    def __init__(self):
        self.db = get_db_manager()
        self.match_service = MatchService()

    async def create_report(
        self,
        match_id: int,
        reporter_discord_id: int,
        p1_goals: int,
        p2_goals: int,
        winner_side: int,
    ) -> dict[str, Any]:
        """Store a pending report. Returns the report and the opponent to ping."""
        if p1_goals < 0 or p2_goals < 0:
            return {"error": "Scores cannot be negative."}
        if p1_goals == p2_goals:
            return {"error": "Ties are not allowed."}
        if (p1_goals > p2_goals) != (winner_side == 0):
            return {"error": "Winner must have more goals than the opponent."}
        if archived := await self.match_service._archived_error(match_id):
            return {"error": archived}

        async with self.db.get_session() as session:
            match = await MatchRepository(session).get_by_id(match_id)
            if not match:
                return {"error": "Match not found."}
            if match.completed_at:
                return {"error": f"Match {match_id} is already completed."}

            players = {
                mp.player.discord_id: mp.player for mp in match.match_players if mp.player
            }
            if reporter_discord_id not in players:
                return {"error": "You are not part of this match."}
            opponents = [d for d in players if d != reporter_discord_id]
            if not opponents:
                return {"error": "Could not find your opponent to confirm the score."}

            repo = MatchReportRepository(session)
            if existing := await repo.get_open_for_match(match_id):
                return {
                    "error": (
                        f"Match {match_id} already has a {existing.status.value} report "
                        f"(#{existing.id}). Wait for it to be resolved."
                    )
                }

            report = await repo.create(
                match_id,
                reporter_discord_id,
                p1_goals,
                p2_goals,
                winner_side,
                reporter_player_id=players[reporter_discord_id].id,
            )
            await session.commit()
            return {"report": report, "opponent_discord_id": opponents[0]}

    async def set_message(self, report_id: int, channel_id: int, message_id: int) -> None:
        async with self.db.get_session() as session:
            report = await MatchReportRepository(session).get(report_id)
            if report:
                report.channel_id = channel_id
                report.message_id = message_id
                await session.commit()

    async def get(self, report_id: int) -> Optional[MatchReport]:
        async with self.db.get_session() as session:
            return await MatchReportRepository(session).get(report_id)

    async def list_open(self) -> list[MatchReport]:
        async with self.db.get_session() as session:
            return await MatchReportRepository(session).list_open()

    async def participants(self, report: MatchReport) -> list[int]:
        """Discord IDs of both players in the reported match."""
        async with self.db.get_session() as session:
            match = await MatchRepository(session).get_by_id(report.match_id)
            if not match:
                return []
            return [mp.player.discord_id for mp in match.match_players if mp.player]

    async def describe(self, report: MatchReport) -> str:
        """Match label such as ``#12 OPEN: Alice vs Bob``."""
        async with self.db.get_session() as session:
            match = await session.get(
                Match, report.match_id,
                options=[
                    selectinload(Match.match_players).selectinload(MatchPlayer.player),
                    selectinload(Match.event),
                ],
            )
            if not match or len(match.match_players) < 2:
                return f"Match {report.match_id}"
            mps = sorted(match.match_players, key=lambda mp: mp.side)
            return self.match_service._format_match_label(match, mps)

    async def can_resolve(self, report: MatchReport, actor_id: int, is_mod: bool) -> bool:
        """The opponent of the reporter, or any moderator."""
        if is_mod:
            return True
        if actor_id == report.reporter_discord_id:
            return False
        return actor_id in await self.participants(report)

    async def _resolve(
        self,
        report_id: int,
        status: ReportStatus,
        actor_id: int,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        async with self.db.get_session() as session:
            repo = MatchReportRepository(session)
            report = await repo.get(report_id)
            if not report:
                return {"error": "Report not found."}
            if report.status not in OPEN_REPORT_STATUSES:
                return {"error": f"Report #{report_id} was already {report.status.value}."}
            await repo.resolve(report, status, actor_id, reason)
            await session.commit()
            return {"ok": True, "report": report}

    async def confirm(self, report_id: int, actor_id: int) -> dict[str, Any]:
        """Apply the reported score and close the report."""
        report = await self.get(report_id)
        if not report:
            return {"error": "Report not found."}
        if report.status not in OPEN_REPORT_STATUSES:
            return {"error": f"Report #{report_id} was already {report.status.value}."}

        async with self.db.get_session() as session:
            match = await MatchRepository(session).get_by_id(report.match_id)
            already_done = match is None or match.completed_at is not None
        if already_done:
            await self._resolve(
                report_id, ReportStatus.REJECTED, actor_id,
                "Match was completed before this report was confirmed.",
            )
            return {
                "error": (
                    f"Match {report.match_id} was already completed. "
                    "Ask an admin to correct it with `/set_score`."
                )
            }

        result = await self.match_service.report_score(
            report.match_id, report.p1_goals, report.p2_goals, report.winner_side
        )
        if "error" in result:
            await self._resolve(report_id, ReportStatus.REJECTED, actor_id, result["error"])
            return {"error": result["error"]}

        resolved = await self._resolve(report_id, ReportStatus.CONFIRMED, actor_id)
        resolved["result"] = result
        resolved["summary"] = await self.match_service.format_result_summary(report.match_id)
        return resolved

    async def reject(
        self, report_id: int, actor_id: int, reason: Optional[str] = None
    ) -> dict[str, Any]:
        return await self._resolve(report_id, ReportStatus.REJECTED, actor_id, reason)

    async def dispute(
        self, report_id: int, actor_id: int, reason: Optional[str] = None
    ) -> dict[str, Any]:
        """Flag for a moderator; the report stays open in the mod queue."""
        report = await self.get(report_id)
        if report and report.status == ReportStatus.DISPUTED:
            return {"error": f"Report #{report_id} is already disputed."}
        return await self._resolve(report_id, ReportStatus.DISPUTED, actor_id, reason)
