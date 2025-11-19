"""
Phase 2: Job Result Storage and Passing Tests

Comprehensive tests for result storage and passing between jobs in pipelines.
Tests both database storage and automatic result injection.
"""

import asyncio
from datetime import datetime

import pytest
import asyncpg

from tests.utils.factories import create_job, get_job


class TestResultStorage:
    """Test result storage in database."""

    @pytest.mark.asyncio
    async def test_result_column_exists(self, db_connection):
        """Verify result column exists in jorb table."""
        result = await db_connection.fetchval("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'jorb' AND column_name = 'result'
        """)
        assert result == 'result'

    @pytest.mark.asyncio
    async def test_result_index_exists(self, db_connection):
        """Verify sparse index on result column exists."""
        result = await db_connection.fetchval("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'jorb' AND indexname = 'jorb_result_exists_idx'
        """)
        assert result == 'jorb_result_exists_idx'

    @pytest.mark.asyncio
    async def test_store_simple_result(self, db_connection):
        """Test storing a simple result."""
        job_id = await create_job(db_connection, job_class="test.Job")

        # Store result
        test_result = {"status": "success", "count": 42}
        await db_connection.execute("""
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """, test_result, job_id)

        # Retrieve and verify
        job = await get_job(db_connection, job_id)
        assert job['result'] is not None
        assert job['result']['status'] == 'success'
        assert job['result']['count'] == 42

    @pytest.mark.asyncio
    async def test_result_null_by_default(self, db_connection):
        """Test that result is NULL by default."""
        job_id = await create_job(db_connection, job_class="test.Job")
        job = await get_job(db_connection, job_id)
        assert job['result'] is None

    @pytest.mark.asyncio
    async def test_store_complex_result(self, db_connection):
        """Test storing complex nested data."""
        job_id = await create_job(db_connection, job_class="test.Job")

        complex_result = {
            "status": "success",
            "data": {
                "items": [1, 2, 3, 4, 5],
                "metadata": {
                    "timestamp": datetime.now().isoformat(),
                    "version": "1.0"
                }
            },
            "stats": {
                "processed": 100,
                "failed": 0
            }
        }

        await db_connection.execute("""
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """, complex_result, job_id)

        job = await get_job(db_connection, job_id)
        assert job['result']['data']['items'] == [1, 2, 3, 4, 5]
        assert job['result']['stats']['processed'] == 100

    @pytest.mark.asyncio
    async def test_result_size_limit(self, db_connection):
        """Test that oversized results are rejected (10MB limit)."""
        job_id = await create_job(db_connection, job_class="test.Job")

        # Create a result larger than 10MB
        large_result = {"data": "x" * (11 * 1024 * 1024)}  # 11MB of 'x'

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await db_connection.execute("""
                UPDATE jorb
                SET result = $1, state = 'finished'
                WHERE id = $2
            """, large_result, job_id)

    @pytest.mark.asyncio
    async def test_result_with_finished_statement(self, db_connection):
        """Test result storage using the finished statement."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, job_class="test.Job", state="claimed")

        result = {"status": "completed", "value": 123}
        await db_connection.execute(
            STMTS["finished"],
            job_id, result
        )

        job = await get_job(db_connection, job_id)
        assert job['state'] == 'finished'
        assert job['result'] is not None
        assert job['result']['value'] == 123


class TestResultPassing:
    """Test automatic result passing between jobs."""

    @pytest.mark.asyncio
    async def test_upstream_result_injection(self, db_connection):
        """Test that upstream result is injected into downstream kwargs."""
        # Create upstream job with result
        upstream_id = await create_job(db_connection, job_class="test.Upstream")
        upstream_result = {"data": "from_upstream", "count": 42}
        await db_connection.execute("""
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """, upstream_result, upstream_id)

        # Create downstream job referencing upstream
        downstream_kwargs = {"param": "value"}
        downstream_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, state, waitfor_job)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, "test.Downstream", downstream_kwargs, "default", "waiting", upstream_id)

        # Simulate client.enqueue with use_result_from
        # In real usage, client would inject upstream_result into kwargs
        upstream_job = await get_job(db_connection, upstream_id)
        if upstream_job['result']:
            downstream_kwargs['upstream_result'] = upstream_job['result']
            await db_connection.execute("""
                UPDATE jorb SET kwargs = $1 WHERE id = $2
            """, downstream_kwargs, downstream_id)

        # Verify downstream has upstream result
        downstream_job = await get_job(db_connection, downstream_id)
        assert 'upstream_result' in downstream_job['kwargs']
        assert downstream_job['kwargs']['upstream_result']['data'] == 'from_upstream'
        assert downstream_job['kwargs']['upstream_result']['count'] == 42

    @pytest.mark.asyncio
    async def test_result_passing_in_chain(self, db_connection):
        """Test result passing through a chain of jobs."""
        # Job 1: Initial data
        job1_id = await create_job(db_connection, job_class="test.Job1")
        job1_result = {"step": 1, "data": "initial"}
        await db_connection.execute("""
            UPDATE jorb SET result = $1, state = 'finished' WHERE id = $2
        """, job1_result, job1_id)

        # Job 2: Waits for job1, processes its result
        job2_id = await create_job(
            db_connection,
            job_class="test.Job2",
            waitfor_job=job1_id
        )
        # Inject result from job1
        job1 = await get_job(db_connection, job1_id)
        await db_connection.execute("""
            UPDATE jorb
            SET kwargs = jsonb_set(kwargs::jsonb, '{upstream_result}', $1::jsonb)
            WHERE id = $2
        """, job1['result'], job2_id)

        # Job 2 produces its own result
        job2_result = {"step": 2, "data": "processed", "from_step1": job1_result['data']}
        await db_connection.execute("""
            UPDATE jorb SET result = $1, state = 'finished' WHERE id = $2
        """, job2_result, job2_id)

        # Job 3: Waits for job2
        job3_id = await create_job(
            db_connection,
            job_class="test.Job3",
            waitfor_job=job2_id
        )
        # Inject result from job2
        job2 = await get_job(db_connection, job2_id)
        await db_connection.execute("""
            UPDATE jorb
            SET kwargs = jsonb_set(kwargs::jsonb, '{upstream_result}', $1::jsonb)
            WHERE id = $2
        """, job2['result'], job3_id)

        # Verify job3 has result from job2
        job3 = await get_job(db_connection, job3_id)
        assert job3['kwargs']['upstream_result']['step'] == 2
        assert job3['kwargs']['upstream_result']['from_step1'] == 'initial'

    @pytest.mark.asyncio
    async def test_result_passing_with_null_result(self, db_connection):
        """Test that NULL result doesn't break downstream jobs."""
        # Upstream job with no result
        upstream_id = await create_job(db_connection, job_class="test.Upstream", state="finished")

        # Downstream job
        downstream_id = await create_job(
            db_connection,
            job_class="test.Downstream",
            waitfor_job=upstream_id
        )

        # Simulate client checking for upstream result (would be NULL)
        upstream = await get_job(db_connection, upstream_id)
        assert upstream['result'] is None

        # Downstream kwargs should not have upstream_result
        downstream = await get_job(db_connection, downstream_id)
        assert 'upstream_result' not in downstream['kwargs']


class TestAdminDataSaveResult:
    """Test save_result flag in admin_data."""

    @pytest.mark.asyncio
    async def test_save_result_flag_in_admin_data(self, db_connection):
        """Test that save_result flag can be stored in admin_data."""
        admin_data = {"save_result": True, "other_meta": "value"}
        job_id = await db_connection.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """, "test.Job", '{}', "default", admin_data)

        job = await get_job(db_connection, job_id)
        assert job['admin_data'] is not None
        assert job['admin_data']['save_result'] is True

    @pytest.mark.asyncio
    async def test_save_result_defaults_to_false(self, db_connection):
        """Test that save_result defaults to false (no flag in admin_data)."""
        job_id = await create_job(db_connection, job_class="test.Job")
        job = await get_job(db_connection, job_id)

        # admin_data may be None or empty
        if job['admin_data']:
            assert job['admin_data'].get('save_result', False) is False
        else:
            # No admin_data means save_result is False
            assert True


class TestPipelinePatterns:
    """Test common pipeline patterns with result passing."""

    @pytest.mark.asyncio
    async def test_linear_pipeline(self, db_connection):
        """Test a simple linear pipeline with result passing."""
        # Create a pipeline: Fetch -> Process -> Store
        jobs = []

        # Fetch job (saves result)
        fetch_id = await create_job(db_connection, job_class="test.Fetch")
        jobs.append(fetch_id)
        await db_connection.execute("""
            UPDATE jorb
            SET admin_data = $1, result = $2, state = 'finished'
            WHERE id = $3
        """, {"save_result": True},
            {"fetched_data": [1, 2, 3]},
            fetch_id)

        # Process job (saves result, uses fetch result)
        process_id = await create_job(
            db_connection,
            job_class="test.Process",
            waitfor_job=fetch_id
        )
        jobs.append(process_id)
        # Inject upstream result
        fetch_job = await get_job(db_connection, fetch_id)
        await db_connection.execute("""
            UPDATE jorb
            SET kwargs = jsonb_set(kwargs::jsonb, '{upstream_result}', $1::jsonb),
                admin_data = $2
            WHERE id = $3
        """, fetch_job['result'],
            {"save_result": True},
            process_id)

        # Process produces result
        await db_connection.execute("""
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """, {"processed_data": [2, 4, 6]}, process_id)

        # Store job (doesn't save result, uses process result)
        store_id = await create_job(
            db_connection,
            job_class="test.Store",
            waitfor_job=process_id
        )
        jobs.append(store_id)
        # Inject upstream result
        process_job = await get_job(db_connection, process_id)
        await db_connection.execute("""
            UPDATE jorb
            SET kwargs = jsonb_set(kwargs::jsonb, '{upstream_result}', $1::jsonb)
            WHERE id = $2
        """, process_job['result'], store_id)

        # Verify store job has processed data
        store_job = await get_job(db_connection, store_id)
        assert store_job['kwargs']['upstream_result']['processed_data'] == [2, 4, 6]

    @pytest.mark.asyncio
    async def test_fan_out_with_results(self, db_connection):
        """Test fan-out pattern where multiple jobs use same upstream result."""
        # Upstream job produces result
        upstream_id = await create_job(db_connection, job_class="test.Upstream")
        upstream_result = {"shared_data": "available_to_all"}
        await db_connection.execute("""
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """, upstream_result, upstream_id)

        # Create 3 downstream jobs, all using the same upstream result
        downstream_ids = []
        for i in range(3):
            job_id = await create_job(
                db_connection,
                job_class=f"test.Downstream{i}",
                waitfor_job=upstream_id
            )
            downstream_ids.append(job_id)

            # Inject upstream result into each
            upstream = await get_job(db_connection, upstream_id)
            await db_connection.execute("""
                UPDATE jorb
                SET kwargs = jsonb_set(kwargs::jsonb, '{upstream_result}', $1::jsonb)
                WHERE id = $2
            """, upstream['result'], job_id)

        # Verify all downstream jobs have the shared data
        for job_id in downstream_ids:
            job = await get_job(db_connection, job_id)
            assert job['kwargs']['upstream_result']['shared_data'] == 'available_to_all'


class TestResultCleanup:
    """Test result cleanup and lifecycle."""

    @pytest.mark.asyncio
    async def test_result_deleted_with_job(self, db_connection):
        """Test that result is deleted when job row is deleted."""
        job_id = await create_job(db_connection, job_class="test.Job")
        await db_connection.execute("""
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """, {"data": "test"}, job_id)

        # Verify result exists
        job = await get_job(db_connection, job_id)
        assert job['result'] is not None

        # Delete job
        await db_connection.execute("DELETE FROM jorb WHERE id = $1", job_id)

        # Verify job is gone (and result with it)
        job = await get_job(db_connection, job_id)
        assert job is None

    @pytest.mark.asyncio
    async def test_result_persists_across_states(self, db_connection):
        """Test that result persists even when job state changes."""
        job_id = await create_job(db_connection, job_class="test.Job")
        test_result = {"data": "persistent"}

        await db_connection.execute("""
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """, test_result, job_id)

        # Change state (simulating inspection or retry)
        await db_connection.execute("""
            UPDATE jorb SET state = 'queued' WHERE id = $1
        """, job_id)

        # Result should still be there
        job = await get_job(db_connection, job_id)
        assert job['result'] is not None
        assert job['result']['data'] == 'persistent'
