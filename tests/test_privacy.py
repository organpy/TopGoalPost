"""Avatars are mirrored only for registered players; image commands are rate-limited."""

from unittest.mock import AsyncMock, MagicMock

from bot.cogs.admin.admin_cog import AdminCog


def _user(discord_id: int, avatar: str):
    user = MagicMock()
    user.id = discord_id
    user.display_avatar = MagicMock(name=avatar)
    user.display_avatar.save = AsyncMock()
    return user


async def test_unregistered_user_avatar_is_not_saved(db):
    cog = AdminCog(MagicMock())
    after = _user(999, "new")
    await cog.on_user_update(_user(999, "old"), after)
    after.display_avatar.save.assert_not_awaited()


async def test_registered_user_avatar_is_saved(db, seed_data, tmp_path, monkeypatch):
    monkeypatch.setattr("bot.cogs.admin.admin_cog.ASSETS_ROOT", tmp_path)
    discord_id = seed_data["players"][0].discord_id
    cog = AdminCog(MagicMock())
    after = _user(discord_id, "new")
    await cog.on_user_update(_user(discord_id, "old"), after)
    after.display_avatar.save.assert_awaited_once()


async def test_getpfps_only_downloads_registered_players(db, seed_data, tmp_path, monkeypatch):
    monkeypatch.setattr("bot.cogs.admin.admin_cog.ASSETS_ROOT", tmp_path)
    registered = {p.discord_id for p in seed_data["players"]}
    members = {d: _user(d, "a") for d in registered}
    stranger = _user(424242, "s")

    ctx = MagicMock()
    ctx.guild.get_member = MagicMock(side_effect=lambda d: members.get(d) or (stranger if d == 424242 else None))
    ctx.send = AsyncMock(return_value=MagicMock(edit=AsyncMock()))

    cog = AdminCog(MagicMock())
    await cog.getpfps_cmd.callback(cog, ctx)

    assert all(m.display_avatar.save.await_count == 1 for m in members.values())
    stranger.display_avatar.save.assert_not_awaited()


def test_image_commands_have_cooldowns():
    from bot.cogs.standings.stats_cog import StatsCog

    commands = {c.name: c for c in StatsCog.__cog_app_commands__}
    for name in ("playercard", "season_summary"):
        assert any("cooldown" in getattr(check, "__qualname__", "") for check in commands[name].checks), name
