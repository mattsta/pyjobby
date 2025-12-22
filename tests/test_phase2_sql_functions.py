"""
Phase 2: SQL Functions Direct Testing

Comprehensive tests for Phase 2 SQL functions:
- calculate_retry_delay() (Migration 006)
- check_timed_out_jobs() (Migration 007)
- get_dag_dependencies() (Migration 008)
- validate_dag_acyclic() (Migration 008)
- auto_complete_dag() trigger (Migration 008)
"""

import asyncio

import pytest

from tests.utils.factories import create_job


class TestCalculateRetryDelayFunction:
    """Test calculate_retry_delay() SQL function (Migration 006)."""

    @pytest.mark.asyncio
    async def test_function_exists(self, db_connection):
        """Verify calculate_retry_delay function exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = 'public' AND p.proname = 'calculate_retry_delay'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_exponential_backoff(self, db_connection):
        """Test exponential strategy: 1s, 2s, 4s, 8s, 16s..."""
        delays = []
        for attempt in range(1, 6):
            delay = await db_connection.fetchval(
                """
                SELECT calculate_retry_delay($1, 'exponential', 1, 3600, 2.0)
            """,
                attempt,
            )
            delays.append(delay)

        # Exponential: 1*2^0, 1*2^1, 1*2^2, 1*2^3, 1*2^4
        # With jitter: roughly 1, 2, 4, 8, 16 (±25%)
        assert 0 <= delays[0] <= 3  # ~1
        assert 1 <= delays[1] <= 5  # ~2
        assert 3 <= delays[2] <= 7  # ~4
        assert 6 <= delays[3] <= 12  # ~8
        assert 12 <= delays[4] <= 22  # ~16

    @pytest.mark.asyncio
    async def test_linear_backoff(self, db_connection):
        """Test linear strategy: 1s, 2s, 3s, 4s, 5s..."""
        delays = []
        for attempt in range(1, 6):
            delay = await db_connection.fetchval(
                """
                SELECT calculate_retry_delay($1, 'linear', 1, 3600, 2.0)
            """,
                attempt,
            )
            delays.append(delay)

        # Linear: 1, 2, 3, 4, 5 with jitter
        assert 0 <= delays[0] <= 3  # ~1
        assert 1 <= delays[1] <= 4  # ~2
        assert 2 <= delays[2] <= 5  # ~3
        assert 3 <= delays[3] <= 6  # ~4
        assert 4 <= delays[4] <= 7  # ~5

    @pytest.mark.asyncio
    async def test_fibonacci_backoff(self, db_connection):
        """Test fibonacci strategy: 1, 1, 2, 3, 5, 8, 13..."""
        delays = []
        for attempt in range(1, 8):
            delay = await db_connection.fetchval(
                """
                SELECT calculate_retry_delay($1, 'fibonacci', 1, 3600, 2.0)
            """,
                attempt,
            )
            delays.append(delay)

        # Fibonacci with jitter
        assert 0 <= delays[0] <= 3  # ~1
        assert 0 <= delays[1] <= 3  # ~1
        assert 1 <= delays[2] <= 4  # ~2
        assert 2 <= delays[3] <= 5  # ~3
        assert 4 <= delays[4] <= 7  # ~5
        assert 6 <= delays[5] <= 11  # ~8
        assert 11 <= delays[6] <= 16  # ~13

    @pytest.mark.asyncio
    async def test_fixed_backoff_legacy(self, db_connection):
        """Test fixed (legacy) strategy: quadratic."""
        delays = []
        for attempt in range(1, 5):
            delay = await db_connection.fetchval(
                """
                SELECT calculate_retry_delay($1, 'fixed', 1, 3600, 2.0)
            """,
                attempt,
            )
            delays.append(delay)

        # Fixed: 2*(n^2) with jitter
        # 1: 2*1 + jitter
        # 2: 2*4 + jitter
        # 3: 2*9 + jitter
        # 4: 2*16 + jitter
        assert 0 <= delays[0] <= 10  # ~2
        assert 5 <= delays[1] <= 15  # ~8
        assert 15 <= delays[2] <= 25  # ~18
        assert 28 <= delays[3] <= 40  # ~32

    @pytest.mark.asyncio
    async def test_max_delay_cap(self, db_connection):
        """Test that delays are capped at max_delay."""
        # Exponential with high attempt should cap
        delay = await db_connection.fetchval("""
            SELECT calculate_retry_delay(20, 'exponential', 1, 60, 2.0)
        """)

        # Should be capped at 60 seconds
        assert delay <= 60

    @pytest.mark.asyncio
    async def test_initial_delay_parameter(self, db_connection):
        """Test custom initial_delay parameter."""
        delay = await db_connection.fetchval("""
            SELECT calculate_retry_delay(1, 'exponential', 10, 3600, 2.0)
        """)

        # First attempt with initial=10 should be ~10
        assert 8 <= delay <= 13

    @pytest.mark.asyncio
    async def test_multiplier_parameter(self, db_connection):
        """Test custom multiplier parameter."""
        delay_2x = await db_connection.fetchval("""
            SELECT calculate_retry_delay(3, 'exponential', 1, 3600, 2.0)
        """)

        delay_3x = await db_connection.fetchval("""
            SELECT calculate_retry_delay(3, 'exponential', 1, 3600, 3.0)
        """)

        # With multiplier=3, delay should be larger
        # Attempt 3 with 2x: ~4, with 3x: ~9
        assert delay_3x > delay_2x

    @pytest.mark.asyncio
    async def test_zero_attempt(self, db_connection):
        """Test with attempt=0."""
        delay = await db_connection.fetchval("""
            SELECT calculate_retry_delay(0, 'exponential', 1, 3600, 2.0)
        """)

        # Should return minimum value
        assert delay >= 0

    @pytest.mark.asyncio
    async def test_negative_attempt(self, db_connection):
        """Test with negative attempt."""
        delay = await db_connection.fetchval("""
            SELECT calculate_retry_delay(-1, 'exponential', 1, 3600, 2.0)
        """)

        # Should handle gracefully
        assert delay >= 0

    @pytest.mark.asyncio
    async def test_unknown_strategy_defaults(self, db_connection):
        """Test that unknown strategy defaults to exponential."""
        delay_unknown = await db_connection.fetchval("""
            SELECT calculate_retry_delay(3, 'unknown_strategy', 1, 3600, 2.0)
        """)

        delay_exponential = await db_connection.fetchval("""
            SELECT calculate_retry_delay(3, 'exponential', 1, 3600, 2.0)
        """)

        # Should be similar (both exponential)
        assert abs(delay_unknown - delay_exponential) <= 3


class TestCheckTimedOutJobsFunction:
    """Test check_timed_out_jobs() SQL function (Migration 007)."""

    @pytest.mark.asyncio
    async def test_function_exists(self, db_connection):
        """Verify check_timed_out_jobs function exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = 'public' AND p.proname = 'check_timed_out_jobs'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_finds_timed_out_job(self, db_connection):
        """Test that function finds jobs past timeout_at."""
        # Create running job with timeout in the past
        job_id = await create_job(
            db_connection, job_class="test.TimeoutJob", state="running"
        )

        await db_connection.execute(
            """
            UPDATE jorb
            SET timeout_at = NOW() - INTERVAL '10 seconds'
            WHERE id = $1
        """,
            job_id,
        )

        # Check for timed out jobs
        timed_out = await db_connection.fetch("""
            SELECT * FROM check_timed_out_jobs()
        """)

        # Should find the job
        job_ids = [row["job_id"] for row in timed_out]
        assert job_id in job_ids

    @pytest.mark.asyncio
    async def test_ignores_future_timeouts(self, db_connection):
        """Test that function ignores jobs with future timeout_at."""
        # Create running job with timeout in the future
        job_id = await create_job(
            db_connection, job_class="test.FutureTimeout", state="running"
        )

        await db_connection.execute(
            """
            UPDATE jorb
            SET timeout_at = NOW() + INTERVAL '1 hour'
            WHERE id = $1
        """,
            job_id,
        )

        # Check for timed out jobs
        timed_out = await db_connection.fetch("""
            SELECT * FROM check_timed_out_jobs()
        """)

        # Should NOT find the job
        job_ids = [row["job_id"] for row in timed_out]
        assert job_id not in job_ids

    @pytest.mark.asyncio
    async def test_ignores_non_running_jobs(self, db_connection):
        """Test that function only considers running jobs."""
        # Create jobs in various states with past timeout_at
        states = ["queued", "finished", "crashed", "cancelled"]
        job_ids = []

        for state in states:
            job_id = await create_job(db_connection, job_class="test.Job", state=state)
            await db_connection.execute(
                """
                UPDATE jorb
                SET timeout_at = NOW() - INTERVAL '10 seconds'
                WHERE id = $1
            """,
                job_id,
            )
            job_ids.append(job_id)

        # Check for timed out jobs
        timed_out = await db_connection.fetch("""
            SELECT * FROM check_timed_out_jobs()
        """)

        found_ids = [row["job_id"] for row in timed_out]

        # Should not find any of these
        for job_id in job_ids:
            assert job_id not in found_ids

    @pytest.mark.asyncio
    async def test_returns_job_details(self, db_connection):
        """Test that function returns necessary job details."""
        # Create timed out job
        admin_data = {"timeout_seconds": 60, "on_timeout": "retry", "max_retries": 5}
        job_id = await create_job(
            db_connection,
            job_class="test.DetailJob",
            state="running",
            admin_data=admin_data,
        )

        await db_connection.execute(
            """
            UPDATE jorb
            SET timeout_at = NOW() - INTERVAL '5 seconds',
                error_count = 2
            WHERE id = $1
        """,
            job_id,
        )

        # Check for timed out jobs
        timed_out = await db_connection.fetch("""
            SELECT * FROM check_timed_out_jobs()
        """)

        # Find our job
        job_row = next((r for r in timed_out if r["job_id"] == job_id), None)
        assert job_row is not None

        # Verify returned fields
        assert job_row["job_class"] == "test.DetailJob"
        assert job_row["error_count"] == 2
        assert job_row["admin_data"] == admin_data

    @pytest.mark.asyncio
    async def test_batch_limit_parameter(self, db_connection):
        """Test batch_limit parameter limits results."""
        # Create 10 timed out jobs
        job_ids = []
        for i in range(10):
            job_id = await create_job(
                db_connection, job_class=f"test.Job{i}", state="running"
            )
            await db_connection.execute(
                """
                UPDATE jorb
                SET timeout_at = NOW() - INTERVAL '10 seconds'
                WHERE id = $1
            """,
                job_id,
            )
            job_ids.append(job_id)

        # Check with limit=5
        timed_out = await db_connection.fetch("""
            SELECT * FROM check_timed_out_jobs(5)
        """)

        # Should return exactly 5
        assert len(timed_out) == 5


class TestGetDAGDependenciesFunction:
    """Test get_dag_dependencies() SQL function (Migration 008)."""

    @pytest.mark.asyncio
    async def test_function_exists(self, db_connection):
        """Verify get_dag_dependencies function exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = 'public' AND p.proname = 'get_dag_dependencies'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_linear_dependencies(self, db_connection):
        """Test function returns linear dependencies."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Linear DAG",
        )

        # Create jobs: job1 -> job2 -> job3
        job1_id = await create_job(db_connection, job_class="test.Job1")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job1_id
        )

        job2_id = await create_job(
            db_connection, job_class="test.Job2", waitfor_job=job1_id
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job2_id
        )

        job3_id = await create_job(
            db_connection, job_class="test.Job3", waitfor_job=job2_id
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job3_id
        )

        # Get dependencies
        deps = await db_connection.fetch(
            """
            SELECT * FROM get_dag_dependencies($1)
        """,
            dag_id,
        )

        # Convert to dict for easier checking
        deps_dict = {row["job_id"]: row["depends_on"] for row in deps}

        assert job1_id in deps_dict
        assert deps_dict[job1_id] == []

        assert job2_id in deps_dict
        assert deps_dict[job2_id] == [job1_id]

        assert job3_id in deps_dict
        assert deps_dict[job3_id] == [job2_id]

    @pytest.mark.asyncio
    async def test_multiple_dependencies(self, db_connection):
        """Test function with multiple dependencies using jorb_dependencies table."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Multi-Dep DAG",
        )

        # Create jobs
        job1_id = await create_job(db_connection, job_class="test.Job1")
        job2_id = await create_job(db_connection, job_class="test.Job2")
        job3_id = await create_job(db_connection, job_class="test.Job3")

        for job_id in [job1_id, job2_id, job3_id]:
            await db_connection.execute(
                "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job_id
            )

        # Add explicit dependencies: job3 depends on job1 and job2
        await db_connection.execute(
            """
            INSERT INTO jorb_dependencies (job_id, depends_on_job_id)
            VALUES ($1, $2), ($1, $3)
        """,
            job3_id,
            job1_id,
            job2_id,
        )

        # Get dependencies
        deps = await db_connection.fetch(
            """
            SELECT * FROM get_dag_dependencies($1)
        """,
            dag_id,
        )

        deps_dict = {row["job_id"]: row["depends_on"] for row in deps}

        assert job1_id in deps_dict
        assert deps_dict[job1_id] == []

        assert job2_id in deps_dict
        assert deps_dict[job2_id] == []

        assert job3_id in deps_dict
        # Should have both dependencies
        assert set(deps_dict[job3_id]) == {job1_id, job2_id}

    @pytest.mark.asyncio
    async def test_empty_dag(self, db_connection):
        """Test function with empty DAG."""
        # Create empty DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Empty DAG",
        )

        # Get dependencies
        deps = await db_connection.fetch(
            """
            SELECT * FROM get_dag_dependencies($1)
        """,
            dag_id,
        )

        assert len(deps) == 0


class TestValidateDAGAcyclicFunction:
    """Test validate_dag_acyclic() SQL function (Migration 008)."""

    @pytest.mark.asyncio
    async def test_function_exists(self, db_connection):
        """Verify validate_dag_acyclic function exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = 'public' AND p.proname = 'validate_dag_acyclic'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_validates_simple_dag(self, db_connection):
        """Test validation of simple linear DAG."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Valid Linear DAG",
        )

        # Create linear chain
        job1_id = await create_job(db_connection, job_class="test.Job1")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job1_id
        )

        job2_id = await create_job(
            db_connection, job_class="test.Job2", waitfor_job=job1_id
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job2_id
        )

        # Validate
        is_valid = await db_connection.fetchval(
            """
            SELECT validate_dag_acyclic($1)
        """,
            dag_id,
        )

        assert is_valid is True

    @pytest.mark.asyncio
    async def test_validates_diamond_dag(self, db_connection):
        """Test validation of diamond pattern DAG."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Diamond DAG",
        )

        # Create diamond: A -> B,C -> D
        job_a = await create_job(db_connection, job_class="test.A")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job_a
        )

        job_b = await create_job(db_connection, job_class="test.B", waitfor_job=job_a)
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job_b
        )

        job_c = await create_job(db_connection, job_class="test.C", waitfor_job=job_a)
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job_c
        )

        # D depends on both B and C (using jorb_dependencies)
        job_d = await create_job(db_connection, job_class="test.D")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job_d
        )
        await db_connection.execute(
            """
            INSERT INTO jorb_dependencies (job_id, depends_on_job_id)
            VALUES ($1, $2), ($1, $3)
        """,
            job_d,
            job_b,
            job_c,
        )

        # Validate
        is_valid = await db_connection.fetchval(
            """
            SELECT validate_dag_acyclic($1)
        """,
            dag_id,
        )

        assert is_valid is True

    @pytest.mark.asyncio
    async def test_validates_empty_dag(self, db_connection):
        """Test validation of empty DAG."""
        # Create empty DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Empty DAG",
        )

        # Validate
        is_valid = await db_connection.fetchval(
            """
            SELECT validate_dag_acyclic($1)
        """,
            dag_id,
        )

        # Empty DAG is valid
        assert is_valid is True

    @pytest.mark.asyncio
    async def test_detects_simple_cycle(self, db_connection):
        """Test detection of simple 2-node cycle."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Cyclic DAG",
        )

        # Create job1 -> job2
        job1_id = await create_job(db_connection, job_class="test.Job1")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job1_id
        )

        job2_id = await create_job(
            db_connection, job_class="test.Job2", waitfor_job=job1_id
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job2_id
        )

        # Create cycle: job2 -> job1 (using jorb_dependencies)
        await db_connection.execute(
            """
            INSERT INTO jorb_dependencies (job_id, depends_on_job_id)
            VALUES ($1, $2)
        """,
            job1_id,
            job2_id,
        )

        # Validate - should detect cycle
        is_valid = await db_connection.fetchval(
            """
            SELECT validate_dag_acyclic($1)
        """,
            dag_id,
        )

        assert is_valid is False


class TestAutoCompleteDAGTrigger:
    """Test auto_complete_dag() trigger function (Migration 008)."""

    @pytest.mark.asyncio
    async def test_trigger_function_exists(self, db_connection):
        """Verify auto_complete_dag trigger function exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = 'public' AND p.proname = 'auto_complete_dag'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_trigger_exists(self, db_connection):
        """Verify auto_complete_dag_trigger exists on jorb table."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_trigger
                WHERE tgname = 'auto_complete_dag_trigger'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_completes_dag_when_all_jobs_finish(self, db_connection):
        """Test that DAG is marked complete when all jobs finish."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Complete Test DAG",
        )

        # Create 3 running jobs
        job_ids = []
        for i in range(3):
            job_id = await create_job(
                db_connection, job_class=f"test.Job{i}", state="running"
            )
            await db_connection.execute(
                "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job_id
            )
            job_ids.append(job_id)

        # Verify DAG not completed yet
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["completed"] is None

        # Finish first two jobs
        for job_id in job_ids[:2]:
            await db_connection.execute(
                "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
            )

        # DAG still not completed
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["completed"] is None

        # Finish last job - should trigger completion
        await db_connection.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_ids[2]
        )

        # DAG should now be completed
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["completed"] is not None

    @pytest.mark.asyncio
    async def test_completes_dag_with_crashed_jobs(self, db_connection):
        """Test that DAG completes even with crashed jobs."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Failed DAG",
        )

        # Create 2 jobs
        job1_id = await create_job(
            db_connection, job_class="test.Job1", state="running"
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job1_id
        )

        job2_id = await create_job(
            db_connection, job_class="test.Job2", state="running"
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job2_id
        )

        # Mark one finished, one crashed
        await db_connection.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job1_id
        )
        await db_connection.execute(
            "UPDATE jorb SET state = 'crashed' WHERE id = $1", job2_id
        )

        # DAG should be completed (all jobs in terminal state)
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["completed"] is not None

    @pytest.mark.asyncio
    async def test_does_not_complete_with_pending_jobs(self, db_connection):
        """Test that DAG does not complete while jobs are still pending."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Incomplete DAG",
        )

        # Create 3 jobs: 2 finished, 1 queued
        job1_id = await create_job(
            db_connection, job_class="test.Job1", state="finished"
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job1_id
        )

        job2_id = await create_job(
            db_connection, job_class="test.Job2", state="finished"
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job2_id
        )

        job3_id = await create_job(db_connection, job_class="test.Job3", state="queued")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job3_id
        )

        # DAG should NOT be completed
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["completed"] is None

    @pytest.mark.asyncio
    async def test_does_not_recomplete_dag(self, db_connection):
        """Test that trigger doesn't update completed timestamp if already set."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name, completed) VALUES ($1, NOW()) RETURNING id
        """,
            "Already Complete DAG",
        )

        # Create finished job
        job_id = await create_job(db_connection, job_class="test.Job", state="finished")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job_id
        )

        # Get original completed timestamp
        original_completed = await db_connection.fetchval(
            "SELECT completed FROM jorb_dag WHERE id = $1", dag_id
        )

        # Wait a bit
        await asyncio.sleep(0.1)

        # Update job state (simulating re-trigger)
        await db_connection.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
        )

        # Completed timestamp should not change
        new_completed = await db_connection.fetchval(
            "SELECT completed FROM jorb_dag WHERE id = $1", dag_id
        )

        assert original_completed == new_completed


class TestSQLFunctionsIntegration:
    """Integration tests combining multiple SQL functions."""

    @pytest.mark.asyncio
    async def test_retry_calculation_in_timeout_handler(self, db_connection):
        """Test using calculate_retry_delay in timeout handling."""
        # Create job with retry config
        admin_data = {
            "timeout_seconds": 60,
            "on_timeout": "retry",
            "max_retries": 5,
            "retry_strategy": "exponential",
            "initial_retry_delay": 2,
        }

        job_id = await create_job(
            db_connection,
            job_class="test.TimeoutRetry",
            state="running",
            admin_data=admin_data,
        )

        # Set timeout in past
        await db_connection.execute(
            """
            UPDATE jorb
            SET timeout_at = NOW() - INTERVAL '10 seconds',
                error_count = 2
            WHERE id = $1
        """,
            job_id,
        )

        # Find timed-out job
        timed_out = await db_connection.fetch("""
            SELECT * FROM check_timed_out_jobs()
        """)

        job_row = next((r for r in timed_out if r["job_id"] == job_id), None)
        assert job_row is not None

        # Calculate retry delay
        retry_delay = await db_connection.fetchval(
            """
            SELECT calculate_retry_delay(
                $1::INT,
                $2::TEXT,
                $3::INT,
                3600,
                2.0
            )
        """,
            job_row["error_count"] + 1,
            admin_data.get("retry_strategy", "exponential"),
            admin_data.get("initial_retry_delay", 1),
        )

        # Should get exponential delay for attempt 3
        assert retry_delay > 0

    @pytest.mark.asyncio
    async def test_dag_validation_before_execution(self, db_connection):
        """Test validating DAG before getting dependencies."""
        # Create valid DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Validate Before Deps",
        )

        job1_id = await create_job(db_connection, job_class="test.Job1")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job1_id
        )

        job2_id = await create_job(
            db_connection, job_class="test.Job2", waitfor_job=job1_id
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job2_id
        )

        # Validate first
        is_valid = await db_connection.fetchval(
            """
            SELECT validate_dag_acyclic($1)
        """,
            dag_id,
        )
        assert is_valid is True

        # Then get dependencies
        deps = await db_connection.fetch(
            """
            SELECT * FROM get_dag_dependencies($1)
        """,
            dag_id,
        )

        assert len(deps) == 2
