"""`identity_key` end to end, including the horizon that bounds it.

The claim CLIENT_LIBRARY.md and OPERATIONS.md make is a claim about the
whole platform, not about one INSERT: this exact work happens at most once
**for as long as retention keeps the row**. So the story is told with real
processes — a spawned `pj` worker that actually runs the job, a spawned
`pj-monitor` whose retention sweep actually reaps it — and asserted at each
of the three moments that matter:

1. the work runs once and the identity holds while it does;
2. a second enqueue of the identity, after the job has FINISHED, returns
   that finished job rather than doing the work again;
3. once the monitor reaps the terminal row, the key is free and a third
   enqueue creates a new job. That is not a leak — it is the documented
   horizon, and a caller who needs more scopes the key to a time it can
   name.

Step 3 is what makes this an ops test rather than a unit one: nothing but a
running monitor proves the horizon is real, and `--retention-days` compressed
to seconds is the same code path as the default thirty days.
"""

from __future__ import annotations

import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import wait_until

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]

OK = "tests.dxe_jobs.OkJob"

#: Retention window for the fleet under test: a job that terminated ~2s ago
#: is already past it. Same shape as tests/ops/test_soak.py's compressed
#: windows — seconds expressed as a fraction of a day, so the flag under test
#: is the production one and not a test-only alias.
RETENTION_DAYS = 2 / 86400


class TestTheIdentityHorizon:
    async def test_the_key_holds_until_retention_reaps_the_row(
        self, fleet, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        key = f"identity:{unique_queue}:horizon"
        fleet.worker(unique_queue)

        # 1. the work is enqueued once and really runs
        first = await client.enqueue(OK, queue=unique_queue, identity_key=key, x=21)
        row = await wait_for_job_state(db_pool, first, ("finished",), timeout=60)
        assert row["result"] == {"doubled": 42}

        # 2. the identity answers with the FINISHED job -- no second row, no
        #    second execution, and `created` says the caller did not make it
        second, created = await client.enqueue_identified(
            OK, identity_key=key, queue=unique_queue, x=21
        )
        assert (second, created) == (first, False)
        assert await client.get_job_result(second) == {"doubled": 42}
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 1
        ), "the identity was enqueued twice and ran twice"

        # 3. the monitor's retention sweep reaps the terminal row, and the
        #    key goes with it
        fleet.monitor("--retention-days", str(RETENTION_DAYS))
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT count(*) = 0 FROM jorb WHERE identity_key = $1", key
            ),
            describe="retention reaping the identified job",
            timeout=60,
        )

        third, created_again = await client.enqueue_identified(
            OK, identity_key=key, queue=unique_queue, x=21
        )
        assert third != first, "the reaped key was not released"
        assert created_again is True
        assert await client.get_job_by_identity(key) is not None

    async def test_the_operator_can_find_a_job_by_the_caller_s_own_name(
        self, fleet, admin, db_pool, unique_queue
    ):
        """The support question this exists for: "did order 4711 ever ship?"
        asked with the key the application already has, not a job id nobody
        wrote down."""
        client = JobClient(pool=db_pool)
        key = f"identity:{unique_queue}:order-4711"
        fleet.worker(unique_queue)
        job_id = await client.enqueue(OK, queue=unique_queue, identity_key=key, x=1)
        await wait_for_job_state(db_pool, job_id, ("finished",), timeout=60)

        listed = admin("jobs", "list", "--identity", key)
        inspected = admin("jobs", "inspect", str(job_id))

        assert listed.returncode == 0, listed.stdout + listed.stderr
        assert str(job_id) in listed.stdout
        assert "Showing 1 job(s)" in listed.stdout
        assert f"Identity:        {key}" in inspected.stdout
