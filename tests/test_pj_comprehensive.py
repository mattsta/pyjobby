"""
Comprehensive tests for pj.py JobSystem worker (schema v1).

Tests the core job processing machinery: prepared statements, claiming
(priority / capability / epoch bump), execution state transitions, the
terminal 'crashed' DLQ, timeout bookkeeping, rescheduling and retry
backoff, job class loading, and connection-loss recovery.
"""

import asyncio
import contextlib
import os
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from pyjobby import db
from pyjobby.pj import STMTS, Job, JobSystem

pytestmark = pytest.mark.asyncio


@contextlib.asynccontextmanager
async def prepared_system(db_params, worker_params):
    """A JobSystem with a real connection and all STMTS prepared."""
    system = JobSystem(dsn=db_params, **worker_params)
    system.cxn = await db.connect(**db_params)
    try:
        system.stmts = {
            name: await system.cxn.prepare(stmt) for name, stmt in STMTS.items()
        }
        yield system
    finally:
        await system.cxn.close()


# =============================================================================
# JOB SYSTEM INITIALIZATION TESTS
# =============================================================================


class TestJobSystemInitialization:
    """Test JobSystem initialization and setup."""

    async def test_job_system_basic_init(self, db_params, worker_params):
        """Test basic JobSystem initialization."""
        system = JobSystem(dsn=db_params, **worker_params)

        assert system.qname == worker_params["qname"]
        assert system.capabilities == worker_params["capabilities"]
        assert system.workerId == worker_params["workerId"]
        assert system.checkInterval == worker_params["checkInterval"]
        assert system.prio == worker_params["prio"]
        assert system.max_retries == worker_params["max_retries"]
        assert system.default_timeout == worker_params["default_timeout"]
        # schema-v1 worker: registry heartbeats instead of recovery guessing
        assert system.heartbeat_interval == 10.0
        assert system._launcher_pid == 0

    async def test_job_system_prepared_statements(self, db_params, worker_params):
        """Test prepared statements are correctly set up."""
        async with prepared_system(db_params, worker_params) as system:
            for name in (
                "claim",
                "get-result",
                "run",
                "finished",
                "retry",
                "crashed",
                "cancelled",
                "reschedule",
                "enqueue-next-if-peer-group-is-finished",
                "enqueue-next-self-finished",
                "load-steps",
                "record-step",
                "set-event",
                "send",
                "recv",
            ):
                assert name in system.stmts

            # machinery removed must stay gone: schema v1's verbs, the dead
            # "get" probe nothing executed, and "set-timeout" (the deadline
            # rides in the "run" statement itself now)
            for gone in (
                "create-retry",
                "recover-abandoned",
                "cancel",
                "crash",
                "get",
                "set-timeout",
            ):
                assert gone not in system.stmts


# =============================================================================
# JOB CLAIMING TESTS
# =============================================================================


class TestJobClaiming:
    """Test job claiming logic and SQL prepared statements."""

    async def test_claim_queued_job(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test claiming a queued job."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio,
                                 capability, run_after)
               VALUES ($1, $2, $3, $4, $5, $6, now() - INTERVAL '1 second')
               RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "queued",
            100,
            "test",
        )

        async with prepared_system(db_params, worker_params) as system:
            claimed = await system.ex(
                "claim",
                system.pid,
                system.node,
                unique_queue,
                ("test",),
                1000,
                None,  # not registered in jorb_worker for this unit test
            )

            assert len(claimed) == 1
            assert claimed[0]["id"] == job_id
            assert claimed[0]["state"] == "claimed"
            assert claimed[0]["queue"] == unique_queue
            assert claimed[0]["worker_pid"] == system.pid
            assert claimed[0]["worker_host"] == system.node
            assert claimed[0]["run_epoch"] == 1

    async def test_claim_respects_priority(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test job claiming respects priority order."""
        await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio, run_after)
               VALUES ($1, $2, $3, $4, $5, now() - INTERVAL '1 second')
               RETURNING id""",
            "test.LowPrio",
            {},
            unique_queue,
            "queued",
            200,
        )
        high_prio_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio, run_after)
               VALUES ($1, $2, $3, $4, $5, now() - INTERVAL '1 second')
               RETURNING id""",
            "test.HighPrio",
            {},
            unique_queue,
            "queued",
            10,
        )

        async with prepared_system(db_params, worker_params) as system:
            # Claim should get most urgent job first (lower number)
            claimed = await system.ex(
                "claim", system.pid, system.node, unique_queue, (), 1000, None
            )

            assert len(claimed) == 1
            assert claimed[0]["id"] == high_prio_id
            assert claimed[0]["prio"] == 10

    async def test_claim_respects_capability_filter(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test job claiming respects capability filtering."""
        no_cap_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio, run_after)
               VALUES ($1, $2, $3, $4, $5, now() - INTERVAL '1 second')
               RETURNING id""",
            "test.NoCapability",
            {},
            unique_queue,
            "queued",
            100,
        )
        special_cap_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio,
                                 capability, run_after)
               VALUES ($1, $2, $3, $4, $5, $6, now() - INTERVAL '1 second')
               RETURNING id""",
            "test.SpecialJob",
            {},
            unique_queue,
            "queued",
            100,
            "special",
        )

        # Worker WITHOUT the special capability
        basic_worker_params = {**worker_params, "capabilities": ()}
        async with prepared_system(db_params, basic_worker_params) as system:
            claimed = await system.ex(
                "claim", system.pid, system.node, unique_queue, (), 1000, None
            )

            # gets the capability-free job, never the special one
            assert [r["id"] for r in claimed] == [no_cap_id]

            # nothing else claimable for this worker
            again = await system.ex(
                "claim", system.pid, system.node, unique_queue, (), 1000, None
            )
            assert again == []

            special = await system.cxn.fetchrow(
                "SELECT state FROM jorb WHERE id = $1", special_cap_id
            )
            assert special["state"] == "queued"


# =============================================================================
# JOB EXECUTION TESTS
# =============================================================================


class TestJobExecution:
    """Test job execution and state transitions."""

    async def test_mark_job_running(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test marking a claimed job as running."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "claimed",
            100,
        )

        async with prepared_system(db_params, worker_params) as system:
            # 'run' is epoch-fenced: this directly inserted row is at epoch 0
            await system.ex("run", job_id, 0, None)

            job = await system.cxn.fetchrow(
                "SELECT state, started FROM jorb WHERE id = $1", job_id
            )
            assert job["state"] == "running"
            assert job["started"] is not None

    async def test_mark_job_success(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test marking a job as successfully finished."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio, started)
               VALUES ($1, $2, $3, $4, $5, now()) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "running",
            100,
        )

        async with prepared_system(db_params, worker_params) as system:
            result_data = {"output": "success", "count": 42}
            await system.ex("finished", job_id, result_data, 0)

            job = await system.cxn.fetchrow(
                "SELECT state, finished, result FROM jorb WHERE id = $1", job_id
            )
            assert job["state"] == "finished"
            assert job["finished"] is not None
            assert job["result"] == result_data

    async def test_mark_job_crashed(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test dead-lettering a job with error details."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio,
                                 started, error_count)
               VALUES ($1, $2, $3, $4, $5, now(), 0) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "running",
            100,
        )

        async with prepared_system(db_params, worker_params) as system:
            error_msg = "Division by zero"
            error_trace = "Traceback (most recent call last):\n  File..."
            await system.ex("crashed", job_id, error_msg, error_trace, 0)

            job = await system.cxn.fetchrow(
                """SELECT state, error_message, error_backtrace, error_count
                   FROM jorb WHERE id = $1""",
                job_id,
            )
            assert job["state"] == "crashed"
            assert job["error_message"] == error_msg
            assert job["error_backtrace"] == error_trace
            assert job["error_count"] == 1


# =============================================================================
# TIMEOUT HANDLING TESTS
# =============================================================================


class TestTimeoutHandling:
    """Test job timeout bookkeeping."""

    async def test_timeout_at_calculation(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test timeout_at is correctly set (and epoch-fenced)."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "claimed",
            100,
        )

        async with prepared_system(db_params, worker_params) as system:
            # the deadline rides in the claimed -> running write itself
            await system.ex(
                "run", job_id, 0, timedelta(seconds=system.default_timeout)
            )

            job = await system.cxn.fetchrow(
                "SELECT timeout_at, started FROM jorb WHERE id = $1", job_id
            )
            assert job["timeout_at"] is not None
            assert job["timeout_at"] > job["started"]

            # a stale epoch cannot start the job again or move the deadline
            stale = await system.ex("run", job_id, 42, timedelta(hours=99))
            assert stale == []
            unchanged = await system.cxn.fetchrow(
                "SELECT timeout_at FROM jorb WHERE id = $1", job_id
            )
            assert unchanged["timeout_at"] == job["timeout_at"]


# =============================================================================
# RETRY LOGIC TESTS
# =============================================================================


class TestRetryLogic:
    """Test job retry mechanisms and error counting."""

    async def test_error_count_increments(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test error_count increments in place on each failure."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio,
                                 started, error_count)
               VALUES ($1, $2, $3, $4, $5, now(), 2) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "running",
            100,
        )

        async with prepared_system(db_params, worker_params) as system:
            await system.ex("crashed", job_id, "Test error", "Traceback...", 0)

            job = await system.cxn.fetchrow(
                "SELECT error_count FROM jorb WHERE id = $1", job_id
            )
            assert job["error_count"] == 3

    async def test_retry_requeues_same_row_with_backoff(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """A retryable failure requeues the SAME row with a delay."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio, started)
               VALUES ($1, $2, $3, $4, $5, now()) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "running",
            100,
        )

        async with prepared_system(db_params, worker_params) as system:
            retried = await system.ex(
                "retry", job_id, timedelta(seconds=30), "flaky", "Traceback...", 0
            )
            assert [r["id"] for r in retried] == [job_id]
            # the statement returns only the id (a retry's full row — result,
            # backtrace — was round-tripped and discarded); read the state
            # it produced from the row itself
            row = await system.cxn.fetchrow(
                "SELECT state, error_count, run_after FROM jorb WHERE id = $1",
                job_id,
            )
            assert row["state"] == "queued"
            assert row["error_count"] == 1
            assert row["run_after"] > datetime.now(UTC)

            # no copy rows were created
            rows = await system.cxn.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            assert rows == 1

    async def test_max_retries_dlq(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test jobs exceeding max_retries enter DLQ (stay crashed)."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio,
                                 started, error_count)
               VALUES ($1, $2, $3, $4, $5, now(), $6) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "running",
            100,
            worker_params["max_retries"],
        )

        async with prepared_system(db_params, worker_params) as system:
            await system.ex("crashed", job_id, "Fatal error", "Traceback...", 0)

            job = await system.cxn.fetchrow(
                "SELECT state, error_count FROM jorb WHERE id = $1", job_id
            )
            # Terminal: 'crashed' IS the dead letter queue
            assert job["state"] == "crashed"
            assert job["error_count"] == worker_params["max_retries"] + 1


# =============================================================================
# JOB CLASS LOADING TESTS
# =============================================================================


class TestJobClassLoading:
    """Test dynamic job class loading."""

    async def test_class_for_klass_from_name(self, worker_params, db_params):
        """Test loading and instantiating a job class from its dotted path."""
        from tests import dxe_jobs

        system = JobSystem(dsn=db_params, **worker_params)

        instance = system.classForKlassFromName(
            "tests.dxe_jobs.OkJob", job={"kwargs": {"x": 3}}
        )
        # module is reloaded on each lookup, so compare by class name
        assert type(instance).__name__ == dxe_jobs.OkJob.__name__
        assert instance.s is system
        assert instance.job == {"kwargs": {"x": 3}}


# =============================================================================
# Job Rescheduling and Retry Strategy Tests
# =============================================================================


class TestJobRescheduling:
    """Tests for job rescheduling and retry backoff strategies."""

    async def test_reschedule_seconds(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test reschedule() with seconds interval."""
        async with prepared_system(db_params, worker_params) as system:
            job_id = await db_pool.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                "test.Job",
                {},
                unique_queue,
                # a task can only reschedule ITSELF, so the row is running:
                # reschedule is fenced to the live attempt
                "running",
                100,
            )
            job = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            job_class = Job(s=system, job=dict(job))

            # Reschedule 300 seconds in the future
            interval = await job_class.reschedule(300, "seconds")
            assert interval == timedelta(seconds=300)

            # Verify job was pushed into the future in the database
            updated_job = await db_pool.fetchrow(
                "SELECT * FROM jorb WHERE id = $1", job_id
            )
            assert updated_job["state"] == "queued"
            assert updated_job["run_after"] > datetime.now(UTC) + timedelta(minutes=4)

    async def test_reschedule_with_custom_deltas(
        self, db_pool, worker_params, db_params, unique_queue
    ):
        """Test reschedule() with custom delta dict."""
        async with prepared_system(db_params, worker_params) as system:
            job_id = await db_pool.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                "test.Job",
                {},
                unique_queue,
                # a task can only reschedule ITSELF, so the row is running:
                # reschedule is fenced to the live attempt
                "running",
                100,
            )
            job = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            job_class = Job(s=system, job=dict(job))

            # Reschedule with complex delta: 1 day + 2 hours + 30 minutes
            interval = await job_class.reschedule(
                0,  # relative is ignored when deltas provided
                deltas={"days": 1, "hours": 2, "minutes": 30},
            )
            assert interval == timedelta(days=1, hours=2, minutes=30)

    async def test_reschedule_backoff_with_retry_strategy(self, db_params):
        """Test rescheduleBackoff() uses the configured retry strategy."""
        job = {
            "error_count": 3,
            "admin_data": {"retry_strategy": "exponential", "initial_retry_delay": 2},
        }
        job_class = Job(s=None, job=job)

        # Formula: initial_delay * (2 ^ (attempt - 1)) = 2 * (2 ^ 2) = 8 s
        # Retry strategies add jitter (0-10% of delay) against thundering herd
        interval = await job_class.rescheduleBackoff(attempt=3)
        assert timedelta(seconds=8) <= interval <= timedelta(seconds=9)

    async def test_reschedule_backoff_uses_error_count(self, db_params):
        """Test rescheduleBackoff() defaults to the job's error_count."""
        job = {
            "error_count": 5,
            "admin_data": {"retry_strategy": "linear", "initial_retry_delay": 10},
        }
        job_class = Job(s=None, job=job)

        # Linear formula: initial_delay * error_count = 10 * 5 = 50 seconds
        interval = await job_class.rescheduleBackoff()
        assert timedelta(seconds=50) <= interval <= timedelta(seconds=55)


# =============================================================================
# JobClass Execution Tests
# =============================================================================


class TestJobClassExecution:
    """Tests for Job.run()."""

    async def test_job_class_run_calls_task(self, db_params, worker_params):
        """Test Job.run() calls task() with kwargs from the job row."""
        system = JobSystem(dsn=db_params, **worker_params)

        class TestJob(Job):
            def task(self, **kwargs):
                # Return kwargs to verify they were passed
                return kwargs

        job_class = TestJob(s=system, job={"kwargs": {"arg1": "value1", "arg2": 42}})
        assert job_class.run() == {"arg1": "value1", "arg2": 42}

    async def test_job_class_run_with_empty_kwargs(self, db_params, worker_params):
        """Test Job.run() with empty kwargs."""
        system = JobSystem(dsn=db_params, **worker_params)

        class TestJob(Job):
            def task(self, **kwargs):
                return "executed"

        job_class = TestJob(s=system, job={"kwargs": {}})
        assert job_class.run() == "executed"


# =============================================================================
# Error Handling and Edge Cases Tests
# =============================================================================


class TestJobSystemErrorHandling:
    """Tests for error handling and edge cases in JobSystem."""

    async def test_shutdown_signal_handler(self, db_params, worker_params):
        """Test shutdown() sets the stop flag."""
        system = JobSystem(dsn=db_params, **worker_params)

        # Initially stop should be False
        assert system.stop is False

        # Call shutdown (signal handler)
        system.shutdown(15, None)  # SIGTERM = 15

        # Verify stop flag is set
        assert system.stop is True

    async def test_class_not_found_error(self, db_params, worker_params):
        """classForKlassFromName() raises FileNotFoundError for missing class."""
        system = JobSystem(dsn=db_params, **worker_params)

        # pyjobby.pj module exists, but NonExistentJobClass doesn't
        with pytest.raises(FileNotFoundError) as exc_info:
            system.classForKlassFromName("pyjobby.pj.NonExistentJobClass")

        assert "Job class not found" in str(exc_info.value)
        assert "pyjobby.pj.NonExistentJobClass" in str(exc_info.value)

    async def test_database_interface_error_reconnect(
        self, db_pool, db_params, worker_params, unique_queue
    ):
        """Test ex() reconnects and retries on InterfaceError."""
        system = JobSystem(dsn=db_params, **worker_params)
        system.cxn = await db.connect(**db_params)
        system.stmts = {
            name: await system.cxn.prepare(stmt) for name, stmt in STMTS.items()
        }
        # reconnect re-runs _listen(); give its callbacks their state
        system._wake = asyncio.Event()
        system._current_job_id = None
        system._exec_task = None

        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
            "queued",
            100,
        )

        # Mock statement fetch to raise InterfaceError with the connection
        # REALLY gone — that pairing is what "lost connection" means, and it
        # is what ex() keys its reconnect on (an InterfaceError over a live
        # connection is caller misuse and raises instead). ex() must then
        # rebuild the connection, re-prepare every statement from STMTS, and
        # retry the operation against the real database.
        call_count = 0

        class MockPreparedStatement:
            async def fetch(self, *args):
                nonlocal call_count
                call_count += 1
                await system.cxn.close()
                raise asyncpg.InterfaceError("connection is closed")

        system.stmts["get-result"] = MockPreparedStatement()

        try:
            result = await system.ex("get-result", job_id)

            # The broken statement was tried exactly once, then replaced by a
            # freshly prepared statement during reconnect — the point is that
            # ex() recovered and completed against the real database.
            assert call_count == 1
            assert [r["state"] for r in result] == ["queued"]
            assert not isinstance(
                system.stmts["get-result"], MockPreparedStatement
            )
        finally:
            await system.cxn.close()

    async def test_ex_reconnects_on_a_raw_oserror(
        self, db_pool, db_params, worker_params, unique_queue
    ):
        """A failover delivers a socket-level drop as ConnectionResetError/
        BrokenPipeError (OSError), below asyncpg's own exception types. On
        the claim path that would escape _process AND run() and kill the
        worker — the exact opposite of self-healing — unless ex() treats it
        as a lost connection and reconnects."""
        system = JobSystem(dsn=db_params, **worker_params)
        system.cxn = await db.connect(**db_params)
        system.stmts = {
            name: await system.cxn.prepare(stmt) for name, stmt in STMTS.items()
        }
        system._wake = asyncio.Event()
        system._current_job_id = None
        system._exec_task = None
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio)
               VALUES ($1, $2, $3, 'queued', 100) RETURNING id""",
            "test.Job",
            {},
            unique_queue,
        )

        tripped = False

        class DropsSocket:
            async def fetch(self, *args):
                nonlocal tripped
                if not tripped:
                    tripped = True
                    await system.cxn.close()
                    raise ConnectionResetError("peer reset the connection")
                raise AssertionError("should have been replaced on reconnect")

        system.stmts["get-result"] = DropsSocket()
        try:
            result = await system.ex("get-result", job_id)
            assert tripped
            assert [r["state"] for r in result] == ["queued"]
            assert not isinstance(system.stmts["get-result"], DropsSocket)
        finally:
            await system.cxn.close()

    async def test_heartbeat_loop_registers_an_unregistered_worker(
        self, db_params, worker_params
    ):
        """Registration failure at startup is a DEGRADED mode (claims carry
        no owner, enqueue wakeups are off) — it must be retried from the
        heartbeat loop, not accepted forever."""
        async with prepared_system(db_params, worker_params) as system:
            system.worker_id = None
            system._hb_cxn = None  # as after a failed startup registration
            system.heartbeat_interval = 0.05

            task = asyncio.create_task(system._heartbeat_loop())
            try:
                deadline = asyncio.get_running_loop().time() + 5.0
                while system.worker_id is None:
                    assert asyncio.get_running_loop().time() < deadline, (
                        "heartbeat loop never registered the worker"
                    )
                    await asyncio.sleep(0.05)
            finally:
                system.stop = True
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                if system._hb_cxn is not None:
                    await system._hb_cxn.close()

            registered = await system.cxn.fetchrow(
                "SELECT queue FROM jorb_worker WHERE id = $1", system.worker_id
            )
            assert registered["queue"] == system.qname

    async def test_ex_raises_on_client_side_misuse_instead_of_spinning(
        self, db_params, worker_params
    ):
        """asyncpg reports wrong-arity calls as InterfaceError — the same
        type a lost connection raises. Over a LIVE connection that is a bug
        in the caller, and reconnecting cannot fix a bug: ex() must raise
        it loudly rather than reconnect-and-retry the same mistake forever
        (which is exactly what it did when the 'run' statement grew a third
        parameter and one caller kept passing two)."""
        async with prepared_system(db_params, worker_params) as system:
            with pytest.raises(asyncpg.exceptions.InterfaceError):
                await system.ex("run", 1, 0)  # missing the deadline argument
            assert not system.cxn.is_closed()


class TestJobClassResolution:
    """Job classes are resolved once and cached; reload is opt-in.

    Importing is a filesystem stat + compile + module execution. Doing that
    per job costs throughput and re-runs arbitrary module-level code every
    time (which is how a module containing test decorators could break a
    test run mid-flight)."""

    async def test_class_is_cached_between_lookups(self, db_params, worker_params):
        async with prepared_system(db_params, worker_params) as system:
            first = system.resolve_job_class("tests.dxe_jobs.OkJob")
            second = system.resolve_job_class("tests.dxe_jobs.OkJob")

            # same object: the module was not re-imported
            assert first is second
            assert system._class_cache["tests.dxe_jobs.OkJob"] is first

    async def test_default_worker_does_not_reload_modules(
        self, db_params, worker_params
    ):
        async with prepared_system(db_params, worker_params) as system:
            assert system.reload_jobs is False
            before = system.resolve_job_class("tests.dxe_jobs.OkJob")
            # a second resolution must not produce a NEW class object, which
            # is what importlib.reload() would do
            assert system.resolve_job_class("tests.dxe_jobs.OkJob") is before

    async def test_unknown_class_raises_file_not_found(self, db_params, worker_params):
        async with prepared_system(db_params, worker_params) as system:
            with pytest.raises(FileNotFoundError, match="nope.NotAJob"):
                system.resolve_job_class("nope.NotAJob")

    async def test_non_job_class_is_rejected(self, db_params, worker_params):
        """A dotted path that resolves to something other than a Job is a
        configuration error, not something to instantiate blindly."""
        async with prepared_system(db_params, worker_params) as system:
            with pytest.raises(TypeError, match="not a pyjobby Job subclass"):
                system.resolve_job_class("json.JSONDecoder")

    async def test_reload_flag_reimports_only_on_source_change(
        self, db_params, worker_params, tmp_path, monkeypatch
    ):
        """With --reload, an edited module is re-imported on next lookup."""
        import sys as _sys

        module_dir = tmp_path
        module_file = module_dir / "reloadable_jobs.py"
        module_file.write_text(
            "from pyjobby.pj import Job\n\n\n"
            "class Reloadable(Job):\n"
            "    marker = 'first'\n\n"
            "    def task(self):\n"
            "        return self.marker\n"
        )
        monkeypatch.syspath_prepend(str(module_dir))
        _sys.modules.pop("reloadable_jobs", None)

        async with prepared_system(
            db_params, {**worker_params, "reload_jobs": True}
        ) as system:
            first = system.resolve_job_class("reloadable_jobs.Reloadable")
            assert first.marker == "first"

            # unchanged source: no re-import, same class object
            assert system.resolve_job_class("reloadable_jobs.Reloadable") is first

            # edit it (bump mtime explicitly; filesystem resolution varies)
            module_file.write_text(
                "from pyjobby.pj import Job\n\n\n"
                "class Reloadable(Job):\n"
                "    marker = 'second'\n\n"
                "    def task(self):\n"
                "        return self.marker\n"
            )
            os.utime(module_file, (time.time() + 10, time.time() + 10))

            reloaded = system.resolve_job_class("reloadable_jobs.Reloadable")
            assert reloaded.marker == "second"
            assert reloaded is not first

        _sys.modules.pop("reloadable_jobs", None)


class TestJobWebExtensionPoint:
    """The per-worker HTTP listener dispatches to Job.web()."""

    async def test_job_without_web_returns_not_implemented(self, db_params):
        from pyjobby.pj import Job

        response = await Job.web(None)  # type: ignore[arg-type]
        assert response.status == 501
        assert "does not implement web()" in response.text

    async def test_handler_rejects_classes_not_listed_in_config(
        self, db_params, worker_params
    ):
        """Only classes in web_listen['paths'] are reachable: the dotted
        name comes from the URL, so an open lookup would let a caller
        import and invoke arbitrary code."""
        from unittest.mock import Mock

        system = JobSystem(
            dsn=db_params,
            **{**worker_params, "webPort": {"paths": {"tests.dxe_jobs.OkJob"}}},
        )
        request = Mock()
        request.path = "/os.system"

        response = await system.webHandler(request)
        assert response.status == 404
