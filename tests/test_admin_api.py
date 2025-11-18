"""
Tests for Admin API

Tests all administrative operations for job, queue, and worker management.
"""

import pytest
import asyncpg
from datetime import datetime, timedelta
from pyjobby.admin_api import AdminAPI, JobInfo, QueueStats, WorkerInfo


@pytest.fixture
async def admin_api(db_connection):
    """Create AdminAPI instance with test database"""
    return AdminAPI(db_connection)


async def create_test_job(conn, **kwargs):
    """Helper to create a test job"""
    defaults = {
        'job_class': 'job.test.TestJob',
        'kwargs': '{}',
        'queue': 'default',
        'state': 'queued',
        'prio': 100,
    }
    defaults.update(kwargs)

    return await conn.fetchval("""
        INSERT INTO jorb (job_class, kwargs, queue, state, prio, uid, error_count)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
    """, defaults['job_class'], defaults['kwargs'], defaults['queue'],
        defaults['state'], defaults['prio'], defaults.get('uid'),
        defaults.get('error_count', 0))


class TestJobManagement:
    """Tests for job management API"""

    async def test_list_jobs_no_filter(self, admin_api, db_connection):
        """Test listing all jobs"""
        # Create test jobs
        job1_id = await create_test_job(db_connection, job_class='job.test.Job1')
        job2_id = await create_test_job(db_connection, job_class='job.test.Job2')

        # List jobs
        jobs = await admin_api.list_jobs(limit=10)

        assert len(jobs) >= 2
        job_ids = [j['id'] for j in jobs]
        assert job1_id in job_ids
        assert job2_id in job_ids

    async def test_list_jobs_filter_by_queue(self, admin_api, db_connection):
        """Test filtering jobs by queue"""
        job1_id = await create_test_job(db_connection, queue='default')
        job2_id = await create_test_job(db_connection, queue='priority')

        # Filter by queue
        jobs = await admin_api.list_jobs(queue='priority')

        assert len(jobs) == 1
        assert jobs[0]['id'] == job2_id
        assert jobs[0]['queue'] == 'priority'

    async def test_list_jobs_filter_by_state(self, admin_api, db_connection):
        """Test filtering jobs by state"""
        job1_id = await create_test_job(db_connection, state='queued')
        job2_id = await create_test_job(db_connection, state='crashed')

        # Filter by state
        jobs = await admin_api.list_jobs(state='crashed')

        assert len(jobs) == 1
        assert jobs[0]['id'] == job2_id
        assert jobs[0]['state'] == 'crashed'

    async def test_list_jobs_filter_by_uid(self, admin_api, db_connection):
        """Test filtering jobs by user ID"""
        job1_id = await create_test_job(db_connection, uid=123)
        job2_id = await create_test_job(db_connection, uid=456)

        # Filter by uid
        jobs = await admin_api.list_jobs(uid=123)

        assert len(jobs) == 1
        assert jobs[0]['id'] == job1_id
        assert jobs[0]['uid'] == 123

    async def test_list_jobs_pagination(self, admin_api, db_connection):
        """Test job list pagination"""
        # Create 5 jobs
        job_ids = []
        for i in range(5):
            job_id = await create_test_job(db_connection, job_class=f'job.test.Job{i}')
            job_ids.append(job_id)

        # Get first 2
        page1 = await admin_api.list_jobs(limit=2, offset=0, order_by='id', order_dir='ASC')
        assert len(page1) >= 2

        # Get next 2
        page2 = await admin_api.list_jobs(limit=2, offset=2, order_by='id', order_dir='ASC')
        assert len(page2) >= 2

        # Pages should be different
        assert page1[0]['id'] != page2[0]['id']

    async def test_get_job(self, admin_api, db_connection):
        """Test getting single job details"""
        job_id = await create_test_job(
            db_connection,
            job_class='job.test.TestJob',
            queue='test-queue',
            prio=50,
            uid=999
        )

        # Get job
        job = await admin_api.get_job(job_id)

        assert job is not None
        assert job['id'] == job_id
        assert job['job_class'] == 'job.test.TestJob'
        assert job['queue'] == 'test-queue'
        assert job['prio'] == 50
        assert job['uid'] == 999

    async def test_get_job_not_found(self, admin_api):
        """Test getting non-existent job"""
        job = await admin_api.get_job(999999)
        assert job is None

    async def test_retry_job_crashed(self, admin_api, db_connection):
        """Test retrying a crashed job"""
        # Create crashed job
        job_id = await create_test_job(
            db_connection,
            state='crashed',
            error_count=3,
            job_class='job.test.FailJob'
        )

        # Retry job
        result = await admin_api.retry_job(job_id)

        assert result['original_job_id'] == job_id
        assert result['new_job_id'] != job_id
        assert result['status'] == 'retry_queued'

        # Check new job was created
        new_job = await admin_api.get_job(result['new_job_id'])
        assert new_job['state'] == 'queued'
        assert new_job['job_class'] == 'job.test.FailJob'
        assert new_job['error_count'] == 0
        assert new_job['admin_data']['parent_job_id'] == job_id

    async def test_retry_job_not_retriable(self, admin_api, db_connection):
        """Test retrying a job in non-retriable state"""
        # Create running job
        job_id = await create_test_job(db_connection, state='running')

        # Attempt retry
        with pytest.raises(ValueError, match="can only retry crashed or cancelled"):
            await admin_api.retry_job(job_id)

    async def test_retry_job_not_found(self, admin_api):
        """Test retrying non-existent job"""
        with pytest.raises(ValueError, match="not found"):
            await admin_api.retry_job(999999)

    async def test_retry_jobs_bulk(self, admin_api, db_connection):
        """Test bulk retry of multiple jobs"""
        # Create 3 crashed jobs
        job_ids = []
        for i in range(3):
            job_id = await create_test_job(db_connection, state='crashed')
            job_ids.append(job_id)

        # Bulk retry
        results = await admin_api.retry_jobs(job_ids)

        assert len(results) == 3
        for result in results:
            assert result['status'] == 'retry_queued'
            assert result['new_job_id'] != result['original_job_id']

    async def test_cancel_job(self, admin_api, db_connection):
        """Test cancelling a queued job"""
        # Create queued job
        job_id = await create_test_job(db_connection, state='queued')

        # Cancel job
        result = await admin_api.cancel_job(job_id)

        assert result['job_id'] == job_id
        assert result['status'] == 'cancelled'

        # Verify job is cancelled
        job = await admin_api.get_job(job_id)
        assert job['state'] == 'cancelled'

    async def test_cancel_waiting_job(self, admin_api, db_connection):
        """Test cancelling a waiting job"""
        # Create waiting job
        job_id = await create_test_job(db_connection, state='waiting')

        # Cancel job
        result = await admin_api.cancel_job(job_id)

        assert result['status'] == 'cancelled'

    async def test_cancel_job_not_cancellable(self, admin_api, db_connection):
        """Test cancelling a job in non-cancellable state"""
        # Create running job
        job_id = await create_test_job(db_connection, state='running')

        # Attempt cancel
        with pytest.raises(ValueError, match="can only cancel queued or waiting"):
            await admin_api.cancel_job(job_id)

    async def test_cancel_jobs_bulk(self, admin_api, db_connection):
        """Test bulk cancellation"""
        # Create jobs in various states
        queued_id = await create_test_job(db_connection, state='queued')
        waiting_id = await create_test_job(db_connection, state='waiting')
        running_id = await create_test_job(db_connection, state='running')

        # Bulk cancel
        results = await admin_api.cancel_jobs([queued_id, waiting_id, running_id])

        assert len(results) == 3

        # First two should succeed
        assert results[0]['status'] == 'cancelled'
        assert results[1]['status'] == 'cancelled'

        # Third should fail
        assert results[2]['status'] == 'error'

    async def test_delete_job(self, admin_api, db_connection):
        """Test deleting a job"""
        # Create job
        job_id = await create_test_job(db_connection)

        # Delete job
        deleted = await admin_api.delete_job(job_id)

        assert deleted is True

        # Verify job is gone
        job = await admin_api.get_job(job_id)
        assert job is None

    async def test_delete_jobs_bulk(self, admin_api, db_connection):
        """Test bulk delete by criteria"""
        # Create jobs in test queue
        for i in range(5):
            await create_test_job(db_connection, queue='test-delete', state='finished')

        # Delete all finished jobs in test queue
        count = await admin_api.delete_jobs(queue='test-delete', state='finished')

        assert count == 5

        # Verify jobs are gone
        jobs = await admin_api.list_jobs(queue='test-delete')
        assert len(jobs) == 0

    async def test_delete_jobs_older_than(self, admin_api, db_connection):
        """Test deleting jobs older than N days"""
        # Create old job (manually set updated timestamp)
        job_id = await create_test_job(db_connection, state='finished')
        await db_connection.execute("""
            UPDATE jorb
            SET updated = updated - interval '31 days'
            WHERE id = $1
        """, job_id)

        # Delete jobs older than 30 days
        count = await admin_api.delete_jobs(state='finished', older_than_days=30)

        assert count >= 1


class TestQueueManagement:
    """Tests for queue management API"""

    async def test_list_queues(self, admin_api, db_connection):
        """Test listing all queues"""
        # Create jobs in different queues
        await create_test_job(db_connection, queue='default')
        await create_test_job(db_connection, queue='priority')
        await create_test_job(db_connection, queue='batch')

        # List queues
        queues = await admin_api.list_queues()

        assert 'default' in queues
        assert 'priority' in queues
        assert 'batch' in queues

    async def test_queue_stats_all(self, admin_api, db_connection):
        """Test getting stats for all queues"""
        # Create jobs in different states
        await create_test_job(db_connection, queue='test', state='queued')
        await create_test_job(db_connection, queue='test', state='queued')
        await create_test_job(db_connection, queue='test', state='running')
        await create_test_job(db_connection, queue='test', state='finished')

        # Get stats
        stats = await admin_api.queue_stats()

        # Find test queue stats
        test_stats = next(s for s in stats if s['queue'] == 'test')

        assert test_stats['queued'] == 2
        assert test_stats['running'] == 1
        assert test_stats['finished'] == 1
        assert test_stats['total'] == 4

    async def test_queue_stats_specific(self, admin_api, db_connection):
        """Test getting stats for specific queue"""
        # Create jobs
        await create_test_job(db_connection, queue='myqueue', state='queued')
        await create_test_job(db_connection, queue='myqueue', state='crashed')
        await create_test_job(db_connection, queue='otherqueue', state='queued')

        # Get stats for specific queue
        stats = await admin_api.queue_stats(queue='myqueue')

        assert len(stats) == 1
        assert stats[0]['queue'] == 'myqueue'
        assert stats[0]['queued'] == 1
        assert stats[0]['crashed'] == 1
        assert stats[0]['total'] == 2

    async def test_queue_stats_oldest_age(self, admin_api, db_connection):
        """Test oldest queued job age calculation"""
        # Create queued job
        await create_test_job(db_connection, queue='test', state='queued')

        # Get stats
        stats = await admin_api.queue_stats(queue='test')

        assert len(stats) == 1
        assert stats[0]['oldest_queued_age_seconds'] is not None
        assert stats[0]['oldest_queued_age_seconds'] >= 0

    async def test_clear_queue(self, admin_api, db_connection):
        """Test clearing queue"""
        # Create jobs in queue
        for i in range(5):
            await create_test_job(db_connection, queue='clear-test', state='finished')

        # Clear queue
        count = await admin_api.clear_queue(queue='clear-test')

        assert count == 5

        # Verify queue is empty
        jobs = await admin_api.list_jobs(queue='clear-test')
        assert len(jobs) == 0

    async def test_clear_queue_by_state(self, admin_api, db_connection):
        """Test clearing queue filtered by state"""
        # Create jobs in different states
        await create_test_job(db_connection, queue='test', state='finished')
        await create_test_job(db_connection, queue='test', state='crashed')

        # Clear only finished jobs
        count = await admin_api.clear_queue(queue='test', state='finished')

        assert count == 1

        # Crashed job should still exist
        jobs = await admin_api.list_jobs(queue='test', state='crashed')
        assert len(jobs) == 1


class TestWorkerManagement:
    """Tests for worker management API"""

    async def test_list_workers_empty(self, admin_api):
        """Test listing workers when none active"""
        workers = await admin_api.list_workers()
        assert isinstance(workers, list)

    async def test_list_workers_active(self, admin_api, db_connection):
        """Test listing active workers"""
        # Create claimed/running jobs
        await db_connection.execute("""
            INSERT INTO jorb (job_class, kwargs, queue, state, worker_host, worker_pid)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, 'job.test.TestJob', '{}', 'default', 'claimed', 'worker-1', 12345)

        await db_connection.execute("""
            INSERT INTO jorb (job_class, kwargs, queue, state, worker_host, worker_pid)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, 'job.test.TestJob', '{}', 'default', 'running', 'worker-2', 67890)

        # List workers
        workers = await admin_api.list_workers()

        assert len(workers) >= 2
        hosts = [w['worker_host'] for w in workers]
        assert 'worker-1' in hosts
        assert 'worker-2' in hosts

    async def test_worker_stats(self, admin_api, db_connection):
        """Test worker statistics"""
        # Create jobs for workers
        await db_connection.execute("""
            INSERT INTO jorb (job_class, kwargs, queue, state, worker_host, worker_pid)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, 'job.test.TestJob', '{}', 'default', 'running', 'worker-1', 100)

        await db_connection.execute("""
            INSERT INTO jorb (job_class, kwargs, queue, state, worker_host, worker_pid)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, 'job.test.TestJob', '{}', 'default', 'running', 'worker-1', 100)

        # Get stats
        stats = await admin_api.worker_stats()

        assert stats['active_workers'] >= 1
        assert len(stats['workers']) >= 1


class TestMetrics:
    """Tests for metrics API"""

    async def test_get_metrics_basic(self, admin_api, db_connection):
        """Test getting basic metrics"""
        # Create jobs
        await create_test_job(db_connection, state='finished')
        await create_test_job(db_connection, state='crashed')

        # Get metrics
        metrics = await admin_api.get_metrics()

        assert 'period_start' in metrics
        assert 'period_end' in metrics
        assert 'state_counts' in metrics
        assert 'finished_count' in metrics
        assert 'crashed_count' in metrics

    async def test_get_metrics_filtered_queue(self, admin_api, db_connection):
        """Test metrics filtered by queue"""
        # Create jobs in different queues
        await create_test_job(db_connection, queue='test1', state='finished')
        await create_test_job(db_connection, queue='test2', state='finished')

        # Get metrics for specific queue
        metrics = await admin_api.get_metrics(queue='test1')

        assert metrics['queue'] == 'test1'

    async def test_get_metrics_top_errors(self, admin_api, db_connection):
        """Test top errors in metrics"""
        # Create crashed jobs
        for i in range(3):
            await db_connection.execute("""
                INSERT INTO jorb (job_class, kwargs, queue, state, error_count, error_message)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, 'job.test.FailJob', '{}', 'default', 'crashed', 5, f'Error {i}')

        # Get metrics
        metrics = await admin_api.get_metrics()

        assert 'top_errors' in metrics
        assert len(metrics['top_errors']) > 0


class TestDeadLetterQueue:
    """Tests for Dead Letter Queue API"""

    async def test_list_dlq(self, admin_api, db_connection):
        """Test listing DLQ jobs"""
        # Create permanently failed job (error_count >= 10)
        await create_test_job(
            db_connection,
            state='crashed',
            error_count=10,
            job_class='job.test.PermanentFail'
        )

        # Create regular crashed job
        await create_test_job(
            db_connection,
            state='crashed',
            error_count=3
        )

        # List DLQ
        dlq_jobs = await admin_api.list_dlq()

        assert len(dlq_jobs) >= 1
        assert all(j['error_count'] >= 10 for j in dlq_jobs)
        assert all(j['state'] == 'crashed' for j in dlq_jobs)

    async def test_retry_from_dlq(self, admin_api, db_connection):
        """Test retrying job from DLQ"""
        # Create DLQ job
        job_id = await create_test_job(
            db_connection,
            state='crashed',
            error_count=10
        )

        # Retry from DLQ
        result = await admin_api.retry_from_dlq(job_id)

        assert result['original_job_id'] == job_id
        assert result['new_job_id'] != job_id
        assert result['status'] == 'retry_queued_from_dlq'

        # Check new job has reset error_count
        new_job = await admin_api.get_job(result['new_job_id'])
        assert new_job['error_count'] == 0
        assert new_job['state'] == 'queued'
        assert new_job['admin_data']['dlq_retry_from'] == job_id
