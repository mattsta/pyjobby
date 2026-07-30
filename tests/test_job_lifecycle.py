"""
Job lifecycle tests (schema v1).

Tests complete job lifecycle scenarios from creation through execution,
failure handling, same-row retries, and dependencies. A job keeps ONE row
for life: retries requeue that row (run_epoch advances on every claim and
every abandonment) and the
per-attempt audit trail lives in jorb_history.
"""

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from pyjobby.pj import STMTS
from tests.utils.factories import (
    count_jobs_by_state,
    create_dependency_chain,
    create_job,
    create_job_batch,
    get_job,
)

pytestmark = pytest.mark.asyncio


def past() -> datetime:
    """A run_after safely before the test transaction's now()."""
    return datetime.now(UTC) - timedelta(seconds=5)


async def claim(
    conn,
    queue,
    *,
    pid=12345,
    host="worker-1",
    caps=("test",),
    prio=1000,
    worker_id=None,
    app_version: str | None = None,
):
    """Claim the next job on `queue` (schema v1 seven-argument claim)."""
    return await conn.fetchrow(
        STMTS["claim"], pid, host, queue, list(caps), prio, worker_id, app_version
    )


class TestJobCreation:
    """Test job creation and initial state."""

    async def test_create_simple_job(self, db_connection, unique_queue):
        """Test creating a basic job."""
        job_id = await create_job(
            db_connection,
            job_class="test.SimpleJob",
            kwargs={"param": "value"},
            queue=unique_queue,
        )

        job = await get_job(db_connection, job_id)
        assert job["id"] == job_id
        assert job["job_class"] == "test.SimpleJob"
        assert job["kwargs"]["param"] == "value"
        assert job["queue"] == unique_queue
        assert job["state"] == "queued"
        assert job["error_count"] == 0
        assert job["run_epoch"] == 0  # no claims yet
        assert job["admin_data"] == {}  # jsonb NOT NULL DEFAULT '{}'

    async def test_create_job_with_priority(self, db_connection, unique_queue):
        """Test job creation with custom priority."""
        high_priority = await create_job(db_connection, queue=unique_queue, prio=10)
        low_priority = await create_job(db_connection, queue=unique_queue, prio=1000)

        job_high = await get_job(db_connection, high_priority)
        job_low = await get_job(db_connection, low_priority)

        # lower number = more urgent
        assert job_high["prio"] < job_low["prio"]

    async def test_create_job_with_capability(self, db_connection, unique_queue):
        """Test job creation with capability requirement."""
        job_id = await create_job(
            db_connection, queue=unique_queue, capability="gpu_required"
        )

        job = await get_job(db_connection, job_id)
        assert job["capability"] == "gpu_required"

    async def test_create_job_with_run_after(self, db_connection, unique_queue):
        """Test scheduling job for future execution."""
        future_time = datetime.now(UTC) + timedelta(hours=2)
        job_id = await create_job(
            db_connection, queue=unique_queue, run_after=future_time
        )

        job = await get_job(db_connection, job_id)
        assert job["run_after"] > datetime.now(UTC)
        assert job["run_after"].tzinfo is not None  # timestamptz round-trips aware

    async def test_create_job_batch(self, db_connection, unique_queue):
        """Test creating multiple jobs at once."""
        job_ids = await create_job_batch(db_connection, count=10, queue=unique_queue)

        assert len(job_ids) == 10
        assert len(set(job_ids)) == 10  # All unique

        # All should be queued
        queued_count = await count_jobs_by_state(db_connection, "queued")
        assert queued_count == 10

    async def test_insert_records_enqueued_history(self, db_connection, unique_queue):
        """Every INSERT writes an 'enqueued' jorb_history row (trigger)."""
        job_id = await create_job(db_connection, queue=unique_queue)

        history = await db_connection.fetch(
            "SELECT event, detail FROM jorb_history WHERE job_id = $1", job_id
        )
        assert len(history) == 1
        assert history[0]["event"] == "enqueued"
        assert history[0]["detail"]["queue"] == unique_queue


class TestJobClaiming:
    """Test job claiming by workers."""

    async def test_claim_queued_job(self, db_connection, unique_queue):
        """Test worker claiming a queued job."""
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        claimed = await claim(db_connection, unique_queue)

        assert claimed is not None
        assert claimed["id"] == job_id
        assert claimed["state"] == "claimed"
        assert claimed["run_count"] == 1
        assert claimed["run_epoch"] == 1

    async def test_cannot_claim_same_job_twice(self, db_connection, unique_queue):
        """Test that a job can only be claimed once."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        # First claim succeeds
        claim1 = await claim(db_connection, unique_queue)
        assert claim1 is not None

        # Second claim fails (no more queued jobs)
        claim2 = await claim(db_connection, unique_queue, pid=12346, host="worker-2")
        assert claim2 is None

    async def test_run_count_and_epoch_increment(self, db_connection, unique_queue):
        """run_count counts attempts; run_epoch only ever increases.

        The epoch is a fencing token, not a counter: it advances when a job
        enters an attempt AND when one abandons it, so it outruns run_count
        as soon as a retry happens.
        """
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        for expected in [1, 2, 3]:
            claimed = await claim(db_connection, unique_queue)
            assert claimed["id"] == job_id
            assert claimed["run_count"] == expected
            assert claimed["run_epoch"] >= expected
            # same-row requeue for the next attempt, fenced with the epoch
            # this attempt actually holds
            await db_connection.execute(
                STMTS["retry"],
                job_id,
                timedelta(seconds=-1),
                "err",
                "trace",
                claimed["run_epoch"],
            )


class TestJobExecution:
    """Test job execution state transitions."""

    async def test_job_lifecycle_success(self, db_connection, unique_queue):
        """Test complete successful job lifecycle."""
        # 1. Create queued job
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"

        # 2. Claim job
        claimed = await claim(db_connection, unique_queue)
        assert claimed["state"] == "claimed"
        epoch = claimed["run_epoch"]

        # 3. Mark as running
        await db_connection.execute(STMTS["run"], job_id, epoch, None)
        job = await get_job(db_connection, job_id)
        assert job["state"] == "running"
        assert job["started"] is not None

        # 4. Mark as finished
        await db_connection.fetchrow(
            STMTS["finished"],
            job_id,
            {"result": "success", "output": "completed"},
            epoch,
        )
        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["result"]["result"] == "success"

    async def test_job_lifecycle_failure(self, db_connection, unique_queue):
        """Test job lifecycle ending in terminal 'crashed' (the DLQ)."""
        # Create and claim job
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        epoch = claimed["run_epoch"]

        # Mark as running
        await db_connection.execute(STMTS["run"], job_id, epoch, None)

        # Dead-letter the job
        await db_connection.execute(
            STMTS["crashed"],
            job_id,
            "RuntimeError: something went wrong",
            "Traceback (most recent call last):\n  ...",
            epoch,
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "crashed"
        assert job["error_count"] == 1
        assert "RuntimeError" in job["error_message"]


class TestRetryMechanism:
    """Test the same-row retry mechanism."""

    async def test_retry_requeues_same_row(self, db_connection, unique_queue):
        """A failed attempt requeues the SAME row with backoff."""
        job_id = await create_job(
            db_connection,
            job_class="test.RetryableJob",
            kwargs={"attempt": 1},
            queue=unique_queue,
            state="queued",
            run_after=past(),
        )
        claimed = await claim(db_connection, unique_queue)

        retried = await db_connection.fetchrow(
            STMTS["retry"],
            job_id,
            timedelta(minutes=5),
            "boom",
            "Traceback...",
            claimed["run_epoch"],
        )

        # same id, back in the queue with backoff — no retry-copy rows
        assert retried["id"] == job_id
        job = await db_connection.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        assert job["state"] == "queued"
        assert job["job_class"] == "test.RetryableJob"
        assert job["error_count"] == 1
        assert "parent_job_id" not in job["admin_data"]
        assert (
            await db_connection.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 1
        )

    async def test_retry_attempts_recorded_in_history(
        self, db_connection, unique_queue
    ):
        """Multiple attempts keep one job id; jorb_history holds the trail."""
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        for attempt in range(1, 6):
            claimed = await claim(db_connection, unique_queue)
            assert claimed["id"] == job_id
            await db_connection.execute(
                STMTS["retry"],
                job_id,
                timedelta(seconds=-1),
                f"error {attempt}",
                "trace",
                claimed["run_epoch"],
            )
            job = await get_job(db_connection, job_id)
            assert job["error_count"] == attempt

        # each retry recorded a 'queued' transition carrying its error
        errors = [
            r["detail"]["error"]
            for r in await db_connection.fetch(
                """SELECT detail FROM jorb_history
                   WHERE job_id = $1 AND event = 'queued' ORDER BY id""",
                job_id,
            )
        ]
        assert errors == [f"error {n}" for n in range(1, 6)]

    async def test_crashed_is_terminal_dlq(self, db_connection, unique_queue):
        """After retries are exhausted the row is 'crashed': the DLQ query
        is simply WHERE state = 'crashed', and the row is not claimable."""
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        await db_connection.execute(
            STMTS["crashed"], job_id, "final failure", "trace", claimed["run_epoch"]
        )

        dlq = await db_connection.fetch(
            "SELECT id FROM jorb WHERE state = 'crashed' AND queue = $1", unique_queue
        )
        assert [r["id"] for r in dlq] == [job_id]
        assert await claim(db_connection, unique_queue) is None


class TestJobDependencies:
    """Test job dependency resolution."""

    async def test_waitfor_job_dependency(self, db_connection, unique_queue):
        """Test job waiting for another job to complete."""
        # Create parent job and complete it
        parent_id = await create_job(db_connection, queue=unique_queue, state="running")
        await db_connection.fetchrow(
            STMTS["finished"], parent_id, {"status": "done"}, 0
        )

        # Create child job waiting for parent
        child_id = await create_job(
            db_connection, queue=unique_queue, waitfor_job=parent_id, state="waiting"
        )

        # Trigger dependency resolution
        await db_connection.fetch(STMTS["enqueue-next-self-finished"], parent_id)

        # Child should be enqueued
        child = await get_job(db_connection, child_id)
        assert child["state"] == "queued"

    async def test_waitfor_group_dependency(self, db_connection, unique_queue):
        """Test job waiting for a group of jobs to complete."""
        group_id = 99999
        for _ in range(3):
            await create_job(
                db_connection,
                queue=unique_queue,
                run_group=group_id,
                state="finished",
            )

        # Create job waiting for group
        waiter = await create_job(
            db_connection, queue=unique_queue, waitfor_group=group_id, state="waiting"
        )

        # Trigger group dependency resolution
        await db_connection.fetch(
            STMTS["enqueue-next-if-peer-group-is-finished"], group_id
        )

        # Waiter should be enqueued
        waiter_job = await get_job(db_connection, waiter)
        assert waiter_job["state"] == "queued"

    async def test_dependency_chain(self, db_connection, unique_queue):
        """Test chain of dependent jobs."""
        job_ids = await create_dependency_chain(
            db_connection, depth=3, queue=unique_queue
        )

        # Complete first job
        await db_connection.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_ids[0]
        )
        await db_connection.fetch(STMTS["enqueue-next-self-finished"], job_ids[0])

        # Second job should be queued
        job2 = await get_job(db_connection, job_ids[1])
        assert job2["state"] == "queued"

        # Third job still waiting
        job3 = await get_job(db_connection, job_ids[2])
        assert job3["state"] == "waiting"


class TestJobScheduling:
    """Test job scheduling with run_after."""

    async def test_future_jobs_not_claimed(self, db_connection, unique_queue):
        """Test that jobs scheduled for future are not claimed."""
        # Create job for 1 hour from now
        await create_job(
            db_connection,
            queue=unique_queue,
            run_after=datetime.now(UTC) + timedelta(hours=1),
            state="queued",
        )

        # Create job runnable now
        now_job = await create_job(
            db_connection, queue=unique_queue, run_after=past(), state="queued"
        )

        # Claim should get the "now" job, not future job
        claimed = await claim(db_connection, unique_queue)
        assert claimed["id"] == now_job

        # and nothing else is claimable
        assert await claim(db_connection, unique_queue) is None

    async def test_reschedule_job(self, db_connection, unique_queue):
        """Test a running job rescheduling itself to run later."""
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        await db_connection.execute(STMTS["run"], job_id, claimed["run_epoch"], None)

        before = datetime.now(UTC)

        # Reschedule for 2 hours later — wins over completion, and is fenced
        # to the attempt that asked for it
        await db_connection.execute(
            STMTS["reschedule"], job_id, timedelta(hours=2), claimed["run_epoch"]
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"
        assert job["run_after"] > before + timedelta(hours=1, minutes=50)


class TestDeadlineKeys:
    """Test deadline key functionality for singleton jobs."""

    async def test_deadline_key_prevents_duplicates(self, db_connection, unique_queue):
        """One queued row per (deadline_key, queue) — enforced by index."""
        deadline_key = "daily-report-2024-01-15"

        await create_job(
            db_connection,
            job_class="test.DailyReport",
            kwargs={"date": "2024-01-15"},
            queue=unique_queue,
            deadline_key=deadline_key,
        )

        # Try to schedule duplicate - should fail
        with pytest.raises(asyncpg.UniqueViolationError):
            await create_job(
                db_connection,
                job_class="test.DailyReport",
                kwargs={"date": "2024-01-15"},
                queue=unique_queue,
                deadline_key=deadline_key,
            )

    async def test_different_deadline_keys_allowed(self, db_connection, unique_queue):
        """Test that different deadline keys can coexist."""
        for day in ("2024-01-15", "2024-01-16"):
            await create_job(
                db_connection,
                job_class="test.Report",
                queue=unique_queue,
                deadline_key=f"report-{day}",
            )

        count = await db_connection.fetchval(
            "SELECT COUNT(*) FROM jorb WHERE deadline_key LIKE 'report-%'"
        )
        assert count == 2

    async def test_deadline_key_free_after_terminal(self, db_connection, unique_queue):
        """The uniqueness only covers QUEUED rows: once the job leaves the
        queue, the same key can be enqueued again."""
        deadline_key = "hourly-sync"
        job_id = await create_job(
            db_connection,
            queue=unique_queue,
            deadline_key=deadline_key,
            run_after=past(),
        )
        claimed = await claim(db_connection, unique_queue)
        await db_connection.fetchrow(
            STMTS["finished"], job_id, {}, claimed["run_epoch"]
        )

        second = await create_job(
            db_connection, queue=unique_queue, deadline_key=deadline_key
        )
        assert second != job_id


@pytest.mark.integration
class TestCompleteJobFlows:
    """Integration tests for complete job flows."""

    async def test_successful_job_flow(self, db_connection, unique_queue):
        """Test a complete successful job from start to finish."""
        # Create job
        job_id = await create_job(
            db_connection,
            job_class="test.SuccessfulJob",
            kwargs={"input": "data"},
            queue=unique_queue,
            prio=50,
            run_after=past(),
        )

        # Worker claims job
        claimed = await claim(db_connection, unique_queue, host="worker-prod-1")
        assert claimed["id"] == job_id
        epoch = claimed["run_epoch"]

        # Worker starts execution
        await db_connection.execute(STMTS["run"], job_id, epoch, None)

        # Job completes successfully
        await db_connection.fetchrow(
            STMTS["finished"],
            job_id,
            {"status": "completed", "output": "processed data"},
            epoch,
        )

        # Verify final state
        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["error_count"] == 0
        assert job["run_count"] == 1
        assert job["result"]["status"] == "completed"

    async def test_failed_job_with_retries(self, db_connection, unique_queue):
        """Test job that fails, retries on the SAME row, then succeeds."""
        # Create job
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        # First attempt - claim, run, fail into retry
        first = await claim(db_connection, unique_queue)
        await db_connection.execute(STMTS["run"], job_id, first["run_epoch"], None)
        await db_connection.execute(
            STMTS["retry"],
            job_id,
            timedelta(seconds=-1),
            "Temporary error",
            "Traceback...",
            first["run_epoch"],
        )

        # Second attempt - claim the SAME row, run, succeed
        second = await claim(db_connection, unique_queue, pid=12346, host="worker-2")
        assert second["id"] == job_id
        assert second["run_epoch"] > first["run_epoch"]
        await db_connection.execute(STMTS["run"], job_id, second["run_epoch"], None)
        await db_connection.fetchrow(
            STMTS["finished"], job_id, {"status": "success"}, second["run_epoch"]
        )

        # One row, terminal success, with the failure history preserved
        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["error_count"] == 1
        assert job["run_count"] == 2

        events = [
            r["event"]
            for r in await db_connection.fetch(
                "SELECT event FROM jorb_history WHERE job_id = $1 ORDER BY id", job_id
            )
        ]
        assert events == [
            "enqueued",
            "claimed",
            "running",  # attempt 1
            "queued",  # retry requeue
            "claimed",
            "running",
            "finished",  # attempt 2
        ]

    async def test_dependency_workflow(self, db_connection, unique_queue):
        """Test workflow with dependent jobs."""
        # Create parent job
        parent_id = await create_job(
            db_connection,
            job_class="test.DataFetch",
            queue=unique_queue,
            state="queued",
            run_after=past(),
        )

        # Create child job waiting for parent
        child_id = await create_job(
            db_connection,
            job_class="test.DataProcess",
            queue=unique_queue,
            waitfor_job=parent_id,
            state="waiting",
            run_after=past(),
        )

        # Execute parent job
        parent_claim = await claim(db_connection, unique_queue)
        assert parent_claim["id"] == parent_id
        await db_connection.execute(
            STMTS["run"], parent_id, parent_claim["run_epoch"], None
        )
        await db_connection.fetchrow(
            STMTS["finished"],
            parent_id,
            {"data": "fetched"},
            parent_claim["run_epoch"],
        )

        # Trigger dependency resolution
        await db_connection.fetch(STMTS["enqueue-next-self-finished"], parent_id)

        # Child should now be queued
        child = await get_job(db_connection, child_id)
        assert child["state"] == "queued"

        # Execute child job
        child_claim = await claim(db_connection, unique_queue, pid=12346)
        assert child_claim["id"] == child_id
        await db_connection.execute(
            STMTS["run"], child_id, child_claim["run_epoch"], None
        )
        await db_connection.fetchrow(
            STMTS["finished"],
            child_id,
            {"result": "processed"},
            child_claim["run_epoch"],
        )

        # Both jobs should be finished
        parent = await get_job(db_connection, parent_id)
        child = await get_job(db_connection, child_id)

        assert parent["state"] == "finished"
        assert child["state"] == "finished"
