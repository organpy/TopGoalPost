"""External branding for the bot, tournament, and custom emojis.

Edit ``branding.json`` in the project root to rename the bot or tournament, or
to change custom emoji IDs. Set ``BRANDING_FILE`` to load a different JSON file.
Missing or invalid values fall back to safe defaults so startup never fails.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BRANDING_FILE = Path(__file__).resolve().parents[1] / "branding.json"

DEFAULT_BOT_NAME = "League Bot"
DEFAULT_TOURNAMENT_NAME = "League Circuit"
DEFAULT_TOURNAMENT_SHORT_NAME = "LC"
DEFAULT_CIRCUIT_LABEL = "CIRCUITS WON"

DEFAULT_EMOJI_FALLBACKS = {
    "LEAGUE": "🏟️",
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


@dataclass(frozen=True)
class EmojiSpec:
    """One custom emoji and the standard emoji used when it is unavailable."""

    name: str
    emoji_id: int | None
    fallback: str

    @property
    def markup(self) -> str | None:
        if self.emoji_id is None:
            return None
        return f"<:{self.name}:{self.emoji_id}>"


@dataclass(frozen=True)
class Branding:
    bot_name: str
    tournament_name: str
    tournament_short_name: str
    circuit_label: str
    emojis: dict[str, EmojiSpec]

    @property
    def footer(self) -> str:
        """Bot and tournament name used on rendered images."""
        return f"{self.bot_name} · {self.tournament_name}"

    def footer_text(self, section: str | None = None) -> str:
        """Embed footer with an optional section name."""
        return f"{self.bot_name} · {section}" if section else self.bot_name


def _text(value: Any, default: str) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return default


def _emoji_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            parsed = int(cleaned)
            return parsed if parsed > 0 else None
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Branding file %s not found; using defaults.", path)
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("Could not read branding file %s: %s; using defaults.", path, exc)
        return {}

    if not isinstance(raw, dict):
        logger.warning("Branding file %s must contain a JSON object; using defaults.", path)
        return {}
    return raw


def _default_emojis() -> dict[str, EmojiSpec]:
    return {
        key: EmojiSpec("", None, fallback)
        for key, fallback in DEFAULT_EMOJI_FALLBACKS.items()
    }


def _load_emojis(raw: Any) -> dict[str, EmojiSpec]:
    specs = _default_emojis()
    if raw is None:
        return specs
    if not isinstance(raw, dict):
        logger.warning("'emojis' must be a JSON object; using standard fallbacks.")
        return specs

    for raw_key, raw_spec in raw.items():
        if not isinstance(raw_key, str) or not raw_key.strip():
            logger.warning("Skipping an emoji with an invalid key.")
            continue

        key = raw_key.strip().upper()
        if not isinstance(raw_spec, dict):
            logger.warning("Emoji %s must be a JSON object; using its fallback.", key)
            continue

        base = specs.get(key)
        fallback = _text(raw_spec.get("fallback"), base.fallback if base else "")
        if not fallback:
            logger.warning("Emoji %s has no fallback; skipping it.", key)
            continue

        name = _text(raw_spec.get("name"), "")
        emoji_id = _emoji_id(raw_spec.get("id"))
        if emoji_id is None or not name:
            logger.warning("Emoji %s has an invalid custom ID; using %s.", key, fallback)
            specs[key] = EmojiSpec("", None, fallback)
            continue

        specs[key] = EmojiSpec(name, emoji_id, fallback)

    return specs


def load_branding(path: str | Path | None = None) -> Branding:
    """Load branding from JSON, merging it over safe defaults."""
    if path is None:
        override = os.getenv("BRANDING_FILE")
        path = Path(override).expanduser() if override else DEFAULT_BRANDING_FILE
    else:
        path = Path(path).expanduser()

    raw = _read_json(path)
    bot = raw.get("bot")
    tournament = raw.get("tournament")
    if not isinstance(bot, dict):
        bot = {}
    if not isinstance(tournament, dict):
        tournament = {}

    return Branding(
        bot_name=_text(bot.get("name"), DEFAULT_BOT_NAME),
        tournament_name=_text(tournament.get("name"), DEFAULT_TOURNAMENT_NAME),
        tournament_short_name=_text(
            tournament.get("short_name"), DEFAULT_TOURNAMENT_SHORT_NAME
        ),
        circuit_label=_text(
            tournament.get("circuit_label"), DEFAULT_CIRCUIT_LABEL
        ),
        emojis=_load_emojis(raw.get("emojis")),
    )


BRANDING = load_branding()
