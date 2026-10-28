"""Custom PBLC emojis stay centralized and fall back cleanly."""

import discord
import pytest

from bot.ui.report import report_prompt_embed, report_submitted_embed, score_report_embed
from bot.ui.signup_button import signup_panel_embed
from bot.utils import emojis
from bot.utils.announce import season_close_embed, stage_announcement_embed


class FakeBot:
    """Only the emoji lookup used by the registry."""

    def __init__(self, available):
        self.available = available

    def get_emoji(self, emoji_id):
        return self.available.get(emoji_id)


@pytest.fixture(autouse=True)
def reset_emoji_binding():
    emojis.unbind()
    yield
    emojis.unbind()


def test_standard_fallbacks_match_the_old_emoji():
    expected = {
        "PBLC": "🏟️",
        "SIGNUP": "📝",
        "OPEN": "🎲",
        "PLAYINS": "⚔️",
        "SWISS": "🇨🇭",
        "PLAYOFFS": "🏆",
        "CHAMPION": "👑",
        "MATCH": "🎮",
        "REPORT": "🎮",
        "REPORT_RECEIPT": "📨",
        "SCHEDULE": "🗓️",
        "OFFER": "⏰",
    }

    assert {key: emojis.get(key) for key in expected} == expected


def test_bound_bot_uses_cached_custom_emoji():
    open_id = emojis.EMOJIS["OPEN"].emoji_id
    bot = FakeBot(
        {open_id: discord.PartialEmoji(name="pb_open", id=open_id)}
    )
    emojis.bind(bot)

    assert emojis.get("OPEN") == f"<:pb_open:{open_id}>"
    # Missing custom emojis still use their fallback while a bot is bound.
    assert emojis.get("SIGNUP") == "📝"


def test_unbind_restores_fallbacks():
    open_id = emojis.EMOJIS["OPEN"].emoji_id
    emojis.bind(
        FakeBot({open_id: discord.PartialEmoji(name="pb_open", id=open_id)})
    )

    emojis.unbind()

    assert emojis.get("OPEN") == "🎲"


def test_embeds_pick_up_custom_emoji_after_binding():
    open_id = emojis.EMOJIS["OPEN"].emoji_id
    signup_id = emojis.EMOJIS["SIGNUP"].emoji_id
    match_id = emojis.EMOJIS["MATCH"].emoji_id
    report_id = emojis.EMOJIS["REPORT"].emoji_id
    receipt_id = emojis.EMOJIS["REPORT_RECEIPT"].emoji_id
    champion_id = emojis.EMOJIS["CHAMPION"].emoji_id
    playoffs_id = emojis.EMOJIS["PLAYOFFS"].emoji_id

    emojis.bind(
        FakeBot(
            {
                open_id: discord.PartialEmoji(name="pb_open", id=open_id),
                signup_id: discord.PartialEmoji(name="pb_signup", id=signup_id),
                match_id: discord.PartialEmoji(name="pb_match", id=match_id),
                report_id: discord.PartialEmoji(name="pb_report", id=report_id),
                champion_id: discord.PartialEmoji(
                    name="pb_champion", id=champion_id
                ),
                playoffs_id: discord.PartialEmoji(
                    name="pb_playoffs", id=playoffs_id
                ),
            }
        )
    )

    assert (
        stage_announcement_embed("OPEN").title
        == f"<:pb_open:{open_id}> The Open stage is live"
    )
    assert (
        signup_panel_embed(4).title
        == f"<:pb_signup:{signup_id}> Season 4 signups are open"
    )
    assert report_prompt_embed().title == f"<:pb_match:{match_id}> How did it go?"
    report = score_report_embed(
        report_id=1,
        match_id=2,
        reporter_id=3,
        opponent_id=4,
        my_goals=2,
        opponent_goals=1,
    )
    assert report.title == f"<:pb_report:{report_id}> Final score check"
    assert (
        report_submitted_embed(match_id=2).title
        == f"<:pb_report:{receipt_id}> Score sent"
    )

    recap = season_close_embed(season=3, champion="Alice")
    assert recap.title == f"<:pb_playoffs:{playoffs_id}> Season 3 is over"
    assert f"<:pb_champion:{champion_id}> Alice" in recap.description
