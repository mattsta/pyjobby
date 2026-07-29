"""The recurring scheduler's promises, fired and misfired for real.

TROUBLESHOOTING.md § "A schedule is not firing" names the ways a schedule
stops and how each one announces itself: the circuit breaker disables it
with a documented ERROR line and `schedule enable` (after fixing the job)
arms it again; a due schedule with no scheduler behind it is doctor's
overdue WARN; a scheduler that was down backfills missed fires only if the
schedule asked for it, and then only as many as it asked for; and a
max_concurrent skip is recorded as skipped, not failed. (That a due schedule
fires exactly once even with two schedulers racing is test_multi_instance.py's;
the backfill window's arithmetic is tests/test_cron_semantics.py's and its
per-tick bookkeeping tests/test_scheduler_correctness.py's.)
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tests.ops.conftest import wait_until

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]


async def add_schedule(admin, db_pool, name: str, queue: str, *args: str) -> int:
    added = admin(
        "schedule",
        "add",
        name,
        "tests.dxe_jobs.OkJob",
        "* * * * *",
        "--queue",
        queue,
        *args,
    )
    assert added.returncode == 0, added.stdout + added.stderr
    return await db_pool.fetchval("SELECT id FROM jorb_schedule WHERE name = $1", name)


async def make_due(db_pool, schedule_id: int, minutes_ago: float = 0.1) -> None:
    await db_pool.execute(
        "UPDATE jorb_schedule SET next_run = now() - ($2 * interval '1 minute') "
        "WHERE id = $1",
        schedule_id,
        minutes_ago,
    )


class TestCircuitBreaker:
    async def test_threshold_disables_with_the_documented_error_and_enable_rearms(
        self, fleet, admin, db_pool, unique_queue, test_id
    ):
        name = f"sched_{test_id}"
        schedule_id = await add_schedule(admin, db_pool, name, unique_queue)
        # At the threshold: five consecutive fire failures already recorded.
        await db_pool.execute(
            "UPDATE jorb_schedule SET consecutive_failures = 5 WHERE id = $1",
            schedule_id,
        )
        await make_due(db_pool, schedule_id)

        scheduler = fleet.scheduler(poll_interval=1)
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb_schedule WHERE id = $1 AND NOT enabled",
                schedule_id,
            ),
            describe="circuit breaker disabled the schedule",
            timeout=20,
        )
        assert (
            f"Schedule '{name}' disabled: Circuit breaker triggered: "
            "5 consecutive failures (threshold: 5)" in scheduler.log_text()
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE schedule_id = $1", schedule_id
            )
            == 0
        ), "a tripped breaker must mint nothing"

        # The documented remedy: fix the job, then `schedule enable` -- which
        # must also reset the failure count, or the very next fire re-trips.
        enabled = admin("schedule", "enable", name)
        assert enabled.returncode == 0
        row = await db_pool.fetchrow(
            "SELECT enabled, consecutive_failures FROM jorb_schedule WHERE id = $1",
            schedule_id,
        )
        assert row["enabled"] and row["consecutive_failures"] == 0

        await make_due(db_pool, schedule_id)
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE schedule_id = $1", schedule_id
            ),
            describe="re-enabled schedule fired",
            timeout=20,
        )


class TestOverdueWarn:
    async def test_a_due_schedule_with_no_scheduler_is_doctors_overdue_warn(
        self, admin, db_pool, unique_queue, test_id
    ):
        schedule_id = await add_schedule(
            admin, db_pool, f"sched_{test_id}", unique_queue
        )
        await make_due(db_pool, schedule_id, minutes_ago=6)

        report = admin("doctor")
        assert report.returncode == 0, "overdue is a WARN; exit stays 0"
        assert "WARN schedules:" in report.stdout
        assert "overdue by >5m" in report.stdout
        assert "is pj-scheduler running?" in report.stdout


class TestNoBackfill:
    """The default: `backfill_limit` 0, and missed ticks stay missed."""

    async def test_a_long_outage_yields_one_fire_not_one_per_missed_tick(
        self, fleet, admin, db_pool, unique_queue, test_id
    ):
        schedule_id = await add_schedule(
            admin, db_pool, f"sched_{test_id}", unique_queue
        )
        # Ten missed every-minute ticks.
        await make_due(db_pool, schedule_id, minutes_ago=10)

        fleet.scheduler(poll_interval=1)
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE schedule_id = $1", schedule_id
            ),
            describe="schedule fired after the outage",
            timeout=20,
        )
        await asyncio.sleep(3)
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE schedule_id = $1", schedule_id
            )
            == 1
        ), "at backfill_limit 0, missed ticks are skipped and never backfilled"
        assert await db_pool.fetchval(
            "SELECT next_run > now() FROM jorb_schedule WHERE id = $1", schedule_id
        ), "next_run advances from now"


class TestBoundedBackfill:
    """The opt-in: `--backfill-limit N`, and never more than N + 1 fires.

    The bound is checked through the real `pj-scheduler` process because that
    is where an unbounded backfill would do its damage -- a burst of jobs
    landing on a queue that is already behind, from a process nobody is
    watching at the moment it recovers.
    """

    async def test_a_recovery_fires_the_bound_and_records_what_it_dropped(
        self, fleet, admin, db_pool, unique_queue, test_id
    ):
        name = f"sched_{test_id}"
        # max-concurrent 3 is what makes "exactly 3" a FACT rather than a race
        # with the next minute: the recovery burst fills the allowance, so the
        # tick after it is refused however long this test then looks.
        schedule_id = await add_schedule(
            admin,
            db_pool,
            name,
            unique_queue,
            "--backfill-limit",
            "2",
            "--max-concurrent",
            "3",
        )
        # Ten missed every-minute ticks, of which two may be caught up on.
        await make_due(db_pool, schedule_id, minutes_ago=10)

        fleet.scheduler(poll_interval=1)
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT count(*) = 3 FROM jorb WHERE schedule_id = $1", schedule_id
            ),
            describe="the due tick and two backfilled ticks fired",
            timeout=20,
        )
        await asyncio.sleep(3)
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE schedule_id = $1", schedule_id
            )
            == 3
        ), "backfill_limit 2 must never mint more than 2 + 1 jobs on recovery"

        # The dropped ticks are RECORDED, as one row -- silence about them is
        # how an unbounded backfill hides, and a bound set too low hides the
        # same way if nobody can see what it cost.
        summaries = await db_pool.fetch(
            "SELECT * FROM jorb_schedule_log WHERE schedule_id = $1 "
            "AND skip_reason = 'backfill_limit'",
            schedule_id,
        )
        assert len(summaries) == 1
        assert "not backfilled (backfill_limit=2)" in summaries[0]["error_message"]

        # and it is in the operator-facing view the docs point at
        history = admin("schedule", "history", name, "--result", "skipped")
        assert history.returncode == 0
        assert "backfill_limit" in history.stdout

        # A backfilled fire is logged against the tick it was FOR, never
        # against the moment it really ran, so the three rows name three
        # different ticks and every one of them ran after its own instant.
        fires = await db_pool.fetch(
            "SELECT scheduled_time, actual_time FROM jorb_schedule_log "
            "WHERE schedule_id = $1 AND result = 'success' ORDER BY scheduled_time",
            schedule_id,
        )
        assert len(fires) == 3
        assert (
            fires[0]["scheduled_time"]
            < fires[1]["scheduled_time"]
            < fires[2]["scheduled_time"]
        )
        assert all(r["actual_time"] > r["scheduled_time"] for r in fires)
        # And they are the MOST RECENT missed ticks, not the oldest: the
        # freshest one is within a couple of minutes of the recovery, which it
        # could not be had the bound kept the head of a ten-minute window.
        assert fires[2]["actual_time"] - fires[2]["scheduled_time"] < timedelta(
            minutes=3
        )


class TestMaxConcurrentSkip:
    async def test_a_blocked_fire_is_recorded_as_skipped_not_failed(
        self, fleet, admin, db_pool, unique_queue, test_id
    ):
        name = f"sched_{test_id}"
        schedule_id = await add_schedule(
            admin, db_pool, name, unique_queue, "--max-concurrent", "1"
        )
        fleet.scheduler(poll_interval=1)

        # First fire mints a job; with no workers it sits queued, which
        # counts against max_concurrent.
        await make_due(db_pool, schedule_id)
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE schedule_id = $1", schedule_id
            ),
            describe="first fire minted its job",
            timeout=20,
        )
        # Second fire must skip, and say so.
        await make_due(db_pool, schedule_id)
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb_schedule_log WHERE schedule_id = $1 "
                "AND result = 'skipped'",
                schedule_id,
            ),
            describe="blocked fire recorded as skipped",
            timeout=20,
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE schedule_id = $1", schedule_id
            )
            == 1
        )
        # The operator-facing view the docs point at: the skipped fire is
        # listed (rendered as the "-" icon) with its reason in Details.
        history = admin("schedule", "history", name, "--result", "skipped")
        assert history.returncode == 0
        assert "max_concurrent" in history.stdout
        assert "Total: 1 execution(s)" in history.stdout
