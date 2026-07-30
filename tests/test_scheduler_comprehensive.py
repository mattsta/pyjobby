"""
Comprehensive tests for Scheduler module.

Tests all aspects of recurring job scheduling:
- Schedule creation and management
- Cron expression parsing and next run calculation
- Safety checks (concurrency, backpressure, circuit breaker, jitter)
- Schedule execution and job creation
- Execution logging and metrics
- Error handling and failure scenarios
- Integration with live database
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from pyjobby.scheduler import (
    ScheduleExecutionResult,
    ScheduleManager,
    SchedulerWorker,
    ScheduleSafetyManager,
)

# ============================================================================
# Test ScheduleExecutionResult
# ============================================================================


class TestScheduleExecutionResult:
    """Test ScheduleExecutionResult dataclass."""

    def test_success_result(self):
        """Test creating success result."""
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
        assert result.jitter_applied == 5
        assert result.queue_depth == 10
        assert result.concurrent_jobs == 2
        assert result.duration_ms == 150
        assert result.skip_reason is None
        assert result.error_message is None

    def test_failure_result(self):
        """Test creating failure result."""
        result = ScheduleExecutionResult(
            result="failure", error_message="Database connection failed", duration_ms=50
        )

        assert result.result == "failure"
        assert result.error_message == "Database connection failed"
        assert result.duration_ms == 50
        assert result.job_id is None

    def test_skipped_result(self):
        """Test creating skipped result."""
        result = ScheduleExecutionResult(
            result="skipped", skip_reason="max_concurrent", concurrent_jobs=5
        )

        assert result.result == "skipped"
        assert result.skip_reason == "max_concurrent"
        assert result.concurrent_jobs == 5


# ============================================================================
# Test ScheduleSafetyManager
# ============================================================================


class TestScheduleSafetyManager:
    """Test ScheduleSafetyManager safety checks."""

    @pytest_asyncio.fixture
    async def safety_manager(self, db_pool):
        """Create safety manager for testing."""
        async with db_pool.acquire() as conn:
            yield ScheduleSafetyManager(conn)

    @pytest.mark.asyncio
    async def test_check_concurrency_below_limit(self, db_pool, client):
        """Test concurrency check when below limit."""
        # Create schedule
        schedule_id = await self._create_test_schedule(db_pool, "test-schedule")

        # Create 2 jobs linked to this schedule
        await self._create_scheduled_job(client, schedule_id, state="running")
        await self._create_scheduled_job(client, schedule_id, state="queued")

        # Create safety manager with fresh connection AFTER jobs are created
        async with db_pool.acquire() as conn:
            safety_manager = ScheduleSafetyManager(conn)

            # Check concurrency with limit of 5
            is_safe, count = await safety_manager.check_concurrency(schedule_id, 5)

            assert is_safe is True
            assert count == 2

    @pytest.mark.asyncio
    async def test_check_concurrency_at_limit(self, db_pool, client):
        """Test concurrency check when at limit."""
        schedule_id = await self._create_test_schedule(db_pool, "test-schedule")

        # Create 3 jobs (limit is 3)
        for _ in range(3):
            await self._create_scheduled_job(client, schedule_id, state="running")

        async with db_pool.acquire() as conn:
            safety_manager = ScheduleSafetyManager(conn)

            is_safe, count = await safety_manager.check_concurrency(schedule_id, 3)

            assert is_safe is False
            assert count == 3

    @pytest.mark.asyncio
    async def test_check_concurrency_finished_jobs_not_counted(self, db_pool, client):
        """Test that finished jobs don't count toward concurrency limit."""
        schedule_id = await self._create_test_schedule(db_pool, "test-schedule")

        # Create 2 running and 3 finished jobs
        await self._create_scheduled_job(client, schedule_id, state="running")
        await self._create_scheduled_job(client, schedule_id, state="running")
        await self._create_scheduled_job(client, schedule_id, state="finished")
        await self._create_scheduled_job(client, schedule_id, state="finished")
        await self._create_scheduled_job(client, schedule_id, state="finished")

        async with db_pool.acquire() as conn:
            safety_manager = ScheduleSafetyManager(conn)

            is_safe, count = await safety_manager.check_concurrency(schedule_id, 5)

            assert is_safe is True
            assert count == 2  # Only running jobs counted

    @pytest.mark.asyncio
    async def test_check_backpressure_below_threshold(
        self, db_pool, safety_manager, client
    ):
        """Test backpressure check when queue is below threshold."""
        # Use unique queue name
        queue_name = f"test_queue_{uuid.uuid4().hex[:8]}"

        # Create 5 jobs
        for _ in range(5):
            await client.enqueue("test.Job", queue=queue_name)

        is_safe, depth = await safety_manager.check_backpressure(queue_name, 10)

        assert is_safe is True
        assert depth == 5

    @pytest.mark.asyncio
    async def test_check_backpressure_at_threshold(
        self, db_pool, safety_manager, client
    ):
        """Test backpressure check when queue is at threshold."""
        # Use unique queue name
        queue_name = f"test_queue_{uuid.uuid4().hex[:8]}"

        # Create 10 jobs (threshold is 10)
        for _ in range(10):
            await client.enqueue("test.Job", queue=queue_name)

        is_safe, depth = await safety_manager.check_backpressure(queue_name, 10)

        assert is_safe is False
        assert depth == 10

    @pytest.mark.asyncio
    async def test_check_backpressure_no_threshold(
        self, db_pool, safety_manager, client
    ):
        """Test backpressure check when threshold is None (unlimited)."""
        # Create 100 jobs
        for _ in range(100):
            await client.enqueue("test.Job", queue="test_queue")

        is_safe, depth = await safety_manager.check_backpressure("test_queue", None)

        assert is_safe is True
        assert depth == 0  # Returns 0 when no threshold

    @pytest.mark.asyncio
    async def test_check_backpressure_finished_jobs_not_counted(
        self, db_pool, safety_manager, client
    ):
        """Test that finished jobs don't count toward backpressure."""
        # Use unique queue name
        queue_name = f"test_queue_{uuid.uuid4().hex[:8]}"

        # Create 3 queued, 2 running, 5 finished
        for i in range(3):
            job_id = await client.enqueue("test.Job", queue=queue_name)

        for i in range(2):
            job_id = await client.enqueue("test.Job", queue=queue_name)
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE jorb SET state = 'running' WHERE id = $1", job_id
                )

        for i in range(5):
            job_id = await client.enqueue("test.Job", queue=queue_name)
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
                )

        is_safe, depth = await safety_manager.check_backpressure(queue_name, 10)

        assert is_safe is True
        assert depth == 5  # Only queued + running

    @pytest.mark.asyncio
    async def test_calculate_jitter_zero(self, safety_manager):
        """Test jitter calculation with 0 seconds."""
        jitter = safety_manager.calculate_jitter(0)
        assert jitter == 0

    @pytest.mark.asyncio
    async def test_calculate_jitter_positive(self, safety_manager):
        """Test jitter calculation with positive value."""
        jitter = safety_manager.calculate_jitter(10)
        assert 0 <= jitter <= 10

    @pytest.mark.asyncio
    async def test_calculate_jitter_distribution(self, safety_manager):
        """Test that jitter is randomly distributed."""
        jitters = [safety_manager.calculate_jitter(100) for _ in range(50)]

        # All should be in valid range
        assert all(0 <= j <= 100 for j in jitters)

        # Should have some variety (not all the same)
        assert len(set(jitters)) > 10

    @pytest.mark.asyncio
    async def test_check_circuit_breaker_below_threshold(self, db_pool, safety_manager):
        """Test circuit breaker when below failure threshold."""
        schedule_id = await self._create_test_schedule(db_pool, "test-schedule")

        # Create schedule with 2 consecutive failures (threshold is 5)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jorb_schedule
                SET consecutive_failures = 2,
                    circuit_breaker_threshold = 5
                WHERE id = $1
            """,
                schedule_id,
            )

            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
            schedule_dict = dict(schedule)

        is_safe, reason = await safety_manager.check_circuit_breaker(schedule_dict)

        assert is_safe is True
        assert reason == ""

    @pytest.mark.asyncio
    async def test_check_circuit_breaker_at_threshold(self, db_pool, safety_manager):
        """Test circuit breaker when at failure threshold (triggers)."""
        schedule_id = await self._create_test_schedule(db_pool, "test-schedule")

        # Create schedule with 5 consecutive failures (threshold is 5)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jorb_schedule
                SET consecutive_failures = 5,
                    circuit_breaker_threshold = 5
                WHERE id = $1
            """,
                schedule_id,
            )

            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
            schedule_dict = dict(schedule)

        is_safe, reason = await safety_manager.check_circuit_breaker(schedule_dict)

        assert is_safe is False
        assert "Circuit breaker triggered" in reason
        assert "5 consecutive failures" in reason

        # Verify schedule was disabled
        async with db_pool.acquire() as conn:
            enabled = await conn.fetchval(
                "SELECT enabled FROM jorb_schedule WHERE id = $1", schedule_id
            )
            assert enabled is False

    # Helper methods
    async def _create_test_schedule(self, db_pool, name):
        """Create a test schedule with unique name."""
        unique_name = f"{name}-{uuid.uuid4().hex[:8]}"
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio,
                    max_concurrent_jobs, circuit_breaker_threshold,
                    next_run
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            """,
                unique_name,
                "test.Job",
                "* * * * *",
                "default",
                100,
                10,
                5,
                datetime.now(UTC),
            )
            return schedule_id

    async def _create_scheduled_job(self, client, schedule_id, state="queued"):
        """Create a job linked to a schedule.

        The link is jorb.schedule_id, the column the scheduler itself writes;
        client.enqueue() has no parameter for it because a client never
        creates a schedule's job.
        """
        job_id = await client.enqueue("test.Job")

        async with client.pool.acquire() as conn:
            await conn.execute(
                "UPDATE jorb SET state = $1, schedule_id = $3 WHERE id = $2",
                state,
                job_id,
                schedule_id,
            )

        return job_id


# ============================================================================
# Test ScheduleManager
# ============================================================================


class TestScheduleManager:
    """Test ScheduleManager business logic."""

    @pytest_asyncio.fixture
    async def manager(self, db_pool):
        """Create schedule manager for testing."""
        async with db_pool.acquire() as conn:
            yield ScheduleManager(conn)

    def test_calculate_next_run_every_minute(self):
        """Test calculating next run for every minute cron."""
        next_run = ScheduleManager.calculate_next_run("* * * * *", "UTC")

        now = datetime.now(UTC)

        # Next run should be within next 2 minutes
        assert next_run > now
        assert next_run < now + timedelta(minutes=2)

    def test_calculate_next_run_daily_at_midnight(self):
        """Test calculating next run for daily at midnight."""
        next_run = ScheduleManager.calculate_next_run("0 0 * * *", "UTC")

        now = datetime.now(UTC)

        # Should be midnight
        assert next_run.hour == 0
        assert next_run.minute == 0

        # Should be in the future
        assert next_run > now

    def test_calculate_next_run_with_timezone(self):
        """Test calculating next run with non-UTC timezone."""
        next_run = ScheduleManager.calculate_next_run("0 12 * * *", "America/New_York")

        # Should have timezone info
        assert next_run.tzinfo is not None

        # Should be noon in New York time
        assert next_run.hour == 12

    def test_calculate_next_run_invalid_cron(self):
        """Test that invalid cron expression raises ValueError."""
        with pytest.raises(ValueError, match="malformed cron expression"):
            ScheduleManager.calculate_next_run("invalid cron", "UTC")

    @pytest.mark.asyncio
    async def test_create_schedule(self, db_pool, manager):
        """Test creating a new schedule."""
        schedule_id = await manager.create_schedule(
            name="test-schedule",
            job_class="test.Job",
            cron_expr="*/5 * * * *",
            queue="test_queue",
            priority=200,
            kwargs={"key": "value"},
        )

        assert schedule_id is not None

        # Verify schedule was created
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            assert schedule["name"] == "test-schedule"
            assert schedule["job_class"] == "test.Job"
            assert schedule["cron_expr"] == "*/5 * * * *"
            assert schedule["queue"] == "test_queue"
            assert schedule["prio"] == 200
            assert schedule["enabled"] is True
            assert schedule["next_run"] is not None

    @pytest.mark.asyncio
    async def test_create_schedule_with_all_options(self, db_pool, manager):
        """Test creating schedule with all optional parameters."""
        schedule_id = await manager.create_schedule(
            name="full-schedule",
            job_class="test.FullJob",
            cron_expr="0 * * * *",
            description="Test schedule with all options",
            queue="custom_queue",
            priority=500,
            capability="special",
            timezone="America/Los_Angeles",
            enabled=False,
            max_concurrent_jobs=3,
            jitter_seconds=30,
            backpressure_threshold=500,
            circuit_breaker_threshold=10,
            kwargs={"param1": "value1", "param2": 42},
            created_by="test_user",
        )

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            assert schedule["description"] == "Test schedule with all options"
            assert schedule["capability"] == "special"
            assert schedule["timezone"] == "America/Los_Angeles"
            assert schedule["enabled"] is False
            assert schedule["max_concurrent_jobs"] == 3
            assert schedule["jitter_seconds"] == 30
            assert schedule["backpressure_threshold"] == 500
            assert schedule["circuit_breaker_threshold"] == 10
            assert schedule["created_by"] == "test_user"

    @pytest.mark.asyncio
    async def test_set_next_run(self, db_pool, manager):
        """Test updating schedule's next_run timestamp.

        The claim is that set_next_run WRITES, so the target instant is a
        fixed literal rather than another cron evaluation: the original
        form compared "next midnight" against "next minute", which are THE
        SAME instant during the 23:59 UTC minute -- a test that failed one
        minute per day, only for whoever happened to run it then.
        """
        schedule_id = await manager.create_schedule(
            name="update-test", job_class="test.Job", cron_expr="0 0 * * *"
        )

        target = datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC)
        await manager.set_next_run(schedule_id, target)

        async with db_pool.acquire() as conn:
            new_next_run = await conn.fetchval(
                "SELECT next_run FROM jorb_schedule WHERE id = $1", schedule_id
            )

        assert new_next_run == target

    @pytest.mark.asyncio
    async def test_record_execution_success(self, db_pool, manager):
        """Test recording successful execution."""
        schedule_id = await manager.create_schedule(
            name="success-test", job_class="test.Job", cron_expr="* * * * *"
        )

        # Record success
        await manager.record_execution_success(schedule_id)

        # Verify counters
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            assert schedule["run_count"] == 1
            assert schedule["success_count"] == 1
            assert schedule["consecutive_failures"] == 0
            assert schedule["last_run"] is not None
            assert schedule["last_success"] is not None

    @pytest.mark.asyncio
    async def test_record_execution_failure(self, db_pool, manager):
        """Test recording failed execution."""
        schedule_id = await manager.create_schedule(
            name="failure-test", job_class="test.Job", cron_expr="* * * * *"
        )

        # Record failure
        await manager.record_execution_failure(schedule_id)

        # Verify counters
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            assert schedule["run_count"] == 1
            assert schedule["failure_count"] == 1
            assert schedule["consecutive_failures"] == 1
            assert schedule["last_run"] is not None
            assert schedule["last_failure"] is not None

    @pytest.mark.asyncio
    async def test_record_consecutive_failures(self, db_pool, manager):
        """Test that consecutive failures increment correctly."""
        schedule_id = await manager.create_schedule(
            name="consecutive-failures", job_class="test.Job", cron_expr="* * * * *"
        )

        # Record 3 failures
        for _ in range(3):
            await manager.record_execution_failure(schedule_id)

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            assert schedule["consecutive_failures"] == 3
            assert schedule["failure_count"] == 3

    @pytest.mark.asyncio
    async def test_record_success_resets_consecutive_failures(self, db_pool, manager):
        """Test that success resets consecutive failures counter."""
        schedule_id = await manager.create_schedule(
            name="reset-failures", job_class="test.Job", cron_expr="* * * * *"
        )

        # Record 3 failures then 1 success
        for _ in range(3):
            await manager.record_execution_failure(schedule_id)

        await manager.record_execution_success(schedule_id)

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            assert schedule["consecutive_failures"] == 0
            assert schedule["failure_count"] == 3
            assert schedule["success_count"] == 1

    @pytest.mark.asyncio
    async def test_record_execution_skip(self, db_pool, manager):
        """Test recording skipped execution."""
        schedule_id = await manager.create_schedule(
            name="skip-test", job_class="test.Job", cron_expr="* * * * *"
        )

        # Record skip
        await manager.record_execution_skip(schedule_id, "max_concurrent")

        # Verify counters
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            assert schedule["skip_count"] == 1


# ============================================================================
# Test SchedulerWorker
# ============================================================================


class TestSchedulerWorker:
    """Test SchedulerWorker execution logic."""

    @pytest_asyncio.fixture
    async def worker(self, db_pool):
        """Create scheduler worker for testing."""
        async with db_pool.acquire() as conn:
            yield SchedulerWorker(conn, poll_interval=1)

    @pytest.mark.asyncio
    async def test_find_due_schedules_none_due(self, db_pool, worker):
        """Test finding schedules when none are due."""
        # Create schedule that runs in the future
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                "future-schedule",
                "test.Job",
                "* * * * *",
                datetime.now(UTC) + timedelta(hours=1),
                True,
            )

        schedules = await worker.find_due_schedules()

        assert len(schedules) == 0

    @pytest.mark.asyncio
    async def test_find_due_schedules_one_due(self, db_pool, worker):
        """Test finding schedules when one is due."""
        # Create schedule that is due now
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                "due-schedule",
                "test.Job",
                "* * * * *",
                datetime.now(UTC) - timedelta(minutes=1),
                True,
            )

        schedules = await worker.find_due_schedules()

        assert len(schedules) == 1
        assert schedules[0]["name"] == "due-schedule"

    @pytest.mark.asyncio
    async def test_find_due_schedules_disabled_not_returned(self, db_pool, worker):
        """Test that disabled schedules are not returned."""
        # Create disabled schedule that is due
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
            """,
                "disabled-schedule",
                "test.Job",
                "* * * * *",
                datetime.now(UTC) - timedelta(minutes=1),
                False,
            )

        schedules = await worker.find_due_schedules()

        assert len(schedules) == 0

    @pytest.mark.asyncio
    async def test_create_scheduled_job(self, db_pool, worker):
        """Test creating a job from a schedule."""
        # Create schedule
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    queue, prio, kwargs
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
            """,
                "test-schedule",
                "test.TestJob",
                "* * * * *",
                datetime.now(UTC),
                True,
                "test_queue",
                500,
                {"key": "value"},
            )

            schedule_dict = dict(schedule)

        scheduled_time = datetime.now(UTC)
        job_id = await worker.create_scheduled_job(schedule_dict, scheduled_time)

        assert job_id is not None

        # Verify job was created
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job["job_class"] == "test.TestJob"
            assert job["queue"] == "test_queue"
            assert job["prio"] == 500
            assert job["state"] == "queued"
            assert job["schedule_id"] == schedule_dict["id"]

    @pytest.mark.asyncio
    async def test_create_scheduled_job_duplicate_prevention(self, db_pool, worker):
        """Test that duplicate jobs are prevented by deadline_key."""
        # Create schedule
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
                RETURNING *
            """,
                "dup-test",
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
            )

            schedule_dict = dict(schedule)

        scheduled_time = datetime.now(UTC)

        # Create first job
        job_id1 = await worker.create_scheduled_job(schedule_dict, scheduled_time)
        assert job_id1 is not None

        # Try to create duplicate (same schedule_id + scheduled_time)
        job_id2 = await worker.create_scheduled_job(schedule_dict, scheduled_time)
        assert job_id2 is None  # Duplicate prevented

    @pytest.mark.asyncio
    async def test_log_execution(self, db_pool, worker, client):
        """Test logging execution to jorb_schedule_log."""
        # Create schedule
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
                RETURNING *
            """,
                f"log-test-{uuid.uuid4().hex[:8]}",
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
            )

            schedule_dict = dict(schedule)

        # Create a real job for the log entry
        job_id = await client.enqueue("test.Job")

        scheduled_time = datetime.now(UTC)
        result = ScheduleExecutionResult(
            result="success",
            job_id=job_id,
            jitter_applied=5,
            queue_depth=10,
            concurrent_jobs=2,
            duration_ms=150,
        )

        await worker.log_execution(schedule_dict, scheduled_time, result)

        # Verify log entry
        async with db_pool.acquire() as conn:
            log = await conn.fetchrow(
                """
                SELECT * FROM jorb_schedule_log
                WHERE schedule_id = $1
            """,
                schedule_dict["id"],
            )

            assert log["result"] == "success"
            assert log["job_id"] == job_id
            assert log["jitter_applied_seconds"] == 5
            assert log["queue_depth_at_run"] == 10
            assert log["concurrent_jobs_at_run"] == 2
            assert log["duration_ms"] == 150

    @pytest.mark.asyncio
    async def test_execute_schedule_success(self, db_pool, worker):
        """Test successful schedule execution."""
        # Create schedule
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    max_concurrent_jobs, jitter_seconds,
                    backpressure_threshold, circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
            """,
                "exec-success",
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
                10,
                0,
                1000,
                5,
            )

            schedule_dict = dict(schedule)

        result = await worker.execute_schedule(schedule_dict)

        assert result.result == "success"
        assert result.job_id is not None

    @pytest.mark.asyncio
    async def test_execute_schedule_circuit_breaker(self, db_pool, worker):
        """Test schedule execution with circuit breaker triggered."""
        # Create schedule with failures at threshold
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    consecutive_failures, circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
            """,
                "circuit-breaker",
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
                5,
                5,
            )

            schedule_dict = dict(schedule)

        result = await worker.execute_schedule(schedule_dict)

        assert result.result == "skipped"
        assert result.skip_reason == "circuit_breaker"

    @pytest.mark.asyncio
    async def test_execute_schedule_max_concurrent(self, db_pool, worker, client):
        """Test schedule execution with max concurrent reached."""
        # Create schedule with max_concurrent = 2
        schedule_name = f"max-concurrent-{uuid.uuid4().hex[:8]}"

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    max_concurrent_jobs, circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
            """,
                schedule_name,
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
                2,
                5,
            )

            schedule_dict = dict(schedule)

        # Create 2 jobs already running for this schedule
        for _ in range(2):
            job_id = await client.enqueue("test.Job")
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE jorb SET state = 'running', schedule_id = $2 WHERE id = $1",
                    job_id,
                    schedule_dict["id"],
                )

        result = await worker.execute_schedule(schedule_dict)

        assert result.result == "skipped"
        assert result.skip_reason == "max_concurrent"

    @pytest.mark.asyncio
    async def test_execute_schedule_backpressure(self, db_pool, worker, client):
        """Test schedule execution with backpressure."""
        # Use unique queue and schedule name
        queue_name = f"test_queue_{uuid.uuid4().hex[:8]}"
        schedule_name = f"backpressure-{uuid.uuid4().hex[:8]}"

        # Create schedule with backpressure_threshold = 5
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    queue, backpressure_threshold, circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
            """,
                schedule_name,
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
                queue_name,
                5,
                5,
            )

            schedule_dict = dict(schedule)

        # Fill queue with 5 jobs (at threshold)
        for _ in range(5):
            await client.enqueue("test.Job", queue=queue_name)

        result = await worker.execute_schedule(schedule_dict)

        assert result.result == "skipped"
        assert result.skip_reason == "backpressure"

    @pytest.mark.asyncio
    async def test_stop_graceful_shutdown(self, worker):
        """Test graceful shutdown of scheduler worker."""
        assert worker.stop_requested is False

        worker.stop()

        assert worker.stop_requested is True

    @pytest.mark.asyncio
    async def test_metrics_tracking(self, db_pool, worker):
        """Test that worker tracks execution metrics."""
        assert worker.executions_total == 0
        assert worker.successes_total == 0
        assert worker.failures_total == 0
        assert worker.skips_total == 0

        # Create schedule for successful execution
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
            """,
                "metrics-test",
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
                5,
            )

            schedule_dict = dict(schedule)

        # Execute schedule (should succeed)
        result = await worker.execute_schedule(schedule_dict)

        # Manually update metrics (normally done in run() loop)
        worker.executions_total += 1
        if result.result == "success":
            worker.successes_total += 1
        elif result.result == "failure":
            worker.failures_total += 1
        elif result.result == "skipped":
            worker.skips_total += 1

        assert worker.executions_total == 1
        assert worker.successes_total == 1

    @pytest.mark.asyncio
    async def test_execute_schedule_with_jitter(self, db_pool, worker, client):
        """Jitter offsets the created job's run_after, never by sleeping.

        Schema v1 applies jitter to when the job may START; the scheduler
        loop itself must not block (one jittery schedule would otherwise
        stall every schedule behind it)."""
        # Create schedule with jitter_seconds > 0
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    queue, prio, kwargs, jitter_seconds
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
            """,
                "jitter-schedule",
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
                "default",
                100,
                {},
                5,
            )

            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

        schedule_dict = dict(schedule)
        scheduled_time = schedule_dict["next_run"]

        import time

        start_time = time.time()
        result = await worker.execute_schedule(schedule_dict)
        elapsed = time.time() - start_time

        assert result.result == "success"
        assert result.job_id is not None
        assert 0 <= result.jitter_applied <= 5

        # The scheduler did NOT sleep out the jitter
        assert elapsed < 1.0, f"execute_schedule slept {elapsed:.2f}s applying jitter"

        # The jitter landed on the job's run_after instead
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", result.job_id)
            assert job is not None
            assert job["job_class"] == "test.Job"
            assert job["run_after"] == scheduled_time + timedelta(
                seconds=result.jitter_applied
            )

    @pytest.mark.asyncio
    async def test_execute_schedule_duplicate_with_transaction_error(
        self, db_pool, worker
    ):
        """Test execute_schedule handles InFailedSQLTransactionError - covers lines 707-717."""
        # Create schedule - scheduler will generate deadline_key from schedule_id and scheduled_time
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    queue, prio, kwargs
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            """,
                "duplicate-schedule",
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
                "default",
                100,
                {},
            )

            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            # Pre-create a job carrying the exact deadline_key the scheduler
            # will generate for this run (schedule:{id}:{next_run.isoformat()})
            scheduled_time = schedule["next_run"]
            deadline_key = f"schedule:{schedule_id}:{scheduled_time.isoformat()}"

            await conn.execute(
                """
                INSERT INTO jorb (
                    job_class, kwargs, queue, state, prio, deadline_key
                )
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "test.Job",
                {},
                "default",
                "queued",
                100,
                deadline_key,
            )

        schedule_dict = dict(schedule)

        # The queued row already owns this deadline_key, so the INSERT hits the
        # jorb_deadline_idx unique index and the run is skipped as a duplicate
        result = await worker.execute_schedule(schedule_dict)

        assert result.result == "skipped"
        assert result.skip_reason == "duplicate"
        assert result.job_id is None

        # ...and no second job was created for this schedule run
        async with db_pool.acquire() as conn:
            job_count = await conn.fetchval(
                "SELECT count(*) FROM jorb WHERE deadline_key = $1", deadline_key
            )
        assert job_count == 1

    @pytest.mark.asyncio
    async def test_execute_schedule_with_exception(self, db_pool, worker, monkeypatch):
        """Test execute_schedule handles exceptions during job creation - covers lines 719-735."""
        # Create schedule
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    queue, prio, kwargs
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            """,
                "exception-schedule",
                "test.Job",
                "* * * * *",
                datetime.now(UTC),
                True,
                "default",
                100,
                {},
            )

            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

        schedule_dict = dict(schedule)

        # Monkeypatch create_scheduled_job to raise an exception
        async def mock_create_job(*args, **kwargs):
            raise RuntimeError("Simulated job creation failure")

        monkeypatch.setattr(worker, "create_scheduled_job", mock_create_job)

        # Execute schedule (should catch exception and return failure)
        result = await worker.execute_schedule(schedule_dict)

        assert result.result == "failure"
        assert result.error_message == "Simulated job creation failure"
        assert result.duration_ms is not None
        assert result.job_id is None

        # Verify failure was recorded
        async with db_pool.acquire() as conn:
            schedule_after = await conn.fetchrow(
                "SELECT failure_count, last_failure FROM jorb_schedule WHERE id = $1",
                schedule_id,
            )

            assert schedule_after["failure_count"] == 1
            assert schedule_after["last_failure"] is not None


# ============================================================================
# Integration Tests
# ============================================================================


class TestSchedulerIntegration:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_schedule_lifecycle(self, db_pool):
        """Test complete schedule lifecycle from creation to execution."""
        async with db_pool.acquire() as conn:
            # 1. Create schedule
            manager = ScheduleManager(conn)
            schedule_id = await manager.create_schedule(
                name="lifecycle-test",
                job_class="test.LifecycleJob",
                cron_expr="* * * * *",
                queue="test_queue",
                kwargs={"param": "value"},
            )

            # 2. Set next_run to past (make it due)
            await conn.execute(
                """
                UPDATE jorb_schedule
                SET next_run = $1
                WHERE id = $2
            """,
                datetime.now(UTC) - timedelta(minutes=1),
                schedule_id,
            )

            # 3. Execute schedule
            worker = SchedulerWorker(conn)
            schedules = await worker.find_due_schedules()

            assert len(schedules) == 1

            result = await worker.execute_schedule(schedules[0])

            assert result.result == "success"
            assert result.job_id is not None

            # 4. Verify job was created
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", result.job_id)

            assert job["job_class"] == "test.LifecycleJob"
            assert job["queue"] == "test_queue"
            assert job["schedule_id"] == schedule_id

            # 5. Verify schedule counters updated
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            assert schedule["run_count"] == 1
            assert schedule["success_count"] == 1

    @pytest.mark.asyncio
    async def test_multiple_schedules_parallel_execution(self, db_pool):
        """Test executing multiple schedules in parallel."""
        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)

            # Create 5 schedules all due now
            schedule_ids = []
            for i in range(5):
                schedule_id = await manager.create_schedule(
                    name=f"parallel-{i}",
                    job_class=f"test.ParallelJob{i}",
                    cron_expr="* * * * *",
                )
                schedule_ids.append(schedule_id)

                # Make them all due
                await conn.execute(
                    """
                    UPDATE jorb_schedule
                    SET next_run = $1
                    WHERE id = $2
                """,
                    datetime.now(UTC) - timedelta(minutes=1),
                    schedule_id,
                )

            # Find and execute all
            worker = SchedulerWorker(conn)
            schedules = await worker.find_due_schedules()

            assert len(schedules) == 5

            # Execute all schedules
            job_ids = []
            for schedule in schedules:
                result = await worker.execute_schedule(schedule)
                assert result.result == "success"
                job_ids.append(result.job_id)

            # Verify all jobs created
            assert len(set(job_ids)) == 5  # All unique

    @pytest.mark.asyncio
    async def test_schedule_with_timezone_conversion(self, db_pool):
        """Test schedule execution with timezone-aware timestamps."""
        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)

            # Create schedule in New York timezone
            schedule_id = await manager.create_schedule(
                name="timezone-test",
                job_class="test.TimezoneJob",
                cron_expr="0 12 * * *",  # Noon
                timezone="America/New_York",
            )

            # Get the schedule
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            # Verify next_run has timezone info
            assert schedule["next_run"] is not None
            assert schedule["timezone"] == "America/New_York"


# =============================================================================
# Edge Cases and Integration Tests for Remaining Coverage
# =============================================================================


@pytest.mark.asyncio
class TestSchedulerEdgeCases:
    """Tests for edge cases and remaining uncovered code paths."""

    async def test_duplicate_skip_with_infailed_transaction_error(
        self, db_pool, client
    ):
        """Test InFailedSQLTransactionError handling in duplicate skip - covers lines 707-714."""
        from unittest.mock import patch

        import asyncpg

        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)

            # Create schedule
            schedule_id = await manager.create_schedule(
                name="duplicate-skip-test",
                job_class="test.DuplicateJob",
                cron_expr="* * * * *",
            )

            # Make it due
            await conn.execute(
                """
                UPDATE jorb_schedule
                SET next_run = $1
                WHERE id = $2
            """,
                datetime.now(UTC) - timedelta(minutes=1),
                schedule_id,
            )

            # Get the schedule
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            worker = SchedulerWorker(conn)

            # Pre-create job with deadline_key to trigger duplicate
            scheduled_time = schedule["next_run"]
            deadline_key = f"schedule:{schedule_id}:{scheduled_time.isoformat()}"

            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, deadline_key)
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "test.DuplicateJob",
                {},
                "default",
                "queued",
                100,
                deadline_key,
            )

            # Mock record_execution_skip to raise InFailedSQLTransactionError
            original_record = worker.manager.record_execution_skip
            call_count = [0]

            async def mock_record_skip(*args, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    # First call raises InFailedSQLTransactionError
                    raise asyncpg.InFailedSQLTransactionError("Transaction aborted")
                return await original_record(*args, **kwargs)

            with patch.object(
                worker.manager, "record_execution_skip", side_effect=mock_record_skip
            ):
                # Execute schedule - should handle duplicate and InFailedSQLTransactionError
                result = await worker.execute_schedule(dict(schedule))

                # Should return skipped with duplicate reason
                assert result.result == "skipped"
                assert result.skip_reason == "duplicate"

                # Verify the exception was caught and handled gracefully
                assert call_count[0] == 1  # Mock was called once


@pytest.mark.asyncio
class TestSchedulerMainLoop:
    """Integration tests for the main scheduler run() loop - covers lines 743-803."""

    async def test_run_loop_executes_due_schedules(self, db_pool):
        """Test run() loop finds and executes due schedules - covers main loop lines."""
        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)

            # Create schedule due now
            schedule_id = await manager.create_schedule(
                name="loop-test", job_class="test.LoopJob", cron_expr="* * * * *"
            )

            # Make it due
            await conn.execute(
                """
                UPDATE jorb_schedule
                SET next_run = $1
                WHERE id = $2
            """,
                datetime.now(UTC) - timedelta(minutes=1),
                schedule_id,
            )

            # Create worker with short poll interval
            worker = SchedulerWorker(conn, poll_interval=0.1)

            # Run scheduler in background for a short time
            import asyncio

            run_task = asyncio.create_task(worker.run())

            # Let it run for 0.5 seconds (enough for 1-2 poll cycles)
            await asyncio.sleep(0.5)

            # Stop the worker
            worker.stop()

            # Wait for it to finish
            try:
                await asyncio.wait_for(run_task, timeout=2.0)
            except TimeoutError:
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task

            # Verify schedule was executed
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
            assert schedule["run_count"] >= 1  # Should have run at least once

            # Verify job was created
            job = await conn.fetchrow("""
                SELECT * FROM jorb
                WHERE job_class = 'test.LoopJob'
                ORDER BY created DESC
                LIMIT 1
            """)
            assert job is not None

            # Verify metrics were tracked
            assert worker.executions_total >= 1
            assert worker.successes_total >= 1

    async def test_concurrent_schedulers_run_a_due_schedule_once(self, db_pool):
        """Two scheduler instances must not double-fire one due schedule.

        run() re-locks each due row inside a transaction with FOR UPDATE
        SKIP LOCKED and re-checks due-ness, so the loser skips the row; the
        deadline_key unique index is the belt-and-braces behind that."""
        import asyncio

        job_class = f"test.Concurrent_{uuid.uuid4().hex[:8]}"

        async with db_pool.acquire() as conn_a, db_pool.acquire() as conn_b:
            manager = ScheduleManager(conn_a)
            schedule_id = await manager.create_schedule(
                name=f"concurrent-loop-{uuid.uuid4().hex[:8]}",
                job_class=job_class,
                cron_expr="* * * * *",
            )
            await conn_a.execute(
                "UPDATE jorb_schedule SET next_run = $1 WHERE id = $2",
                datetime.now(UTC) - timedelta(minutes=1),
                schedule_id,
            )

            worker_a = SchedulerWorker(conn_a, poll_interval=0.1)
            worker_b = SchedulerWorker(conn_b, poll_interval=0.1)

            tasks = [
                asyncio.create_task(worker_a.run()),
                asyncio.create_task(worker_b.run()),
            ]
            await asyncio.sleep(0.8)
            worker_a.stop()
            worker_b.stop()
            for task in tasks:
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except TimeoutError:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            job_count = await conn_a.fetchval(
                "SELECT count(*) FROM jorb WHERE job_class = $1", job_class
            )
            assert job_count == 1, f"schedule fired {job_count} times, expected 1"

            schedule = await conn_a.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
            assert schedule["run_count"] == 1
            assert schedule["next_run"] > datetime.now(UTC)

    async def test_run_loop_handles_exceptions_gracefully(self, db_pool):
        """Test run() loop handles exceptions in schedule execution - covers exception paths."""
        from unittest.mock import patch

        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)

            # Create schedule
            schedule_id = await manager.create_schedule(
                name="exception-test",
                job_class="test.ExceptionJob",
                cron_expr="* * * * *",
            )

            # Make it due
            await conn.execute(
                """
                UPDATE jorb_schedule
                SET next_run = $1
                WHERE id = $2
            """,
                datetime.now(UTC) - timedelta(minutes=1),
                schedule_id,
            )

            worker = SchedulerWorker(conn, poll_interval=0.1)

            # Mock execute_schedule to raise an exception on first call
            original_execute = worker.execute_schedule
            call_count = [0]

            async def mock_execute(schedule):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("Simulated execution error")
                # After first exception, return success to allow loop to continue
                return ScheduleExecutionResult(result="success", job_id=999)

            with patch.object(worker, "execute_schedule", side_effect=mock_execute):
                # Run scheduler
                import asyncio

                run_task = asyncio.create_task(worker.run())

                # Let it run briefly
                await asyncio.sleep(0.5)

                # Stop it
                worker.stop()

                try:
                    await asyncio.wait_for(run_task, timeout=2.0)
                except TimeoutError:
                    run_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await run_task

                # Verify exception was logged but loop continued
                # Worker should have called execute at least once
                assert call_count[0] >= 1

    async def test_run_loop_updates_metrics_every_10_executions(self, db_pool):
        """Test run() loop logs metrics every 10 executions - covers lines 788-794."""
        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)

            # Create 10 schedules all due now
            schedule_ids = []
            for i in range(10):
                schedule_id = await manager.create_schedule(
                    name=f"metrics-test-{i}",
                    job_class=f"test.MetricsJob{i}",
                    cron_expr="* * * * *",
                )
                schedule_ids.append(schedule_id)

                # Make them all due
                await conn.execute(
                    """
                    UPDATE jorb_schedule
                    SET next_run = $1
                    WHERE id = $2
                """,
                    datetime.now(UTC) - timedelta(minutes=1),
                    schedule_id,
                )

            worker = SchedulerWorker(conn, poll_interval=0.1)

            # Run scheduler
            import asyncio

            run_task = asyncio.create_task(worker.run())

            # Let it run long enough to process all 10 schedules
            await asyncio.sleep(2.0)

            # Stop it
            worker.stop()

            try:
                await asyncio.wait_for(run_task, timeout=2.0)
            except TimeoutError:
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task

            # Verify metrics tracking
            assert worker.executions_total >= 10
            assert worker.successes_total >= 10

            # Verify all schedules were updated
            for schedule_id in schedule_ids:
                schedule = await conn.fetchrow(
                    "SELECT run_count FROM jorb_schedule WHERE id = $1", schedule_id
                )
                assert schedule["run_count"] >= 1

    async def test_run_loop_main_exception_handler(self, db_pool):
        """Test run() loop handles main loop exceptions - covers lines 799-801."""
        from unittest.mock import patch

        async with db_pool.acquire() as conn:
            worker = SchedulerWorker(conn, poll_interval=0.1)

            # Mock find_due_schedules to raise exception on first call
            original_find = worker.find_due_schedules
            call_count = [0]

            async def mock_find():
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("Simulated main loop error")
                # After first error, return empty list to continue gracefully
                return []

            with patch.object(worker, "find_due_schedules", side_effect=mock_find):
                # Run scheduler
                import asyncio

                run_task = asyncio.create_task(worker.run())

                # Let it run briefly (should hit error, sleep 10s, then continue)
                await asyncio.sleep(0.5)

                # Stop it
                worker.stop()

                try:
                    await asyncio.wait_for(run_task, timeout=2.0)
                except TimeoutError:
                    run_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await run_task

                # Verify exception was handled - find_due_schedules was called
                assert call_count[0] >= 1

    async def test_run_loop_graceful_shutdown(self, db_pool):
        """Test run() loop stops gracefully when stop() is called - covers lines 747, 803."""
        async with db_pool.acquire() as conn:
            worker = SchedulerWorker(conn, poll_interval=0.5)

            # Start the worker
            import asyncio

            run_task = asyncio.create_task(worker.run())

            # Let it start up
            await asyncio.sleep(0.1)

            # Request stop
            worker.stop()

            # Should finish within reasonable time
            try:
                await asyncio.wait_for(run_task, timeout=2.0)
            except TimeoutError:
                pytest.fail("Worker did not stop within timeout")

            # Verify stop was logged (worker completed gracefully)
            assert worker.stop_requested is True

    async def test_execution_result_counters_all_types(self, db_pool, monkeypatch):
        """Test result counters for success/failure/skip in run() loop - covers lines 766-772."""
        import asyncio

        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)

            # Create schedule 1: Will succeed
            success_sched_id = await manager.create_schedule(
                name="success-test", job_class="test.SuccessJob", cron_expr="* * * * *"
            )
            await conn.execute(
                "UPDATE jorb_schedule SET next_run = $1 WHERE id = $2",
                datetime.now(UTC) - timedelta(minutes=1),
                success_sched_id,
            )

            # Create schedule 2: Will be skipped due to concurrency
            skip_sched_id = await manager.create_schedule(
                name="skip-test", job_class="test.SkipJob", cron_expr="* * * * *"
            )
            await conn.execute(
                """
                UPDATE jorb_schedule SET max_concurrent_jobs = 1, next_run = $1
                WHERE id = $2
            """,
                datetime.now(UTC) - timedelta(minutes=1),
                skip_sched_id,
            )

            # Create running job to trigger concurrency skip
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio,
                                  schedule_id)
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "test.SkipJob",
                {},
                "default",
                "running",
                100,
                skip_sched_id,
            )

            # Create schedule 3: Will fail (monkeypatch create_scheduled_job)
            fail_sched_id = await manager.create_schedule(
                name="fail-test", job_class="test.FailJob", cron_expr="* * * * *"
            )
            await conn.execute(
                "UPDATE jorb_schedule SET next_run = $1 WHERE id = $2",
                datetime.now(UTC) - timedelta(minutes=1),
                fail_sched_id,
            )

            worker = SchedulerWorker(conn, poll_interval=0.1)

            # Inject failure for the fail-test schedule only
            original_create = worker.create_scheduled_job

            async def selective_failure(schedule, scheduled_time, **kwargs):
                if schedule["name"] == "fail-test":
                    raise RuntimeError("Simulated failure for testing")
                return await original_create(schedule, scheduled_time, **kwargs)

            monkeypatch.setattr(worker, "create_scheduled_job", selective_failure)

            # Run scheduler loop in background
            run_task = asyncio.create_task(worker.run())

            # Let it process all 3 schedules
            await asyncio.sleep(1.0)

            # Stop worker
            worker.stop()

            try:
                await asyncio.wait_for(run_task, timeout=2.0)
            except TimeoutError:
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task

            # Verify ALL counter paths were executed by the REAL run() loop code!
            assert worker.executions_total >= 3, (
                f"Expected >=3 executions, got {worker.executions_total}"
            )
            assert worker.successes_total >= 1, (
                f"Expected >=1 success, got {worker.successes_total}"
            )  # Line 768 ✅
            assert worker.failures_total >= 1, (
                f"Expected >=1 failure, got {worker.failures_total}"
            )  # Line 770 ✅
            assert worker.skips_total >= 1, (
                f"Expected >=1 skip, got {worker.skips_total}"
            )  # Line 772 ✅

    async def test_infailed_transaction_error_on_duplicate_skip(
        self, db_pool, monkeypatch
    ):
        """Test InFailedSQLTransactionError handling in duplicate skip - covers lines 707-714."""
        import asyncpg

        async with db_pool.acquire() as conn:
            manager = ScheduleManager(conn)
            worker = SchedulerWorker(conn, poll_interval=0.1)

            # Create schedule
            schedule_id = await manager.create_schedule(
                name="infailed-test",
                job_class="test.InFailedJob",
                cron_expr="* * * * *",
            )
            await conn.execute(
                "UPDATE jorb_schedule SET next_run = $1 WHERE id = $2",
                datetime.now(UTC) - timedelta(minutes=1),
                schedule_id,
            )
            schedule = await conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )

            # Pre-create job with matching deadline_key to trigger duplicate
            scheduled_time = schedule["next_run"]
            deadline_key = f"schedule:{schedule_id}:{scheduled_time.isoformat()}"
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, state, prio, deadline_key)
                VALUES ($1, $2, $3, $4, $5, $6)
            """,
                "test.InFailedJob",
                {},
                "default",
                "queued",
                100,
                deadline_key,
            )

            # Mock record_execution_skip to raise InFailedSQLTransactionError
            original_record = worker.manager.record_execution_skip
            mock_called = []

            async def mock_failing_record(*args, **kwargs):
                # Raise InFailedSQLTransactionError to simulate transaction already aborted
                mock_called.append(True)
                raise asyncpg.InFailedSQLTransactionError(
                    "Transaction already aborted from UniqueViolationError"
                )

            monkeypatch.setattr(
                worker.manager, "record_execution_skip", mock_failing_record
            )

            # Execute schedule - should hit duplicate path and catch InFailedSQLTransactionError
            result = await worker.execute_schedule(dict(schedule))

            # Should return skipped despite exception
            assert result.result == "skipped"
            assert result.skip_reason == "duplicate"

            # Verify the mock was actually called (covers line 708)
            assert len(mock_called) == 1, "Mock should have been called once"
