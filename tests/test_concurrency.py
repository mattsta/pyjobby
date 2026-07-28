"""
Concurrency and race condition tests (schema v1).

Tests concurrent access patterns, race conditions, and database
locking behavior under high contention scenarios. Uses real
(non-transactional) connections so SKIP LOCKED and epoch fencing are
exercised exactly as production sees them.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from pyjobby import db
from pyjobby.pj import STMTS
from tests.utils.factories import create_job, create_job_batch, get_job

pytestmark = pytest.mark.asyncio


async def connect_with_codec(db_params):
    """Create a connection with pyjobby's JSON codecs configured."""
    return await db.connect(**db_params)


async def claim(conn, queue, *, pid=1, host="worker", caps=("test",), prio=1000):
    """Claim the next job on `queue` (schema v1 six-argument claim)."""
    return await conn.fetchrow(STMTS["claim"], pid, host, queue, list(caps), prio, None)


async def count_state(conn, queue: str, state: str) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM jorb WHERE queue = $1 AND state = $2", queue, state
    )


class TestConcurrentJobClaiming:
    """Test concurrent job claiming scenarios."""

    async def test_multiple_workers_claim_different_jobs(self, db_params, unique_queue):
        """Test that multiple workers can claim different jobs without conflicts."""
        conn = await connect_with_codec(db_params)
        try:
            job_ids = await create_job_batch(
                conn, count=20, queue=unique_queue, state="queued"
            )
        finally:
            await conn.close()

        # Simulate 5 workers claiming jobs concurrently
        async def worker_claim(worker_id: int) -> list[int]:
            """Worker claims jobs until none are available."""
            conn = await connect_with_codec(db_params)
            claimed = []

            try:
                for _attempt in range(10):  # Try to claim up to 10 jobs
                    result = await claim(
                        conn,
                        unique_queue,
                        pid=worker_id,
                        host=f"worker-{worker_id}",
                    )
                    if result:
                        claimed.append(result["id"])
                    else:
                        break  # No more jobs available

                    # Small delay to simulate processing
                    await asyncio.sleep(0.001)
            finally:
                await conn.close()

            return claimed

        # Run 5 workers concurrently
        results = await asyncio.gather(*[worker_claim(i) for i in range(5)])

        # Verify results
        all_claimed = []
        for worker_claims in results:
            all_claimed.extend(worker_claims)

        # Should have claimed all 20 jobs
        assert len(all_claimed) == 20

        # Each job should only be claimed once (no duplicates)
        assert len(set(all_claimed)) == 20

        # All claimed IDs should be from our original batch
        assert set(all_claimed) == set(job_ids)

    async def test_skip_locked_prevents_double_claims(self, db_params, unique_queue):
        """Test that FOR UPDATE SKIP LOCKED prevents double claims."""
        conn = await connect_with_codec(db_params)
        await create_job(conn, queue=unique_queue, state="queued")
        await conn.close()

        async def attempt_claim(worker_id: int):
            """Try to claim the same job."""
            conn = await connect_with_codec(db_params)
            try:
                return await claim(
                    conn, unique_queue, pid=worker_id, host=f"worker-{worker_id}"
                )
            finally:
                await conn.close()

        # 10 workers try to claim the same job simultaneously
        results = await asyncio.gather(*[attempt_claim(i) for i in range(10)])

        # Only one should succeed
        successful_claims = [r for r in results if r is not None]
        assert len(successful_claims) == 1
        assert successful_claims[0]["run_epoch"] == 1

        # The rest should be None
        failed_claims = [r for r in results if r is None]
        assert len(failed_claims) == 9

    async def test_concurrent_claims_different_queues(self, db_params, unique_queue):
        """Test concurrent claims from different queues don't interfere."""
        queue_a = f"{unique_queue}_a"
        queue_b = f"{unique_queue}_b"
        conn = await connect_with_codec(db_params)

        # Create jobs in different queues
        queue_a_jobs = []
        queue_b_jobs = []

        for _ in range(10):
            queue_a_jobs.append(await create_job(conn, queue=queue_a, state="queued"))
            queue_b_jobs.append(await create_job(conn, queue=queue_b, state="queued"))

        await conn.close()

        async def claim_from_queue(queue_name: str, worker_id: int):
            """Claim all jobs from a specific queue."""
            conn = await connect_with_codec(db_params)
            claimed = []

            try:
                while True:
                    result = await claim(
                        conn,
                        queue_name,
                        pid=worker_id,
                        host=f"worker-{queue_name}-{worker_id}",
                    )
                    if result:
                        claimed.append(result["id"])
                    else:
                        break
                    await asyncio.sleep(0.001)
            finally:
                await conn.close()

            return claimed

        # Workers claim from both queues concurrently
        queue_a_results, queue_b_results = await asyncio.gather(
            claim_from_queue(queue_a, 1), claim_from_queue(queue_b, 2)
        )

        # Each queue should have all its jobs claimed
        assert set(queue_a_results) == set(queue_a_jobs)
        assert set(queue_b_results) == set(queue_b_jobs)

        # No overlap
        assert not set(queue_a_results).intersection(set(queue_b_results))


class TestConcurrentStateTransitions:
    """Test concurrent state transitions and updates."""

    async def test_concurrent_finish_operations(self, db_params, unique_queue):
        """Test multiple jobs finishing concurrently (epoch-fenced)."""
        conn = await connect_with_codec(db_params)

        # Create and claim multiple jobs (claiming sets run_epoch = 1)
        job_ids = []
        for _ in range(10):
            await create_job(conn, queue=unique_queue, state="queued")
        for _ in range(10):
            claimed = await claim(conn, unique_queue)
            job_ids.append(claimed["id"])

        await conn.close()

        async def finish_job(job_id: int):
            """Mark a job as finished."""
            conn = await connect_with_codec(db_params)
            try:
                result = await conn.fetchrow(
                    STMTS["finished"],
                    job_id,
                    {"status": "success", "result": f"job-{job_id}"},
                    1,  # the epoch our claim owns
                )
                return result["id"]
            finally:
                await conn.close()

        # Finish all jobs concurrently
        finished_ids = await asyncio.gather(*[finish_job(jid) for jid in job_ids])

        # All should finish successfully
        assert set(finished_ids) == set(job_ids)

        # Verify all are finished
        conn = await connect_with_codec(db_params)
        finished_count = await count_state(conn, unique_queue, "finished")
        await conn.close()

        assert finished_count == 10

    async def test_concurrent_crashed_operations(self, db_params, unique_queue):
        """Test multiple jobs dead-lettering concurrently."""
        conn = await connect_with_codec(db_params)

        job_ids = []
        for _ in range(10):
            await create_job(conn, queue=unique_queue, state="queued")
        for _ in range(10):
            claimed = await claim(conn, unique_queue)
            job_ids.append(claimed["id"])

        await conn.close()

        async def crash_job(job_id: int, error_num: int):
            """Mark a job as terminally crashed."""
            conn = await connect_with_codec(db_params)
            try:
                await conn.execute(
                    STMTS["crashed"],
                    job_id,
                    f"Error {error_num}",
                    f"Traceback for error {error_num}",
                    1,
                )
                return job_id
            finally:
                await conn.close()

        # Crash all jobs concurrently
        crashed_ids = await asyncio.gather(
            *[crash_job(jid, i) for i, jid in enumerate(job_ids)]
        )

        assert len(crashed_ids) == 10

        # Verify all crashed (the DLQ is WHERE state = 'crashed')
        conn = await connect_with_codec(db_params)
        crashed_count = await count_state(conn, unique_queue, "crashed")
        await conn.close()

        assert crashed_count == 10


class TestConcurrentRetries:
    """Test concurrent same-row retry requeues."""

    async def test_concurrent_retry_requeues(self, db_params, unique_queue):
        """Retrying many claimed jobs concurrently requeues each SAME row."""
        conn = await connect_with_codec(db_params)

        job_ids = []
        for _ in range(10):
            await create_job(conn, queue=unique_queue, state="queued")
        for _ in range(10):
            claimed = await claim(conn, unique_queue)
            job_ids.append(claimed["id"])

        await conn.close()

        async def retry_job(job_id: int, delay_minutes: int):
            """Requeue the same row with backoff."""
            conn = await connect_with_codec(db_params)
            try:
                result = await conn.fetchrow(
                    STMTS["retry"],
                    job_id,
                    timedelta(minutes=delay_minutes),
                    "transient",
                    "trace",
                    1,
                )
                return result["id"]
            finally:
                await conn.close()

        # Retry concurrently
        retried_ids = await asyncio.gather(
            *[retry_job(jid, i + 1) for i, jid in enumerate(job_ids)]
        )

        # Same rows came back — no copies were created
        assert set(retried_ids) == set(job_ids)

        conn = await connect_with_codec(db_params)
        total = await conn.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
        )
        assert total == 10
        for job_id in job_ids:
            job = await get_job(conn, job_id)
            assert job["state"] == "queued"
            assert job["error_count"] == 1
        await conn.close()


class TestConcurrentDependencyResolution:
    """Test concurrent dependency resolution."""

    async def test_concurrent_waitfor_job_resolution(self, db_params, unique_queue):
        """Test resolving multiple waitfor_job dependencies concurrently."""
        conn = await connect_with_codec(db_params)

        # Create parent jobs
        parent_ids = []
        for _ in range(5):
            parent_id = await create_job(conn, queue=unique_queue, state="finished")
            parent_ids.append(parent_id)

        # Create child jobs waiting for parents
        child_ids = []
        for parent_id in parent_ids:
            child_id = await create_job(
                conn, queue=unique_queue, waitfor_job=parent_id, state="waiting"
            )
            child_ids.append(child_id)

        await conn.close()

        async def resolve_dependency(parent_id: int):
            """Trigger dependency resolution for a parent job."""
            conn = await connect_with_codec(db_params)
            try:
                results = await conn.fetch(
                    STMTS["enqueue-next-self-finished"], parent_id
                )
                return len(results)
            finally:
                await conn.close()

        # Resolve all dependencies concurrently
        results = await asyncio.gather(*[resolve_dependency(pid) for pid in parent_ids])

        # Each should have resolved exactly 1 dependency
        assert results == [1] * 5

        # Verify all children are queued
        conn = await connect_with_codec(db_params)
        for child_id in child_ids:
            child = await get_job(conn, child_id)
            assert child["state"] == "queued"
        await conn.close()

    async def test_concurrent_waitfor_group_resolution(self, db_params, unique_queue):
        """Test resolving multiple waitfor_group dependencies concurrently."""
        conn = await connect_with_codec(db_params)

        # Create multiple groups
        groups = []
        for group_num in range(3):
            group_id = 10000 + group_num

            # Create jobs in group
            for _ in range(3):
                await create_job(
                    conn, queue=unique_queue, run_group=group_id, state="finished"
                )

            # Create waiter for group
            waiter_id = await create_job(
                conn, queue=unique_queue, waitfor_group=group_id, state="waiting"
            )

            groups.append((group_id, waiter_id))

        await conn.close()

        async def resolve_group(group_id: int):
            """Resolve group dependency."""
            conn = await connect_with_codec(db_params)
            try:
                results = await conn.fetch(
                    STMTS["enqueue-next-if-peer-group-is-finished"], group_id
                )
                return len(results)
            finally:
                await conn.close()

        # Resolve all groups concurrently
        await asyncio.gather(*[resolve_group(gid) for gid, _ in groups])

        # Verify all waiters are queued
        conn = await connect_with_codec(db_params)
        for _group_id, waiter_id in groups:
            waiter = await get_job(conn, waiter_id)
            assert waiter["state"] == "queued"
        await conn.close()


class TestConcurrentEpochFencing:
    """Epoch fencing under concurrency: exactly one attempt owns the row."""

    async def test_stale_epoch_writers_lose(self, db_params, unique_queue):
        """After a requeue+reclaim, writers holding the old epoch are no-ops.

        The requeue advances the epoch by itself, so the first attempt is
        already fenced before the second claim even happens.
        """
        conn = await connect_with_codec(db_params)
        job_id = await create_job(conn, queue=unique_queue, state="queued")

        first = await claim(conn, unique_queue)
        assert first["id"] == job_id and first["run_epoch"] == 1

        # the row is requeued (e.g. by the monitor) and claimed again
        await db.requeue_job(
            conn, job_id, allowed_states=("claimed", "running"), reset_errors=False
        )
        second = await claim(conn, unique_queue, pid=2, host="worker-2")
        assert second["run_epoch"] > first["run_epoch"]
        await conn.close()

        async def finish_with_epoch(epoch: int):
            conn = await connect_with_codec(db_params)
            try:
                rows = await conn.fetch(
                    STMTS["finished"], job_id, {"epoch": epoch}, epoch
                )
                return len(rows)
            finally:
                await conn.close()

        stale, current = await asyncio.gather(
            finish_with_epoch(first["run_epoch"]),
            finish_with_epoch(second["run_epoch"]),
        )
        assert stale == 0  # fenced out
        assert current == 1

        conn = await connect_with_codec(db_params)
        job = await get_job(conn, job_id)
        assert job["state"] == "finished"
        assert job["result"] == {"epoch": second["run_epoch"]}
        await conn.close()


class TestHighContentionScenarios:
    """Test behavior under high contention."""

    async def test_high_contention_claiming(self, db_params, unique_queue):
        """Test claiming with many workers competing for few jobs."""
        conn = await connect_with_codec(db_params)

        # Create only 5 jobs
        job_ids = await create_job_batch(
            conn, count=5, queue=unique_queue, state="queued"
        )
        await conn.close()

        async def aggressive_claimer(worker_id: int):
            """Worker aggressively tries to claim jobs."""
            conn = await connect_with_codec(db_params)
            claimed = []

            try:
                # Try 20 times even though only 5 jobs exist
                for _ in range(20):
                    result = await claim(
                        conn,
                        unique_queue,
                        pid=worker_id,
                        host=f"worker-{worker_id}",
                    )
                    if result:
                        claimed.append(result["id"])
                    await asyncio.sleep(0.0001)
            finally:
                await conn.close()

            return claimed

        # 20 workers compete for 5 jobs
        results = await asyncio.gather(*[aggressive_claimer(i) for i in range(20)])

        # Collect all claims
        all_claimed = []
        for worker_claims in results:
            all_claimed.extend(worker_claims)

        # No duplicates - each job claimed exactly once
        assert len(all_claimed) == len(set(all_claimed))

        # Our 5 jobs should all be claimed
        assert set(all_claimed) == set(job_ids)

    async def test_rapid_state_transitions(self, db_params, unique_queue):
        """Test rapid state transitions don't cause corruption."""
        conn = await connect_with_codec(db_params)
        job_id = await create_job(conn, queue=unique_queue, state="queued")
        await conn.close()

        # Multiple operations on same job
        async def claim_and_finish(worker_id: int):
            """Try to claim and finish the job."""
            conn = await connect_with_codec(db_params)
            try:
                # Try to claim
                claimed = await claim(
                    conn, unique_queue, pid=worker_id, host=f"worker-{worker_id}"
                )

                if claimed:
                    # Finish under our claim's epoch
                    await conn.fetchrow(
                        STMTS["finished"],
                        claimed["id"],
                        {"worker": worker_id},
                        claimed["run_epoch"],
                    )
                    return True
                return False
            finally:
                await conn.close()

        # 10 workers race to claim and finish
        results = await asyncio.gather(*[claim_and_finish(i) for i in range(10)])

        # Only one should succeed
        successes = sum(1 for r in results if r)
        assert successes == 1

        # Job should be finished
        conn = await connect_with_codec(db_params)
        job = await get_job(conn, job_id)
        assert job["state"] == "finished"
        await conn.close()


class TestDeadlockPrevention:
    """Test that our SQL patterns don't cause deadlocks."""

    async def test_concurrent_updates_no_deadlock(self, db_params, unique_queue):
        """Test that concurrent updates don't cause deadlocks."""
        conn = await connect_with_codec(db_params)

        # Create jobs
        await create_job_batch(conn, count=20, queue=unique_queue, state="queued")
        await conn.close()

        async def worker_lifecycle(worker_id: int):
            """Complete lifecycle: claim, run, finish."""
            conn = await connect_with_codec(db_params)
            processed = 0

            try:
                for _ in range(10):
                    # Claim
                    claimed = await claim(
                        conn,
                        unique_queue,
                        pid=worker_id,
                        host=f"worker-{worker_id}",
                    )

                    if not claimed:
                        break

                    job_id = claimed["id"]
                    epoch = claimed["run_epoch"]

                    # Run
                    await conn.execute(STMTS["run"], job_id, epoch, None)

                    # Small processing delay
                    await asyncio.sleep(0.001)

                    # Finish
                    await conn.fetchrow(
                        STMTS["finished"], job_id, {"worker": worker_id}, epoch
                    )

                    processed += 1
            finally:
                await conn.close()

            return processed

        # Run 10 workers concurrently
        results = await asyncio.gather(*[worker_lifecycle(i) for i in range(10)])

        # All jobs should be processed
        total_processed = sum(results)
        assert total_processed == 20

        # No jobs should be left in intermediate states
        conn = await connect_with_codec(db_params)
        finished_count = await count_state(conn, unique_queue, "finished")
        await conn.close()

        assert finished_count == 20


@pytest.mark.stress
class TestStressScenarios:
    """Stress tests with high load."""

    async def test_many_workers_many_jobs(self, db_params, unique_queue):
        """Test system under high load: 50 workers, 100 jobs."""
        conn = await connect_with_codec(db_params)
        await create_job_batch(conn, count=100, queue=unique_queue, state="queued")
        await conn.close()

        async def worker_process(worker_id: int):
            """Worker claims and processes jobs."""
            conn = await connect_with_codec(db_params)
            processed = 0

            try:
                while True:
                    claimed = await claim(
                        conn,
                        unique_queue,
                        pid=worker_id,
                        host=f"worker-{worker_id}",
                    )

                    if not claimed:
                        break

                    epoch = claimed["run_epoch"]
                    await conn.execute(STMTS["run"], claimed["id"], epoch, None)
                    await asyncio.sleep(0.0001)  # Tiny processing time
                    await conn.fetchrow(
                        STMTS["finished"], claimed["id"], {"worker": worker_id}, epoch
                    )

                    processed += 1
            finally:
                await conn.close()

            return processed

        # 50 workers
        start_time = datetime.now(UTC)
        results = await asyncio.gather(*[worker_process(i) for i in range(50)])
        duration = (datetime.now(UTC) - start_time).total_seconds()

        # Verify all jobs processed
        total_processed = sum(results)
        assert total_processed == 100

        # Performance check - should complete in reasonable time
        assert duration < 10.0  # 100 jobs in under 10 seconds

        print(f"\nProcessed 100 jobs with 50 workers in {duration:.2f}s")
        print(f"Throughput: {total_processed / duration:.1f} jobs/sec")


@pytest.mark.integration
class TestConcurrencyIntegration:
    """Integration tests for concurrent workflows."""

    async def test_concurrent_dependency_workflow(self, db_params, unique_queue):
        """Test complex workflow with concurrent dependency resolution."""
        conn = await connect_with_codec(db_params)

        # Create fan-out pattern: 1 parent -> 5 children
        parent_id = await create_job(conn, queue=unique_queue, state="queued")

        child_ids = []
        for _ in range(5):
            child_id = await create_job(
                conn, queue=unique_queue, waitfor_job=parent_id, state="waiting"
            )
            child_ids.append(child_id)

        # Process parent
        claimed = await claim(conn, unique_queue)
        epoch = claimed["run_epoch"]
        await conn.execute(STMTS["run"], claimed["id"], epoch, None)
        await conn.fetchrow(STMTS["finished"], claimed["id"], {}, epoch)

        # Trigger dependency resolution
        await conn.fetch(STMTS["enqueue-next-self-finished"], parent_id)
        await conn.close()

        # Process children concurrently
        async def process_child(worker_id: int):
            """Claim and process a child job."""
            conn = await connect_with_codec(db_params)
            try:
                claimed = await claim(
                    conn, unique_queue, pid=worker_id, host=f"worker-{worker_id}"
                )
                if claimed:
                    epoch = claimed["run_epoch"]
                    await conn.execute(STMTS["run"], claimed["id"], epoch, None)
                    await conn.fetchrow(STMTS["finished"], claimed["id"], {}, epoch)
                    return claimed["id"]
                return None
            finally:
                await conn.close()

        # 5 workers process 5 children
        results = await asyncio.gather(*[process_child(i) for i in range(5)])

        # All children should be processed
        processed_ids = [r for r in results if r]
        assert len(processed_ids) == 5
        assert set(processed_ids) == set(child_ids)

        # Verify all finished
        conn = await connect_with_codec(db_params)
        for child_id in child_ids:
            child = await get_job(conn, child_id)
            assert child["state"] == "finished"
        await conn.close()
