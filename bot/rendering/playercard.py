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
CARD_H = 770
MARGIN = 40
CONTENT_W = CARD_W - MARGIN * 2

PANEL = (38, 38, 38)
RULE = (78, 78, 78)
MUTED = (170, 178, 190)
WHITE = (255, 255, 255)
GOLD = (245, 196, 70)
GOLD_DARK = (58, 50, 30)
RADIUS = 10

AVATAR = 120
TILE_H = 100
TILE_GAP = 12
TABLE_TOP = 304
TABLE_HEADER_H = 36
TABLE_ROW_H = 46
AWARDS_TOP = 578
AWARDS_H = 108
FOOTER_Y = CARD_H - 48

# x-centre of each numeric column, matching the header labels.
COLUMNS = (("GAMES", 300), ("W", 372), ("L", 436), ("GF", 500), ("GA", 564), ("GD", 628))
STATUS_X = 700

COUNTRY_NAMES = {code.upper(): name for name, code in COUNTRIES.items()}


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


def _accent(stats: PlayerStats) -> tuple[int, int, int]:
    """Gold trim for this season's champion, the playoff blue otherwise."""
    return GOLD if stats.champion else playoff_color


def _pill(
    d: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
    size: int = 20,
    fill: tuple[int, int, int] | None = None,
    anchor_right: bool = False,
) -> int:
    """Outlined rounded label; returns its width so pills can be chained."""
    font = _font(size)
    width = int(font.getlength(text)) + 24
    height = size + 12
    left = x - width if anchor_right else x
    d.rounded_rectangle(
        (left, y, left + width, y + height),
        radius=height // 2,
        fill=fill,
        outline=color,
        width=2,
    )
    d.text((left + width / 2, y + height / 2), text, font=font, fill=color, anchor="mm")
    return width


def _draw_trophy(d: ImageDraw.ImageDraw, cx: int, top: int, size: int, color) -> None:
    """Small vector trophy: cup, handles, stem and base."""
    cup_w = size * 0.62
    cup_h = size * 0.5
    left = cx - cup_w / 2
    handle = size * 0.16
    line = max(2, size // 14)
    d.arc((left - handle, top + size * 0.06, left + handle, top + size * 0.34), 90, 270, fill=color, width=line)
    d.arc(
        (left + cup_w - handle, top + size * 0.06, left + cup_w + handle, top + size * 0.34),
        270, 90, fill=color, width=line,
    )
    d.chord((left, top - cup_h, left + cup_w, top + cup_h), 0, 180, fill=color)
    stem_w = size * 0.1
    d.rectangle((cx - stem_w / 2, top + cup_h, cx + stem_w / 2, top + size * 0.74), fill=color)
    base_w = size * 0.46
    d.rounded_rectangle(
        (cx - base_w / 2, top + size * 0.74, cx + base_w / 2, top + size * 0.88),
        radius=2, fill=color,
    )


def _draw_header(img: Image.Image, d: ImageDraw.ImageDraw, stats: PlayerStats) -> None:
    accent = _accent(stats)
    d.rectangle((0, 0, CARD_W, 6), fill=accent)

    _paste_avatar(img, stats.discord_id, MARGIN, 32, AVATAR)
    d.ellipse(
        (MARGIN - 3, 29, MARGIN + AVATAR + 3, 32 + AVATAR + 3),
        outline=accent,
        width=4,
    )

    name_x = MARGIN + AVATAR + 26
    d.text(
        (name_x, 34),
        _fit(stats.name, CARD_W - name_x - 260, 54),
        font=_font(54),
        fill=GOLD if stats.champion else WHITE,
    )

    subtitle_y = 104
    if stats.country:
        code = stats.country.upper()
        flag = get_flag(code, width=34)
        img.paste(flag, (name_x, subtitle_y + (28 - flag.height) // 2), flag)
        label = COUNTRY_NAMES.get(code, code)
        d.text((name_x + 44, subtitle_y + 14), label, font=_font(24), fill=MUTED, anchor="lm")
    else:
        d.text((name_x, subtitle_y + 14), "No country set", font=_font(24), fill=MUTED, anchor="lm")

    if stats.champion:
        _pill(d, name_x, 134, f"SEASON {stats.season} CHAMPION", GOLD, size=16, fill=GOLD_DARK)

    right = CARD_W - MARGIN
    d.text((right, 30), f"SEASON {stats.season}", font=_font(46), fill=WHITE, anchor="ra")
    _pill(
        d, right, 88,
        "ACTIVE" if stats.season_active else "ARCHIVED",
        softgreen if stats.season_active else MUTED,
        size=16,
        anchor_right=True,
    )
    if not stats.champion:
        champion_text, champion_color = _champion_line(stats)
        d.text(
            (right, 132),
            _fit(champion_text, 240, 20),
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
        d.rounded_rectangle(
            (left, top, left + tile_w, top + TILE_H), radius=RADIUS, fill=PANEL, outline=RULE
        )
        d.rounded_rectangle(
            (left, top, left + tile_w, top + 8), radius=4, fill=accent,
            corners=(True, True, False, False),
        )
        d.rectangle((left + 1, top + 4, left + tile_w - 1, top + 8), fill=PANEL)
        d.text((left + 16, top + 20), label, font=_font(18), fill=MUTED)
        d.text((left + 16, top + 42), value, font=_font(46), fill=color)


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

    d.rounded_rectangle(
        (MARGIN, TABLE_TOP, right, TABLE_TOP + height), radius=RADIUS, fill=PANEL, outline=RULE
    )
    d.rounded_rectangle(
        (MARGIN, TABLE_TOP, right, TABLE_TOP + TABLE_HEADER_H),
        radius=RADIUS, fill=playoff_color, corners=(True, True, False, False),
    )
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
    """Career awards: a title count with a trophy, then one chip per title."""
    top = AWARDS_TOP
    bottom = top + AWARDS_H
    right = CARD_W - MARGIN
    won = stats.circuits_won
    color = GOLD if won else MUTED

    d.text((MARGIN, top - 10), "CAREER AWARDS", font=_font(18), fill=MUTED, anchor="ls")

    # Left: trophy + count.
    count_right = MARGIN + 250
    d.rounded_rectangle(
        (MARGIN, top, count_right, bottom),
        radius=RADIUS,
        fill=GOLD_DARK if won else PANEL,
        outline=GOLD if won else RULE,
        width=2 if won else 1,
    )
    _draw_trophy(d, MARGIN + 58, top + 24, 68, color)
    d.text((MARGIN + 112, top + 54), str(won), font=_font(64), fill=color, anchor="lm")
    d.text(
        (MARGIN + 112 + _font(64).getlength(str(won)) + 12, top + 54),
        "TITLE" if won == 1 else "TITLES",
        font=_font(22),
        fill=color,
        anchor="lm",
    )
    d.text((MARGIN + 112, top + 92), "PBL CIRCUITS WON", font=_font(15), fill=MUTED, anchor="lm")

    # Right: one chip per won season.
    seasons_left = count_right + 16
    d.rounded_rectangle((seasons_left, top, right, bottom), radius=RADIUS, fill=PANEL, outline=RULE)
    d.text((seasons_left + 20, top + 24), "SEASONS WON", font=_font(18), fill=MUTED, anchor="lm")
    if not won:
        d.text((seasons_left + 20, top + 68), "No titles yet", font=_font(28), fill=MUTED, anchor="lm")
        return
    x = seasons_left + 20
    limit = right - 20
    for index, number in enumerate(stats.championship_seasons):
        label = f"S{number}"
        width = int(_font(24).getlength(label)) + 24
        remaining = len(stats.championship_seasons) - index
        if x + width > limit or (remaining > 1 and x + width + 70 > limit):
            _pill(d, x, top + 50, f"+{remaining}", MUTED, size=24)
            break
        current = number == stats.season
        x += _pill(
            d, x, top + 50, label, GOLD, size=24,
            fill=GOLD_DARK if current else None,
        ) + 10


def _draw_footer(d: ImageDraw.ImageDraw, stats: PlayerStats) -> None:
    y = FOOTER_Y
    d.line((MARGIN, y, CARD_W - MARGIN, y), fill=RULE)
    d.text(
        (MARGIN, y + 14),
        "TopGoalPost · Pro Bobble League Circuit",
        font=_font(18),
        fill=MUTED,
    )
    d.text(
        (CARD_W - MARGIN, y + 14),
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
