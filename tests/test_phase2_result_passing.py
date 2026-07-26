"""
Phase 2: Job Result Storage and Passing Tests

Comprehensive tests for result storage and passing between jobs in pipelines.
Tests both database storage and the worker's RUN-time result injection
(admin_data.use_result_from -> kwargs['upstream_result']).
"""

from datetime import datetime
from typing import Any

import pytest

from pyjobby.pj import Job
from tests.conftest import wait_for_job_state
from tests.utils.factories import create_job, get_job


class EchoUpstreamJob(Job):
    """Returns whatever upstream result the worker injected at run time."""

    async def task(
        self, upstream_result: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        return {"upstream": upstream_result}


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
        assert result == "result"

    # NOTE: schema v1 removed the jorb_result_exists_idx sparse index and the
    # 10MB result-size CHECK constraint; tests of both were deleted.

    @pytest.mark.asyncio
    async def test_store_simple_result(self, db_connection):
        """Test storing a simple result."""
        job_id = await create_job(db_connection, job_class="test.Job")

        # Store result
        test_result = {"status": "success", "count": 42}
        await db_connection.execute(
            """
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """,
            test_result,
            job_id,
        )

        # Retrieve and verify
        job = await get_job(db_connection, job_id)
        assert job["result"] is not None
        assert job["result"]["status"] == "success"
        assert job["result"]["count"] == 42

    @pytest.mark.asyncio
    async def test_result_null_by_default(self, db_connection):
        """Test that result is NULL by default."""
        job_id = await create_job(db_connection, job_class="test.Job")
        job = await get_job(db_connection, job_id)
        assert job["result"] is None

    @pytest.mark.asyncio
    async def test_store_complex_result(self, db_connection):
        """Test storing complex nested data."""
        job_id = await create_job(db_connection, job_class="test.Job")

        complex_result = {
            "status": "success",
            "data": {
                "items": [1, 2, 3, 4, 5],
                "metadata": {"timestamp": datetime.now().isoformat(), "version": "1.0"},
            },
            "stats": {"processed": 100, "failed": 0},
        }

        await db_connection.execute(
            """
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """,
            complex_result,
            job_id,
        )

        job = await get_job(db_connection, job_id)
        assert job["result"]["data"]["items"] == [1, 2, 3, 4, 5]
        assert job["result"]["stats"]["processed"] == 100

    @pytest.mark.asyncio
    async def test_result_with_finished_statement(self, db_connection):
        """Test result storage using the (epoch-fenced) finished statement."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, job_class="test.Job", state="claimed")

        result = {"status": "completed", "value": 123}
        # $3 is the fencing epoch; a fresh row is at run_epoch 0
        await db_connection.execute(STMTS["finished"], job_id, result, 0)

        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["result"] is not None
        assert job["result"]["value"] == 123

    @pytest.mark.asyncio
    async def test_finished_statement_is_epoch_fenced(self, db_connection):
        """A stale epoch cannot write a result (fencing token no-ops it)."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, job_class="test.Job", state="claimed")
        # simulate a newer claim having bumped the epoch
        await db_connection.execute(
            "UPDATE jorb SET run_epoch = 2 WHERE id = $1", job_id
        )

        rows = await db_connection.fetch(
            STMTS["finished"], job_id, {"stale": True}, 1
        )
        assert rows == []

        job = await get_job(db_connection, job_id)
        assert job["state"] == "claimed"
        assert job["result"] is None


class TestResultPassing:
    """Test the worker's RUN-time result injection between jobs."""

    @pytest.mark.asyncio
    async def test_upstream_result_injected_at_run_time(
        self, live_worker, unique_queue, db_pool
    ):
        """The worker injects kwargs['upstream_result'] from the job named in
        admin_data.use_result_from when the downstream job runs."""
        await live_worker()

        # upstream runs first and stores a result
        upstream_id = await db_pool.fetchval(
            "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
            "tests.dxe_jobs.OkJob",
            {"x": 21},
            unique_queue,
        )
        upstream = await wait_for_job_state(db_pool, upstream_id, ("finished",))
        assert upstream["result"] == {"doubled": 42}

        # downstream references the upstream job via admin_data.use_result_from
        downstream_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
               VALUES ($1,$2,$3,$4) RETURNING id""",
            "tests.test_phase2_result_passing.EchoUpstreamJob",
            {"param": "value"},
            unique_queue,
            {"use_result_from": upstream_id},
        )

        downstream = await wait_for_job_state(db_pool, downstream_id, ("finished",))
        assert downstream["result"] == {"upstream": {"doubled": 42}}

    @pytest.mark.asyncio
    async def test_no_injection_when_upstream_unfinished(
        self, live_worker, unique_queue, db_pool
    ):
        """No upstream_result is injected if the referenced job is not
        finished (the task just sees its plain kwargs)."""
        await live_worker()

        # an upstream that never runs (parked on an unrelated queue)
        upstream_id = await db_pool.fetchval(
            "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
            "tests.dxe_jobs.OkJob",
            {"x": 1},
            f"{unique_queue}_parked",
        )

        downstream_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
               VALUES ($1,$2,$3,$4) RETURNING id""",
            "tests.test_phase2_result_passing.EchoUpstreamJob",
            {},
            unique_queue,
            {"use_result_from": upstream_id},
        )

        downstream = await wait_for_job_state(db_pool, downstream_id, ("finished",))
        assert downstream["result"] == {"upstream": None}

    @pytest.mark.asyncio
    async def test_result_passing_in_chain(self, db_connection):
        """Test result passing through a chain of jobs."""
        # Job 1: Initial data
        job1_id = await create_job(db_connection, job_class="test.Job1")
        job1_result = {"step": 1, "data": "initial"}
        await db_connection.execute(
            """
            UPDATE jorb SET result = $1, state = 'finished' WHERE id = $2
        """,
            job1_result,
            job1_id,
        )

        # Job 2: Waits for job1, processes its result
        job2_id = await create_job(
            db_connection, job_class="test.Job2", waitfor_job=job1_id
        )
        # Inject result from job1
        job1 = await get_job(db_connection, job1_id)
        await db_connection.execute(
            """
            UPDATE jorb
            SET kwargs = jsonb_set(kwargs, '{upstream_result}', to_jsonb($1::jsonb))
            WHERE id = $2
        """,
            job1["result"],
            job2_id,
        )

        # Job 2 produces its own result
        job2_result = {
            "step": 2,
            "data": "processed",
            "from_step1": job1_result["data"],
        }
        await db_connection.execute(
            """
            UPDATE jorb SET result = $1, state = 'finished' WHERE id = $2
        """,
            job2_result,
            job2_id,
        )

        # Job 3: Waits for job2
        job3_id = await create_job(
            db_connection, job_class="test.Job3", waitfor_job=job2_id
        )
        # Inject result from job2
        job2 = await get_job(db_connection, job2_id)
        await db_connection.execute(
            """
            UPDATE jorb
            SET kwargs = jsonb_set(kwargs, '{upstream_result}', to_jsonb($1::jsonb))
            WHERE id = $2
        """,
            job2["result"],
            job3_id,
        )

        # Verify job3 has result from job2
        job3 = await get_job(db_connection, job3_id)
        assert job3["kwargs"]["upstream_result"]["step"] == 2
        assert job3["kwargs"]["upstream_result"]["from_step1"] == "initial"

    @pytest.mark.asyncio
    async def test_result_passing_with_null_result(self, db_connection):
        """Test that NULL result doesn't break downstream jobs."""
        # Upstream job with no result
        upstream_id = await create_job(
            db_connection, job_class="test.Upstream", state="finished"
        )

        # Downstream job
        downstream_id = await create_job(
            db_connection, job_class="test.Downstream", waitfor_job=upstream_id
        )

        # Simulate client checking for upstream result (would be NULL)
        upstream = await get_job(db_connection, upstream_id)
        assert upstream["result"] is None

        # Downstream kwargs should not have upstream_result
        downstream = await get_job(db_connection, downstream_id)
        assert "upstream_result" not in downstream["kwargs"]


class TestAdminDataSaveResult:
    """Test save_result semantics: results are saved by default; an explicit
    save_result=False in admin_data discards the result."""

    @pytest.mark.asyncio
    async def test_save_result_flag_in_admin_data(self, db_connection):
        """Test that save_result flag can be stored in admin_data."""
        admin_data = {"save_result": False, "other_meta": "value"}
        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """,
            "test.Job",
            {},
            "default",
            admin_data,
        )

        job = await get_job(db_connection, job_id)
        assert job["admin_data"] is not None
        assert job["admin_data"]["save_result"] is False

    @pytest.mark.asyncio
    async def test_result_saved_by_default(self, live_worker, unique_queue, db_pool):
        """Without a save_result flag the worker stores the task's result."""
        await live_worker()

        job_id = await db_pool.fetchval(
            "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
            "tests.dxe_jobs.OkJob",
            {"x": 3},
            unique_queue,
        )

        row = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert row["result"] == {"doubled": 6}

    @pytest.mark.asyncio
    async def test_save_result_false_discards_result(
        self, live_worker, unique_queue, db_pool
    ):
        """An explicit save_result=False discards the task's return value."""
        await live_worker()

        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
               VALUES ($1,$2,$3,$4) RETURNING id""",
            "tests.dxe_jobs.OkJob",
            {"x": 3},
            unique_queue,
            {"save_result": False},
        )

        row = await wait_for_job_state(db_pool, job_id, ("finished",))
        assert row["state"] == "finished"
        assert row["result"] is None


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
        await db_connection.execute(
            """
            UPDATE jorb
            SET admin_data = $1, result = $2, state = 'finished'
            WHERE id = $3
        """,
            {"save_result": True},
            {"fetched_data": [1, 2, 3]},
            fetch_id,
        )

        # Process job (saves result, uses fetch result)
        process_id = await create_job(
            db_connection, job_class="test.Process", waitfor_job=fetch_id
        )
        jobs.append(process_id)
        # Inject upstream result
        fetch_job = await get_job(db_connection, fetch_id)
        await db_connection.execute(
            """
            UPDATE jorb
            SET kwargs = jsonb_set(kwargs, '{upstream_result}', to_jsonb($1::jsonb)),
                admin_data = $2
            WHERE id = $3
        """,
            fetch_job["result"],
            {"save_result": True},
            process_id,
        )

        # Process produces result
        await db_connection.execute(
            """
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """,
            {"processed_data": [2, 4, 6]},
            process_id,
        )

        # Store job (doesn't save result, uses process result)
        store_id = await create_job(
            db_connection, job_class="test.Store", waitfor_job=process_id
        )
        jobs.append(store_id)
        # Inject upstream result
        process_job = await get_job(db_connection, process_id)
        await db_connection.execute(
            """
            UPDATE jorb
            SET kwargs = jsonb_set(kwargs, '{upstream_result}', to_jsonb($1::jsonb))
            WHERE id = $2
        """,
            process_job["result"],
            store_id,
        )

        # Verify store job has processed data
        store_job = await get_job(db_connection, store_id)
        assert store_job["kwargs"]["upstream_result"]["processed_data"] == [2, 4, 6]

    @pytest.mark.asyncio
    async def test_fan_out_with_results(self, db_connection):
        """Test fan-out pattern where multiple jobs use same upstream result."""
        # Upstream job produces result
        upstream_id = await create_job(db_connection, job_class="test.Upstream")
        upstream_result = {"shared_data": "available_to_all"}
        await db_connection.execute(
            """
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """,
            upstream_result,
            upstream_id,
        )

        # Create 3 downstream jobs, all using the same upstream result
        downstream_ids = []
        for i in range(3):
            job_id = await create_job(
                db_connection, job_class=f"test.Downstream{i}", waitfor_job=upstream_id
            )
            downstream_ids.append(job_id)

            # Inject upstream result into each
            upstream = await get_job(db_connection, upstream_id)
            await db_connection.execute(
                """
                UPDATE jorb
                SET kwargs = jsonb_set(kwargs, '{upstream_result}', to_jsonb($1::jsonb))
                WHERE id = $2
            """,
                upstream["result"],
                job_id,
            )

        # Verify all downstream jobs have the shared data
        for job_id in downstream_ids:
            job = await get_job(db_connection, job_id)
            assert job["kwargs"]["upstream_result"]["shared_data"] == "available_to_all"


class TestResultCleanup:
    """Test result cleanup and lifecycle."""

    @pytest.mark.asyncio
    async def test_result_deleted_with_job(self, db_connection):
        """Test that result is deleted when job row is deleted."""
        job_id = await create_job(db_connection, job_class="test.Job")
        await db_connection.execute(
            """
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """,
            {"data": "test"},
            job_id,
        )

        # Verify result exists
        job = await get_job(db_connection, job_id)
        assert job["result"] is not None

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

        await db_connection.execute(
            """
            UPDATE jorb
            SET result = $1, state = 'finished'
            WHERE id = $2
        """,
            test_result,
            job_id,
        )

        # Change state (simulating inspection or retry)
        await db_connection.execute(
            """
            UPDATE jorb SET state = 'queued' WHERE id = $1
        """,
            job_id,
        )

        # Result should still be there
        job = await get_job(db_connection, job_id)
        assert job["result"] is not None
        assert job["result"]["data"] == "persistent"
