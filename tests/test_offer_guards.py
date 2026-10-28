"""Safety guards for player-authored scheduling offers."""

from unittest.mock import AsyncMock, MagicMock

from bot.utils.content_filter import find_vulgarity
from bot.utils.rate_limit import SlidingWindowRateLimiter


def test_content_filter_allows_clean_message():
    assert find_vulgarity("Good game, see you at 19:00") is None


def test_content_filter_catches_common_variants():
    assert find_vulgarity("what the fuck") == "fuck"
    assert find_vulgarity("f.u.c.k") == "fuck"
    assert find_vulgarity("you fuuucking idiot") == "fucking"
    assert find_vulgarity("sh1t") == "shit"


def test_content_filter_avoids_innocent_substrings():
    assert find_vulgarity("classic match") is None
    assert find_vulgarity("Scunthorpe") is None
    assert find_vulgarity("assassin") is None


def test_content_filter_caches_repeated_checks():
    find_vulgarity.cache_clear()
    message = "cache probe: f.u.c.k"

    first = find_vulgarity(message)
    second = find_vulgarity(message)

    assert first == second == "fuck"
    assert find_vulgarity.cache_info().hits >= 1


def test_sliding_window_rate_limiter_blocks_and_recovers():
    now = 1000.0
    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10, clock=lambda: now)

    assert limiter.consume(1) == 0
    assert limiter.consume(1) == 0
    assert limiter.consume(1) == 10
    assert limiter.retry_after(1) == 10

    now += 11
    assert limiter.retry_after(1) == 0
    assert limiter.consume(1) == 0


def test_offer_embeds_hide_vulgar_notes_as_a_fallback():
    from bot.ui.scheduling import _safe_note

    assert _safe_note("Good game") == "Good game"
    assert _safe_note("you fuck") == "Message hidden by the safety filter."


async def test_counter_offer_note_rejects_vulgarity_before_sending():
    from bot.ui.scheduling import CounterOfferModal, MatchConfirmView

    view = MatchConfirmView(target=42)
    modal = CounterOfferModal(view)
    modal.note._value = "you fucking suck"

    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 42
    interaction.response = AsyncMock()

    await modal.on_submit(interaction)

    assert view.value is None
    assert view.counter_submitted is False
    message = interaction.response.send_message.call_args[0][0]
    assert "respectful" in message.lower()


async def test_counter_offer_cannot_be_submitted_twice():
    from bot.ui.scheduling import CounterOfferModal, MatchConfirmView

    view = MatchConfirmView(target=42)
    view.counter_submitted = True
    modal = CounterOfferModal(view)
    modal.note._value = "GG"

    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 42
    interaction.response = AsyncMock()

    await modal.on_submit(interaction)

    assert view.value is None
    message = interaction.response.send_message.call_args[0][0]
    assert "already answered" in message
