"""``debounce()``: a burst of equivalent enqueues collapsed onto one job.

The platform now has three enqueue-side keys, and each one answers the same
question -- "what happens to a DUPLICATE?" -- differently. That is what most
of this file is written to pin:

* ``deadline_key`` IGNORES the duplicate. The second enqueue raises and the
  job that is already queued is not touched.
* ``identity_key`` RETURNS the duplicate's target. The existing job comes
  back, in whatever state it is in, unchanged.
* ``debounce_key`` MOVES the job. ``run_after`` is pushed out to now + this
  caller's quiet period and the row's kwargs are REPLACED, so what finally
  runs is one job carrying the freshest arguments.

The other half is the lifetime of the key: the row holds it while it is
queued and has never been claimed, so the first claim releases it for good.
A burst arriving while the collapsed job runs opens a NEW window and the two
rows coexist -- and so do a RETRY of the first and a parked second, which is
what stops a requeued row from taking its key back and breaking the worker's
own retry. All of it is asserted against a real ``jorb`` row and, at the
end, against a real worker that executes the collapsed job and is checked
for having run the LAST arguments.

Races are proved by racing, not by reading the SQL: concurrent debounces on
one pool converge on one row, and the bounce-vs-claim race is made
deterministic by holding the claim in an uncommitted transaction until the
bouncing statement is provably blocked on it.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from click.testing import CliRunner

from pyjobby import db as db_module
from pyjobby.cli import cli
from pyjobby.client import JobClient

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio

OK = "tests.dxe_jobs.OkJob"
OTHER = "tests.dxe_jobs.StepPipelineJob"


@pytest.fixture
def dsn(db_params: dict) -> str:
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
    )


async def run_cli(*args: str):
    """Invoke pj-admin in a worker thread (the CLI owns its own event loop)."""
    return await asyncio.to_thread(lambda: CliRunner().invoke(cli, list(args)))


async def rows_for(pool: asyncpg.Pool, queue: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in await pool.fetch(
            "SELECT id, state::text AS state, kwargs, run_after, "
            "       debounce_key, debounce_deadline, updated "
            "  FROM jorb WHERE queue = $1 ORDER BY id",
            queue,
        )
    ]


async def row(pool: asyncpg.Pool, job_id: int) -> dict[str, Any]:
    return dict(await pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id))


async def wait_until_blocked_on_a_transaction(
    pool: asyncpg.Pool, timeout: float = 20.0
) -> None:
    """Poll until some backend here is waiting on another's transaction lock.

    Both halves of debounce can block on one: the bounce UPDATE waits for a
    concurrent updater of the same row, and the speculative insert waits for
    the transaction holding the conflicting key. Observing the wait -- rather
    than sleeping and hoping -- is what makes the race tests below
    deterministic: the holder is released only once the other side is
    provably stuck on it.

    Each xdist worker owns its own database (conftest.db_params), so
    `current_database()` scopes this to this test's own traffic.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        blocked = await pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            " WHERE datname = current_database() "
            "   AND wait_event_type = 'Lock' AND wait_event = 'transactionid'"
        )
        if blocked:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"no backend blocked on the holder's transaction within {timeout}s: "
        f"the racing debounce resolved without waiting, which it must not"
    )


class TestTheFirstCallParks:
    """A quiet key produces an ORDINARY job, parked. The claim path is
    untouched: a queued row with a future run_after is durable sleep, which
    the platform already implements."""

    async def test_it_is_a_queued_row_parked_one_period_out(
        self, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        before = datetime.now(UTC)

        job_id, created = await client.debounce(
            OK, key=f"{unique_queue}:doc", period=30.0, queue=unique_queue, x=1
        )

        assert created is True
        parked = await row(db_pool, job_id)
        assert parked["state"] == "queued"
        assert parked["debounce_key"] == f"{unique_queue}:doc"
        assert parked["kwargs"] == {"x": 1}
        # ~now + period, generous either side: the clock is the caller's and
        # the assertion is about the period, not about scheduling precision.
        assert before + timedelta(seconds=25) < parked["run_after"]
        assert parked["run_after"] < before + timedelta(seconds=35)

    async def test_without_a_cap_nothing_bounds_the_deferral(
        self, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        job_id, _ = await client.debounce(
            OK, key=f"{unique_queue}:uncapped", period=30.0, queue=unique_queue
        )
        assert (await row(db_pool, job_id))["debounce_deadline"] is None

    async def test_a_cap_is_written_by_the_first_call(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        before = datetime.now(UTC)
        job_id, _ = await client.debounce(
            OK, key=f"{unique_queue}:capped", period=5.0, cap=60.0, queue=unique_queue
        )
        deadline = (await row(db_pool, job_id))["debounce_deadline"]
        assert deadline is not None
        assert before + timedelta(seconds=55) < deadline < before + timedelta(65)

    async def test_the_job_carries_every_other_enqueue_option(
        self, db_pool, unique_queue
    ):
        """debounce() is enqueue() with a collapse window in front of it, so
        an option that changes what the job IS still applies."""
        client = JobClient(pool=db_pool)
        job_id, _ = await client.debounce(
            OK,
            key=f"{unique_queue}:opts",
            period=30.0,
            queue=unique_queue,
            priority=7,
            capability="test",
            tags={"customer": "acme"},
            max_retries=3,
            doc=1,
        )
        parked = await row(db_pool, job_id)
        assert parked["prio"] == 7
        assert parked["capability"] == "test"
        assert parked["tags"] == {"customer": "acme"}
        assert parked["admin_data"]["max_retries"] == 3
        assert parked["kwargs"] == {"doc": 1}


class TestABounceMovesTheJob:
    """The contrast that names the feature: a duplicate does not raise
    (deadline_key) and is not answered with the row as it stands
    (identity_key) -- it MOVES the row."""

    async def test_the_same_row_comes_back_and_no_second_row_is_written(
        self, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:doc"
        first, created_first = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1
        )
        second, created_second = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=2
        )

        assert (second, created_second) == (first, False)
        assert created_first is True
        assert len(await rows_for(db_pool, unique_queue)) == 1

    async def test_the_bounce_pushes_run_after_further_out(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:doc"
        job_id, _ = await client.debounce(OK, key=key, period=10.0, queue=unique_queue)
        first_fire = (await row(db_pool, job_id))["run_after"]

        await client.debounce(OK, key=key, period=60.0, queue=unique_queue)

        moved = await row(db_pool, job_id)
        assert moved["run_after"] > first_fire

    async def test_last_writer_wins_on_the_arguments(self, db_pool, unique_queue):
        """The collapsed job runs with the FRESHEST arguments -- which is why
        debounce is only for work whose latest arguments are the right ones."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:doc"
        job_id, _ = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1, revision="a"
        )
        await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=2, revision="b"
        )
        await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=3, revision="c"
        )

        assert (await row(db_pool, job_id))["kwargs"] == {"x": 3, "revision": "c"}

    async def test_kwargs_are_replaced_not_merged(self, db_pool, unique_queue):
        """A key the latest call omits is GONE, because the row's arguments
        are this call's arguments and not an accumulation of the burst."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:doc"
        job_id, _ = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1, extra="dropped"
        )
        await client.debounce(OK, key=key, period=30.0, queue=unique_queue, x=2)

        assert (await row(db_pool, job_id))["kwargs"] == {"x": 2}

    async def test_a_bounce_can_also_shorten_the_wait(self, db_pool, unique_queue):
        """`period` RESTATES the wait rather than extending it: it is the
        caller's current quiet window, so a shorter one pulls the job in."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:doc"
        job_id, _ = await client.debounce(OK, key=key, period=600.0, queue=unique_queue)
        far = (await row(db_pool, job_id))["run_after"]

        await client.debounce(OK, key=key, period=5.0, queue=unique_queue)

        assert (await row(db_pool, job_id))["run_after"] < far

    async def test_the_bounce_touches_updated(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:doc"
        job_id, _ = await client.debounce(OK, key=key, period=30.0, queue=unique_queue)
        before = (await row(db_pool, job_id))["updated"]
        await asyncio.sleep(0.01)

        await client.debounce(OK, key=key, period=30.0, queue=unique_queue)

        assert (await row(db_pool, job_id))["updated"] > before

    async def test_a_different_key_is_a_different_window(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        one, _ = await client.debounce(
            OK, key=f"{unique_queue}:a", period=30.0, queue=unique_queue
        )
        two, created = await client.debounce(
            OK, key=f"{unique_queue}:b", period=30.0, queue=unique_queue
        )
        assert two != one
        assert created is True

    async def test_the_key_is_not_scoped_to_a_queue(self, db_pool, unique_queue):
        """Unlike deadline_key: a debounce_key names a burst of events in the
        application, and the same burst routed elsewhere is still one burst."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:cross"
        first, _ = await client.debounce(OK, key=key, period=30.0, queue=unique_queue)
        second, created = await client.debounce(
            OK, key=key, period=30.0, queue=f"{unique_queue}_other"
        )
        assert (second, created) == (first, False)


class TestTheCapBoundsCollapse:
    """Collapse without a bound is starvation, and `cap` is the answer."""

    async def test_bounces_past_the_cap_clamp_to_the_deadline(
        self, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:capped"
        job_id, _ = await client.debounce(
            OK, key=key, period=0.5, cap=1.0, queue=unique_queue, n=0
        )
        deadline = (await row(db_pool, job_id))["debounce_deadline"]

        for n in range(1, 6):
            await client.debounce(
                OK, key=key, period=60.0, cap=1.0, queue=unique_queue, n=n
            )

        clamped = await row(db_pool, job_id)
        assert clamped["run_after"] == deadline, (
            "a bounce pushed run_after past the cap the first call set"
        )
        # and the kwargs still followed the last writer
        assert clamped["kwargs"] == {"n": 5}

    async def test_the_deadline_is_never_rewritten_by_a_later_call(
        self, db_pool, unique_queue
    ):
        """The cap belongs to the WINDOW, not to the call: a later caller
        passing a bigger one does not extend the window it joined, and a
        caller passing none does not remove the ceiling."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:capped"
        job_id, _ = await client.debounce(
            OK, key=key, period=1.0, cap=2.0, queue=unique_queue
        )
        deadline = (await row(db_pool, job_id))["debounce_deadline"]

        await client.debounce(OK, key=key, period=1.0, cap=3600.0, queue=unique_queue)
        await client.debounce(OK, key=key, period=1.0, queue=unique_queue)

        assert (await row(db_pool, job_id))["debounce_deadline"] == deadline

    async def test_a_cap_below_the_period_clamps_the_first_parking_too(
        self, db_pool, unique_queue
    ):
        """Not an error: the ceiling is what the caller asked to be bounded
        by, so the very first parking honours it."""
        client = JobClient(pool=db_pool)
        job_id, _ = await client.debounce(
            OK, key=f"{unique_queue}:tiny", period=600.0, cap=1.0, queue=unique_queue
        )
        parked = await row(db_pool, job_id)
        assert parked["run_after"] == parked["debounce_deadline"]

    @pytest.mark.e2e
    async def test_a_capped_key_bounced_forever_still_fires(
        self, live_worker, db_pool, unique_queue
    ):
        """The whole point of the cap, end to end: a worker really runs the
        job even though the producers never stop."""
        await live_worker()
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:relentless"

        job_id, _ = await client.debounce(
            OK, key=key, period=5.0, cap=1.0, queue=unique_queue, x=1
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = await db_pool.fetchval(
                "SELECT state::text FROM jorb WHERE id = $1", job_id
            )
            if state != "queued":
                break
            # keep bouncing with a period far beyond the cap
            await client.debounce(
                OK, key=key, period=5.0, cap=1.0, queue=unique_queue, x=1
            )
            await asyncio.sleep(0.1)

        finished = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
        assert finished["result"] == {"doubled": 2}


class TestTheKeyIsReleasedAtTheClaim:
    """The lifetime of the key is the row's QUEUED lifetime, which is what
    jorb_debounce_idx says -- so the release is at the claim, not at the
    enqueue and not at the completion."""

    async def test_a_burst_after_the_claim_opens_a_new_window(
        self, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:doc"
        first, _ = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1
        )
        # a worker takes it; the row leaves the index and the key is free
        await db_pool.execute(
            "UPDATE jorb SET state = 'running', updated = now() WHERE id = $1", first
        )

        second, created = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=2
        )

        assert second != first
        assert created is True
        rows = await rows_for(db_pool, unique_queue)
        assert [r["state"] for r in rows] == ["running", "queued"]
        assert [r["kwargs"] for r in rows] == [{"x": 1}, {"x": 2}]

    @pytest.mark.parametrize("state", ["claimed", "running", "finished", "crashed"])
    async def test_no_non_queued_state_holds_the_key(
        self, db_pool, unique_queue, state
    ):
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:{state}"
        first, _ = await client.debounce(OK, key=key, period=30.0, queue=unique_queue)
        await db_pool.execute(
            "UPDATE jorb SET state = $2::jorbstate, updated = now() WHERE id = $1",
            first,
            state,
        )

        second, created = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue
        )
        assert (second != first, created) == (True, True)

    async def test_a_retried_job_does_not_take_the_key_back(
        self, db_pool, unique_queue
    ):
        """The release is PERMANENT, and this is why jorb_debounce_idx says
        `run_count = 0` and not just `state = 'queued'`. A debounced job that
        was claimed, failed and is retried comes back to 'queued' still
        carrying its key -- and if a burst opened a new window meanwhile, a
        key held by both rows would make the worker's own retry UPDATE
        violate the index, inside its failure handler, leaving a job that can
        never be retried."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:retry"
        first, _ = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1
        )
        # a worker claims it (run_count is bumped by claim_jorb) and it fails
        await db_pool.execute(
            "UPDATE jorb SET state = 'running', run_count = 1, "
            "run_epoch = 1, updated = now() WHERE id = $1",
            first,
        )
        second, created = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=2
        )
        assert created is True

        # the retry: the SAME statement pj.py runs on a failed attempt
        requeued = await db_pool.fetchval(
            "UPDATE jorb SET state = 'queued', run_epoch = run_epoch + 1, "
            "run_after = now() + interval '10 seconds', "
            "error_count = error_count + 1, updated = now() "
            "WHERE id = $1 AND state IN ('claimed', 'running') AND run_epoch = 1 "
            "RETURNING id",
            first,
        )
        assert requeued == first, "the retry of a debounced job was refused"

        # ... and the retried row is not bounceable: a duplicate collapses
        # onto the OPEN window, not onto the job that is retrying.
        third, created_again = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=3
        )
        assert (third, created_again) == (second, False)
        assert (await row(db_pool, first))["kwargs"] == {"x": 1}

    async def test_a_retried_job_keeps_the_key_as_provenance(
        self, db_pool, dsn, unique_queue
    ):
        """Released is not erased: the row still names the window it came out
        of, and `jobs inspect` says the window is closed rather than quoting a
        fire time that means nothing."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:provenance"
        job_id, _ = await client.debounce(OK, key=key, period=30.0, queue=unique_queue)
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished', run_count = 1, "
            "finished = now(), updated = now() WHERE id = $1",
            job_id,
        )

        assert (await row(db_pool, job_id))["debounce_key"] == key
        result = await run_cli("--dsn", dsn, "jobs", "inspect", str(job_id))
        assert f"Debounce:        {key}" in result.output
        assert "window:        closed" in result.output

    async def test_why_does_not_blame_a_debounce_for_retry_backoff(
        self, db_pool, dsn, unique_queue
    ):
        """A retried debounced job is deferred by BACKOFF. Saying producers
        are pushing it out would send the operator to the wrong place."""
        client = JobClient(pool=db_pool)
        job_id, _ = await client.debounce(
            OK, key=f"{unique_queue}:backoff", period=30.0, queue=unique_queue
        )
        await db_pool.execute(
            "UPDATE jorb SET run_count = 1, error_count = 1, updated = now() "
            "WHERE id = $1",
            job_id,
        )

        result = await run_cli("--dsn", dsn, "jobs", "why", str(job_id), "--json")

        answer = json.loads(result.output)
        assert answer["reason"] == "deferred"
        assert "DEBOUNCED" not in answer["summary"]
        assert "debounce_key" not in answer["details"]

    async def test_a_duplicate_after_run_after_passed_still_collapses(
        self, db_pool, unique_queue
    ):
        """The window closes at the CLAIM, not when run_after elapses. Nothing
        has started yet, so there is nothing to collapse the duplicate out
        of -- and the wait is restated, exactly as any other bounce does."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:due"
        job_id, _ = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1
        )
        # the window has elapsed but no worker exists to claim it
        await db_pool.execute(
            "UPDATE jorb SET run_after = now() - interval '1 minute' WHERE id = $1",
            job_id,
        )

        second, created = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=2
        )

        assert (second, created) == (job_id, False)
        moved = await row(db_pool, job_id)
        assert moved["run_after"] > datetime.now(UTC)
        assert moved["kwargs"] == {"x": 2}


@pytest.mark.concurrency
class TestRacingProducers:
    async def test_concurrent_debounces_converge_on_one_row(
        self, db_pool, unique_queue
    ):
        """Eight callers, one row: whoever loses the insert race blocks on the
        winner's transaction, comes back with nothing, re-asks with a fresh
        snapshot and bounces the row it could not see."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:race"

        results = await asyncio.gather(
            *(
                client.debounce(OK, key=key, period=30.0, queue=unique_queue, n=n)
                for n in range(8)
            )
        )

        ids = {job_id for job_id, _ in results}
        assert len(ids) == 1, f"the burst wrote {len(ids)} rows: {ids}"
        assert sum(1 for _, created in results if created) == 1
        assert len(await rows_for(db_pool, unique_queue)) == 1

    async def test_a_bounce_losing_to_a_claim_falls_through_to_a_fresh_insert(
        self, db_pool, db_params, unique_queue
    ):
        """The race the bounce UPDATE restates its predicate for: the row is
        claimed out from under the statement while it waits on the claiming
        transaction, so nothing is bounced -- and the freed key lets the same
        call open the next window instead of raising."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:claimrace"
        first, _ = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1
        )

        claimer = await asyncpg.connect(**db_params)
        try:
            transaction = claimer.transaction()
            await transaction.start()
            await claimer.execute(
                "UPDATE jorb SET state = 'claimed', updated = now() WHERE id = $1",
                first,
            )

            bouncing = asyncio.create_task(
                client.debounce(OK, key=key, period=30.0, queue=unique_queue, x=2)
            )
            await wait_until_blocked_on_a_transaction(db_pool)
            await transaction.commit()
            second, created = await bouncing
        finally:
            await claimer.close()

        assert second != first, "a claimed row was bounced"
        assert created is True
        assert (await row(db_pool, first))["kwargs"] == {"x": 1}, (
            "the claimed row's arguments were rewritten by the losing bounce"
        )

    async def test_a_debounce_losing_the_insert_bounces_on_the_next_pass(
        self, db_pool, db_params, unique_queue
    ):
        """The other half: the speculative insert blocks on an uncommitted
        producer, answers with no row, and the retry -- a fresh snapshot --
        finds the window and collapses into it."""
        key = f"{unique_queue}:insertrace"
        client = JobClient(pool=db_pool)

        # db_module.connect, not asyncpg's: the outbox INSERT writes jsonb
        # kwargs and needs pyjobby's codecs on the connection.
        holder = await db_module.connect(**db_params)
        try:
            transaction = holder.transaction()
            await transaction.start()
            winner = await JobClient.enqueue_in_transaction(
                holder,
                OK,
                queue=unique_queue,
                debounce_key=key,
                run_after=datetime.now(UTC) + timedelta(seconds=30),
                x=1,
            )

            joining = asyncio.create_task(
                client.debounce(OK, key=key, period=30.0, queue=unique_queue, x=2)
            )
            await wait_until_blocked_on_a_transaction(db_pool)
            await transaction.commit()
            joined, created = await joining
        finally:
            await holder.close()

        assert (joined, created) == (winner, False)
        assert (await row(db_pool, winner))["kwargs"] == {"x": 2}
        assert len(await rows_for(db_pool, unique_queue)) == 1


class TestRefusals:
    """Every one of these is a caller error the platform can see, so it says
    so rather than writing a row whose behaviour would not match the name."""

    @pytest.mark.parametrize("other", [{"identity_key": "i"}, {"deadline_key": "d"}])
    async def test_the_three_keys_cannot_be_combined(
        self, db_pool, unique_queue, other
    ):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="promise different things"):
            await client.debounce(OK, key="k", period=30.0, queue=unique_queue, **other)

    async def test_both_other_keys_are_named_at_once(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="identity_key and deadline_key"):
            await client.debounce(
                OK,
                key="k",
                period=30.0,
                queue=unique_queue,
                identity_key="i",
                deadline_key="d",
            )

    @pytest.mark.parametrize("waiter", [{"waitfor_job": 1}, {"waitfor_group": 1}])
    async def test_a_debounced_job_cannot_also_wait(
        self, db_pool, unique_queue, waiter
    ):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="nothing would ever collapse"):
            await client.debounce(
                OK, key="k", period=30.0, queue=unique_queue, **waiter
            )

    async def test_the_priority_ceiling_applies_as_it_does_to_enqueue(
        self, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="above the worker priority ceiling"):
            await client.debounce(
                OK, key="k", period=30.0, queue=unique_queue, priority=5000
            )

    @pytest.mark.parametrize("period", [0, -1.0])
    async def test_a_period_that_collapses_nothing_is_refused(
        self, db_pool, unique_queue, period
    ):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="positive number of seconds"):
            await client.debounce(OK, key="k", period=period, queue=unique_queue)

    @pytest.mark.parametrize("cap", [0, -1.0])
    async def test_a_non_positive_cap_is_refused(self, db_pool, unique_queue, cap):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="pass None"):
            await client.debounce(OK, key="k", period=1.0, cap=cap, queue=unique_queue)

    async def test_a_cap_without_a_key_bounds_nothing(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="no window without a key"):
            await client.enqueue(
                OK, queue=unique_queue, debounce_deadline=datetime.now(UTC)
            )

    async def test_a_key_naming_two_job_classes_is_refused(self, db_pool, unique_queue):
        """The one caller error the bounce itself can detect, and it is
        detected BEFORE the row is touched: bouncing would leave the parked
        job running the other class's arguments."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:twoclasses"
        job_id, _ = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1
        )

        with pytest.raises(ValueError, match="not the requested"):
            await client.debounce(OTHER, key=key, period=30.0, queue=unique_queue)

        assert (await row(db_pool, job_id))["kwargs"] == {"x": 1}

    async def test_a_batch_refuses_the_option_shared_and_per_job(
        self, db_pool, unique_queue
    ):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="not a batch option"):
            await client.enqueue_batch(
                [(OK, {"x": 1})], queue=unique_queue, debounce_key="k"
            )
        with pytest.raises(ValueError, match="job 0: debounce_key is not a batch"):
            await client.enqueue_batch(
                [(OK, {"x": 1}, {"debounce_key": "k"})], queue=unique_queue
            )


class TestAForkDoesNotInheritTheWindow:
    async def test_the_fork_holds_no_debounce_key(self, db_pool, unique_queue):
        """FORK_JOB_SQL lists the columns it copies and neither of these is
        one: a fork is a second live row, and two live rows sharing a collapse
        window would make the window mean nothing."""
        client = JobClient(pool=db_pool)
        source, _ = await client.debounce(
            OK, key=f"{unique_queue}:fork", period=30.0, cap=60.0, queue=unique_queue
        )

        forked = await db_module.fork_job(db_pool, source)

        fork_row = await row(db_pool, forked["job_id"])
        assert fork_row["debounce_key"] is None
        assert fork_row["debounce_deadline"] is None
        # ... and the source still holds its own
        assert (await row(db_pool, source))["debounce_key"] == f"{unique_queue}:fork"


class TestTheOperatorSurfaces:
    async def test_inspect_shows_the_key_the_fire_time_and_the_cap(
        self, db_pool, dsn, unique_queue
    ):
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:inspect"
        job_id, _ = await client.debounce(
            OK, key=key, period=30.0, cap=90.0, queue=unique_queue
        )

        result = await run_cli("--dsn", dsn, "jobs", "inspect", str(job_id))

        assert result.exit_code == 0, result.output
        assert f"Debounce:        {key}" in result.output
        assert "fires:" in result.output
        assert "cap:" in result.output

    async def test_inspect_says_so_when_nothing_caps_the_window(
        self, db_pool, dsn, unique_queue
    ):
        client = JobClient(pool=db_pool)
        job_id, _ = await client.debounce(
            OK, key=f"{unique_queue}:uncapped", period=30.0, queue=unique_queue
        )
        result = await run_cli("--dsn", dsn, "jobs", "inspect", str(job_id))
        assert "none (may defer indefinitely)" in result.output

    async def test_why_explains_the_deferral_as_the_debounce_it_is(
        self, db_pool, dsn, unique_queue
    ):
        """The TROUBLESHOOTING claim: a debounced job sitting queued with a
        future run_after is working as designed, and `jobs why` says when it
        fires and what bounds it."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:why"
        job_id, _ = await client.debounce(
            OK, key=key, period=30.0, cap=90.0, queue=unique_queue
        )

        result = await run_cli("--dsn", dsn, "jobs", "why", str(job_id), "--json")

        answer = json.loads(result.output)
        assert answer["reason"] == "deferred"
        assert "DEBOUNCED" in answer["summary"]
        assert key in answer["summary"]
        assert answer["details"]["debounce_key"] == key
        assert answer["details"]["debounce_deadline"] != "none"

    async def test_why_names_the_unbounded_window_as_unbounded(
        self, db_pool, dsn, unique_queue
    ):
        client = JobClient(pool=db_pool)
        job_id, _ = await client.debounce(
            OK, key=f"{unique_queue}:why2", period=30.0, queue=unique_queue
        )
        result = await run_cli("--dsn", dsn, "jobs", "why", str(job_id), "--json")
        answer = json.loads(result.output)
        assert "nothing caps that" in answer["summary"]
        assert answer["details"]["debounce_deadline"] == "none"

    async def test_an_ordinary_deferred_job_says_nothing_about_debounce(
        self, db_pool, dsn, unique_queue
    ):
        """The extension is additive: retry backoff and enqueue-at still get
        the answer they always got."""
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            OK, queue=unique_queue, run_after=datetime.now(UTC) + timedelta(hours=1)
        )
        result = await run_cli("--dsn", dsn, "jobs", "why", str(job_id), "--json")
        answer = json.loads(result.output)
        assert answer["reason"] == "deferred"
        assert "DEBOUNCED" not in answer["summary"]
        assert "debounce_key" not in answer["details"]


@pytest.mark.e2e
class TestAgainstARealWorker:
    async def test_the_burst_runs_once_with_the_last_arguments(
        self, live_worker, db_pool, unique_queue
    ):
        """The whole feature in one story: five enqueues over a second, one
        job, and it executes the arguments of the fifth."""
        await live_worker()
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:live"

        job_id, created = await client.debounce(
            OK, key=key, period=2.0, queue=unique_queue, x=1
        )
        assert created is True
        for x in (2, 3, 4, 21):
            await asyncio.sleep(0.15)
            bounced, was_created = await client.debounce(
                OK, key=key, period=2.0, queue=unique_queue, x=x
            )
            assert (bounced, was_created) == (job_id, False)

        finished = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
        assert finished["result"] == {"doubled": 42}, (
            "the burst ran the wrong arguments"
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 1
        ), "the burst was not collapsed onto one job"

    async def test_a_burst_after_the_fire_is_a_second_job(
        self, live_worker, db_pool, unique_queue
    ):
        """The key re-arms at the claim, so the next burst is genuinely new
        work and not a duplicate the platform should have suppressed."""
        await live_worker()
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:rearm"

        first, _ = await client.debounce(
            OK, key=key, period=0.5, queue=unique_queue, x=1
        )
        await wait_for_job_state(db_pool, first, ("finished",), timeout=30)

        second, created = await client.debounce(
            OK, key=key, period=0.5, queue=unique_queue, x=3
        )
        assert second != first
        assert created is True
        finished = await wait_for_job_state(db_pool, second, ("finished",), timeout=30)
        assert finished["result"] == {"doubled": 6}
