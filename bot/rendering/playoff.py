"""Playoff bracket rendering — extracted from StandingsMaker for size."""

import asyncio
import hashlib

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    EventType,
    Match as DbMatch,
    MatchPlayer as DbMatchPlayer,
)
from bot.services.pairing import ADVANCEMENT_REVERSE_MAP, PLAYOFF_BRACKET
from .StandingsMaker import (
    pfp_w,
    flag_w,
    margin,
    bg_color,
    playoff_color,
    softgreen,
    softred,
    name_font,
    mask,
    pfp_size,
    get_flag,
    _fit_name,
    _get_season,
    _get_event,
    _build_match_rows,
    _match_signature,
    _get_cached_link,
)
from . import asset_path

# Playoff cards are intentionally larger than the shared standings cards:
# names get more room and the three score cells have a fixed width.
PLAYOFF_ROW_H = 44
PLAYOFF_BOX_GAP = 4
PLAYOFF_BOX_H = PLAYOFF_ROW_H * 2 - PLAYOFF_BOX_GAP
PLAYOFF_PFP_W = pfp_w
PLAYOFF_FLAG_W = flag_w
PLAYOFF_NAME_W = 260
PLAYOFF_PLAYER_W = PLAYOFF_PFP_W + PLAYOFF_FLAG_W + PLAYOFF_NAME_W
PLAYOFF_SCORE_W = 48
PLAYOFF_BOX_W = PLAYOFF_PLAYER_W + PLAYOFF_SCORE_W * 3
PLAYOFF_COL_GAP = 90  # was 150
PLAYOFF_COL_STEP = PLAYOFF_BOX_W + PLAYOFF_COL_GAP
PLAYOFF_MARGIN = 40
# The grand-final reset is routed in the channel to the right of its column.
PLAYOFF_RIGHT_MARGIN = PLAYOFF_MARGIN + PLAYOFF_COL_GAP // 2
PLAYOFF_HEADER_H = 48
PLAYOFF_LEAF_STEP = PLAYOFF_BOX_H + 20
PLAYOFF_L_LEAF_STEP = PLAYOFF_LEAF_STEP * 2
PLAYOFF_BAND_GAP = 120
PLAYOFF_GF_STEP = PLAYOFF_BOX_H + 52
PLAYOFF_COLS = 8
PLAYOFF_CONNECTOR = (170, 178, 190)
PLAYOFF_LABEL_COLOR = (195, 202, 214)
PLAYOFF_RESET_COLOR = (200, 60, 60)
PLAYOFF_CHAMPION_COLOR = (245, 196, 70)
PLAYOFF_LABEL_FONT = ImageFont.truetype(
    font=asset_path("fonts", "Agdasima-Regular.ttf"), size=20
)
PLAYOFF_FEEDER_FONT = ImageFont.truetype(
    font=asset_path("fonts", "Agdasima-Regular.ttf"), size=19
)


def _round_number(slot: str) -> int:
    return int(slot.split("-R")[1].split("-")[0])


def _slot_column(slot: str) -> int:
    if slot.startswith("W-"):
        return _round_number(slot) - 1
    if slot.startswith("L-"):
        return _round_number(slot)
    return PLAYOFF_COLS - 1


def _build_playoff_layout():
    """Lay out the bracket from its feeder graph, not from arbitrary offsets.

    Winners bracket: leaves are W-R1; every later match is centered between the
    two matches feeding it. Losers bracket: leaves are L-R1; later matches are
    centered over their losers-bracket feeder(s), while W losers enter from the
    upper band. The grand final sits between the two bands.
    """
    w_slots = [row[0] for row in PLAYOFF_BRACKET if row[0].startswith("W-")]
    l_slots = [row[0] for row in PLAYOFF_BRACKET if row[0].startswith("L-")]

    w_top = PLAYOFF_MARGIN + PLAYOFF_HEADER_H
    w_center_base = w_top + PLAYOFF_BOX_H / 2
    centers: dict[str, float] = {}

    w_r1 = [slot for slot in w_slots if slot.startswith("W-R1-")]
    for i, slot in enumerate(w_r1):
        centers[slot] = w_center_base + i * PLAYOFF_LEAF_STEP

    for slot in w_slots:
        if slot in centers:
            continue
        feeders = [
            src
            for src, outcome, _side in ADVANCEMENT_REVERSE_MAP.get(slot, [])
            if outcome == "winner" and src.startswith("W-")
        ]
        if not feeders:
            raise ValueError(f"No winners-bracket feeder for {slot}")
        centers[slot] = sum(centers[src] for src in feeders) / len(feeders)

    w_bottom = max(centers[slot] + PLAYOFF_BOX_H / 2 for slot in w_slots)

    l_top = w_bottom + PLAYOFF_BAND_GAP
    l_center_base = l_top + PLAYOFF_BOX_H / 2
    l_r1 = [slot for slot in l_slots if slot.startswith("L-R1-")]
    for i, slot in enumerate(l_r1):
        centers[slot] = l_center_base + i * PLAYOFF_L_LEAF_STEP

    for slot in l_slots:
        if slot in centers:
            continue
        feeders = [
            src
            for src, outcome, _side in ADVANCEMENT_REVERSE_MAP.get(slot, [])
            if outcome == "winner" and src.startswith("L-")
        ]
        if not feeders:
            raise ValueError(f"No losers-bracket feeder for {slot}")
        centers[slot] = sum(centers[src] for src in feeders) / len(feeders)

    l_bottom = max(centers[slot] + PLAYOFF_BOX_H / 2 for slot in l_slots)

    gf_center = (centers["W-R4-M1"] + centers["L-R6-M1"]) / 2
    centers["GF-M1"] = gf_center
    centers["GF-M2"] = gf_center + PLAYOFF_GF_STEP

    positions = {
        slot: (_slot_column(slot), int(round(center - PLAYOFF_BOX_H / 2)))
        for slot, center in centers.items()
    }
    width = (
        PLAYOFF_MARGIN
        + PLAYOFF_COLS * PLAYOFF_BOX_W
        + (PLAYOFF_COLS - 1) * PLAYOFF_COL_GAP
        + PLAYOFF_RIGHT_MARGIN
    )
    height = int(
        round(max(center + PLAYOFF_BOX_H / 2 for center in centers.values()) + PLAYOFF_MARGIN)
    )
    return positions, width, height, centers, w_top, w_bottom, l_top, l_bottom


(
    PLAYOFF_SLOT_POS,
    PLAYOFF_LAYOUT_W,
    PLAYOFF_LAYOUT_H,
    _PLAYOFF_CENTERS,
    _PLAYOFF_W_TOP,
    _PLAYOFF_W_BOTTOM,
    _PLAYOFF_L_TOP,
    _PLAYOFF_L_BOTTOM,
) = _build_playoff_layout()
_PLAYOFF_CORRIDOR_Y = (_PLAYOFF_W_BOTTOM + _PLAYOFF_L_TOP) / 2


def _card_rect(slot: str) -> tuple[float, float, float, float]:
    col, top = PLAYOFF_SLOT_POS[slot]
    left = PLAYOFF_MARGIN + col * PLAYOFF_COL_STEP
    return left, top, left + PLAYOFF_BOX_W, top + PLAYOFF_BOX_H


def _feeder_label(code: str | None, row_by_id: dict[int, list]) -> str:
    """'PO266W' -> 'Winner of Organ vs Dummy' using the feeder's real players."""
    if not code:
        return "TBA"
    try:
        feeder_id = int(code[2:-1])
    except (TypeError, ValueError):
        return "TBA"
    feeder = row_by_id.get(feeder_id)
    if not feeder:
        return "TBA"
    names = [name for name in (feeder[5], feeder[6]) if name]
    if len(names) < 2:
        return "TBA"
    outcome = "Winner" if code[-1] == "W" else "Loser"
    return f"{outcome} of {names[0]} vs {names[1]}"


def _playoff_placeholder_font(label: str):
    """Feeder text is slightly larger than the old side font; TBA stays normal."""
    return name_font if label == "TBA" else PLAYOFF_FEEDER_FONT


def _playoff_placeholder_x(left: float) -> float:
    """Center placeholder text across the whole player half, pfp included."""
    return left + PLAYOFF_PLAYER_W / 2


def _playoff_champion(
    slot_map: dict[str, list[list]],
) -> tuple[int | None, str | None]:
    """Return (winner discord_id, deciding slot) once the grand final is over.

    GF-M2 is only created when the losers-bracket champion wins GF-M1, so a
    populated GF-M2 is always the deciding card.
    """
    for slot in ("GF-M2", "GF-M1"):
        rows = slot_map.get(slot) or []
        if not rows:
            continue
        wins: dict[int, int] = {}
        for row in rows:
            if row[4]:
                wins[row[4]] = wins.get(row[4], 0) + 1
        for player_id, count in wins.items():
            if count >= 2:
                return player_id, slot
        # A started but unfinished series has no champion yet.
        return None, None
    return None, None


def _make_po_box(
    img: Image.Image, d: ImageDraw.ImageDraw,
    left: float | int, top: float | int,
    matches: list[list], letter: str,
    color=playoff_color,
    row_by_id: dict[int, list] | None = None,
    champion_id: int | None = None,
    champion_slot: str | None = None,
):
    """Draw a playoff bracket box with BO3 score columns per player row."""
    row_by_id = row_by_id or {}
    d.rectangle(
        xy=(left, top, left + PLAYOFF_BOX_W, top + PLAYOFF_BOX_H),
        fill=color,
        outline="white",
    )
    d.line(xy=(left, top + PLAYOFF_ROW_H, left + PLAYOFF_BOX_W, top + PLAYOFF_ROW_H))

    d.line(
        xy=(left + PLAYOFF_PLAYER_W, top, left + PLAYOFF_PLAYER_W, top + PLAYOFF_BOX_H)
    )
    for ci in range(1, 4):
        x = left + PLAYOFF_PLAYER_W + PLAYOFF_SCORE_W * ci
        d.line(xy=(x, top, x, top + PLAYOFF_BOX_H))

    # Champion card gets a gold border on top of the standard white one.
    if champion_id and champion_slot == letter:
        d.rectangle(
            xy=(left, top, left + PLAYOFF_BOX_W, top + PLAYOFF_BOX_H),
            outline=PLAYOFF_CHAMPION_COLOR,
            width=3,
        )

    slot_matches = [m for m in matches if m[12] == letter]
    slot_matches.sort(key=lambda x: (x[13] or 0, x[9]))
    if slot_matches:
        m_last = slot_matches[-1]
        p1_id = m_last[0]
        p2_id = m_last[1]
        p1_label = _feeder_label(m_last[10], row_by_id)
        p2_label = _feeder_label(m_last[11], row_by_id)
    else:
        p1_id = None
        p2_id = None
        p1_label = p2_label = "TBA"

    if letter == "GF-M2" and not slot_matches:
        p1_label = p2_label = "Bracket Reset"

    score_colors_p1: list[tuple[int, int, int]] = []
    score_colors_p2: list[tuple[int, int, int]] = []
    for m in slot_matches[:3]:
        if m[4] and p1_id and m[4] == p1_id:
            score_colors_p1.append(softgreen)
            score_colors_p2.append(softred)
        elif m[4] and p2_id and m[4] == p2_id:
            score_colors_p1.append(softred)
            score_colors_p2.append(softgreen)
        else:
            score_colors_p1.append((255, 255, 255))
            score_colors_p2.append((255, 255, 255))

    # ── Player 1 row ──────────────────────────────────────────
    if p1_id:
        try:
            pfp = Image.open(fp=asset_path("pfps", f"{p1_id}.png")).resize(pfp_size).convert("RGBA")
            img.paste(
                box=(
                    int(left + (PLAYOFF_PFP_W - pfp_size[0]) / 2),
                    int((PLAYOFF_PFP_W - pfp_size[0]) / 2 + top),
                ),
                im=pfp,
                mask=mask,
            )
        except FileNotFoundError:
            try:
                pfp = Image.open(fp=asset_path("pfps", "default.png")).resize(pfp_size).convert("RGBA")
                img.paste(
                    box=(
                        int(left + (PLAYOFF_PFP_W - pfp_size[0]) / 2),
                        int((PLAYOFF_PFP_W - pfp_size[0]) / 2 + top),
                    ),
                    im=pfp,
                    mask=mask,
                )
            except FileNotFoundError:
                pass
        d.text(
            xy=(left + PLAYOFF_PFP_W + PLAYOFF_FLAG_W + PLAYOFF_NAME_W / 2, top + PLAYOFF_ROW_H * 0.5),
            text=_fit_name(m_last[5], PLAYOFF_NAME_W - 4),
            fill=PLAYOFF_CHAMPION_COLOR if p1_id == champion_id else "white",
            font=name_font,
            anchor="mm",
        )
        try:
            flag = get_flag(m_last[7])
            img.paste(
                im=flag,
                box=(
                    int(left + PLAYOFF_PFP_W + (PLAYOFF_FLAG_W - flag.width) / 2),
                    int(top + (PLAYOFF_ROW_H - flag.height) / 2 + 1),
                ),
                mask=flag,
            )
        except Exception:
            pass
    else:
        font = _playoff_placeholder_font(p1_label)
        d.text(
            xy=(
                _playoff_placeholder_x(left),
                top + PLAYOFF_ROW_H * 0.5,
            ),
            text=_fit_name(p1_label, PLAYOFF_PLAYER_W - 4, font),
            fill="white",
            font=font,
            anchor="mm",
        )

    # ── Player 2 row ──────────────────────────────────────────
    if p2_id:
        try:
            pfp = Image.open(fp=asset_path("pfps", f"{p2_id}.png")).resize(pfp_size).convert("RGBA")
            img.paste(
                box=(
                    int(left + (PLAYOFF_PFP_W - pfp_size[0]) / 2),
                    int((PLAYOFF_PFP_W - pfp_size[0]) / 2 + top + PLAYOFF_ROW_H),
                ),
                im=pfp,
                mask=mask,
            )
        except FileNotFoundError:
            try:
                pfp = Image.open(fp=asset_path("pfps", "default.png")).resize(pfp_size).convert("RGBA")
                img.paste(
                    box=(
                        int(left + (PLAYOFF_PFP_W - pfp_size[0]) / 2),
                        int((PLAYOFF_PFP_W - pfp_size[0]) / 2 + top + PLAYOFF_ROW_H),
                    ),
                    im=pfp,
                    mask=mask,
                )
            except FileNotFoundError:
                pass
        d.text(
            xy=(left + PLAYOFF_PFP_W + PLAYOFF_FLAG_W + PLAYOFF_NAME_W / 2, top + PLAYOFF_ROW_H * 1.5),
            text=_fit_name(m_last[6], PLAYOFF_NAME_W - 4),
            fill=PLAYOFF_CHAMPION_COLOR if p2_id == champion_id else "white",
            font=name_font,
            anchor="mm",
        )
        try:
            flag = get_flag(m_last[8])
            img.paste(
                im=flag,
                box=(
                    int(left + PLAYOFF_PFP_W + (PLAYOFF_FLAG_W - flag.width) / 2),
                    int(top + PLAYOFF_ROW_H + (PLAYOFF_ROW_H - flag.height) / 2 + 1),
                ),
                mask=flag,
            )
        except Exception:
            pass
    else:
        font = _playoff_placeholder_font(p2_label)
        d.text(
            xy=(
                _playoff_placeholder_x(left),
                top + PLAYOFF_ROW_H * 1.5,
            ),
            text=_fit_name(p2_label, PLAYOFF_PLAYER_W - 4, font),
            fill="white",
            font=font,
            anchor="mm",
        )

    # ── BO3 game scores across the 3 fixed-width columns ──────
    for i in range(min(len(slot_matches), 3)):
        cx = left + PLAYOFF_PLAYER_W + PLAYOFF_SCORE_W * i + PLAYOFF_SCORE_W / 2
        # Unplayed games carry default goals of 0 — leave their cells blank so
        # a future card does not look like a 0-0 result.
        played = bool(slot_matches[i][4])
        sc1 = str(slot_matches[i][2]) if played and slot_matches[i][2] is not None else ""
        sc2 = str(slot_matches[i][3]) if played and slot_matches[i][3] is not None else ""
        col1 = score_colors_p1[i] if i < len(score_colors_p1) else (255, 255, 255)
        col2 = score_colors_p2[i] if i < len(score_colors_p2) else (255, 255, 255)
        d.text(
            xy=(cx, top + PLAYOFF_ROW_H * 0.5),
            text=sc1,
            fill=col1,
            font=name_font,
            anchor="mm",
        )
        d.text(
            xy=(cx, top + PLAYOFF_ROW_H * 1.5),
            text=sc2,
            fill=col2,
            font=name_font,
            anchor="mm",
        )


def _connector_source_y(src_slot: str) -> float:
    """Always leave a card from its center, even before a winner is known."""
    return PLAYOFF_SLOT_POS[src_slot][1] + PLAYOFF_BOX_H / 2


def _connector_points(
    src_slot: str, src_y: float, tgt_slot: str, tgt_y: float
) -> list[tuple[float, float]]:
    """Route an orthogonal connector in the gaps between rounds."""
    src_col, _src_top = PLAYOFF_SLOT_POS[src_slot]
    tgt_col, _tgt_top = PLAYOFF_SLOT_POS[tgt_slot]
    src_left = PLAYOFF_MARGIN + src_col * PLAYOFF_COL_STEP
    tgt_left = PLAYOFF_MARGIN + tgt_col * PLAYOFF_COL_STEP
    src_right = src_left + PLAYOFF_BOX_W
    tgt_right = tgt_left + PLAYOFF_BOX_W

    if src_col == tgt_col:
        channel = src_right + PLAYOFF_COL_GAP / 2
        return [
            (src_right, src_y),
            (channel, src_y),
            (channel, tgt_y),
            (tgt_right, tgt_y),
        ]

    src_gap = src_right + PLAYOFF_COL_GAP / 2
    tgt_gap = tgt_left - PLAYOFF_COL_GAP / 2

    # A W->L drop that skips a column needs a horizontal run between the two
    # bands; otherwise it would cut through unrelated L cards.
    if (
        not src_slot.startswith("L-")
        and tgt_slot.startswith("L-")
        and abs(tgt_col - src_col) > 1
    ):
        return [
            (src_right, src_y),
            (src_gap, src_y),
            (src_gap, _PLAYOFF_CORRIDOR_Y),
            (tgt_gap, _PLAYOFF_CORRIDOR_Y),
            (tgt_gap, tgt_y),
            (tgt_left, tgt_y),
        ]

    return [
        (src_right, src_y),
        (src_gap, src_y),
        (src_gap, tgt_y),
        (tgt_left, tgt_y),
    ]


def _draw_connector(
    d: ImageDraw.ImageDraw,
    src: str,
    side: int | None,
    tgt: str,
) -> None:
    src_y = _connector_source_y(src)
    tgt_top = PLAYOFF_SLOT_POS[tgt][1]
    if side is None:
        tgt_y = tgt_top + PLAYOFF_BOX_H / 2
    else:
        tgt_y = tgt_top + side * PLAYOFF_ROW_H + PLAYOFF_ROW_H / 2

    points = _connector_points(src, src_y, tgt, tgt_y)
    d.line(points, fill=PLAYOFF_CONNECTOR, width=2)


def _draw_connectors(
    d: ImageDraw.ImageDraw,
    allowed_slots: set[str] | None = None,
) -> None:
    for tgt, sources in ADVANCEMENT_REVERSE_MAP.items():
        if tgt not in PLAYOFF_SLOT_POS:
            continue
        if allowed_slots is not None and tgt not in allowed_slots:
            continue
        for src, _outcome, side in sources:
            if src in PLAYOFF_SLOT_POS and (
                allowed_slots is None or src in allowed_slots
            ):
                _draw_connector(d, src, side, tgt)

    # The reset is created dynamically, so it is not in the static reverse map.
    if allowed_slots is None or "GF-M1" in allowed_slots:
        _draw_connector(d, "GF-M1", None, "GF-M2")


def _column_label_top(prefix: str, col: int) -> float | None:
    tops = [
        top
        for slot, (slot_col, top) in PLAYOFF_SLOT_POS.items()
        if slot.startswith(prefix) and slot_col == col
    ]
    return min(tops) if tops else None


def _draw_labels(
    d: ImageDraw.ImageDraw,
    allowed_slots: set[str] | None = None,
    show_grand_final: bool = True,
    champion_name: str | None = None,
) -> None:
    def allowed(slot: str) -> bool:
        return allowed_slots is None or slot in allowed_slots

    if any(allowed(slot) for slot in PLAYOFF_SLOT_POS if slot.startswith("W-")):
        d.text(
            xy=(PLAYOFF_MARGIN, PLAYOFF_MARGIN + 4),
            text="WINNERS BRACKET",
            fill=PLAYOFF_LABEL_COLOR,
            font=PLAYOFF_LABEL_FONT,
            anchor="lm",
        )
    if any(allowed(slot) for slot in PLAYOFF_SLOT_POS if slot.startswith("L-")):
        d.text(
            xy=(PLAYOFF_MARGIN, _PLAYOFF_L_TOP - 34),
            text="LOSERS BRACKET",
            fill=PLAYOFF_LABEL_COLOR,
            font=PLAYOFF_LABEL_FONT,
            anchor="lm",
        )

    for col, label in enumerate(("W R1", "W R2", "W R3", "W R4")):
        if not any(
            allowed(slot)
            for slot in PLAYOFF_SLOT_POS
            if slot.startswith("W-") and PLAYOFF_SLOT_POS[slot][0] == col
        ):
            continue
        top = _column_label_top("W-", col)
        if top is None:
            continue
        d.text(
            xy=(PLAYOFF_MARGIN + col * PLAYOFF_COL_STEP + 6, top - 18),
            text=label,
            fill=PLAYOFF_LABEL_COLOR,
            font=PLAYOFF_LABEL_FONT,
            anchor="lm",
        )
    for col, label in enumerate(("L R1", "L R2", "L R3", "L R4", "L R5", "L R6"), 1):
        if not any(
            allowed(slot)
            for slot in PLAYOFF_SLOT_POS
            if slot.startswith("L-") and PLAYOFF_SLOT_POS[slot][0] == col
        ):
            continue
        top = _column_label_top("L-", col)
        if top is None:
            continue
        d.text(
            xy=(PLAYOFF_MARGIN + col * PLAYOFF_COL_STEP + 6, top - 18),
            text=label,
            fill=PLAYOFF_LABEL_COLOR,
            font=PLAYOFF_LABEL_FONT,
            anchor="lm",
        )

    if show_grand_final and allowed("GF-M1"):
        gf_left = PLAYOFF_MARGIN + (PLAYOFF_COLS - 1) * PLAYOFF_COL_STEP + 6
        if champion_name:
            d.text(
                xy=(gf_left, PLAYOFF_SLOT_POS["GF-M1"][1] - 56),
                text=_fit_name(
                    f"CHAMPION: {champion_name}", PLAYOFF_BOX_W - 10, PLAYOFF_LABEL_FONT
                ),
                fill=PLAYOFF_CHAMPION_COLOR,
                font=PLAYOFF_LABEL_FONT,
                anchor="lm",
            )
        d.text(
            xy=(gf_left, PLAYOFF_SLOT_POS["GF-M1"][1] - 24),
            text="GRAND FINAL",
            fill=PLAYOFF_LABEL_COLOR,
            font=PLAYOFF_LABEL_FONT,
            anchor="lm",
        )
        if "GF-M2" in PLAYOFF_SLOT_POS and allowed("GF-M2"):
            d.text(
                xy=(gf_left, PLAYOFF_SLOT_POS["GF-M2"][1] - 20),
                text="BRACKET RESET",
                fill=PLAYOFF_RESET_COLOR,
                font=PLAYOFF_LABEL_FONT,
                anchor="lm",
            )


async def get_playoff(view: str = "full", season: int | None = None):
    view = (view or "full").lower()
    if view not in {"full", "winners", "losers"}:
        view = "full"

    async with get_db_manager().get_session() as session:
        season = await _get_season(session, season)
        filename = asset_path("tables", f"playoff_s{season}_{view}.png")
        event = await _get_event(session, EventType.PLAYOFF, season)
        matches = []
        if event:
            stmt = (
                select(DbMatch)
                .options(selectinload(DbMatch.match_players).selectinload(DbMatchPlayer.player))
                .where(DbMatch.event_id == event.id)
                .order_by(DbMatch.id)
            )
            result = await session.execute(stmt)
            matches = _build_match_rows(result.scalars().unique().all(), "PO")

        table_hash = hashlib.sha3_256(
            bytes(
                repr(_match_signature(matches))
                + f"playoff_v11_{view}_{season}",
                "utf-8",
            )
        ).hexdigest()
        link = await _get_cached_link(session, table_hash)
        if link:
            return link, 0, 0

    slot_map: dict[str, list[list]] = {}
    for match in matches:
        slot_map.setdefault(match[12], []).append(match)

    if view == "winners":
        allowed_slots = {s for s in PLAYOFF_SLOT_POS if s.startswith("W-")}
        allowed_slots |= {"GF-M1", "GF-M2"}
    elif view == "losers":
        allowed_slots = {s for s in PLAYOFF_SLOT_POS if s.startswith("L-")}
    else:
        allowed_slots = set(PLAYOFF_SLOT_POS)

    row_by_id = {
        int(match[9][2:]): match
        for match in matches
        if match[9] and match[9][2:].isdigit()
    }

    champion_id, champion_slot = _playoff_champion(slot_map)
    champion_name = None
    if champion_id:
        for match in matches:
            if match[12] == champion_slot and match[4] == champion_id:
                champion_name = match[5] if match[0] == champion_id else match[6]
                break

    def _render_playoff_bracket(raw_slot_map, filename_copy):
        img = Image.new(
            mode="RGBA",
            size=(PLAYOFF_LAYOUT_W, PLAYOFF_LAYOUT_H),
            color=bg_color,
        )
        d = ImageDraw.Draw(img)

        _draw_connectors(d, allowed_slots)
        for slot, (col, top) in PLAYOFF_SLOT_POS.items():
            if slot not in allowed_slots:
                continue
            _make_po_box(
                img=img,
                d=d,
                left=PLAYOFF_MARGIN + col * PLAYOFF_COL_STEP,
                top=top,
                matches=raw_slot_map.get(slot, []),
                letter=slot,
                color=PLAYOFF_RESET_COLOR if slot == "GF-M2" else playoff_color,
                row_by_id=row_by_id,
                champion_id=champion_id,
                champion_slot=champion_slot,
            )
        _draw_labels(
            d,
            allowed_slots,
            show_grand_final=(view in ("full", "winners")),
            champion_name=champion_name if view in ("full", "winners") else None,
        )

        if view != "full":
            xs = []
            ys = []
            for slot in allowed_slots:
                col, top = PLAYOFF_SLOT_POS[slot]
                left = PLAYOFF_MARGIN + col * PLAYOFF_COL_STEP
                xs.extend((left, left + PLAYOFF_BOX_W))
                ys.extend((top - 34, top + PLAYOFF_BOX_H))
            if champion_name and view == "winners":
                ys.append(PLAYOFF_SLOT_POS["GF-M1"][1] - 56)
            if xs and ys:
                pad = 48
                crop = (
                    max(0, int(min(xs) - pad)),
                    max(0, int(min(ys) - pad)),
                    min(PLAYOFF_LAYOUT_W, int(max(xs) + pad)),
                    min(PLAYOFF_LAYOUT_H, int(max(ys) + pad)),
                )
                img = img.crop(crop)

        img.save(fp=filename_copy, format="png")
        return 0, filename_copy, table_hash

    return await asyncio.to_thread(_render_playoff_bracket, slot_map, filename)
