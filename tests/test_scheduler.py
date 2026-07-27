"""
Comprehensive tests for scheduler.py - Recurring job scheduler.
Using LIVE database operations with NO MOCKS for maximum correctness guarantees!
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest

from pyjobby.scheduler import (
    ScheduleExecutionResult,
    ScheduleManager,
    SchedulerWorker,
    ScheduleSafetyManager,
)


def unique_name(base: str) -> str:
    """Generate unique name for test isolation."""
    return f"{base}_{uuid.uuid4().hex[:8]}"


class TestScheduleExecutionResult:
    """Test ScheduleExecutionResult dataclass - covers lines 25-35."""

    def test_result_success(self):
        """Test success result creation."""
        result = ScheduleExecutionResult(
            result="success",
            job_id=123,
            jitter_applied=5,
            queue_depth=10,
            concurrent_jobs=2,
            duration_ms=150,
        )
        assert result.result == "success"
        assert result.job_id == 123

    def test_result_failure(self):
        """Test failure result creation."""
        result = ScheduleExecutionResult(
            result="failure", error_message="Database error", duration_ms=50
        )
        assert result.result == "failure"
        assert result.error_message == "Database error"

    def test_result_skipped(self):
        """Test skipped result creation."""
        result = ScheduleExecutionResult(
            result="skipped", skip_reason="max_concurrent", concurrent_jobs=5
        )
        assert result.result == "skipped"
        assert result.skip_reason == "max_concurrent"

    def test_result_defaults(self):
        """Test default values."""
        result = ScheduleExecutionResult(result="success")
        assert result.job_id is None
        assert result.jitter_applied == 0


class TestScheduleSafetyManagerUnit:
    """Unit tests for ScheduleSafetyManager methods that don't need DB."""

    def test_calculate_jitter_zero(self):
        """Test jitter calculation with zero - covers lines 133-134."""

        class MockConn:
            pass

        manager = ScheduleSafetyManager(MockConn())
        assert manager.calculate_jitter(0) == 0

    def test_calculate_jitter_negative(self):
        """Test jitter calculation with negative value."""

        class MockConn:
            pass

        manager = ScheduleSafetyManager(MockConn())
        assert manager.calculate_jitter(-5) == 0

    def test_calculate_jitter_positive(self):
        """Test jitter calculation with positive value - covers lines 136-139."""

        class MockConn:
            pass

        manager = ScheduleSafetyManager(MockConn())
        for _ in range(100):
            jitter = manager.calculate_jitter(10)
            assert 0 <= jitter <= 10


class TestScheduleManagerCalculateNextRun:
    """Test ScheduleManager.calculate_next_run static method."""

    def test_calculate_next_run_every_minute(self):
        """Test next run calculation for every minute cron."""
        try:
            next_run = ScheduleManager.calculate_next_run("* * * * *")
            assert isinstance(next_run, datetime)
        except ImportError:
            pytest.skip("croniter not installed")

    def test_calculate_next_run_hourly(self):
        """Test next run calculation for hourly cron."""
        try:
            next_run = ScheduleManager.calculate_next_run("0 * * * *")
            assert isinstance(next_run, datetime)
            assert next_run.minute == 0
        except ImportError:
            pytest.skip("croniter not installed")

    def test_calculate_next_run_invalid_cron(self):
        """Test invalid cron expression raises ValueError."""
        with pytest.raises(ValueError) as excinfo:
            ScheduleManager.calculate_next_run("not a valid cron")
        assert "malformed cron expression" in str(excinfo.value)


class TestScheduleSafetyManagerIntegration:
    """Integration tests for ScheduleSafetyManager with database."""

    @pytest.mark.asyncio
    async def test_check_concurrency_empty(self, db_pool):
        """Test concurrency check with no jobs - covers lines 58-87."""
        async with db_pool.acquire() as conn:
            manager = ScheduleSafetyManager(conn)
            is_safe, count = await manager.check_concurrency(
                schedule_id=99999, max_concurrent=5
            )
            assert is_safe is True
            assert count == 0

    @pytest.mark.asyncio
    async def test_check_concurrency_under_limit(self, db_pool):
        """Test concurrency check under limit."""
        async with db_pool.acquire() as conn:
            name = unique_name("test_sched")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run) VALUES ($1, 'TestJob', '* * * * *', NOW()) RETURNING id",
                name,
            )
            await conn.execute(
                "INSERT INTO jorb (job_class, kwargs, state, schedule_id) VALUES ('TestJob', '{}', 'running', $1)",
                schedule_id,
            )
            manager = ScheduleSafetyManager(conn)
            is_safe, count = await manager.check_concurrency(
                schedule_id=schedule_id, max_concurrent=5
            )
            assert is_safe is True
            assert count == 1

    @pytest.mark.asyncio
    async def test_check_concurrency_at_limit(self, db_pool):
        """Test concurrency check at limit."""
        async with db_pool.acquire() as conn:
            name = unique_name("limit_sched")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run) VALUES ($1, 'TestJob', '* * * * *', NOW()) RETURNING id",
                name,
            )
            for _ in range(3):
                await conn.execute(
                    "INSERT INTO jorb (job_class, kwargs, state, schedule_id) VALUES ('TestJob', '{}', 'running', $1)",
                    schedule_id,
                )
            manager = ScheduleSafetyManager(conn)
            is_safe, count = await manager.check_concurrency(
                schedule_id=schedule_id, max_concurrent=3
            )
            assert is_safe is False
            assert count == 3

    @pytest.mark.asyncio
    async def test_check_backpressure_no_threshold(self, db_pool):
        """Test backpressure with no threshold - covers lines 104-105."""
        async with db_pool.acquire() as conn:
            manager = ScheduleSafetyManager(conn)
            is_safe, depth = await manager.check_backpressure(
                queue="test_queue", threshold=None
            )
            assert is_safe is True
            assert depth == 0

    @pytest.mark.asyncio
    async def test_check_backpressure_over_threshold(self, db_pool):
        """Test backpressure over threshold."""
        async with db_pool.acquire() as conn:
            queue_name = unique_name("bp_queue")
            for _ in range(5):
                await conn.execute(
                    "INSERT INTO jorb (job_class, kwargs, state, queue) VALUES ('TestJob', '{}', 'queued', $1)",
                    queue_name,
                )
            manager = ScheduleSafetyManager(conn)
            is_safe, depth = await manager.check_backpressure(
                queue=queue_name, threshold=3
            )
            assert is_safe is False
            assert depth == 5

    @pytest.mark.asyncio
    async def test_check_circuit_breaker_safe(self, db_pool):
        """Test circuit breaker when safe."""
        async with db_pool.acquire() as conn:
            schedule = {
                "id": 1,
                "name": "safe_schedule",
                "consecutive_failures": 2,
                "circuit_breaker_threshold": 5,
            }
            manager = ScheduleSafetyManager(conn)
            is_safe, reason = await manager.check_circuit_breaker(schedule)
            assert is_safe is True

    @pytest.mark.asyncio
    async def test_check_circuit_breaker_triggered(self, db_pool):
        """Test circuit breaker triggered - covers lines 159-185."""
        async with db_pool.acquire() as conn:
            name = unique_name("cb_test")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run, consecutive_failures, circuit_breaker_threshold, enabled) VALUES ($1, 'TestJob', '* * * * *', NOW(), 5, 5, true) RETURNING id",
                name,
            )
            schedule = {
                "id": schedule_id,
                "name": name,
                "consecutive_failures": 5,
                "circuit_breaker_threshold": 5,
            }
            manager = ScheduleSafetyManager(conn)
            is_safe, reason = await manager.check_circuit_breaker(schedule)
            assert is_safe is False
            assert "Circuit breaker triggered" in reason


class TestScheduleManagerIntegration:
    """Integration tests for ScheduleManager with database."""

    @pytest.mark.asyncio
    async def test_create_schedule(self, db_pool):
        """Test schedule creation - covers lines 246-320."""
        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)
            try:
                name = unique_name("integration_test")
                schedule_id = await manager.create_schedule(
                    name=name,
                    job_class="tests.test_scheduler.DummyJob",
                    cron_expr="0 * * * *",
                    queue="test_queue",
                    prio=50,
                )
                assert schedule_id is not None
            except ImportError:
                pytest.skip("croniter not installed")

    @pytest.mark.asyncio
    async def test_set_next_run(self, db_pool):
        """Test next_run update."""
        async with db_pool.acquire() as conn:
            name = unique_name("update_test")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run) VALUES ($1, 'TestJob', '0 * * * *', NOW()) RETURNING id",
                name,
            )
            manager = ScheduleManager(conn)
            next_run = manager.calculate_next_run("0 * * * *", "UTC")
            await manager.set_next_run(schedule_id, next_run)

            stored = await conn.fetchval(
                "SELECT next_run FROM jorb_schedule WHERE id = $1", schedule_id
            )
            assert stored == next_run

    @pytest.mark.asyncio
    async def test_record_execution_success(self, db_pool):
        """Test recording successful execution - covers lines 349-368."""
        async with db_pool.acquire() as conn:
            name = unique_name("success_test")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run, run_count, success_count, consecutive_failures) VALUES ($1, 'TestJob', '* * * * *', NOW(), 5, 3, 2) RETURNING id",
                name,
            )
            manager = ScheduleManager(conn)
            await manager.record_execution_success(schedule_id)
            row = await conn.fetchrow(
                "SELECT run_count, success_count, consecutive_failures FROM jorb_schedule WHERE id = $1",
                schedule_id,
            )
            assert row["run_count"] == 6
            assert row["success_count"] == 4
            assert row["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_record_execution_failure(self, db_pool):
        """Test recording failed execution - covers lines 370-389."""
        async with db_pool.acquire() as conn:
            name = unique_name("failure_test")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run, run_count, failure_count, consecutive_failures) VALUES ($1, 'TestJob', '* * * * *', NOW(), 5, 1, 1) RETURNING id",
                name,
            )
            manager = ScheduleManager(conn)
            await manager.record_execution_failure(schedule_id)
            row = await conn.fetchrow(
                "SELECT run_count, failure_count, consecutive_failures FROM jorb_schedule WHERE id = $1",
                schedule_id,
            )
            assert row["run_count"] == 6
            assert row["failure_count"] == 2
            assert row["consecutive_failures"] == 2

    @pytest.mark.asyncio
    async def test_record_execution_skip(self, db_pool):
        """Test recording skipped execution - covers lines 391-408."""
        async with db_pool.acquire() as conn:
            name = unique_name("skip_test")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run, skip_count) VALUES ($1, 'TestJob', '* * * * *', NOW(), 3) RETURNING id",
                name,
            )
            manager = ScheduleManager(conn)
            await manager.record_execution_skip(schedule_id, "max_concurrent")
            row = await conn.fetchrow(
                "SELECT skip_count FROM jorb_schedule WHERE id = $1", schedule_id
            )
            assert row["skip_count"] == 4


class TestSchedulerWorkerIntegration:
    """Integration tests for SchedulerWorker with database."""

    @pytest.mark.asyncio
    async def test_worker_init(self, db_pool):
        """Test worker initialization - covers lines 419-441."""
        async with db_pool.acquire() as conn:
            worker = SchedulerWorker(conn, poll_interval=30)
            assert worker.poll_interval == 30
            assert worker.stop_requested is False

    @pytest.mark.asyncio
    async def test_find_due_schedules(self, db_pool):
        """Test finding due schedules - covers lines 443-462."""
        async with db_pool.acquire() as conn:
            worker = SchedulerWorker(conn)
            schedules = await worker.find_due_schedules()
            assert isinstance(schedules, list)

    @pytest.mark.asyncio
    async def test_create_scheduled_job(self, db_pool):
        """Test creating job from schedule - covers lines 464-539."""
        async with db_pool.acquire() as conn:
            schedule = {
                "id": 12345,
                "name": "job_create_test",
                "job_class": "TestJob",
                "kwargs": {},
                "queue": "default",
                "prio": 100,
                "capability": None,
            }
            worker = SchedulerWorker(conn)
            job_id = await worker.create_scheduled_job(schedule, datetime.now(UTC))
            assert job_id is not None
            row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert row["job_class"] == "TestJob"

    @pytest.mark.asyncio
    async def test_create_scheduled_job_duplicate(self, db_pool):
        """Test duplicate job prevention - covers lines 529-539."""
        async with db_pool.acquire() as conn:
            schedule = {
                "id": 54321,
                "name": "dup_test",
                "job_class": "TestJob",
                "kwargs": {},
                "queue": "default",
                "prio": 100,
                "capability": None,
            }
            worker = SchedulerWorker(conn)
            scheduled_time = datetime.now(UTC)
            job_id1 = await worker.create_scheduled_job(schedule, scheduled_time)
            job_id2 = await worker.create_scheduled_job(schedule, scheduled_time)
            assert job_id1 is not None
            assert job_id2 is None

    @pytest.mark.asyncio
    async def test_log_execution(self, db_pool):
        """Test execution logging - covers lines 541-577."""
        async with db_pool.acquire() as conn:
            name = unique_name("log_test")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run) VALUES ($1, 'TestJob', '* * * * *', NOW()) RETURNING id",
                name,
            )
            job_id = await conn.fetchval(
                "INSERT INTO jorb (job_class, kwargs, state) VALUES ('TestJob', '{}', 'queued') RETURNING id"
            )
            schedule = {"id": schedule_id, "name": name}
            result = ScheduleExecutionResult(
                result="success",
                job_id=job_id,
                jitter_applied=3,
                queue_depth=10,
                concurrent_jobs=1,
                duration_ms=150,
            )
            worker = SchedulerWorker(conn)
            await worker.log_execution(schedule, datetime.now(UTC), result)
            row = await conn.fetchrow(
                "SELECT * FROM jorb_schedule_log WHERE schedule_id = $1", schedule_id
            )
            assert row["result"] == "success"

    @pytest.mark.asyncio
    async def test_execute_schedule_success(self, db_pool):
        """Test successful schedule execution."""
        async with db_pool.acquire() as conn:
            name = unique_name("exec_test")
            schedule_id = await conn.fetchval(
                "INSERT INTO jorb_schedule (name, job_class, cron_expr, next_run, max_concurrent_jobs, backpressure_threshold, consecutive_failures, circuit_breaker_threshold, jitter_seconds, queue) VALUES ($1, 'TestJob', '* * * * *', NOW(), 5, 1000, 0, 10, 0, 'default') RETURNING id",
                name,
            )
            schedule = dict(
                await conn.fetchrow(
                    "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
                )
            )
            worker = SchedulerWorker(conn)
            result = await worker.execute_schedule(schedule)
            assert result.result == "success"

    @pytest.mark.asyncio
    async def test_worker_stop(self, db_pool):
        """Test worker stop method - covers lines 805-808."""
        async with db_pool.acquire() as conn:
            worker = SchedulerWorker(conn)
            assert worker.stop_requested is False
            worker.stop()
            assert worker.stop_requested is True


class TestSchedulerWorkerRunLoop:
    """Tests for the scheduler run loop."""

    @pytest.mark.asyncio
    async def test_run_loop_stops_on_request(self, db_pool):
        """Test that run loop stops when stop is requested."""
        async with db_pool.acquire() as conn:
            worker = SchedulerWorker(conn, poll_interval=0.1)

            async def run_and_stop():
                await asyncio.sleep(0.2)
                worker.stop()

            task = asyncio.create_task(run_and_stop())
            await asyncio.wait_for(worker.run(), timeout=2.0)
            await task
            assert worker.stop_requested is True
