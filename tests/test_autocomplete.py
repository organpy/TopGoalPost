"""Autocomplete helpers backed by the local player table."""

from bot.db import Player
from bot.ui.autocomplete import player_list_autocomplete, username_autocomplete


async def _seed_players(db):
    async with db.get_session() as session:
        alice = Player(discord_id=1001, name="Alice", country="us")
        session.add_all(
            [
                alice,
                Player(discord_id=1002, name="Bob", country="fr"),
                Player(discord_id=2001, name="Alicia", country="es"),
            ]
        )
        await session.commit()
    return alice


async def test_empty_query_lists_players(db):
    await _seed_players(db)
    choices = await username_autocomplete(None, "")
    assert [c.value for c in choices] == ["Alice", "Alicia", "Bob"]


async def test_filters_by_name(db):
    await _seed_players(db)
    choices = await username_autocomplete(None, "ali")
    assert [c.value for c in choices] == ["Alice", "Alicia"]


async def test_filters_by_discord_id(db):
    await _seed_players(db)
    choices = await username_autocomplete(None, "1002")
    assert [c.value for c in choices] == ["Bob"]


async def test_filters_by_internal_player_id(db):
    alice = await _seed_players(db)
    choices = await username_autocomplete(None, str(alice.id))
    assert "Alice" in [c.value for c in choices]


async def test_list_autocomplete_appends_after_comma(db):
    await _seed_players(db)
    choices = await player_list_autocomplete(None, "Alice, Bo")
    assert [c.value for c in choices] == ["Alice, Bob"]


async def test_list_autocomplete_keeps_prefix(db):
    await _seed_players(db)
    choices = await player_list_autocomplete(None, "Alice, ")
    assert [c.value for c in choices] == ["Alice, Alicia", "Alice, Bob"]
