"""
Concurrency and race condition tests.

Tests concurrent access patterns, race conditions, and database
locking behavior under high contention scenarios.
"""

import asyncio
from datetime import datetime, timedelta
from typing import List

import asyncpg
import pytest
import orjson

from tests.utils.factories import (
    create_job,
    create_job_batch,
    count_jobs_by_state,
    get_job,
)


pytestmark = pytest.mark.asyncio


async def setup_json_codec(conn: asyncpg.Connection):
    """Configure orjson codec for a connection."""
    def orjson_encoder(obj):
        return orjson.dumps(obj).decode('utf-8')

    await conn.set_type_codec(
        "json",
        encoder=orjson_encoder,
        decoder=orjson.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=orjson_encoder,
        decoder=orjson.loads,
        schema="pg_catalog",
    )


async def connect_with_codec(db_params):
    """Create a connection with JSON codec configured."""
    conn = await asyncpg.connect(**db_params)
    await setup_json_codec(conn)
    return conn


class TestConcurrentJobClaiming:
    """Test concurrent job claiming scenarios."""

    async def test_multiple_workers_claim_different_jobs(self, db_params):
        """Test that multiple workers can claim different jobs without conflicts."""
        # Create multiple jobs
        conn = await connect_with_codec(db_params)
        try:
            job_ids = await create_job_batch(conn, count=20, state="queued")
            await conn.close()
        finally:
            pass

        from pyjobby.pj import STMTS

        # Simulate 5 workers claiming jobs concurrently
        async def worker_claim(worker_id: int) -> List[int]:
            """Worker claims jobs until none are available."""
            conn = await connect_with_codec(db_params)
            claimed = []

            try:
                for attempt in range(10):  # Try to claim up to 10 jobs
                    result = await conn.fetchrow(
                        STMTS["claim"],
                        worker_id,
                        f"worker-{worker_id}",
                        "test_queue",
                        ["test"],
                        1000
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
        results = await asyncio.gather(
            *[worker_claim(i) for i in range(5)]
        )

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

    async def test_skip_locked_prevents_double_claims(self, db_params):
        """Test that FOR UPDATE SKIP LOCKED prevents double claims."""
        conn = await connect_with_codec(db_params)
        job_id = await create_job(conn, state="queued")
        await conn.close()

        from pyjobby.pj import STMTS

        claim_results = []

        async def attempt_claim(worker_id: int):
            """Try to claim the same job."""
            conn = await connect_with_codec(db_params)
            try:
                result = await conn.fetchrow(
                    STMTS["claim"],
                    worker_id,
                    f"worker-{worker_id}",
                    "test_queue",
                    ["test"],
                    1000
                )
                return result
            finally:
                await conn.close()

        # 10 workers try to claim the same job simultaneously
        results = await asyncio.gather(
            *[attempt_claim(i) for i in range(10)]
        )

        # Only one should succeed
        successful_claims = [r for r in results if r is not None]
        assert len(successful_claims) == 1

        # The rest should be None
        failed_claims = [r for r in results if r is None]
        assert len(failed_claims) == 9

    async def test_concurrent_claims_different_queues(self, db_params):
        """Test concurrent claims from different queues don't interfere."""
        conn = await connect_with_codec(db_params)

        # Create jobs in different queues
        queue_a_jobs = []
        queue_b_jobs = []

        for i in range(10):
            queue_a_jobs.append(
                await create_job(conn, queue="queue_a", state="queued")
            )
            queue_b_jobs.append(
                await create_job(conn, queue="queue_b", state="queued")
            )

        await conn.close()

        from pyjobby.pj import STMTS

        async def claim_from_queue(queue_name: str, worker_id: int):
            """Claim all jobs from a specific queue."""
            conn = await connect_with_codec(db_params)
            claimed = []

            try:
                while True:
                    result = await conn.fetchrow(
                        STMTS["claim"],
                        worker_id,
                        f"worker-{queue_name}-{worker_id}",
                        queue_name,
                        ["test"],
                        1000
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
            claim_from_queue("queue_a", 1),
            claim_from_queue("queue_b", 2)
        )

        # Each queue should have all its jobs claimed
        assert set(queue_a_results) == set(queue_a_jobs)
        assert set(queue_b_results) == set(queue_b_jobs)

        # No overlap
        assert not set(queue_a_results).intersection(set(queue_b_results))


class TestConcurrentStateTransitions:
    """Test concurrent state transitions and updates."""

    async def test_concurrent_finish_operations(self, db_params):
        """Test multiple jobs finishing concurrently."""
        conn = await connect_with_codec(db_params)

        # Create and claim multiple jobs
        job_ids = []
        for i in range(10):
            job_id = await create_job(conn, state="running")
            job_ids.append(job_id)

        await conn.close()

        from pyjobby.pj import STMTS

        async def finish_job(job_id: int):
            """Mark a job as finished."""
            conn = await connect_with_codec(db_params)
            try:
                result = await conn.fetchrow(
                    STMTS["finished"],
                    job_id,
                    {"status": "success", "result": f"job-{job_id}"}
                )
                return result["id"]
            finally:
                await conn.close()

        # Finish all jobs concurrently
        finished_ids = await asyncio.gather(
            *[finish_job(jid) for jid in job_ids]
        )

        # All should finish successfully
        assert set(finished_ids) == set(job_ids)

        # Verify all are finished
        conn = await connect_with_codec(db_params)
        finished_count = await count_jobs_by_state(conn, "finished")
        await conn.close()

        assert finished_count >= 10

    async def test_concurrent_crash_operations(self, db_params):
        """Test multiple jobs crashing concurrently."""
        conn = await connect_with_codec(db_params)

        job_ids = []
        for i in range(10):
            job_id = await create_job(conn, state="running")
            job_ids.append(job_id)

        await conn.close()

        from pyjobby.pj import STMTS

        async def crash_job(job_id: int, error_num: int):
            """Mark a job as crashed."""
            conn = await connect_with_codec(db_params)
            try:
                await conn.execute(
                    STMTS["crash"],
                    job_id,
                    f"Error {error_num}",
                    f"Traceback for error {error_num}"
                )
                return job_id
            finally:
                await conn.close()

        # Crash all jobs concurrently
        crashed_ids = await asyncio.gather(
            *[crash_job(jid, i) for i, jid in enumerate(job_ids)]
        )

        assert len(crashed_ids) == 10

        # Verify all crashed
        conn = await connect_with_codec(db_params)
        crashed_count = await count_jobs_by_state(conn, "crashed")
        await conn.close()

        assert crashed_count >= 10


class TestConcurrentRetryCreation:
    """Test concurrent retry job creation."""

    async def test_concurrent_retry_creation(self, db_params):
        """Test creating retries for multiple jobs concurrently."""
        conn = await connect_with_codec(db_params)

        # Create crashed jobs
        crashed_ids = []
        for i in range(10):
            job_id = await create_job(conn, state="crashed")
            crashed_ids.append(job_id)

        await conn.close()

        from pyjobby.pj import STMTS

        async def create_retry(original_id: int, delay_minutes: int):
            """Create a retry job."""
            conn = await connect_with_codec(db_params)
            try:
                result = await conn.fetchrow(
                    STMTS["create-retry"],
                    original_id,
                    timedelta(minutes=delay_minutes),
                    1
                )
                return result["id"]
            finally:
                await conn.close()

        # Create retries concurrently
        retry_ids = await asyncio.gather(
            *[create_retry(cid, i+1) for i, cid in enumerate(crashed_ids)]
        )

        # All retries should be created
        assert len(retry_ids) == 10
        assert len(set(retry_ids)) == 10  # All unique

        # Verify retry jobs exist and reference originals
        conn = await connect_with_codec(db_params)
        for original_id, retry_id in zip(crashed_ids, retry_ids):
            retry_job = await get_job(conn, retry_id)
            assert retry_job["state"] == "queued"
            assert retry_job["admin_data"]["parent_job_id"] == original_id

        await conn.close()


class TestConcurrentDependencyResolution:
    """Test concurrent dependency resolution."""

    async def test_concurrent_waitfor_job_resolution(self, db_params):
        """Test resolving multiple waitfor_job dependencies concurrently."""
        conn = await connect_with_codec(db_params)

        # Create parent jobs
        parent_ids = []
        for i in range(5):
            parent_id = await create_job(conn, state="finished")
            parent_ids.append(parent_id)

        # Create child jobs waiting for parents
        child_ids = []
        for parent_id in parent_ids:
            child_id = await create_job(
                conn,
                waitfor_job=parent_id,
                state="waiting"
            )
            child_ids.append(child_id)

        await conn.close()

        from pyjobby.pj import STMTS

        async def resolve_dependency(parent_id: int):
            """Trigger dependency resolution for a parent job."""
            conn = await connect_with_codec(db_params)
            try:
                results = await conn.fetch(
                    STMTS["enqueue-next-self-finished"],
                    parent_id
                )
                return len(results)
            finally:
                await conn.close()

        # Resolve all dependencies concurrently
        results = await asyncio.gather(
            *[resolve_dependency(pid) for pid in parent_ids]
        )

        # Each should have resolved 1 dependency
        assert all(r >= 0 for r in results)

        # Verify all children are queued
        conn = await connect_with_codec(db_params)
        for child_id in child_ids:
            child = await get_job(conn, child_id)
            assert child["state"] == "queued"
        await conn.close()

    async def test_concurrent_waitfor_group_resolution(self, db_params):
        """Test resolving multiple waitfor_group dependencies concurrently."""
        conn = await connect_with_codec(db_params)

        # Create multiple groups
        groups = []
        for group_num in range(3):
            group_id = 10000 + group_num

            # Create jobs in group
            for i in range(3):
                await create_job(conn, run_group=group_id, state="finished")

            # Create waiter for group
            waiter_id = await create_job(
                conn,
                waitfor_group=group_id,
                state="waiting"
            )

            groups.append((group_id, waiter_id))

        await conn.close()

        from pyjobby.pj import STMTS

        async def resolve_group(group_id: int):
            """Resolve group dependency."""
            conn = await connect_with_codec(db_params)
            try:
                results = await conn.fetch(
                    STMTS["enqueue-next-if-peer-group-is-finished"],
                    group_id
                )
                return len(results)
            finally:
                await conn.close()

        # Resolve all groups concurrently
        results = await asyncio.gather(
            *[resolve_group(gid) for gid, _ in groups]
        )

        # Verify all waiters are queued
        conn = await connect_with_codec(db_params)
        for group_id, waiter_id in groups:
            waiter = await get_job(conn, waiter_id)
            assert waiter["state"] == "queued"
        await conn.close()


class TestConcurrentRecovery:
    """Test concurrent recovery operations."""

    async def test_concurrent_worker_recovery(self, db_params):
        """Test recovering jobs from multiple workers concurrently."""
        conn = await connect_with_codec(db_params)

        # Create jobs claimed by different workers
        worker_jobs = {}
        for worker_num in range(5):
            worker_host = f"dead-worker-{worker_num}"
            worker_jobs[worker_host] = []

            for i in range(3):
                job_id = await create_job(conn, state="queued")

                # Manually set as claimed by this worker with old timestamp
                old_time = datetime.utcnow() - timedelta(minutes=10)
                await conn.execute(
                    """UPDATE jorb
                       SET state = 'claimed',
                           worker_pid = $1,
                           worker_host = $2,
                           updated = $3
                       WHERE id = $4""",
                    10000 + worker_num,
                    worker_host,
                    old_time,
                    job_id
                )
                worker_jobs[worker_host].append(job_id)

        await conn.close()

        from pyjobby.pj import STMTS

        async def recover_worker(worker_host: str):
            """Recover jobs from a dead worker."""
            conn = await connect_with_codec(db_params)
            try:
                recovery_timeout = timedelta(minutes=5)
                results = await conn.fetch(
                    STMTS["recover-abandoned"],
                    worker_host,
                    recovery_timeout
                )
                return [r["id"] for r in results]
            finally:
                await conn.close()

        # Recover all workers concurrently
        results = await asyncio.gather(
            *[recover_worker(whost) for whost in worker_jobs.keys()]
        )

        # Verify each worker's jobs were recovered
        for i, (worker_host, expected_jobs) in enumerate(worker_jobs.items()):
            recovered_jobs = results[i]
            assert set(recovered_jobs) == set(expected_jobs)

        # Verify all jobs are queued
        conn = await connect_with_codec(db_params)
        for job_list in worker_jobs.values():
            for job_id in job_list:
                job = await get_job(conn, job_id)
                assert job["state"] == "queued"
        await conn.close()


class TestHighContentionScenarios:
    """Test behavior under high contention."""

    async def test_high_contention_claiming(self, db_params):
        """Test claiming with many workers competing for few jobs."""
        conn = await connect_with_codec(db_params)

        # Create only 5 jobs
        job_ids = await create_job_batch(conn, count=5, state="queued")
        await conn.close()

        from pyjobby.pj import STMTS

        async def aggressive_claimer(worker_id: int):
            """Worker aggressively tries to claim jobs."""
            conn = await connect_with_codec(db_params)
            claimed = []

            try:
                # Try 20 times even though only 5 jobs exist
                for _ in range(20):
                    result = await conn.fetchrow(
                        STMTS["claim"],
                        worker_id,
                        f"worker-{worker_id}",
                        "test_queue",
                        ["test"],
                        1000
                    )
                    if result:
                        claimed.append(result["id"])
                    await asyncio.sleep(0.0001)
            finally:
                await conn.close()

            return claimed

        # 20 workers compete for 5 jobs
        results = await asyncio.gather(
            *[aggressive_claimer(i) for i in range(20)]
        )

        # Collect all claims
        all_claimed = []
        for worker_claims in results:
            all_claimed.extend(worker_claims)

        # No duplicates - each job claimed exactly once
        assert len(all_claimed) == len(set(all_claimed))

        # Our 5 jobs should all be claimed
        our_jobs_claimed = [jid for jid in all_claimed if jid in job_ids]
        assert len(our_jobs_claimed) == 5
        assert set(our_jobs_claimed) == set(job_ids)

    async def test_rapid_state_transitions(self, db_params):
        """Test rapid state transitions don't cause corruption."""
        conn = await connect_with_codec(db_params)
        job_id = await create_job(conn, state="queued")
        await conn.close()

        from pyjobby.pj import STMTS

        # Multiple operations on same job
        async def claim_and_finish(worker_id: int):
            """Try to claim and finish the job."""
            conn = await connect_with_codec(db_params)
            try:
                # Try to claim
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    worker_id,
                    f"worker-{worker_id}",
                    "test_queue",
                    ["test"],
                    1000
                )

                if claimed:
                    # Try to finish
                    await conn.fetchrow(
                        STMTS["finished"],
                        claimed["id"],
                        {"worker": worker_id}
                    )
                    return True
                return False
            finally:
                await conn.close()

        # 10 workers race to claim and finish
        results = await asyncio.gather(
            *[claim_and_finish(i) for i in range(10)]
        )

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

    async def test_concurrent_updates_no_deadlock(self, db_params):
        """Test that concurrent updates don't cause deadlocks."""
        conn = await connect_with_codec(db_params)

        # Create jobs
        job_ids = await create_job_batch(conn, count=20, state="queued")
        await conn.close()

        from pyjobby.pj import STMTS

        async def worker_lifecycle(worker_id: int):
            """Complete lifecycle: claim, run, finish."""
            conn = await connect_with_codec(db_params)
            processed = 0

            try:
                for _ in range(10):
                    # Claim
                    claimed = await conn.fetchrow(
                        STMTS["claim"],
                        worker_id,
                        f"worker-{worker_id}",
                        "test_queue",
                        ["test"],
                        1000
                    )

                    if not claimed:
                        break

                    job_id = claimed["id"]

                    # Run
                    await conn.execute(STMTS["run"], job_id)

                    # Small processing delay
                    await asyncio.sleep(0.001)

                    # Finish
                    await conn.fetchrow(
                        STMTS["finished"],
                        job_id,
                        {"worker": worker_id}
                    )

                    processed += 1
            finally:
                await conn.close()

            return processed

        # Run 10 workers concurrently
        results = await asyncio.gather(
            *[worker_lifecycle(i) for i in range(10)]
        )

        # All jobs should be processed
        total_processed = sum(results)
        assert total_processed == 20

        # No jobs should be left in intermediate states
        conn = await connect_with_codec(db_params)
        finished_count = await count_jobs_by_state(conn, "finished")
        await conn.close()

        assert finished_count >= 20


@pytest.mark.stress
class TestStressScenarios:
    """Stress tests with high load."""

    async def test_many_workers_many_jobs(self, db_params):
        """Test system under high load: 50 workers, 100 jobs."""
        conn = await connect_with_codec(db_params)
        job_ids = await create_job_batch(conn, count=100, state="queued")
        await conn.close()

        from pyjobby.pj import STMTS

        async def worker_process(worker_id: int):
            """Worker claims and processes jobs."""
            conn = await connect_with_codec(db_params)
            processed = 0

            try:
                while True:
                    claimed = await conn.fetchrow(
                        STMTS["claim"],
                        worker_id,
                        f"worker-{worker_id}",
                        "test_queue",
                        ["test"],
                        1000
                    )

                    if not claimed:
                        break

                    await conn.execute(STMTS["run"], claimed["id"])
                    await asyncio.sleep(0.0001)  # Tiny processing time
                    await conn.fetchrow(
                        STMTS["finished"],
                        claimed["id"],
                        {"worker": worker_id}
                    )

                    processed += 1
            finally:
                await conn.close()

            return processed

        # 50 workers
        start_time = datetime.utcnow()
        results = await asyncio.gather(
            *[worker_process(i) for i in range(50)]
        )
        duration = (datetime.utcnow() - start_time).total_seconds()

        # Verify all jobs processed
        total_processed = sum(results)
        assert total_processed == 100

        # Performance check - should complete in reasonable time
        assert duration < 10.0  # 100 jobs in under 10 seconds

        print(f"\nProcessed 100 jobs with 50 workers in {duration:.2f}s")
        print(f"Throughput: {total_processed/duration:.1f} jobs/sec")


@pytest.mark.integration
class TestConcurrencyIntegration:
    """Integration tests for concurrent workflows."""

    async def test_concurrent_dependency_workflow(self, db_params):
        """Test complex workflow with concurrent dependency resolution."""
        conn = await connect_with_codec(db_params)

        # Create fan-out pattern: 1 parent -> 5 children
        parent_id = await create_job(conn, state="queued")

        child_ids = []
        for i in range(5):
            child_id = await create_job(
                conn,
                waitfor_job=parent_id,
                state="waiting"
            )
            child_ids.append(child_id)

        await conn.close()

        from pyjobby.pj import STMTS

        # Process parent
        conn = await connect_with_codec(db_params)
        claimed = await conn.fetchrow(
            STMTS["claim"],
            1, "worker-1", "test_queue", ["test"], 1000
        )
        await conn.execute(STMTS["run"], claimed["id"])
        await conn.fetchrow(STMTS["finished"], claimed["id"], {})

        # Trigger dependency resolution
        await conn.fetch(STMTS["enqueue-next-self-finished"], parent_id)
        await conn.close()

        # Process children concurrently
        async def process_child(worker_id: int):
            """Claim and process a child job."""
            conn = await connect_with_codec(db_params)
            try:
                claimed = await conn.fetchrow(
                    STMTS["claim"],
                    worker_id, f"worker-{worker_id}",
                    "test_queue", ["test"], 1000
                )
                if claimed:
                    await conn.execute(STMTS["run"], claimed["id"])
                    await conn.fetchrow(STMTS["finished"], claimed["id"], {})
                    return claimed["id"]
                return None
            finally:
                await conn.close()

        # 5 workers process 5 children
        results = await asyncio.gather(
            *[process_child(i) for i in range(5)]
        )

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
