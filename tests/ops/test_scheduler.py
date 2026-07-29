"""The recurring scheduler's promises, fired and misfired for real.

TROUBLESHOOTING.md § "A schedule is not firing" names the ways a schedule
stops and how each one announces itself: the circuit breaker disables it
with a documented ERROR line and `schedule enable` (after fixing the job)
arms it again; a due schedule with no scheduler behind it is doctor's
overdue WARN; a scheduler that was down does not backfill missed fires;
and a max_concurrent skip is recorded as skipped, not failed. (That a due
schedule fires exactly once even with two schedulers racing is
test_multi_instance.py's.)
"""

from __future__ import annotations

import asyncio

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
        ), "missed ticks are skipped, never backfilled"
        assert await db_pool.fetchval(
            "SELECT next_run > now() FROM jorb_schedule WHERE id = $1", schedule_id
        ), "next_run advances from now"


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
