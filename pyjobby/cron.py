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

from datetime import datetime
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


def next_cron_run(expr: str, timezone: str, after: datetime | None = None) -> datetime:
    """When ``expr`` next fires in ``timezone``, strictly after ``after``.

    ``after`` defaults to now. The result is aware and carries the schedule's
    own timezone, so storing it in a timestamptz column records the intended
    instant.

    Validation happens up front so a bad expression or zone fails where it
    was entered, rather than at fire time -- an unevaluatable schedule is a
    schedule that silently never runs.
    """
    tz = resolve_timezone(timezone)
    validate_cron(expr)
    moment = after.astimezone(tz) if after is not None else datetime.now(tz)
    fire: datetime = croniter(expr, moment).get_next(datetime)
    return fire
