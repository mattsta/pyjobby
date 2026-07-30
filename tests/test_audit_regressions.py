"""Regressions from the three-way adversarial audit.

Each test here pins a defect that was REPRODUCED against the real database
before it was fixed, and the reason it lives in one file is that the defects
share one shape: a rule the codebase had written down in prose and had not
made true in every statement that was subject to it.

Three families:

* **A dedupe key's collapse duty ends the first time its row leaves
  'queued'.** ``jorb_deadline_idx`` and ``jorb_debounce_idx`` are partial on
  ``state = 'queued'``, so a statement that puts a row BACK into 'queued'
  while it still carries a key re-enters an index the row was already released
  from. When a later burst holds that key, the statement raises instead of
  requeueing -- and the sweeps and the bulk verbs are BATCH statements, so one
  such row aborted the whole thing and stranded every unrelated job in it.
  ``db.REQUEUE_CLEARS_KEYS`` / ``db.WAKE_CLEARS_KEYS`` are the fix; these are
  the four ways it went wrong.

* **The keys' mutual exclusion.** ``client._KEYS_CONTRADICT``'s comment has
  always said the three enqueue-side keys are mutually exclusive, and only the
  debounce arm of it was enforced. An identity combined with a deadline_key, a
  dependency edge or a DAG node is refused now -- the DAG case because a node
  whose identity already existed had ``execute()`` rewrite a PRE-EXISTING job's
  ``dag_id``, stealing a live job out of the DAG it belonged to.

* **Validation of caller-chosen keys**, which had one bound (partition_key)
  and no emptiness check at all.

Tests that belong to a subsystem with a real home live there instead:
streams in ``test_dxe_streams.py``, the append fence in ``test_dxe_faults.py``,
the fork FK race in ``test_job_fork.py``, backfill ordering in
``test_scheduler_correctness.py``, the typed enqueue in ``test_registry.py``,
the doctor check in ``test_cli_doctor.py``.
"""

from __future__ import annotations

import datetime

import asyncpg
import pytest

from pyjobby import db
from pyjobby.client import MAX_KEY_LENGTH, JobClient
from pyjobby.dag import DAGBuilder
from pyjobby.monitor import (
    RETRY_TIMED_OUT_SQL,
    SWEEP_DEAD_WORKER_JOBS_SQL,
    SWEEP_STUCK_CLAIMS_SQL,
    WAKE_WAITERS_SQL,
    sweep_stranded_waiters,
)
from pyjobby.pj import STMTS

pytestmark = pytest.mark.asyncio

OK = "tests.dxe_jobs.OkJob"
CLAIM_HOST = "audit-regression-host"


# ===========================================================================
# helpers
# ===========================================================================


async def claim_once(conn, queue: str):
    """Claim through the REAL claim statement (bumps run_epoch, run_count)."""
    return await conn.fetchrow(
        STMTS["claim"], 4242, CLAIM_HOST, queue, ["test"], 1000, None, None
    )


async def row(pool, job_id: int):
    return await pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)


# ===========================================================================
# FIX-1: leaving 'queued' ends the key, and coming back must not take it back
# ===========================================================================


class TestRequeueReleasesTheDedupeKeys:
    """Every statement that returns a row to 'queued' clears the keys.

    The row is the same one in each case; what differs is which statement
    puts it back, and each of them was independently able to raise.
    """

    async def test_retrying_a_cancelled_debounced_row_does_not_violate_the_index(
        self, db_pool, unique_queue
    ):
        """The case that has no run_count protection at all.

        ``jorb_debounce_idx`` is partial on ``run_count = 0`` as well as on
        'queued', which releases the key at the first CLAIM -- but a debounced
        row cancelled while still PARKED was never claimed, so run_count is
        still 0. Retrying it carried the key straight back into the index, and
        a burst that had opened a new window in the meantime made that retry
        raise ``UniqueViolationError``: a job that could never be retried.
        """
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:parked"

        parked, created = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1
        )
        assert created
        await db.cancel_job(db_pool, parked)

        # the burst that opens a NEW window while the first row is out of it
        successor, created_again = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=2
        )
        assert created_again and successor != parked

        assert await db.retry_job(db_pool, parked) == parked

        retried = await row(db_pool, parked)
        assert retried["state"] == "queued"
        assert retried["debounce_key"] is None
        assert retried["debounce_deadline"] is None
        # the successor still holds the window; the retry took nothing from it
        assert (await row(db_pool, successor))["debounce_key"] == key

    async def test_one_such_row_no_longer_poisons_a_bulk_retry(
        self, db_pool, unique_queue
    ):
        """``retry_jobs`` is ONE statement, which is the whole point of it.

        A unique violation on any row aborts the entire UPDATE, so before the
        fix a single parked-and-cancelled debounced row meant a thousand-job
        DLQ retry requeued NOTHING -- and told the operator so with an
        asyncpg error naming an index, not a job.
        """
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:batch"

        poisoner, _ = await client.debounce(
            OK, key=key, period=30.0, queue=unique_queue, x=1
        )
        await db.cancel_job(db_pool, poisoner)
        await client.debounce(OK, key=key, period=30.0, queue=unique_queue, x=2)

        bystanders = [
            await db_pool.fetchval(
                """INSERT INTO jorb (job_class, queue, state, finished)
                   VALUES ($1, $2, 'crashed', now()) RETURNING id""",
                OK,
                unique_queue,
            )
            for _ in range(3)
        ]

        requeued = await db.retry_jobs(db_pool, [poisoner, *bystanders])

        assert sorted(requeued) == sorted([poisoner, *bystanders]), (
            "one collided row must not cost the rest of the batch its retry"
        )
        assert (await row(db_pool, poisoner))["debounce_key"] is None

    async def test_the_worker_retry_path_survives_a_re_armed_deadline_key(
        self, db_pool, unique_queue
    ):
        """``STMTS['retry']``, fired the way a worker's failure handler fires it.

        A deadline_key re-arms at the claim, so a duplicate enqueued while the
        job runs is legal and holds the key. The worker's own retry then put
        the running row back into 'queued' still carrying it -- a unique
        violation raised INSIDE the failure handler, which is the one place a
        job cannot afford another error.
        """
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:deadline"

        job_id = await client.enqueue(OK, queue=unique_queue, deadline_key=key)
        claimed = await claim_once(db_pool, unique_queue)
        assert claimed["id"] == job_id

        # legal now that the key has re-armed: the work has started
        duplicate = await client.enqueue(OK, queue=unique_queue, deadline_key=key)

        retried = await db_pool.fetchval(
            STMTS["retry"],
            job_id,
            datetime.timedelta(seconds=0),
            "boom",
            "traceback",
            claimed["run_epoch"],
        )

        assert retried == job_id
        after = await row(db_pool, job_id)
        assert after["state"] == "queued"
        assert after["deadline_key"] is None
        assert (await row(db_pool, duplicate))["deadline_key"] == key

    async def test_a_durable_sleep_survives_a_re_armed_deadline_key(
        self, db_pool, unique_queue
    ):
        """``STMTS['reschedule']`` -- the statement behind ``Job.reschedule()``
        and durable ``sleep()``.

        It parks the row back in 'queued' for the duration of the nap, so it is
        subject to the same rule as retry and was the one requeue statement that
        did not carry it. The failure is worse than retry's: a job that sleeps
        raises the unique violation from inside ``sleep()`` -- ordinary
        control flow in the middle of a durable workflow, with no failure
        handler anywhere near it.
        """
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:sleep"

        job_id = await client.enqueue(OK, queue=unique_queue, deadline_key=key)
        claimed = await claim_once(db_pool, unique_queue)
        assert claimed["id"] == job_id

        # legal: the key re-armed at the claim above
        duplicate = await client.enqueue(OK, queue=unique_queue, deadline_key=key)

        slept = await db_pool.fetchval(
            STMTS["reschedule"],
            job_id,
            datetime.timedelta(seconds=60),
            claimed["run_epoch"],
        )

        assert slept == job_id
        after = await row(db_pool, job_id)
        assert after["state"] == "queued"
        assert after["deadline_key"] is None
        assert after["debounce_key"] is None
        assert after["debounce_deadline"] is None
        assert (await row(db_pool, duplicate))["deadline_key"] == key

    @pytest.mark.parametrize(
        "sweep",
        [SWEEP_DEAD_WORKER_JOBS_SQL, SWEEP_STUCK_CLAIMS_SQL],
        ids=["dead-worker", "stuck-claim"],
    )
    async def test_a_monitor_sweep_reclaims_the_whole_batch(
        self, db_pool, unique_queue, sweep
    ):
        """The batch poisoning that permanently disabled recovery.

        Both sweeps requeue every doomed row in ONE statement. A single row
        whose deadline_key a duplicate had re-armed aborted it, so no job of
        any dead worker was reclaimed -- and because the condition is
        level-triggered, the same row aborted the sweep again on every cycle
        after that, forever.
        """
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:sweep"

        collider = await client.enqueue(OK, queue=unique_queue, deadline_key=key)
        plain = [await client.enqueue(OK, queue=unique_queue) for _ in range(3)]

        worker_id = await db_pool.fetchval(
            """INSERT INTO jorb_worker (host, pid, queue, last_seen)
               VALUES ($1, 1, $2, now() - interval '1 hour') RETURNING id""",
            CLAIM_HOST,
            unique_queue,
        )
        doomed = [collider, *plain]
        await db_pool.execute(
            """UPDATE jorb SET state = 'claimed', claimed_by = $2,
                               claimed_at = now(),
                               updated = now() - interval '1 hour'
                WHERE id = ANY($1::bigint[])""",
            doomed,
            worker_id,
        )
        # the duplicate that re-armed the key while the first job was claimed
        duplicate = await client.enqueue(OK, queue=unique_queue, deadline_key=key)

        requeued = await db_pool.fetch(sweep, datetime.timedelta(seconds=1), 100)

        assert sorted(r["id"] for r in requeued) == sorted(doomed)
        assert (await row(db_pool, collider))["deadline_key"] is None
        assert (await row(db_pool, duplicate))["deadline_key"] == key

    async def test_the_timeout_retry_releases_the_key_too(self, db_pool, unique_queue):
        """``RETRY_TIMED_OUT_SQL`` is the monitor's per-job requeue: one row at
        a time, so it strands only itself -- but it strands it permanently,
        since a job that cannot be requeued cannot be retried by anybody."""
        client = JobClient(pool=db_pool)
        key = f"{unique_queue}:timeout"

        job_id = await client.enqueue(OK, queue=unique_queue, deadline_key=key)
        await db_pool.execute("UPDATE jorb SET state = 'running' WHERE id = $1", job_id)
        await client.enqueue(OK, queue=unique_queue, deadline_key=key)

        await db_pool.execute(
            RETRY_TIMED_OUT_SQL, job_id, "timed out", datetime.timedelta(0)
        )

        after = await row(db_pool, job_id)
        assert after["state"] == "queued"
        assert after["deadline_key"] is None


class TestWakingWaitersReleasesTheDeadlineKey:
    """'waiting' is outside jorb_deadline_idx, so two waiters may legally hold
    the same key -- and both wake paths wake a whole BATCH in one UPDATE.

    The rows are INSERTed directly, and that is now the only way to make them:
    ``build_enqueue_row`` refuses ``deadline_key`` together with a dependency
    edge (``_NO_DEADLINE_WAITFOR``), because the key such a row carries never
    collapses anything -- the wake below is what drops it. The statement-level
    release therefore stays as defence for rows the client did not write: a
    direct INSERT like this one, a row from before the refusal existed, an
    operator's UPDATE. Removing it because the door is now shut would make the
    day somebody opens a second door a batch-poisoning outage.
    """

    async def _two_waiters(self, pool, queue: str) -> tuple[int, int, int]:
        client = JobClient(pool=pool)
        upstream = await client.enqueue(OK, queue=queue)
        key = f"{queue}:waiters"
        # legal at the schema level: a waiting row is not in the index, so
        # both may hold the key
        waiters = [
            await pool.fetchval(
                """INSERT INTO jorb (job_class, kwargs, queue, state,
                                     waitfor_job, deadline_key)
                   VALUES ($1, '{}', $2, 'waiting', $3, $4) RETURNING id""",
                OK,
                queue,
                upstream,
                key,
            )
            for _ in range(2)
        ]
        return upstream, waiters[0], waiters[1]

    async def test_the_workers_wake_queues_both(self, db_pool, unique_queue):
        """``STMTS['enqueue-next-self-finished']`` -- the edge-triggered wake
        the finishing worker performs. Both waiters entering the unique index
        at once violated it, so the UPDATE rolled back and NEITHER woke."""
        upstream, first, second = await self._two_waiters(db_pool, unique_queue)
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1",
            upstream,
        )

        woken = await db_pool.fetch(STMTS["enqueue-next-self-finished"], upstream)

        assert sorted(r["id"] for r in woken) == sorted([first, second])
        for waiter in (first, second):
            after = await row(db_pool, waiter)
            assert after["state"] == "queued"
            assert after["deadline_key"] is None

    async def test_the_monitors_level_triggered_sweep_queues_both(
        self, db_pool, unique_queue
    ):
        """``WAKE_WAITERS_SQL`` -- the safety net that runs when the edge was
        missed. Failing here was the worse half: level-triggered, so it failed
        again every pass and starved every other waiter in the same batch
        forever."""
        upstream, first, second = await self._two_waiters(db_pool, unique_queue)
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1",
            upstream,
        )

        assert await sweep_stranded_waiters(db_pool) == 2

        for waiter in (first, second):
            after = await row(db_pool, waiter)
            assert after["state"] == "queued"
            assert after["deadline_key"] is None

    @pytest.mark.parametrize("edge", ["waitfor_job", "waitfor_group"])
    async def test_the_client_refuses_to_write_one_in_the_first_place(
        self, db_pool, unique_queue, edge
    ):
        """The silence the wake above exposes, closed at the door.

        A ``deadline_key`` on a dependent job was accepted and did nothing at
        any point in the row's life: 'waiting' is outside jorb_deadline_idx, so
        it refuses no duplicate there, and the wake NULLs the column on the way
        into 'queued', so it refuses none afterwards either. The caller asked
        for at-most-one-pending and got every duplicate, with no error --
        exactly the shape ``_NO_DEBOUNCE_WAITFOR`` was already refusing beside
        it.
        """
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="waitfor_job/waitfor_group"):
            await client.enqueue(
                OK, queue=unique_queue, deadline_key="d", **{edge: "x"}
            )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 0
        )

    async def test_the_wake_statement_leaves_the_debounce_columns_alone(self):
        """A waiting row can never hold a debounce_key (a debounced enqueue
        with waitfor_* is refused at the door), so the wake clears the ONE
        column it can, and the statement says which."""
        assert "deadline_key = NULL" in WAKE_WAITERS_SQL
        assert "debounce_key" not in WAKE_WAITERS_SQL


# ===========================================================================
# FIX-5: the keys' mutual exclusion, completed
# ===========================================================================


class TestIdentityRefusesWhatItCannotHonour:
    # "identity_key + deadline_key is refused" is asserted in
    # test_job_identity.py, which also proves NOTHING WAS WRITTEN and states
    # the contrast between the two promises -- a strict superset of what the
    # test that used to sit here checked. Two tests of one refusal is two
    # places to update and one of them will be missed.

    @pytest.mark.parametrize("edge", ["waitfor_job", "waitfor_group"])
    async def test_identity_with_a_dependency_edge_is_refused(
        self, db_pool, unique_queue, edge
    ):
        """An identified enqueue may return a job it did not create, whose
        dependency is whatever the enqueue that really made it asked for --
        so this edge would silently not be applied."""
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="waitfor_job/waitfor_group"):
            await client.enqueue(OK, queue=unique_queue, identity_key="i", **{edge: 1})

    async def test_a_dag_node_cannot_carry_an_identity(self):
        """Refused at ``add()``, before any DAG exists to execute."""
        dag = DAGBuilder(name="identified")
        with pytest.raises(ValueError, match="not a DAG node option"):
            dag.add(OK, {}, identity_key="i")

    async def test_a_dag_with_a_pre_existing_identity_raises_and_steals_nothing(
        self, db_pool, unique_queue
    ):
        """The empirical failure: the node's identity already existed, the
        enqueue handed back somebody else's job, and ``execute()``'s
        ``UPDATE jorb SET dag_id = ...`` rewired that live job into this DAG.

        Built past ``add()``'s check (the option is written straight onto the
        node) so that ``validate()`` -- the gate ``execute()`` really runs
        behind -- is the thing under test.
        """
        client = JobClient(pool=db_pool)
        identity = f"identity:{unique_queue}:stolen"
        victim = await client.enqueue(OK, queue=unique_queue, identity_key=identity)
        owner = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id", "the-real-owner"
        )
        await db_pool.execute(
            "UPDATE jorb SET dag_id = $2 WHERE id = $1", victim, owner
        )

        dag = DAGBuilder(name="thief")
        node = dag.add(OK, {}, queue=unique_queue)
        node._job_options["identity_key"] = identity

        with pytest.raises(ValueError, match="not a DAG node option"):
            await dag.execute(client)

        assert (await row(db_pool, victim))["dag_id"] == owner


class TestTheOutboxPathRefusesDebounce:
    async def test_debounce_key_is_refused_before_the_insert(
        self, db_connection, unique_queue
    ):
        """This path runs the plain INSERT: a key already held would abort the
        CALLER's transaction on jorb_debounce_idx instead of collapsing, and a
        key not yet held would open a window with no cap to clamp it."""
        with pytest.raises(ValueError, match="is not an enqueue"):
            await JobClient.enqueue_in_transaction(
                db_connection, OK, queue=unique_queue, debounce_key="k"
            )

    async def test_nothing_was_written(self, db_connection, unique_queue):
        before = await db_connection.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
        )
        with pytest.raises(ValueError):
            await JobClient.enqueue_in_transaction(
                db_connection, OK, queue=unique_queue, debounce_key="k"
            )
        assert (
            await db_connection.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == before
        )


# ===========================================================================
# FIX-6: every caller-chosen key is a NAME -- non-empty and bounded
# ===========================================================================


KEY_OPTIONS = ("deadline_key", "identity_key", "debounce_key", "partition_key")


class TestKeyValidation:
    """Asserted against ``build_enqueue_row`` for all four keys, because that
    IS the check's home: every writer in the platform -- enqueue, the batch,
    the outbox, debounce and the scheduler -- assembles its row there, so a
    key refused here is refused everywhere. The public verbs are then spot-
    checked below to prove the wiring."""

    @pytest.mark.parametrize("option", KEY_OPTIONS)
    @pytest.mark.parametrize("empty", ["", "   "], ids=["empty", "whitespace"])
    async def test_an_empty_key_is_refused(self, unique_queue, option, empty):
        """'' is a real value, not the absence of one: it takes a slot in that
        column's index, so every other caller who passed an empty key would
        collide with this job."""
        with pytest.raises(ValueError, match=f"{option} is empty"):
            JobClient.build_enqueue_row(OK, queue=unique_queue, **{option: empty})

    @pytest.mark.parametrize("option", KEY_OPTIONS)
    async def test_an_oversized_key_is_refused(self, unique_queue, option):
        # derived from the constant, never a literal 257: a bound written twice
        # is a test that keeps passing against the number it was written for
        # while the platform accepts a different one
        over = MAX_KEY_LENGTH + 1
        with pytest.raises(ValueError, match=f"{option} is {over} characters"):
            JobClient.build_enqueue_row(OK, queue=unique_queue, **{option: "k" * over})

    @pytest.mark.parametrize("option", KEY_OPTIONS)
    async def test_the_bound_itself_is_accepted(self, unique_queue, option):
        """The bound is the limit, not the first refusal: an off-by-one here
        would reject a key the documentation promises."""
        assert JobClient.build_enqueue_row(
            OK, queue=unique_queue, **{option: "k" * MAX_KEY_LENGTH}
        )

    async def test_the_enqueue_verb_refuses_before_writing(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="deadline_key is empty"):
            await client.enqueue(OK, queue=unique_queue, deadline_key="")
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 0
        )

    async def test_the_batch_path_is_validated_too(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="partition_key is 257 characters"):
            await client.enqueue_batch(
                [(OK, {}, {"partition_key": "k" * 257})], queue=unique_queue
            )

    async def test_debounce_validates_its_own_key(self, db_pool, unique_queue):
        """The one verb whose key is a named argument rather than an option,
        and which reaches the column by a different statement."""
        client = JobClient(pool=db_pool)
        with pytest.raises(ValueError, match="debounce_key is empty"):
            await client.debounce(OK, key="", period=1.0, queue=unique_queue)


# ===========================================================================
# FIX-10: the admin API's app_version twin writes what the validator returned
# ===========================================================================


async def test_update_job_app_version_writes_the_validated_value(
    db_pool, db_connection, unique_queue
):
    """Parity with ``client.update_job_app_version``: the validator's RETURN
    value is what reaches the UPDATE, so the two surfaces cannot drift the day
    it normalises anything."""
    from pyjobby.admin_api import AdminAPI

    client = JobClient(pool=db_pool)
    job_id = await client.enqueue(OK, queue=unique_queue)
    api = AdminAPI(db_connection)

    assert await api.update_job_app_version(job_id, "2026.07.29") is True
    # read back on the API's OWN connection: db_connection holds an open
    # transaction for test isolation, so the pool cannot see the write yet
    assert (await row(db_connection, job_id))["app_version"] == "2026.07.29"

    with pytest.raises(ValueError):
        await api.update_job_app_version(job_id, "")
    assert (await row(db_connection, job_id))["app_version"] == "2026.07.29"


# ===========================================================================
# structural: the rule is one rule, spelled once
# ===========================================================================
#
# "every statement that returns a job to 'queued' carries the release" is
# asserted in test_lifecycle.py, which already PARSES ``STMTS`` for the
# transitions it declares and can therefore derive the set instead of listing
# it. The negative control for the whole family stays here, beside the
# failures it controls for.


async def test_asyncpg_still_reports_the_violation_this_batch_removed(
    db_pool, unique_queue
):
    """The negative control for the whole family.

    Every test above asserts a statement no longer raises. If the index that
    made them raise had simply been dropped, they would all pass for the wrong
    reason -- so this proves the constraint is still there and still bites a
    genuine duplicate.
    """
    client = JobClient(pool=db_pool)
    key = f"{unique_queue}:control"
    await client.enqueue(OK, queue=unique_queue, deadline_key=key)
    with pytest.raises(asyncpg.UniqueViolationError):
        await client.enqueue(OK, queue=unique_queue, deadline_key=key)


# ===========================================================================
# polish: the once-a-minute gate must not swallow a report that never happened
# ===========================================================================


async def test_hidden_work_is_only_marked_reported_once_both_queries_answered(
    prepared_worker, unique_queue, db_pool
):
    """``_report_hidden_work`` stamps its rate-limit AFTER its two statements.

    Stamped up front, a connection that dropped between the two suppressed the
    whole report for a minute -- and the reconnect restarts the timer, so a
    worker with a flapping connection could report the invisible work it is
    sitting on approximately never. This is the ONLY surface that names the
    condition from inside the fleet (a job above every live ceiling, or pinned
    to a version nobody advertises, never fails and never reaches the DLQ), and
    re-running it costs nothing: the caller only gets here when the worker
    found nothing claimable.
    """
    system = prepared_worker
    await db_pool.execute(
        """INSERT INTO jorb (job_class, queue, state, prio)
           VALUES ($1, $2, 'queued', 9000)""",
        OK,
        unique_queue,
    )
    real_ex = system.ex

    async def drops_on_the_second_statement(op: str, *args):
        if op == "hidden-versions":
            raise ConnectionResetError("the worker's connection went away")
        return await real_ex(op, *args)

    system.ex = drops_on_the_second_statement
    with pytest.raises(ConnectionResetError):
        await system._report_hidden_work()

    assert system._hidden_reported == 0.0, (
        "a report that did not complete must not consume the minute's budget"
    )

    system.ex = real_ex
    await system._report_hidden_work()
    assert system._hidden_reported > 0.0

    # ...and the gate still works once a report really did happen
    stamped = system._hidden_reported
    await system._report_hidden_work()
    assert system._hidden_reported == stamped
