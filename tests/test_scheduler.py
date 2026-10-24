"""Tests for the scheduler service (timezones, timeslots, shared match lookup)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.cogs.scheduling.schedule_cog import parse_hhmm, resolve_local_datetime
from bot.config import Config
from bot.db import EventRepository, MatchRepository, MatchPlayerRepository, EventStatus
from bot.db.models import EventType
from bot.services.scheduler import SchedulerService


class TestConfigRemove:
    async def test_remove_existing_key(self, db):
        cfg = Config()
        await cfg.set("agreements_channel", "12345")
        assert await cfg.get("agreements_channel") == "12345"
        assert await cfg.remove("agreements_channel") is True
        assert await cfg.get("agreements_channel") is None

    async def test_remove_missing_key(self, db):
        cfg = Config()
        assert await cfg.remove("never_set_key") is False


class TestParsingHelpers:
    async def test_parse_hhmm(self, db):
        assert parse_hhmm("19:45") == (19, 45)
        assert parse_hhmm("19 45") == (19, 45)
        assert parse_hhmm("1945") == (19, 45)
        assert parse_hhmm("9") == (9, 0)
        assert parse_hhmm("abc") is None
        assert parse_hhmm("12345") is None

    async def test_resolve_local_datetime_same_month(self, db):
        tz = ZoneInfo("UTC")
        now = datetime(2026, 5, 10, 12, 0, tzinfo=tz)
        dt = resolve_local_datetime(tz, now, day=15, hour=19, minute=30)
        assert dt == datetime(2026, 5, 15, 19, 30, tzinfo=tz)

    async def test_resolve_local_datetime_rolls_month(self, db):
        tz = ZoneInfo("UTC")
        now = datetime(2026, 5, 20, 12, 0, tzinfo=tz)
        dt = resolve_local_datetime(tz, now, day=5, hour=19, minute=0)
        assert dt == datetime(2026, 6, 5, 19, 0, tzinfo=tz)

    async def test_resolve_local_datetime_year_rollover(self, db):
        tz = ZoneInfo("UTC")
        now = datetime(2026, 12, 20, 12, 0, tzinfo=tz)
        dt = resolve_local_datetime(tz, now, day=5, hour=19, minute=0)
        assert dt == datetime(2027, 1, 5, 19, 0, tzinfo=tz)

    async def test_resolve_local_datetime_invalid_day(self, db):
        tz = ZoneInfo("UTC")
        now = datetime(2026, 1, 31, 12, 0, tzinfo=tz)
        # day 30 < 31 rolls into February, which has no day 30
        assert resolve_local_datetime(tz, now, day=30, hour=19, minute=0) is None



class TestSchedulerService:
    async def test_timezone_roundtrip(self, db):
        svc = SchedulerService()
        assert await svc.get_timezone(123) is None
        await svc.set_timezone(123, "Europe/Berlin")
        assert await svc.get_timezone(123) == "Europe/Berlin"
        await svc.set_timezone(123, "America/New_York")
        assert await svc.get_timezone(123) == "America/New_York"

    async def test_resolve_timezone(self, db):
        assert SchedulerService.resolve_timezone("UTC") is not None
        assert SchedulerService.resolve_timezone("Europe/Berlin") is not None
        assert SchedulerService.resolve_timezone("Not/AZone") is None
        assert SchedulerService.resolve_timezone("") is None

    async def test_timeslot_lifecycle(self, db):
        svc = SchedulerService()
        await svc.add_timeslot(1, 1000, 2000, prio=1)
        await svc.add_timeslot(1, 3000, 4000, prio=0)
        await svc.add_timeslot(2, 1500, 2500, prio=0)

        slots = await svc.get_user_timeslots(1)
        assert [s.start for s in slots] == [1000, 3000]
        assert slots[0].prio == 1

        await svc.remove_timeslot(1, 1000, 2000, 1)
        slots = await svc.get_user_timeslots(1)
        assert [s.start for s in slots] == [3000]

        await svc.clear_timeslots(1)
        assert await svc.get_user_timeslots(1) == []
        assert len(await svc.get_user_timeslots(2)) == 1

    async def test_overlapping_slots(self, db):
        svc = SchedulerService()
        await svc.add_timeslot(1, 1000, 2000)
        await svc.add_timeslot(2, 1500, 2500)
        await svc.add_timeslot(2, 5000, 6000)

        overlaps = await svc.get_overlapping_slots(1, 2)
        assert overlaps == [(1500, 2000)]

    async def test_find_shared_match(self, db, seed_data):
        svc = SchedulerService()
        p1, p2, p3 = seed_data["players"][:3]

        async with db.get_session() as session:
            event_repo = EventRepository(session)
            event = await event_repo.create("open", EventType.OPEN, 1)
            event.status = EventStatus.ACTIVE

            match_repo = MatchRepository(session)
            match = await match_repo.create(event.id)
            mp_repo = MatchPlayerRepository(session)
            await mp_repo.create(match.id, side=0, player_id=p1.id)
            await mp_repo.create(match.id, side=1, player_id=p2.id)
            await session.commit()
            match_id = match.id

        found = await svc.find_shared_match(p1.id, p2.id, 1)
        assert found is not None
        assert found.id == match_id

        assert await svc.find_shared_match(p1.id, p3.id, 1) is None

        async with db.get_session() as session:
            from datetime import datetime
            m = await MatchRepository(session).get_by_id(match_id)
            m.completed_at = datetime.utcnow()
            await session.commit()

        assert await svc.find_shared_match(p1.id, p2.id, 1) is None


class TestWeeklyTimeslots:
    async def test_weekly_slot_lifecycle(self, db):
        from bot.services.scheduler import WEEKDAYS

        svc = SchedulerService()
        slot = await svc.add_weekly_timeslot(1, 2, 19 * 60, 21 * 60)
        assert slot.weekday == 2
        assert slot.start_minute == 1140
        assert slot.finish_minute == 1260

        await svc.add_weekly_timeslot(1, 5, 10 * 60, 12 * 60)
        slots = await svc.get_weekly_timeslots(1)
        assert [s.weekday for s in slots] == [2, 5]

        await svc.remove_weekly_timeslot(1, slot.id)
        slots = await svc.get_weekly_timeslots(1)
        assert [s.weekday for s in slots] == [5]
        assert WEEKDAYS[5] == "Saturday"

    async def test_weekly_slot_rejects_duplicates_and_reversed_range(self, db):
        import pytest

        svc = SchedulerService()
        await svc.add_weekly_timeslot(1, 3, 600, 700)
        with pytest.raises(ValueError):
            await svc.add_weekly_timeslot(1, 3, 600, 700)
        with pytest.raises(ValueError):
            await svc.add_weekly_timeslot(1, 3, 700, 600)
        with pytest.raises(ValueError):
            await svc.add_weekly_timeslot(1, 3, 700, 700)

    async def test_remove_dated_slot_by_id(self, db):
        svc = SchedulerService()
        await svc.add_timeslot(1, 1000, 2000)
        slot = (await svc.get_user_timeslots(1))[0]
        await svc.remove_timeslot_by_id(1, slot.id)
        assert await svc.get_user_timeslots(1) == []

    async def test_remove_dated_slot_by_id_ignores_other_users(self, db):
        svc = SchedulerService()
        await svc.add_timeslot(1, 1000, 2000)
        slot = (await svc.get_user_timeslots(1))[0]
        await svc.remove_timeslot_by_id(2, slot.id)
        assert len(await svc.get_user_timeslots(1)) == 1


class TestParseWeekday:
    async def test_full_and_short_names(self, db):
        from bot.services.scheduler import parse_weekday

        assert parse_weekday("Wednesday") == 2
        assert parse_weekday("wed") == 2
        assert parse_weekday("WEDNESDAY") == 2
        assert parse_weekday("Monday") == 0
        assert parse_weekday("sunday") == 6

    async def test_numeric_and_invalid(self, db):
        from bot.services.scheduler import parse_weekday

        assert parse_weekday("1") == 0
        assert parse_weekday("7") == 6
        assert parse_weekday("0") is None
        assert parse_weekday("8") is None
        assert parse_weekday("") is None
        assert parse_weekday("funday") is None


class TestAutocomplete:
    @staticmethod
    def _interaction(discord_id: int = 1):
        from unittest.mock import MagicMock

        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.user.id = discord_id
        return interaction

    async def test_timezone_autocomplete_common_when_empty(self, db):
        from bot.cogs.scheduling.schedule_cog import (
            COMMON_TIMEZONES,
            timezone_autocomplete,
        )

        choices = await timezone_autocomplete(self._interaction(), "")
        assert [c.value for c in choices] == list(COMMON_TIMEZONES)

    async def test_timezone_autocomplete_filters(self, db):
        from bot.cogs.scheduling.schedule_cog import timezone_autocomplete

        choices = await timezone_autocomplete(self._interaction(), "Europe/Ber")
        assert choices
        assert all("europe/ber" in c.value.lower() for c in choices)
        assert len(choices) <= 25

    async def test_time_autocomplete(self, db):
        from bot.cogs.scheduling.schedule_cog import time_autocomplete

        choices = await time_autocomplete(self._interaction(), "19:")
        assert [c.value for c in choices] == ["19:00", "19:30"]
        assert all(len(c.name) <= 100 for c in choices)

    async def test_day_autocomplete_uses_timezone(self, db):
        from bot.cogs.scheduling.schedule_cog import day_autocomplete

        svc = SchedulerService()
        await svc.set_timezone(1, "UTC")
        choices = await day_autocomplete(self._interaction(1), "")
        assert len(choices) == 14
        assert all(c.value.isdigit() for c in choices)

    async def test_slot_autocomplete_lists_both_kinds(self, db):
        from bot.cogs.scheduling.schedule_cog import slot_autocomplete

        svc = SchedulerService()
        await svc.add_timeslot(1, 1000, 2000)
        await svc.add_weekly_timeslot(1, 2, 600, 700)

        values = [c.value for c in await slot_autocomplete(self._interaction(1), "")]
        assert len(values) == 2
        assert any(v.startswith("date:") for v in values)
        assert any(v.startswith("weekly:") for v in values)

        only_weekly = [
            c.value for c in await slot_autocomplete(self._interaction(1), "Wed")
        ]
        assert len(only_weekly) == 1
        assert only_weekly[0].startswith("weekly:")

    async def test_slot_autocomplete_scopes_to_a_player_target(self, db):
        from unittest.mock import MagicMock

        from bot.cogs.scheduling.schedule_cog import slot_autocomplete

        svc = SchedulerService()
        await svc.add_weekly_timeslot(1, 2, 600, 700)
        await svc.add_weekly_timeslot(42, 4, 800, 900)

        interaction = self._interaction(1)
        interaction.namespace = MagicMock()
        interaction.namespace.target = MagicMock()
        interaction.namespace.target.id = 42

        choices = await slot_autocomplete(interaction, "")
        assert len(choices) == 1
        assert "Friday" in choices[0].name

    async def test_day_autocomplete_uses_the_targets_timezone(self, db):
        from unittest.mock import MagicMock

        from bot.cogs.scheduling.schedule_cog import day_autocomplete

        svc = SchedulerService()
        await svc.set_timezone(1, "UTC")
        await svc.set_timezone(42, "Pacific/Kiritimati")

        interaction = self._interaction(1)
        interaction.namespace = MagicMock()
        interaction.namespace.target = MagicMock()
        interaction.namespace.target.id = 42

        target_days = await day_autocomplete(interaction, "")
        caller_days = await day_autocomplete(self._interaction(1), "")
        assert len(target_days) == 14

        # Each list starts on "today" in that user's timezone. Comparing the
        # literal lists is time-of-day dependent (the two dates match for part
        # of the day), so assert against each zone's actual date instead.
        from datetime import datetime
        from zoneinfo import ZoneInfo

        assert target_days[0].value == str(
            datetime.now(ZoneInfo("Pacific/Kiritimati")).day
        )
        assert caller_days[0].value == str(datetime.now(ZoneInfo("UTC")).day)
