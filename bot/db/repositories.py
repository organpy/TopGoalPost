from datetime import datetime
from typing import Optional
from sqlalchemy import select, update, delete, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import (
    Player,
    PlayerSeason,
    Season,
    Setting,
    Event,
    Match,
    MatchPlayer,
    AuditLog,
    ImageCache,
    MatchReport,
    OPEN_REPORT_STATUSES,
    ReportStatus,
    Timezone,
    Timeslot,
    EventType,
    EventStatus,
    SeasonStatus,
)

# Distinguishes "leave this field alone" from "set it to NULL".
_UNSET = object()


class PlayerRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_discord_id(self, discord_id: int) -> Optional[Player]:
        stmt = select(Player).where(Player.discord_id == discord_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_by_id(self, player_id: int) -> Optional[Player]:
        stmt = select(Player).where(Player.id == player_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_by_name(self, name: str) -> Optional[Player]:
        stmt = select(Player).where(Player.name == name)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def create(
        self,
        discord_id: int,
        name: str,
        country: Optional[str] = None,
    ) -> Player:
        player = Player(discord_id=discord_id, name=name, country=country)
        self.session.add(player)
        await self.session.flush()
        return player

    async def create_placeholder(
        self,
        name: str,
        country: Optional[str] = None,
    ) -> Player:
        """Create a player with no Discord account yet.

        ``discord_id`` is non-nullable, so name-only substitutes get a unique
        negative ID. Positive IDs are reserved for real Discord snowflakes.
        """
        lowest = (
            await self.session.execute(select(func.min(Player.discord_id)))
        ).scalar()
        placeholder_id = min(lowest or 0, 0) - 1
        return await self.create(
            discord_id=placeholder_id, name=name, country=country
        )

    async def update(
        self,
        player_id: int,
        *,
        discord_id: Optional[int] = None,
        name: Optional[str] = None,
        country=_UNSET,
    ) -> Optional[Player]:
        stmt = select(Player).where(Player.id == player_id)
        result = await self.session.execute(stmt)
        player = result.scalars().first()
        if not player:
            return None
        if discord_id is not None:
            player.discord_id = discord_id
        if name is not None:
            player.name = name
        if country is not _UNSET:
            player.country = country
        self.session.add(player)
        await self.session.flush()
        return player


class SeasonRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_active(self) -> Optional[Season]:
        stmt = select(Season).where(Season.status == SeasonStatus.ACTIVE)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_by_number(self, number: int) -> Optional[Season]:
        stmt = select(Season).where(Season.number == number)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def create(self, number: int) -> Season:
        season = Season(number=number)
        self.session.add(season)
        await self.session.flush()
        return season

    async def archive(self, number: int) -> None:
        stmt = update(Season).where(Season.number == number).values(
            status=SeasonStatus.ARCHIVED,
            ended_at=func.now(),
        )
        await self.session.execute(stmt)

    async def set_champion(self, number: int, player_id: int) -> None:
        stmt = update(Season).where(Season.number == number).values(champion_id=player_id)
        await self.session.execute(stmt)


class PlayerSeasonRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, player_id: int, season_number: int) -> Optional[PlayerSeason]:
        stmt = select(PlayerSeason).where(
            and_(PlayerSeason.player_id == player_id, PlayerSeason.season_number == season_number)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_by_season(self, season_number: int) -> list[PlayerSeason]:
        stmt = select(PlayerSeason).where(PlayerSeason.season_number == season_number)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, player_id: int, season_number: int) -> PlayerSeason:
        ps = PlayerSeason(player_id=player_id, season_number=season_number)
        self.session.add(ps)
        await self.session.flush()
        return ps

    async def delete(self, player_id: int, season_number: int) -> None:
        stmt = delete(PlayerSeason).where(
            and_(PlayerSeason.player_id == player_id, PlayerSeason.season_number == season_number)
        )
        await self.session.execute(stmt)


class SettingRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, key: str) -> Optional[str]:
        stmt = select(Setting).where(Setting.key == key)
        result = await self.session.execute(stmt)
        row = result.scalars().first()
        return row.value if row else None

    async def set(self, key: str, value: str) -> None:
        stmt = update(Setting).where(Setting.key == key).values(value=value)
        result = await self.session.execute(stmt)
        if result.rowcount == 0:
            setting = Setting(key=key, value=value)
            self.session.add(setting)
        await self.session.flush()

    async def remove(self, key: str) -> bool:
        stmt = delete(Setting).where(Setting.key == key)
        result = await self.session.execute(stmt)
        return result.rowcount > 0


class EventRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, event_id: int) -> Optional[Event]:
        stmt = select(Event).where(Event.id == event_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_by_type_and_season(
        self, event_type: EventType, season: int
    ) -> Optional[Event]:
        stmt = select(Event).where(
            and_(Event.type == event_type, Event.season == season)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_active_by_season(self, season: int) -> list[Event]:
        stmt = select(Event).where(
            and_(Event.season == season, Event.status == EventStatus.ACTIVE)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create(self, name: str, event_type: EventType, season: int) -> Event:
        event = Event(name=name, type=event_type, season=season)
        self.session.add(event)
        await self.session.flush()
        return event


class MatchRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, match_id: int) -> Optional[Match]:
        stmt = (
            select(Match)
            .options(selectinload(Match.match_players).selectinload(MatchPlayer.player))
            .where(Match.id == match_id)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_unplayed_by_event(self, event_id: int) -> list[Match]:
        stmt = (
            select(Match)
            .options(selectinload(Match.match_players).selectinload(MatchPlayer.player))
            .where(and_(Match.event_id == event_id, Match.completed_at.is_(None)))
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_by_event(self, event_id: int) -> list[Match]:
        stmt = (
            select(Match)
            .options(selectinload(Match.match_players).selectinload(MatchPlayer.player))
            .where(Match.event_id == event_id)
            .order_by(Match.id)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def set_thread_id(self, match_id: int, thread_id: int) -> None:
        stmt = update(Match).where(Match.id == match_id).values(thread_id=thread_id)
        await self.session.execute(stmt)

    async def create(
        self,
        event_id: int,
        p1_prereq_id: Optional[int] = None,
        p2_prereq_id: Optional[int] = None,
    ) -> Match:
        match = Match(event_id=event_id, p1_prereq_id=p1_prereq_id, p2_prereq_id=p2_prereq_id)
        self.session.add(match)
        await self.session.flush()
        return match


class MatchPlayerRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_id(self, match_player_id: int) -> Optional[MatchPlayer]:
        stmt = select(MatchPlayer).where(MatchPlayer.id == match_player_id)
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_by_match_and_side(
        self, match_id: int, side: int
    ) -> Optional[MatchPlayer]:
        stmt = select(MatchPlayer).where(
            and_(MatchPlayer.match_id == match_id, MatchPlayer.side == side)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def get_by_match(self, match_id: int) -> list[MatchPlayer]:
        stmt = select(MatchPlayer).where(MatchPlayer.match_id == match_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_player_matches(self, player_id: int) -> list[MatchPlayer]:
        stmt = select(MatchPlayer).where(MatchPlayer.player_id == player_id)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_unplayed_player_matches(
        self, player_id: int, season: Optional[int] = None
    ) -> list[MatchPlayer]:
        stmt = select(MatchPlayer).join(Match).where(
            and_(
                MatchPlayer.player_id == player_id,
                Match.completed_at.is_(None),
            )
        )
        if season is not None:
            stmt = stmt.join(Event, Match.event_id == Event.id).where(
                Event.season == season
            )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(
        self,
        match_id: int,
        side: int,
        player_id: Optional[int] = None,
        goals: int = 0,
    ) -> MatchPlayer:
        match_player = MatchPlayer(
            match_id=match_id, side=side, player_id=player_id, goals=goals
        )
        self.session.add(match_player)
        await self.session.flush()
        return match_player

    async def update_score(self, match_player_id: int, goals: int) -> None:
        stmt = update(MatchPlayer).where(MatchPlayer.id == match_player_id).values(goals=goals)
        await self.session.execute(stmt)

    async def set_winner(self, match_id: int, side: int) -> None:
        stmt = update(MatchPlayer).where(MatchPlayer.match_id == match_id).values(is_winner=False)
        await self.session.execute(stmt)

        stmt = update(MatchPlayer).where(
            and_(MatchPlayer.match_id == match_id, MatchPlayer.side == side)
        ).values(is_winner=True)
        await self.session.execute(stmt)


class MatchReportRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        match_id: int,
        reporter_discord_id: int,
        p1_goals: int,
        p2_goals: int,
        winner_side: int,
        reporter_player_id: Optional[int] = None,
    ) -> MatchReport:
        report = MatchReport(
            match_id=match_id,
            reporter_discord_id=reporter_discord_id,
            reporter_player_id=reporter_player_id,
            p1_goals=p1_goals,
            p2_goals=p2_goals,
            winner_side=winner_side,
            status=ReportStatus.PENDING,
        )
        self.session.add(report)
        await self.session.flush()
        return report

    async def get(self, report_id: int) -> Optional[MatchReport]:
        return await self.session.get(MatchReport, report_id)

    async def get_open_for_match(self, match_id: int) -> Optional[MatchReport]:
        stmt = select(MatchReport).where(
            and_(
                MatchReport.match_id == match_id,
                MatchReport.status.in_(OPEN_REPORT_STATUSES),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

    async def list_open(self, statuses=OPEN_REPORT_STATUSES) -> list[MatchReport]:
        stmt = (
            select(MatchReport)
            .where(MatchReport.status.in_(statuses))
            .order_by(MatchReport.created_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def resolve(
        self,
        report: MatchReport,
        status: ReportStatus,
        resolved_by: int,
        reason: Optional[str] = None,
    ) -> MatchReport:
        report.status = status
        report.resolved_by = resolved_by
        if reason:
            report.reason = reason
        if status not in OPEN_REPORT_STATUSES:
            report.resolved_at = datetime.utcnow()
        await self.session.flush()
        return report


class AuditLogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, **fields) -> AuditLog:
        entry = AuditLog(**fields)
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def recent(self, limit: int = 20, action: Optional[str] = None) -> list[AuditLog]:
        stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        if action:
            stmt = stmt.where(AuditLog.action.like(f"{action}%"))
        return list((await self.session.execute(stmt)).scalars().all())


class ImageCacheRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, hash: str) -> Optional[str]:
        stmt = select(ImageCache).where(ImageCache.hash == hash)
        result = await self.session.execute(stmt)
        row = result.scalars().first()
        return row.link if row else None

    async def set(self, hash: str, link: str) -> None:
        await self.delete(hash)
        cache = ImageCache(hash=hash, link=link)
        self.session.add(cache)
        await self.session.flush()

    async def delete(self, hash: str) -> None:
        stmt = delete(ImageCache).where(ImageCache.hash == hash)
        await self.session.execute(stmt)

    async def delete_all(self) -> None:
        stmt = delete(ImageCache)
        await self.session.execute(stmt)


class TimezoneRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, discord_id: int) -> Optional[str]:
        stmt = select(Timezone).where(Timezone.discord_id == discord_id)
        result = await self.session.execute(stmt)
        row = result.scalars().first()
        return row.timezone if row else None

    async def set(self, discord_id: int, timezone: str) -> None:
        stmt = update(Timezone).where(Timezone.discord_id == discord_id).values(timezone=timezone)
        result = await self.session.execute(stmt)
        if result.rowcount == 0:
            tz = Timezone(discord_id=discord_id, timezone=timezone)
            self.session.add(tz)
        await self.session.flush()


class TimeslotRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_active_by_user(self, discord_id: int) -> list:
        from datetime import datetime
        now = int(datetime.utcnow().timestamp())
        stmt = select(Timeslot).where(
            and_(Timeslot.discord_id == discord_id, Timeslot.finish > now)
        ).order_by(Timeslot.finish)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(
        self,
        discord_id: int,
        start: int,
        finish: int,
        prio: int = 0,
    ) -> None:
        ts = Timeslot(discord_id=discord_id, start=start, finish=finish, prio=prio)
        self.session.add(ts)
        await self.session.flush()

    async def delete(
        self,
        discord_id: int,
        start: int,
        finish: int,
        prio: int,
    ) -> None:
        stmt = delete(Timeslot).where(
            and_(
                Timeslot.discord_id == discord_id,
                Timeslot.start == start,
                Timeslot.finish == finish,
                Timeslot.prio == prio,
            )
        )
        await self.session.execute(stmt)
