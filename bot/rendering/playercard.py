"""Player card rendering.

Pure drawing: takes a :class:`PlayerStats` object and writes a PNG. No
database access, so the layout can be rendered and tested without a bot or a
session. PIL is blocking, so call it from a worker thread.
"""

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from bot.services.player_stats import OpenState, PlayerStats
from bot.utils.countries import COUNTRIES
from . import asset_path, save_png_atomic
from .StandingsMaker import bg_color, get_flag, playoff_color, softgreen, softred

CARD_W = 1000
CARD_H = 700
MARGIN = 40
CONTENT_W = CARD_W - MARGIN * 2

PANEL = (38, 38, 38)
RULE = (78, 78, 78)
MUTED = (170, 178, 190)
WHITE = (255, 255, 255)
GOLD = (245, 196, 70)

AVATAR = 120
TILE_H = 100
TILE_GAP = 12
TABLE_TOP = 304
TABLE_HEADER_H = 36
TABLE_ROW_H = 46
AWARDS_TOP = 548
AWARDS_H = 76

# x-centre of each numeric column, matching the header labels.
COLUMNS = (("GAMES", 300), ("W", 372), ("L", 436), ("GF", 500), ("GA", 564), ("GD", 628))
STATUS_X = 700

COUNTRY_NAMES = {code: name for name, code in COUNTRIES.items()}


@lru_cache(maxsize=None)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        font=asset_path("fonts", "Agdasima-Regular.ttf"), size=size
    )


def _fit(text: str, max_width: int, size: int) -> str:
    """Clip text with an ellipsis so it cannot run into the next block."""
    font = _font(size)
    text = text or ""
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text + "…"


def _signed(value: int) -> str:
    """+3 / -2 / 0 — a bare 0 reads better than +0."""
    return f"{value:+d}" if value else "0"


def _champion_line(stats: PlayerStats) -> tuple[str, tuple[int, int, int]]:
    """Text and colour for the season-champion indicator."""
    if stats.champion:
        return "SEASON CHAMPION", GOLD
    if stats.season_champion_name:
        return f"Champion: {stats.season_champion_name}", WHITE
    if stats.season_active:
        return "Champion: TBD", MUTED
    return "Champion: Not recorded", MUTED


def _awards_seasons(stats: PlayerStats) -> str:
    """Compact list of seasons the player has won."""
    if not stats.championship_seasons:
        return "None yet"
    return " · ".join(f"S{number}" for number in stats.championship_seasons)


def _paste_avatar(img: Image.Image, discord_id: int, x: int, y: int, size: int) -> None:
    try:
        pfp = Image.open(asset_path("pfps", f"{discord_id}.png")).convert("RGBA")
    except (FileNotFoundError, OSError):
        pfp = Image.open(asset_path("pfps", "default.png")).convert("RGBA")
    pfp = pfp.resize((size, size))
    circle = Image.open(asset_path("pfps", "circle.png")).convert("RGBA").resize((size, size))
    img.paste(pfp, (x, y), circle)


def _draw_header(img: Image.Image, d: ImageDraw.ImageDraw, stats: PlayerStats) -> None:
    d.rectangle((0, 0, CARD_W, 6), fill=playoff_color)

    _paste_avatar(img, stats.discord_id, MARGIN, 32, AVATAR)
    d.ellipse(
        (MARGIN - 2, 30, MARGIN + AVATAR + 2, 32 + AVATAR + 2),
        outline=playoff_color,
        width=3,
    )

    name_x = MARGIN + AVATAR + 26
    d.text(
        (name_x, 40),
        _fit(stats.name, CARD_W - name_x - 300, 54),
        font=_font(54),
        fill=WHITE,
    )

    subtitle_y = 108
    if stats.country:
        code = stats.country.upper()
        flag = get_flag(code, width=34)
        img.paste(flag, (name_x, subtitle_y + (34 - flag.height) // 2), flag)
        label = COUNTRY_NAMES.get(code, code)
        d.text((name_x + 42, subtitle_y), label, font=_font(24), fill=MUTED)
    else:
        d.text((name_x, subtitle_y), "No country set", font=_font(24), fill=MUTED)

    d.text(
        (CARD_W - MARGIN, 34),
        f"SEASON {stats.season}",
        font=_font(46),
        fill=WHITE,
        anchor="ra",
    )
    d.text(
        (CARD_W - MARGIN, 92),
        "ACTIVE" if stats.season_active else "ARCHIVED",
        font=_font(20),
        fill=softgreen if stats.season_active else MUTED,
        anchor="ra",
    )
    champion_text, champion_color = _champion_line(stats)
    d.text(
        (CARD_W - MARGIN, 118),
        _fit(champion_text, 300, 20),
        font=_font(20),
        fill=champion_color,
        anchor="ra",
    )


def _tiles(stats: PlayerStats) -> list[tuple[str, str, tuple[int, int, int], tuple[int, int, int]]]:
    """(label, value, accent colour, value colour).

    Accents keep the stage semantics; values fade to grey when the player has
    nothing recorded, so an empty card does not look like a wall of zeros.
    """
    total = stats.total
    diff = total.goal_diff
    diff_color = softgreen if diff > 0 else softred if diff < 0 else WHITE
    return [
        ("GAMES", str(total.games), WHITE, WHITE if total.games else MUTED),
        ("WINS", str(total.wins), softgreen, softgreen if total.wins else MUTED),
        ("LOSSES", str(total.losses), softred, softred if total.losses else MUTED),
        ("GOALS", str(total.goals_for), WHITE, WHITE if total.goals_for else MUTED),
        ("GOAL DIFF", _signed(diff), diff_color, diff_color if diff else MUTED),
        ("WIN RATE", f"{total.win_rate:.0%}", WHITE, WHITE if total.games else MUTED),
    ]


def _draw_tiles(d: ImageDraw.ImageDraw, stats: PlayerStats) -> None:
    entries = _tiles(stats)
    tile_w = (CONTENT_W - TILE_GAP * (len(entries) - 1)) // len(entries)
    top = 176
    for index, (label, value, accent, color) in enumerate(entries):
        left = MARGIN + index * (tile_w + TILE_GAP)
        d.rectangle((left, top, left + tile_w, top + TILE_H), fill=PANEL, outline=RULE)
        d.rectangle((left, top, left + tile_w, top + 4), fill=accent)
        d.text((left + 16, top + 18), label, font=_font(18), fill=MUTED)
        d.text((left + 16, top + 40), value, font=_font(46), fill=color)


def _open_note(stats: PlayerStats) -> str:
    state = stats.open_state
    if state is OpenState.PLAYED:
        return "Played"
    if state is OpenState.SKIPPED:
        return f"Skipped Open (seed #{stats.swiss_seed})"
    return "Did not play"


def _stage_note(stats: PlayerStats, stage: str, record) -> str:
    if stage == "PLAYOFF" and stats.champion:
        return "Champion"
    if not record.played:
        return "Did not play"
    if stage == "PLAYIN" and stats.playin_rank:
        return f"Rank #{stats.playin_rank}"
    if stage == "SWISS" and stats.swiss_seed:
        return f"Seed #{stats.swiss_seed}"
    return ""


def _stage_rows(stats: PlayerStats):
    return [
        ("OPEN", stats.open, _open_note(stats)),
        ("PLAY-IN", stats.playin, _stage_note(stats, "PLAYIN", stats.playin)),
        ("SWISS", stats.swiss, _stage_note(stats, "SWISS", stats.swiss)),
        ("PLAYOFF", stats.playoff, _stage_note(stats, "PLAYOFF", stats.playoff)),
    ]


def _draw_stage_table(d: ImageDraw.ImageDraw, stats: PlayerStats) -> None:
    rows = _stage_rows(stats)
    height = TABLE_HEADER_H + TABLE_ROW_H * len(rows)
    right = CARD_W - MARGIN

    d.rectangle((MARGIN, TABLE_TOP, right, TABLE_TOP + height), fill=PANEL, outline=RULE)
    d.rectangle((MARGIN, TABLE_TOP, right, TABLE_TOP + TABLE_HEADER_H), fill=playoff_color)
    d.text((MARGIN + 16, TABLE_TOP + 8), "STAGE", font=_font(18), fill=WHITE)
    for label, x in COLUMNS:
        d.text((x, TABLE_TOP + 8), label, font=_font(18), fill=WHITE, anchor="ma")
    d.text((STATUS_X, TABLE_TOP + 8), "STATUS", font=_font(18), fill=WHITE)

    for index, (label, record, note) in enumerate(rows):
        top = TABLE_TOP + TABLE_HEADER_H + index * TABLE_ROW_H
        if index:
            d.line((MARGIN, top, right, top), fill=RULE)
        middle = top + TABLE_ROW_H // 2

        d.text((MARGIN + 16, middle), label, font=_font(24), fill=WHITE, anchor="lm")

        values = (
            str(record.games),
            str(record.wins),
            str(record.losses),
            str(record.goals_for),
            str(record.goals_against),
            _signed(record.goal_diff),
        )
        for (column, x), value in zip(COLUMNS, values):
            color = WHITE
            if not record.played:
                color = MUTED
            elif column == "W" and record.wins:
                color = softgreen
            elif column == "L" and record.losses:
                color = softred
            elif column == "GD":
                color = (
                    softgreen
                    if record.goal_diff > 0
                    else softred if record.goal_diff < 0 else WHITE
                )
            d.text((x, middle), value, font=_font(24), fill=color, anchor="mm")

        if note:
            d.text(
                (STATUS_X, middle),
                note,
                font=_font(22),
                fill=GOLD if note == "Champion" else MUTED,
                anchor="lm",
            )


def _draw_awards(d: ImageDraw.ImageDraw, stats: PlayerStats) -> None:
    """Career awards strip below the stage table."""
    top = AWARDS_TOP
    right = CARD_W - MARGIN
    d.rectangle((MARGIN, top, right, top + AWARDS_H), fill=PANEL, outline=RULE)
    d.rectangle((MARGIN, top, MARGIN + 6, top + AWARDS_H), fill=GOLD)
    d.text(
        (MARGIN + 20, top + 8),
        "CAREER AWARDS",
        font=_font(18),
        fill=MUTED,
    )

    count_x = MARGIN + 20
    d.text(
        (count_x, top + 28),
        "PBL CIRCUITS WON",
        font=_font(16),
        fill=MUTED,
    )
    d.text(
        (count_x, top + 40),
        str(stats.circuits_won),
        font=_font(30),
        fill=GOLD if stats.circuits_won else MUTED,
    )

    divider_x = MARGIN + 210
    d.line((divider_x, top + 26, divider_x, top + AWARDS_H - 10), fill=RULE)

    seasons_x = divider_x + 20
    d.text(
        (seasons_x, top + 28),
        "SEASONS WON",
        font=_font(16),
        fill=MUTED,
    )
    seasons_text = _awards_seasons(stats)
    d.text(
        (seasons_x, top + 42),
        _fit(seasons_text, right - seasons_x - 16, 26),
        font=_font(26),
        fill=WHITE if stats.circuits_won else MUTED,
    )


def _draw_footer(d: ImageDraw.ImageDraw, stats: PlayerStats) -> None:
    y = CARD_H - 58
    d.line((MARGIN, y, CARD_W - MARGIN, y), fill=RULE)
    d.text(
        (MARGIN, y + 12),
        "TopGoalPost · Pro Bobble League Circuit",
        font=_font(18),
        fill=MUTED,
    )
    d.text(
        (CARD_W - MARGIN, y + 12),
        f"Season {stats.season} · {stats.total.games} games",
        font=_font(18),
        fill=MUTED,
        anchor="ra",
    )


def render_player_card(stats: PlayerStats, filename: str) -> str:
    """Draw the card to ``filename`` and return the path."""
    img = Image.new("RGBA", (CARD_W, CARD_H), bg_color)
    d = ImageDraw.Draw(img)
    _draw_header(img, d, stats)
    _draw_tiles(d, stats)
    _draw_stage_table(d, stats)
    _draw_awards(d, stats)
    _draw_footer(d, stats)
    save_png_atomic(img, filename)
    return filename
