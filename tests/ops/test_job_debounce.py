"""`debounce()` against a real fleet: the burst collapses, then re-arms.

The claim CLIENT_LIBRARY.md and writing-jobs.md make is about EXECUTIONS, not
about rows -- "one job runs, once the burst stops, with the freshest
arguments" -- and only a spawned worker actually claiming and running the job
can settle that. So a real `pj` process is what fires here, and the two facts
an operator would check are the two asserted:

1. five enqueues spread across a second, with a two-second quiet period,
   produce exactly ONE execution, and its result is the LAST call's argument
   rather than the first's;
2. the key is released at the claim, so a debounce arriving after the job has
   fired opens a new window and runs a SECOND job -- the collapse is a quiet
   period, not a lock on the work.

`pj-admin jobs why` is asked while the window is still open, because that is
the verb an operator reaches for when a job "has not run yet" and the answer
has to say that the deferral is the feature.
"""

from __future__ import annotations

import asyncio

import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import wait_until

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]

OK = "tests.dxe_jobs.OkJob"

#: The quiet period under test. Long enough that five enqueues 200ms apart
#: all land inside one window on a loaded box, short enough that the test
#: does not become a sleep.
PERIOD = 2.0


class TestTheBurstCollapses:
    async def test_five_enqueues_become_one_execution_of_the_last_arguments(
        self, fleet, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        key = f"debounce:{unique_queue}:burst"
        fleet.worker(unique_queue)

        job_id, created = await client.debounce(
            OK, key=key, period=PERIOD, queue=unique_queue, x=1
        )
        assert created is True
        for x in (2, 3, 4, 50):
            await asyncio.sleep(0.2)
            bounced, was_created = await client.debounce(
                OK, key=key, period=PERIOD, queue=unique_queue, x=x
            )
            assert (bounced, was_created) == (job_id, False), (
                "a call inside the quiet window wrote its own job"
            )

        finished = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=60)
        assert finished["result"] == {"doubled": 100}, (
            "the collapsed job ran arguments other than the last call's"
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 1
        ), "the burst was not collapsed onto one job"
        assert finished["run_count"] == 1

    async def test_a_debounce_after_the_fire_runs_a_second_job(
        self, fleet, db_pool, unique_queue
    ):
        """The key re-arms at the claim. A burst that arrives after the
        collapsed job has run is new work, and suppressing it would be the
        bug -- this is a quiet period, not an identity."""
        client = JobClient(pool=db_pool)
        key = f"debounce:{unique_queue}:rearm"
        fleet.worker(unique_queue)

        first, _ = await client.debounce(
            OK, key=key, period=0.5, queue=unique_queue, x=2
        )
        first_row = await wait_for_job_state(db_pool, first, ("finished",), timeout=60)
        assert first_row["result"] == {"doubled": 4}

        second, created = await client.debounce(
            OK, key=key, period=0.5, queue=unique_queue, x=11
        )
        assert second != first
        assert created is True

        second_row = await wait_for_job_state(
            db_pool, second, ("finished",), timeout=60
        )
        assert second_row["result"] == {"doubled": 22}
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 2
        )

    async def test_the_operator_is_told_the_deferral_is_the_debounce(
        self, fleet, admin, db_pool, unique_queue
    ):
        """The TROUBLESHOOTING answer to "my job is queued and nothing runs
        it": a debounced row parked in the future is working as designed, and
        both verbs say so while the window is still open."""
        client = JobClient(pool=db_pool)
        key = f"debounce:{unique_queue}:why"
        # no worker yet -- the window must still be open when we ask
        job_id, _ = await client.debounce(
            OK, key=key, period=3600.0, cap=7200.0, queue=unique_queue, x=1
        )

        why = admin("jobs", "why", str(job_id))
        inspected = admin("jobs", "inspect", str(job_id))

        assert why.returncode == 0, why.stdout + why.stderr
        assert "deferred" in why.stdout
        assert "DEBOUNCED" in why.stdout
        assert key in why.stdout
        assert f"Debounce:        {key}" in inspected.stdout

        # and it really does fire once a worker exists and the wait is over
        fleet.worker(unique_queue)
        await db_pool.execute("UPDATE jorb SET run_after = now() WHERE id = $1", job_id)
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT state = 'finished' FROM jorb WHERE id = $1", job_id
            ),
            describe="the debounced job firing once its wait was over",
            timeout=60,
        )
