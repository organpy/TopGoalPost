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
