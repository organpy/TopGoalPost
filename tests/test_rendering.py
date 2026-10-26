"""Rendering layout and label tests."""

import pytest
from PIL import Image, ImageDraw

from bot.db import ImageCacheRepository
from bot.db.models import EventType, Season, SeasonStatus
from bot.rendering.StandingsMaker import (
    _fit_name,
    _get_season,
    _playin_label_x,
    _playin_player_label,
    box_w,
    flag_w,
    name_font,
    name_w,
    pfp_w,
    swiss_specs,
    swiss_texts,
)
from bot.rendering.playoff import (
    PLAYOFF_BOX_H,
    PLAYOFF_BOX_W,
    PLAYOFF_CHAMPION_COLOR,
    PLAYOFF_COL_GAP,
    PLAYOFF_COL_STEP,
    PLAYOFF_FEEDER_FONT,
    PLAYOFF_GF_STEP,
    PLAYOFF_LAYOUT_H,
    PLAYOFF_LAYOUT_W,
    PLAYOFF_MARGIN,
    PLAYOFF_NAME_W,
    PLAYOFF_PLAYER_W,
    PLAYOFF_ROW_H,
    PLAYOFF_SCORE_W,
    PLAYOFF_SLOT_POS,
    _card_rect,
    _champion_banner_rect,
    _connector_points,
    _connector_source_y,
    _draw_champion_banner,
    _draw_losers_feed_line,
    _losers_feed_geometry,
    _playoff_champion,
    _playoff_placeholder_font,
    _playoff_placeholder_x,
    _winners_positions,
)
from bot.services.pairing import ADVANCEMENT_REVERSE_MAP, PLAYOFF_BRACKET


def _card_center(slot: str) -> float:
    return PLAYOFF_SLOT_POS[slot][1] + PLAYOFF_BOX_H / 2


def _row_center(slot: str, side: int | None) -> float:
    top = PLAYOFF_SLOT_POS[slot][1]
    if side is None:
        return top + PLAYOFF_BOX_H / 2
    return top + side * PLAYOFF_ROW_H + PLAYOFF_ROW_H / 2


def _segment_hits_card_interior(a, b, rect) -> bool:
    x1, y1 = a
    x2, y2 = b
    left, top, right, bottom = rect
    if y1 == y2 and top < y1 < bottom:
        return max(min(x1, x2), left) < min(max(x1, x2), right)
    if x1 == x2 and left < x1 < right:
        return max(min(y1, y2), top) < min(max(y1, y2), bottom)
    return False


def test_playoff_layout_covers_every_slot():
    expected = {row[0] for row in PLAYOFF_BRACKET} | {"GF-M2"}
    assert set(PLAYOFF_SLOT_POS) == expected

    positions = list(PLAYOFF_SLOT_POS.values())
    assert len(positions) == len(set(positions))


def test_playoff_advancement_references_known_slots():
    for target, sources in ADVANCEMENT_REVERSE_MAP.items():
        assert target in PLAYOFF_SLOT_POS
        for source, _outcome, _side in sources:
            assert source in PLAYOFF_SLOT_POS


def test_playoff_cards_are_wide_and_scores_are_fixed_width():
    assert PLAYOFF_NAME_W >= 240
    assert PLAYOFF_BOX_W == PLAYOFF_PLAYER_W + PLAYOFF_SCORE_W * 3
    assert PLAYOFF_LAYOUT_W > 4000


def test_playoff_cards_do_not_overlap():
    rects = {slot: _card_rect(slot) for slot in PLAYOFF_SLOT_POS}

    for slot, (left, top, right, bottom) in rects.items():
        for other, (o_left, o_top, o_right, o_bottom) in rects.items():
            if slot == other:
                continue
            assert right <= o_left or o_right <= left or bottom <= o_top or o_bottom <= top


def test_playoff_parents_are_centered_on_feeders():
    assert _card_center("W-R2-M1") == pytest.approx(
        (_card_center("W-R1-M1") + _card_center("W-R1-M2")) / 2,
        abs=0.6,
    )
    assert _card_center("W-R3-M1") == pytest.approx(
        (_card_center("W-R2-M1") + _card_center("W-R2-M2")) / 2,
        abs=0.6,
    )
    assert _card_center("W-R4-M1") == pytest.approx(
        (_card_center("W-R3-M1") + _card_center("W-R3-M2")) / 2,
        abs=0.6,
    )
    assert _card_center("L-R3-M1") == pytest.approx(
        (_card_center("L-R2-M1") + _card_center("L-R2-M2")) / 2,
        abs=0.6,
    )
    assert _card_center("L-R5-M1") == pytest.approx(
        (_card_center("L-R4-M1") + _card_center("L-R4-M2")) / 2,
        abs=0.6,
    )
    assert _card_center("GF-M1") == pytest.approx(
        (_card_center("W-R4-M1") + _card_center("L-R6-M1")) / 2,
        abs=0.6,
    )


def test_playoff_connectors_do_not_cross_cards():
    rects = {slot: _card_rect(slot) for slot in PLAYOFF_SLOT_POS}
    edges = [
        (src, outcome, side, tgt)
        for tgt, sources in ADVANCEMENT_REVERSE_MAP.items()
        if tgt in PLAYOFF_SLOT_POS
        for src, outcome, side in sources
        if src in PLAYOFF_SLOT_POS
    ]
    edges.append(("GF-M1", "winner", None, "GF-M2"))

    for src, _outcome, side, tgt in edges:
        tgt_y = _row_center(tgt, side)
        for src_side in (None, 0, 1):
            src_y = _row_center(src, src_side)
            points = _connector_points(src, src_y, tgt, tgt_y)
            for point in points:
                assert 0 <= point[0] <= PLAYOFF_LAYOUT_W
                assert 0 <= point[1] <= PLAYOFF_LAYOUT_H
            for a, b in zip(points, points[1:]):
                for slot, rect in rects.items():
                    assert not _segment_hits_card_interior(a, b, rect), (
                        f"{src}->{tgt} crosses {slot}"
                    )


def test_playoff_placeholder_text_is_centered_and_feeder_font_is_larger():
    assert _playoff_placeholder_font("TBA") is name_font
    assert _playoff_placeholder_font("Winner of A vs B") is PLAYOFF_FEEDER_FONT
    assert PLAYOFF_FEEDER_FONT.size > 15
    assert PLAYOFF_FEEDER_FONT.size < name_font.size

    left = 123
    assert _playoff_placeholder_x(left) == left + PLAYOFF_PLAYER_W / 2


def test_winners_view_moves_grand_final_beside_w_r4():
    positions = _winners_positions()
    gf_col, gf_top = positions["GF-M1"]

    assert gf_col == positions["W-R4-M1"][0] + 1
    assert gf_top == positions["W-R4-M1"][1]
    assert positions["GF-M2"] == (gf_col, gf_top + PLAYOFF_GF_STEP)

    def right_edge(slot: str) -> float:
        col, _top = positions[slot]
        return PLAYOFF_MARGIN + col * PLAYOFF_COL_STEP + PLAYOFF_BOX_W

    allowed = {s for s in positions if s.startswith("W-")}
    allowed |= {"GF-M1", "GF-M2"}
    assert max(right_edge(slot) for slot in allowed) < (
        PLAYOFF_MARGIN
        + PLAYOFF_SLOT_POS["GF-M1"][0] * PLAYOFF_COL_STEP
        + PLAYOFF_BOX_W
    )


def test_winners_losers_feed_enters_second_gf_row_from_below():
    positions = _winners_positions()
    gf_left = (
        PLAYOFF_MARGIN + positions["GF-M1"][0] * PLAYOFF_COL_STEP
    )
    bottom = 720.0
    feed_x, row_y, returned_bottom = _losers_feed_geometry(
        positions, bottom
    )

    assert returned_bottom == bottom
    assert feed_x == gf_left - PLAYOFF_COL_GAP / 4
    assert row_y == positions["GF-M1"][1] + PLAYOFF_ROW_H * 1.5
    assert feed_x < gf_left
    assert feed_x > (
        PLAYOFF_MARGIN
        + positions["W-R4-M1"][0] * PLAYOFF_COL_STEP
        + PLAYOFF_BOX_W
    )

    class _Draw:
        points = None

        def line(self, points, fill, width):
            self.points = points

    d = _Draw()
    _draw_losers_feed_line(d, positions, bottom)

    assert d.points == [(feed_x, bottom), (feed_x, row_y), (gf_left, row_y)]
    # The vertical run stays left of the reset card.
    reset_left = (
        PLAYOFF_MARGIN
        + positions["GF-M2"][0] * PLAYOFF_COL_STEP
    )
    assert feed_x < reset_left


def test_playoff_champion_banner_is_top_right_and_draws_gold():
    left, top, right, bottom = _champion_banner_rect()
    assert top == PLAYOFF_MARGIN
    assert right == PLAYOFF_LAYOUT_W - PLAYOFF_MARGIN
    assert left > PLAYOFF_LAYOUT_W / 2
    assert bottom < PLAYOFF_SLOT_POS["GF-M1"][1]

    img = Image.new(
        "RGBA", (PLAYOFF_LAYOUT_W, PLAYOFF_LAYOUT_H), (0, 0, 0, 0)
    )
    d = ImageDraw.Draw(img)
    _draw_champion_banner(img, d, (123, "Champion", "US"))

    assert img.getpixel((int(left) + 30, int(top))) == (
        *PLAYOFF_CHAMPION_COLOR,
        255,
    )


def test_playoff_connector_leaves_every_card_from_its_center():
    for slot in PLAYOFF_SLOT_POS:
        top = PLAYOFF_SLOT_POS[slot][1]
        assert _connector_source_y(slot) == top + PLAYOFF_BOX_H / 2

    src = "W-R1-M1"
    tgt = "W-R2-M1"
    src_y = _connector_source_y(src)
    points = _connector_points(src, src_y, tgt, _card_center(tgt))
    assert points[0][1] == src_y


def test_playin_label_has_no_hp():
    assert _playin_player_label("Player") == "Player"
    assert "HP" not in _playin_player_label("Player")
    assert _playin_player_label("") == "TBA"


def test_playin_feeder_labels_center_across_the_empty_half_box():
    name_column = pfp_w + flag_w + name_w / 2
    empty_half_box = (pfp_w + flag_w + name_w) / 2

    assert _playin_label_x(10, box_w, 0, True) == 10 + name_column
    assert _playin_label_x(10, box_w, 0, False) == 10 + empty_half_box
    assert _playin_label_x(10, box_w, 1, True) == 10 + box_w - name_column
    assert _playin_label_x(10, box_w, 1, False) == 10 + box_w - empty_half_box
    assert _playin_label_x(10, box_w, 0, False) < _playin_label_x(
        10, box_w, 1, False
    )


def test_swiss_survival_board_matches_round_counts():
    assert len(swiss_specs) == len(swiss_texts) == 5
    assert [sum(specs) for specs in swiss_specs] == [16, 16, 16, 12, 6]
    assert sum(sum(specs) for specs in swiss_specs) == 66
    for specs, titles in zip(swiss_specs, swiss_texts):
        assert len(specs) == len(titles)


def test_fit_name_clips_to_the_name_column():
    long_name = "A very long player name that cannot possibly fit"
    clipped = _fit_name(long_name)
    assert clipped.endswith("...")
    assert name_font.getlength(clipped) <= name_w - 4

    assert _fit_name("Short") == "Short"
    assert _fit_name("") == ""


async def test_get_season_does_not_default_to_one(db):
    async with db.get_session() as session:
        with pytest.raises(ValueError, match="No active season"):
            await _get_season(session)


async def test_swiss_renders_fixed_five_round_board(db, tmp_path, monkeypatch):
    from bot.rendering import StandingsMaker as standings

    target = tmp_path / "swiss.png"
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.commit()

    monkeypatch.setattr(standings, "asset_path", lambda *parts: str(target))

    _, filename, *_ = await standings.get_swiss()

    assert filename == str(target)
    assert target.stat().st_size > 0
    with Image.open(target) as img:
        # Five columns on the survival ladder.
        assert img.size[0] > 5 * 300


async def test_prerender_dispatches_finished_stage_and_never_raises(
    monkeypatch,
):
    from bot.rendering import StandingsMaker, prerender

    calls = []

    async def fake_open(season):
        calls.append(("OPEN", season))

    async def fake_playin(season):
        calls.append(("PLAYIN", season))

    async def fake_swiss(season):
        calls.append(("SWISS", season))

    monkeypatch.setattr(StandingsMaker, "get_open", fake_open)
    monkeypatch.setattr(StandingsMaker, "get_playin", fake_playin)
    monkeypatch.setattr(StandingsMaker, "get_swiss", fake_swiss)

    await prerender("OPEN", 3)
    await prerender("PLAYIN", 3)
    await prerender("SWISS", 3)
    assert calls == [("OPEN", 3), ("PLAYIN", 3), ("SWISS", 3)]

    async def fail(season):
        raise RuntimeError("render failed")

    monkeypatch.setattr(StandingsMaker, "get_open", fail)
    await prerender("OPEN", 3)


async def test_swiss_reuses_and_invalidates_the_local_render_cache(
    db, tmp_path, monkeypatch
):
    from bot.rendering import StandingsMaker as standings

    target = tmp_path / "swiss.png"
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.commit()

    monkeypatch.setattr(standings, "asset_path", lambda *parts: str(target))

    _, filename, table_hash = await standings.get_swiss()
    assert filename == str(target)
    async with db.get_session() as session:
        assert await ImageCacheRepository(session).get(table_hash) == str(target)

    cached = await standings.get_swiss()
    assert cached[0] == str(target)
    assert cached[1] == 0

    target.unlink()
    rerendered = await standings.get_swiss()
    assert rerendered[1] == str(target)
    assert target.exists()
    async with db.get_session() as session:
        assert await ImageCacheRepository(session).get(table_hash) == str(target)


async def test_swiss_renders_incremental_one_sided_slot(db, tmp_path, monkeypatch):
    from datetime import datetime
    from bot.db import Event, EventType, Match, MatchPlayer, Player
    from bot.rendering import StandingsMaker as standings
    from bot.rendering import asset_path as real_asset_path

    target = tmp_path / "swiss.png"
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.flush()
        p1 = Player(discord_id=101, name="Alice", country="us")
        p2 = Player(discord_id=102, name="Bob", country="fr")
        session.add_all([p1, p2])
        await session.flush()
        event = Event(name="Swiss", type=EventType.SWISS, season=1)
        session.add(event)
        await session.flush()
        r1 = Match(
            event_id=event.id,
            series_game=1,
            bracket_slot="SW-R1-M1",
            completed_at=datetime.utcnow(),
        )
        r2 = Match(
            event_id=event.id,
            series_game=2,
            bracket_slot="SW-R2-M1",
        )
        session.add_all([r1, r2])
        await session.flush()
        session.add_all(
            [
                MatchPlayer(
                    match_id=r1.id,
                    side=0,
                    player_id=p1.id,
                    goals=1,
                    is_winner=True,
                ),
                MatchPlayer(
                    match_id=r1.id,
                    side=1,
                    player_id=p2.id,
                    goals=0,
                    is_winner=False,
                ),
                MatchPlayer(match_id=r2.id, side=0, player_id=p1.id),
                MatchPlayer(match_id=r2.id, side=1, player_id=None),
            ]
        )
        await session.commit()

    monkeypatch.setattr(
        standings,
        "asset_path",
        lambda *parts: (
            str(target)
            if parts == ("tables", "swiss_s1.png")
            else real_asset_path(*parts)
        ),
    )

    _, filename, *_ = await standings.get_swiss()

    assert filename == str(target)
    assert target.stat().st_size > 0


async def test_playoff_renders_empty_bracket(db, tmp_path, monkeypatch):
    from bot.rendering import playoff as playoff_module

    target = tmp_path / "playoff.png"
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.commit()

    monkeypatch.setattr(playoff_module, "asset_path", lambda *parts: str(target))

    _, filename, *_ = await playoff_module.get_playoff()

    assert filename == str(target)
    assert target.stat().st_size > 0
    with Image.open(target) as img:
        assert img.size == (PLAYOFF_LAYOUT_W, PLAYOFF_LAYOUT_H)


def test_swiss_bucket_parses_slot_names():
    from bot.rendering.StandingsMaker import _swiss_bucket

    assert _swiss_bucket("SW-R1-M4") == "M"
    assert _swiss_bucket("SW-R2-U1") == "U"
    assert _swiss_bucket("SW-R2-L8") == "L"
    assert _swiss_bucket("SW-R3-M12") == "M"
    assert _swiss_bucket("SW-R5-M6") == "M"
    assert _swiss_bucket(None) == "M"


def test_swiss_feeder_labels_use_match_ids():
    from bot.rendering.StandingsMaker import _feeder_label, _prereq_code

    assert _prereq_code("SW", 266, "winner") == "SW266W"
    assert _prereq_code("SW", 266, "loser") == "SW266L"
    assert _feeder_label("SW266W") == "Winner of 266"
    assert _feeder_label("SW266L") == "Loser of 266"


def test_playoff_feeder_label_uses_real_player_names():
    from bot.rendering.playoff import _feeder_label

    row = [1, 2, 5, 0, 1, "Organ", "Dummy", "US", "US", "PO266", None, None, "W-R1-M1", 1]
    row_by_id = {266: row}
    assert _feeder_label("PO266W", row_by_id) == "Winner of Organ vs Dummy"
    assert _feeder_label("PO266L", row_by_id) == "Loser of Organ vs Dummy"
    assert _feeder_label("PO999W", row_by_id) == "TBA"
    assert _feeder_label(None, row_by_id) == "TBA"


def test_swiss_specs_match_fixed_bracket_buckets():
    from bot.services.pairing import SWISS_BRACKET

    counts = {}
    for slot, _round, *_rest in SWISS_BRACKET:
        rnd = int(slot.split("-R")[1].split("-")[0])
        bucket = slot.split("-")[2][0]
        counts.setdefault(rnd, {}).setdefault(bucket, 0)
        counts[rnd][bucket] += 1

    expected = {
        1: {"M": 16},
        2: {"U": 8, "L": 8},
        3: {"U": 4, "M": 8, "L": 4},
        4: {"U": 6, "L": 6},
        5: {"M": 6},
    }
    assert counts == expected

    # The rendered column sizes line up with the bucket order used for display.
    from bot.rendering.StandingsMaker import swiss_letters

    for rnd in range(1, 6):
        assert set(swiss_letters[rnd - 1]) == set(expected[rnd])
        for letter, size in zip(swiss_letters[rnd - 1], swiss_specs[rnd - 1]):
            assert expected[rnd][letter] == size


async def test_playoff_winners_view_includes_grand_final(db, tmp_path, monkeypatch):
    from bot.rendering import playoff as playoff_module

    target = tmp_path / "playoff_winners.png"
    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.commit()

    monkeypatch.setattr(playoff_module, "asset_path", lambda *parts: str(target))

    _, filename, *_ = await playoff_module.get_playoff(view="winners")

    assert filename == str(target)
    with Image.open(target) as img:
        assert img.width > 0 and img.height > 0
        # The grand final is compacted beside W R4, so the crop ends there
        # instead of at the far-right full-bracket column.
        gf_right = (
            playoff_module.PLAYOFF_MARGIN
            + (
                playoff_module.PLAYOFF_SLOT_POS["W-R4-M1"][0] + 1
            )
            * playoff_module.PLAYOFF_COL_STEP
            + playoff_module.PLAYOFF_BOX_W
        )
        assert img.width <= gf_right + 49
        assert img.width < playoff_module.PLAYOFF_LAYOUT_W

    assert "GF-M1" in playoff_module.PLAYOFF_SLOT_POS
    assert "GF-M2" in playoff_module.PLAYOFF_SLOT_POS

def _po_row(p1, p2, score1, score2, winner, slot, game=1):
    return [
        p1, p2, score1, score2, winner,
        f"P{p1}", f"P{p2}", "US", "US",
        "PO1", None, None, slot, game,
    ]

def test_playoff_champion_needs_a_decided_series():
    assert _playoff_champion({}) == (None, None)
    # 1-0 in the grand final: still playing.
    assert _playoff_champion(
        {"GF-M1": [_po_row(1, 2, 5, 0, 1, "GF-M1")]}
    ) == (None, None)

def test_playoff_champion_from_grand_final():
    rows = [
        _po_row(1, 2, 5, 0, 1, "GF-M1", 1),
        _po_row(1, 2, 3, 2, 1, "GF-M1", 2),
    ]
    assert _playoff_champion({"GF-M1": rows}) == (1, "GF-M1")

    rows = [
        _po_row(1, 2, 5, 0, 1, "GF-M1", 1),
        _po_row(1, 2, 2, 3, 2, "GF-M1", 2),
        _po_row(1, 2, 1, 3, 2, "GF-M1", 3),
    ]
    assert _playoff_champion({"GF-M1": rows}) == (2, "GF-M1")

def test_playoff_champion_ignores_reset_winner_of_grand_final():
    # The losers-bracket champ won GF-M1, so the reset decides the title.
    gf1 = [
        _po_row(1, 2, 5, 0, 1, "GF-M1", 1),
        _po_row(1, 2, 2, 3, 2, "GF-M1", 2),
        _po_row(1, 2, 1, 3, 2, "GF-M1", 3),
    ]
    gf2 = [_po_row(1, 2, 4, 3, 1, "GF-M2", 1)]
    assert _playoff_champion({"GF-M1": gf1, "GF-M2": gf2}) == (None, None)

    gf2.append(_po_row(1, 2, 3, 1, 1, "GF-M2", 2))
    assert _playoff_champion({"GF-M1": gf1, "GF-M2": gf2}) == (1, "GF-M2")

def _count_champion_pixels(img, slot):
    from bot.rendering.playoff import PLAYOFF_CHAMPION_COLOR

    left, top, right, bottom = _card_rect(slot)
    card = img.convert("RGB").crop(
        (int(left), int(top), int(right), int(bottom))
    )
    colors = card.getcolors(maxcolors=1 << 20) or []
    return sum(count for count, color in colors if color == PLAYOFF_CHAMPION_COLOR)

async def test_playoff_champion_card_is_highlighted(db, tmp_path, monkeypatch):
    from bot.db.models import Match, MatchPlayer
    from bot.db.repositories import EventRepository, PlayerRepository
    from bot.rendering import playoff as playoff_module

    target = tmp_path / "playoff.png"
    monkeypatch.setattr(playoff_module, "asset_path", lambda *parts: str(target))

    async with db.get_session() as session:
        session.add(Season(number=1, status=SeasonStatus.ACTIVE))
        await session.commit()
        event = await EventRepository(session).create(
            "test", EventType.PLAYOFF, 1
        )
        champ = await PlayerRepository(session).create(
            discord_id=111, name="Champ"
        )
        runner = await PlayerRepository(session).create(
            discord_id=222, name="Runner"
        )
        for game, (s1, s2, winner) in enumerate(
            ((5, 0, champ), (3, 2, champ)), start=1
        ):
            match = Match(
                event_id=event.id, bracket_slot="GF-M1", series_game=game
            )
            session.add(match)
            await session.flush()
            session.add_all([
                MatchPlayer(
                    match_id=match.id, side=0, player_id=champ.id,
                    goals=s1, is_winner=winner is champ,
                ),
                MatchPlayer(
                    match_id=match.id, side=1, player_id=runner.id,
                    goals=s2, is_winner=winner is runner,
                ),
            ])
        await session.commit()

    _, filename, *_ = await playoff_module.get_playoff()

    assert filename == str(target)
    with Image.open(target) as img:
        # Gold border + gold name on the deciding card.
        assert _count_champion_pixels(img, "GF-M1") > 100
        # The reset card does not exist yet, so it stays plain.
        assert _count_champion_pixels(img, "GF-M2") == 0


def test_player_card_renders_every_state(tmp_path):
    """The card is pure: it renders from a dataclass with no database."""
    from bot.rendering.playercard import CARD_H, CARD_W, render_player_card
    from bot.services.player_stats import PlayerStats, StageRecord

    played = PlayerStats(
        player_id=1,
        name="organ",
        country="US",
        discord_id=1001,
        season=3,
        season_active=True,
        registered=True,
        open=StageRecord(games=4, wins=3, losses=1, goals_for=12, goals_against=6),
        playin=StageRecord(games=3, wins=2, losses=1, goals_for=7, goals_against=5),
        swiss=StageRecord(games=4, wins=3, losses=1, goals_for=10, goals_against=8),
        playoff=StageRecord(games=5, wins=3, losses=2, goals_for=13, goals_against=11),
        playin_rank=7,
        hp=2,
        champion=True,
    )
    skipped = PlayerStats(
        player_id=2,
        name="A name long enough to be clipped by the header",
        country="BR",
        discord_id=1002,
        season=3,
        season_active=True,
        registered=True,
        swiss_seed=4,
        hp=4,
    )
    empty = PlayerStats(
        player_id=3,
        name="newbie",
        country=None,
        discord_id=1003,
        season=2,
        season_active=False,
        registered=True,
    )

    for name, stats in (("played", played), ("skipped", skipped), ("empty", empty)):
        path = render_player_card(stats, str(tmp_path / f"{name}.png"))
        with Image.open(path) as img:
            assert img.size == (CARD_W, CARD_H)


def test_player_card_champion_indicator_and_awards():
    from bot.rendering.playercard import (
        AWARDS_H,
        AWARDS_TOP,
        CARD_H,
        TABLE_HEADER_H,
        TABLE_ROW_H,
        TABLE_TOP,
        _awards_seasons,
        _champion_line,
    )
    from bot.services.player_stats import PlayerStats

    def stats(**kwargs):
        return PlayerStats(
            player_id=1,
            name="organ",
            country="US",
            discord_id=1001,
            season=3,
            season_active=kwargs.pop("season_active", True),
            registered=True,
            **kwargs,
        )

    champion = stats(
        champion=True,
        season_champion_name="organ",
        championship_seasons=[1, 3],
    )
    other = stats(season_champion_name="rival")
    tbd = stats()
    archived = stats(season_active=False)

    assert _champion_line(champion)[0] == "SEASON CHAMPION"
    assert _champion_line(other)[0] == "Champion: rival"
    assert _champion_line(tbd)[0] == "Champion: TBD"
    assert _champion_line(archived)[0] == "Champion: Not recorded"
    assert _awards_seasons(champion) == "S1 · S3"
    assert _awards_seasons(other) == "None yet"
    assert champion.circuits_won == 2
    assert other.circuits_won == 0

    table_bottom = TABLE_TOP + TABLE_HEADER_H + TABLE_ROW_H * 4
    assert AWARDS_TOP > table_bottom
    assert AWARDS_TOP + AWARDS_H < CARD_H - 58


def test_season_summary_renders_podium_and_leaders(tmp_path):
    from bot.rendering.seasonsummary import (
        SUMMARY_H,
        SUMMARY_W,
        render_season_summary,
    )
    from bot.services.season_summary import (
        SeasonAward,
        SeasonSummary,
        SummaryPlayer,
    )

    champion = SummaryPlayer(
        player_id=1,
        name="organ",
        discord_id=1001,
        country="US",
        games=4,
        wins=4,
        goals_for=18,
        goals_against=8,
    )
    runner_up = SummaryPlayer(
        player_id=2,
        name="rival",
        discord_id=1002,
        country="BR",
        games=5,
        wins=2,
        losses=3,
        goals_for=15,
        goals_against=16,
    )
    awards = [
        SeasonAward("top_scorer", "TOP SCORER", champion, "18", "4 games played"),
        SeasonAward("most_conceded", "MOST CONCEDED", runner_up, "16", "5 games played"),
        SeasonAward("most_games", "MOST GAMES", runner_up, "5", "2-3 record"),
        SeasonAward("most_wins", "MOST WINS", champion, "4", "4 games played"),
        SeasonAward("best_diff", "BEST GOAL DIFF", champion, "+10", "18 scored · 8 conceded"),
        SeasonAward("best_rate", "BEST WIN RATE", champion, "100%", "4-0 record"),
    ]
    summary = SeasonSummary(
        season=1,
        active=False,
        matches=6,
        players=4,
        goals=37,
        champion=champion,
        runner_up=runner_up,
        awards=awards,
    )
    path = render_season_summary(summary, str(tmp_path / "summary.png"))

    with Image.open(path) as img:
        assert img.size == (SUMMARY_W, SUMMARY_H)

    empty = SeasonSummary(
        season=2,
        active=True,
        matches=0,
        players=0,
        goals=0,
    )
    empty_path = render_season_summary(
        empty, str(tmp_path / "summary_empty.png")
    )
    with Image.open(empty_path) as img:
        assert img.size == (SUMMARY_W, SUMMARY_H)


async def test_open_renders_one_file_per_season(db, tmp_path, monkeypatch):
    """Archived seasons get their own filename and do not overwrite the live one."""
    from datetime import datetime
    from bot.db import Event, Match, MatchPlayer, Player
    from bot.rendering import StandingsMaker as standings
    from bot.rendering import asset_path as real_asset_path

    async with db.get_session() as session:
        session.add_all([
            Season(number=1, status=SeasonStatus.ARCHIVED),
            Season(number=2, status=SeasonStatus.ACTIVE),
        ])
        await session.flush()
        p1 = Player(discord_id=201, name="OldTimer", country="us")
        p2 = Player(discord_id=202, name="Newcomer", country="fr")
        session.add_all([p1, p2])
        await session.flush()

        old_event = Event(name="S1 Open", type=EventType.OPEN, season=1)
        new_event = Event(name="S2 Open", type=EventType.OPEN, season=2)
        session.add_all([old_event, new_event])
        await session.flush()

        for event, winner, loser in ((old_event, p1, p2), (new_event, p2, p1)):
            match = Match(event_id=event.id, completed_at=datetime.utcnow())
            session.add(match)
            await session.flush()
            session.add_all([
                MatchPlayer(
                    match_id=match.id, side=0, player_id=winner.id,
                    goals=5, is_winner=True,
                ),
                MatchPlayer(
                    match_id=match.id, side=1, player_id=loser.id,
                    goals=1, is_winner=False,
                ),
            ])
        await session.commit()

    monkeypatch.setattr(
        standings,
        "asset_path",
        lambda *parts: (
            str(tmp_path / parts[-1])
            if parts and parts[0] == "tables"
            else real_asset_path(*parts)
        ),
    )

    _, old_file, *_ = await standings.get_open(season=1)
    _, new_file, *_ = await standings.get_open(season=2)

    assert old_file.endswith("open_s1.png")
    assert new_file.endswith("open_s2.png")
    assert (tmp_path / "open_s1.png").stat().st_size > 0
    assert (tmp_path / "open_s2.png").stat().st_size > 0


def test_concurrent_atomic_saves_leave_a_valid_png(tmp_path):
    """Two threads writing the same image never leave a partial file behind."""
    from concurrent.futures import ThreadPoolExecutor

    from PIL import Image as PILImage

    from bot.rendering import save_png_atomic

    target = tmp_path / "table.png"
    images = [PILImage.new("RGBA", (400, 400), color) for color in ("red", "blue")] * 8

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda img: save_png_atomic(img, str(target)), images))

    with PILImage.open(target) as result:
        result.verify()
    assert target.stat().st_size > 0
    assert list(tmp_path.glob(".table-*")) == []
