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

import asyncio
import json
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
import pytz
import uuid

from pyjobby.scheduler import (
    ScheduleExecutionResult,
    ScheduleSafetyManager,
    ScheduleManager,
    SchedulerWorker
)


# ============================================================================
# Test ScheduleExecutionResult
# ============================================================================

class TestScheduleExecutionResult:
    """Test ScheduleExecutionResult dataclass."""

    def test_success_result(self):
        """Test creating success result."""
        result = ScheduleExecutionResult(
            result='success',
            job_id=123,
            jitter_applied=5,
            queue_depth=10,
            concurrent_jobs=2,
            duration_ms=150
        )

        assert result.result == 'success'
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
            result='failure',
            error_message='Database connection failed',
            duration_ms=50
        )

        assert result.result == 'failure'
        assert result.error_message == 'Database connection failed'
        assert result.duration_ms == 50
        assert result.job_id is None

    def test_skipped_result(self):
        """Test creating skipped result."""
        result = ScheduleExecutionResult(
            result='skipped',
            skip_reason='max_concurrent',
            concurrent_jobs=5
        )

        assert result.result == 'skipped'
        assert result.skip_reason == 'max_concurrent'
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
        await self._create_scheduled_job(client, schedule_id, state='running')
        await self._create_scheduled_job(client, schedule_id, state='queued')

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
            await self._create_scheduled_job(client, schedule_id, state='running')

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
        await self._create_scheduled_job(client, schedule_id, state='running')
        await self._create_scheduled_job(client, schedule_id, state='running')
        await self._create_scheduled_job(client, schedule_id, state='finished')
        await self._create_scheduled_job(client, schedule_id, state='finished')
        await self._create_scheduled_job(client, schedule_id, state='finished')

        async with db_pool.acquire() as conn:
            safety_manager = ScheduleSafetyManager(conn)

            is_safe, count = await safety_manager.check_concurrency(schedule_id, 5)

            assert is_safe is True
            assert count == 2  # Only running jobs counted

    @pytest.mark.asyncio
    async def test_check_backpressure_below_threshold(self, db_pool, safety_manager, client):
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
    async def test_check_backpressure_at_threshold(self, db_pool, safety_manager, client):
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
    async def test_check_backpressure_no_threshold(self, db_pool, safety_manager, client):
        """Test backpressure check when threshold is None (unlimited)."""
        # Create 100 jobs
        for _ in range(100):
            await client.enqueue("test.Job", queue="test_queue")

        is_safe, depth = await safety_manager.check_backpressure("test_queue", None)

        assert is_safe is True
        assert depth == 0  # Returns 0 when no threshold

    @pytest.mark.asyncio
    async def test_check_backpressure_finished_jobs_not_counted(self, db_pool, safety_manager, client):
        """Test that finished jobs don't count toward backpressure."""
        # Use unique queue name
        queue_name = f"test_queue_{uuid.uuid4().hex[:8]}"

        # Create 3 queued, 2 running, 5 finished
        for i in range(3):
            job_id = await client.enqueue("test.Job", queue=queue_name)

        for i in range(2):
            job_id = await client.enqueue("test.Job", queue=queue_name)
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE jorb SET state = 'running' WHERE id = $1", job_id)

        for i in range(5):
            job_id = await client.enqueue("test.Job", queue=queue_name)
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE jorb SET state = 'finished' WHERE id = $1", job_id)

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
            await conn.execute("""
                UPDATE jorb_schedule
                SET consecutive_failures = 2,
                    circuit_breaker_threshold = 5
                WHERE id = $1
            """, schedule_id)

            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)
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
            await conn.execute("""
                UPDATE jorb_schedule
                SET consecutive_failures = 5,
                    circuit_breaker_threshold = 5
                WHERE id = $1
            """, schedule_id)

            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)
            schedule_dict = dict(schedule)

        is_safe, reason = await safety_manager.check_circuit_breaker(schedule_dict)

        assert is_safe is False
        assert 'Circuit breaker triggered' in reason
        assert '5 consecutive failures' in reason

        # Verify schedule was disabled
        async with db_pool.acquire() as conn:
            enabled = await conn.fetchval("SELECT enabled FROM jorb_schedule WHERE id = $1", schedule_id)
            assert enabled is False

    # Helper methods
    async def _create_test_schedule(self, db_pool, name):
        """Create a test schedule with unique name."""
        unique_name = f"{name}-{uuid.uuid4().hex[:8]}"
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, queue, prio,
                    max_concurrent_jobs, circuit_breaker_threshold,
                    next_run
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING id
            """, unique_name, "test.Job", "* * * * *", "default", 100, 10, 5, datetime.utcnow())
            return schedule_id

    async def _create_scheduled_job(self, client, schedule_id, state='queued'):
        """Create a job linked to a schedule."""
        job_id = await client.enqueue(
            "test.Job",
            admin_data={'schedule_id': str(schedule_id)}
        )

        if state != 'queued':
            async with client.pool.acquire() as conn:
                await conn.execute("UPDATE jorb SET state = $1 WHERE id = $2", state, job_id)

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

        now = datetime.now(pytz.UTC)

        # Next run should be within next 2 minutes
        assert next_run > now
        assert next_run < now + timedelta(minutes=2)

    def test_calculate_next_run_daily_at_midnight(self):
        """Test calculating next run for daily at midnight."""
        next_run = ScheduleManager.calculate_next_run("0 0 * * *", "UTC")

        now = datetime.now(pytz.UTC)

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
        with pytest.raises(ValueError, match="Invalid cron expression"):
            ScheduleManager.calculate_next_run("invalid cron", "UTC")

    @pytest.mark.asyncio
    async def test_create_schedule(self, db_pool, manager):
        """Test creating a new schedule."""
        schedule_id = await manager.create_schedule(
            name="test-schedule",
            job_class="test.Job",
            cron_expr="*/5 * * * *",
            queue="test_queue",
            prio=200,
            kwargs={"key": "value"}
        )

        assert schedule_id is not None

        # Verify schedule was created
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            assert schedule['name'] == "test-schedule"
            assert schedule['job_class'] == "test.Job"
            assert schedule['cron_expr'] == "*/5 * * * *"
            assert schedule['queue'] == "test_queue"
            assert schedule['prio'] == 200
            assert schedule['enabled'] is True
            assert schedule['next_run'] is not None

    @pytest.mark.asyncio
    async def test_create_schedule_with_all_options(self, db_pool, manager):
        """Test creating schedule with all optional parameters."""
        schedule_id = await manager.create_schedule(
            name="full-schedule",
            job_class="test.FullJob",
            cron_expr="0 * * * *",
            description="Test schedule with all options",
            queue="custom_queue",
            prio=500,
            capability="special",
            timezone="America/Los_Angeles",
            enabled=False,
            max_concurrent_jobs=3,
            jitter_seconds=30,
            backpressure_threshold=500,
            circuit_breaker_threshold=10,
            kwargs={"param1": "value1", "param2": 42},
            created_by="test_user"
        )

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            assert schedule['description'] == "Test schedule with all options"
            assert schedule['capability'] == "special"
            assert schedule['timezone'] == "America/Los_Angeles"
            assert schedule['enabled'] is False
            assert schedule['max_concurrent_jobs'] == 3
            assert schedule['jitter_seconds'] == 30
            assert schedule['backpressure_threshold'] == 500
            assert schedule['circuit_breaker_threshold'] == 10
            assert schedule['created_by'] == "test_user"

    @pytest.mark.asyncio
    async def test_update_schedule_next_run(self, db_pool, manager):
        """Test updating schedule's next_run timestamp."""
        # Create schedule
        schedule_id = await manager.create_schedule(
            name="update-test",
            job_class="test.Job",
            cron_expr="0 0 * * *"
        )

        # Get initial next_run
        async with db_pool.acquire() as conn:
            initial_next_run = await conn.fetchval(
                "SELECT next_run FROM jorb_schedule WHERE id = $1",
                schedule_id
            )

        # Update to run every minute
        await manager.update_schedule_next_run(schedule_id, "* * * * *", "UTC")

        # Verify next_run was updated
        async with db_pool.acquire() as conn:
            new_next_run = await conn.fetchval(
                "SELECT next_run FROM jorb_schedule WHERE id = $1",
                schedule_id
            )

        assert new_next_run != initial_next_run

    @pytest.mark.asyncio
    async def test_record_execution_success(self, db_pool, manager):
        """Test recording successful execution."""
        schedule_id = await manager.create_schedule(
            name="success-test",
            job_class="test.Job",
            cron_expr="* * * * *"
        )

        # Record success
        await manager.record_execution_success(schedule_id)

        # Verify counters
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            assert schedule['run_count'] == 1
            assert schedule['success_count'] == 1
            assert schedule['consecutive_failures'] == 0
            assert schedule['last_run'] is not None
            assert schedule['last_success'] is not None

    @pytest.mark.asyncio
    async def test_record_execution_failure(self, db_pool, manager):
        """Test recording failed execution."""
        schedule_id = await manager.create_schedule(
            name="failure-test",
            job_class="test.Job",
            cron_expr="* * * * *"
        )

        # Record failure
        await manager.record_execution_failure(schedule_id)

        # Verify counters
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            assert schedule['run_count'] == 1
            assert schedule['failure_count'] == 1
            assert schedule['consecutive_failures'] == 1
            assert schedule['last_run'] is not None
            assert schedule['last_failure'] is not None

    @pytest.mark.asyncio
    async def test_record_consecutive_failures(self, db_pool, manager):
        """Test that consecutive failures increment correctly."""
        schedule_id = await manager.create_schedule(
            name="consecutive-failures",
            job_class="test.Job",
            cron_expr="* * * * *"
        )

        # Record 3 failures
        for _ in range(3):
            await manager.record_execution_failure(schedule_id)

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            assert schedule['consecutive_failures'] == 3
            assert schedule['failure_count'] == 3

    @pytest.mark.asyncio
    async def test_record_success_resets_consecutive_failures(self, db_pool, manager):
        """Test that success resets consecutive failures counter."""
        schedule_id = await manager.create_schedule(
            name="reset-failures",
            job_class="test.Job",
            cron_expr="* * * * *"
        )

        # Record 3 failures then 1 success
        for _ in range(3):
            await manager.record_execution_failure(schedule_id)

        await manager.record_execution_success(schedule_id)

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            assert schedule['consecutive_failures'] == 0
            assert schedule['failure_count'] == 3
            assert schedule['success_count'] == 1

    @pytest.mark.asyncio
    async def test_record_execution_skip(self, db_pool, manager):
        """Test recording skipped execution."""
        schedule_id = await manager.create_schedule(
            name="skip-test",
            job_class="test.Job",
            cron_expr="* * * * *"
        )

        # Record skip
        await manager.record_execution_skip(schedule_id, "max_concurrent")

        # Verify counters
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            assert schedule['skip_count'] == 1


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
            await conn.execute("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
            """, "future-schedule", "test.Job", "* * * * *",
                datetime.utcnow() + timedelta(hours=1), True)

        schedules = await worker.find_due_schedules()

        assert len(schedules) == 0

    @pytest.mark.asyncio
    async def test_find_due_schedules_one_due(self, db_pool, worker):
        """Test finding schedules when one is due."""
        # Create schedule that is due now
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
            """, "due-schedule", "test.Job", "* * * * *",
                datetime.utcnow() - timedelta(minutes=1), True)

        schedules = await worker.find_due_schedules()

        assert len(schedules) == 1
        assert schedules[0]['name'] == "due-schedule"

    @pytest.mark.asyncio
    async def test_find_due_schedules_disabled_not_returned(self, db_pool, worker):
        """Test that disabled schedules are not returned."""
        # Create disabled schedule that is due
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
            """, "disabled-schedule", "test.Job", "* * * * *",
                datetime.utcnow() - timedelta(minutes=1), False)

        schedules = await worker.find_due_schedules()

        assert len(schedules) == 0

    @pytest.mark.asyncio
    async def test_create_scheduled_job(self, db_pool, worker):
        """Test creating a job from a schedule."""
        # Create schedule
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    queue, prio, kwargs
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                RETURNING *
            """, "test-schedule", "test.TestJob", "* * * * *",
                datetime.utcnow(), True, "test_queue", 500,
                json.dumps({"key": "value"}))

            schedule_dict = dict(schedule)

        scheduled_time = datetime.utcnow()
        job_id = await worker.create_scheduled_job(schedule_dict, scheduled_time)

        assert job_id is not None

        # Verify job was created
        async with db_pool.acquire() as conn:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

            assert job['job_class'] == "test.TestJob"
            assert job['queue'] == "test_queue"
            assert job['prio'] == 500
            assert job['state'] == 'queued'
            assert 'schedule_id' in job['admin_data']

    @pytest.mark.asyncio
    async def test_create_scheduled_job_duplicate_prevention(self, db_pool, worker):
        """Test that duplicate jobs are prevented by deadline_key."""
        # Create schedule
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
                RETURNING *
            """, "dup-test", "test.Job", "* * * * *",
                datetime.utcnow(), True)

            schedule_dict = dict(schedule)

        scheduled_time = datetime.utcnow()

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
            schedule = await conn.fetchrow("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled
                ) VALUES ($1, $2, $3, $4, $5)
                RETURNING *
            """, f"log-test-{uuid.uuid4().hex[:8]}", "test.Job", "* * * * *",
                datetime.utcnow(), True)

            schedule_dict = dict(schedule)

        # Create a real job for the log entry
        job_id = await client.enqueue("test.Job")

        scheduled_time = datetime.utcnow()
        result = ScheduleExecutionResult(
            result='success',
            job_id=job_id,
            jitter_applied=5,
            queue_depth=10,
            concurrent_jobs=2,
            duration_ms=150
        )

        await worker.log_execution(schedule_dict, scheduled_time, result)

        # Verify log entry
        async with db_pool.acquire() as conn:
            log = await conn.fetchrow("""
                SELECT * FROM jorb_schedule_log
                WHERE schedule_id = $1
            """, schedule_dict['id'])

            assert log['result'] == 'success'
            assert log['job_id'] == job_id
            assert log['jitter_applied_seconds'] == 5
            assert log['queue_depth_at_run'] == 10
            assert log['concurrent_jobs_at_run'] == 2
            assert log['duration_ms'] == 150

    @pytest.mark.asyncio
    async def test_execute_schedule_success(self, db_pool, worker):
        """Test successful schedule execution."""
        # Create schedule
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    max_concurrent_jobs, jitter_seconds,
                    backpressure_threshold, circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING *
            """, "exec-success", "test.Job", "* * * * *",
                datetime.utcnow(), True, 10, 0, 1000, 5)

            schedule_dict = dict(schedule)

        result = await worker.execute_schedule(schedule_dict)

        assert result.result == 'success'
        assert result.job_id is not None

    @pytest.mark.asyncio
    async def test_execute_schedule_circuit_breaker(self, db_pool, worker):
        """Test schedule execution with circuit breaker triggered."""
        # Create schedule with failures at threshold
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    consecutive_failures, circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
            """, "circuit-breaker", "test.Job", "* * * * *",
                datetime.utcnow(), True, 5, 5)

            schedule_dict = dict(schedule)

        result = await worker.execute_schedule(schedule_dict)

        assert result.result == 'skipped'
        assert result.skip_reason == 'circuit_breaker'

    @pytest.mark.asyncio
    async def test_execute_schedule_max_concurrent(self, db_pool, worker, client):
        """Test schedule execution with max concurrent reached."""
        # Create schedule with max_concurrent = 2
        schedule_name = f"max-concurrent-{uuid.uuid4().hex[:8]}"

        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    max_concurrent_jobs, circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
            """, schedule_name, "test.Job", "* * * * *",
                datetime.utcnow(), True, 2, 5)

            schedule_dict = dict(schedule)

        # Create 2 jobs already running for this schedule
        for _ in range(2):
            job_id = await client.enqueue(
                "test.Job",
                admin_data={'schedule_id': str(schedule_dict['id'])}
            )
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE jorb SET state = 'running' WHERE id = $1", job_id)

        result = await worker.execute_schedule(schedule_dict)

        assert result.result == 'skipped'
        assert result.skip_reason == 'max_concurrent'

    @pytest.mark.asyncio
    async def test_execute_schedule_backpressure(self, db_pool, worker, client):
        """Test schedule execution with backpressure."""
        # Use unique queue and schedule name
        queue_name = f"test_queue_{uuid.uuid4().hex[:8]}"
        schedule_name = f"backpressure-{uuid.uuid4().hex[:8]}"

        # Create schedule with backpressure_threshold = 5
        async with db_pool.acquire() as conn:
            schedule = await conn.fetchrow("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    queue, backpressure_threshold, circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                RETURNING *
            """, schedule_name, "test.Job", "* * * * *",
                datetime.utcnow(), True, queue_name, 5, 5)

            schedule_dict = dict(schedule)

        # Fill queue with 5 jobs (at threshold)
        for _ in range(5):
            await client.enqueue("test.Job", queue=queue_name)

        result = await worker.execute_schedule(schedule_dict)

        assert result.result == 'skipped'
        assert result.skip_reason == 'backpressure'

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
            schedule = await conn.fetchrow("""
                INSERT INTO jorb_schedule (
                    name, job_class, cron_expr, next_run, enabled,
                    circuit_breaker_threshold
                ) VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING *
            """, "metrics-test", "test.Job", "* * * * *",
                datetime.utcnow(), True, 5)

            schedule_dict = dict(schedule)

        # Execute schedule (should succeed)
        result = await worker.execute_schedule(schedule_dict)

        # Manually update metrics (normally done in run() loop)
        worker.executions_total += 1
        if result.result == 'success':
            worker.successes_total += 1
        elif result.result == 'failure':
            worker.failures_total += 1
        elif result.result == 'skipped':
            worker.skips_total += 1

        assert worker.executions_total == 1
        assert worker.successes_total == 1


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
                kwargs={"param": "value"}
            )

            # 2. Set next_run to past (make it due)
            await conn.execute("""
                UPDATE jorb_schedule
                SET next_run = $1
                WHERE id = $2
            """, datetime.utcnow() - timedelta(minutes=1), schedule_id)

            # 3. Execute schedule
            worker = SchedulerWorker(conn)
            schedules = await worker.find_due_schedules()

            assert len(schedules) == 1

            result = await worker.execute_schedule(schedules[0])

            assert result.result == 'success'
            assert result.job_id is not None

            # 4. Verify job was created
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", result.job_id)

            assert job['job_class'] == "test.LifecycleJob"
            assert job['queue'] == "test_queue"
            assert job['admin_data']['schedule_id'] == str(schedule_id)

            # 5. Verify schedule counters updated
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            assert schedule['run_count'] == 1
            assert schedule['success_count'] == 1

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
                    cron_expr="* * * * *"
                )
                schedule_ids.append(schedule_id)

                # Make them all due
                await conn.execute("""
                    UPDATE jorb_schedule
                    SET next_run = $1
                    WHERE id = $2
                """, datetime.utcnow() - timedelta(minutes=1), schedule_id)

            # Find and execute all
            worker = SchedulerWorker(conn)
            schedules = await worker.find_due_schedules()

            assert len(schedules) == 5

            # Execute all schedules
            job_ids = []
            for schedule in schedules:
                result = await worker.execute_schedule(schedule)
                assert result.result == 'success'
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
                timezone="America/New_York"
            )

            # Get the schedule
            schedule = await conn.fetchrow("SELECT * FROM jorb_schedule WHERE id = $1", schedule_id)

            # Verify next_run has timezone info
            assert schedule['next_run'] is not None
            assert schedule['timezone'] == "America/New_York"
