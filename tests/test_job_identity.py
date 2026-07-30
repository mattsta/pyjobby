"""``identity_key``: at-most-once work, bounded by the retention horizon.

The platform has two enqueue-side dedupe primitives and they promise
different things, so nearly everything here is written as a CONTRAST:

* ``deadline_key`` is unique among *queued* rows, per queue. It collapses
  duplicate submissions of work that has not started and then **re-arms** the
  moment a worker claims the job — tomorrow's digest is a legitimate second
  job, and a second enqueue while the first is still queued *raises*.
* ``identity_key`` is unique across *every* state, table-wide. The row holds
  the key for its entire life, so a second enqueue does not raise: it returns
  the id of the job that already exists, whatever state that job is in.

The horizon is the honest part and is tested as such: retention reaping the
terminal row frees the key, and the sweep under test is the monitor's real
``sweep_expired_jobs`` rather than a DELETE that only resembles it.

Concurrency is proved by racing real enqueues on one pool, not by reasoning
about the SQL: two callers, one row, both told the same id, exactly one of
them told it created it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import asyncpg
import pytest
from click.testing import CliRunner

from pyjobby import client as client_module
from pyjobby import monitor
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


async def rows_holding(pool: asyncpg.Pool, key: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in await pool.fetch(
            "SELECT id, state::text AS state, job_class, queue, deadline_key "
            "FROM jorb WHERE identity_key = $1 ORDER BY id",
            key,
        )
    ]


async def wait_until_blocked_on_a_transaction(
    pool: asyncpg.Pool, timeout: float = 20.0
) -> None:
    """Poll until some backend here is waiting on another's transaction lock.

    That wait is what ON CONFLICT does when the identity it wants belongs to
    an uncommitted transaction: it blocks on the conflicting tuple's xact
    rather than skipping it. Observing the wait — instead of sleeping for a
    duration and hoping — is what makes the tests below deterministic: they
    release the holder only once the joiner is provably stuck on it.

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
        f"the second enqueue resolved without waiting, which it must not"
    )


async def put_in_state(pool: asyncpg.Pool, job_id: int, state: str) -> None:
    """Move a job to a terminal or in-flight state without running it.

    The claim about identity is about the ROW's state, not about how it got
    there, so these tests do not spend a worker to reach one.
    """
    await pool.execute(
        "UPDATE jorb SET state = $2::jorbstate, finished = "
        "CASE WHEN $2 IN ('finished', 'crashed', 'cancelled') THEN now() END, "
        "updated = now() WHERE id = $1",
        job_id,
        state,
    )


class TestTheKeyIsHeldInEveryState:
    """The difference from deadline_key, stated four times over."""

    @pytest.mark.parametrize(
        "state", ["queued", "running", "finished", "crashed", "cancelled"]
    )
    async def test_the_same_key_returns_the_same_id(
        self, job_client, db_pool, unique_queue, state
    ):
        key = f"identity:{unique_queue}:{state}"
        first = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, first, state)

        second = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)

        assert second == first, f"a {state} job did not hold its identity"
        assert len(await rows_holding(db_pool, key)) == 1

    async def test_a_deadline_key_would_have_re_armed_instead(
        self, job_client, db_pool, unique_queue
    ):
        """The same scenario under the other primitive, so the contrast is
        proved rather than asserted in prose: once the job leaves 'queued' a
        deadline_key is free again and the next enqueue is a NEW job."""
        deadline = f"deadline:{unique_queue}"
        first = await job_client.enqueue(OK, queue=unique_queue, deadline_key=deadline)
        await put_in_state(db_pool, first, "finished")

        second = await job_client.enqueue(OK, queue=unique_queue, deadline_key=deadline)

        assert second != first
        # and while it IS queued, the deadline_key raises where an identity
        # would have answered
        with pytest.raises(asyncpg.UniqueViolationError):
            await job_client.enqueue(OK, queue=unique_queue, deadline_key=deadline)

    async def test_the_identity_is_not_scoped_to_a_queue(
        self, job_client, db_pool, unique_queue
    ):
        """deadline_key is unique per (key, queue) because it is about the
        pending work in that queue. An identity names the WORK, so routing
        the same identity elsewhere is still the same work."""
        key = f"identity:{unique_queue}:global"
        first = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)

        second = await job_client.enqueue(
            OK, queue=f"{unique_queue}_alt", identity_key=key
        )

        assert second == first
        held = await rows_holding(db_pool, key)
        assert len(held) == 1
        assert held[0]["queue"] == unique_queue, "the second call re-routed the job"

    async def test_the_two_keys_cannot_be_combined_on_one_row(
        self, job_client, db_pool, unique_queue
    ):
        """Carrying both is refused, and nothing is written.

        The two answer the SAME question -- what happens to a duplicate
        enqueue? -- with opposite answers: an identity hands the existing job
        back for the life of the row, a deadline_key raises and then re-arms at
        the claim. A row carrying both makes which answer a caller gets depend
        on which index its INSERT collided with first, and the second call gets
        a job whose identity promise it never asked for. (This USED to be
        allowed: the previous test here asserted the coexistence, which was the
        absence of a check rather than a decision -- client._KEYS_CONTRADICT's
        own comment has always described the three keys as mutually exclusive.)
        """
        key = f"identity:{unique_queue}:both"
        deadline = f"deadline:{unique_queue}:both"

        with pytest.raises(ValueError, match="cannot be combined with deadline_key"):
            await job_client.enqueue(
                OK, queue=unique_queue, identity_key=key, deadline_key=deadline
            )

        assert await rows_holding(db_pool, key) == []


class TestTheCreatedFlag:
    """`enqueue` returns an id either way, so something has to say which."""

    async def test_it_is_true_only_for_the_call_that_wrote_the_row(
        self, job_client, unique_queue
    ):
        key = f"identity:{unique_queue}:flag"

        first_id, created = await job_client.enqueue_identified(
            OK, identity_key=key, queue=unique_queue
        )
        second_id, again = await job_client.enqueue_identified(
            OK, identity_key=key, queue=unique_queue
        )

        assert created is True
        assert again is False
        assert second_id == first_id

    async def test_it_stays_false_once_the_job_is_terminal(
        self, job_client, db_pool, unique_queue
    ):
        """A finished job is still the answer, and `created` still says the
        caller did not create it — which is what distinguishes "already done"
        from "just submitted"."""
        key = f"identity:{unique_queue}:terminal-flag"
        job_id, _ = await job_client.enqueue_identified(
            OK, identity_key=key, queue=unique_queue
        )
        await put_in_state(db_pool, job_id, "finished")

        again_id, created = await job_client.enqueue_identified(
            OK, identity_key=key, queue=unique_queue
        )

        assert (again_id, created) == (job_id, False)

    async def test_plain_enqueue_is_still_a_bare_int(self, job_client, unique_queue):
        """The return shape of enqueue() does not change because an option
        was passed: callers who do not need the flag do not pay for it."""
        result = await job_client.enqueue(
            OK, queue=unique_queue, identity_key=f"identity:{unique_queue}:bare"
        )
        assert isinstance(result, int)

    async def test_the_handle_composes(self, job_client, unique_queue):
        """enqueue_handle takes the same options, so "join the existing job
        and wait on it" is the ordinary workflow with one more argument."""
        key = f"identity:{unique_queue}:handle"
        first = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)

        handle = await job_client.enqueue_handle(
            OK, queue=unique_queue, identity_key=key
        )

        assert handle.id == first
        assert await handle.status() == "queued"


class TestTheClassMismatch:
    """The one case identity refuses instead of absorbing."""

    async def test_it_names_both_classes_and_the_key(self, job_client, unique_queue):
        key = f"identity:{unique_queue}:mismatch"
        await job_client.enqueue(OK, queue=unique_queue, identity_key=key)

        with pytest.raises(ValueError) as excinfo:
            await job_client.enqueue(OTHER, queue=unique_queue, identity_key=key)

        message = str(excinfo.value)
        assert key in message
        assert OK in message
        assert OTHER in message

    async def test_it_refuses_against_a_terminal_holder_too(
        self, job_client, db_pool, unique_queue
    ):
        """The key is held by the row, not by the row's liveness, so the
        refusal does not quietly lapse when the job finishes."""
        key = f"identity:{unique_queue}:mismatch-terminal"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, job_id, "crashed")

        with pytest.raises(ValueError, match=key):
            await job_client.enqueue(OTHER, queue=unique_queue, identity_key=key)

    async def test_nothing_was_written(self, job_client, db_pool, unique_queue):
        """A refused enqueue leaves the holder alone: ON CONFLICT DO NOTHING
        means the losing INSERT never touched the row it collided with."""
        key = f"identity:{unique_queue}:mismatch-clean"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        before = dict(
            await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        )

        with pytest.raises(ValueError):
            await job_client.enqueue(OTHER, queue=unique_queue, identity_key=key)

        after = dict(await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id))
        assert after == before
        assert len(await rows_holding(db_pool, key)) == 1


@pytest.mark.concurrency
class TestTheRace:
    """No read-then-insert window: the insert IS the read."""

    async def test_concurrent_enqueues_produce_one_row(
        self, db_pool, unique_queue, job_client
    ):
        key = f"identity:{unique_queue}:race"

        outcomes = await asyncio.gather(
            *(
                job_client.enqueue_identified(
                    OK, identity_key=key, queue=unique_queue, n=i
                )
                for i in range(8)
            )
        )

        ids = {job_id for job_id, _ in outcomes}
        assert len(ids) == 1, f"the race made more than one job: {ids}"
        assert sum(1 for _, created in outcomes if created) == 1, (
            "exactly one caller may report having created the job"
        )
        assert len(await rows_holding(db_pool, key)) == 1

    async def test_the_loser_never_sees_a_unique_violation(
        self, db_pool, unique_queue, job_client
    ):
        """The failure mode this replaces: a plain INSERT would have raised
        asyncpg.UniqueViolationError at whichever caller lost, turning a
        successful dedupe into an error the caller has to know to catch."""
        key = f"identity:{unique_queue}:race-quiet"

        results = await asyncio.gather(
            *(
                job_client.enqueue(OK, queue=unique_queue, identity_key=key)
                for _ in range(8)
            ),
            return_exceptions=True,
        )

        assert all(isinstance(r, int) for r in results), results
        assert len(set(results)) == 1

    async def test_an_uncommitted_holder_is_waited_out_not_duplicated(
        self, db_pool, unique_queue, job_client
    ):
        """The case a gather() cannot be relied on to produce, pinned by
        holding the losing side open on purpose.

        A transaction holds the identity and does not commit. The second
        caller blocks inside PostgreSQL — ON CONFLICT waits for an in-progress
        conflict rather than skipping it — so it must not resolve at all until
        the holder ends, and must then resolve to the holder's job rather than
        to a second row. Run at the SHIPPED attempt budget: convergence here
        is a property of the default configuration.
        """
        key = f"identity:{unique_queue}:uncommitted"
        holding = asyncio.Event()
        release = asyncio.Event()
        held: list[int] = []

        async def hold() -> None:
            async with db_pool.acquire() as conn, conn.transaction():
                held.append(
                    await JobClient.enqueue_in_transaction(
                        conn, OK, queue=unique_queue, identity_key=key
                    )
                )
                holding.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await holding.wait()
        joiner = asyncio.create_task(
            job_client.enqueue_identified(OK, identity_key=key, queue=unique_queue)
        )
        await wait_until_blocked_on_a_transaction(db_pool)

        assert not joiner.done(), (
            "the joiner answered while the holder's transaction was still "
            "open: it either invented a second row or reported an id that "
            "might yet be rolled back"
        )

        release.set()
        await holder
        job_id, created = await asyncio.wait_for(joiner, timeout=20)

        assert (job_id, created) == (held[0], False)
        assert len(await rows_holding(db_pool, key)) == 1

    async def test_a_rolled_back_holder_leaves_the_joiner_to_create_it(
        self, db_pool, unique_queue, job_client
    ):
        """The other way the wait can end. The joiner was blocked on a row
        that never existed, so when the holder rolls back the joiner's own
        INSERT is the one that lands — and `created` says so."""
        key = f"identity:{unique_queue}:rolled-back"
        holding = asyncio.Event()
        release = asyncio.Event()

        async def hold_then_abandon() -> None:
            async with db_pool.acquire() as conn:
                with contextlib.suppress(RuntimeError):
                    async with conn.transaction():
                        await JobClient.enqueue_in_transaction(
                            conn, OK, queue=unique_queue, identity_key=key
                        )
                        holding.set()
                        await release.wait()
                        raise RuntimeError("abandon")

        holder = asyncio.create_task(hold_then_abandon())
        await holding.wait()
        joiner = asyncio.create_task(
            job_client.enqueue_identified(OK, identity_key=key, queue=unique_queue)
        )
        await wait_until_blocked_on_a_transaction(db_pool)
        release.set()
        await holder
        job_id, created = await asyncio.wait_for(joiner, timeout=20)

        assert created is True
        assert [h["id"] for h in await rows_holding(db_pool, key)] == [job_id]

    async def test_one_attempt_is_not_enough_which_is_why_there_is_a_loop(
        self, db_pool, unique_queue, job_client, monkeypatch
    ):
        """The reason ENQUEUE_IDENTIFIED_SQL is run in a loop instead of once,
        demonstrated rather than argued.

        Budget cut to a single attempt, then exactly the sequence the loop
        exists for: the joiner blocks on the holder, the holder commits, and
        the joiner's statement — whose snapshot predates that commit — can
        neither insert (the conflict is real) nor see the row (it is newer
        than the snapshot). With no retry left it reports instead of guessing,
        and nothing extra was written.

        The message is pinned too, because this exception is the one place a
        caller learns their isolation level cannot support an identity.
        """
        monkeypatch.setattr(client_module, "_SPECULATIVE_ATTEMPTS", 1)
        key = f"identity:{unique_queue}:one-attempt"
        holding = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            async with db_pool.acquire() as conn, conn.transaction():
                await JobClient.enqueue_in_transaction(
                    conn, OK, queue=unique_queue, identity_key=key
                )
                holding.set()
                await release.wait()

        holder = asyncio.create_task(hold())
        await holding.wait()
        joiner = asyncio.create_task(
            job_client.enqueue_identified(OK, identity_key=key, queue=unique_queue)
        )
        # released only once the joiner is provably blocked on it, so the
        # commit lands INSIDE the joiner's single statement every run
        await wait_until_blocked_on_a_transaction(db_pool)
        release.set()
        await holder

        with pytest.raises(RuntimeError, match="REPEATABLE READ") as excinfo:
            await asyncio.wait_for(joiner, timeout=20)

        assert "Nothing was written" in str(excinfo.value)
        assert len(await rows_holding(db_pool, key)) == 1

    async def test_racing_callers_of_different_classes_are_both_answered(
        self, unique_queue, job_client
    ):
        """The class check reads the row that WON, so a caller that lost the
        race is still told about the mismatch — the check cannot be dodged by
        arriving second."""
        key = f"identity:{unique_queue}:race-mismatch"

        results = await asyncio.gather(
            *(
                job_client.enqueue(
                    OK if i % 2 else OTHER, queue=unique_queue, identity_key=key
                )
                for i in range(8)
            ),
            return_exceptions=True,
        )

        ids = {r for r in results if isinstance(r, int)}
        errors = [r for r in results if isinstance(r, ValueError)]
        assert len(ids) == 1
        assert errors, "every caller of the losing class must be told"
        assert all(key in str(e) for e in errors)


class TestTheSameRowVerbs:
    """retry, rerun and dlq retry requeue ONE row, so identity rides along."""

    async def test_rerun_keeps_the_identity(
        self, job_client, db_pool, unique_queue, dsn
    ):
        key = f"identity:{unique_queue}:rerun"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, job_id, "finished")

        result = await job_client.rerun_job(job_id)

        assert result["job_id"] == job_id
        held = await rows_holding(db_pool, key)
        assert [h["id"] for h in held] == [job_id]
        assert held[0]["state"] == "queued"
        # and the identity still answers, with the requeued row
        assert await job_client.enqueue(OK, queue=unique_queue, identity_key=key) == (
            job_id
        )

    async def test_retry_keeps_the_identity(self, job_client, db_pool, unique_queue):
        key = f"identity:{unique_queue}:retry"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, job_id, "crashed")

        result = await job_client.retry_job(job_id)

        assert result["status"] != "not_retriable"
        held = await rows_holding(db_pool, key)
        assert [h["id"] for h in held] == [job_id]

    async def test_the_dlq_retry_verb_keeps_it_too(
        self, job_client, db_pool, unique_queue, dsn
    ):
        """`pj-admin dlq retry` is the operator's spelling of the same
        transition, so it must not be the one that loses the key."""
        key = f"identity:{unique_queue}:dlq"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, job_id, "crashed")

        result = await run_cli("--dsn", dsn, "dlq", "retry", str(job_id))

        assert result.exit_code == 0, result.output
        held = await rows_holding(db_pool, key)
        assert [h["id"] for h in held] == [job_id]


class TestTheRetentionHorizon:
    """The honest bound: at-most-once for as long as the row is remembered."""

    async def test_the_sweep_frees_the_key(self, job_client, db_pool, unique_queue):
        """Driven by the monitor's REAL sweep, on a window small enough that
        a just-finished job is already past it — the same function
        `pj-monitor --retention-days` calls, so this cannot pass while the
        thing operators run behaves differently."""
        key = f"identity:{unique_queue}:reaped"
        first = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, first, "finished")

        deleted = await monitor.sweep_expired_jobs(db_pool, retention_days=0)

        assert deleted >= 1
        assert await rows_holding(db_pool, key) == []
        second = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        assert second != first, "the key was not freed by the reap"

    async def test_a_live_job_is_not_reaped_and_keeps_its_key(
        self, job_client, db_pool, unique_queue
    ):
        """The horizon applies to TERMINAL rows only, so a long-running job
        holding an identity for weeks is never quietly replaced."""
        key = f"identity:{unique_queue}:live"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, job_id, "running")

        await monitor.sweep_expired_jobs(db_pool, retention_days=0)

        assert await job_client.enqueue(OK, queue=unique_queue, identity_key=key) == (
            job_id
        )


class TestTheLookup:
    async def test_it_finds_the_holder_in_any_state(
        self, job_client, db_pool, unique_queue
    ):
        key = f"identity:{unique_queue}:lookup"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, job_id, "crashed")

        found = await job_client.get_job_by_identity(key)

        assert found is not None
        assert (found.id, found.state, found.job_class) == (job_id, "crashed", OK)

    async def test_an_unknown_key_is_none_not_an_error(self, job_client, unique_queue):
        assert await job_client.get_job_by_identity(f"never:{unique_queue}") is None

    async def test_a_reaped_identity_reads_as_absent(
        self, job_client, db_pool, unique_queue
    ):
        """Which is the same answer as "never enqueued", and deliberately so:
        both mean the next enqueue creates a new job."""
        key = f"identity:{unique_queue}:lookup-reaped"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await put_in_state(db_pool, job_id, "finished")
        await monitor.sweep_expired_jobs(db_pool, retention_days=0)

        assert await job_client.get_job_by_identity(key) is None


class TestTheBatchRefusal:
    """A batch returns one id per row IN ORDER; identity cannot promise that."""

    async def test_a_shared_identity_option_is_refused(self, job_client, unique_queue):
        with pytest.raises(ValueError, match="not a batch option"):
            await job_client.enqueue_batch(
                [(OK, {"n": 1}), (OK, {"n": 2})],
                queue=unique_queue,
                identity_key=f"identity:{unique_queue}:batch",
            )

    async def test_a_per_job_identity_option_is_refused_by_index(
        self, job_client, unique_queue
    ):
        with pytest.raises(ValueError, match="job 1"):
            await job_client.enqueue_batch(
                [
                    (OK, {"n": 1}),
                    (OK, {"n": 2}, {"identity_key": f"identity:{unique_queue}:b1"}),
                ],
                queue=unique_queue,
            )

    async def test_nothing_was_enqueued(self, job_client, db_pool, unique_queue):
        """Refused before the INSERT, so a rejected batch is not a half batch."""
        with pytest.raises(ValueError):
            await job_client.enqueue_batch(
                [(OK, {"n": 1})],
                queue=unique_queue,
                identity_key=f"identity:{unique_queue}:batch-clean",
            )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 0
        )


class TestTheOutboxPath:
    async def test_an_identified_enqueue_works_inside_a_caller_transaction(
        self, db_pool, unique_queue
    ):
        """The transactional-outbox helper takes the same option, so a job
        written beside the row that justifies it is deduped the same way."""
        key = f"identity:{unique_queue}:outbox"
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                first = await JobClient.enqueue_in_transaction(
                    conn, OK, queue=unique_queue, identity_key=key
                )
            async with conn.transaction():
                second = await JobClient.enqueue_in_transaction(
                    conn, OK, queue=unique_queue, identity_key=key
                )

        assert second == first
        assert len(await rows_holding(db_pool, key)) == 1

    async def test_a_rolled_back_outbox_leaves_the_key_free(
        self, db_pool, unique_queue
    ):
        """The identity is as durable as the transaction that claimed it, and
        no more: an abandoned outbox write must not burn the key."""
        key = f"identity:{unique_queue}:outbox-rollback"
        async with db_pool.acquire() as conn:
            with pytest.raises(RuntimeError, match="abandon"):
                async with conn.transaction():
                    await JobClient.enqueue_in_transaction(
                        conn, OK, queue=unique_queue, identity_key=key
                    )
                    raise RuntimeError("abandon the surrounding work")

        assert await rows_holding(db_pool, key) == []
        client = JobClient(pool=db_pool)
        assert await client.enqueue(OK, queue=unique_queue, identity_key=key)


class TestTheOperatorSurfaces:
    async def test_jobs_list_filters_by_identity(self, job_client, unique_queue, dsn):
        key = f"identity:{unique_queue}:list"
        wanted = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)
        await job_client.enqueue(OK, queue=unique_queue)

        result = await run_cli("--dsn", dsn, "jobs", "list", "--identity", key)

        assert result.exit_code == 0, result.output
        assert str(wanted) in result.output
        assert "Showing 1 job(s)" in result.output

    async def test_an_unheld_identity_lists_nothing_and_still_exits_zero(
        self, unique_queue, dsn
    ):
        """An empty answer is not a failure — the same rule every other
        filter follows."""
        result = await run_cli(
            "--dsn", dsn, "jobs", "list", "--identity", f"never:{unique_queue}"
        )

        assert result.exit_code == 0, result.output
        assert "No jobs found" in result.output

    async def test_jobs_inspect_shows_the_identity(self, job_client, unique_queue, dsn):
        key = f"identity:{unique_queue}:inspect"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)

        result = await run_cli("--dsn", dsn, "jobs", "inspect", str(job_id))

        assert result.exit_code == 0, result.output
        assert f"Identity:        {key}" in result.output

    async def test_jobs_inspect_says_nothing_when_there_is_none(
        self, job_client, unique_queue, dsn
    ):
        job_id = await job_client.enqueue(OK, queue=unique_queue)

        result = await run_cli("--dsn", dsn, "jobs", "inspect", str(job_id))

        assert "Identity:" not in result.output

    async def test_jobs_why_reports_it(self, job_client, unique_queue, dsn):
        """Because it changes the remedy: re-submitting this work would come
        straight back to this job."""
        key = f"identity:{unique_queue}:why"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)

        result = await run_cli("--dsn", dsn, "jobs", "why", str(job_id))

        assert result.exit_code == 0, result.output
        assert f"identity {key}" in result.output

    async def test_the_json_forms_carry_it(self, job_client, unique_queue, dsn):
        import json

        key = f"identity:{unique_queue}:json"
        job_id = await job_client.enqueue(OK, queue=unique_queue, identity_key=key)

        listed = await run_cli("--dsn", dsn, "jobs", "list", "--json")
        why = await run_cli("--dsn", dsn, "jobs", "why", str(job_id), "--json")

        assert json.loads(listed.output)[0]["identity_key"] == key
        assert json.loads(why.output)["identity_key"] == key


@pytest.mark.e2e
class TestAgainstARealWorker:
    async def test_the_second_enqueue_joins_the_job_that_already_ran(
        self, live_worker, db_pool, unique_queue
    ):
        """End to end on one row: enqueue, let a worker finish it, enqueue the
        identity again and get the finished job back — with its result, not a
        second execution."""
        await live_worker()
        client = JobClient(pool=db_pool)
        key = f"identity:{unique_queue}:live"
        first = await client.enqueue(OK, queue=unique_queue, identity_key=key, x=2)
        await wait_for_job_state(db_pool, first, ("finished",), timeout=30)

        second, created = await client.enqueue_identified(
            OK, identity_key=key, queue=unique_queue, x=2
        )

        assert (second, created) == (first, False)
        assert await client.get_job_result(second) is not None
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 1
        )
