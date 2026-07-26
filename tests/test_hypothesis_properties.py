"""
Property-Based Testing with Hypothesis for Pyjobby Producer-Consumer Workflows

This module uses Hypothesis to generate random test scenarios and verify
producer-consumer invariants hold across thousands of generated examples.

Key invariants tested:
1. Jobs created = Jobs processed (eventually)
2. No duplicate processing (each job processed exactly once)
3. State transitions are valid
4. Concurrent producers don't create conflicts
5. Recovery properly handles crashed workers
6. Dependencies are respected

Hypothesis generates random:
- Job counts
- Priority values
- Queue names
- Capability strings
- Timing scenarios
- Concurrent operations

Each test runs 100+ examples by default to find edge cases.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from pyjobby.pj import STMTS
from tests.utils.factories import create_job, get_job

# ============================================================================
# Hypothesis Strategies (Data Generators)
# ============================================================================

# Strategy for generating valid job classes
job_classes = st.sampled_from(
    [
        "examples.jobs.example_jobs.BasicJob",
        "examples.jobs.example_jobs.FailingJob",
        "examples.jobs.example_jobs.TimeoutJob",
    ]
)

# Strategy for generating queue names
queue_names = st.sampled_from(["default", "high_priority", "low_priority", "batch"])

# Strategy for generating capabilities
capabilities = st.sampled_from(
    [
        None,  # No capability requirement
        "cpu_intensive",
        "gpu_required",
        "disk_io",
        "network_io",
    ]
)

# Strategy for generating priorities (lower = higher priority)
priorities = st.integers(min_value=1, max_value=10000)

# Strategy for generating job states
job_states = st.sampled_from(
    ["queued", "claimed", "running", "finished", "crashed", "waiting"]
)

# Strategy for small positive integers (job counts, worker counts)
small_counts = st.integers(min_value=1, max_value=10)

# Strategy for timing offsets (seconds into future/past)
time_offsets = st.integers(min_value=-3600, max_value=3600)

# Strategy for worker hostnames
worker_hosts = st.sampled_from([f"worker-{i}" for i in range(5)])


# ============================================================================
# Property Test: Producer-Consumer Invariants
# ============================================================================

pytestmark = pytest.mark.asyncio


# ============================================================================
# Hypothesis Profiles for Different Testing Intensities
# ============================================================================

# Default profile: Quick validation (already registered by Hypothesis)
# hypothesis.settings.register_profile("default", max_examples=100)

# Stress profile: Exhaustive testing with thousands of examples
from hypothesis import Phase, settings

settings.register_profile(
    "stress",
    max_examples=1000,  # 1000 examples per test!
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.shrink],
)

# CI profile: Balance between coverage and speed
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# To use: pytest --hypothesis-profile=stress tests/test_hypothesis_properties.py


@pytest.mark.hypothesis
class TestProducerConsumerInvariants:
    """Property-based tests for producer-consumer invariants."""

    @settings(
        max_examples=500,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        job_count=st.integers(min_value=1, max_value=50),  # Increased range
        queue=queue_names,
        prio=priorities,
    )
    async def test_all_created_jobs_are_claimable(
        self, db_connection, job_count: int, queue: str, prio: int
    ):
        """Property: All created jobs in 'queued' state should eventually be claimable."""

        # Create N jobs (run_after in the past: inside the test transaction
        # now() is frozen at transaction start, so a wall-clock run_after
        # would not be claimable yet)
        claimable_at = datetime.now(UTC) - timedelta(minutes=5)
        job_ids = []
        for i in range(job_count):
            job_id = await create_job(
                db_connection,
                state="queued",
                queue=queue,
                prio=prio + i,  # Vary priority slightly
                run_after=claimable_at,
            )
            job_ids.append(job_id)

        # Invariant: All jobs should be claimable
        claimed = []
        for _ in range(job_count):
            result = await db_connection.fetch(
                STMTS["claim"],
                12345,  # worker_pid
                "test-host",
                queue,
                [None],  # capabilities - accept jobs with no capability
                prio + job_count,  # max priority
                None,  # claimed_by (unregistered worker)
            )
            if result:
                claimed.append(result[0]["id"])

        # All jobs should have been claimed
        assert len(claimed) == job_count
        assert set(claimed) == set(job_ids)

    @settings(
        max_examples=500,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        job_count=st.integers(min_value=2, max_value=30),  # Increased range
        queue=queue_names,
    )
    async def test_no_duplicate_claims(self, db_connection, job_count: int, queue: str):
        """Property: Each job should be claimed at most once (SKIP LOCKED ensures this)."""

        # Create N jobs (run_after backdated past the frozen transaction now())
        claimable_at = datetime.now(UTC) - timedelta(minutes=5)
        job_ids = []
        for _ in range(job_count):
            job_id = await create_job(
                db_connection, state="queued", queue=queue, run_after=claimable_at
            )
            job_ids.append(job_id)

        # Simulate multiple workers trying to claim simultaneously
        # Each should get a different job
        claimed_jobs = []

        for worker_id in range(job_count):
            result = await db_connection.fetch(
                STMTS["claim"],
                10000 + worker_id,
                f"worker-{worker_id}",
                queue,
                [None],
                10000,
                None,  # claimed_by (unregistered worker)
            )
            if result:
                claimed_jobs.append(result[0]["id"])

        # Invariant: No duplicates, all claimed
        assert len(claimed_jobs) == len(set(claimed_jobs)), "Duplicate claims detected!"
        assert len(claimed_jobs) == job_count

    @settings(
        max_examples=500,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        finish_count=st.integers(min_value=1, max_value=20),  # Increased range
        crash_count=st.integers(min_value=1, max_value=20),  # Increased range
    )
    async def test_jobs_reach_terminal_states(
        self, db_connection, finish_count: int, crash_count: int
    ):
        """Property: Jobs should eventually reach terminal states (finished/crashed)."""

        total_count = finish_count + crash_count

        # Create and claim jobs
        job_ids = []
        for _ in range(total_count):
            job_id = await create_job(db_connection, state="queued")
            # Claim it
            await db_connection.execute(
                """UPDATE jorb SET state = 'claimed', worker_pid = 12345,
                   worker_host = 'test-host' WHERE id = $1""",
                job_id,
            )
            job_ids.append(job_id)

        # Mark some as finished, some as crashed (epoch 0 = fresh rows)
        for i, job_id in enumerate(job_ids):
            if i < finish_count:
                await db_connection.execute(
                    STMTS["finished"], job_id, {"status": "success"}, 0
                )
            else:
                await db_connection.execute(
                    STMTS["crashed"], job_id, "Error", "Traceback", 0
                )

        # Invariant: All jobs should be in terminal states
        for job_id in job_ids:
            job = await get_job(db_connection, job_id)
            assert job["state"] in ["finished", "crashed"]


# ============================================================================
# Property Test: Concurrent Producers
# ============================================================================


@pytest.mark.hypothesis
class TestConcurrentProducers:
    """Property tests for concurrent job producers."""

    @settings(
        max_examples=300,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        producer_count=st.integers(min_value=2, max_value=8),  # More producers
        jobs_per_producer=st.integers(min_value=1, max_value=10),  # More jobs
    )
    async def test_concurrent_producers_create_all_jobs(
        self, db_params, producer_count: int, jobs_per_producer: int
    ):
        """Property: N concurrent producers creating M jobs each = N*M total jobs."""

        async def producer(producer_id: int, job_count: int, conn: asyncpg.Connection):
            """Single producer creating jobs."""
            created = []
            for i in range(job_count):
                job_id = await create_job(
                    conn,
                    state="queued",
                    admin_data={"producer_id": producer_id, "job_number": i},
                )
                created.append(job_id)
            return created

        # Create connections for each producer (shared factory registers
        # both json AND jsonb codecs)
        from pyjobby import db as pjdb

        connections = []
        for _ in range(producer_count):
            connections.append(await pjdb.connect(**db_params))

        try:
            # Run producers concurrently
            results = await asyncio.gather(
                *[
                    producer(i, jobs_per_producer, connections[i])
                    for i in range(producer_count)
                ]
            )

            # Invariant: Total jobs created = producer_count * jobs_per_producer
            all_job_ids = [job_id for result in results for job_id in result]
            assert len(all_job_ids) == producer_count * jobs_per_producer

            # Invariant: All job IDs are unique
            assert len(set(all_job_ids)) == len(all_job_ids)

            # Verify all jobs exist in database
            verify_conn = connections[0]
            for job_id in all_job_ids:
                job = await get_job(verify_conn, job_id)
                assert job is not None
                assert job["state"] == "queued"

        finally:
            # Cleanup
            for conn in connections:
                await conn.execute("DELETE FROM jorb")
                await conn.close()


# ============================================================================
# Property Test: Recovery Invariants
# ============================================================================


@pytest.mark.hypothesis
class TestRecoveryInvariants:
    """Property tests for reclaiming jobs abandoned by dead workers.

    Schema v1 removed the worker-side 'recover-abandoned' statement; the
    equivalent primitive is the monitor's unregistered-claim sweep
    (pyjobby.monitor.sweep_unregistered_claims), which requeues 'claimed'
    jobs with no registry reference once they age past a grace period."""

    @settings(
        max_examples=500,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        crashed_job_count=st.integers(min_value=1, max_value=25),  # More jobs
        recovery_timeout_minutes=st.integers(min_value=1, max_value=60),  # Wider range
    )
    async def test_recovery_returns_abandoned_jobs(
        self, db_pool, crashed_job_count: int, recovery_timeout_minutes: int
    ):
        """Property: Claims from dead workers older than the grace period
        should be requeued."""
        from pyjobby.monitor import sweep_unregistered_claims

        # Create claimed jobs from a "crashed" (never registered) worker
        job_ids = []
        old_time = datetime.now(UTC) - timedelta(minutes=recovery_timeout_minutes + 5)

        for _ in range(crashed_job_count):
            job_id = await create_job(db_pool, state="queued")
            # Claim it as the crashed worker (no jorb_worker registration)
            await db_pool.execute(
                """UPDATE jorb SET state = 'claimed', worker_host = 'crashed-worker',
                   worker_pid = 99999, updated = $1 WHERE id = $2""",
                old_time,
                job_id,
            )
            job_ids.append(job_id)

        # Recover abandoned claims
        recovered = await sweep_unregistered_claims(
            db_pool,
            claimed_grace_seconds=recovery_timeout_minutes * 60,
            batch_size=crashed_job_count,
        )

        # Invariant: All jobs should be recovered
        assert recovered == crashed_job_count

        # Invariant: All recovered jobs should be queued again
        for job_id in job_ids:
            job = await get_job(db_pool, job_id)
            assert job["state"] == "queued"

        # keep hypothesis examples independent of each other
        await db_pool.execute("DELETE FROM jorb WHERE id = ANY($1)", job_ids)

    @settings(
        max_examples=500,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        old_job_count=st.integers(min_value=1, max_value=15),  # More jobs
        recent_job_count=st.integers(min_value=1, max_value=15),  # More jobs
    )
    async def test_recovery_respects_timeout(
        self, db_pool, old_job_count: int, recent_job_count: int
    ):
        """Property: Only claims older than the grace period are requeued."""
        from pyjobby.monitor import sweep_unregistered_claims

        # Create old jobs (should be recovered)
        old_job_ids = []
        old_time = datetime.now(UTC) - timedelta(minutes=10)
        for _ in range(old_job_count):
            job_id = await create_job(db_pool, state="queued")
            await db_pool.execute(
                """UPDATE jorb SET state = 'claimed', worker_host = 'worker-1',
                   updated = $1 WHERE id = $2""",
                old_time,
                job_id,
            )
            old_job_ids.append(job_id)

        # Create recent jobs (should NOT be recovered)
        recent_job_ids = []
        recent_time = datetime.now(UTC) - timedelta(minutes=2)
        for _ in range(recent_job_count):
            job_id = await create_job(db_pool, state="queued")
            await db_pool.execute(
                """UPDATE jorb SET state = 'claimed', worker_host = 'worker-1',
                   updated = $1 WHERE id = $2""",
                recent_time,
                job_id,
            )
            recent_job_ids.append(job_id)

        # Recover with 5 minute grace
        recovered = await sweep_unregistered_claims(db_pool, claimed_grace_seconds=300)

        # Invariant: Only old jobs should be recovered
        assert recovered == old_job_count
        for job_id in old_job_ids:
            job = await get_job(db_pool, job_id)
            assert job["state"] == "queued"

        # Invariant: Recent jobs should still be claimed
        for job_id in recent_job_ids:
            job = await get_job(db_pool, job_id)
            assert job["state"] == "claimed"

        # keep hypothesis examples independent of each other (a leftover
        # "recent" claim would age past the grace in a later example)
        await db_pool.execute(
            "DELETE FROM jorb WHERE id = ANY($1)", old_job_ids + recent_job_ids
        )


# ============================================================================
# Property Test: Priority Ordering
# ============================================================================


@pytest.mark.hypothesis
class TestPriorityOrdering:
    """Property tests for job priority ordering."""

    @settings(
        max_examples=500,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        priorities=st.lists(
            st.integers(min_value=1, max_value=10000),  # Wider range
            min_size=2,
            max_size=25,  # More priorities to test
            unique=True,
        )
    )
    async def test_jobs_claimed_in_priority_order(
        self, db_connection, priorities: list[int]
    ):
        """Property: Jobs should be claimed in priority order (lower number first)."""

        # Create jobs with different priorities (run_after backdated past the
        # frozen transaction now())
        queue = "test"
        claimable_at = datetime.now(UTC) - timedelta(minutes=5)
        for prio in priorities:
            await create_job(
                db_connection,
                state="queued",
                queue=queue,
                prio=prio,
                run_after=claimable_at,
            )

        # Claim jobs one by one
        claimed_priorities = []
        for _ in range(len(priorities)):
            result = await db_connection.fetch(
                STMTS["claim"],
                12345,
                "test-host",
                queue,
                [None],
                max(priorities),  # Accept up to max priority
                None,  # claimed_by (unregistered worker)
            )
            if result:
                claimed_priorities.append(result[0]["prio"])

        # Invariant: Claimed priorities should be in ascending order
        assert claimed_priorities == sorted(priorities)


# ============================================================================
# Property Test: Capability Matching
# ============================================================================


@pytest.mark.hypothesis
class TestCapabilityMatching:
    """Property tests for capability-based job routing."""

    @settings(
        max_examples=30,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        required_capability=st.sampled_from(["cpu", "gpu", "disk", "network"]),
        job_count=st.integers(min_value=1, max_value=5),
    )
    async def test_worker_only_claims_matching_capability(
        self, db_connection, unique_queue: str, required_capability: str, job_count: int
    ):
        """Property: Workers claim jobs whose capability they advertise (or
        that require none), and never jobs demanding a capability they lack."""

        # hypothesis reuses the function-scoped connection across examples;
        # start each one from an empty queue
        await db_connection.execute("DELETE FROM jorb WHERE queue = $1", unique_queue)

        other_capability = next(
            c for c in ("cpu", "gpu", "disk", "network") if c != required_capability
        )
        # run_after backdated past the frozen transaction now()
        claimable_at = datetime.now(UTC) - timedelta(minutes=5)

        # Jobs requiring the worker's capability
        matching_job_ids = []
        for _ in range(job_count):
            job_id = await create_job(
                db_connection,
                state="queued",
                queue=unique_queue,
                capability=required_capability,
                run_after=claimable_at,
            )
            matching_job_ids.append(job_id)

        # Jobs with NO capability requirement (any worker may claim)
        no_cap_job_ids = []
        for _ in range(job_count):
            job_id = await create_job(
                db_connection,
                state="queued",
                queue=unique_queue,
                capability=None,
                run_after=claimable_at,
            )
            no_cap_job_ids.append(job_id)

        # Jobs demanding a capability this worker does NOT advertise
        unmatched_job_ids = []
        for _ in range(job_count):
            job_id = await create_job(
                db_connection,
                state="queued",
                queue=unique_queue,
                capability=other_capability,
                run_after=claimable_at,
            )
            unmatched_job_ids.append(job_id)

        # Worker with matching capability drains what it can
        claimed = []
        for _ in range(job_count * 3):
            result = await db_connection.fetch(
                STMTS["claim"],
                12345,
                "test-host",
                unique_queue,
                [required_capability],  # worker capabilities
                10000,
                None,  # claimed_by (unregistered worker)
            )
            if result:
                claimed.append(result[0]["id"])

        # Invariant: matching-capability AND no-capability jobs are claimed...
        assert set(claimed) == set(matching_job_ids + no_cap_job_ids)
        # ...and jobs needing a capability the worker lacks are left queued
        for job_id in unmatched_job_ids:
            assert (await get_job(db_connection, job_id))["state"] == "queued"


# ============================================================================
# Property Test: Dependency Resolution
# ============================================================================


@pytest.mark.hypothesis
class TestDependencyResolution:
    """Property tests for job dependency resolution."""

    @settings(
        max_examples=400,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        parent_count=st.integers(min_value=1, max_value=10),  # More parents
        children_per_parent=st.integers(min_value=1, max_value=5),  # More children
    )
    async def test_waitfor_job_resolution(
        self, db_connection, parent_count: int, children_per_parent: int
    ):
        """Property: Child jobs should only run after parent job finishes."""

        for _ in range(parent_count):
            # Create parent job
            parent_id = await create_job(db_connection, state="queued")

            # Create child jobs waiting for parent
            child_ids = []
            for _ in range(children_per_parent):
                child_id = await create_job(
                    db_connection, state="waiting", waitfor_job=parent_id
                )
                child_ids.append(child_id)

            # Invariant: Children should be in waiting state
            for child_id in child_ids:
                job = await get_job(db_connection, child_id)
                assert job["state"] == "waiting"

            # Claim and finish parent (epoch 0 = fresh row)
            await db_connection.execute(
                """UPDATE jorb SET state = 'claimed' WHERE id = $1""", parent_id
            )
            await db_connection.execute(
                STMTS["finished"], parent_id, {"status": "success"}, 0
            )

            # Trigger dependency resolution
            await db_connection.fetch(STMTS["enqueue-next-self-finished"], parent_id)

            # Invariant: All children should now be queued
            for child_id in child_ids:
                job = await get_job(db_connection, child_id)
                assert job["state"] == "queued"


# ============================================================================
# Stateful Testing: Job State Machine
# ============================================================================


@pytest.mark.hypothesis
class JobStateMachine(RuleBasedStateMachine):
    """
    Stateful property-based testing of job state machine.

    This generates random sequences of operations and verifies
    that invariants hold throughout.
    """

    def __init__(self):
        super().__init__()
        self.job_ids: set[int] = set()
        self.claimed_jobs: set[int] = set()
        self.finished_jobs: set[int] = set()
        self.crashed_jobs: set[int] = set()

    @initialize()
    async def setup(self):
        """Initialize state machine with database connection."""
        # Note: This would need proper async fixture handling
        pass

    @rule(count=st.integers(min_value=1, max_value=5))
    async def create_jobs(self, count: int):
        """Create N jobs."""
        # Placeholder - would create jobs and add IDs to self.job_ids
        pass

    @rule(job_id=st.sampled_from([]))  # Would sample from self.job_ids
    async def claim_job(self, job_id: int):
        """Claim a job."""
        # Placeholder - would claim job and add to self.claimed_jobs
        pass

    @rule(job_id=st.sampled_from([]))  # Would sample from self.claimed_jobs
    async def finish_job(self, job_id: int):
        """Finish a claimed job."""
        # Placeholder - would finish job and add to self.finished_jobs
        pass

    @invariant()
    def check_no_overlapping_states(self):
        """Invariant: Job can't be in multiple states simultaneously."""
        assert len(self.claimed_jobs & self.finished_jobs) == 0
        assert len(self.claimed_jobs & self.crashed_jobs) == 0
        assert len(self.finished_jobs & self.crashed_jobs) == 0


# Note: Stateful testing would be run with:
# TestJobStateMachine.TestCase.settings = settings(max_examples=50)
# TestJobStateMachine.TestCase().runTest()
