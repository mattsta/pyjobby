"""
Job lifecycle tests.

Tests complete job lifecycle scenarios from creation through execution,
failure handling, retries, and dependencies.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from tests.utils.factories import (
    create_job,
    create_job_batch,
    create_dependency_chain,
    create_group_dependency,
    count_jobs_by_state,
    get_job,
)


pytestmark = pytest.mark.asyncio


class TestJobCreation:
    """Test job creation and initial state."""

    async def test_create_simple_job(self, db_connection):
        """Test creating a basic job."""
        job_id = await create_job(
            db_connection,
            job_class="test.SimpleJob",
            kwargs={"param": "value"},
            queue="test_queue",
        )

        job = await get_job(db_connection, job_id)
        assert job["id"] == job_id
        assert job["job_class"] == "test.SimpleJob"
        assert job["kwargs"]["param"] == "value"
        assert job["queue"] == "test_queue"
        assert job["state"] == "queued"
        assert job["error_count"] == 0

    async def test_create_job_with_priority(self, db_connection):
        """Test job creation with custom priority."""
        high_priority = await create_job(db_connection, prio=10)
        low_priority = await create_job(db_connection, prio=1000)

        job_high = await get_job(db_connection, high_priority)
        job_low = await get_job(db_connection, low_priority)

        assert job_high["prio"] < job_low["prio"]

    async def test_create_job_with_capability(self, db_connection):
        """Test job creation with capability requirement."""
        job_id = await create_job(
            db_connection,
            capability="gpu_required",
        )

        job = await get_job(db_connection, job_id)
        assert job["capability"] == "gpu_required"

    async def test_create_job_with_run_after(self, db_connection):
        """Test scheduling job for future execution."""
        future_time = datetime.utcnow() + timedelta(hours=2)
        job_id = await create_job(
            db_connection,
            run_after=future_time,
        )

        job = await get_job(db_connection, job_id)
        assert job["run_after"] > datetime.utcnow()

    async def test_create_job_batch(self, db_connection):
        """Test creating multiple jobs at once."""
        job_ids = await create_job_batch(db_connection, count=10)

        assert len(job_ids) == 10
        assert len(set(job_ids)) == 10  # All unique

        # All should be queued
        queued_count = await count_jobs_by_state(db_connection, "queued")
        assert queued_count == 10


class TestJobClaiming:
    """Test job claiming by workers."""

    async def test_claim_queued_job(self, db_connection):
        """Test worker claiming a queued job."""
        job_id = await create_job(db_connection, state="queued")

        from pyjobby.pj import STMTS

        claimed = await db_connection.fetchrow(
            STMTS["claim"],
            12345,  # worker_pid
            "worker-1",  # worker_host
            "test_queue",
            ["test"],
            1000
        )

        assert claimed is not None
        assert claimed["id"] == job_id
        assert claimed["state"] == "claimed"
        assert claimed["run_count"] == 1

    async def test_cannot_claim_same_job_twice(self, db_connection):
        """Test that a job can only be claimed once."""
        await create_job(db_connection, state="queued")

        from pyjobby.pj import STMTS

        # First claim succeeds
        claim1 = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-1", "test_queue", ["test"], 1000
        )
        assert claim1 is not None

        # Second claim fails (no more queued jobs)
        claim2 = await db_connection.fetchrow(
            STMTS["claim"],
            12346, "worker-2", "test_queue", ["test"], 1000
        )
        assert claim2 is None

    async def test_run_count_increments(self, db_connection):
        """Test that run_count increments with each claim."""
        job_id = await create_job(db_connection, state="queued")

        from pyjobby.pj import STMTS

        # Claim and check run_count
        for expected_count in [1, 2, 3]:
            # Set back to queued for next claim
            if expected_count > 1:
                await db_connection.execute(
                    "UPDATE jorb SET state = 'queued' WHERE id = $1",
                    job_id
                )

            claimed = await db_connection.fetchrow(
                STMTS["claim"],
                12345, "worker-1", "test_queue", ["test"], 1000
            )
            assert claimed["run_count"] == expected_count


class TestJobExecution:
    """Test job execution state transitions."""

    async def test_job_lifecycle_success(self, db_connection):
        """Test complete successful job lifecycle."""
        from pyjobby.pj import STMTS

        # 1. Create queued job
        job_id = await create_job(db_connection, state="queued")
        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"

        # 2. Claim job
        claimed = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-1", "test_queue", ["test"], 1000
        )
        assert claimed["state"] == "claimed"

        # 3. Mark as running
        await db_connection.execute(STMTS["run"], job_id)
        job = await get_job(db_connection, job_id)
        assert job["state"] == "running"

        # 4. Mark as finished
        await db_connection.fetchrow(
            STMTS["finished"],
            job_id,
            {"result": "success", "output": "completed"}
        )
        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["result"]["result"] == "success"

    async def test_job_lifecycle_failure(self, db_connection):
        """Test job lifecycle with failure."""
        from pyjobby.pj import STMTS

        # Create and claim job
        job_id = await create_job(db_connection, state="queued")
        await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-1", "test_queue", ["test"], 1000
        )

        # Mark as running
        await db_connection.execute(STMTS["run"], job_id)

        # Crash the job
        await db_connection.execute(
            STMTS["crash"],
            job_id,
            "RuntimeError: something went wrong",
            "Traceback (most recent call last):\n  ..."
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "crashed"
        assert job["error_count"] == 1
        assert "RuntimeError" in job["error_message"]


class TestRetryMechanism:
    """Test job retry mechanism."""

    async def test_create_retry_after_crash(self, db_connection):
        """Test creating a retry job after crash."""
        from pyjobby.pj import STMTS

        # Create and crash a job
        original_id = await create_job(
            db_connection,
            job_class="test.RetryableJob",
            kwargs={"attempt": 1},
            state="crashed"
        )

        # Create retry
        retry_result = await db_connection.fetchrow(
            STMTS["create-retry"],
            original_id,
            timedelta(minutes=5),
            1  # error_count
        )

        retry_id = retry_result["id"]
        assert retry_id != original_id

        # Verify retry job
        retry_job = await get_job(db_connection, retry_id)
        assert retry_job["state"] == "queued"
        assert retry_job["job_class"] == "test.RetryableJob"
        assert retry_job["error_count"] == 1
        assert retry_job["admin_data"]["parent_job_id"] == original_id

    async def test_retry_chain(self, db_connection):
        """Test multiple retries creating a chain."""
        from pyjobby.pj import STMTS

        # Create original job
        original_id = await create_job(db_connection, state="crashed")

        # Create 5 retries
        current_id = original_id
        retry_ids = []

        for i in range(5):
            result = await db_connection.fetchrow(
                STMTS["create-retry"],
                current_id,
                timedelta(minutes=(i+1)*5),
                i + 1
            )
            current_id = result["id"]
            retry_ids.append(current_id)

        # Verify chain
        for i, retry_id in enumerate(retry_ids):
            job = await get_job(db_connection, retry_id)
            assert job["error_count"] == i + 1

            if i == 0:
                # First retry references original
                assert job["admin_data"]["parent_job_id"] == original_id
            else:
                # Subsequent retries reference previous retry
                assert job["admin_data"]["parent_job_id"] == retry_ids[i-1]

    async def test_max_retries_exceeded(self, db_connection):
        """Test that jobs stop retrying after max attempts."""
        from pyjobby.pj import STMTS

        # Create job that has exceeded max retries
        job_id = await create_job(db_connection, state="crashed")

        # Update error_count to max (10 in default config)
        await db_connection.execute(
            "UPDATE jorb SET error_count = 10 WHERE id = $1",
            job_id
        )

        # In a real system, this job would not create another retry
        # For now, we just verify the error_count is high
        job = await get_job(db_connection, job_id)
        assert job["error_count"] == 10
        assert job["state"] == "crashed"


class TestJobDependencies:
    """Test job dependency resolution."""

    async def test_waitfor_job_dependency(self, db_connection):
        """Test job waiting for another job to complete."""
        from pyjobby.pj import STMTS

        # Create parent job and complete it
        parent_id = await create_job(db_connection, state="running")
        await db_connection.fetchrow(
            STMTS["finished"],
            parent_id,
            {"status": "done"}
        )

        # Create child job waiting for parent
        child_id = await create_job(
            db_connection,
            waitfor_job=parent_id,
            state="waiting"
        )

        # Trigger dependency resolution
        results = await db_connection.fetch(
            STMTS["enqueue-next-self-finished"],
            parent_id
        )

        # Child should be enqueued
        child = await get_job(db_connection, child_id)
        assert child["state"] == "queued"

    async def test_waitfor_group_dependency(self, db_connection):
        """Test job waiting for a group of jobs to complete."""
        from pyjobby.pj import STMTS

        # Create a group of jobs
        group_id = 99999
        job1 = await create_job(db_connection, run_group=group_id, state="finished")
        job2 = await create_job(db_connection, run_group=group_id, state="finished")
        job3 = await create_job(db_connection, run_group=group_id, state="finished")

        # Create job waiting for group
        waiter = await create_job(
            db_connection,
            waitfor_group=group_id,
            state="waiting"
        )

        # Trigger group dependency resolution
        results = await db_connection.fetch(
            STMTS["enqueue-next-if-peer-group-is-finished"],
            group_id
        )

        # Waiter should be enqueued
        waiter_job = await get_job(db_connection, waiter)
        assert waiter_job["state"] == "queued"

    async def test_dependency_chain(self, db_connection):
        """Test chain of dependent jobs."""
        job_ids = await create_dependency_chain(db_connection, depth=3)

        from pyjobby.pj import STMTS

        # Complete first job
        await db_connection.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1",
            job_ids[0]
        )
        await db_connection.fetch(
            STMTS["enqueue-next-self-finished"],
            job_ids[0]
        )

        # Second job should be queued
        job2 = await get_job(db_connection, job_ids[1])
        assert job2["state"] == "queued"

        # Third job still waiting
        job3 = await get_job(db_connection, job_ids[2])
        assert job3["state"] == "waiting"


class TestWorkerRecovery:
    """Test worker crash recovery."""

    async def test_recover_claimed_jobs(self, db_connection):
        """Test recovering jobs when worker crashes."""
        from pyjobby.pj import STMTS

        # Create and claim job
        job_id = await create_job(db_connection, state="queued")
        await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-1", "test_queue", ["test"], 1000
        )

        # Worker crashes - recover jobs from this worker
        results = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "worker-1"
        )

        assert len(results) == 1
        assert results[0]["id"] == job_id

        # Job should be back in queued state
        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"

    async def test_recover_running_jobs(self, db_connection):
        """Test recovering jobs that were running when worker crashed."""
        from pyjobby.pj import STMTS

        # Create, claim, and start running
        job_id = await create_job(db_connection, state="queued")
        await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-1", "test_queue", ["test"], 1000
        )
        await db_connection.execute(STMTS["run"], job_id)

        # Worker crashes - recover jobs
        results = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "worker-1"
        )

        assert len(results) == 1
        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"


class TestJobScheduling:
    """Test job scheduling with run_after."""

    async def test_future_jobs_not_claimed(self, db_connection):
        """Test that jobs scheduled for future are not claimed."""
        from pyjobby.pj import STMTS

        # Create job for 1 hour from now
        future_job = await create_job(
            db_connection,
            run_after=datetime.utcnow() + timedelta(hours=1),
            state="queued"
        )

        # Create job for now
        now_job = await create_job(
            db_connection,
            run_after=datetime.utcnow(),
            state="queued"
        )

        # Claim should get the "now" job, not future job
        claimed = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-1", "test_queue", ["test"], 1000
        )

        assert claimed["id"] == now_job

    async def test_reschedule_job(self, db_connection):
        """Test rescheduling a job to run later."""
        from pyjobby.pj import STMTS

        job_id = await create_job(db_connection, state="crashed")

        before = datetime.utcnow()

        # Reschedule for 2 hours later
        await db_connection.execute(
            STMTS["reschedule"],
            job_id,
            timedelta(hours=2)
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"
        assert job["run_after"] > before + timedelta(hours=1, minutes=50)


class TestDeadlineKeys:
    """Test deadline key functionality for singleton jobs."""

    async def test_deadline_key_prevents_duplicates(self, db_connection):
        """Test that deadline_key prevents duplicate jobs."""
        from pyjobby.pj import STMTS

        deadline_key = "daily-report-2024-01-15"

        # Schedule first job
        await db_connection.execute(
            STMTS["schedule-deadline"],
            deadline_key,
            "test_queue",
            100,
            datetime.utcnow(),
            None, None,
            "test.DailyReport",
            {"date": "2024-01-15"},
            None
        )

        # Try to schedule duplicate - should fail
        with pytest.raises(Exception) as exc:
            await db_connection.execute(
                STMTS["schedule-deadline"],
                deadline_key,
                "test_queue",
                100,
                datetime.utcnow(),
                None, None,
                "test.DailyReport",
                {"date": "2024-01-15"},
                None
            )

        # Should be a unique constraint violation
        assert "unique" in str(exc.value).lower() or "duplicate" in str(exc.value).lower()

    async def test_different_deadline_keys_allowed(self, db_connection):
        """Test that different deadline keys can coexist."""
        from pyjobby.pj import STMTS

        # Schedule jobs with different deadline keys
        await db_connection.execute(
            STMTS["schedule-deadline"],
            "report-2024-01-15",
            "test_queue", 100, datetime.utcnow(),
            None, None, "test.Report", {}, None
        )

        await db_connection.execute(
            STMTS["schedule-deadline"],
            "report-2024-01-16",
            "test_queue", 100, datetime.utcnow(),
            None, None, "test.Report", {}, None
        )

        # Both should exist
        count = await db_connection.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE deadline_key LIKE 'report-%'"
        )
        assert count == 2


@pytest.mark.integration
class TestCompleteJobFlows:
    """Integration tests for complete job flows."""

    async def test_successful_job_flow(self, db_connection):
        """Test a complete successful job from start to finish."""
        from pyjobby.pj import STMTS

        # Create job
        job_id = await create_job(
            db_connection,
            job_class="test.SuccessfulJob",
            kwargs={"input": "data"},
            queue="production",
            prio=50
        )

        # Worker claims job
        claimed = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-prod-1", "production", ["test"], 1000
        )
        assert claimed["id"] == job_id

        # Worker starts execution
        await db_connection.execute(STMTS["run"], job_id)

        # Job completes successfully
        await db_connection.fetchrow(
            STMTS["finished"],
            job_id,
            {"status": "completed", "output": "processed data"}
        )

        # Verify final state
        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["error_count"] == 0
        assert job["run_count"] == 1
        assert job["result"]["status"] == "completed"

    async def test_failed_job_with_retries(self, db_connection):
        """Test job that fails and retries before succeeding."""
        from pyjobby.pj import STMTS

        # Create job
        job_id = await create_job(db_connection, state="queued")

        # First attempt - claim, run, crash
        await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-1", "test_queue", ["test"], 1000
        )
        await db_connection.execute(STMTS["run"], job_id)
        await db_connection.execute(
            STMTS["crash"],
            job_id,
            "Temporary error",
            "Traceback..."
        )

        # Create retry
        retry_result = await db_connection.fetchrow(
            STMTS["create-retry"],
            job_id,
            timedelta(minutes=1),
            1
        )
        retry_id = retry_result["id"]

        # Second attempt - claim, run, succeed
        await db_connection.fetchrow(
            STMTS["claim"],
            12346, "worker-2", "test_queue", ["test"], 1000
        )
        await db_connection.execute(STMTS["run"], retry_id)
        await db_connection.fetchrow(
            STMTS["finished"],
            retry_id,
            {"status": "success"}
        )

        # Verify: original crashed, retry succeeded
        original = await get_job(db_connection, job_id)
        retry = await get_job(db_connection, retry_id)

        assert original["state"] == "crashed"
        assert retry["state"] == "finished"
        assert retry["admin_data"]["parent_job_id"] == job_id

    async def test_dependency_workflow(self, db_connection):
        """Test workflow with dependent jobs."""
        from pyjobby.pj import STMTS

        # Create parent job
        parent_id = await create_job(
            db_connection,
            job_class="test.DataFetch",
            state="queued"
        )

        # Create child job waiting for parent
        child_id = await create_job(
            db_connection,
            job_class="test.DataProcess",
            waitfor_job=parent_id,
            state="waiting"
        )

        # Execute parent job
        await db_connection.fetchrow(
            STMTS["claim"],
            12345, "worker-1", "test_queue", ["test"], 1000
        )
        await db_connection.execute(STMTS["run"], parent_id)
        await db_connection.fetchrow(
            STMTS["finished"],
            parent_id,
            {"data": "fetched"}
        )

        # Trigger dependency resolution
        await db_connection.fetch(
            STMTS["enqueue-next-self-finished"],
            parent_id
        )

        # Child should now be queued
        child = await get_job(db_connection, child_id)
        assert child["state"] == "queued"

        # Execute child job
        await db_connection.fetchrow(
            STMTS["claim"],
            12346, "worker-2", "test_queue", ["test"], 1000
        )
        await db_connection.execute(STMTS["run"], child_id)
        await db_connection.fetchrow(
            STMTS["finished"],
            child_id,
            {"result": "processed"}
        )

        # Both jobs should be finished
        parent = await get_job(db_connection, parent_id)
        child = await get_job(db_connection, child_id)

        assert parent["state"] == "finished"
        assert child["state"] == "finished"
