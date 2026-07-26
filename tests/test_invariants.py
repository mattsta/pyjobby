"""Platform invariants validated over generated workloads.

The other hypothesis suites generate SQL-level scenarios (claim ordering,
capability matching, dependency resolution). This one runs REAL workers
over generated job mixes and then validates the properties the platform
promises, from the recorded history rather than from what any single test
expected:

1. every job reaches exactly one terminal state, and stays there unless an
   operator requeues it
2. `jorb_history` is a legal walk of the state machine — no transition the
   platform does not define
3. `run_epoch` never decreases, and only advances on a claim
4. a completed DXE step is never recomputed (exactly-once effects) no matter
   how many attempts a job takes
5. queue controls are never violated: nothing runs on a paused queue, and
   in-flight count never exceeds `max_concurrency`
6. a job that waits for another never runs before that other job finished

These are the properties where "the test passed" and "the property held"
can diverge, so they are asserted globally over the whole workload instead
of per-case.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from .conftest import wait_for_job_state


def example_queue(base: str) -> str:
    """A fresh queue per hypothesis example.

    Function-scoped fixtures (including the autouse cleanup) run ONCE per
    test function, not per generated example, so examples would otherwise
    share a queue and each other's leftover jobs."""
    return f"{base}_{uuid.uuid4().hex[:8]}"


pytestmark = [pytest.mark.asyncio, pytest.mark.hypothesis]


# ============================================================================
# The state machine the platform actually defines
# ============================================================================
# Derived from pyjobby/pj.py (claim/run/finished/retry/crashed/cancelled),
# pyjobby/monitor.py (requeue sweeps), and pyjobby/db.py (cancel/requeue).
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    # a fresh row is 'enqueued' as queued (claimable) or waiting (blocked)
    "enqueued": {"queued", "waiting", "claimed", "running"},
    "waiting": {"queued", "cancelled"},
    "queued": {"claimed", "cancelled"},
    # claimed -> running normally; the monitor may requeue it, and a
    # cancel/failure can land before execution starts
    "claimed": {"running", "queued", "cancelled", "crashed"},
    # running -> terminal, or back to queued (retry backoff, self-reschedule,
    # durable sleep, monitor requeue)
    "running": {"finished", "crashed", "cancelled", "queued"},
    # terminal states are final EXCEPT for an explicit operator requeue
    "finished": {"queued"},
    "crashed": {"queued"},
    "cancelled": {"queued"},
}

TERMINAL = {"finished", "crashed", "cancelled"}


async def assert_history_is_a_legal_walk(pool, job_ids: list[int]) -> None:
    """Every recorded transition must be one the platform defines."""
    for job_id in job_ids:
        rows = await pool.fetch(
            "SELECT event, detail FROM jorb_history WHERE job_id = $1 ORDER BY id",
            job_id,
        )
        assert rows, f"job {job_id} has no history (the trigger must record it)"
        assert rows[0]["event"] == "enqueued", (
            f"job {job_id} history starts with {rows[0]['event']!r}, not 'enqueued'"
        )

        previous = "enqueued"
        epochs = []
        for row in rows[1:]:
            event = row["event"]
            assert event in LEGAL_TRANSITIONS[previous], (
                f"job {job_id}: illegal transition {previous!r} -> {event!r} "
                f"(history: {[r['event'] for r in rows]})"
            )
            if (epoch := row["detail"].get("run_epoch")) is not None:
                epochs.append(epoch)
            previous = event

        assert epochs == sorted(epochs), (
            f"job {job_id}: run_epoch went backwards across attempts: {epochs}"
        )


async def assert_at_most_one_live_terminal(pool, job_ids: list[int]) -> None:
    """A job's CURRENT state is terminal at most once — and if the history
    shows several terminal events, each must be followed by a requeue."""
    for job_id in job_ids:
        events = [
            r["event"]
            for r in await pool.fetch(
                "SELECT event FROM jorb_history WHERE job_id = $1 ORDER BY id", job_id
            )
        ]
        for i, event in enumerate(events):
            if event in TERMINAL and i + 1 < len(events):
                assert events[i + 1] == "queued", (
                    f"job {job_id}: {event!r} was followed by {events[i + 1]!r}; "
                    f"only an operator requeue may follow a terminal state"
                )


@pytest.fixture
async def effects_table(db_pool):
    """A side-effect table for proving steps execute exactly once."""
    await db_pool.execute(
        """CREATE TABLE IF NOT EXISTS jorb_step_effects (
               id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
               job_id BIGINT NOT NULL,
               at TIMESTAMPTZ NOT NULL DEFAULT now()
           )"""
    )
    await db_pool.execute("DELETE FROM jorb_step_effects")
    yield
    await db_pool.execute("DROP TABLE IF EXISTS jorb_step_effects")


# ============================================================================
# Generated workloads
# ============================================================================

JOB_SPECS = st.sampled_from(
    [
        ("tests.invariant_jobs.SucceedJob", {"n": 1}, 10),
        ("tests.invariant_jobs.FlakyJob", {"fail_times": 1}, 10),
        ("tests.invariant_jobs.FlakyJob", {"fail_times": 2}, 10),
        ("tests.invariant_jobs.AlwaysFailJob", {}, 2),
    ]
)


class TestWorkloadInvariants:
    @settings(
        max_examples=8,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    @given(specs=st.lists(JOB_SPECS, min_size=2, max_size=6))
    async def test_generated_workload_holds_every_invariant(
        self, db_pool, unique_queue, live_worker, specs
    ):
        """Run an arbitrary job mix and validate the platform's promises."""
        queue = example_queue(unique_queue)
        await live_worker(qname=queue)

        job_ids = []
        for job_class, kwargs, max_retries in specs:
            job_ids.append(
                await db_pool.fetchval(
                    """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
                       VALUES ($1, $2, $3, $4) RETURNING id""",
                    job_class,
                    kwargs,
                    queue,
                    {"max_retries": max_retries, "initial_retry_delay": 0},
                )
            )

        # every job must settle — nothing may get stuck
        for job_id in job_ids:
            await wait_for_job_state(db_pool, job_id, tuple(TERMINAL), timeout=30)

        await assert_history_is_a_legal_walk(db_pool, job_ids)
        await assert_at_most_one_live_terminal(db_pool, job_ids)

        # flaky jobs must have SUCCEEDED (their retry budget allows it) and
        # always-failing ones must be dead-lettered, never stuck elsewhere
        for job_id, (job_class, _, _) in zip(job_ids, specs, strict=True):
            state = await db_pool.fetchval(
                "SELECT state FROM jorb WHERE id = $1", job_id
            )
            if job_class.endswith("AlwaysFailJob"):
                assert state == "crashed", (
                    f"{job_class} should dead-letter, got {state}"
                )
            else:
                assert state == "finished", f"{job_class} should finish, got {state}"

    @settings(
        max_examples=5,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    @given(fail_times=st.integers(min_value=1, max_value=3))
    async def test_completed_step_never_recomputed_across_retries(
        self, db_pool, unique_queue, live_worker, effects_table, fail_times
    ):
        """However many attempts a job takes, its completed step ran once."""
        queue = example_queue(unique_queue)
        await live_worker(qname=queue)

        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
               VALUES ('tests.invariant_jobs.CountingStepJob', $1, $2, $3)
               RETURNING id""",
            {"fail_after_step": True},
            queue,
            {"max_retries": fail_times + 3, "initial_retry_delay": 0},
        )

        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
        assert row["result"] == {"marker": {"recorded_for": job_id}}

        # the job took more than one attempt...
        attempts = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_history WHERE job_id = $1 AND event = 'running'",
            job_id,
        )
        assert attempts >= 2, "the job must have retried for this test to mean anything"

        # ...yet the step's side effect happened exactly once
        effects = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step_effects WHERE job_id = $1", job_id
        )
        assert effects == 1, (
            f"step executed {effects} times across {attempts} attempts — "
            f"a completed step must never be recomputed"
        )


class TestQueueControlInvariants:
    @settings(
        max_examples=4,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ],
    )
    @given(max_concurrency=st.integers(min_value=1, max_value=3))
    async def test_in_flight_never_exceeds_max_concurrency(
        self, db_pool, unique_queue, live_worker, max_concurrency
    ):
        """Sampling the queue while several workers churn must never show
        more in-flight jobs than the configured cap."""
        queue = example_queue(unique_queue)
        await db_pool.execute(
            "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, $2)",
            queue,
            max_concurrency,
        )

        # workers first: starting them takes ~0.4s each, and jobs enqueued
        # beforehand would drain while the rest of the fleet is still coming
        # up, leaving nothing in flight to sample
        for _ in range(max_concurrency + 2):
            await live_worker(qname=queue)

        for _ in range(12):
            await db_pool.execute(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ('tests.dxe_jobs.SlowJob', $1, $2)""",
                {"seconds": 0.5},
                queue,
            )

        observed_peak = 0
        for _ in range(40):
            in_flight = await db_pool.fetchval(
                """SELECT count(*) FROM jorb
                   WHERE queue = $1 AND state IN ('claimed', 'running')""",
                queue,
            )
            observed_peak = max(observed_peak, in_flight)
            assert in_flight <= max_concurrency, (
                f"{in_flight} jobs in flight exceeds max_concurrency={max_concurrency}"
            )
            await asyncio.sleep(0.1)

        assert observed_peak >= 1, "no job ever ran; the test proved nothing"

    async def test_paused_queue_never_starts_a_job(
        self, db_pool, unique_queue, live_worker
    ):
        """While paused, no job on the queue may reach 'running'."""
        await db_pool.execute(
            "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)", unique_queue
        )
        job_ids = [
            await db_pool.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue)
                   VALUES ('tests.dxe_jobs.OkJob', '{}', $1) RETURNING id""",
                unique_queue,
            )
            for _ in range(4)
        ]
        for _ in range(3):
            await live_worker()

        for _ in range(15):
            started = await db_pool.fetchval(
                """SELECT count(*) FROM jorb
                   WHERE queue = $1 AND started IS NOT NULL""",
                unique_queue,
            )
            assert started == 0, "a paused queue must not start any job"
            await asyncio.sleep(0.1)

        # and unpausing releases them
        await db_pool.execute(
            "UPDATE jorb_queue SET paused = FALSE WHERE name = $1", unique_queue
        )
        for job_id in job_ids:
            await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)


class TestDependencyInvariants:
    async def test_dependent_never_runs_before_its_upstream_finishes(
        self, db_pool, unique_queue, live_worker
    ):
        """waitfor_job ordering holds under a real worker."""
        upstream = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue)
               VALUES ('tests.dxe_jobs.SlowJob', $1, $2) RETURNING id""",
            {"seconds": 1.0},
            unique_queue,
        )
        downstream = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, waitfor_job)
               VALUES ('tests.dxe_jobs.OkJob', '{}', $1, 'waiting', $2)
               RETURNING id""",
            unique_queue,
            upstream,
        )

        await live_worker()
        await live_worker()

        await wait_for_job_state(db_pool, downstream, ("finished",), timeout=30)

        rows = await db_pool.fetch(
            "SELECT id, started, finished FROM jorb WHERE id = ANY($1::bigint[])",
            [upstream, downstream],
        )
        by_id = {r["id"]: r for r in rows}
        assert by_id[downstream]["started"] >= by_id[upstream]["finished"], (
            "the dependent job started before its upstream finished"
        )

        await assert_history_is_a_legal_walk(db_pool, [upstream, downstream])
