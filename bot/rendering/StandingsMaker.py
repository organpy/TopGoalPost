import asyncio
import hashlib
import os
import random

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.db import (
    get_db_manager,
    EventType,
    Player as DbPlayer,
    Match as DbMatch,
    MatchPlayer as DbMatchPlayer,
    PlayerSeason,
    SettingRepository,
    EventRepository,
    ImageCacheRepository,
    SeasonRepository,
)
from ._adapters import MatchShim, OpenPlayerShim
from . import asset_path, save_png_atomic

name_w = 170
row_h = 40
flag_w = 25
column_w = row_h
pfp_w = row_h
pfp_size = (int(pfp_w * 0.75), int(pfp_w * 0.75))
player_w = pfp_w + flag_w + name_w
margin = 20
swiss_specs = [[16], [8, 8], [4, 8, 4], [6, 6], [6]]
# Bucket letter per round, matching the fixed SWISS_BRACKET slot names.
swiss_letters = [["M"], ["U", "L"], ["U", "M", "L"], ["U", "L"], ["M"]]
swiss_texts = [
    ["OPENING (0-0)"],
    ["UPPER (1-0)", "LOWER (0-1)"],
    ["UPPER (2-0)", "MIDDLE (1-1)", "LOWER (0-2)"],
    ["UPPER (2-1)", "LOWER (1-2)"],
    ["DECIDER (2-2)"],
]
box_w = player_w * 2 + column_w

bg_color = (50, 50, 50)
open_color = (255, 100, 100)
swiss_color = (26, 188, 156)
playoff_color = (29, 89, 212)
survival_color = (230, 126, 34)
softgreen = (163, 255, 163)
softred = (255, 163, 163)

name_font = ImageFont.truetype(font=asset_path("fonts", "Agdasima-Regular.ttf"), size=25)
playin_center_font = ImageFont.truetype(
    font=asset_path("fonts", "Agdasima-Regular.ttf"), size=21
)
side_font = ImageFont.truetype(font=asset_path("fonts", "Agdasima-Regular.ttf"), size=15)
mask = Image.open(fp=asset_path("pfps", "circle.png")).resize(pfp_size).convert("RGBA")


def _fit_name(text: str, max_width: int = name_w - 4, font=name_font) -> str:
    """Clip a player name to the available column width."""
    text = text or ""
    if font.getlength(text) <= max_width:
        return text
    ellipsis = "..."
    while text and font.getlength(text + ellipsis) > max_width:
        text = text[:-1]
    return text + ellipsis


async def _get_season(session, season: int | None = None) -> int:
    if season is not None:
        return season
    season = await SeasonRepository(session).get_active()
    if season:
        return season.number
    season_str = await SettingRepository(session).get("season")
    if season_str:
        try:
            return int(season_str)
        except (TypeError, ValueError):
            pass
    raise ValueError("No active season. Create one with `/season create N`.")


async def _get_event(session, event_type: EventType, season: int):
    repo = EventRepository(session)
    return await repo.get_by_type_and_season(event_type, season)


def _match_code(prefix: str, match_id: int) -> str:
    return f"{prefix}{match_id:03d}"


def _feeder_label(code: str) -> str:
    """'SW266W' -> 'Winner of 266'. The prefix is two characters."""
    if len(code) < 2:
        return "TBA"
    outcome = "Winner" if code[-1] == "W" else "Loser"
    return f"{outcome} of {code[2:-1]}"


def _swiss_bucket(bracket_slot: str | None) -> str:
    """Bucket letter of a Swiss slot: 'SW-R3-U1' -> 'U'. Defaults to 'M'."""
    if not bracket_slot:
        return "M"
    parts = bracket_slot.split("-")
    if len(parts) < 3:
        return "M"
    return parts[2][:1] or "M"


def _prereq_code(prefix: str, match_id: int, outcome: str = "winner") -> str:
    """Feeder label code: 'SW266W' (winner) or 'SW266L' (loser)."""
    letter = "W" if outcome == "winner" else "L"
    return f"{prefix}{match_id:03d}{letter}"


def _build_match_rows(matches: list[DbMatch], prefix: str) -> list[list]:
    rows: list[list] = []
    for match in matches:
        mp_by_side = {mp.side: mp for mp in match.match_players}
        p1_mp = mp_by_side.get(0)
        p2_mp = mp_by_side.get(1)
        p1 = p1_mp.player if p1_mp else None
        p2 = p2_mp.player if p2_mp else None

        p1_id = p1.discord_id if p1 else None
        p2_id = p2.discord_id if p2 else None
        p1_score = p1_mp.goals if p1_mp else None
        p2_score = p2_mp.goals if p2_mp else None

        winner_id = None
        if p1_mp and p1_mp.is_winner and p1:
            winner_id = p1.discord_id
        elif p2_mp and p2_mp.is_winner and p2:
            winner_id = p2.discord_id

        rows.append(
            [
                p1_id,
                p2_id,
                p1_score,
                p2_score,
                winner_id,
                p1.name if p1 else "",
                p2.name if p2 else "",
                p1.country if p1 else "",
                p2.country if p2 else "",
                _match_code(prefix, match.id),
                _prereq_code(prefix, match.p1_prereq_id, match.p1_prereq_type)
                if match.p1_prereq_id
                else None,
                _prereq_code(prefix, match.p2_prereq_id, match.p2_prereq_type)
                if match.p2_prereq_id
                else None,
                match.bracket_slot,
                match.series_game,
            ]
        )
    return rows


async def _get_cached_link(session, table_hash: str) -> str | None:
    """Return a cached image path only while the file still exists.

    Old rows may contain Discord CDN URLs; those are still usable as-is.
    """
    repo = ImageCacheRepository(session)
    cached = await repo.get(table_hash)
    if not cached:
        return None
    if cached.startswith(("http://", "https://")):
        return cached
    if os.path.exists(cached):
        return cached
    await repo.delete(table_hash)
    await session.commit()
    return None


async def _cache_rendered(table_hash: str, filename: str) -> None:
    async with get_db_manager().get_session() as session:
        await ImageCacheRepository(session).set(table_hash, filename)
        await session.commit()


def _match_signature(matches: list[list]) -> list[tuple]:
    return [
        (
            m[0],
            m[1],
            m[2],
            m[3],
            m[4],
            m[9],
            m[10],
            m[11],
        )
        for m in matches
    ]


def _pad_matches(matches: list[list], total: int, prefix: str) -> list[list]:
    padded = list(matches)
    for idx in range(len(padded), total):
        padded.append(
            [
                None,
                None,
                None,
                None,
                None,
                "",
                "",
                "",
                "",
                _match_code(prefix, idx + 1),
                None,
                None,
                None,
                None,
            ]
        )
    return padded


def _render_placeholder(filename: str, title: str) -> tuple[int, str, str]:
    img = Image.new(mode="RGBA", size=(600, 120), color=bg_color)
    d = ImageDraw.Draw(img)
    d.rectangle(xy=((0, 0), (600, 40)), fill=playoff_color)
    d.text(xy=(300, 20), text=title, fill="white", font=name_font, anchor="mm")
    save_png_atomic(img, filename)
    return 0, filename, ""


def make_and_fill_box(
    img: Image,
    d: ImageDraw,
    title: str,
    color: tuple[int, int, int],
    matches: list[list],
    left: float | int,
    top: float | int,
    advancing: bool = False,
    eliminating: bool = False,
    advance_color: tuple[int, int, int] = (0, 0, 0),
):
    rows = len(matches)
    d.rectangle(xy=((left, top), (left + box_w, top + row_h * (rows + 1))), fill=color, outline="white")

    d.text(xy=(left + box_w / 2, top + row_h / 2), text=title, fill="white", font=name_font, anchor="mm")
    d.rectangle(
        xy=((left + player_w, top + row_h), (left + player_w + column_w, top + row_h * (rows + 1))),
        fill=bg_color,
    )
    for k in range(rows + 1):
        d.line(xy=((left, top + (k + 1) * row_h), (left + box_w, top + (k + 1) * row_h)), fill="white")
    for k, match in enumerate(matches):
        is_bye = bool(match[12] and "-B" in match[12])
        if is_bye:
            d.text(
                xy=(left + box_w / 2, top + row_h * (k + 1.5)),
                text="BYE",
                font=name_font,
                fill=(200, 200, 200),
                anchor="mm",
            )
        elif match[4]:
            d.text(
                xy=(left + box_w / 2, top + row_h * (k + 1.5)),
                text=f"{match[2]}-{match[3]}",
                font=name_font,
                anchor="mm",
            )
            if match[4] == match[0]:
                if advancing:
                    d.rectangle(
                        xy=((left, top + row_h * (k + 1)), (left + player_w, top + row_h * (k + 2))),
                        fill=playoff_color,
                        outline="white",
                    )
                if eliminating:
                    d.rectangle(
                        xy=((left + player_w + column_w, top + row_h * (k + 1)), (left + 2 * player_w + column_w, top + row_h * (k + 2))),
                        fill=open_color,
                        outline="white",
                    )
                d.line(
                    xy=((left + player_w, top + row_h * (k + 1) + 1), (left + player_w, top + row_h * (k + 2) - 1)),
                    fill=softgreen,
                    width=3,
                )
                d.line(
                    xy=((left + player_w + column_w, top + row_h * (k + 1) + 1), (left + player_w + column_w, top + row_h * (k + 2) - 1)),
                    fill=softred,
                    width=3,
                )
            else:
                if advancing:
                    d.rectangle(
                        xy=((left + player_w + column_w, top + row_h * (k + 1)), (left + 2 * player_w + column_w, top + row_h * (k + 2))),
                        fill=playoff_color,
                        outline="white",
                    )
                if eliminating:
                    d.rectangle(
                        xy=((left, top + row_h * (k + 1)), (left + player_w, top + row_h * (k + 2))),
                        fill=open_color,
                        outline="white",
                    )
                d.line(
                    xy=((left + player_w, top + row_h * (k + 1) + 1), (left + player_w, top + row_h * (k + 2) - 1)),
                    fill=softred,
                    width=3,
                )
                d.line(
                    xy=((left + player_w + column_w, top + row_h * (k + 1) + 1), (left + player_w + column_w, top + row_h * (k + 2) - 1)),
                    fill=softgreen,
                    width=3,
                )
        else:
            d.text(
                xy=(left + box_w / 2, top + row_h * (k + 1.5)),
                text=match[9][2:] if match[9] else "TBA",
                font=name_font,
                fill=(200, 200, 200),
                anchor="mm",
            )
        if match[0]:
            try:
                pfp = Image.open(fp=asset_path("pfps", f"{match[0]}.png")).resize(pfp_size).convert("RGBA")
            except FileNotFoundError:
                pfp = Image.open(fp=asset_path("pfps", "default.png")).resize(pfp_size).convert("RGBA")
            img.paste(
                box=(int(left + (pfp_w - pfp_size[0]) / 2), int(row_h * (k + 1) + (pfp_w - pfp_size[0]) / 2 + top)),
                im=pfp,
                mask=mask,
            )
            d.text(
                xy=(left + pfp_w + flag_w + name_w / 2, top + row_h * (k + 1.5)),
                text=_fit_name(match[5]),
                fill="white",
                font=name_font,
                anchor="mm",
            )
            flag = get_flag(match[7])
            img.paste(
                im=flag,
                box=(int(left + pfp_w + (flag_w - flag.width) / 2), int(top + row_h * (k + 1) + (row_h - flag.height) / 2 + 1)),
                mask=flag,
            )
        elif match[10]:
            d.text(
                xy=(left + (pfp_w + flag_w + name_w) / 2, top + row_h * (k + 1.5)),
                text=_fit_name(_feeder_label(match[10])),
                fill="white",
                font=name_font,
                anchor="mm",
            )
        else:
            d.text(
                xy=(left + (pfp_w + flag_w + name_w) / 2, top + row_h * (k + 1.5)),
                text="TBA",
                fill="white",
                font=name_font,
                anchor="mm",
            )
        if is_bye:
            pass
        elif match[1]:
            try:
                pfp = Image.open(fp=asset_path("pfps", f"{match[1]}.png")).resize(pfp_size).convert("RGBA")
            except FileNotFoundError:
                pfp = Image.open(fp=asset_path("pfps", "default.png")).resize(pfp_size).convert("RGBA")
            img.paste(
                box=(
                    int(left + box_w - pfp_w + ((pfp_w - pfp_size[0]) / 2)),
                    int(top + row_h * (k + 1) + (pfp_w - pfp_size[0]) / 2),
                ),
                im=pfp,
                mask=mask,
            )
            flag = get_flag(match[8])
            img.paste(
                im=flag,
                box=(int(left + box_w - pfp_w - flag_w + (flag_w - flag.width) / 2), int(top + row_h * (k + 1) + (row_h - flag.height) / 2 + 1)),
                mask=flag,
            )
            d.text(
                xy=(left + box_w - (pfp_w + flag_w + name_w / 2), top + row_h * (k + 1.5)),
                text=_fit_name(match[6]),
                fill="white",
                font=name_font,
                anchor="mm",
            )
        elif match[11]:
            d.text(
                xy=(left + box_w - ((pfp_w + flag_w + name_w) / 2), top + row_h * (k + 1.5)),
                text=_fit_name(_feeder_label(match[11])),
                fill="white",
                font=name_font,
                anchor="mm",
            )
        else:
            d.text(
                xy=(left + box_w - ((pfp_w + flag_w + name_w) / 2), top + row_h * (k + 1.5)),
                text="TBA",
                fill="white",
                font=name_font,
                anchor="mm",
            )


def _country_to_emoji(code: str | None) -> str:
    if not code:
        return ""
    if len(code) == 2 and code.isascii() and code.isalpha():
        return "".join(chr(0x1F1E6 + ord(c.upper()) - ord("A")) for c in code)
    return code


def _emoji_to_filename(emoji: str) -> str:
    parts = []
    for ch in emoji:
        cp = ord(ch)
        if 0x1F1E6 <= cp <= 0x1F1FF:
            parts.append(f"{cp:x}")
    return "-".join(parts) + ".png" if parts else ""


def get_flag(country_code: str, width: float | None = None) -> Image:
    emoji = _country_to_emoji(country_code)
    flag_path = asset_path("flags", f"{emoji}.png")
    try:
        flag = Image.open(fp=flag_path).convert("RGBA")
    except FileNotFoundError:
        twemoji_name = _emoji_to_filename(emoji)
        flag = None
        if twemoji_name:
            from urllib import request
            try:
                request.urlretrieve(
                    "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/" + twemoji_name,
                    flag_path,
                )
                flag = Image.open(fp=flag_path).convert("RGBA")
            except Exception:
                pass
        if flag is None:
            flag = Image.new("RGBA", (72, 72), (0, 0, 0, 0))
    target_w = width if width is not None else flag_w * 0.75
    scaling_factor = target_w / flag.width
    flag = flag.resize((int(flag.width * scaling_factor), int(flag.height * scaling_factor)), resample=1)
    return flag


async def get_open(sort_by_rank: int = 0, season: int | None = None):
    return await _get_open_table(
        sort_by_rank=sort_by_rank,
        header_color=open_color,
        season=season,
        return_players=False,
    )


async def get_open_with_players(sort_by_rank: int = 0, season: int | None = None):
    return await _get_open_table(
        sort_by_rank=sort_by_rank,
        header_color=open_color,
        season=season,
        return_players=True,
    )


async def _get_open_table(
    sort_by_rank: int,
    header_color: tuple[int, int, int],
    season: int | None = None,
    return_players: bool = False,
):
    async with get_db_manager().get_session() as session:
        season = await _get_season(session, season)
        filename = asset_path("tables", f"open_s{season}.png")
        event = await _get_event(session, EventType.OPEN, season)

        matches: list[list] = []
        if event:
            stmt = (
                select(DbMatch)
                .options(selectinload(DbMatch.match_players).selectinload(DbMatchPlayer.player))
                .where(DbMatch.event_id == event.id)
                .order_by(DbMatch.id)
            )
            result = await session.execute(stmt)
            db_matches = result.scalars().unique().all()
            matches = _build_match_rows(db_matches, "OP")

        players_stmt = select(DbPlayer).where(
            DbPlayer.id.in_(
                select(DbMatchPlayer.player_id).where(
                    DbMatchPlayer.match_id.in_(
                        select(DbMatch.id).where(DbMatch.event_id == event.id)
                    )
                )
            )
        ) if event else select(DbPlayer).where(1 == 0)
        result = await session.execute(players_stmt)
        db_players = result.scalars().all()

        players = [
            OpenPlayerShim(id=p.discord_id, name=p.name, country=p.country or "")
            for p in db_players
        ]

        layout_version = "open_table_v2"
        table_hash = hashlib.sha3_256(
            bytes(
                repr(_match_signature(matches))
                + repr(sort_by_rank)
                + repr(season)
                + layout_version,
                "utf-8",
            )
        ).hexdigest()

        link = await _get_cached_link(session, table_hash)
        if link:
            if return_players:
                return link, 0, 0, players
            return link, 0, 0

    def _render_open_table(matches_copy, players_copy):
        matches_obj = [MatchShim(match) for match in matches_copy]
        for player in players_copy:
            player_matches = player.get_matches_array(match_array=matches_obj)
            for match in player_matches:
                player.add_game_to_stats(match)
        if sort_by_rank:
            random.shuffle(players_copy)
            players_copy.sort(key=lambda el: (-el.wins, -el.gd, -el.goals_scored))
        else:
            players_copy.sort(key=lambda el: el.name.upper())

        max_matches = max([len(i.get_matches_array(matches_obj)) for i in players_copy], default=0)
        max_matches = max(4, max_matches)
        width = player_w + (name_w + column_w) * max_matches + column_w * 4
        height = row_h * len(players_copy) + row_h
        img = Image.new(mode="RGBA", size=(width, height), color=bg_color)
        d = ImageDraw.Draw(img)
        d.rectangle(xy=((0, 0), (width, row_h)), fill=header_color)
        d.rectangle(xy=((0, 0), (player_w, height)), fill=header_color)
        d.line(xy=((player_w, 0), (player_w, height)))
        for i in range(1, max_matches):
            d.line(
                xy=(
                    (player_w + (name_w + column_w) * i, row_h),
                    (player_w + (name_w + column_w) * i, height),
                )
            )
        for i in range(1, 5):
            d.line(xy=((width - column_w * i, 0), (width - column_w * i, height)))
        d.text(
            xy=(player_w + (name_w + column_w) * max_matches / 2, row_h * 0.5),
            text="OPPONENTS",
            fill="white",
            font=name_font,
            anchor="mm",
        )
        d.text(xy=(width - column_w * 3.5, row_h * 0.5), text="W", fill="white", font=name_font, anchor="mm")
        d.text(xy=(width - column_w * 2.5, row_h * 0.5), text="L", fill="white", font=name_font, anchor="mm")
        d.text(xy=(width - column_w * 1.5, row_h * 0.5), text="HP", fill="white", font=name_font, anchor="mm")
        d.text(xy=(width - column_w * 0.5, row_h * 0.5), text="GD", fill="white", font=name_font, anchor="mm")
        for i, player in enumerate(players_copy):
            d.line(xy=((0, row_h * (i + 1)), (width, row_h * (i + 1))), fill="white")
            try:
                pfp = Image.open(fp=asset_path("pfps", f"{player.id}.png")).resize(pfp_size).convert("RGBA")
            except FileNotFoundError:
                pfp = Image.open(fp=asset_path("pfps", "default.png")).resize(pfp_size).convert("RGBA")
            img.paste(
                im=pfp,
                box=(int((pfp_w - pfp_size[0]) / 2), int((i + 1) * row_h + (pfp_w - pfp_size[0]) / 2)),
                mask=mask,
            )
            flag = get_flag(player.country)
            img.paste(
                im=flag,
                box=(int(pfp_w + (flag_w - flag.width) / 2), int((i + 1) * row_h + (row_h - flag.height) / 2 + 1)),
                mask=flag,
            )
            d.text(
                xy=(pfp_w + flag_w, row_h * (i + 1.5)),
                text=_fit_name(player.name),
                fill="white",
                font=name_font,
                anchor="lm",
            )
            player_matches = player.get_matches_array(match_array=matches_obj)
            player_matches.sort(key=lambda el: (-bool(el.winner_id), el.get_opponent(player.id).name))
            for j, match in enumerate(player_matches):
                opponent = match.get_opponent(player.id)
                if opponent.id == player.id:
                    opponent_label = "BYE"
                else:
                    opponent_label = opponent.name
                if match.winner_id:
                    d.text(
                        xy=(player_w + (name_w + column_w) * (j + 0.5) - column_w / 2 + 5, row_h * (i + 1.5)),
                        text=_fit_name(
                            opponent_label if opponent_label == "BYE" else (
                                f"{opponent_label} (L)" if match.p1_id == player.id else f"{opponent_label} (R)"
                            ),
                            max_width=name_w - 8,
                        ),
                        fill=(100, 100, 100),
                        font=name_font,
                        anchor="mm",
                    )
                    score_text = f"{match.get_score_from_player(player.id)[0]}-{match.get_score_from_player(player.id)[1]}"
                    d.text(
                        xy=(player_w + (name_w + column_w) * (j + 1) - column_w / 2, row_h * (i + 1.5)),
                        text=score_text,
                        fill=softgreen if match.winner_id == player.id else softred,
                        font=name_font,
                        anchor="mm",
                    )
                else:
                    d.text(
                        xy=(player_w + (name_w + column_w) * (j + 0.5), row_h * (i + 1.5)),
                        text=_fit_name(
                            opponent_label if opponent_label == "BYE" else (
                                f"{opponent_label} (L)" if match.p1_id == player.id else f"{opponent_label} (R)"
                            ),
                            max_width=name_w - 8,
                        ),
                        fill=(200, 200, 200),
                        font=name_font,
                        anchor="mm",
                    )
            health = max(0, 4 - player.losses)
            d.text(xy=(width - 3.5 * column_w, row_h * (i + 1.5)), text=f"{player.wins}", fill="white", font=name_font, anchor="mm")
            d.text(xy=(width - 2.5 * column_w, row_h * (i + 1.5)), text=f"{player.losses}", fill="white", font=name_font, anchor="mm")
            d.text(
                xy=(width - 1.5 * column_w, row_h * (i + 1.5)),
                text=f"{health}",
                fill=softgreen if health > 1 else softred,
                font=name_font,
                anchor="mm",
            )
            d.text(xy=(width - 0.5 * column_w, row_h * (i + 1.5)), text=f"{player.gd:+}", fill="white", font=name_font, anchor="mm")
        save_png_atomic(img, filename)
        if return_players:
            return 0, filename, table_hash, players_copy
        return 0, filename, table_hash

    result = await asyncio.to_thread(_render_open_table, matches, players)
    await _cache_rendered(table_hash, filename)
    return result


def _compute_playin_layout(all_ps: list[PlayerSeason]) -> dict:
    """Mirror PlayinService's stepped-ladder math for display."""
    from bot.services.playin import compute_playin_math

    math = compute_playin_math(all_ps)
    return {
        "returning_seeds": math["returning_seeds"],
        "target": math["target"],
        "four_wins": math["four_wins"],
        "direct_three_wins": math["direct_three_wins"],
        "one_wins": math["one_wins"],
        "two_wins": math["two_wins"],
        "three_wins": math["three_wins"],
        "expected_matches": math["expected_matches"],
        "byes": math["byes"],
        "entrants": math["entrants"],
        "base_qualifiers": math["base_qualifiers"],
        "qualifiers": math["qualifiers"],
        "over_by": math["over_by"],
        "cut_matches": math["cut_matches"],
        "cut_players": math["cut_players"],
    }


def _playin_player_label(name: str) -> str:
    return _fit_name(name) if name else "TBA"


def _playin_slot_label(slot: str | None) -> str:
    if not slot:
        return "TBA"
    text = slot.replace("P-", "")
    if "-M" in text:
        rnd, num = text.split("-M", 1)
        return f"{rnd.replace('-', ' ')}-{num}".replace("R", "R")
    if "-B" in text:
        rnd = text.split("-B", 1)[0]
        return f"{rnd} BYE"
    return text


def _playin_label_x(left: float, box_w: float, side: int, has_player: bool) -> float:
    """Text center for a playin slot: name column or the full empty half-box."""
    if has_player:
        offset = pfp_w + flag_w + name_w / 2
    else:
        offset = (pfp_w + flag_w + name_w) / 2
    return left + offset if side == 0 else left + box_w - offset


async def get_playin(season: int | None = None):
    async with get_db_manager().get_session() as session:
        season = await _get_season(session, season)
        filename = asset_path("tables", f"playin_s{season}.png")
        playin_event = await _get_event(session, EventType.PLAYIN, season)

        matches = []
        slot_by_id: dict[int, str] = {}
        if playin_event:
            stmt = (
                select(DbMatch)
                .options(selectinload(DbMatch.match_players).selectinload(DbMatchPlayer.player))
                .where(DbMatch.event_id == playin_event.id)
                .order_by(DbMatch.id)
            )
            result = await session.execute(stmt)
            raw_matches = result.scalars().unique().all()
            slot_by_id = {
                match.id: match.bracket_slot
                for match in raw_matches
                if match.bracket_slot
            }
            matches = _build_match_rows(raw_matches, "PL")

        round_matches: dict[int, list[list]] = {}
        for match in matches:
            current_round = match[13] if len(match) > 13 and match[13] else 1
            round_matches.setdefault(current_round, []).append(match)

        rounds = [1, 2, 3]
        cut_preview = 0
        if not round_matches.get(4):
            from bot.services.playin import compute_playin_math

            all_ps = (
                await session.execute(
                    select(PlayerSeason).where(
                        PlayerSeason.season_number == season
                    )
                )
            ).scalars().all()
            cut_preview = compute_playin_math(all_ps)["cut_matches"]
            if cut_preview:
                round_matches[4] = _pad_matches([], cut_preview, "PL")
        if round_matches.get(4):
            rounds.append(4)
            round_labels_extra = {4: "CUT"}
        else:
            round_labels_extra = {}
        if not round_matches.get(1):
            round_matches[1] = _pad_matches([], 1, "PL")

        round_labels = {
            1: "ROUND 1",
            2: "ROUND 2",
            3: "ROUND 3",
            **round_labels_extra,
        }
        layout_name = "playin_ladder_v9"
        table_hash = hashlib.sha3_256(
            bytes(
                repr(_match_signature(matches))
                + repr(rounds)
                + repr(cut_preview)
                + repr(season)
                + layout_name,
                "utf-8",
            )
        ).hexdigest()

        link = await _get_cached_link(session, table_hash)
        if link:
            return link, 0, 0

    def _prereq_label(code: str | None) -> str:
        if not code:
            return "TBA"
        try:
            match_id = int(code[2:-1])
        except (TypeError, ValueError):
            return "TBA"
        slot = slot_by_id.get(match_id)
        return f"Winner of {_playin_slot_label(slot)}" if slot else "TBA"

    def _render_playin(
        rounds_copy,
        round_matches_copy,
        round_labels_copy,
        filename_copy,
        cut_preview_copy=0,
    ):
        rows_per_round = {
            r: max(1, len(round_matches_copy.get(r, []))) for r in rounds_copy
        }
        width = margin + len(rounds_copy) * (box_w + margin)
        grid_body_h = max(row_h * (rows_per_round[r] + 1) for r in rounds_copy)
        height = margin + grid_body_h + margin * 2
        img = Image.new(mode="RGBA", size=(width, height), color=bg_color)
        d = ImageDraw.Draw(img)

        grid_top = margin
        for col, rnd in enumerate(rounds_copy):
            left = margin + col * (box_w + margin)
            top = grid_top
            rows = rows_per_round[rnd]
            d.rectangle(xy=((left, top), (left + box_w, top + row_h * (rows + 1))), fill=survival_color, outline="white")
            d.rectangle(
                xy=((left + player_w, top + row_h), (left + player_w + column_w, top + row_h * (rows + 1))),
                fill=bg_color,
            )
            label = round_labels_copy.get(rnd, f"ROUND {rnd}")
            lines = label.split("\n")
            for li, line in enumerate(lines):
                d.text(
                    xy=(left + box_w / 2, top + row_h / 2 + li * row_h * 0.35),
                    text=line,
                    fill="white",
                    font=name_font,
                    anchor="mm",
                )
            for k in range(rows + 1):
                d.line(xy=((left, top + (k + 1) * row_h), (left + box_w, top + (k + 1) * row_h)), fill="white")

            rmatches = round_matches_copy.get(rnd, [])
            for k in range(rows):
                if k >= len(rmatches):
                    d.text(
                        xy=(left + box_w / 2, top + row_h * (k + 1.5)),
                        text="TBA",
                        fill=(160, 160, 160),
                        font=name_font,
                        anchor="mm",
                    )
                    continue
                match = rmatches[k]
                p1, p2 = match[0], match[1]
                slot = match[12] or ""
                is_bye = "-B" in slot
                left_name = match[5] or _prereq_label(match[10])
                right_name = match[6] or _prereq_label(match[11])
                if is_bye:
                    right_name = "BYE"

                # A player name sits beside its pfp/flag. A pre-placed feeder
                # (or TBA) has no pfp, so center it across the whole half-box
                # exactly like the Swiss board.
                d.text(
                    xy=(
                        _playin_label_x(left, box_w, 0, bool(p1)),
                        top + row_h * (k + 1.5),
                    ),
                    text=_playin_player_label(left_name),
                    fill="white",
                    font=name_font,
                    anchor="mm",
                )
                if not is_bye:
                    d.text(
                        xy=(
                            _playin_label_x(left, box_w, 1, bool(p2)),
                            top + row_h * (k + 1.5),
                        ),
                        text=_playin_player_label(right_name),
                        fill="white",
                        font=name_font,
                        anchor="mm",
                    )

                center_x = left + box_w / 2
                center_y = top + row_h * (k + 1.5)
                if is_bye:
                    d.text(xy=(center_x, center_y), text="BYE", fill=(200, 200, 200), font=name_font, anchor="mm")
                elif match[4]:
                    if match[4] == p1:
                        left_color, right_color = softgreen, softred
                    else:
                        left_color, right_color = softred, softgreen
                    d.text(xy=(center_x - 12, center_y), text=str(match[2]), fill=left_color, font=name_font, anchor="mm")
                    d.text(xy=(center_x, center_y), text="-", fill="white", font=name_font, anchor="mm")
                    d.text(xy=(center_x + 12, center_y), text=str(match[3]), fill=right_color, font=name_font, anchor="mm")
                else:
                    center_text = _playin_slot_label(slot) or f"R{rnd}-{k + 1}"
                    if cut_preview_copy and rnd == 4 and not p1 and not p2:
                        center_text = "CUT"
                    d.text(
                        xy=(center_x, center_y),
                        text=center_text,
                        fill=(200, 200, 200),
                        font=playin_center_font,
                        anchor="mm",
                    )

                if p1:
                    try:
                        pfp = Image.open(fp=asset_path("pfps", f"{p1}.png")).resize(pfp_size).convert("RGBA")
                    except FileNotFoundError:
                        pfp = Image.open(fp=asset_path("pfps", "default.png")).resize(pfp_size).convert("RGBA")
                    img.paste(
                        box=(int(left + (pfp_w - pfp_size[0]) / 2), int(top + row_h * (k + 1) + (pfp_w - pfp_size[0]) / 2)),
                        im=pfp,
                        mask=mask,
                    )
                if p2 and not is_bye:
                    try:
                        pfp = Image.open(fp=asset_path("pfps", f"{p2}.png")).resize(pfp_size).convert("RGBA")
                    except FileNotFoundError:
                        pfp = Image.open(fp=asset_path("pfps", "default.png")).resize(pfp_size).convert("RGBA")
                    img.paste(
                        box=(
                            int(left + box_w - pfp_w + ((pfp_w - pfp_size[0]) / 2)),
                            int(top + row_h * (k + 1) + (pfp_w - pfp_size[0]) / 2),
                        ),
                        im=pfp,
                        mask=mask,
                    )
        save_png_atomic(img, filename_copy)
        return 0, filename_copy, table_hash

    result = await asyncio.to_thread(
        _render_playin,
        rounds,
        round_matches,
        round_labels,
        filename,
        cut_preview,
    )
    await _cache_rendered(table_hash, filename)
    return result


async def get_swiss(season: int | None = None):
    async with get_db_manager().get_session() as session:
        season = await _get_season(session, season)
        filename = asset_path("tables", f"swiss_s{season}.png")
        event = await _get_event(session, EventType.SWISS, season)
        matches = []
        if event:
            stmt = (
                select(DbMatch)
                .options(selectinload(DbMatch.match_players).selectinload(DbMatchPlayer.player))
                .where(DbMatch.event_id == event.id)
                .order_by(DbMatch.id)
            )
            result = await session.execute(stmt)
            matches = _build_match_rows(result.scalars().unique().all(), "SW")

        round_rows: dict[int, list[list]] = {}
        for match in matches:
            rnd = match[13] if len(match) > 13 and match[13] else 1
            round_rows.setdefault(rnd, []).append(match)

        # Survival board: 3 wins qualifies, 3 losses eliminates. The fixed
        # bracket encodes the record bucket in the slot name (SW-R3-U1), so
        # grouping is a pure lookup — no standings recalculation needed.
        groups: dict[int, list[tuple[str, list[list]]]] = {}
        for rnd in range(1, 6):
            grouped: dict[str, list[list]] = {}
            for row in round_rows.get(rnd, []):
                letter = _swiss_bucket(row[12])
                grouped.setdefault(letter, []).append(row)

            groups[rnd] = []
            for letter, title, size in zip(
                swiss_letters[rnd - 1], swiss_texts[rnd - 1], swiss_specs[rnd - 1]
            ):
                rows = sorted(
                    grouped.get(letter, []),
                    key=lambda r: (r[12] or "", r[9] or ""),
                )[:size]
                padded = _pad_matches(rows, size, "SW")
                for row in padded[len(rows):]:
                    row[9] = None
                groups[rnd].append((title, padded))

        rounds = list(range(1, 6))

        table_hash = hashlib.sha3_256(
            bytes(
                repr(_match_signature(matches))
                + repr(
                    [
                        (rnd, [(title, len(rows)) for title, rows in groups[rnd]])
                        for rnd in rounds
                    ]
                )
                + repr(season)
                + "swiss_fixed_graph_v1",
                "utf-8",
            )
        ).hexdigest()
        link = await _get_cached_link(session, table_hash)
        if link:
            return link, 0, 0

    def _render_swiss():
        column_heights = []
        for rnd in rounds:
            group_list = groups[rnd]
            column_heights.append(
                sum(row_h * (len(rows) + 1) for _, rows in group_list)
                + margin * max(0, len(group_list) - 1)
            )
        width = margin + len(rounds) * (box_w + margin)
        height = max(column_heights + [row_h * 3]) + margin * 3
        img = Image.new(mode="RGBA", size=(width, height), color=bg_color)
        d = ImageDraw.Draw(img)

        left = margin
        for rnd in rounds:
            group_list = groups[rnd]
            col_height = sum(
                row_h * (len(rows) + 1) for _, rows in group_list
            ) + margin * max(0, len(group_list) - 1)
            top = (height - col_height) / 2
            for index, (title, rows) in enumerate(group_list):
                make_and_fill_box(
                    img=img,
                    d=d,
                    title=f"ROUND {rnd} {title}",
                    color=swiss_color,
                    matches=rows,
                    left=left,
                    top=top,
                    advancing=rnd >= 3 and index == 0,
                    eliminating=(rnd + index == 5),
                    advance_color=playoff_color,
                )
                top += row_h * (len(rows) + 1) + margin
            left += box_w + margin

        save_png_atomic(img, filename)
        return 0, filename, table_hash

    result = await asyncio.to_thread(_render_swiss)
    await _cache_rendered(table_hash, filename)
    return result
