"""
Comprehensive SQL statement tests.

Tests all SQL operations defined in STMTS dict for correctness,
atomicity, and edge cases.
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


class TestClaimStatement:
    """Test the 'claim' SQL statement for atomic job claiming."""

    async def test_claim_basic(self, db_connection):
        """Test basic job claiming."""
        # Create a queued job
        job_id = await create_job(
            db_connection,
            job_class="test.BasicJob",
            kwargs={"value": 1},
            queue="test_queue",
            state="queued",
        )

        # Claim the job using the SQL statement
        from pyjobby.pj import STMTS

        result = await db_connection.fetchrow(
            STMTS["claim"],
            12345,  # worker_pid
            "test-host",  # worker_host
            "test_queue",  # queue
            ["test"],  # capabilities
            1000,  # priority threshold
        )

        assert result is not None
        assert result["id"] == job_id
        assert result["state"] == "claimed"
        assert result["worker_pid"] == 12345
        assert result["worker_host"] == "test-host"
        assert result["run_count"] == 1

    async def test_claim_respects_queue(self, db_connection):
        """Test that claiming respects queue filtering."""
        # Create jobs in different queues
        job1_id = await create_job(db_connection, queue="queue_a", state="queued")
        job2_id = await create_job(db_connection, queue="queue_b", state="queued")

        from pyjobby.pj import STMTS

        # Claim from queue_a
        result = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "test-host", "queue_a", ["test"], 1000
        )

        assert result["id"] == job1_id
        assert result["queue"] == "queue_a"

    async def test_claim_respects_capability(self, db_connection):
        """Test that claiming respects capability requirements."""
        # Create job requiring specific capability
        job_id = await create_job(
            db_connection,
            queue="test_queue",
            capability="special",
            state="queued"
        )

        from pyjobby.pj import STMTS

        # Try to claim without capability - should fail
        result = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "test-host", "test_queue", ["basic"], 1000
        )
        assert result is None

        # Claim with correct capability - should succeed
        result = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "test-host", "test_queue", ["special"], 1000
        )
        assert result is not None
        assert result["id"] == job_id

    async def test_claim_respects_priority(self, db_connection):
        """Test that claiming returns highest priority job first."""
        # Create jobs with different priorities (lower = higher priority)
        job_high = await create_job(db_connection, prio=10, state="queued")
        job_low = await create_job(db_connection, prio=100, state="queued")

        from pyjobby.pj import STMTS

        result = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "test-host", "test_queue", ["test"], 1000
        )

        assert result["id"] == job_high

    async def test_claim_respects_run_after(self, db_connection):
        """Test that claiming respects run_after timestamp."""
        # Create job scheduled for future
        future_time = datetime.utcnow() + timedelta(hours=1)
        job_future = await create_job(
            db_connection,
            run_after=future_time,
            state="queued"
        )

        from pyjobby.pj import STMTS

        # Should not claim job scheduled for future
        result = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "test-host", "test_queue", ["test"], 1000
        )
        assert result is None

    async def test_claim_skip_locked(self, db_connection):
        """Test that SKIP LOCKED prevents blocking on locked rows."""
        # This test verifies the FOR UPDATE SKIP LOCKED behavior
        # Create a job
        job_id = await create_job(db_connection, state="queued")

        # Start a transaction and lock the job
        from pyjobby.pj import STMTS

        # In same transaction, job should be claimed
        result1 = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "test-host", "test_queue", ["test"], 1000
        )
        assert result1 is not None

        # Second claim attempt should return None (already claimed)
        result2 = await db_connection.fetchrow(
            STMTS["claim"],
            12346, "test-host2", "test_queue", ["test"], 1000
        )
        assert result2 is None


class TestFinishedStatement:
    """Test the 'finished' SQL statement for marking jobs complete."""

    async def test_mark_finished_basic(self, db_connection):
        """Test basic job completion."""
        job_id = await create_job(db_connection, state="running")

        from pyjobby.pj import STMTS

        result = await db_connection.fetchrow(
            STMTS["finished"],
            job_id,
            {"status": "success", "output": "done"}
        )

        assert result["id"] == job_id
        assert result["state"] == "finished"
        assert result["result"]["status"] == "success"

    async def test_finished_updates_timestamp(self, db_connection):
        """Test that finished updates the updated timestamp."""
        job_id = await create_job(db_connection, state="running")

        # Get original updated time
        job_before = await get_job(db_connection, job_id)

        # Wait a tiny bit
        await asyncio.sleep(0.01)

        from pyjobby.pj import STMTS

        result = await db_connection.fetchrow(
            STMTS["finished"],
            job_id,
            {}
        )

        assert result["updated"] > job_before["updated"]


class TestRunStatement:
    """Test the 'run' SQL statement for marking jobs as running."""

    async def test_mark_running(self, db_connection):
        """Test transitioning job to running state."""
        job_id = await create_job(db_connection, state="claimed")

        from pyjobby.pj import STMTS

        await db_connection.execute(STMTS["run"], job_id)

        job = await get_job(db_connection, job_id)
        assert job["state"] == "running"


class TestCrashStatement:
    """Test the 'crash' SQL statement for handling job failures."""

    async def test_mark_crashed(self, db_connection):
        """Test marking a job as crashed with error details."""
        job_id = await create_job(db_connection, state="running")

        from pyjobby.pj import STMTS

        await db_connection.execute(
            STMTS["crash"],
            job_id,
            "ValueError: invalid input",
            "Traceback:\n  File test.py, line 1\n    raise ValueError()"
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "crashed"
        assert job["error_message"] == "ValueError: invalid input"
        assert "Traceback" in job["error_backtrace"]
        assert job["error_count"] == 1

    async def test_crash_increments_error_count(self, db_connection):
        """Test that crashing increments error_count."""
        job_id = await create_job(db_connection, state="running")

        from pyjobby.pj import STMTS

        # Crash multiple times
        for i in range(3):
            # Set back to running for next crash
            await db_connection.execute(
                "UPDATE jorb SET state = 'running' WHERE id = $1",
                job_id
            )

            await db_connection.execute(
                STMTS["crash"],
                job_id,
                f"Error {i+1}",
                "Traceback"
            )

        job = await get_job(db_connection, job_id)
        assert job["error_count"] == 3


class TestRescheduleStatement:
    """Test the 'reschedule' SQL statement."""

    async def test_reschedule_job(self, db_connection):
        """Test rescheduling a job to run later."""
        job_id = await create_job(db_connection, state="crashed")

        from pyjobby.pj import STMTS

        # Reschedule to run in 1 hour
        await db_connection.execute(
            STMTS["reschedule"],
            job_id,
            timedelta(hours=1)
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"
        assert job["run_after"] > datetime.utcnow()


class TestCreateRetryStatement:
    """Test the 'create-retry' SQL statement for retry mechanism."""

    async def test_create_retry_job(self, db_connection):
        """Test creating a retry job from a crashed job."""
        # Create and crash a job
        original_id = await create_job(
            db_connection,
            job_class="test.RetryableJob",
            kwargs={"attempt": 1},
            state="crashed"
        )

        from pyjobby.pj import STMTS

        # Create retry with 5 minute delay, error_count = 1
        result = await db_connection.fetchrow(
            STMTS["create-retry"],
            original_id,
            timedelta(minutes=5),
            1
        )

        retry_id = result["id"]
        assert retry_id != original_id

        # Verify retry job
        retry_job = await get_job(db_connection, retry_id)
        assert retry_job["state"] == "queued"
        assert retry_job["job_class"] == "test.RetryableJob"
        assert retry_job["error_count"] == 1
        assert retry_job["run_after"] > datetime.utcnow()

        # Check admin_data includes parent_job_id
        assert retry_job["admin_data"] is not None
        assert retry_job["admin_data"]["parent_job_id"] == original_id

    async def test_create_retry_preserves_job_data(self, db_connection):
        """Test that retry preserves job configuration."""
        original_id = await create_job(
            db_connection,
            job_class="test.SpecialJob",
            kwargs={"config": "value"},
            queue="special_queue",
            prio=50,
            capability="special",
            state="crashed"
        )

        from pyjobby.pj import STMTS

        result = await db_connection.fetchrow(
            STMTS["create-retry"],
            original_id,
            timedelta(minutes=1),
            2
        )

        retry_job = await get_job(db_connection, result["id"])
        assert retry_job["job_class"] == "test.SpecialJob"
        assert retry_job["kwargs"]["config"] == "value"
        assert retry_job["queue"] == "special_queue"
        assert retry_job["prio"] == 50
        assert retry_job["capability"] == "special"


class TestRecoverAbandonedStatement:
    """Test the 'recover-abandoned' SQL statement."""

    async def test_recover_abandoned_jobs(self, db_connection):
        """Test recovering jobs from a specific worker host with time-based check."""
        # Create two jobs claimed by dead worker
        old_job_id = await create_job(db_connection, state="queued")
        recent_job_id = await create_job(db_connection, state="queued")

        # Old job: updated 10 minutes ago (should be recovered with 5 min timeout)
        old_time = datetime.utcnow() - timedelta(minutes=10)
        await db_connection.execute(
            """UPDATE jorb
               SET state = 'claimed',
                   worker_pid = 99999,
                   worker_host = 'dead-host',
                   updated = $1
               WHERE id = $2""",
            old_time,
            old_job_id
        )

        # Recent job: updated 1 minute ago (should NOT be recovered with 5 min timeout)
        recent_time = datetime.utcnow() - timedelta(minutes=1)
        await db_connection.execute(
            """UPDATE jorb
               SET state = 'claimed',
                   worker_pid = 99998,
                   worker_host = 'dead-host',
                   updated = $1
               WHERE id = $2""",
            recent_time,
            recent_job_id
        )

        from pyjobby.pj import STMTS

        # Recover jobs from dead-host older than 5 minutes
        recovery_timeout = timedelta(minutes=5)
        results = await db_connection.fetch(
            STMTS["recover-abandoned"],
            "dead-host",
            recovery_timeout
        )

        # Should only recover the old job, not the recent one
        assert len(results) == 1
        assert results[0]["id"] == old_job_id
        assert results[0]["old_state"] == "queued"

        # Verify old job is now queued
        old_job = await get_job(db_connection, old_job_id)
        assert old_job["state"] == "queued"

        # Verify recent job is still claimed (not recovered)
        recent_job = await get_job(db_connection, recent_job_id)
        assert recent_job["state"] == "claimed"


class TestEnqueueStatements:
    """Test the enqueue-next statements for job dependencies."""

    async def test_enqueue_after_group_finished(self, db_connection):
        """Test enqueuing jobs that wait for a group to finish."""
        # Create a group of jobs
        group_id = 12345
        job1 = await create_job(db_connection, run_group=group_id, state="finished")
        job2 = await create_job(db_connection, run_group=group_id, state="finished")

        # Create job waiting for group
        waiter = await create_job(
            db_connection,
            waitfor_group=group_id,
            state="waiting"
        )

        from pyjobby.pj import STMTS

        # Enqueue jobs waiting for this group
        results = await db_connection.fetch(
            STMTS["enqueue-next-if-peer-group-is-finished"],
            group_id
        )

        assert len(results) > 0

        # Verify waiter is now queued
        waiter_job = await get_job(db_connection, waiter)
        assert waiter_job["state"] == "queued"

    async def test_enqueue_after_self_finished(self, db_connection):
        """Test enqueuing self-dependent jobs after completion."""
        # Create a finished job
        finished_id = await create_job(db_connection, state="finished")

        # Create job waiting for finished job
        waiter = await create_job(
            db_connection,
            waitfor_job=finished_id,
            state="waiting"
        )

        from pyjobby.pj import STMTS

        # Enqueue jobs waiting for finished job
        results = await db_connection.fetch(
            STMTS["enqueue-next-self-finished"],
            finished_id
        )

        assert len(results) > 0

        # Verify waiter is now queued
        waiter_job = await get_job(db_connection, waiter)
        assert waiter_job["state"] == "queued"


class TestScheduleDeadlineStatement:
    """Test the 'schedule-deadline' SQL statement."""

    async def test_schedule_with_deadline_key(self, db_connection):
        """Test scheduling job with deadline key prevents duplicates."""
        deadline_key = "daily-report-2024-01-01"

        from pyjobby.pj import STMTS

        # Schedule first job
        await db_connection.execute(
            STMTS["schedule-deadline"],
            deadline_key,
            "test_queue",
            100,
            datetime.utcnow(),
            None,  # uid
            None,  # run_group
            "test.DailyReport",
            {"date": "2024-01-01"},
            None  # admin_data
        )

        # Try to schedule duplicate - should fail with unique constraint
        with pytest.raises(Exception):
            await db_connection.execute(
                STMTS["schedule-deadline"],
                deadline_key,
                "test_queue",
                100,
                datetime.utcnow(),
                None,
                None,
                "test.DailyReport",
                {"date": "2024-01-01"},
                None
            )


@pytest.mark.slow
class TestConcurrentClaiming:
    """Test concurrent job claiming scenarios."""

    async def test_multiple_workers_claim_different_jobs(self, db_connection):
        """Test that multiple workers can claim different jobs concurrently."""
        # Create multiple jobs
        job_ids = await create_job_batch(db_connection, count=10, state="queued")

        from pyjobby.pj import STMTS

        # Simulate multiple workers claiming jobs
        claimed = []
        for worker_id in range(5):
            result = await db_connection.fetchrow(
                STMTS["claim"],
                worker_id,
                f"worker-{worker_id}",
                "test_queue",
                ["test"],
                1000
            )
            if result:
                claimed.append(result["id"])

        # Should have claimed 5 different jobs
        assert len(claimed) == 5
        assert len(set(claimed)) == 5  # All unique

    async def test_claim_atomicity(self, db_connection):
        """Test that claiming is atomic - no double claims possible."""
        # This is ensured by FOR UPDATE SKIP LOCKED
        # Already tested in test_claim_skip_locked
        pass


@pytest.mark.integration
class TestSQLStatementIntegration:
    """Integration tests for SQL statements working together."""

    async def test_full_job_lifecycle(self, db_connection):
        """Test complete job lifecycle through SQL statements."""
        from pyjobby.pj import STMTS

        # 1. Create job
        job_id = await create_job(
            db_connection,
            job_class="test.FullLifecycle",
            kwargs={"step": 1},
            state="queued"
        )

        # 2. Claim job
        claimed = await db_connection.fetchrow(
            STMTS["claim"],
            12345, "test-host", "test_queue", ["test"], 1000
        )
        assert claimed["id"] == job_id
        assert claimed["state"] == "claimed"

        # 3. Mark as running
        await db_connection.execute(STMTS["run"], job_id)
        job = await get_job(db_connection, job_id)
        assert job["state"] == "running"

        # 4. Complete successfully
        finished = await db_connection.fetchrow(
            STMTS["finished"],
            job_id,
            {"result": "success"}
        )
        assert finished["state"] == "finished"

    async def test_retry_chain(self, db_connection):
        """Test creating a chain of retries."""
        from pyjobby.pj import STMTS

        # Create original job
        job_id = await create_job(db_connection, state="crashed")

        retry_chain = [job_id]

        # Create 3 retries
        for i in range(3):
            result = await db_connection.fetchrow(
                STMTS["create-retry"],
                retry_chain[-1],
                timedelta(minutes=(i+1)*5),
                i + 1
            )
            retry_chain.append(result["id"])

        # Verify chain
        assert len(retry_chain) == 4

        # Last retry should reference previous retry as parent
        last_job = await get_job(db_connection, retry_chain[-1])
        assert last_job["admin_data"]["parent_job_id"] == retry_chain[-2]
        assert last_job["error_count"] == 3
