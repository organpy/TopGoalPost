"""Scheduler service.

Manages player timezones and timeslots for scheduling matches.
Timeslots are stored as unix epoch timestamps.
"""

from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, delete

from bot.db import (
    get_db_manager,
    Timezone,
    Timeslot,
    WeeklyTimeslot,
    EventRepository,
    MatchRepository,
)

WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def parse_weekday(text: str) -> Optional[int]:
    """Parse a weekday name or number into 0 (Monday) .. 6 (Sunday)."""
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return None
    for index, name in enumerate(WEEKDAYS):
        if cleaned == name.lower() or (
            len(cleaned) >= 3 and name.lower().startswith(cleaned)
        ):
            return index
    if cleaned.isdigit():
        number = int(cleaned)
        if 1 <= number <= 7:
            return number - 1
    return None


class SchedulerService:
    def __init__(self):
        self.db = get_db_manager()

    @staticmethod
    def resolve_timezone(timezone_str: str) -> Optional[ZoneInfo]:
        """Return a ZoneInfo for the given string, or None if invalid."""
        try:
            return ZoneInfo(timezone_str)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return None

    async def get_timezone(self, discord_id: int) -> Optional[str]:
        """Get a user's timezone string."""
        async with self.db.get_session() as session:
            stmt = select(Timezone).where(Timezone.discord_id == discord_id)
            result = await session.execute(stmt)
            tz = result.scalars().first()
            return tz.timezone if tz else None

    async def set_timezone(self, discord_id: int, timezone_str: str) -> None:
        """Set a user's timezone."""
        async with self.db.get_session() as session:
            stmt = select(Timezone).where(Timezone.discord_id == discord_id)
            result = await session.execute(stmt)
            tz = result.scalars().first()
            if tz:
                tz.timezone = timezone_str
            else:
                session.add(Timezone(discord_id=discord_id, timezone=timezone_str))
            await session.commit()

    async def get_user_timeslots(self, discord_id: int) -> List[Timeslot]:
        """Get all timeslots for a user, ordered by start time."""
        async with self.db.get_session() as session:
            stmt = (
                select(Timeslot)
                .where(Timeslot.discord_id == discord_id)
                .order_by(Timeslot.start)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def add_timeslot(
        self, discord_id: int, start: int, finish: int, prio: int = 0
    ) -> Timeslot:
        """Add a timeslot. Start and finish are unix epoch seconds."""
        async with self.db.get_session() as session:
            slot = Timeslot(discord_id=discord_id, start=start, finish=finish, prio=prio)
            session.add(slot)
            await session.commit()
            return slot

    async def remove_timeslot(
        self, discord_id: int, start: int, finish: int, prio: int
    ) -> None:
        """Remove a specific timeslot."""
        async with self.db.get_session() as session:
            stmt = delete(Timeslot).where(
                Timeslot.discord_id == discord_id,
                Timeslot.start == start,
                Timeslot.finish == finish,
                Timeslot.prio == prio,
            )
            await session.execute(stmt)
            await session.commit()

    async def clear_timeslots(self, discord_id: int) -> None:
        """Remove all timeslots for a user."""
        async with self.db.get_session() as session:
            stmt = delete(Timeslot).where(Timeslot.discord_id == discord_id)
            await session.execute(stmt)
            await session.commit()

    async def remove_timeslot_by_id(self, discord_id: int, slot_id: int) -> None:
        """Remove a dated timeslot by its row id."""
        async with self.db.get_session() as session:
            stmt = delete(Timeslot).where(
                Timeslot.discord_id == discord_id,
                Timeslot.id == slot_id,
            )
            await session.execute(stmt)
            await session.commit()

    async def add_weekly_timeslot(
        self,
        discord_id: int,
        weekday: int,
        start_minute: int,
        finish_minute: int,
    ) -> WeeklyTimeslot:
        """Add a recurring weekly availability window."""
        if finish_minute <= start_minute:
            raise ValueError("Finish time must be after the start time.")
        async with self.db.get_session() as session:
            existing = (
                await session.execute(
                    select(WeeklyTimeslot).where(
                        WeeklyTimeslot.discord_id == discord_id,
                        WeeklyTimeslot.weekday == weekday,
                        WeeklyTimeslot.start_minute == start_minute,
                        WeeklyTimeslot.finish_minute == finish_minute,
                    )
                )
            ).scalars().first()
            if existing:
                raise ValueError("That weekly slot already exists.")
            slot = WeeklyTimeslot(
                discord_id=discord_id,
                weekday=weekday,
                start_minute=start_minute,
                finish_minute=finish_minute,
            )
            session.add(slot)
            await session.commit()
            return slot

    async def get_weekly_timeslots(self, discord_id: int) -> List[WeeklyTimeslot]:
        """Get a user's recurring weekly availability."""
        async with self.db.get_session() as session:
            stmt = (
                select(WeeklyTimeslot)
                .where(WeeklyTimeslot.discord_id == discord_id)
                .order_by(WeeklyTimeslot.weekday, WeeklyTimeslot.start_minute)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def remove_weekly_timeslot(self, discord_id: int, slot_id: int) -> None:
        """Remove a recurring weekly slot by its row id."""
        async with self.db.get_session() as session:
            stmt = delete(WeeklyTimeslot).where(
                WeeklyTimeslot.discord_id == discord_id,
                WeeklyTimeslot.id == slot_id,
            )
            await session.execute(stmt)
            await session.commit()

    async def get_overlapping_slots(
        self, discord_id_1: int, discord_id_2: int
    ) -> List[tuple[int, int]]:
        """Find overlapping timeslots between two users.

        Returns a list of (start, finish) epoch intervals.
        """
        slots1 = await self.get_user_timeslots(discord_id_1)
        slots2 = await self.get_user_timeslots(discord_id_2)

        overlaps = []
        for s1 in slots1:
            for s2 in slots2:
                overlap_start = max(s1.start, s2.start)
                overlap_finish = min(s1.finish, s2.finish)
                if overlap_start < overlap_finish:
                    overlaps.append((overlap_start, overlap_finish))
        return overlaps

    async def find_shared_match(
        self, player_id: int, opponent_id: int, season: int
    ) -> Optional[object]:
        """Find an unplayed match between two players in any active event of the season."""
        async with self.db.get_session() as session:
            event_repo = EventRepository(session)
            match_repo = MatchRepository(session)
            events = await event_repo.get_active_by_season(season)
            for event in events:
                matches = await match_repo.get_unplayed_by_event(event.id)
                for match in matches:
                    ids = {mp.player_id for mp in match.match_players}
                    if player_id in ids and opponent_id in ids:
                        return match
        return None
