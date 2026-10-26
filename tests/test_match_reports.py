"""Durable score reports: repository and service behaviour."""

from bot.db import (
    DatabaseManager,
    EventRepository,
    EventType,
    MatchReportRepository,
    MatchRepository,
    ReportStatus,
)


async def _match(session) -> int:
    event = await EventRepository(session).create("Open", EventType.OPEN, 1)
    match = await MatchRepository(session).create(event.id)
    return match.id


async def test_report_survives_reconnect(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}"
    first = DatabaseManager(url)
    await first.init()
    await first.create_tables()
    async with first.get_session() as session:
        match_id = await _match(session)
        report = await MatchReportRepository(session).create(match_id, 42, 3, 1, 0)
        await session.commit()
        report_id = report.id
    await first.close()

    second = DatabaseManager(url)
    await second.init()
    async with second.get_session() as session:
        stored = await MatchReportRepository(session).get(report_id)
    await second.close()

    assert stored.status == ReportStatus.PENDING
    assert (stored.p1_goals, stored.p2_goals, stored.winner_side) == (3, 1, 0)


async def test_open_report_lookup_and_resolve(db):
    async with db.get_session() as session:
        repo = MatchReportRepository(session)
        match_id = await _match(session)
        report = await repo.create(match_id, 42, 2, 0, 0)

        assert (await repo.get_open_for_match(match_id)).id == report.id
        assert [r.id for r in await repo.list_open()] == [report.id]

        await repo.resolve(report, ReportStatus.REJECTED, resolved_by=7, reason="wrong")
        await session.commit()

        assert await repo.get_open_for_match(match_id) is None
        assert report.resolved_at is not None
        assert report.reason == "wrong"


async def test_reports_deleted_with_match(db):
    from sqlalchemy import select
    from bot.db import Match, MatchReport

    async with db.get_session() as session:
        match_id = await _match(session)
        await MatchReportRepository(session).create(match_id, 42, 2, 0, 0)
        await session.commit()

        await session.delete(await session.get(Match, match_id))
        await session.commit()
        remaining = (await session.execute(select(MatchReport))).scalars().all()
    assert remaining == []


async def _scheduled_match(db, season_status=None):
    from bot.db import Player, MatchPlayer, Season, SeasonStatus

    async with db.get_session() as session:
        session.add(Season(number=1, status=season_status or SeasonStatus.ACTIVE))
        if season_status == SeasonStatus.ARCHIVED:
            session.add(Season(number=2, status=SeasonStatus.ACTIVE))
        p1 = Player(discord_id=111, name="Alice", country="US")
        p2 = Player(discord_id=222, name="Bob", country="US")
        session.add_all([p1, p2])
        await session.flush()
        match_id = await _match(session)
        session.add(MatchPlayer(match_id=match_id, side=0, player_id=p1.id))
        session.add(MatchPlayer(match_id=match_id, side=1, player_id=p2.id))
        await session.commit()
        return match_id


async def _completed(db, match_id: int) -> bool:
    from bot.db import Match

    async with db.get_session() as session:
        return (await session.get(Match, match_id)).completed_at is not None


class TestMatchReportService:
    async def test_confirm_applies_score(self, db):
        from bot.services.match_report import MatchReportService

        svc = MatchReportService()
        match_id = await _scheduled_match(db)
        created = await svc.create_report(match_id, 111, 3, 1, 0)
        assert created["opponent_discord_id"] == 222

        result = await svc.confirm(created["report"].id, 222)

        assert result["ok"] is True
        assert await _completed(db, match_id)
        assert (await svc.get(created["report"].id)).status == ReportStatus.CONFIRMED

    async def test_reject_and_dispute_do_not_apply(self, db):
        from bot.services.match_report import MatchReportService

        svc = MatchReportService()
        match_id = await _scheduled_match(db)
        first = (await svc.create_report(match_id, 111, 3, 1, 0))["report"]
        await svc.reject(first.id, 222)
        assert (await svc.get(first.id)).status == ReportStatus.REJECTED

        second = (await svc.create_report(match_id, 111, 3, 1, 0))["report"]
        await svc.dispute(second.id, 222)
        assert (await svc.get(second.id)).status == ReportStatus.DISPUTED
        assert not await _completed(db, match_id)

    async def test_one_open_report_per_match(self, db):
        from bot.services.match_report import MatchReportService

        svc = MatchReportService()
        match_id = await _scheduled_match(db)
        await svc.create_report(match_id, 111, 3, 1, 0)
        again = await svc.create_report(match_id, 222, 0, 2, 1)
        assert "already has a pending report" in again["error"]

    async def test_archived_match_report_rejected(self, db):
        from bot.db import SeasonStatus
        from bot.services.match_report import MatchReportService

        match_id = await _scheduled_match(db, SeasonStatus.ARCHIVED)
        created = await MatchReportService().create_report(match_id, 111, 3, 1, 0)
        assert created["error"] == "Season 1 is archived and read-only."

    async def test_only_opponent_or_mod_can_resolve(self, db):
        from bot.services.match_report import MatchReportService

        svc = MatchReportService()
        match_id = await _scheduled_match(db)
        report = (await svc.create_report(match_id, 111, 3, 1, 0))["report"]
        assert await svc.can_resolve(report, 222, is_mod=False)
        assert not await svc.can_resolve(report, 111, is_mod=False)
        assert not await svc.can_resolve(report, 999, is_mod=False)
        assert await svc.can_resolve(report, 999, is_mod=True)


async def test_button_rebuilt_from_custom_id_resolves_report(db):
    """Simulates a restart: only the custom_id survives, the DB has the rest."""
    import re
    from unittest.mock import AsyncMock, MagicMock

    from bot.services.match_report import MatchReportService
    from bot.ui.report import ReportActionButton, score_confirm_view

    match_id = await _scheduled_match(db)
    report = (await MatchReportService().create_report(match_id, 111, 3, 1, 0))["report"]
    custom_id = score_confirm_view(report.id).children[0].custom_id
    assert custom_id == f"matchreport:confirm:{report.id}"

    match = re.fullmatch(ReportActionButton.__discord_ui_compiled_template__, custom_id)
    button = await ReportActionButton.from_custom_id(MagicMock(), MagicMock(), match)

    interaction = MagicMock()
    interaction.user.id = 222
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.message.edit = AsyncMock()
    assert await button.interaction_check(interaction)
    await button.callback(interaction)

    assert await _completed(db, match_id)
    assert "confirmed" in interaction.message.edit.call_args.kwargs["content"]
