"""Small profanity filter for player-authored scheduling messages."""

from functools import lru_cache
import re

# Focused on harassment, slurs, profanity and self-harm incitement. Patterns
# allow repeated letters, common leetspeak substitutions, and separated
# spellings such as "f.u.c.k"; phrases match with any spacing or punctuation
# between words ("kill-yourself", "killyourself").
VULGARITY = frozenset(
    {
        "ass",
        "asshole",
        "assholes",
        "bastard",
        "bastards",
        "batty boy",
        "beaner",
        "bitch",
        "bitches",
        "bollocks",
        "bullshit",
        "bumboy",
        "camel jockey",
        "chink",
        "cock",
        "coolie",
        "coon",
        "cunt",
        "cunts",
        "darkie",
        "dick",
        "dickhead",
        "douche",
        "douchebag",
        "dumbass",
        "end your life",
        "fag",
        "faggot",
        "fuck",
        "fucked",
        "fucker",
        "fuckers",
        "fucking",
        "gas the jews",
        "go die",
        "go kill yourself",
        "gook",
        "greaser",
        "hang yourself",
        "heeb",
        "heil hitler",
        "injun",
        "jackass",
        "jigaboo",
        "jungle bunny",
        "kike",
        "kill yourself",
        "kys",
        "mong",
        "motherfucker",
        "nigga",
        "nigger",
        "paki",
        "piss",
        "poofter",
        "porch monkey",
        "prick",
        "pussy",
        "raghead",
        "redskin",
        "retard",
        "retarded",
        "sambo",
        "sandnigger",
        "shemale",
        "shit",
        "shitty",
        "sieg heil",
        "slope",
        "slut",
        "sluts",
        "spastic",
        "spaz",
        "spear chucker",
        "spic",
        "towelhead",
        "tranny",
        "twat",
        "wanker",
        "wetback",
        "whore",
        "whores",
        "you should die",
        "zipperhead",
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
    letters = [rf"{re.escape(letter)}+" for letter in word.replace(" ", "")]
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
