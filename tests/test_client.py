"""
Comprehensive tests for client.py - Job client library.
Using LIVE database operations with NO MOCKS for maximum correctness guarantees!
"""

import pytest
import asyncio
import asyncpg
import uuid
from datetime import datetime, timedelta
from pyjobby.client import (
    JobOptions,
    JobInfo,
    JobClient,
)


def unique_name(base: str) -> str:
    """Generate unique name for test isolation."""
    return f"{base}_{uuid.uuid4().hex[:8]}"


class TestJobOptionsDataclass:
    """Test JobOptions dataclass - covers lines 48-74."""

    def test_job_options_defaults(self):
        """Test default values."""
        options = JobOptions()
        assert options.queue == 'default'
        assert options.priority == 100
        assert options.run_after is None
        assert options.capability is None
        assert options.uid is None
        assert options.run_group is None
        assert options.waitfor_job is None
        assert options.waitfor_group is None
        assert options.deadline_key is None
        assert options.admin_data is None

    def test_job_options_custom(self):
        """Test custom values."""
        options = JobOptions(
            queue='priority',
            priority=500,
            capability='gpu',
            uid=42
        )
        assert options.queue == 'priority'
        assert options.priority == 500
        assert options.capability == 'gpu'
        assert options.uid == 42


class TestJobInfoDataclass:
    """Test JobInfo dataclass - covers lines 77-85."""

    def test_job_info_creation(self):
        """Test JobInfo creation."""
        now = datetime.utcnow()
        info = JobInfo(
            id=123,
            job_class='TestJob',
            queue='default',
            priority=100,
            state='queued',
            created=now
        )
        assert info.id == 123
        assert info.job_class == 'TestJob'
        assert info.state == 'queued'


class TestJobClientBasics:
    """Test JobClient basic functionality."""

    @pytest.mark.asyncio
    async def test_client_init(self, db_pool):
        """Test client initialization - covers lines 108-118."""
        client = JobClient(db_pool)
        assert client.pool == db_pool
        assert client._closed is False

    @pytest.mark.asyncio
    async def test_client_close(self, db_pool, db_params):
        """Test client close - covers lines 196-200."""
        # Create a new pool just for this test
        new_pool = await asyncpg.create_pool(**db_params, min_size=1, max_size=2)
        client = JobClient(new_pool)
        
        assert client._closed is False
        await client.close()
        assert client._closed is True
        
        # Close again should be safe (idempotent)
        await client.close()
        assert client._closed is True

    @pytest.mark.asyncio
    async def test_client_context_manager(self, db_pool):
        """Test context manager - covers lines 202-208."""
        async with JobClient(db_pool) as client:
            assert isinstance(client, JobClient)
            # Don't actually close since we're using shared pool


class TestJobClientEnqueue:
    """Test JobClient.enqueue method - covers lines 214-388."""

    @pytest.mark.asyncio
    async def test_enqueue_simple_job(self, db_pool):
        """Test simple job enqueueing."""
        client = JobClient(db_pool)
        
        job_id = await client.enqueue('TestJob', arg1='value1', arg2=42)
        
        assert job_id is not None
        assert isinstance(job_id, int)
        
        # Verify job was created
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert row['job_class'] == 'TestJob'
            assert row['state'] == 'queued'
            assert row['kwargs']['arg1'] == 'value1'

    @pytest.mark.asyncio
    async def test_enqueue_with_queue(self, db_pool):
        """Test enqueueing to specific queue."""
        client = JobClient(db_pool)
        queue_name = unique_name('test_queue')
        
        job_id = await client.enqueue('QueuedJob', queue=queue_name)
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT queue FROM jorb WHERE id = $1", job_id)
            assert row['queue'] == queue_name

    @pytest.mark.asyncio
    async def test_enqueue_with_priority(self, db_pool):
        """Test enqueueing with priority."""
        client = JobClient(db_pool)
        
        job_id = await client.enqueue('PriorityJob', priority=500)
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT prio FROM jorb WHERE id = $1", job_id)
            assert row['prio'] == 500

    @pytest.mark.asyncio
    async def test_enqueue_with_run_after(self, db_pool):
        """Test enqueueing with scheduled time."""
        client = JobClient(db_pool)
        future_time = datetime.utcnow() + timedelta(hours=1)
        
        job_id = await client.enqueue('ScheduledJob', run_after=future_time)
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT run_after FROM jorb WHERE id = $1", job_id)
            assert row['run_after'] is not None

    @pytest.mark.asyncio
    async def test_enqueue_with_waitfor_job(self, db_pool):
        """Test enqueueing with job dependency - covers lines 337-340."""
        client = JobClient(db_pool)
        
        # Create first job
        job1_id = await client.enqueue('Job1')
        
        # Create dependent job
        job2_id = await client.enqueue('Job2', waitfor_job=job1_id)
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT state, waitfor_job FROM jorb WHERE id = $1", job2_id)
            assert row['state'] == 'waiting'
            assert row['waitfor_job'] == job1_id

    @pytest.mark.asyncio
    async def test_enqueue_both_waitfor_raises(self, db_pool):
        """Test that both waitfor_job and waitfor_group raises - covers lines 319-320."""
        client = JobClient(db_pool)
        
        with pytest.raises(ValueError) as excinfo:
            await client.enqueue('BothWaitJob', waitfor_job=1, waitfor_group=2)
        assert 'Cannot specify both' in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_enqueue_with_retry_strategy(self, db_pool):
        """Test enqueueing with retry strategy - covers lines 350-354."""
        client = JobClient(db_pool)
        
        job_id = await client.enqueue(
            'RetryJob',
            retry_strategy='linear',
            max_retries=5,
            initial_retry_delay=10
        )
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT admin_data FROM jorb WHERE id = $1", job_id)
            admin = row['admin_data']
            assert admin['retry_strategy'] == 'linear'
            assert admin['max_retries'] == 5
            assert admin['initial_retry_delay'] == 10

    @pytest.mark.asyncio
    async def test_enqueue_with_timeout(self, db_pool):
        """Test enqueueing with timeout - covers lines 356-359."""
        client = JobClient(db_pool)
        
        job_id = await client.enqueue(
            'TimeoutJob',
            timeout_seconds=30,
            on_timeout='fail'
        )
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT admin_data FROM jorb WHERE id = $1", job_id)
            admin = row['admin_data']
            assert admin['timeout_seconds'] == 30
            assert admin['on_timeout'] == 'fail'


class TestJobClientEnqueueBatch:
    """Test JobClient.enqueue_batch method - covers lines 390-477."""

    @pytest.mark.asyncio
    async def test_enqueue_batch_empty(self, db_pool):
        """Test batch enqueueing empty list - covers lines 426-427."""
        client = JobClient(db_pool)
        
        job_ids = await client.enqueue_batch([])
        
        assert job_ids == []

    @pytest.mark.asyncio
    async def test_enqueue_batch_multiple(self, db_pool):
        """Test batch enqueueing multiple jobs."""
        client = JobClient(db_pool)
        
        jobs = [
            ('BatchJob1', {'index': 0}),
            ('BatchJob2', {'index': 1}),
            ('BatchJob3', {'index': 2}),
        ]
        
        job_ids = await client.enqueue_batch(jobs)
        
        assert len(job_ids) == 3
        for jid in job_ids:
            assert isinstance(jid, int)

    @pytest.mark.asyncio
    async def test_enqueue_batch_with_queue(self, db_pool):
        """Test batch enqueueing to specific queue."""
        client = JobClient(db_pool)
        queue_name = unique_name('batch_queue')
        
        jobs = [('BatchJob', {'i': i}) for i in range(3)]
        job_ids = await client.enqueue_batch(jobs, queue=queue_name)
        
        async with db_pool.acquire() as conn:
            for jid in job_ids:
                row = await conn.fetchrow("SELECT queue FROM jorb WHERE id = $1", jid)
                assert row['queue'] == queue_name


class TestJobClientJobManagement:
    """Test job inspection and management methods."""

    @pytest.mark.asyncio
    async def test_get_job(self, db_pool):
        """Test get_job - covers lines 483-508."""
        client = JobClient(db_pool)
        
        # Create job
        job_id = await client.enqueue('GetTestJob')
        
        # Get job
        job_info = await client.get_job(job_id)
        
        assert job_info is not None
        assert job_info.id == job_id
        assert job_info.job_class == 'GetTestJob'
        assert job_info.state == 'queued'

    @pytest.mark.asyncio
    async def test_get_job_not_found(self, db_pool):
        """Test get_job with non-existent ID - covers lines 505-506."""
        client = JobClient(db_pool)
        
        job_info = await client.get_job(-99999)
        
        assert job_info is None

    @pytest.mark.asyncio
    async def test_cancel_job(self, db_pool):
        """Test cancel_job - covers lines 510-532."""
        client = JobClient(db_pool)
        
        # Create job
        job_id = await client.enqueue('CancelTestJob')
        
        # Cancel
        result = await client.cancel_job(job_id)
        
        assert result is True
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT state FROM jorb WHERE id = $1", job_id)
            assert row['state'] == 'cancelled'

    @pytest.mark.asyncio
    async def test_cancel_job_not_found(self, db_pool):
        """Test cancel_job with non-existent ID."""
        client = JobClient(db_pool)
        
        result = await client.cancel_job(-99999)
        
        assert result is False

    @pytest.mark.asyncio
    async def test_retry_job(self, db_pool):
        """Test retry_job - covers lines 534-567."""
        client = JobClient(db_pool)
        
        # Create and crash a job
        job_id = await client.enqueue('RetryTestJob')
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE jorb SET state = 'crashed' WHERE id = $1", job_id)
        
        # Retry
        new_job_id = await client.retry_job(job_id)
        
        assert new_job_id is not None
        assert new_job_id != job_id
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT admin_data FROM jorb WHERE id = $1", new_job_id)
            assert row['admin_data']['retry_of'] == job_id


class TestJobClientQueueOperations:
    """Test queue operation methods."""

    @pytest.mark.asyncio
    async def test_queue_depth(self, db_pool):
        """Test queue_depth - covers lines 573-593."""
        client = JobClient(db_pool)
        queue_name = unique_name('depth_queue')
        
        # Create some jobs
        for _ in range(5):
            await client.enqueue('DepthJob', queue=queue_name)
        
        depth = await client.queue_depth(queue_name)
        
        assert depth == 5

    @pytest.mark.asyncio
    async def test_queue_stats(self, db_pool):
        """Test queue_stats - covers lines 595-623."""
        client = JobClient(db_pool)
        queue_name = unique_name('stats_queue')
        
        # Create jobs in different states
        job_id = await client.enqueue('StatsJob', queue=queue_name)
        await client.enqueue('StatsJob2', queue=queue_name)
        
        stats = await client.queue_stats(queue_name)
        
        assert isinstance(stats, dict)
        assert 'queued' in stats
        assert stats['queued'] >= 2

    @pytest.mark.asyncio
    async def test_list_queues(self, db_pool):
        """Test list_queues - covers lines 625-654."""
        client = JobClient(db_pool)
        
        queues = await client.list_queues()
        
        assert isinstance(queues, list)
        # Should have at least the default queue
        for q in queues:
            assert 'queue' in q
            assert 'total' in q

    @pytest.mark.asyncio
    async def test_purge_queue(self, db_pool):
        """Test purge_queue - covers lines 656-685."""
        client = JobClient(db_pool)
        queue_name = unique_name('purge_queue')
        
        # Create jobs
        for _ in range(3):
            await client.enqueue('PurgeJob', queue=queue_name)
        
        # Purge
        deleted = await client.purge_queue(queue_name)
        
        assert deleted == 3
        
        # Verify queue is empty
        depth = await client.queue_depth(queue_name)
        assert depth == 0


class TestJobClientBulkOperations:
    """Test bulk operation methods."""

    @pytest.mark.asyncio
    async def test_bulk_cancel_empty(self, db_pool):
        """Test bulk_cancel with empty list - covers lines 1042-1043."""
        client = JobClient(db_pool)
        
        result = await client.bulk_cancel([])
        
        assert result == 0

    @pytest.mark.asyncio
    async def test_bulk_cancel_multiple(self, db_pool):
        """Test bulk_cancel - covers lines 1028-1053."""
        client = JobClient(db_pool)
        
        # Create jobs
        job_ids = []
        for _ in range(3):
            jid = await client.enqueue('BulkCancelJob')
            job_ids.append(jid)
        
        # Bulk cancel
        cancelled = await client.bulk_cancel(job_ids)
        
        assert cancelled == 3

    @pytest.mark.asyncio
    async def test_bulk_retry_empty(self, db_pool):
        """Test bulk_retry with empty list - covers lines 1069-1070."""
        client = JobClient(db_pool)
        
        result = await client.bulk_retry([])
        
        assert result == []

    @pytest.mark.asyncio
    async def test_bulk_delete_empty(self, db_pool):
        """Test bulk_delete with empty list - covers lines 1106-1107."""
        client = JobClient(db_pool)
        
        result = await client.bulk_delete([])
        
        assert result == 0

    @pytest.mark.asyncio
    async def test_bulk_delete_multiple(self, db_pool):
        """Test bulk_delete - covers lines 1092-1115."""
        client = JobClient(db_pool)
        
        # Create jobs
        job_ids = []
        for _ in range(3):
            jid = await client.enqueue('BulkDeleteJob')
            job_ids.append(jid)
        
        # Bulk delete
        deleted = await client.bulk_delete(job_ids)
        
        assert deleted == 3

    @pytest.mark.asyncio
    async def test_bulk_update_priority_empty(self, db_pool):
        """Test bulk_update_priority with empty list - covers lines 1132-1133."""
        client = JobClient(db_pool)
        
        result = await client.bulk_update_priority([], 500)
        
        assert result == 0


class TestJobClientAdvancedFeatures:
    """Test advanced features."""

    @pytest.mark.asyncio
    async def test_create_pipeline(self, db_pool):
        """Test create_pipeline - covers lines 1149-1194."""
        client = JobClient(db_pool)
        
        steps = [
            ('PipelineStep1', {'data': 'a'}),
            ('PipelineStep2', {'data': 'b'}),
            ('PipelineStep3', {'data': 'c'}),
        ]
        
        job_ids = await client.create_pipeline(steps)
        
        assert len(job_ids) == 3
        
        # Verify dependencies
        async with db_pool.acquire() as conn:
            row2 = await conn.fetchrow("SELECT waitfor_job FROM jorb WHERE id = $1", job_ids[1])
            assert row2['waitfor_job'] == job_ids[0]
            
            row3 = await conn.fetchrow("SELECT waitfor_job FROM jorb WHERE id = $1", job_ids[2])
            assert row3['waitfor_job'] == job_ids[1]

    @pytest.mark.asyncio
    async def test_create_pipeline_empty(self, db_pool):
        """Test create_pipeline with empty list - covers lines 1177-1178."""
        client = JobClient(db_pool)
        
        job_ids = await client.create_pipeline([])
        
        assert job_ids == []

    @pytest.mark.asyncio
    async def test_create_fan_out(self, db_pool):
        """Test create_fan_out - covers lines 1196-1245."""
        client = JobClient(db_pool)
        
        items = [{'item_id': i} for i in range(5)]
        
        job_ids, run_group = await client.create_fan_out('FanOutJob', items)
        
        assert len(job_ids) == 5
        assert run_group is not None
        
        # Verify all jobs are in same run_group
        async with db_pool.acquire() as conn:
            for jid in job_ids:
                row = await conn.fetchrow("SELECT run_group FROM jorb WHERE id = $1", jid)
                assert row['run_group'] == run_group

    @pytest.mark.asyncio
    async def test_health_check(self, db_pool):
        """Test health_check - covers lines 1247-1263."""
        client = JobClient(db_pool)
        
        result = await client.health_check()
        
        assert result is True

    @pytest.mark.asyncio
    async def test_dag_builder(self, db_pool):
        """Test dag method - covers lines 1269-1302."""
        client = JobClient(db_pool)
        
        dag = client.dag(name='TestDAG', queue='dag_queue')
        
        from pyjobby.dag import DAGBuilder
        assert isinstance(dag, DAGBuilder)
        assert dag.name == 'TestDAG'
        assert dag.common_options.get('queue') == 'dag_queue'


class TestJobClientGetJobs:
    """Test get_jobs and search_jobs methods."""

    @pytest.mark.asyncio
    async def test_get_jobs_basic(self, db_pool):
        """Test get_jobs - covers lines 798-864."""
        client = JobClient(db_pool)
        queue_name = unique_name('get_jobs_queue')
        
        # Create some jobs
        for i in range(5):
            await client.enqueue(f'GetJobsTest{i}', queue=queue_name)
        
        jobs = await client.get_jobs(queue=queue_name, limit=10)
        
        assert len(jobs) == 5

    @pytest.mark.asyncio
    async def test_get_jobs_with_state(self, db_pool):
        """Test get_jobs with state filter."""
        client = JobClient(db_pool)
        
        jobs = await client.get_jobs(state='queued', limit=10)
        
        for job in jobs:
            assert job['state'] == 'queued'

    @pytest.mark.asyncio
    async def test_search_jobs(self, db_pool):
        """Test search_jobs - covers lines 866-959."""
        client = JobClient(db_pool)
        
        # Create job
        job_id = await client.enqueue('SearchableJob', uid=12345)
        
        jobs = await client.search_jobs(job_class='SearchableJob', uid=12345)
        
        assert len(jobs) >= 1
        assert any(j['id'] == job_id for j in jobs)

    @pytest.mark.asyncio
    async def test_get_failed_jobs(self, db_pool):
        """Test get_failed_jobs - covers lines 961-996."""
        client = JobClient(db_pool)
        
        jobs = await client.get_failed_jobs(limit=10)
        
        assert isinstance(jobs, list)
        for job in jobs:
            assert job['state'] == 'crashed'

    @pytest.mark.asyncio
    async def test_get_waiting_jobs(self, db_pool):
        """Test get_waiting_jobs - covers lines 998-1022."""
        client = JobClient(db_pool)
        
        jobs = await client.get_waiting_jobs(limit=10)
        
        assert isinstance(jobs, list)
        for job in jobs:
            assert job['state'] == 'waiting'


class TestJobClientFullJob:
    """Test get_job_full and related methods."""

    @pytest.mark.asyncio
    async def test_get_job_full(self, db_pool):
        """Test get_job_full - covers lines 691-717."""
        client = JobClient(db_pool)
        
        job_id = await client.enqueue('FullJob', test_arg='test_value')
        
        job = await client.get_job_full(job_id)
        
        assert job is not None
        assert job['id'] == job_id
        assert 'kwargs' in job
        assert job['kwargs']['test_arg'] == 'test_value'

    @pytest.mark.asyncio
    async def test_get_job_full_not_found(self, db_pool):
        """Test get_job_full with non-existent ID - covers lines 714-715."""
        client = JobClient(db_pool)
        
        job = await client.get_job_full(-99999)
        
        assert job is None

    @pytest.mark.asyncio
    async def test_delete_job(self, db_pool):
        """Test delete_job - covers lines 750-770."""
        client = JobClient(db_pool)
        
        job_id = await client.enqueue('DeleteJob')
        
        result = await client.delete_job(job_id)
        
        assert result is True
        
        # Verify deleted
        job = await client.get_job(job_id)
        assert job is None

    @pytest.mark.asyncio
    async def test_update_job_priority(self, db_pool):
        """Test update_job_priority - covers lines 772-796."""
        client = JobClient(db_pool)
        
        job_id = await client.enqueue('PrioUpdateJob', priority=100)
        
        result = await client.update_job_priority(job_id, 500)
        
        assert result is True
        
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT prio FROM jorb WHERE id = $1", job_id)
            assert row['prio'] == 500
