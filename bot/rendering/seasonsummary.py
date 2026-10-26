"""Season summary rendering.

Pure drawing: the service supplies a :class:`SeasonSummary`, the renderer
writes a PNG. No database access here, so the layout can be tested without a
bot session.
"""

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from bot.services.season_summary import SeasonAward, SeasonSummary, SummaryPlayer
from bot.utils.countries import COUNTRIES
from . import asset_path, save_png_atomic
from .StandingsMaker import bg_color, get_flag, playoff_color, softgreen, softred

SUMMARY_W = 1200
SUMMARY_H = 880
MARGIN = 48
CONTENT_W = SUMMARY_W - MARGIN * 2

PANEL = (38, 38, 38)
RULE = (78, 78, 78)
MUTED = (170, 178, 190)
WHITE = (255, 255, 255)
GOLD = (245, 196, 70)
SILVER = (198, 204, 214)

PODIUM_TOP = 160
PODIUM_H = 200
PODIUM_GAP = 24
PODIUM_W = (CONTENT_W - PODIUM_GAP) // 2
PODIUM_AVATAR = 96

TILE_TOP = 420
TILE_H = 158
TILE_GAP = 18
TILE_COLS = 3
TILE_W = (CONTENT_W - TILE_GAP * (TILE_COLS - 1)) // TILE_COLS
TILE_AVATAR = 64

COUNTRY_NAMES = {code: name for name, code in COUNTRIES.items()}

AWARD_COLORS = {
    "top_scorer": GOLD,
    "most_conceded": softred,
    "most_games": WHITE,
    "most_wins": softgreen,
    "best_diff": softgreen,
    "best_rate": GOLD,
}


@lru_cache(maxsize=None)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        font=asset_path("fonts", "Agdasima-Regular.ttf"), size=size
    )


def _fit(text: str, max_width: int, size: int) -> str:
    font = _font(size)
    text = text or ""
    if font.getlength(text) <= max_width:
        return text
    while text and font.getlength(text + "…") > max_width:
        text = text[:-1]
    return text + "…"


def _paste_avatar(
    img: Image.Image, discord_id: int, x: int, y: int, size: int
) -> None:
    try:
        pfp = Image.open(asset_path("pfps", f"{discord_id}.png")).convert("RGBA")
    except (FileNotFoundError, OSError):
        try:
            pfp = Image.open(asset_path("pfps", "default.png")).convert("RGBA")
        except (FileNotFoundError, OSError):
            return
    pfp = pfp.resize((size, size))
    circle = Image.open(asset_path("pfps", "circle.png")).convert("RGBA").resize(
        (size, size)
    )
    img.paste(pfp, (x, y), circle)


def _draw_header(d: ImageDraw.ImageDraw, summary: SeasonSummary) -> None:
    d.rectangle((0, 0, SUMMARY_W, 6), fill=playoff_color)
    d.text(
        (MARGIN, 30),
        _fit(f"SEASON {summary.season} SUMMARY", CONTENT_W - 240, 64),
        font=_font(64),
        fill=WHITE,
    )
    d.text(
        (SUMMARY_W - MARGIN, 44),
        "ACTIVE" if summary.active else "ARCHIVED",
        font=_font(24),
        fill=softgreen if summary.active else MUTED,
        anchor="ra",
    )
    d.text(
        (MARGIN, 106),
        (
            f"{summary.matches} matches · {summary.players} players · "
            f"{summary.goals} goals"
        ),
        font=_font(24),
        fill=MUTED,
    )


def _draw_podium_panel(
    img: Image.Image,
    d: ImageDraw.ImageDraw,
    player: SummaryPlayer | None,
    left: int,
    label: str,
    accent: tuple[int, int, int],
    placeholder: str,
    missing_note: str,
) -> None:
    top = PODIUM_TOP
    right = left + PODIUM_W
    d.rounded_rectangle(
        (left, top, right, top + PODIUM_H),
        radius=12,
        fill=PANEL,
        outline=accent,
        width=2,
    )
    d.rectangle((left, top, right, top + 5), fill=accent)
    d.text(
        (left + 22, top + 16),
        label,
        font=_font(20),
        fill=accent,
    )

    if player is None:
        d.text(
            (left + 22, top + 64),
            placeholder,
            font=_font(42),
            fill=MUTED,
        )
        d.text(
            (left + 22, top + 124),
            missing_note,
            font=_font(22),
            fill=MUTED,
        )
        return

    avatar_x = left + 22
    avatar_y = top + 54
    _paste_avatar(img, player.discord_id, avatar_x, avatar_y, PODIUM_AVATAR)
    d.ellipse(
        (
            avatar_x - 2,
            avatar_y - 2,
            avatar_x + PODIUM_AVATAR + 2,
            avatar_y + PODIUM_AVATAR + 2,
        ),
        outline=accent,
        width=3,
    )

    text_x = avatar_x + PODIUM_AVATAR + 24
    name_width = right - text_x - 22
    d.text(
        (text_x, top + 45),
        _fit(player.name, name_width, 42),
        font=_font(42),
        fill=WHITE,
    )

    flag_y = top + 101
    if player.country:
        code = player.country.upper()
        try:
            flag = get_flag(code, width=32)
            img.paste(flag, (text_x, flag_y + (32 - flag.height) // 2), flag)
        except Exception:
            pass
        country = COUNTRY_NAMES.get(code, code)
        d.text(
            (text_x + 42, flag_y),
            country,
            font=_font(22),
            fill=MUTED,
        )
    else:
        d.text((text_x, flag_y), "No country set", font=_font(22), fill=MUTED)

    record = (
        f"{player.games} games · {player.wins}-{player.losses} · "
        f"{player.goal_diff:+d} GD"
    )
    d.text(
        (text_x, top + 139),
        _fit(record, name_width, 24),
        font=_font(24),
        fill=accent if player.wins else MUTED,
    )


def _draw_podium(
    img: Image.Image, d: ImageDraw.ImageDraw, summary: SeasonSummary
) -> None:
    placeholder = "TBD" if summary.active else "Not recorded"
    _draw_podium_panel(
        img,
        d,
        summary.champion,
        MARGIN,
        "1ST PLACE · CHAMPION",
        GOLD,
        placeholder,
        "Grand final not decided" if summary.active else "No champion recorded",
    )
    _draw_podium_panel(
        img,
        d,
        summary.runner_up,
        MARGIN + PODIUM_W + PODIUM_GAP,
        "2ND PLACE · RUNNER-UP",
        SILVER,
        placeholder,
        (
            "Grand final not decided"
            if summary.active
            else "Runner-up not recorded"
        ),
    )


def _draw_award_tile(
    img: Image.Image,
    d: ImageDraw.ImageDraw,
    award: SeasonAward,
    left: int,
    top: int,
) -> None:
    right = left + TILE_W
    color = AWARD_COLORS.get(award.key, WHITE)
    d.rounded_rectangle(
        (left, top, right, top + TILE_H),
        radius=10,
        fill=PANEL,
        outline=RULE,
        width=1,
    )
    d.rectangle((left, top, right, top + 4), fill=color)

    d.text(
        (left + 16, top + 14),
        award.label,
        font=_font(18),
        fill=MUTED,
    )
    d.text(
        (right - 16, top + 6),
        award.value,
        font=_font(38),
        fill=color if award.player else MUTED,
        anchor="ra",
    )

    if award.player is None:
        d.text(
            (left + 16, top + 68),
            "No completed matches",
            font=_font(24),
            fill=MUTED,
        )
        d.text(
            (left + 16, top + 104),
            award.caption,
            font=_font(18),
            fill=MUTED,
        )
        return

    player = award.player
    avatar_x = left + 16
    avatar_y = top + 60
    _paste_avatar(img, player.discord_id, avatar_x, avatar_y, TILE_AVATAR)

    name_x = avatar_x + TILE_AVATAR + 12
    if player.country:
        code = player.country.upper()
        try:
            flag = get_flag(code, width=26)
            img.paste(
                flag,
                (name_x, avatar_y + 4 + (26 - flag.height) // 2),
                flag,
            )
        except Exception:
            pass
        name_x += 34

    d.text(
        (name_x, avatar_y - 2),
        _fit(player.name, right - name_x - 14, 28),
        font=_font(28),
        fill=WHITE,
    )
    d.text(
        (avatar_x + TILE_AVATAR + 12, avatar_y + 38),
        _fit(award.caption, right - avatar_x - TILE_AVATAR - 26, 18),
        font=_font(18),
        fill=MUTED,
    )


def _draw_leaders(
    img: Image.Image, d: ImageDraw.ImageDraw, summary: SeasonSummary
) -> None:
    d.text(
        (MARGIN, 384),
        "SEASON LEADERS",
        font=_font(22),
        fill=MUTED,
    )
    for index, award in enumerate(summary.awards[: TILE_COLS * 2]):
        row, column = divmod(index, TILE_COLS)
        left = MARGIN + column * (TILE_W + TILE_GAP)
        top = TILE_TOP + row * (TILE_H + TILE_GAP)
        _draw_award_tile(img, d, award, left, top)


def _draw_footer(d: ImageDraw.ImageDraw, summary: SeasonSummary) -> None:
    y = SUMMARY_H - 58
    d.line((MARGIN, y, SUMMARY_W - MARGIN, y), fill=RULE)
    d.text(
        (MARGIN, y + 12),
        "TopGoalPost · Pro Bobble League Circuit",
        font=_font(18),
        fill=MUTED,
    )
    d.text(
        (SUMMARY_W - MARGIN, y + 12),
        f"Season {summary.season} · {summary.matches} matches",
        font=_font(18),
        fill=MUTED,
        anchor="ra",
    )


def render_season_summary(summary: SeasonSummary, filename: str) -> str:
    """Draw the summary to ``filename`` and return the path."""
    img = Image.new("RGBA", (SUMMARY_W, SUMMARY_H), bg_color)
    d = ImageDraw.Draw(img)
    _draw_header(d, summary)
    _draw_podium(img, d, summary)
    _draw_leaders(img, d, summary)
    _draw_footer(d, summary)
    save_png_atomic(img, filename)
    return filename
