"""
Tests for Recurring Scheduler

Comprehensive tests for all scheduler features:
- Schedule creation and management
- Safety features (concurrency, jitter, backpressure, circuit breaker)
- Schedule execution
- Logging and metrics
"""

import pytest
import asyncpg
from datetime import datetime, timedelta
from pyjobby.admin_api import AdminAPI
from pyjobby.scheduler import ScheduleSafetyManager, ScheduleManager, SchedulerWorker
import asyncio


@pytest.fixture
async def admin_api(db_connection):
    """Create AdminAPI instance with test database"""
    return AdminAPI(db_connection)


@pytest.fixture
async def safety_manager(db_connection):
    """Create ScheduleSafetyManager instance"""
    return ScheduleSafetyManager(db_connection)


@pytest.fixture
async def schedule_manager(db_connection):
    """Create ScheduleManager instance"""
    return ScheduleManager(db_connection)


async def create_test_schedule(conn, **kwargs):
    """Helper to create a test schedule"""
    defaults = {
        'name': f'test-schedule-{datetime.utcnow().timestamp()}',
        'job_class': 'test.TestJob',
        'cron_expr': '0 * * * *',  # Hourly
        'queue': 'default',
        'prio': 100,
        'timezone': 'UTC',
        'enabled': True,
        'max_concurrent_jobs': 1,
        'jitter_seconds': 0,
        'backpressure_threshold': 1000,
        'circuit_breaker_threshold': 5,
        'kwargs': '{}',
        'run_count': 0,
        'success_count': 0,
        'failure_count': 0,
        'skip_count': 0,
        'consecutive_failures': 0,
    }
    defaults.update(kwargs)

    # Calculate next_run
    from croniter import croniter
    import pytz
    tz = pytz.timezone(defaults['timezone'])
    now = datetime.now(tz)
    cron = croniter(defaults['cron_expr'], now)
    next_run = cron.get_next(datetime)

    return await conn.fetchval("""
        INSERT INTO jorb_schedule (
            name, job_class, cron_expr, queue, prio, timezone, enabled,
            max_concurrent_jobs, jitter_seconds, backpressure_threshold,
            circuit_breaker_threshold, kwargs, next_run,
            run_count, success_count, failure_count, skip_count, consecutive_failures
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                  $14, $15, $16, $17, $18)
        RETURNING id
    """, defaults['name'], defaults['job_class'], defaults['cron_expr'],
        defaults['queue'], defaults['prio'], defaults['timezone'],
        defaults['enabled'], defaults['max_concurrent_jobs'],
        defaults['jitter_seconds'], defaults['backpressure_threshold'],
        defaults['circuit_breaker_threshold'], defaults['kwargs'], next_run,
        defaults['run_count'], defaults['success_count'], defaults['failure_count'],
        defaults['skip_count'], defaults['consecutive_failures'])


async def create_test_job_for_schedule(conn, schedule_id, state='queued'):
    """Helper to create a job associated with a schedule"""
    return await conn.fetchval("""
        INSERT INTO jorb (
            job_class, kwargs, queue, state, prio,
            admin_data
        ) VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
    """, 'test.TestJob', {}, 'default', state, 100,
        {'schedule_id': str(schedule_id), 'schedule_name': 'test'})


class TestScheduleCreation:
    """Tests for schedule creation via Admin API"""

    async def test_create_schedule_basic(self, admin_api):
        """Test creating a basic schedule"""
        schedule = await admin_api.create_schedule(
            name='test-schedule',
            job_class='test.TestJob',
            cron_expr='0 2 * * *',  # 2am daily
        )

        assert schedule['name'] == 'test-schedule'
        assert schedule['job_class'] == 'test.TestJob'
        assert schedule['cron_expr'] == '0 2 * * *'
        assert schedule['enabled'] is True
        assert schedule['max_concurrent_jobs'] == 1
        assert schedule['jitter_seconds'] == 0
        assert schedule['next_run'] is not None

    async def test_create_schedule_with_safety_features(self, admin_api):
        """Test creating schedule with all safety features"""
        schedule = await admin_api.create_schedule(
            name='test-safe-schedule',
            job_class='test.SafeJob',
            cron_expr='*/5 * * * *',  # Every 5 minutes
            max_concurrent_jobs=3,
            jitter_seconds=60,
            backpressure_threshold=500,
            circuit_breaker_threshold=10,
            description='Test schedule with safety features',
        )

        assert schedule['max_concurrent_jobs'] == 3
        assert schedule['jitter_seconds'] == 60
        assert schedule['backpressure_threshold'] == 500
        assert schedule['circuit_breaker_threshold'] == 10
        assert schedule['description'] == 'Test schedule with safety features'

    async def test_create_schedule_invalid_cron(self, admin_api):
        """Test creating schedule with invalid cron expression"""
        with pytest.raises(ValueError, match='Invalid cron expression'):
            await admin_api.create_schedule(
                name='test-invalid',
                job_class='test.TestJob',
                cron_expr='invalid cron',
            )

    async def test_create_schedule_duplicate_name(self, admin_api):
        """Test creating schedule with duplicate name"""
        await admin_api.create_schedule(
            name='test-duplicate',
            job_class='test.TestJob',
            cron_expr='0 * * * *',
        )

        with pytest.raises(Exception):  # asyncpg.UniqueViolationError
            await admin_api.create_schedule(
                name='test-duplicate',
                job_class='test.TestJob',
                cron_expr='0 * * * *',
            )


class TestScheduleManagement:
    """Tests for schedule management operations"""

    async def test_list_schedules(self, admin_api, db_connection):
        """Test listing schedules"""
        # Create test schedules
        await create_test_schedule(db_connection, name='schedule-1')
        await create_test_schedule(db_connection, name='schedule-2')

        schedules = await admin_api.list_schedules()

        assert len(schedules) >= 2
        schedule_names = [s['name'] for s in schedules]
        assert 'schedule-1' in schedule_names
        assert 'schedule-2' in schedule_names

    async def test_list_schedules_filter_enabled(self, admin_api, db_connection):
        """Test filtering schedules by enabled status"""
        await create_test_schedule(db_connection, name='enabled-schedule', enabled=True)
        await create_test_schedule(db_connection, name='disabled-schedule', enabled=False)

        enabled_schedules = await admin_api.list_schedules(enabled=True)
        disabled_schedules = await admin_api.list_schedules(enabled=False)

        enabled_names = [s['name'] for s in enabled_schedules]
        disabled_names = [s['name'] for s in disabled_schedules]

        assert 'enabled-schedule' in enabled_names
        assert 'disabled-schedule' in disabled_names
        assert 'disabled-schedule' not in enabled_names

    async def test_get_schedule_by_id(self, admin_api, db_connection):
        """Test getting schedule by ID"""
        schedule_id = await create_test_schedule(db_connection, name='test-get-schedule')

        schedule = await admin_api.get_schedule(schedule_id=schedule_id)

        assert schedule is not None
        assert schedule['id'] == schedule_id
        assert schedule['name'] == 'test-get-schedule'

    async def test_get_schedule_by_name(self, admin_api, db_connection):
        """Test getting schedule by name"""
        await create_test_schedule(db_connection, name='test-get-by-name')

        schedule = await admin_api.get_schedule(name='test-get-by-name')

        assert schedule is not None
        assert schedule['name'] == 'test-get-by-name'

    async def test_enable_schedule(self, admin_api, db_connection):
        """Test enabling a disabled schedule"""
        schedule_id = await create_test_schedule(db_connection, enabled=False)

        updated = await admin_api.enable_schedule(schedule_id)

        assert updated['enabled'] is True
        assert updated['consecutive_failures'] == 0  # Should reset

    async def test_disable_schedule(self, admin_api, db_connection):
        """Test disabling an enabled schedule"""
        schedule_id = await create_test_schedule(db_connection, enabled=True)

        updated = await admin_api.disable_schedule(schedule_id)

        assert updated['enabled'] is False

    async def test_delete_schedule(self, admin_api, db_connection):
        """Test deleting a schedule"""
        schedule_id = await create_test_schedule(db_connection)

        result = await admin_api.delete_schedule(schedule_id)

        assert result['status'] == 'deleted'

        # Verify it's actually deleted
        schedule = await admin_api.get_schedule(schedule_id=schedule_id)
        assert schedule is None

    async def test_update_schedule_cron(self, admin_api, db_connection):
        """Test updating schedule cron expression"""
        schedule_id = await create_test_schedule(db_connection, cron_expr='0 * * * *')

        updated = await admin_api.update_schedule(
            schedule_id,
            cron_expr='0 2 * * *'  # Change to 2am daily
        )

        assert updated['cron_expr'] == '0 2 * * *'
        # next_run should be recalculated
        assert updated['next_run'] is not None


class TestSafetyFeatures:
    """Tests for scheduler safety features"""

    async def test_concurrency_check_no_limit(self, safety_manager, db_connection):
        """Test concurrency check when under limit"""
        schedule_id = await create_test_schedule(db_connection, max_concurrent_jobs=3)

        # Create 2 running jobs (under limit of 3)
        await create_test_job_for_schedule(db_connection, schedule_id, 'running')
        await create_test_job_for_schedule(db_connection, schedule_id, 'running')

        can_run, count = await safety_manager.check_concurrency(schedule_id, 3)

        assert can_run is True
        assert count == 2

    async def test_concurrency_check_at_limit(self, safety_manager, db_connection):
        """Test concurrency check when at limit"""
        schedule_id = await create_test_schedule(db_connection, max_concurrent_jobs=2)

        # Create 2 running jobs (at limit)
        await create_test_job_for_schedule(db_connection, schedule_id, 'running')
        await create_test_job_for_schedule(db_connection, schedule_id, 'running')

        can_run, count = await safety_manager.check_concurrency(schedule_id, 2)

        assert can_run is False
        assert count == 2

    async def test_backpressure_check_under_threshold(self, safety_manager, db_connection):
        """Test backpressure check when queue is not overloaded"""
        # Create a few jobs in queue (under threshold)
        for _ in range(10):
            await db_connection.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """, 'test.Job', '{}', 'default', 'queued', 100)

        can_run, depth = await safety_manager.check_backpressure('default', 1000)

        assert can_run is True
        assert depth >= 10

    async def test_backpressure_check_over_threshold(self, safety_manager, db_connection):
        """Test backpressure check when queue is overloaded"""
        # Create many jobs to exceed threshold
        for _ in range(15):
            await db_connection.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """, 'test.Job', '{}', 'test-queue', 'queued', 100)

        can_run, depth = await safety_manager.check_backpressure('test-queue', 10)

        assert can_run is False
        assert depth >= 15

    async def test_jitter_calculation_zero(self, safety_manager):
        """Test jitter calculation with 0 seconds"""
        jitter = safety_manager.calculate_jitter(0)

        assert jitter == 0

    async def test_jitter_calculation_nonzero(self, safety_manager):
        """Test jitter calculation with non-zero seconds"""
        jitter = safety_manager.calculate_jitter(60)

        assert 0 <= jitter <= 60

    async def test_circuit_breaker_under_threshold(self, safety_manager, db_connection):
        """Test circuit breaker when under failure threshold"""
        schedule_id = await create_test_schedule(
            db_connection,
            circuit_breaker_threshold=5,
            consecutive_failures=3
        )

        schedule = await db_connection.fetchrow(
            'SELECT * FROM jorb_schedule WHERE id = $1', schedule_id
        )

        can_run, reason = await safety_manager.check_circuit_breaker(dict(schedule))

        assert can_run is True
        assert reason == ""

    async def test_circuit_breaker_at_threshold(self, safety_manager, db_connection):
        """Test circuit breaker when at failure threshold (should disable)"""
        schedule_id = await create_test_schedule(
            db_connection,
            circuit_breaker_threshold=5,
            consecutive_failures=5
        )

        schedule = await db_connection.fetchrow(
            'SELECT * FROM jorb_schedule WHERE id = $1', schedule_id
        )

        can_run, reason = await safety_manager.check_circuit_breaker(dict(schedule))

        assert can_run is False
        assert 'Circuit breaker' in reason

        # Verify schedule was disabled
        updated = await db_connection.fetchrow(
            'SELECT * FROM jorb_schedule WHERE id = $1', schedule_id
        )
        assert updated['enabled'] is False


class TestScheduleExecution:
    """Tests for schedule execution logic"""

    async def test_find_due_schedules(self, db_connection):
        """Test finding schedules that are due to run"""
        worker = SchedulerWorker(db_connection)

        # Create schedule that's due now
        now = datetime.utcnow()
        await db_connection.execute("""
            INSERT INTO jorb_schedule (
                name, job_class, cron_expr, queue, prio, enabled,
                next_run, kwargs
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, 'test-due-schedule', 'test.Job', '0 * * * *', 'default', 100,
            True, now - timedelta(minutes=1), {})

        # Create schedule that's not due yet
        await db_connection.execute("""
            INSERT INTO jorb_schedule (
                name, job_class, cron_expr, queue, prio, enabled,
                next_run, kwargs
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, 'test-future-schedule', 'test.Job', '0 * * * *', 'default', 100,
            True, now + timedelta(hours=1), {})

        due_schedules = await worker.find_due_schedules()

        schedule_names = [s['name'] for s in due_schedules]
        assert 'test-due-schedule' in schedule_names
        assert 'test-future-schedule' not in schedule_names

    async def test_execute_schedule_success(self, db_connection):
        """Test successful schedule execution"""
        worker = SchedulerWorker(db_connection)

        # Create schedule
        schedule_id = await create_test_schedule(
            db_connection,
            name='test-execute-schedule'
        )

        schedule = await db_connection.fetchrow(
            'SELECT * FROM jorb_schedule WHERE id = $1', schedule_id
        )

        result = await worker.execute_schedule(dict(schedule))

        assert result.result == 'success'
        assert result.job_id is not None
        assert result.skip_reason is None

    async def test_execute_schedule_skip_concurrency(self, db_connection):
        """Test schedule execution skipped due to concurrency limit"""
        worker = SchedulerWorker(db_connection)

        # Create schedule with max_concurrent=1
        schedule_id = await create_test_schedule(
            db_connection,
            max_concurrent_jobs=1
        )

        # Create a running job for this schedule
        await create_test_job_for_schedule(db_connection, schedule_id, 'running')

        schedule = await db_connection.fetchrow(
            'SELECT * FROM jorb_schedule WHERE id = $1', schedule_id
        )

        result = await worker.execute_schedule(dict(schedule))

        assert result.result == 'skipped'
        assert result.skip_reason == 'max_concurrent'
        assert result.concurrent_jobs == 1

    async def test_execute_schedule_skip_backpressure(self, db_connection):
        """Test schedule execution skipped due to backpressure"""
        worker = SchedulerWorker(db_connection)

        # Create schedule with low backpressure threshold
        schedule_id = await create_test_schedule(
            db_connection,
            backpressure_threshold=5
        )

        # Create many jobs in queue to trigger backpressure
        for _ in range(10):
            await db_connection.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, state, prio)
                VALUES ($1, $2, $3, $4, $5)
            """, 'test.Job', '{}', 'default', 'queued', 100)

        schedule = await db_connection.fetchrow(
            'SELECT * FROM jorb_schedule WHERE id = $1', schedule_id
        )

        result = await worker.execute_schedule(dict(schedule))

        assert result.result == 'skipped'
        assert result.skip_reason == 'backpressure'
        assert result.queue_depth >= 10


class TestScheduleStats:
    """Tests for schedule statistics and history"""

    async def test_get_schedule_stats(self, admin_api, db_connection):
        """Test getting schedule statistics"""
        # Create schedule with some execution data
        schedule_id = await create_test_schedule(
            db_connection,
            run_count=10,
            success_count=8,
            failure_count=2
        )

        stats = await admin_api.get_schedule_stats()

        schedule_stats = next((s for s in stats if s['id'] == schedule_id), None)
        assert schedule_stats is not None
        assert schedule_stats['run_count'] == 10
        assert schedule_stats['success_count'] == 8
        assert schedule_stats['failure_count'] == 2
        assert schedule_stats['success_rate_pct'] == 80.0

    async def test_get_schedule_history(self, admin_api, db_connection):
        """Test getting schedule execution history"""
        schedule_id = await create_test_schedule(db_connection)

        # Create some execution history
        for i in range(5):
            await db_connection.execute("""
                INSERT INTO jorb_schedule_log (
                    schedule_id, schedule_name, scheduled_time, result
                ) VALUES ($1, $2, $3, $4)
            """, schedule_id, 'test-schedule', datetime.utcnow(), 'success')

        history = await admin_api.get_schedule_history(schedule_id=schedule_id)

        assert len(history) == 5
        assert all(h['result'] == 'success' for h in history)

    async def test_get_schedule_history_filter(self, admin_api, db_connection):
        """Test filtering schedule history by result"""
        schedule_id = await create_test_schedule(db_connection)

        # Create mixed results
        for _ in range(3):
            await db_connection.execute("""
                INSERT INTO jorb_schedule_log (
                    schedule_id, schedule_name, scheduled_time, result
                ) VALUES ($1, $2, $3, $4)
            """, schedule_id, 'test-schedule', datetime.utcnow(), 'success')

        for _ in range(2):
            await db_connection.execute("""
                INSERT INTO jorb_schedule_log (
                    schedule_id, schedule_name, scheduled_time, result
                ) VALUES ($1, $2, $3, $4)
            """, schedule_id, 'test-schedule', datetime.utcnow(), 'failure')

        # Filter by success
        success_history = await admin_api.get_schedule_history(
            schedule_id=schedule_id,
            result_filter='success'
        )

        assert len(success_history) == 3
        assert all(h['result'] == 'success' for h in success_history)


class TestScheduleManager:
    """Tests for ScheduleManager utility methods"""

    def test_calculate_next_run_hourly(self):
        """Test calculating next run for hourly schedule"""
        import pytz
        next_run = ScheduleManager.calculate_next_run('0 * * * *')  # Hourly
        now = datetime.now(pytz.UTC)

        assert next_run > now
        # Should be within the next hour
        assert next_run < now + timedelta(hours=2)

    def test_calculate_next_run_daily(self):
        """Test calculating next run for daily schedule"""
        import pytz
        next_run = ScheduleManager.calculate_next_run('0 2 * * *')  # 2am daily
        now = datetime.now(pytz.UTC)

        assert next_run > now
        # Should be 2am
        assert next_run.hour == 2
        assert next_run.minute == 0

    def test_calculate_next_run_with_timezone(self):
        """Test calculating next run with specific timezone"""
        import pytz
        next_run = ScheduleManager.calculate_next_run(
            '0 12 * * *',  # Noon
            timezone='America/New_York'
        )
        now = datetime.now(pytz.timezone('America/New_York'))

        assert next_run > now
        # Should have timezone info
        assert next_run.tzinfo is not None


class TestEdgeCases:
    """Tests for edge cases and error handling"""

    async def test_schedule_with_deadline_key_prevents_duplicates(self, db_connection):
        """Test that deadline keys prevent duplicate job creation"""
        worker = SchedulerWorker(db_connection)

        schedule_id = await create_test_schedule(db_connection)
        schedule = await db_connection.fetchrow(
            'SELECT * FROM jorb_schedule WHERE id = $1', schedule_id
        )

        # Execute schedule twice with same scheduled time
        result1 = await worker.execute_schedule(dict(schedule))
        result2 = await worker.execute_schedule(dict(schedule))

        assert result1.result == 'success'
        assert result1.job_id is not None

        # Second execution should be skipped due to duplicate
        assert result2.result == 'skipped'
        assert result2.skip_reason == 'duplicate'

    async def test_schedule_with_very_high_jitter(self, safety_manager):
        """Test jitter with very high value"""
        jitter = safety_manager.calculate_jitter(3600)  # 1 hour

        assert 0 <= jitter <= 3600

    async def test_update_schedule_invalid_field(self, admin_api, db_connection):
        """Test updating schedule with invalid field (should be ignored)"""
        schedule_id = await create_test_schedule(db_connection)

        updated = await admin_api.update_schedule(
            schedule_id,
            invalid_field='should-be-ignored',
            description='Valid update'
        )

        assert updated['description'] == 'Valid update'
        assert 'invalid_field' not in updated

    async def test_circuit_breaker_resets_on_enable(self, admin_api, db_connection):
        """Test that enabling schedule resets circuit breaker"""
        schedule_id = await create_test_schedule(
            db_connection,
            enabled=False,
            consecutive_failures=10
        )

        updated = await admin_api.enable_schedule(schedule_id)

        assert updated['enabled'] is True
        assert updated['consecutive_failures'] == 0
