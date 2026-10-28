"""Stage and season announcements are public embeds, not plain text."""

import discord

from bot.utils.announce import (
    announce,
    season_close_embed,
    stage_announcement_embed,
)


def test_stage_announcement_embed_has_actions_and_next_stage():
    embed = stage_announcement_embed("open")

    assert embed.title == "🎲 The Open stage is live"
    fields = {field.name: field.value for field in embed.fields}
    assert "/open" in fields["📊 Standings"]
    assert fields["⏭️ Up next"] == "Playins"
    assert "**▸ Open**" in embed.description
    assert embed.footer.text == "TopGoalPost · Season stage update"


def test_public_announcements_never_mention_host_commands():
    host_commands = ("/season ", "/tourney", "/dashboard", "/set_score", "/player ", "`!")
    embeds = [stage_announcement_embed(s) for s in ("SIGNUP", "OPEN", "PLAYIN", "SWISS", "PLAYOFF")]
    embeds.append(season_close_embed(season=3, champion="Alice", runner_up="Bob"))
    for embed in embeds:
        text = " ".join(
            [embed.title or "", embed.description or "", embed.footer.text or ""]
            + [f.name + " " + f.value for f in embed.fields]
        )
        assert not any(cmd in text for cmd in host_commands), text


def test_unknown_stage_has_no_announcement():
    assert stage_announcement_embed("OFFSEASON") is None
    assert stage_announcement_embed("bogus") is None


def test_season_close_embed_summarizes_result():
    embed = season_close_embed(season=3, champion="Alice", runner_up="Bob")

    assert embed.title == "🏆 Season 3 is over"
    assert "Alice" in embed.description
    assert "Bob" in embed.description
    fields = {field.name: field.value for field in embed.fields}
    assert "/season_summary" in fields["📜 Full recap"]
    assert "Season 4" in fields["🔜 Next season"]
    assert "/season create" not in "".join(f.value for f in embed.fields)


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
