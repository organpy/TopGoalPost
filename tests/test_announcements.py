"""Stage and season announcements are public embeds, not plain text."""

import discord

from bot.utils.announce import (
    announce,
    season_close_embed,
    stage_announcement_embed,
)


def test_stage_announcement_embed_has_actions_and_next_stage():
    embed = stage_announcement_embed("open")

    assert embed.title == "🎲 Open stage is live"
    fields = {field.name: field.value for field in embed.fields}
    assert "/open" in fields["What to do"]
    assert fields["Next stage"] == "Playins"
    assert embed.footer.text == "TopGoalPost · Season stage update"


def test_unknown_stage_has_no_announcement():
    assert stage_announcement_embed("OFFSEASON") is None
    assert stage_announcement_embed("bogus") is None


def test_season_close_embed_summarizes_result():
    embed = season_close_embed(season=3, champion="Alice", runner_up="Bob")

    assert embed.title == "🏆 Season 3 is over"
    assert "Alice" in embed.description
    assert "Bob" in embed.description
    fields = {field.name: field.value for field in embed.fields}
    assert "/season_summary" in fields["Full recap"]
    assert "Season 4" in fields["Next season"]


async def test_announce_posts_embed_to_configured_channel(db):
    from unittest.mock import AsyncMock, MagicMock

    from bot.config import Config

    await Config().set("announcements_channel", "123")
    embed = stage_announcement_embed("PLAYOFF")
    channel = MagicMock(send=AsyncMock())
    bot = MagicMock(get_channel=MagicMock(return_value=channel))

    await announce(bot, embed=embed)

    bot.get_channel.assert_called_once_with(123)
    channel.send.assert_awaited_once_with(embed=embed)
    assert isinstance(embed, discord.Embed)
