"""Player display-name validation.

Names are echoed in bot messages, so anything Discord can turn into a mention
(`@everyone`, `<@id>`, `<@&role>`, `<#channel>`) must never be stored.
"""

import re
import unicodedata

NAME_MIN_LENGTH = 3
NAME_MAX_LENGTH = 50

# `@` covers @everyone/@here, `<`/`>` cover user, role, channel and emoji
# mentions, and a backtick would break the code spans names are shown in.
FORBIDDEN_NAME_CHARS = frozenset("@<>`")

_WHITESPACE = re.compile(r"\s+")


def _strip_invisible(text: str) -> str:
    """Drop control and format characters (zero-width spaces, RTL marks)."""
    return "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf")
    )


def unsafe_name_reason(name: str) -> str | None:
    """Return why a name could ping or break formatting, or None if safe."""
    found = sorted({ch for ch in name if ch in FORBIDDEN_NAME_CHARS})
    if found:
        listed = " ".join(f"`{ch}`" if ch != "`" else "backtick" for ch in found)
        return f"Names can't contain {listed}."
    return None


def clean_player_name(name: str) -> tuple[str, str | None]:
    """Normalise a player-chosen name and validate it.

    Returns ``(cleaned, error)``; ``error`` is None when the name is usable.
    """
    cleaned = _WHITESPACE.sub(" ", _strip_invisible(name or "")).strip()
    if not NAME_MIN_LENGTH <= len(cleaned) <= NAME_MAX_LENGTH:
        return cleaned, (
            f"Name must be between {NAME_MIN_LENGTH} and "
            f"{NAME_MAX_LENGTH} characters."
        )
    return cleaned, unsafe_name_reason(cleaned)
