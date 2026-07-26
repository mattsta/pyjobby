"""The cron semantics this platform's scheduling contract rests on.

pyjobby delegates cron parsing and next-fire computation to croniter. That
makes croniter's exact behavior part of OUR contract: when it changes, our
schedules change, and the failure is silent -- jobs quietly fire at a
different time, or twice, or not at all.

So every property the scheduler relies on is pinned here against real
datetimes rather than assumed from documentation. These tests need no
database and no worker: they are about arithmetic.

The DST cases are the ones that matter. A recurring-job platform lives or
dies on what it does at a transition, and both directions are surprising.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from croniter import croniter

from pyjobby.cron import is_wall_clock_anchored, next_cron_run

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def fires(expr: str, start: datetime, count: int) -> list[datetime]:
    """The next `count` fire times strictly after `start`, straight from
    croniter -- used to pin the RAW library behavior."""
    it = croniter(expr, start)
    return [it.get_next(datetime) for _ in range(count)]


def series(expr: str, start: datetime, count: int) -> list[datetime]:
    """The next `count` fire times through pyjobby's own entry point.

    This is what a schedule actually does: the scheduler recomputes from the
    instant it last fired, so the sequence is chained, not iterated.
    """
    out, cur = [], start
    for _ in range(count):
        cur = next_cron_run(expr, "America/New_York", after=cur)
        out.append(cur)
    return out


class TestFieldLayout:
    """How many columns an expression has, and what each one means."""

    @pytest.mark.parametrize(
        "expr,valid",
        [
            ("0 2 * * *", True),  # 5: minute hour dom month dow
            ("0 2 * * * 30", True),  # 6: + seconds, at the END
            ("0 2 * * * 30 2027", True),  # 7: + year
            ("0 2 * * *  ", True),  # surrounding whitespace is tolerated
            ("nope", False),
            ("0 99 * * *", False),  # hour out of range
            ("0 2 * *", False),  # too few columns
            ("", False),
        ],
    )
    def test_validity(self, expr, valid):
        assert croniter.is_valid(expr) is valid

    def test_sixth_field_is_seconds_at_the_end(self):
        """`0 2 * * * 30` is 02:00:30 -- NOT 30 seconds past every 2am minute.

        The scheduler must never build a 6-field expression assuming the
        Quartz seconds-first layout: the same six numbers mean different
        times under the two conventions.
        """
        start = datetime(2026, 1, 1, 0, 0, tzinfo=NY)

        assert fires("0 2 * * * 30", start, 1)[0] == datetime(
            2026, 1, 1, 2, 0, 30, tzinfo=NY
        )

    def test_seventh_field_is_the_year(self):
        start = datetime(2026, 1, 1, 0, 0, tzinfo=NY)

        assert fires("0 2 * * * 0 2028", start, 1)[0] == datetime(
            2028, 1, 1, 2, 0, tzinfo=NY
        )


class TestIteration:
    """What get_next() returns relative to the instant it was given."""

    def test_next_is_strictly_after_now(self):
        """Standing exactly on a fire time yields the FOLLOWING one.

        The scheduler stores next_run and recomputes from the moment it
        fired; if get_next were inclusive it would hand back the same
        instant forever and the schedule would fire in a loop.
        """
        exactly_on_it = datetime(2026, 1, 1, 2, 0, 0, tzinfo=NY)

        assert fires("0 2 * * *", exactly_on_it, 1)[0] == datetime(
            2026, 1, 2, 2, 0, tzinfo=NY
        )

    def test_result_keeps_the_start_timezone(self):
        """A schedule's timezone must survive the computation.

        next_run lands in a timestamptz column, so a result that came back
        naive or in UTC would be stored as a different instant.
        """
        start = datetime(2026, 6, 1, 0, 0, tzinfo=NY)

        result = fires("0 2 * * *", start, 1)[0]

        assert result.tzinfo is NY
        assert result.utcoffset() is not None

    def test_a_utc_schedule_is_unaffected_by_local_dst(self):
        """UTC has no transitions -- the escape hatch for anyone who wants
        an exact 24h period rather than a wall-clock time."""
        start = datetime(2027, 11, 6, 3, 0, tzinfo=UTC)

        assert fires("30 1 * * *", start, 3) == [
            datetime(2027, 11, 7, 1, 30, tzinfo=UTC),
            datetime(2027, 11, 8, 1, 30, tzinfo=UTC),
            datetime(2027, 11, 9, 1, 30, tzinfo=UTC),
        ]


class TestDaylightSavingTransitions:
    """The two days a year when wall-clock scheduling gets interesting."""

    def test_spring_forward_shifts_a_nonexistent_time_rather_than_skipping_it(self):
        """On 2027-03-14 America/New_York jumps 02:00 -> 03:00.

        02:30 does not exist that day. The job is NOT skipped: it fires at
        03:00, the instant the clock lands on. That is the behavior we want
        for a job platform -- a daily job silently not running is worse than
        one running a half hour late.
        """
        day_before = datetime(2027, 3, 13, 3, 0, tzinfo=NY)

        result = fires("30 2 * * *", day_before, 3)

        assert result[0] == datetime(2027, 3, 14, 3, 0, tzinfo=NY)
        assert result[0].utcoffset().total_seconds() == -4 * 3600  # EDT
        # and the schedule returns to its normal time immediately after
        assert result[1] == datetime(2027, 3, 15, 2, 30, tzinfo=NY)
        assert result[2] == datetime(2027, 3, 16, 2, 30, tzinfo=NY)

    def test_a_daily_schedule_fires_ONCE_on_fall_back_day(self):
        """The defect this rule exists to prevent: a daily job running twice.

        `30 1 * * *` means "once a day, at half past one". On the day 01:30
        happens twice, firing at both would run the job twice and duplicate
        every side effect it has -- a second invoice, a second email, a second
        charge. It fires once, and resumes normally the next day.
        """
        day_before = datetime(2027, 11, 6, 3, 0, tzinfo=NY)

        result = series("30 1 * * *", day_before, 3)

        assert result == [
            datetime(2027, 11, 7, 1, 30, tzinfo=NY),  # EDT, the first pass
            datetime(2027, 11, 8, 1, 30, tzinfo=NY),
            datetime(2027, 11, 9, 1, 30, tzinfo=NY),
        ]
        # one fire on the transition day, not two
        assert [f for f in result if f.date() == date(2027, 11, 7)] == [result[0]]
        assert result[0].utcoffset().total_seconds() == -4 * 3600

    @pytest.mark.parametrize(
        "expr,anchored",
        [
            ("30 1 * * *", True),  # a named hour
            ("0 2,14 * * *", True),  # several named hours
            ("30 1 * * * 15", True),  # 6-column form: hour is still field 2
            ("0 * * * *", False),  # every hour
            ("*/15 * * * *", False),  # every 15 minutes
            ("0 */2 * * *", False),  # every 2 hours
        ],
    )
    def test_which_schedules_are_wall_clock_anchored(self, expr, anchored):
        """The hour field decides whether a schedule means a time or a rate."""
        assert is_wall_clock_anchored(expr) is anchored

    def test_an_interval_schedule_keeps_both_passes(self):
        """An interval means real elapsed time, so both passes are genuine.

        Skipping one would leave a two-hour gap in an hourly schedule -- the
        opposite mistake from the daily double-fire.
        """
        before = datetime(2027, 11, 7, 0, 5, tzinfo=NY)

        result = series("0 * * * *", before, 4)

        # 01:00 appears twice, once at each offset
        assert [f.utcoffset().total_seconds() / 3600 for f in result[:2]] == [-4, -5]
        assert result[0].replace(tzinfo=None) == result[1].replace(tzinfo=None)
        # and the cadence is exactly one hour of real time throughout
        utc = [f.astimezone(UTC) for f in result]
        gaps = [(b - a).total_seconds() for a, b in zip(utc, utc[1:], strict=False)]
        assert gaps == [3600.0, 3600.0, 3600.0]

    def test_fall_back_yields_the_repeated_hour_twice(self):
        """On 2027-11-07 America/New_York repeats 01:00-02:00.

        01:30 happens twice -- once at UTC-4, once at UTC-5 -- and croniter
        yields BOTH, because as a wall-clock expression both really are
        matches.

        This is correct for a sub-hourly schedule, where skipping one would
        leave a two-hour gap. It is NOT what a DAILY schedule means, and
        pyjobby has to make that distinction itself: see the scheduler's
        deadline key. This test pins the raw behavior that distinction is
        built on.
        """
        day_before = datetime(2027, 11, 6, 3, 0, tzinfo=NY)

        result = fires("30 1 * * *", day_before, 3)

        first, second, next_day = result
        # same wall clock, same calendar day...
        assert first.replace(tzinfo=None, fold=0) == datetime(2027, 11, 7, 1, 30)
        assert second.replace(tzinfo=None, fold=0) == datetime(2027, 11, 7, 1, 30)
        # ...distinguished only by fold, and therefore by offset
        assert (first.fold, second.fold) == (0, 1)
        assert first.utcoffset().total_seconds() == -4 * 3600  # EDT
        assert second.utcoffset().total_seconds() == -5 * 3600  # EST
        # NOTE: `second - first` is 0. Subtracting two datetimes that share a
        # tzinfo is naive arithmetic -- Python ignores fold -- so any elapsed
        # time computed across a transition must go through UTC first.
        assert (second - first).total_seconds() == 0
        assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=1)
        assert next_day.replace(tzinfo=None, fold=0) == datetime(2027, 11, 8, 1, 30)

    def test_an_hourly_schedule_keeps_its_cadence_through_fall_back(self):
        """The reason the repeated hour must not be blanket-skipped."""
        before = datetime(2027, 11, 7, 0, 5, tzinfo=NY)

        result = fires("0 * * * *", before, 4)

        # every consecutive pair is exactly one hour of REAL time apart --
        # measured in UTC, because same-zone subtraction ignores fold and
        # would report the repeated hour as zero
        utc = [r.astimezone(UTC) for r in result]
        gaps = [(b - a).total_seconds() for a, b in zip(utc, utc[1:], strict=False)]
        assert gaps == [3600.0, 3600.0, 3600.0]

    def test_a_time_outside_the_transition_is_untouched(self):
        """Control: the transition affects only the hour it moves."""
        just_after_5am = datetime(2027, 11, 6, 5, 30, tzinfo=NY)

        result = fires("0 5 * * *", just_after_5am, 2)

        assert result[0] == datetime(2027, 11, 7, 5, 0, tzinfo=NY)
        assert result[1] == datetime(2027, 11, 8, 5, 0, tzinfo=NY)


class TestZoneInfoIsSufficient:
    """The scheduler uses the standard library, not pytz."""

    def test_zoneinfo_start_produces_correctly_offset_results(self):
        """A ZoneInfo tzinfo survives croniter's arithmetic with the right
        offset on each side of a transition -- which is what pytz's
        localize()/normalize() dance existed to guarantee."""
        start = datetime(2027, 3, 1, 0, 0, tzinfo=NY)

        winter, *_ = fires("0 12 * * *", start, 1)
        summer, *_ = fires("0 12 * * *", datetime(2027, 7, 1, 0, 0, tzinfo=NY), 1)

        assert winter.utcoffset().total_seconds() == -5 * 3600  # EST
        assert summer.utcoffset().total_seconds() == -4 * 3600  # EDT
        assert winter.tzname() == "EST"
        assert summer.tzname() == "EDT"

    def test_an_unknown_timezone_is_rejected(self):
        from zoneinfo import ZoneInfoNotFoundError

        with pytest.raises(ZoneInfoNotFoundError):
            ZoneInfo("Mars/Phobos")
