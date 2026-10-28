"""Small profanity filter for player-authored scheduling messages."""

from functools import lru_cache
import re

# Focused on harassment/profanity. Patterns allow repeated letters, common
# leetspeak substitutions, and separated spellings such as "f.u.c.k".
VULGARITY = frozenset(
    {
        "ass",
        "asshole",
        "assholes",
        "bastard",
        "bastards",
        "bitch",
        "bitches",
        "bollocks",
        "bullshit",
        "cock",
        "cunt",
        "cunts",
        "dick",
        "dickhead",
        "douche",
        "douchebag",
        "dumbass",
        "fag",
        "faggot",
        "fuck",
        "fucked",
        "fucker",
        "fuckers",
        "fucking",
        "jackass",
        "motherfucker",
        "nigga",
        "nigger",
        "piss",
        "prick",
        "pussy",
        "retard",
        "retarded",
        "shit",
        "shitty",
        "slut",
        "sluts",
        "twat",
        "wanker",
        "whore",
        "whores",
    }
)

_LEETSPEAK = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "@": "a",
        "$": "s",
        "!": "i",
        "+": "t",
    }
)


def _pattern(word: str) -> re.Pattern[str]:
    letters = [rf"{re.escape(letter)}+" for letter in word]
    return re.compile(
        r"(?<![a-z])" + r"[^a-z]*".join(letters) + r"(?![a-z])"
    )


_PATTERNS = tuple(
    (word, _pattern(word))
    for word in sorted(VULGARITY, key=len, reverse=True)
)


@lru_cache(maxsize=2048)
def find_vulgarity(text: str) -> str | None:
    """Return the matched vulgar word, or None when the text is clean.

    Results are cached because counter-offer validation can run repeatedly for
    the same message.
    """
    if not text:
        return None
    normalized = text.casefold().translate(_LEETSPEAK)
    for word, pattern in _PATTERNS:
        if pattern.search(normalized):
            return word
    return None


def contains_vulgarity(text: str) -> bool:
    return find_vulgarity(text) is not None
