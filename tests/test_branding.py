"""External branding loads safely and falls back when a value is missing."""

import json

from bot import branding


def _write(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_default_branding_file_loads():
    loaded = branding.load_branding()

    assert loaded.bot_name
    assert loaded.tournament_name
    assert loaded.tournament_short_name
    assert loaded.circuit_label
    assert "OPEN" in loaded.emojis
    assert loaded.emojis["OPEN"].fallback
    assert loaded.footer_text() == loaded.bot_name
    assert loaded.footer_text("Section") == f"{loaded.bot_name} · Section"


def test_custom_branding_overrides_names_and_emoji(tmp_path):
    path = _write(
        tmp_path / "branding.json",
        {
            "bot": {"name": "Test Bot"},
            "tournament": {
                "name": "Test Circuit",
                "short_name": "TC",
                "circuit_label": "TC WINS",
            },
            "emojis": {
                "OPEN": {
                    "name": "test_open",
                    "id": "123456789012345678",
                    "fallback": "O",
                }
            },
        },
    )

    loaded = branding.load_branding(path)

    assert loaded.bot_name == "Test Bot"
    assert loaded.tournament_name == "Test Circuit"
    assert loaded.tournament_short_name == "TC"
    assert loaded.circuit_label == "TC WINS"
    assert loaded.footer == "Test Bot · Test Circuit"
    assert loaded.emojis["OPEN"].name == "test_open"
    assert loaded.emojis["OPEN"].emoji_id == 123456789012345678
    assert loaded.emojis["OPEN"].fallback == "O"
    # Emojis omitted from the override stay on their standard fallback.
    assert loaded.emojis["SIGNUP"].emoji_id is None
    assert loaded.emojis["SIGNUP"].fallback == branding.DEFAULT_EMOJI_FALLBACKS["SIGNUP"]


def test_missing_or_invalid_files_use_defaults(tmp_path):
    missing = branding.load_branding(tmp_path / "missing.json")
    assert missing.bot_name == branding.DEFAULT_BOT_NAME
    assert missing.tournament_name == branding.DEFAULT_TOURNAMENT_NAME
    assert missing.emojis["OPEN"].emoji_id is None

    malformed = tmp_path / "broken.json"
    malformed.write_text("{not json", encoding="utf-8")
    loaded = branding.load_branding(malformed)
    assert loaded.bot_name == branding.DEFAULT_BOT_NAME


def test_invalid_emoji_id_keeps_the_standard_fallback(tmp_path):
    path = _write(
        tmp_path / "branding.json",
        {
            "emojis": {
                "OPEN": {
                    "name": "bad_open",
                    "id": "not-a-snowflake",
                    "fallback": "O",
                }
            }
        },
    )

    loaded = branding.load_branding(path)

    assert loaded.emojis["OPEN"].name == ""
    assert loaded.emojis["OPEN"].emoji_id is None
    assert loaded.emojis["OPEN"].fallback == "O"


def test_branding_file_env_override(tmp_path, monkeypatch):
    path = _write(
        tmp_path / "other.json",
        {"bot": {"name": "Other Bot"}, "tournament": {"name": "Other Circuit"}},
    )
    monkeypatch.setenv("BRANDING_FILE", str(path))

    loaded = branding.load_branding()

    assert loaded.bot_name == "Other Bot"
    assert loaded.tournament_name == "Other Circuit"
