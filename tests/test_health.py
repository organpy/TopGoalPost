"""Doctor checks and backups."""

import sqlite3

from bot.services.health import HealthService, backup_database


async def test_missing_required_config_is_reported(db):
    from bot.config import Config

    await Config().set("score_channel", "123")
    checks = {c.name: c for c in await HealthService().check()}

    assert not checks["config `match_channel`"].ok
    assert checks["config `match_channel`"].icon == "❌"
    assert checks["config `score_channel`"].ok
    assert checks["config `moderator_role`"].icon == "⚠️"
    assert checks["database writable"].ok


async def test_backup_retention_keeps_newest(tmp_path):
    source = tmp_path / "bot.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")

    made = [await backup_database(source, tmp_path / "backups", keep=2) for _ in range(4)]

    remaining = sorted((tmp_path / "backups").glob("bot_*.db"))
    assert remaining == made[-2:]


async def test_doctor_command_lists_checks(db):
    from unittest.mock import AsyncMock, MagicMock

    from bot.cogs.admin.admin_cog import AdminCog

    interaction = MagicMock()
    interaction.guild = None
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    cog = AdminCog(MagicMock())
    await cog.doctor_cmd.callback(cog, interaction)

    text = interaction.followup.send.call_args[0][0]
    assert text.startswith("**Doctor:**")
    assert "❌ config `match_channel`" in text
