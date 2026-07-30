"""
Comprehensive SQL statement tests (schema v1).

Tests the SQL operations defined in pyjobby.pj.STMTS for correctness,
atomicity, and edge cases: claiming (with the jorb_queue control plane),
epoch fencing, same-row retries, the terminal 'crashed' DLQ, and
dependency wakeups.
"""

from datetime import UTC, datetime, timedelta

import pytest

from pyjobby.pj import STMTS
from tests.utils.factories import create_job, get_job

pytestmark = pytest.mark.asyncio


def past() -> datetime:
    """A run_after safely before the test transaction's now()."""
    return datetime.now(UTC) - timedelta(seconds=5)


async def claim(
    conn,
    queue,
    *,
    pid=12345,
    host="test-host",
    caps=("test",),
    prio=1000,
    worker_id=None,
    app_version: str | None = None,
):
    """Claim the next job on `queue` (schema v1 seven-argument claim)."""
    return await conn.fetchrow(
        STMTS["claim"], pid, host, queue, list(caps), prio, worker_id, app_version
    )


class TestClaimStatement:
    """Test the 'claim' SQL statement for atomic job claiming."""

    async def test_claim_basic(self, db_connection, unique_queue):
        """Claiming stamps worker identity and bumps run_count/run_epoch."""
        job_id = await create_job(
            db_connection,
            job_class="test.BasicJob",
            kwargs={"value": 1},
            queue=unique_queue,
            state="queued",
            run_after=past(),
        )

        result = await claim(db_connection, unique_queue, worker_id=None)

        assert result is not None
        assert result["id"] == job_id
        assert result["state"] == "claimed"
        assert result["worker_pid"] == 12345
        assert result["worker_host"] == "test-host"
        assert result["run_count"] == 1
        assert result["run_epoch"] == 1  # fencing token bumps on claim

    async def test_claim_records_worker_registry_id(self, db_connection, unique_queue):
        """claimed_by references the claiming worker's jorb_worker row."""
        worker_id = await db_connection.fetchval(
            """INSERT INTO jorb_worker (host, pid, queue, capabilities)
               VALUES ($1, $2, $3, $4) RETURNING id""",
            "test-host",
            12345,
            unique_queue,
            ["test"],
        )
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        result = await claim(db_connection, unique_queue, worker_id=worker_id)
        assert result["claimed_by"] == worker_id

    async def test_claim_respects_queue(self, db_connection, unique_queue):
        """Test that claiming respects queue filtering."""
        queue_a = f"{unique_queue}_a"
        queue_b = f"{unique_queue}_b"
        job1_id = await create_job(
            db_connection, queue=queue_a, state="queued", run_after=past()
        )
        await create_job(db_connection, queue=queue_b, state="queued", run_after=past())

        result = await claim(db_connection, queue_a)

        assert result["id"] == job1_id
        assert result["queue"] == queue_a

    async def test_claim_respects_capability(self, db_connection, unique_queue):
        """Test that claiming respects capability requirements."""
        job_id = await create_job(
            db_connection,
            queue=unique_queue,
            capability="special",
            state="queued",
            run_after=past(),
        )

        # Try to claim without capability - should fail
        result = await claim(db_connection, unique_queue, caps=("basic",))
        assert result is None

        # Claim with correct capability - should succeed
        result = await claim(db_connection, unique_queue, caps=("special",))
        assert result is not None
        assert result["id"] == job_id

    async def test_claim_respects_priority(self, db_connection, unique_queue):
        """Test that claiming returns the most urgent (lowest prio) job."""
        job_high = await create_job(
            db_connection,
            prio=10,
            queue=unique_queue,
            state="queued",
            run_after=past(),
        )
        await create_job(
            db_connection,
            prio=100,
            queue=unique_queue,
            state="queued",
            run_after=past(),
        )

        result = await claim(db_connection, unique_queue)
        assert result["id"] == job_high

    async def test_claim_respects_priority_ceiling(self, db_connection, unique_queue):
        """Workers only claim jobs with prio <= their ceiling."""
        await create_job(
            db_connection,
            prio=500,
            queue=unique_queue,
            state="queued",
            run_after=past(),
        )

        assert await claim(db_connection, unique_queue, prio=100) is None
        result = await claim(db_connection, unique_queue, prio=500)
        assert result is not None

    async def test_claim_respects_run_after(self, db_connection, unique_queue):
        """Test that claiming respects run_after timestamp."""
        future_time = datetime.now(UTC) + timedelta(hours=1)
        await create_job(
            db_connection, queue=unique_queue, run_after=future_time, state="queued"
        )

        # Should not claim job scheduled for future
        result = await claim(db_connection, unique_queue)
        assert result is None

    async def test_claim_skip_locked(self, db_connection, unique_queue):
        """Test that a claimed job cannot be claimed twice."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        result1 = await claim(db_connection, unique_queue)
        assert result1 is not None

        # Second claim attempt should return None (already claimed)
        result2 = await claim(db_connection, unique_queue, pid=12346, host="host2")
        assert result2 is None

    async def test_claim_honors_paused_queue(self, db_connection, unique_queue):
        """A paused jorb_queue row blocks claims until unpaused."""
        await db_connection.execute(
            "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)", unique_queue
        )
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        assert await claim(db_connection, unique_queue) is None

        await db_connection.execute(
            "UPDATE jorb_queue SET paused = FALSE WHERE name = $1", unique_queue
        )
        assert await claim(db_connection, unique_queue) is not None

    async def test_claim_honors_max_concurrency(self, db_connection, unique_queue):
        """max_concurrency caps claimed+running rows for the queue."""
        await db_connection.execute(
            "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, 1)",
            unique_queue,
        )
        for _ in range(2):
            await create_job(
                db_connection, queue=unique_queue, state="queued", run_after=past()
            )

        assert await claim(db_connection, unique_queue) is not None
        # one in flight already: the cap blocks a second claim
        assert await claim(db_connection, unique_queue) is None

    async def test_epoch_advances_whenever_the_job_leaves_or_enters_an_attempt(
        self, db_connection, unique_queue
    ):
        """run_epoch is a fencing token, not an attempt counter.

        It advances on claim AND on the retry that abandons an attempt, so an
        execution the platform has given up on is fenced out the moment it is
        abandoned rather than when the next worker happens to claim. The
        attempt number is run_count.
        """
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        first = await claim(db_connection, unique_queue)
        assert first["run_epoch"] == 1
        assert first["run_count"] == 1

        # same-row retry back into the queue: the abandoned attempt is fenced
        # out immediately, before anything re-claims the row
        requeued = await db_connection.fetchrow(
            STMTS["retry"], job_id, timedelta(seconds=-1), "boom", "trace", 1
        )
        assert requeued["id"] == job_id
        requeued_epoch = await db_connection.fetchval(
            "SELECT run_epoch FROM jorb WHERE id = $1", job_id
        )
        assert requeued_epoch > first["run_epoch"]

        second = await claim(db_connection, unique_queue)
        assert second["id"] == job_id
        assert second["run_epoch"] > requeued_epoch
        assert second["run_count"] == 2


class TestRunStatement:
    """Test the 'run' SQL statement for marking jobs as running."""

    async def test_mark_running(self, db_connection, unique_queue):
        """Test transitioning job to running state (records `started`)."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        job_id, epoch = claimed["id"], claimed["run_epoch"]

        await db_connection.execute(STMTS["run"], job_id, epoch, None)

        job = await get_job(db_connection, job_id)
        assert job["state"] == "running"
        assert job["started"] is not None

    async def test_run_is_epoch_fenced(self, db_connection, unique_queue):
        """A stale epoch cannot move a job to running."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)

        stale = await db_connection.fetch(
            STMTS["run"], claimed["id"], claimed["run_epoch"] - 1, None
        )
        assert stale == []
        job = await get_job(db_connection, claimed["id"])
        assert job["state"] == "claimed"


class TestFinishedStatement:
    """Test the 'finished' SQL statement for marking jobs complete."""

    async def test_mark_finished_basic(self, db_connection, unique_queue):
        """Test basic job completion."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        job_id, epoch = claimed["id"], claimed["run_epoch"]

        result = await db_connection.fetchrow(
            STMTS["finished"], job_id, {"status": "success", "output": "done"}, epoch
        )

        assert result["id"] == job_id
        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"
        assert job["result"]["status"] == "success"
        assert job["finished"] is not None

    async def test_finished_updates_timestamp(self, db_connection, unique_queue):
        """Test that finished updates the updated timestamp."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)

        # created/claimed at the frozen transaction now(); clock_timestamp()
        # is unavailable through now()-based statements inside one
        # transaction, so assert monotonic non-decrease instead
        result = await db_connection.fetchrow(
            STMTS["finished"], claimed["id"], {}, claimed["run_epoch"]
        )
        assert result["id"] == claimed["id"]
        updated = await db_connection.fetchval(
            "SELECT updated FROM jorb WHERE id = $1", claimed["id"]
        )
        assert updated >= claimed["updated"]

    async def test_finished_is_epoch_fenced(self, db_connection, unique_queue):
        """A superseded execution's completion is a no-op."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)

        stale = await db_connection.fetch(
            STMTS["finished"], claimed["id"], {"stale": True}, claimed["run_epoch"] - 1
        )
        assert stale == []
        job = await get_job(db_connection, claimed["id"])
        assert job["state"] == "claimed"
        assert job["result"] is None


class TestCrashedStatement:
    """Test the 'crashed' SQL statement (terminal failure — the DLQ)."""

    async def test_mark_crashed(self, db_connection, unique_queue):
        """Test marking a job as crashed with error details."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        job_id, epoch = claimed["id"], claimed["run_epoch"]
        await db_connection.execute(STMTS["run"], job_id, epoch, None)

        await db_connection.execute(
            STMTS["crashed"],
            job_id,
            "ValueError: invalid input",
            "Traceback:\n  File test.py, line 1\n    raise ValueError()",
            epoch,
        )

        job = await get_job(db_connection, job_id)
        assert job["state"] == "crashed"
        assert job["error_message"] == "ValueError: invalid input"
        assert "Traceback" in job["error_backtrace"]
        assert job["error_count"] == 1
        assert job["finished"] is not None

    async def test_crashed_is_terminal(self, db_connection, unique_queue):
        """'crashed' rows are the DLQ: not claimable again."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        await db_connection.execute(
            STMTS["crashed"], claimed["id"], "boom", "trace", claimed["run_epoch"]
        )

        assert await claim(db_connection, unique_queue) is None

    async def test_crashed_is_epoch_fenced(self, db_connection, unique_queue):
        """A stale execution cannot dead-letter the row."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)

        stale = await db_connection.fetch(
            STMTS["crashed"],
            claimed["id"],
            "boom",
            "trace",
            claimed["run_epoch"] - 1,
        )
        assert stale == []
        job = await get_job(db_connection, claimed["id"])
        assert job["state"] == "claimed"


class TestRetryStatement:
    """Test the 'retry' SQL statement: same-row requeue with backoff."""

    async def test_retry_requeues_same_row(self, db_connection, unique_queue):
        """A retry keeps the job id and puts the SAME row back in queue."""
        job_id = await create_job(
            db_connection,
            job_class="test.RetryableJob",
            kwargs={"attempt": 1},
            queue=unique_queue,
            state="queued",
            run_after=past(),
        )
        claimed = await claim(db_connection, unique_queue)
        assert claimed["id"] == job_id

        result = await db_connection.fetchrow(
            STMTS["retry"],
            job_id,
            timedelta(minutes=5),
            "transient failure",
            "Traceback...",
            claimed["run_epoch"],
        )

        # SAME row, no retry-copy rows anywhere
        assert result["id"] == job_id
        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"
        assert job["error_count"] == 1
        assert job["error_message"] == "transient failure"
        assert job["run_after"] > datetime.now(UTC) - timedelta(seconds=1)
        total_rows = await db_connection.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
        )
        assert total_rows == 1

    async def test_retry_preserves_job_data(self, db_connection, unique_queue):
        """The requeued row keeps its class/kwargs/prio/capability."""
        job_id = await create_job(
            db_connection,
            job_class="test.SpecialJob",
            kwargs={"config": "value"},
            queue=unique_queue,
            prio=50,
            capability="special",
            state="queued",
            run_after=past(),
        )
        claimed = await claim(db_connection, unique_queue, caps=("special",))

        await db_connection.execute(
            STMTS["retry"],
            job_id,
            timedelta(minutes=1),
            "err",
            "trace",
            claimed["run_epoch"],
        )

        job = await get_job(db_connection, job_id)
        assert job["job_class"] == "test.SpecialJob"
        assert job["kwargs"]["config"] == "value"
        assert job["queue"] == unique_queue
        assert job["prio"] == 50
        assert job["capability"] == "special"

    async def test_retry_is_epoch_fenced(self, db_connection, unique_queue):
        """A stale execution cannot requeue the row."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)

        stale = await db_connection.fetch(
            STMTS["retry"],
            claimed["id"],
            timedelta(minutes=1),
            "err",
            "trace",
            claimed["run_epoch"] - 1,
        )
        assert stale == []


class TestRescheduleStatement:
    """Test the 'reschedule' SQL statement (self-reschedule)."""

    async def test_reschedule_job(self, db_connection, unique_queue):
        """Test rescheduling a job to run later."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        job_id = claimed["id"]
        await db_connection.execute(STMTS["run"], job_id, claimed["run_epoch"], None)

        # Reschedule to run in 1 hour (fenced to this attempt, like every
        # other state-changing statement)
        applied = await db_connection.fetch(
            STMTS["reschedule"], job_id, timedelta(hours=1), claimed["run_epoch"]
        )
        assert [r["id"] for r in applied] == [job_id]

        job = await get_job(db_connection, job_id)
        assert job["state"] == "queued"
        assert job["run_after"] > datetime.now(UTC)

    async def test_reschedule_from_a_superseded_attempt_is_a_noop(
        self, db_connection, unique_queue
    ):
        """A stale attempt may not requeue a job the live attempt is running."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        job_id = claimed["id"]
        await db_connection.execute(STMTS["run"], job_id, claimed["run_epoch"], None)

        applied = await db_connection.fetch(
            STMTS["reschedule"], job_id, timedelta(hours=1), claimed["run_epoch"] - 1
        )
        assert applied == []
        assert (await get_job(db_connection, job_id))["state"] == "running"


class TestRunStampsTheDeadline:
    """The deadline is written by 'run' itself, in the same statement as
    the claimed -> running transition — there is no separate set-timeout
    write to forget, misorder, or pay a second row version for."""

    async def test_run_with_a_timeout_stamps_timeout_at(
        self, db_connection, unique_queue
    ):
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)

        await db_connection.execute(
            STMTS["run"], claimed["id"], claimed["run_epoch"], timedelta(seconds=30)
        )
        job = await get_job(db_connection, claimed["id"])
        assert job["state"] == "running"
        assert job["timeout_at"] is not None
        assert job["timeout_at"] > job["started"]

    async def test_run_without_a_timeout_leaves_no_deadline(
        self, db_connection, unique_queue
    ):
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)

        await db_connection.execute(
            STMTS["run"], claimed["id"], claimed["run_epoch"], None
        )
        job = await get_job(db_connection, claimed["id"])
        assert job["state"] == "running"
        assert job["timeout_at"] is None

    async def test_run_is_idempotent_at_its_own_epoch(
        self, db_connection, unique_queue
    ):
        """A run whose COMMIT ack was lost is replayed by ex()'s reconnect.
        The replay must find the already-running row at the same epoch and
        return its id (a no-op self-transition), not zero rows — otherwise
        the worker reads it as 'superseded' and abandons a timeout=0 job
        that no sweep can then recover."""
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)

        first = await db_connection.fetch(
            STMTS["run"], claimed["id"], claimed["run_epoch"], None
        )
        assert [r["id"] for r in first] == [claimed["id"]]

        replay = await db_connection.fetch(
            STMTS["run"], claimed["id"], claimed["run_epoch"], None
        )
        assert [r["id"] for r in replay] == [claimed["id"]], (
            "the lost-ack replay must be a no-op that still returns the id"
        )

        # a DIFFERENT (superseding) attempt is still fenced out by its epoch
        stale = await db_connection.fetch(
            STMTS["run"], claimed["id"], claimed["run_epoch"] - 1, None
        )
        assert stale == []


class TestEnqueueStatements:
    """Test the enqueue-next statements for job dependencies."""

    async def test_enqueue_after_group_finished(self, db_connection, unique_queue):
        """Test enqueuing jobs that wait for a group to finish."""
        group_id = 12345
        await create_job(
            db_connection, queue=unique_queue, run_group=group_id, state="finished"
        )
        await create_job(
            db_connection, queue=unique_queue, run_group=group_id, state="finished"
        )

        waiter = await create_job(
            db_connection, queue=unique_queue, waitfor_group=group_id, state="waiting"
        )

        results = await db_connection.fetch(
            STMTS["enqueue-next-if-peer-group-is-finished"], group_id
        )
        assert len(results) > 0

        waiter_job = await get_job(db_connection, waiter)
        assert waiter_job["state"] == "queued"

    async def test_group_wakeup_requires_all_finished(
        self, db_connection, unique_queue
    ):
        """No wakeup while ANY job in the group is unfinished."""
        group_id = 54321
        await create_job(
            db_connection, queue=unique_queue, run_group=group_id, state="finished"
        )
        await create_job(
            db_connection, queue=unique_queue, run_group=group_id, state="queued"
        )
        waiter = await create_job(
            db_connection, queue=unique_queue, waitfor_group=group_id, state="waiting"
        )

        results = await db_connection.fetch(
            STMTS["enqueue-next-if-peer-group-is-finished"], group_id
        )
        assert results == []
        assert (await get_job(db_connection, waiter))["state"] == "waiting"

    async def test_enqueue_after_self_finished(self, db_connection, unique_queue):
        """Test enqueuing dependent jobs after their upstream finishes."""
        finished_id = await create_job(
            db_connection, queue=unique_queue, state="finished"
        )

        waiter = await create_job(
            db_connection, queue=unique_queue, waitfor_job=finished_id, state="waiting"
        )

        results = await db_connection.fetch(
            STMTS["enqueue-next-self-finished"], finished_id
        )
        assert len(results) > 0

        waiter_job = await get_job(db_connection, waiter)
        assert waiter_job["state"] == "queued"


class TestDeadlineKey:
    """Idempotent enqueue: one queued row per (deadline_key, queue)."""

    async def test_deadline_key_prevents_duplicates(self, db_connection, unique_queue):
        """A second queued INSERT with the same deadline key must fail."""
        deadline_key = "daily-report-2024-01-01"

        await create_job(
            db_connection,
            job_class="test.DailyReport",
            kwargs={"date": "2024-01-01"},
            queue=unique_queue,
            deadline_key=deadline_key,
        )

        import asyncpg

        with pytest.raises(asyncpg.UniqueViolationError):
            await create_job(
                db_connection,
                job_class="test.DailyReport",
                kwargs={"date": "2024-01-01"},
                queue=unique_queue,
                deadline_key=deadline_key,
            )


class TestHistoryTrigger:
    """Every INSERT and state change writes a jorb_history row."""

    async def test_lifecycle_recorded_in_history(self, db_connection, unique_queue):
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        job_id, epoch = claimed["id"], claimed["run_epoch"]
        await db_connection.execute(STMTS["run"], job_id, epoch, None)
        await db_connection.execute(STMTS["finished"], job_id, {"ok": True}, epoch)

        events = [
            r["event"]
            for r in await db_connection.fetch(
                "SELECT event FROM jorb_history WHERE job_id = $1 ORDER BY id", job_id
            )
        ]
        assert events == ["enqueued", "claimed", "running", "finished"]

    async def test_retry_history_carries_error(self, db_connection, unique_queue):
        await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )
        claimed = await claim(db_connection, unique_queue)
        await db_connection.execute(
            STMTS["retry"],
            claimed["id"],
            timedelta(minutes=1),
            "kapow",
            "trace",
            claimed["run_epoch"],
        )

        detail = await db_connection.fetchval(
            """SELECT detail FROM jorb_history
               WHERE job_id = $1 AND event = 'queued'
               ORDER BY id DESC LIMIT 1""",
            claimed["id"],
        )
        assert detail["error"] == "kapow"
        assert detail["error_count"] == 1
        # the history row records the epoch AFTER the retry advanced the
        # fence -- the attempt that failed has already been superseded
        assert detail["run_epoch"] > 1


@pytest.mark.slow
class TestConcurrentClaiming:
    """Test concurrent job claiming scenarios."""

    async def test_multiple_workers_claim_different_jobs(
        self, db_connection, unique_queue
    ):
        """Test that multiple workers can claim different jobs."""
        for i in range(10):
            await create_job(
                db_connection,
                kwargs={"batch_index": i},
                queue=unique_queue,
                state="queued",
                run_after=past(),
            )

        claimed = []
        for worker_id in range(5):
            result = await claim(
                db_connection,
                unique_queue,
                pid=worker_id,
                host=f"worker-{worker_id}",
            )
            if result:
                claimed.append(result["id"])

        # Should have claimed 5 different jobs
        assert len(claimed) == 5
        assert len(set(claimed)) == 5  # All unique


@pytest.mark.integration
class TestSQLStatementIntegration:
    """Integration tests for SQL statements working together."""

    async def test_full_job_lifecycle(self, db_connection, unique_queue):
        """Test complete job lifecycle through SQL statements."""
        # 1. Create job
        job_id = await create_job(
            db_connection,
            job_class="test.FullLifecycle",
            kwargs={"step": 1},
            queue=unique_queue,
            state="queued",
            run_after=past(),
        )

        # 2. Claim job
        claimed = await claim(db_connection, unique_queue)
        assert claimed["id"] == job_id
        assert claimed["state"] == "claimed"
        epoch = claimed["run_epoch"]

        # 3. Mark as running
        await db_connection.execute(STMTS["run"], job_id, epoch, None)
        job = await get_job(db_connection, job_id)
        assert job["state"] == "running"

        # 4. Complete successfully
        finished = await db_connection.fetchrow(
            STMTS["finished"], job_id, {"result": "success"}, epoch
        )
        assert finished["id"] == job_id
        job = await get_job(db_connection, job_id)
        assert job["state"] == "finished"

    async def test_repeated_attempts_share_one_row(self, db_connection, unique_queue):
        """Three failing attempts stay on ONE row; history holds the trail."""
        job_id = await create_job(
            db_connection, queue=unique_queue, state="queued", run_after=past()
        )

        for attempt in range(1, 4):
            claimed = await claim(db_connection, unique_queue)
            assert claimed["id"] == job_id
            # run_count is the attempt number; run_epoch only ever increases
            assert claimed["run_count"] == attempt
            assert claimed["run_epoch"] >= attempt
            requeued = await db_connection.fetchrow(
                STMTS["retry"],
                job_id,
                timedelta(seconds=-1),  # immediately claimable again
                f"Error {attempt}",
                "Traceback",
                claimed["run_epoch"],  # the fence: this attempt's own epoch
            )
            assert requeued is not None, "the retry must apply at the live epoch"

        job = await get_job(db_connection, job_id)
        assert job["error_count"] == 3
        assert job["error_message"] == "Error 3"
        assert job["state"] == "queued"
        total_rows = await db_connection.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
        )
        assert total_rows == 1

        requeues = await db_connection.fetchval(
            "SELECT count(*) FROM jorb_history WHERE job_id=$1 AND event='queued'",
            job_id,
        )
        assert requeues == 3
