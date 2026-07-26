"""
Comprehensive tests for pj.py JobSystem run() loop - THE CORE WORKER!

Tests the ACTUAL worker execution loop with LIVE database operations.
NO MOCKS - real workers via the ``live_worker`` fixture, real job classes
(shared ones from ``tests.dxe_jobs``, plus local classes for shapes that
have no shared equivalent: sync tasks, async generators, self-reschedules,
web handlers).

Schema v1 semantics under test: same-row retries with error_count/run_epoch
bumped in place, terminal 'crashed' as the DLQ, timeout handling honoring
admin_data on_timeout, and jorb_history as the per-attempt audit trail.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

import pytest

from pyjobby.pj import Job, JobSystem

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio


# ============================================================================
# Local Job Classes (shapes not covered by tests.dxe_jobs)
# ============================================================================


class SyncQuickJob(Job):
    """Synchronous job that completes quickly (OkJob is async)."""

    def task(self, value: str = "default"):
        return f"quick: {value}"


class AsyncGenJob(Job):
    """Job that returns an async generator."""

    async def task(self):
        async def gen():
            for i in range(3):
                yield i

        return gen()


class AsyncJobNoTimeout(Job):
    """Async job with NO timeout configured."""

    timeout = 0  # Explicitly set to 0 to disable timeout

    async def task(self, value: str = "no_timeout"):
        await asyncio.sleep(0.01)
        return f"async_no_timeout: {value}"


class AsyncGenJobNoTimeout(Job):
    """Async generator job with NO timeout configured."""

    timeout = 0  # Explicitly set to 0 to disable timeout

    async def task(self):
        async def gen():
            for i in range(2):
                yield f"item_{i}"

        return gen()


class DirectAsyncGenJob(Job):
    """Job that directly returns async generator (not from async function)."""

    timeout = 0  # Explicitly set to 0 to disable timeout

    def run(self):
        """Override run() to return async generator directly."""

        async def gen():
            for i in range(2):
                yield f"direct_{i}"

        return gen()


class AsyncGenJobWithTimeout(Job):
    """Async generator job WITH timeout configured."""

    timeout = 5  # 5 second timeout

    async def task(self):
        async def gen():
            for i in range(3):
                await asyncio.sleep(0.01)
                yield f"item_{i}"

        return gen()


class DirectAsyncGenJobWithTimeout(Job):
    """Job that directly returns async generator WITH timeout."""

    timeout = 5  # 5 second timeout

    def run(self):
        """Override run() to return async generator directly."""

        async def gen():
            for i in range(2):
                await asyncio.sleep(0.01)
                yield f"direct_{i}"

        return gen()


class ReschedulingJob(Job):
    """Job that reschedules itself to run later."""

    async def task(self, seconds_delay: int = 60):
        # Call reschedule to defer this job
        await self.reschedule(seconds_delay, "seconds")
        return f"rescheduled_for_{seconds_delay}_seconds"


class ReschedulingJobWithDeltas(Job):
    """Job that reschedules itself using deltas dict."""

    async def task(self):
        # Use deltas dict for complex rescheduling
        await self.reschedule(0, deltas={"minutes": 5, "seconds": 30})
        return "rescheduled_with_deltas"


THIS = "tests.test_pj_worker_run_loop"


async def enqueue(conn, queue, job_class, kwargs=None, admin_data=None, **cols):
    """Insert a queued job row (jsonb columns default to {}; never NULL)."""
    return await conn.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        job_class,
        kwargs or {},
        queue,
        admin_data or {},
    )


async def wait_for(condition, timeout: float = 10.0, interval: float = 0.1):
    """Poll `condition` (async, returns truthy/row) until it holds."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = await condition()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


# ============================================================================
# Test Main run() Loop
# ============================================================================


class TestWorkerRunLoop:
    """Test the actual worker run() loop execution."""

    async def test_worker_processes_single_job(
        self, live_worker, unique_queue, db_pool
    ):
        """Test worker run loop processes a (sync) job."""
        await live_worker()

        job_id = await enqueue(
            db_pool, unique_queue, f"{THIS}.SyncQuickJob", {"value": "test1"}
        )

        job = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert job["result"] == "quick: test1"
        assert job["run_count"] == 1
        assert job["run_epoch"] == 1
        assert job["worker_pid"] is not None

    async def test_worker_processes_multiple_jobs(
        self, live_worker, unique_queue, db_pool
    ):
        """Test worker processes multiple jobs in sequence."""
        await live_worker()

        job_ids = [
            await enqueue(
                db_pool, unique_queue, f"{THIS}.SyncQuickJob", {"value": f"job{i}"}
            )
            for i in range(3)
        ]

        for i, job_id in enumerate(job_ids):
            job = await wait_for_job_state(db_pool, job_id, ("finished",))
            assert job["result"] == f"quick: job{i}"

    async def test_worker_processes_async_jobs(
        self, live_worker, unique_queue, db_pool
    ):
        """Test worker can process async jobs (shared OkJob is async)."""
        await live_worker()

        job_id = await enqueue(db_pool, unique_queue, "tests.dxe_jobs.OkJob", {"x": 21})

        job = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert job["result"] == {"doubled": 42}


# ============================================================================
# Test Timeout Handling in run() Loop
# ============================================================================


class TestWorkerTimeoutHandling:
    """Test timeout handling within the run() loop."""

    async def test_worker_handles_job_timeout_with_retry(
        self, live_worker, unique_queue, db_pool
    ):
        """A timeout retries the SAME row, then dead-letters when exhausted."""
        await live_worker()

        job_id = await enqueue(
            db_pool,
            unique_queue,
            "tests.dxe_jobs.SlowJob",
            {"seconds": 30},
            admin_data={
                "timeout_seconds": 1,
                "on_timeout": "retry",
                "max_retries": 2,
                "initial_retry_delay": 0,
            },
        )

        # attempt 1 times out -> requeued (same row); attempt 2 times out ->
        # retries exhausted -> terminal 'crashed'
        job = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=20)
        assert job["error_count"] == 2
        assert job["run_epoch"] == 2
        assert "timed out" in job["error_message"].lower()

        # the retry was a same-row requeue carrying the timeout error
        requeue_detail = await db_pool.fetchval(
            """SELECT detail FROM jorb_history
               WHERE job_id = $1 AND event = 'queued' LIMIT 1""",
            job_id,
        )
        assert "timed out" in requeue_detail["error"].lower()

        # ONE row for the whole life of the job — no retry copies
        rows = await db_pool.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
        )
        assert rows == 1

    async def test_worker_handles_timeout_with_fail(
        self, live_worker, unique_queue, db_pool
    ):
        """Test worker handles timeout when on_timeout=fail."""
        await live_worker()

        job_id = await enqueue(
            db_pool,
            unique_queue,
            "tests.dxe_jobs.SlowJob",
            {"seconds": 30},
            admin_data={"timeout_seconds": 1, "on_timeout": "fail", "max_retries": 3},
        )

        # first timeout dead-letters immediately (no retry allowed)
        job = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=15)
        assert job["error_count"] == 1
        assert job["run_epoch"] == 1

        # never requeued: no 'queued' transition after the initial enqueue
        requeues = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_history WHERE job_id = $1 AND event='queued'",
            job_id,
        )
        assert requeues == 0


# ============================================================================
# Test Exception Handling in run() Loop
# ============================================================================


class TestWorkerExceptionHandling:
    """Test exception handling and retry logic in run() loop."""

    async def test_worker_handles_exception_with_retry(
        self, live_worker, unique_queue, db_pool
    ):
        """A failing job goes back to 'queued' on the SAME row with backoff."""
        await live_worker()

        job_id = await enqueue(
            db_pool,
            unique_queue,
            "tests.dxe_jobs.FailJob",
            admin_data={
                "max_retries": 3,
                "retry_strategy": "exponential",
                # long delay so the retry sits observably queued
                "initial_retry_delay": 60,
            },
        )

        # wait for the first failed attempt to be requeued
        job = await wait_for(
            lambda: db_pool.fetchrow(
                "SELECT * FROM jorb WHERE id = $1 AND error_count > 0", job_id
            )
        )

        assert job["state"] == "queued"  # same row, back in the queue
        assert job["error_count"] == 1
        assert "intentional failure" in job["error_message"]
        assert "Traceback" in (job["error_backtrace"] or "")
        assert job["run_after"] > datetime.now(UTC)  # backoff applied

        # no retry-copy rows: the queue still holds exactly one job
        rows = await db_pool.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
        )
        assert rows == 1

    async def test_worker_stops_retry_after_max_attempts(
        self, live_worker, unique_queue, db_pool
    ):
        """Test worker dead-letters after max_retries attempts."""
        await live_worker()

        # error_count starts at 2: the next failure is attempt 3 of 3
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, error_count, admin_data)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            "tests.dxe_jobs.FailJob",
            {},
            unique_queue,
            2,
            {"max_retries": 3, "initial_retry_delay": 0},
        )

        job = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=15)
        assert job["error_count"] == 3

        # terminal: nothing left queued
        queued = await db_pool.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1 AND state = 'queued'",
            unique_queue,
        )
        assert queued == 0


# ============================================================================
# Test run() Loop Edge Cases
# ============================================================================


class TestWorkerRunLoopEdgeCases:
    """Test edge cases in the run() loop."""

    async def test_worker_handles_empty_queue(self, live_worker, unique_queue, db_pool):
        """Test worker correctly idles when queue is empty."""
        system = await live_worker()

        # Let worker poll an empty queue for a while
        await asyncio.sleep(0.8)

        # still alive and heartbeating (registry row is open)
        assert not system.stop
        live = await db_pool.fetchval(
            """SELECT count(*) FROM jorb_worker
               WHERE queue = $1 AND shutdown_at IS NULL""",
            unique_queue,
        )
        assert live == 1

    async def test_worker_respects_queue_filter(
        self, live_worker, unique_queue, db_pool
    ):
        """Test worker only processes jobs from its own queue."""
        other_queue = f"{unique_queue}_other"
        await live_worker()

        job_id_a = await enqueue(
            db_pool, unique_queue, f"{THIS}.SyncQuickJob", {"value": "mine"}
        )
        job_id_b = await enqueue(
            db_pool, other_queue, f"{THIS}.SyncQuickJob", {"value": "other"}
        )

        job_a = await wait_for_job_state(db_pool, job_id_a, ("finished",))
        assert job_a["result"] == "quick: mine"

        # the other queue's job is untouched
        await asyncio.sleep(0.5)
        job_b = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id_b)
        assert job_b["state"] == "queued", (
            f"other-queue job should not be processed, got {job_b['state']}"
        )

    async def test_worker_processes_async_generator_job(
        self, live_worker, unique_queue, db_pool
    ):
        """Test worker can handle async generator jobs."""
        await live_worker()

        job_id = await enqueue(db_pool, unique_queue, f"{THIS}.AsyncGenJob")

        job = await wait_for_job_state(db_pool, job_id, ("finished",))
        # Result should be list from async generator
        assert job["result"] == [0, 1, 2]


# ============================================================================
# Test Jobs Without Timeouts
# ============================================================================


class TestJobsWithoutTimeouts:
    """Test jobs that have NO timeout configured."""

    async def test_async_job_without_timeout(self, live_worker, unique_queue, db_pool):
        """Test async job with NO timeout configured."""
        await live_worker()

        job_id = await enqueue(
            db_pool, unique_queue, f"{THIS}.AsyncJobNoTimeout", {"value": "test"}
        )

        job = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert job["result"] == "async_no_timeout: test"
        assert job["timeout_at"] is None  # no deadline was armed

    async def test_async_generator_from_async_function_without_timeout(
        self, live_worker, unique_queue, db_pool
    ):
        """Test async generator (from async function) with NO timeout."""
        await live_worker()

        job_id = await enqueue(db_pool, unique_queue, f"{THIS}.AsyncGenJobNoTimeout")

        job = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert job["result"] == ["item_0", "item_1"]

    async def test_direct_async_generator_without_timeout(
        self, live_worker, unique_queue, db_pool
    ):
        """Test direct async generator (via run() override) with NO timeout."""
        await live_worker()

        job_id = await enqueue(db_pool, unique_queue, f"{THIS}.DirectAsyncGenJob")

        job = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert job["result"] == ["direct_0", "direct_1"]


# ============================================================================
# Test RescheduleBackoff Edge Cases
# ============================================================================


class TestRescheduleBackoffEdgeCases:
    """Test rescheduleBackoff edge cases (pure calculation, no DB)."""

    async def test_rescheduleBackoff_with_none_attempt(self):
        """rescheduleBackoff with attempt=None uses the job's error_count."""
        from datetime import timedelta

        job_dict = {
            "id": 999,
            "error_count": 2,
            "admin_data": {},  # No retry_strategy specified, will use default
        }

        # Class-style call with attempt=None: uses error_count = 2
        delay = await Job.rescheduleBackoff(job_dict, attempt=None)

        assert isinstance(delay, timedelta)
        assert delay.total_seconds() > 0, "Delay should be positive"

    async def test_rescheduleBackoff_with_explicit_attempt(self):
        """rescheduleBackoff with explicit attempt ignores error_count."""
        job_dict = {
            "id": 999,
            "error_count": 5,  # This should be ignored when attempt is provided
            "admin_data": {"retry_strategy": "exponential"},
        }

        # Should use attempt=1, NOT error_count=5
        delay = await Job.rescheduleBackoff(job_dict, attempt=1)

        # With exponential and attempt=1, should be 1 second (2^0 = 1)
        assert delay.total_seconds() == 1.0, (
            f"Expected 1s for attempt=1, got {delay.total_seconds()}s"
        )


# ============================================================================
# Test Async Generator WITH Timeout
# ============================================================================


class TestAsyncGeneratorWithTimeout:
    """Test async generators WITH a timeout configured."""

    async def test_async_generator_from_async_function_with_timeout(
        self, live_worker, unique_queue, db_pool
    ):
        """Test async generator (from async function) WITH timeout."""
        await live_worker()

        job_id = await enqueue(db_pool, unique_queue, f"{THIS}.AsyncGenJobWithTimeout")

        job = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert job["result"] == ["item_0", "item_1", "item_2"]

    async def test_direct_async_generator_with_timeout(
        self, live_worker, unique_queue, db_pool
    ):
        """Test direct async generator (via run() override) WITH timeout."""
        await live_worker()

        job_id = await enqueue(
            db_pool, unique_queue, f"{THIS}.DirectAsyncGenJobWithTimeout"
        )

        job = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert job["result"] == ["direct_0", "direct_1"]


# ============================================================================
# Test Job Rescheduling
# ============================================================================


class TestJobReschedule:
    """Test job.reschedule(): a self-reschedule wins over completion."""

    async def test_job_reschedule_with_seconds(
        self, live_worker, unique_queue, db_pool
    ):
        """Test job calls reschedule() to defer execution."""
        await live_worker()

        job_id = await enqueue(
            db_pool,
            unique_queue,
            f"{THIS}.ReschedulingJob",
            {"seconds_delay": 300},  # Reschedule for 5 minutes
        )

        # reschedule() puts the job back in 'queued' with a future run_after;
        # the worker's finished-update is epoch/state-guarded so it does NOT
        # cancel the self-requested reschedule.
        job = await wait_for(
            lambda: db_pool.fetchrow(
                """SELECT * FROM jorb
                   WHERE id = $1 AND state = 'queued' AND run_after > now()""",
                job_id,
            )
        )
        assert job["state"] == "queued"
        assert job["run_after"] > datetime.now(UTC)
        assert job["result"] is None  # completion did not overwrite

    async def test_job_reschedule_with_deltas(self, live_worker, unique_queue, db_pool):
        """Test job calls reschedule() with a deltas dict."""
        await live_worker()

        job_id = await enqueue(
            db_pool, unique_queue, f"{THIS}.ReschedulingJobWithDeltas"
        )

        # reschedule() wins over normal completion: the job stays queued
        # for its future run instead of being stamped finished.
        job = await wait_for(
            lambda: db_pool.fetchrow(
                """SELECT * FROM jorb
                   WHERE id = $1 AND state = 'queued' AND run_after > now()""",
                job_id,
            )
        )
        assert job["run_after"] > datetime.now(UTC)


# ============================================================================
# Test Web Handler
# ============================================================================


class WebEnabledJob(Job):
    """Job that has a web() method for handling HTTP requests."""

    @classmethod
    def web(cls, request):
        from aiohttp import web as aiohttp_web

        return aiohttp_web.Response(text="web_job_response")


class AsyncWebEnabledJob(Job):
    """Job that has an async web() method."""

    @classmethod
    async def web(cls, request):
        from aiohttp import web as aiohttp_web

        await asyncio.sleep(0.01)  # Simulate async work
        return aiohttp_web.Response(text="async_web_job_response")


class TestWebHandler:
    """Test web handler functionality."""

    async def test_web_handler_sync_response(self, db_params, worker_params):
        """Test webHandler with sync web() method."""
        from aiohttp.test_utils import make_mocked_request

        system = JobSystem(
            dsn=db_params,
            **{
                **worker_params,
                "webPort": {"paths": {f"{THIS}.WebEnabledJob"}, "sites": []},
            },
        )

        request = make_mocked_request("GET", f"/{THIS}.WebEnabledJob")
        response = await system.webHandler(request)

        assert response.status == 200
        assert response.text == "web_job_response"

    async def test_web_handler_async_response(self, db_params, worker_params):
        """Test webHandler with async web() method."""
        from aiohttp.test_utils import make_mocked_request

        system = JobSystem(
            dsn=db_params,
            **{
                **worker_params,
                "webPort": {"paths": {f"{THIS}.AsyncWebEnabledJob"}, "sites": []},
            },
        )

        request = make_mocked_request("GET", f"/{THIS}.AsyncWebEnabledJob")
        response = await system.webHandler(request)

        assert response.status == 200
        assert response.text == "async_web_job_response"

    async def test_web_handler_invalid_path(self, db_params, worker_params):
        """Test webHandler with a path not in the allowlist."""
        from aiohttp.test_utils import make_mocked_request

        system = JobSystem(
            dsn=db_params,
            **{
                **worker_params,
                "webPort": {"paths": {f"{THIS}.WebEnabledJob"}, "sites": []},
            },
        )

        request = make_mocked_request("GET", "/invalid.path.NotFound")
        response = await system.webHandler(request)

        assert response.status == 200
        assert response.text == "not so fast!"


# ============================================================================
# Test Class Loading Error Handling
# ============================================================================


class TestClassLoadingErrors:
    """Test error handling when loading job classes."""

    async def test_class_not_found_raises_file_not_found(
        self, db_params, worker_params
    ):
        """Loading a non-existent job class raises FileNotFoundError."""
        system = JobSystem(dsn=db_params, **worker_params)

        # Use a real module (asyncio) but fake class name
        with pytest.raises(FileNotFoundError) as excinfo:
            system.classForKlassFromName("asyncio.NonExistentClass")

        assert "Job class not found" in str(excinfo.value)
        assert "asyncio.NonExistentClass" in str(excinfo.value)


# ============================================================================
# Test Web Server Startup
# ============================================================================


class TestWebServerStartup:
    """Test web server startup when webPort is configured."""

    async def test_web_server_tcp_startup(self, live_worker, unique_queue):
        """Test that worker starts a TCP web server when configured."""
        import random

        import aiohttp

        # Choose a random high port to avoid conflicts
        port = random.randint(49152, 65535)

        await live_worker(
            webPort={
                "paths": {f"{THIS}.WebEnabledJob"},
                "sites": [{"host": "127.0.0.1", "port": port}],
            }
        )

        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{port}/{THIS}.WebEnabledJob") as resp,
        ):
            assert resp.status == 200
            text = await resp.text()
            assert text == "web_job_response"

    async def test_web_server_unix_socket_startup(self, live_worker, unique_queue):
        """Test that worker starts a Unix socket web server when configured."""
        import os
        import tempfile

        # Create a temporary file path for the Unix socket
        with tempfile.NamedTemporaryFile(delete=True) as f:
            socket_path = f.name

        system = await live_worker(
            webPort={
                "paths": {f"{THIS}.WebEnabledJob"},
                "sites": [{"path": socket_path}],  # Unix socket path
            }
        )

        # Socket file is created with the worker ID appended
        expected_socket = f"{socket_path}-{system.workerId}"
        try:
            assert os.path.exists(expected_socket), (
                f"Unix socket should have been created at {expected_socket}"
            )
        finally:
            with contextlib.suppress(OSError):
                os.unlink(expected_socket)
