"""Regression tests for recurring-scheduler correctness.

Each test here pins a property the scheduler got wrong: an execution-log
timestamp that was off by the database server's UTC offset, a schedule whose
cron expression cannot be evaluated wedging the poll loop forever, and a
stop() that only took effect a whole poll interval later.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from pyjobby import db
from pyjobby.scheduler import (
    ScheduleExecutionResult,
    ScheduleManager,
    SchedulerWorker,
    ScheduleSafetyManager,
)

pytestmark = pytest.mark.asyncio


async def _insert_schedule(conn, *, cron_expr: str, timezone: str = "UTC") -> int:
    """Insert a schedule row directly, bypassing validation.

    Schedules can reach this state through a hand-edited row or a cron
    expression that a newer croniter no longer accepts.
    """
    schedule_id: int = await conn.fetchval(
        """
        INSERT INTO jorb_schedule (name, job_class, cron_expr, timezone, next_run)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING id
        """,
        f"correctness-{uuid.uuid4().hex[:8]}",
        f"test.Job_{uuid.uuid4().hex[:8]}",
        cron_expr,
        timezone,
        datetime.now(UTC) - timedelta(minutes=5),
    )
    return schedule_id


async def _run_one_pass(worker: SchedulerWorker) -> None:
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.4)
    worker.stop()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestExecutionLogTimestamps:
    async def test_actual_time_is_the_real_instant(self, db_connection):
        """actual_time is timestamptz, so it must be written aware.

        asyncpg encodes a NAIVE datetime for a timestamptz column by reading
        it in the SERVER's time zone. datetime.utcnow() is naive, so every
        jorb_schedule_log row was displaced by the server's UTC offset --
        invisible on a UTC server, hours wrong on any other.
        """
        # Pin a non-UTC server zone so the defect cannot hide behind the
        # local postgres configuration (rolled back with the test's txn).
        await db_connection.execute("SET TIME ZONE 'America/New_York'")

        schedule_id = await _insert_schedule(db_connection, cron_expr="* * * * *")
        schedule = dict(
            await db_connection.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
        )

        worker = SchedulerWorker(db_connection)
        await worker.log_execution(
            schedule,
            schedule["next_run"],
            ScheduleExecutionResult(result="success", job_id=1),
        )

        actual_time, server_now = await db_connection.fetchrow(
            "SELECT actual_time, now() FROM jorb_schedule_log WHERE schedule_id = $1",
            schedule_id,
        )
        assert abs(actual_time - server_now) < timedelta(seconds=10)


class TestUnevaluatableSchedule:
    async def test_broken_cron_disables_the_schedule(self, db_pool):
        """A schedule whose cron cannot be evaluated must fail loudly, once.

        next_run was advanced at the END of the firing transaction, so an
        unevaluatable expression rolled the whole transaction back: no job,
        no log row, no failure counted, and next_run still in the past. The
        schedule was then re-selected and re-failed on every single poll,
        forever, without leaving a trace anywhere an operator would look.
        """
        async with db_pool.acquire() as conn:
            schedule_id = await _insert_schedule(conn, cron_expr="not a cron")
            before = await conn.fetchrow(
                "SELECT next_run, run_count FROM jorb_schedule WHERE id = $1",
                schedule_id,
            )

            worker = SchedulerWorker(conn, poll_interval=0.05)
            await _run_one_pass(worker)

            after = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
            assert after["enabled"] is False
            assert after["failure_count"] == 1
            assert after["consecutive_failures"] == 1
            assert after["run_count"] == before["run_count"] + 1
            assert after["next_run"] == before["next_run"]

            log = await conn.fetch(
                "SELECT * FROM jorb_schedule_log WHERE schedule_id = $1", schedule_id
            )
            assert len(log) == 1
            assert log[0]["result"] == "failure"
            assert "not a cron" in log[0]["error_message"]

    async def test_broken_timezone_disables_the_schedule(self, db_pool):
        """Same handling for a timezone the platform cannot resolve."""
        async with db_pool.acquire() as conn:
            schedule_id = await _insert_schedule(
                conn, cron_expr="* * * * *", timezone="Mars/Olympus_Mons"
            )

            worker = SchedulerWorker(conn, poll_interval=0.05)
            await _run_one_pass(worker)

            after = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
            assert after["enabled"] is False
            assert after["failure_count"] == 1

    async def test_working_schedule_still_advances(self, db_pool):
        """The guard must not disturb a schedule that evaluates fine."""
        async with db_pool.acquire() as conn:
            schedule_id = await _insert_schedule(conn, cron_expr="* * * * *")

            worker = SchedulerWorker(conn, poll_interval=0.05)
            await _run_one_pass(worker)

            after = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
            assert after["enabled"] is True
            assert after["success_count"] >= 1
            assert after["next_run"] > datetime.now(UTC)
            expected = ScheduleManager.calculate_next_run("* * * * *", "UTC")
            assert after["next_run"] <= expected


class TestGracefulShutdown:
    async def test_stop_interrupts_the_poll_sleep(self, db_pool):
        """stop() must cut the poll sleep short.

        The loop only re-read stop_requested after a full asyncio.sleep(
        poll_interval), so SIGTERM took up to a whole poll interval to take
        effect -- long enough for an orchestrator to escalate to SIGKILL
        while the scheduler was between polls.
        """
        async with db_pool.acquire() as conn:
            worker = SchedulerWorker(conn, poll_interval=30)

            task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.2)  # let it reach the sleep

            started = time.monotonic()
            worker.stop()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                pytest.fail("run() did not return after stop()")

            assert time.monotonic() - started < 5.0


class TestFireTimeCeiling:
    async def test_a_schedule_above_the_ceiling_is_disabled_not_spun(
        self, db_connection
    ):
        """The scheduler mints a job per firing through the SHARED enqueue
        path, so the fleet's priority ceiling is enforced at fire time — a
        schedule above it used to stream unclaimable jobs forever with no
        validation anywhere. Disabling with the reason (the unevaluatable-
        cron treatment) beats one failure per poll forever."""
        schedule_id = await _insert_schedule(db_connection, cron_expr="* * * * *")
        await db_connection.execute(
            "UPDATE jorb_schedule SET prio = 5000 WHERE id = $1", schedule_id
        )
        schedule = dict(
            await db_connection.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
        )

        worker = SchedulerWorker(db_connection, prio_ceiling=1000)
        result = await worker.execute_schedule(schedule)

        assert result.result == "failure"
        assert "ceiling" in (result.error_message or "")
        row = await db_connection.fetchrow(
            "SELECT enabled, failure_count FROM jorb_schedule WHERE id = $1",
            schedule_id,
        )
        assert row["enabled"] is False
        assert row["failure_count"] >= 1
        # and no unclaimable job escaped
        minted = await db_connection.fetchval(
            "SELECT count(*) FROM jorb WHERE schedule_id = $1", schedule_id
        )
        assert minted == 0


class TestConnectionRecovery:
    """A scheduler that stops firing after a database restart has failed at
    its one job. The loop reconnects with backoff instead."""

    async def test_reconnect_rebuilds_the_connection_and_every_holder(
        self, db_params, db_pool
    ):
        conn = await db.connect(**db_params)
        worker = SchedulerWorker(conn, poll_interval=0.05)
        worker.db_params = dict(db_params)

        await conn.close()
        assert conn.is_closed()

        await worker._reconnect()

        # a fresh connection, shared by every component that queries
        assert not worker.conn.is_closed()
        assert worker.manager.conn is worker.conn
        assert worker.safety.conn is worker.conn
        assert await worker.conn.fetchval("SELECT 1") == 1
        await worker.conn.close()

    async def test_the_loop_survives_a_killed_connection(self, db_params, db_pool):
        """End to end: the run loop hits a dead connection, reconnects, and
        keeps polling instead of dying or spinning."""
        conn = await db.connect(**db_params)
        worker = SchedulerWorker(conn, poll_interval=0.05)
        worker.db_params = dict(db_params)

        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.2)  # let it poll at least once
        await conn.close()  # the "database restart"

        # A schedule that comes due only AFTER the connection dies: reconnecting
        # is necessary but not sufficient -- a loop that reconnects yet no
        # longer fires is exactly the silent failure "survives" must exclude,
        # so the proof is a job this schedule could only have created by firing
        # post-recovery.
        name = f"survives-{uuid.uuid4().hex[:8]}"
        schedule_id = await db_pool.fetchval(
            """
            INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run)
            VALUES ($1, 'test.Job', '* * * * *', $2)
            RETURNING id
            """,
            name,
            datetime.now(UTC) - timedelta(minutes=1),
        )
        await asyncio.sleep(1.0)  # a few cycles to notice and recover

        assert not worker.conn.is_closed(), "scheduler did not reconnect"
        fired = await db_pool.fetchval(
            "SELECT count(*) FROM jorb WHERE schedule_id = $1", schedule_id
        )
        assert fired >= 1, "scheduler reconnected but stopped firing schedules"
        worker.stop()
        await asyncio.wait_for(task, timeout=5.0)
        await worker.conn.close()


class TestConcurrencyLimitStillEnforces:
    """max_concurrent_jobs is a SAFETY limit, and it was made faster.

    The check moved from counting `admin_data->>'schedule_id'` -- which no
    index could serve, so it scanned the whole job table on every firing --
    to counting the `jorb.schedule_id` column through a partial index.
    tests/test_scale_plans.py asserts it got cheaper. These assert it still
    REFUSES, because a limit that has been optimised into never binding is a
    schedule outrunning its own job with nothing in the log to say so, which
    is the failure the limit exists to prevent.

    Both directions matter and neither implies the other: a check that always
    counted zero would pass "it fires", and a check that always counted
    infinity would pass "it refuses".
    """

    async def _schedule_at_limit(self, conn, *, limit: int) -> dict:
        """A schedule that is already due, with `limit` slots.

        Both tests fire it by calling execute_schedule() once rather than by
        running the poll loop, and that is not shorthand -- it is the only
        way these assertions are FACTS. The loop advances next_run to the top
        of the next minute, so a pass that happens to straddle a minute
        boundary fires the schedule a second time and the counters land on
        whatever the wall clock was doing. That is a test whose result
        depends on when it ran, which is worse than no test: it fails for a
        reason that has nothing to do with the limit.
        """
        row = await conn.fetchrow(
            """
            INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run,
                                       max_concurrent_jobs)
            VALUES ($1, 'test.Job', '* * * * *', $2, $3)
            RETURNING *
            """,
            f"concurrency-{uuid.uuid4().hex[:8]}",
            datetime.now(UTC) - timedelta(minutes=5),
            limit,
        )
        return dict(row)

    async def test_a_schedule_at_its_limit_does_not_fire(self, db_pool):
        async with db_pool.acquire() as conn:
            schedule = await self._schedule_at_limit(conn, limit=2)
            for state in ("running", "queued"):
                await conn.execute(
                    "INSERT INTO jorb (job_class, kwargs, state, schedule_id) "
                    "VALUES ('test.Job', '{}', $1, $2)",
                    state,
                    schedule["id"],
                )

            result = await SchedulerWorker(conn).execute_schedule(schedule)

            assert result.result == "skipped"
            assert result.skip_reason == "max_concurrent"
            assert result.concurrent_jobs == 2
            assert result.job_id is None
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM jorb WHERE schedule_id = $1", schedule["id"]
                )
                == 2
            ), "the schedule created a third job while at max_concurrent_jobs 2"
            after = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule["id"]
            )
            assert after["skip_count"] == 1
            assert after["success_count"] == 0

    async def test_it_fires_again_once_a_job_finishes(self, db_pool):
        """The other direction: the refusal has to be temporary.

        A terminal job is outside the index predicate entirely, which is the
        whole reason the check reads only in-flight work -- so this also pins
        that leaving the index means leaving the count.
        """
        async with db_pool.acquire() as conn:
            schedule = await self._schedule_at_limit(conn, limit=1)
            blocker = await conn.fetchval(
                "INSERT INTO jorb (job_class, kwargs, state, schedule_id) "
                "VALUES ('test.Job', '{}', 'running', $1) RETURNING id",
                schedule["id"],
            )
            worker = SchedulerWorker(conn)

            blocked = await worker.execute_schedule(schedule)
            assert blocked.result == "skipped"
            assert blocked.skip_reason == "max_concurrent"

            await conn.execute(
                "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1",
                blocker,
            )

            freed = await worker.execute_schedule(schedule)

            assert freed.result == "success", "the slot freed up and it still refused"
            assert freed.concurrent_jobs == 0, "a finished job still counted"
            created = await conn.fetchrow(
                "SELECT * FROM jorb WHERE id = $1", freed.job_id
            )
            assert created["state"] == "queued"
            assert created["schedule_id"] == schedule["id"]
            after = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule["id"]
            )
            assert after["success_count"] == 1
            assert after["skip_count"] == 1


class TestScheduleProvenanceHasOneSource:
    """Which schedule made a job is the COLUMN, and only the column.

    It used to be `admin_data->>'schedule_id'` as well, and keeping both would
    have been the ordinary way to make this change safe -- which is exactly
    how two copies of one fact start disagreeing, silently, in the direction
    nobody checks. So the key is gone and the column is the whole answer: the
    scheduler writes it, the concurrency check reads it, and a job reads it
    off its own row as `self.job["schedule_id"]` rather than out of a jsonb
    blob as a string.
    """

    async def test_the_created_job_carries_the_column_and_not_the_json_key(
        self, db_pool
    ):
        async with db_pool.acquire() as conn:
            schedule_id = await _insert_schedule(conn, cron_expr="* * * * *")
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            worker = SchedulerWorker(conn, poll_interval=0.05)
            job_id = await worker.create_scheduled_job(
                dict(schedule), datetime.now(UTC), jitter_seconds=0
            )

            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job["schedule_id"] == schedule_id
            assert "schedule_id" not in job["admin_data"], (
                "the jsonb copy came back; two sources of one fact will "
                "disagree eventually"
            )
            # the descriptive half stays in admin_data -- nothing filters on it
            assert job["admin_data"]["schedule_name"] == schedule["name"]
            assert "scheduled_time" in job["admin_data"]

    async def test_the_concurrency_check_counts_what_the_scheduler_wrote(self, db_pool):
        """The two halves of the contract, joined.

        Writing the column and counting by it are separate lines of code, and
        a check that counted something the scheduler does not write would be
        a limit that never binds -- passing every test that only ever inserts
        its own fixture rows.
        """
        async with db_pool.acquire() as conn:
            schedule_id = await _insert_schedule(conn, cron_expr="* * * * *")
            schedule = dict(
                await conn.fetchrow(
                    "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
                )
            )
            safety = ScheduleSafetyManager(conn)
            worker = SchedulerWorker(conn, poll_interval=0.05)

            assert await safety.check_concurrency(schedule_id, 3) == (True, 0)

            await worker.create_scheduled_job(
                schedule, datetime.now(UTC), jitter_seconds=0
            )
            assert await safety.check_concurrency(schedule_id, 3) == (True, 1)

            await worker.create_scheduled_job(
                schedule, datetime.now(UTC) + timedelta(minutes=1), jitter_seconds=0
            )
            await worker.create_scheduled_job(
                schedule, datetime.now(UTC) + timedelta(minutes=2), jitter_seconds=0
            )
            assert await safety.check_concurrency(schedule_id, 3) == (False, 3)
