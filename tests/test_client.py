#!/usr/bin/env python3
"""
Comprehensive tests for the JobClient library.

Tests all client functionality including:
- Connection pooling and lifecycle
- Job enqueueing (simple, scheduled, pipelines)
- Batch operations
- Job management (cancel, retry)
- Queue operations
- Advanced patterns (pipeline, fan-out)
- Error handling
"""

import pytest
import asyncpg
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any

from pyjobby.client import JobClient, JobOptions, JobInfo


# =============================================================================
# Fixtures
# =============================================================================

# NOTE: We use the db_pool and client fixtures from conftest.py
# which have proper JSON codec setup for asyncpg 0.30.0+

@pytest.fixture
async def pool(db_pool):
    """Alias for db_pool to match test function signatures"""
    return db_pool


@pytest.fixture
async def clean_db(db_pool):
    """Clean database before each test"""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM jorb")
        await conn.execute("DELETE FROM jorb_schedule_log")
        await conn.execute("DELETE FROM jorb_schedule")


# =============================================================================
# Client Lifecycle Tests
# =============================================================================

@pytest.mark.asyncio
async def test_client_create(db_params):
    """Test creating client with connection details"""
    client = await JobClient.create(**db_params, min_size=2, max_size=5)

    assert client.pool is not None
    assert not client._closed

    # Test that pool works
    async with client.pool.acquire() as conn:
        result = await conn.fetchval("SELECT 1")
        assert result == 1

    await client.close()
    assert client._closed


@pytest.mark.asyncio
async def test_client_context_manager(db_params):
    """Test using client as context manager"""
    async with await JobClient.create(**db_params) as client:
        assert not client._closed

        # Can use client normally
        await client.health_check()

    # Client should be closed after exiting context
    assert client._closed


@pytest.mark.asyncio
async def test_health_check(client):
    """Test health check returns True when database is accessible"""
    healthy = await client.health_check()
    assert healthy is True


# =============================================================================
# Basic Job Enqueueing Tests
# =============================================================================

@pytest.mark.asyncio
async def test_enqueue_simple_job(client, clean_db, pool):
    """Test enqueueing a simple job"""
    job_id = await client.enqueue('test.SimpleJob', arg1='value1', arg2=123)

    assert isinstance(job_id, int)
    assert job_id > 0

    # Verify job was created correctly
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        import json
        kwargs = json.loads(row['kwargs']) if isinstance(row['kwargs'], str) else row['kwargs']
        assert row['job_class'] == 'test.SimpleJob'
        assert kwargs['arg1'] == 'value1'
        assert kwargs['arg2'] == 123
        assert row['queue'] == 'default'
        assert row['prio'] == 100
        assert row['state'] == 'queued'


@pytest.mark.asyncio
async def test_enqueue_with_queue_and_priority(client, clean_db, pool):
    """Test enqueueing job with custom queue and priority"""
    job_id = await client.enqueue(
        'test.EmailJob',
        queue='emails',
        priority=200,
        to='user@example.com'
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row['queue'] == 'emails'
        assert row['prio'] == 200


@pytest.mark.asyncio
async def test_enqueue_scheduled_job(client, clean_db, pool):
    """Test enqueueing a job with run_after"""
    future_time = datetime.utcnow() + timedelta(hours=1)

    job_id = await client.enqueue(
        'test.ScheduledJob',
        run_after=future_time,
        task='cleanup'
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        # Times should be close (within 1 second due to database rounding)
        assert abs((row['run_after'] - future_time).total_seconds()) < 1


@pytest.mark.asyncio
async def test_enqueue_with_capability(client, clean_db, pool):
    """Test enqueueing job with capability requirement"""
    job_id = await client.enqueue(
        'test.GPUJob',
        capability='gpu',
        model='resnet50'
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert row['capability'] == 'gpu'


@pytest.mark.asyncio
async def test_enqueue_with_deadline_key(client, clean_db, pool):
    """Test enqueueing job with deadline_key for idempotency"""
    job_id1 = await client.enqueue(
        'test.PaymentJob',
        deadline_key='payment:12345',
        payment_id=12345
    )

    # Second enqueue with same deadline_key should fail
    with pytest.raises(asyncpg.UniqueViolationError):
        await client.enqueue(
            'test.PaymentJob',
            deadline_key='payment:12345',
            payment_id=12345
        )

    # Different deadline_key should work
    job_id2 = await client.enqueue(
        'test.PaymentJob',
        deadline_key='payment:67890',
        payment_id=67890
    )

    assert job_id2 != job_id1


@pytest.mark.asyncio
async def test_enqueue_with_admin_data(client, clean_db, pool):
    """Test enqueueing job with admin_data metadata"""
    admin_data = {'user_id': 123, 'request_id': 'abc-def'}

    job_id = await client.enqueue(
        'test.TrackedJob',
        admin_data=admin_data,
        task='process'
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        import json
        admin_data = json.loads(row['admin_data']) if isinstance(row['admin_data'], str) else row['admin_data']
        assert admin_data['user_id'] == 123
        assert admin_data['request_id'] == 'abc-def'


# =============================================================================
# Job Pipeline Tests
# =============================================================================

@pytest.mark.asyncio
async def test_enqueue_with_waitfor_job(client, clean_db, pool):
    """Test enqueueing job that waits for another job"""
    job1 = await client.enqueue('test.Step1', data='input')
    job2 = await client.enqueue('test.Step2', waitfor_job=job1, data='output')

    async with pool.acquire() as conn:
        row1 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job1)
        row2 = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job2)

        assert row1['state'] == 'queued'
        assert row2['state'] == 'waiting'
        assert row2['waitfor_job'] == job1


@pytest.mark.asyncio
async def test_enqueue_cannot_specify_both_waitfor(client, clean_db):
    """Test that specifying both waitfor_job and waitfor_group raises error"""
    with pytest.raises(ValueError, match="Cannot specify both"):
        await client.enqueue(
            'test.Job',
            waitfor_job=1,
            waitfor_group=2
        )


@pytest.mark.asyncio
async def test_create_pipeline(client, clean_db, pool):
    """Test creating a job pipeline"""
    job_ids = await client.create_pipeline([
        ('test.FetchData', {'source': 'api'}),
        ('test.TransformData', {'format': 'json'}),
        ('test.LoadData', {'destination': 'db'}),
    ])

    assert len(job_ids) == 3
    assert job_ids[0] != job_ids[1] != job_ids[2]

    # Verify pipeline dependencies
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, state, waitfor_job
            FROM jorb
            WHERE id = ANY($1::bigint[])
            ORDER BY id
        """, job_ids)

        # First job should be queued with no dependencies
        assert rows[0]['state'] == 'queued'
        assert rows[0]['waitfor_job'] is None

        # Second job should wait for first
        assert rows[1]['state'] == 'waiting'
        assert rows[1]['waitfor_job'] == job_ids[0]

        # Third job should wait for second
        assert rows[2]['state'] == 'waiting'
        assert rows[2]['waitfor_job'] == job_ids[1]


@pytest.mark.asyncio
async def test_create_pipeline_empty(client, clean_db):
    """Test creating empty pipeline returns empty list"""
    job_ids = await client.create_pipeline([])
    assert job_ids == []


# =============================================================================
# Fan-Out Pattern Tests
# =============================================================================

@pytest.mark.asyncio
async def test_create_fan_out(client, clean_db, pool):
    """Test creating fan-out pattern"""
    items = [
        {'order_id': 1, 'amount': 100},
        {'order_id': 2, 'amount': 200},
        {'order_id': 3, 'amount': 300},
    ]

    job_ids, group_id = await client.create_fan_out(
        'test.ProcessOrder',
        items,
        queue='processing',
        priority=150
    )

    assert len(job_ids) == 3
    assert isinstance(group_id, int)

    # Verify all jobs have same run_group
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, run_group, queue, prio, kwargs
            FROM jorb
            WHERE id = ANY($1::bigint[])
            ORDER BY id
        """, job_ids)

        for i, row in enumerate(rows):
            import json
            kwargs = json.loads(row['kwargs']) if isinstance(row['kwargs'], str) else row['kwargs']
            assert row['run_group'] == group_id
            assert row['queue'] == 'processing'
            assert row['prio'] == 150
            assert kwargs['order_id'] == items[i]['order_id']


@pytest.mark.asyncio
async def test_fan_out_with_fan_in(client, clean_db, pool):
    """Test fan-out pattern with fan-in (waitfor_group)"""
    items = [{'item_id': i} for i in range(10)]

    # Create fan-out
    job_ids, group_id = await client.create_fan_out(
        'test.ProcessItem',
        items
    )

    # Create fan-in job that waits for all
    summary_job = await client.enqueue(
        'test.SummarizeResults',
        waitfor_group=group_id
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", summary_job)
        assert row['state'] == 'waiting'
        assert row['waitfor_group'] == group_id


# =============================================================================
# Batch Operations Tests
# =============================================================================

@pytest.mark.asyncio
async def test_enqueue_batch(client, clean_db, pool):
    """Test batch enqueueing jobs"""
    jobs = [
        ('test.Job1', {'arg': 1}),
        ('test.Job2', {'arg': 2}),
        ('test.Job3', {'arg': 3}),
    ]

    job_ids = await client.enqueue_batch(jobs, queue='batch', priority=150)

    assert len(job_ids) == 3

    # Verify all jobs created correctly
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, job_class, kwargs, queue, prio
            FROM jorb
            WHERE id = ANY($1::bigint[])
            ORDER BY id
        """, job_ids)

        for i, row in enumerate(rows):
            import json
            kwargs = json.loads(row['kwargs']) if isinstance(row['kwargs'], str) else row['kwargs']
            assert row['job_class'] == jobs[i][0]
            assert kwargs['arg'] == jobs[i][1]['arg']
            assert row['queue'] == 'batch'
            assert row['prio'] == 150


@pytest.mark.asyncio
async def test_enqueue_batch_large(client, clean_db, pool):
    """Test batch enqueueing large number of jobs (performance test)"""
    # Create 1000 jobs
    jobs = [
        ('test.ProcessItem', {'item_id': i})
        for i in range(1000)
    ]

    job_ids = await client.enqueue_batch(jobs)

    assert len(job_ids) == 1000

    # Verify count in database
    async with pool.acquire() as conn:
        count = await conn.fetchval("""
            SELECT COUNT(*) FROM jorb
            WHERE id = ANY($1::bigint[])
        """, job_ids)
        assert count == 1000


@pytest.mark.asyncio
async def test_enqueue_batch_empty(client, clean_db):
    """Test batch enqueueing empty list returns empty list"""
    job_ids = await client.enqueue_batch([])
    assert job_ids == []


@pytest.mark.asyncio
async def test_enqueue_batch_with_run_group(client, clean_db, pool):
    """Test batch enqueueing with run_group"""
    jobs = [('test.Job', {'i': i}) for i in range(5)]

    job_ids = await client.enqueue_batch(jobs, run_group=999)

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT run_group FROM jorb
            WHERE id = ANY($1::bigint[])
        """, job_ids)

        # All should have same run_group
        for row in rows:
            assert row['run_group'] == 999


# =============================================================================
# Job Management Tests
# =============================================================================

@pytest.mark.asyncio
async def test_get_job(client, clean_db, pool):
    """Test getting job information"""
    job_id = await client.enqueue('test.TestJob', arg='value')

    job_info = await client.get_job(job_id)

    assert job_info is not None
    assert isinstance(job_info, JobInfo)
    assert job_info.id == job_id
    assert job_info.job_class == 'test.TestJob'
    assert job_info.queue == 'default'
    assert job_info.priority == 100
    assert job_info.state == 'queued'
    assert isinstance(job_info.created, datetime)


@pytest.mark.asyncio
async def test_get_job_not_found(client, clean_db):
    """Test getting non-existent job returns None"""
    job_info = await client.get_job(99999)
    assert job_info is None


@pytest.mark.asyncio
async def test_cancel_job_queued(client, clean_db, pool):
    """Test cancelling a queued job"""
    job_id = await client.enqueue('test.TestJob')

    result = await client.cancel_job(job_id)
    assert result is True

    # Verify state changed to cancelled
    async with pool.acquire() as conn:
        state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
        assert state == 'cancelled'


@pytest.mark.asyncio
async def test_cancel_job_waiting(client, clean_db, pool):
    """Test cancelling a waiting job"""
    job1 = await client.enqueue('test.Job1')
    job2 = await client.enqueue('test.Job2', waitfor_job=job1)

    result = await client.cancel_job(job2)
    assert result is True

    async with pool.acquire() as conn:
        state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job2)
        assert state == 'cancelled'


@pytest.mark.asyncio
async def test_cancel_job_running(client, clean_db, pool):
    """Test cannot cancel a running job"""
    # Create job and manually set to running
    job_id = await client.enqueue('test.TestJob')

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE jorb SET state = 'running'
            WHERE id = $1
        """, job_id)

    result = await client.cancel_job(job_id)
    assert result is False

    # State should still be running
    async with pool.acquire() as conn:
        state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
        assert state == 'running'


@pytest.mark.asyncio
async def test_cancel_job_not_found(client, clean_db):
    """Test cancelling non-existent job returns False"""
    result = await client.cancel_job(99999)
    assert result is False


@pytest.mark.asyncio
async def test_retry_job_crashed(client, clean_db, pool):
    """Test retrying a crashed job"""
    # Create job and manually set to crashed
    original_job = await client.enqueue('test.TestJob', arg='value', queue='custom')

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE jorb SET state = 'crashed'
            WHERE id = $1
        """, original_job)

    # Retry the job
    new_job_id = await client.retry_job(original_job)

    assert new_job_id is not None
    assert new_job_id != original_job

    # Verify new job has same parameters
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", new_job_id)
        import json
        kwargs = json.loads(row['kwargs']) if isinstance(row['kwargs'], str) else row['kwargs']
        admin_data = json.loads(row['admin_data']) if isinstance(row['admin_data'], str) else row['admin_data']
        assert row['job_class'] == 'test.TestJob'
        assert kwargs['arg'] == 'value'
        assert row['queue'] == 'custom'
        assert row['state'] == 'queued'
        assert admin_data['retry_of'] == original_job


@pytest.mark.asyncio
async def test_retry_job_finished(client, clean_db, pool):
    """Test retrying a finished job"""
    job_id = await client.enqueue('test.TestJob')

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE jorb SET state = 'finished'
            WHERE id = $1
        """, job_id)

    new_job_id = await client.retry_job(job_id)
    assert new_job_id is not None


@pytest.mark.asyncio
async def test_retry_job_not_found(client, clean_db):
    """Test retrying non-existent job returns None"""
    new_job_id = await client.retry_job(99999)
    assert new_job_id is None


# =============================================================================
# Queue Operations Tests
# =============================================================================

@pytest.mark.asyncio
async def test_queue_depth(client, clean_db):
    """Test getting queue depth"""
    # Create jobs in different queues
    await client.enqueue('test.Job1', queue='emails')
    await client.enqueue('test.Job2', queue='emails')
    await client.enqueue('test.Job3', queue='processing')

    emails_depth = await client.queue_depth('emails')
    processing_depth = await client.queue_depth('processing')
    default_depth = await client.queue_depth('default')

    assert emails_depth == 2
    assert processing_depth == 1
    assert default_depth == 0


@pytest.mark.asyncio
async def test_queue_depth_ignores_non_queued(client, clean_db, pool):
    """Test queue_depth only counts queued jobs"""
    job1 = await client.enqueue('test.Job1')
    job2 = await client.enqueue('test.Job2')

    # Manually set one to running
    async with pool.acquire() as conn:
        await conn.execute("UPDATE jorb SET state = 'running' WHERE id = $1", job1)

    depth = await client.queue_depth('default')
    assert depth == 1  # Only job2 is still queued


@pytest.mark.asyncio
async def test_queue_stats(client, clean_db, pool):
    """Test getting queue statistics"""
    # Create jobs in various states
    job1 = await client.enqueue('test.Job1')
    job2 = await client.enqueue('test.Job2')
    job3 = await client.enqueue('test.Job3')
    job4 = await client.enqueue('test.Job4')

    async with pool.acquire() as conn:
        await conn.execute("UPDATE jorb SET state = 'running' WHERE id = $1::bigint", job1)
        await conn.execute("UPDATE jorb SET state = 'finished' WHERE id = $1::bigint", job2)
        await conn.execute("UPDATE jorb SET state = 'crashed' WHERE id = $1::bigint", job3)
        # job4 remains queued

    stats = await client.queue_stats('default')

    assert stats['queued'] == 1
    assert stats['running'] == 1
    assert stats['finished'] == 1
    assert stats['crashed'] == 1
    assert stats['waiting'] == 0
    assert stats['claimed'] == 0
    assert stats['cancelled'] == 0


@pytest.mark.asyncio
async def test_queue_stats_empty_queue(client, clean_db):
    """Test stats for empty queue returns zeros"""
    stats = await client.queue_stats('nonexistent')

    assert stats['queued'] == 0
    assert stats['running'] == 0
    assert stats['finished'] == 0


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================

@pytest.mark.asyncio
async def test_enqueue_with_unicode_args(client, clean_db, pool):
    """Test enqueueing job with unicode characters in kwargs"""
    job_id = await client.enqueue(
        'test.UnicodeJob',
        message='Hello 世界 🌍',
        emoji='✅ ❌ 🚀'
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT kwargs FROM jorb WHERE id = $1", job_id)
        import json
        kwargs = json.loads(row['kwargs']) if isinstance(row['kwargs'], str) else row['kwargs']
        assert kwargs['message'] == 'Hello 世界 🌍'
        assert kwargs['emoji'] == '✅ ❌ 🚀'


@pytest.mark.asyncio
async def test_enqueue_with_complex_kwargs(client, clean_db, pool):
    """Test enqueueing job with nested dict/list kwargs"""
    complex_data = {
        'nested': {
            'array': [1, 2, 3],
            'dict': {'key': 'value'},
        },
        'list': ['a', 'b', 'c'],
        'null': None,
        'bool': True,
    }

    job_id = await client.enqueue('test.ComplexJob', data=complex_data)

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT kwargs FROM jorb WHERE id = $1", job_id)
        import json
        kwargs = json.loads(row['kwargs']) if isinstance(row['kwargs'], str) else row['kwargs']
        assert kwargs['data'] == complex_data


@pytest.mark.asyncio
async def test_multiple_clients_share_pool(pool, clean_db):
    """Test multiple clients can share same pool"""
    client1 = JobClient(pool)
    client2 = JobClient(pool)

    job1 = await client1.enqueue('test.Job1')
    job2 = await client2.enqueue('test.Job2')

    assert job1 != job2

    # Both can see each other's jobs
    info1 = await client2.get_job(job1)
    info2 = await client1.get_job(job2)

    assert info1 is not None
    assert info2 is not None

    await client1.close()
    await client2.close()


@pytest.mark.asyncio
async def test_enqueue_with_uid(client, clean_db, pool):
    """Test enqueueing job with uid (multi-tenancy)"""
    job_id = await client.enqueue('test.TenantJob', uid=12345, data='tenant-data')

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT uid FROM jorb WHERE id = $1", job_id)
        assert row['uid'] == 12345


# =============================================================================
# Phase 2 Features and Edge Cases Tests
# =============================================================================

@pytest.mark.asyncio
async def test_enqueue_with_save_result(client, clean_db, pool):
    """Test enqueueing job with save_result flag - covers line 348."""
    job_id = await client.enqueue('test.Job', save_result=True, data='test')

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT admin_data FROM jorb WHERE id = $1", job_id)
        import json
        admin_data = json.loads(row['admin_data']) if isinstance(row['admin_data'], str) else row['admin_data']
        assert admin_data.get('save_result') is True


@pytest.mark.asyncio
async def test_enqueue_with_timeout_seconds(client, clean_db, pool):
    """Test enqueueing job with timeout configuration - covers lines 358-359."""
    job_id = await client.enqueue(
        'test.Job',
        timeout_seconds=300,
        on_timeout='fail',
        data='test'
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT admin_data FROM jorb WHERE id = $1", job_id)
        import json
        admin_data = json.loads(row['admin_data']) if isinstance(row['admin_data'], str) else row['admin_data']
        assert admin_data.get('timeout_seconds') == 300
        assert admin_data.get('on_timeout') == 'fail'


@pytest.mark.asyncio
async def test_enqueue_with_use_result_from(client, clean_db, pool):
    """Test enqueueing job with use_result_from - covers lines 324-330."""
    from pyjobby.pj import STMTS

    # Create and finish upstream job with result
    upstream_id = await client.enqueue('test.UpstreamJob', data='upstream')

    async with pool.acquire() as conn:
        # Claim, run, and finish with result
        await conn.execute(STMTS["claim"], 12345, "worker", "default", [], 1000)
        await conn.execute(STMTS["run"], upstream_id)
        await conn.execute(
            STMTS["finished"],
            upstream_id,
            {"status": "success", "value": 42}
        )

    # Create downstream job that uses upstream result
    downstream_id = await client.enqueue(
        'test.DownstreamJob',
        use_result_from=upstream_id,
        data='downstream'
    )

    # Verify downstream job received upstream result
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT kwargs FROM jorb WHERE id = $1", downstream_id)
        # kwargs is already a dict with JSON codec
        assert 'upstream_result' in row['kwargs']
        assert row['kwargs']['upstream_result']['status'] == 'success'
        assert row['kwargs']['upstream_result']['value'] == 42


@pytest.mark.asyncio
async def test_health_check_exception_path(pool):
    """Test health_check returns False on exception - covers lines 1262-1263."""
    import asyncpg
    from unittest.mock import AsyncMock, MagicMock

    # Create a client with a mock pool that raises an error
    mock_pool = MagicMock()
    mock_acquire = AsyncMock(side_effect=asyncpg.PostgresError("Connection failed"))
    mock_pool.acquire = MagicMock(return_value=mock_acquire())

    client = JobClient(mock_pool)

    # Health check should return False on exception
    healthy = await client.health_check()
    assert healthy is False


@pytest.mark.asyncio
async def test_get_job_full_not_found(client, clean_db):
    """Test get_job_full returns None for non-existent job - covers line 715."""
    job = await client.get_job_full(999999999)
    assert job is None


@pytest.mark.asyncio
async def test_get_job_result_with_string_result(client, clean_db, pool):
    """Test get_job_result parses string JSON result - covers line 748."""
    import json
    from pyjobby.pj import STMTS

    # Create and finish job with result
    job_id = await client.enqueue('test.Job', data='test')

    async with pool.acquire() as conn:
        await conn.execute(STMTS["claim"], 12345, "worker", "default", [], 1000)
        await conn.execute(STMTS["run"], job_id)
        # Store result as JSON string
        await conn.execute(
            STMTS["finished"],
            job_id,
            json.dumps({"result": "completed", "value": 123})
        )

    # Get result - should parse JSON string
    result = await client.get_job_result(job_id)
    assert result is not None
    assert result['result'] == 'completed'
    assert result['value'] == 123


@pytest.mark.asyncio
async def test_get_jobs_with_invalid_order_by(client, clean_db):
    """Test get_jobs validates order_by field - covers line 848."""
    # Create some test jobs
    await client.enqueue('test.Job', data='test1')
    await client.enqueue('test.Job', data='test2')

    # Try invalid order_by field (should default to 'created')
    jobs = await client.get_jobs(order_by='invalid_field', limit=10)

    # Should succeed with default ordering
    assert len(jobs) >= 2


@pytest.mark.asyncio
async def test_create_pipeline_with_results(client, clean_db, pool):
    """Test create_pipeline_with_results - covers lines 1409-1428."""
    # Create pipeline with result passing
    stages = [
        ('test.FetchData', {'source': 'api'}, True),      # Save result
        ('test.ProcessData', {'format': 'json'}, True),   # Save result
        ('test.StoreData', {'dest': 'db'}, False),        # Don't save
    ]

    job_ids = await client.create_pipeline_with_results(
        stages,
        queue='pipeline',
        priority=200
    )

    assert len(job_ids) == 3

    # Verify all jobs created
    async with pool.acquire() as conn:
        for job_id in job_ids:
            job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
            assert job is not None
            assert job['queue'] == 'pipeline'
            assert job['prio'] == 200

        # Verify first job is queued, others are waiting
        job1 = await conn.fetchrow("SELECT state FROM jorb WHERE id = $1", job_ids[0])
        assert job1['state'] == 'queued'

        job2 = await conn.fetchrow("SELECT state, waitfor_job FROM jorb WHERE id = $1", job_ids[1])
        assert job2['state'] == 'waiting'
        assert job2['waitfor_job'] == job_ids[0]

        job3 = await conn.fetchrow("SELECT state, waitfor_job FROM jorb WHERE id = $1", job_ids[2])
        assert job3['state'] == 'waiting'
        assert job3['waitfor_job'] == job_ids[1]


@pytest.mark.asyncio
async def test_from_config(tmp_path):
    """Test creating client from config file - covers lines 184-194."""
    # Create temporary config file
    config_file = tmp_path / "pyjobby.conf.py"
    config_content = """
db_params = {
    'host': 'localhost',
    'port': 5432,
    'database': 'pyjobby_test',
    'user': 'pyjobby_test',
    'password': 'pyjobby_test_password',
}
"""
    config_file.write_text(config_content)

    # Create client from config
    client = await JobClient.from_config(str(config_file), min_size=2, max_size=5)

    try:
        assert client.pool is not None
        assert not client._closed

        # Test that pool works
        async with client.pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
            assert result == 1

        # Verify pool has correct connection parameters
        assert client.pool._minsize == 2
        assert client.pool._maxsize == 5

    finally:
        await client.close()
        assert client._closed
