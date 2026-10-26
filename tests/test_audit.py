"""Audit log: destructive actions leave a trail with the acting user."""

import json

from bot.services import audit
from bot.services.audit import current_actor


async def _entries(action=None):
    return await audit.recent(50, action)


async def test_forced_transition_is_logged_with_actor(db):
    from bot.services.season import SeasonService

    token = current_actor.set(4242)
    try:
        await SeasonService().advance_state("SWISS", force=True)
    finally:
        current_actor.reset(token)

    [entry] = await _entries("stage")
    assert entry.action == "stage.force"
    assert entry.actor_id == 4242
    assert json.loads(entry.details) == {"previous": "OFFSEASON"}


async def test_score_edit_is_logged(db, seed_data, open_event):
    from bot.db import MatchRepository, MatchPlayerRepository
    from bot.services.match import MatchService

    async with db.get_session() as session:
        match = await MatchRepository(session).create(open_event.id)
        mp_repo = MatchPlayerRepository(session)
        await mp_repo.create(match.id, side=0, player_id=seed_data["players"][0].id)
        await mp_repo.create(match.id, side=1, player_id=seed_data["players"][1].id)
        await session.commit()

    svc = MatchService()
    await svc.report_score(match.id, 2, 0, 0)
    await svc.report_score(match.id, 0, 3, 1)

    actions = [e.action for e in await _entries("score")]
    assert actions == ["score.edit", "score.report"]
    assert "reset.downstream" in [e.action for e in await _entries("reset")]


async def test_archived_refusal_writes_nothing(db):
    from datetime import datetime
    from bot.db import Event, EventType, Match, MatchPlayer, Player, Season, SeasonStatus
    from bot.services.match import MatchService

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ARCHIVED))
        session.add(Season(number=2, status=SeasonStatus.ACTIVE))
        a, b = Player(discord_id=1, name="A"), Player(discord_id=2, name="B")
        session.add_all([a, b])
        event = Event(name="Open", type=EventType.OPEN, season=1)
        session.add(event)
        await session.flush()
        match = Match(event_id=event.id, completed_at=datetime.utcnow())
        session.add(match)
        await session.flush()
        session.add(MatchPlayer(match_id=match.id, side=0, player_id=a.id))
        session.add(MatchPlayer(match_id=match.id, side=1, player_id=b.id))
        await session.commit()

    result = await MatchService().report_score(match.id, 2, 0, 0)
    assert "archived" in result["error"]
    assert await _entries() == []


async def test_audit_command_lists_entries(db):
    from unittest.mock import AsyncMock, MagicMock

    from bot.cogs.admin.admin_cog import AdminCog

    await audit.record("config.set", "config", "score_channel", actor_id=7, value="123")
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()

    cog = AdminCog(MagicMock())
    await cog.audit_cmd.callback(cog, interaction, 15, None)

    text = interaction.response.send_message.call_args[0][0]
    assert "<@7> **config.set** config:score_channel" in text
