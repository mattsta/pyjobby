"""Cron evaluation for pyjobby schedules.

croniter does the parsing and the arithmetic. This module owns the two
conventions everything else in the platform depends on, so that no caller
can get them half right:

* **Every croniter instance is built from a timezone-AWARE datetime.**
  croniter's documentation is explicit that this is required for DST to be
  handled at all ("Be sure to init your croniter instance with a TZ aware
  datetime for this to work!"); a naive datetime silently computes fire
  times in some other zone.
* **Timezones come from the standard library.** ``zoneinfo`` is the first
  option croniter documents, and it needs no ``localize()``/``normalize()``
  ritual to produce correctly-offset results on both sides of a transition.

Column layout (croniter's default, pinned by tests/test_cron_semantics.py):
``minute hour day-of-month month day-of-week``, with an optional 6th column
for **seconds at the END** and an optional 7th for the year. The seconds
column is NOT the Quartz seconds-first layout -- the same six numbers mean
different times under the two conventions.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter  # type: ignore[import-untyped]


def resolve_timezone(name: str) -> ZoneInfo:
    """The IANA zone called ``name``.

    Raises ValueError (not KeyError) so callers have one exception type to
    handle for "this schedule cannot be evaluated".
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone {name!r}") from e


def validate_cron(expr: str) -> None:
    """Raise ValueError unless ``expr`` is a cron expression croniter accepts."""
    if not croniter.is_valid(expr):
        raise ValueError(f"malformed cron expression {expr!r}")


def is_wall_clock_anchored(expr: str) -> bool:
    """True when ``expr`` names specific hours rather than an interval.

    This is the distinction that decides what a schedule MEANS when a
    daylight-saving transition repeats an hour:

    * ``30 1 * * *`` names 01:30. It means "once a day, at half past one",
      and it must fire once on the day 01:30 happens twice.
    * ``0 * * * *`` and ``0 */2 * * *`` name an interval. They mean "every
      hour" / "every two hours" of REAL time, so both passes through the
      repeated hour are genuine -- skipping one would leave a two-hour gap.

    The hour field decides it: a wildcard or a step is an interval, anything
    that enumerates hours is anchored to the wall clock. (This is the rule
    vixie cron settled on for the same reason.)
    """
    fields = expr.split()
    if len(fields) < 5:
        return False
    # 6- and 7-column forms append seconds and year, so the hour is always
    # the second field -- the layout croniter uses by default.
    hour = fields[1]
    return "*" not in hour


def next_cron_run(expr: str, timezone: str, after: datetime | None = None) -> datetime:
    """When ``expr`` next fires in ``timezone``, strictly after ``after``.

    ``after`` defaults to now. The result is aware and carries the schedule's
    own timezone, so storing it in a timestamptz column records the intended
    instant.

    Validation happens up front so a bad expression or zone fails where it
    was entered, rather than at fire time -- an unevaluatable schedule is a
    schedule that silently never runs.

    On the day a zone falls back, a wall-clock-anchored schedule skips the
    SECOND pass through the repeated hour: `fold=1` marks that instant as a
    replay of a wall-clock time the schedule has already fired at, and firing
    again would run a daily job twice and duplicate its side effects. Interval
    schedules keep both passes -- see :func:`is_wall_clock_anchored`.
    """
    tz = resolve_timezone(timezone)
    validate_cron(expr)
    moment = after if after is not None else datetime.now(tz)
    return next(_fire_series(expr, tz, moment))


def _fire_series(expr: str, tz: ZoneInfo, after: datetime) -> Iterator[datetime]:
    """Every instant ``expr`` fires at in ``tz``, ascending, strictly after
    ``after``. Infinite: cron expressions do not end.

    THE one walk of a cron expression in the platform. Both questions asked of
    one -- "when does this fire next" and "what did it miss while nothing was
    running" -- read this series, so the fall-back rule below cannot apply to
    one and not the other. A backfill that enumerated ticks by a second route
    would eventually enqueue an instant the firing path never fires at, or drop
    one it does, and neither disagreement announces itself.
    """
    anchored = is_wall_clock_anchored(expr)
    it = croniter(expr, after.astimezone(tz))
    while True:
        fire: datetime = it.get_next(datetime)
        # `fold=1` marks a REPLAY of a wall-clock time an anchored schedule has
        # already fired at -- see this module's docstring and next_cron_run.
        if fire.fold == 1 and anchored:
            continue
        yield fire


class MissedRuns(NamedTuple):
    """What a schedule would have fired at while nothing was firing it.

    ``kept`` is what a bounded backfill enqueues; ``dropped`` counts the older
    instants it deliberately will not, and ``dropped_window`` bounds them --
    ``None`` exactly when ``dropped`` is 0 -- so the one summary row recording
    them can say which ticks they were.
    """

    kept: tuple[datetime, ...]
    dropped: int
    dropped_window: tuple[datetime, datetime] | None


def missed_cron_runs(
    expr: str, timezone: str, *, after: datetime, until: datetime, keep: int
) -> MissedRuns:
    """The instants ``expr`` fires at in ``(after, until]``, newest ``keep``.

    Both bounds are aware datetimes; the results carry ``timezone``, like
    :func:`next_cron_run`'s, so they can be stored in a timestamptz column or
    used as a job's ``run_after`` without conversion. ``after`` is EXCLUSIVE:
    a schedule's own ``next_run`` is the tick it is currently due for, not a
    tick it missed.

    Only the newest ``keep`` are returned because the value of a late fire
    decays -- yesterday's report is worth running, last Tuesday's is not --
    and because returning all of them is how a recovered scheduler floods a
    queue that is still behind.

    One croniter step per instant in the window, so this costs the length of
    the outage rather than ``keep``. That is the price of an EXACT dropped
    count, and the count is the point: an outage whose size is recorded
    nowhere is an outage nobody notices.
    """
    tz = resolve_timezone(timezone)
    validate_cron(expr)

    kept: list[datetime] = []
    dropped = 0
    first_dropped: datetime | None = None
    last_dropped: datetime | None = None

    def drop(fire: datetime) -> None:
        nonlocal dropped, first_dropped, last_dropped
        dropped += 1
        if first_dropped is None:
            first_dropped = fire
        last_dropped = fire

    for fire in _fire_series(expr, tz, after):
        if fire > until:
            break
        if keep <= 0:
            drop(fire)
        else:
            # The window is walked forward, so "the newest keep" is whatever
            # survives eviction -- and an evicted instant is a dropped one.
            if len(kept) == keep:
                drop(kept.pop(0))
            kept.append(fire)

    window = (
        None
        if first_dropped is None or last_dropped is None
        else (first_dropped, last_dropped)
    )
    return MissedRuns(kept=tuple(kept), dropped=dropped, dropped_window=window)
