"""Fork: a NEW job from an existing job's checkpoint prefix.

The platform has three verbs for "run this again" and only one of them makes
a second row:

* ``retry`` and ``rerun`` requeue the SAME row — same id, same history, same
  identity — which is why ``tests/test_dxe_faults.py`` proves resumption
  against one job id;
* ``fork`` inserts a NEW row that re-executes the source's work from step N,
  with steps 1..N-1 copied in as its own checkpoints, and touches the source
  not at all.

Everything here is about that difference, and the claims that matter are
proved from OUTSIDE the checkpoint table: the ``jorb_test_effect`` ledger
counts what really executed, per job, so "the prefix fast-forwarded" is a
count of side effects and not a reading of the rows the fork itself wrote.

The refusals are pinned by message, not just by type. A fork is an operator
action taken during an incident; "invalid from_step" is not an answer, "job
1234 recorded 3 step(s)" is.
"""

from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import pytest
from click.testing import CliRunner

from pyjobby import db
from pyjobby.admin_api import AdminAPI
from pyjobby.cli import cli
from pyjobby.client import DEFAULT_PRIO_CEILING
from pyjobby.pj import Job

from .conftest import wait_for_job_state
from .utils.faults import (
    effect_counts_per_job,
    ensure_effects_table,
    record_effect,
    wait_until_blocked_on_a_transaction,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# job classes (resolved by the worker through their dotted path)
# ============================================================================


class ForkLedgerJob(Job):
    """Three checkpointed steps, each recording its own real execution.

    Every step's OUTPUT carries the id of the job that produced it, so a
    fast-forwarded step is visible twice over: the ledger has no row for it
    under the fork's id, and the value the fork received names the source.
    """

    async def task(self, tag: str) -> dict[str, Any]:
        one = await self.step("one", self._mark, tag, "one")
        two = await self.step("two", self._mark, tag, "two")
        three = await self.step("three", self._mark, tag, "three")
        return {"one": one, "two": two, "three": three}

    async def _mark(self, tag: str, label: str) -> dict[str, Any]:
        await record_effect(self.s.cxn, tag, self.job["id"], label)
        return {"by": self.job["id"]}


class ForkStreamJob(Job):
    """Streams, checkpoints, then streams again — a prefix worth forking.

    Step sequence: 1 the 'first' stream write, 2 a plain step, 3 the 'second'
    stream write, 4 the close. A fork from step 2 copies only the first
    write's checkpoint, which fast-forwards WITHOUT appending anything to
    the fork's own stream.
    """

    async def task(self, tag: str, key: str = "rows") -> dict[str, Any]:
        await self.stream_write(key, {"phase": "first", "by": self.job["id"]})
        await self.step("mid", self._mark, tag)
        await self.stream_write(key, {"phase": "second", "by": self.job["id"]})
        await self.stream_close(key)
        return {"streamed": 2}

    async def _mark(self, tag: str) -> dict[str, Any]:
        await record_effect(self.s.cxn, tag, self.job["id"], "mid")
        return {"by": self.job["id"]}


# ============================================================================
# helpers
# ============================================================================


@pytest.fixture
def dsn(db_params: dict) -> str:
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
    )


async def run_cli(*args: str):
    """Invoke pj-admin in a worker thread (the CLI owns its own event loop)."""
    return await asyncio.to_thread(lambda: CliRunner().invoke(cli, list(args)))


async def make_source(pool: asyncpg.Pool, queue: str, **cols: Any) -> int:
    """A job row with whatever columns a test needs to watch survive a fork."""
    columns = {
        "job_class": "tests.dxe_jobs.OkJob",
        "queue": queue,
        "state": "finished",
        **cols,
    }
    names = ", ".join(columns)
    params = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    return await pool.fetchval(
        f"INSERT INTO jorb ({names}) VALUES ({params}) RETURNING id",
        *columns.values(),
    )


async def record_steps(
    pool: asyncpg.Pool, job_id: int, count: int, *, epoch: int = 3, error_at: int = 0
) -> None:
    """`count` checkpoints on `job_id`, optionally with one recorded failure."""
    for seq in range(1, count + 1):
        await pool.execute(
            """INSERT INTO jorb_step
                   (job_id, step_seq, name, output, error, run_epoch, finished)
               VALUES ($1, $2, $3, $4, $5, $6, now())""",
            job_id,
            seq,
            f"step-{seq}",
            {"seq": seq},
            "RuntimeError: boom" if seq == error_at else None,
            epoch,
        )


async def steps_of(pool: asyncpg.Pool, job_id: int) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in await pool.fetch(
            """SELECT step_seq, name, output, error, run_epoch FROM jorb_step
                WHERE job_id = $1 ORDER BY step_seq""",
            job_id,
        )
    ]


async def stream_rows(pool: asyncpg.Pool, job_id: int, key: str = "rows") -> list[dict]:
    return [
        dict(r)
        for r in await pool.fetch(
            """SELECT seq, value, closed FROM jorb_stream
                WHERE job_id = $1 AND key = $2 ORDER BY seq""",
            job_id,
            key,
        )
    ]


async def history_of(pool: asyncpg.Pool, job_id: int) -> list[asyncpg.Record]:
    return list(
        await pool.fetch(
            "SELECT event, detail FROM jorb_history WHERE job_id = $1 ORDER BY id",
            job_id,
        )
    )


# ============================================================================
# the row a fork makes
# ============================================================================


class TestTheForkedRow:
    """What is copied, what is deliberately not, and what is new."""

    async def test_the_prefix_is_copied_and_the_rest_is_not(
        self, db_pool, unique_queue
    ):
        """from_step=4 copies steps 1-3 and leaves 4-5 behind."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 5)

        fork = await db.fork_job(db_pool, source, from_step=4)

        assert fork["job_id"] != source
        assert fork["steps_copied"] == 3
        copied = await steps_of(db_pool, fork["job_id"])
        assert [s["step_seq"] for s in copied] == [1, 2, 3]
        assert [s["name"] for s in copied] == ["step-1", "step-2", "step-3"]
        assert [s["output"] for s in copied] == [{"seq": 1}, {"seq": 2}, {"seq": 3}]

    async def test_copied_checkpoints_predate_the_forks_first_attempt(
        self, db_pool, unique_queue
    ):
        """Epoch 0 on every copied row: no attempt of the FORK wrote them,
        and the first claim will bump the row to 1."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3, epoch=7)

        fork = await db.fork_job(db_pool, source, from_step=3)

        assert [s["run_epoch"] for s in await steps_of(db_pool, fork["job_id"])] == [
            0,
            0,
        ]
        assert (
            await db_pool.fetchval(
                "SELECT run_epoch FROM jorb WHERE id = $1", fork["job_id"]
            )
            == 0
        )

    async def test_from_step_1_copies_nothing(self, db_pool, unique_queue):
        """The default: a fresh run of the whole job under a new id."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        fork = await db.fork_job(db_pool, source)

        assert fork["from_step"] == 1
        assert fork["steps_copied"] == 0
        assert await steps_of(db_pool, fork["job_id"]) == []

    async def test_the_work_is_inherited_and_the_identity_is_not(
        self, db_pool, unique_queue
    ):
        """The split that makes a fork a fork.

        What describes the WORK comes across (class, arguments, routing,
        capability, tags, retry/timeout policy). What identifies the job, or
        wires it into somebody else's structure, does not — two live rows
        sharing a deadline_key would make idempotent enqueue mean nothing,
        an identity_key promises there is only one row holding it, and a
        fork is nobody's DAG member.
        """
        upstream = await make_source(db_pool, unique_queue)
        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id", unique_queue
        )
        source = await make_source(
            db_pool,
            unique_queue,
            dag_id=dag_id,
            job_class="tests.dxe_jobs.StepPipelineJob",
            kwargs={"n": 7},
            prio=42,
            capability="gpu",
            tags={"customer": "acme"},
            admin_data={"max_retries": 2, "timeout_seconds": 30},
            uid=99,
            deadline_key="payment:1",
            identity_key=f"identity:{unique_queue}",
            schedule_id=1234,
            run_group=5,
            waitfor_job=upstream,
            state="waiting",
        )

        fork = await db.fork_job(db_pool, source)

        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", fork["job_id"])
        assert row["job_class"] == "tests.dxe_jobs.StepPipelineJob"
        assert row["kwargs"] == {"n": 7}
        assert row["queue"] == unique_queue
        assert row["prio"] == 42
        assert row["capability"] == "gpu"
        assert row["tags"] == {"customer": "acme"}
        assert row["admin_data"]["max_retries"] == 2
        assert row["admin_data"]["timeout_seconds"] == 30
        assert row["state"] == "queued"
        # uid is a tenant LABEL like tags, so a fork stays attributed to its
        # tenant; deadline_key, identity_key and schedule_id are identity,
        # never inherited. identity_key is the load-bearing one: its index is
        # unique across EVERY state, so a fork that inherited it could not
        # have been inserted at all -- FORK_JOB_SQL lists its columns, and
        # this asserts identity_key is still not among them.
        assert row["uid"] == 99
        assert (row["deadline_key"], row["identity_key"], row["schedule_id"]) == (
            None,
            None,
            None,
        )
        assert (row["dag_id"], row["run_group"], row["waitfor_job"]) == (
            None,
            None,
            None,
        )
        assert (row["run_count"], row["error_count"], row["run_epoch"]) == (0, 0, 0)
        assert row["result"] is None and row["error_message"] is None

    async def test_the_overrides_apply(self, db_pool, unique_queue):
        """Queue, priority and arguments are the three knobs a fork exists to
        change: the row-level facts a retry cannot touch."""
        source = await make_source(db_pool, unique_queue, kwargs={"n": 1}, prio=100)

        fork = await db.fork_job(
            db_pool,
            source,
            queue=f"{unique_queue}_alt",
            priority=7,
            kwargs_override={"n": 2, "verbose": True},
        )

        row = await db_pool.fetchrow(
            "SELECT queue, prio, kwargs FROM jorb WHERE id = $1", fork["job_id"]
        )
        assert row["queue"] == f"{unique_queue}_alt"
        assert row["prio"] == 7
        assert row["kwargs"] == {"n": 2, "verbose": True}
        assert (fork["queue"], fork["priority"]) == (f"{unique_queue}_alt", 7)

    async def test_lineage_is_recorded_on_the_row_and_in_the_history(
        self, db_pool, unique_queue
    ):
        """Both, because they answer at different times: the column is the
        live edge (and dies with the source), the history row is the audit
        (and does not)."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        fork = await db.fork_job(db_pool, source, from_step=3)

        row = await db_pool.fetchrow(
            "SELECT forked_from, admin_data FROM jorb WHERE id = $1", fork["job_id"]
        )
        assert row["forked_from"] == source
        assert row["admin_data"]["fork"] == {"from_step": 3, "steps_copied": 2}

        history = await history_of(db_pool, fork["job_id"])
        assert [h["event"] for h in history] == ["enqueued"]
        assert history[0]["detail"]["forked_from"] == source
        assert history[0]["detail"]["from_step"] == 3
        assert history[0]["detail"]["steps_copied"] == 2

    async def test_a_fork_of_a_fork_records_its_own_origin(self, db_pool, unique_queue):
        """The fork block describes THIS row's creation, so the one inherited
        with admin_data must be replaced rather than carried along."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        first = await db.fork_job(db_pool, source, from_step=3)
        second = await db.fork_job(db_pool, first["job_id"], from_step=2)

        row = await db_pool.fetchrow(
            "SELECT forked_from, admin_data FROM jorb WHERE id = $1",
            second["job_id"],
        )
        assert row["forked_from"] == first["job_id"]
        assert row["admin_data"]["fork"] == {"from_step": 2, "steps_copied": 1}

    async def test_the_source_is_untouched(self, db_pool, unique_queue):
        """A fork is not a write to the source. Any state, including one an
        execution owns right now."""
        source = await make_source(
            db_pool,
            unique_queue,
            state="running",
            run_epoch=4,
            run_count=1,
            result={"done": True},
        )
        await record_steps(db_pool, source, 3, epoch=4)
        before = dict(
            await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", source)
        )
        steps_before = await steps_of(db_pool, source)
        history_before = await history_of(db_pool, source)

        await db.fork_job(db_pool, source, from_step=2)

        after = dict(await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", source))
        assert after == before
        assert await steps_of(db_pool, source) == steps_before
        assert len(await history_of(db_pool, source)) == len(history_before)
        assert [s["run_epoch"] for s in steps_before] == [4, 4, 4]

    async def test_the_fork_outlives_a_reaped_source(self, db_pool, unique_queue):
        """Retention deletes terminal jobs on its own schedule, and the source
        is usually the older row. ON DELETE SET NULL: the fork survives with
        its checkpoints, and its history keeps the id the column just lost."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)
        fork = await db.fork_job(db_pool, source, from_step=3)

        await db_pool.execute("DELETE FROM jorb WHERE id = $1", source)

        row = await db_pool.fetchrow(
            "SELECT forked_from, admin_data FROM jorb WHERE id = $1", fork["job_id"]
        )
        assert row is not None, "the fork was deleted with its source"
        assert row["forked_from"] is None
        assert row["admin_data"]["fork"] == {"from_step": 3, "steps_copied": 2}
        assert len(await steps_of(db_pool, fork["job_id"])) == 2
        history = await history_of(db_pool, fork["job_id"])
        assert history[0]["detail"]["forked_from"] == source


class TestForkRefusals:
    """Every refusal names the number that makes it actionable."""

    async def test_it_is_exported_where_a_caller_can_catch_it(self):
        """A client calls fork_job and has to handle the refusal, so the
        exception belongs beside the other client-facing ones."""
        import pyjobby

        assert pyjobby.ForkRefused is db.ForkRefused

    async def test_no_such_job(self, db_pool):
        with pytest.raises(db.ForkRefused) as refused:
            await db.fork_job(db_pool, 999_999_999)

        assert str(refused.value) == (
            "job 999999999 not found, so there is nothing to fork"
        )

    async def test_from_step_below_one(self, db_pool, unique_queue):
        source = await make_source(db_pool, unique_queue)

        with pytest.raises(db.ForkRefused) as refused:
            await db.fork_job(db_pool, source, from_step=0)

        assert str(refused.value) == (
            "from_step must be at least 1 (steps are numbered from 1); got 0"
        )

    async def test_from_step_past_the_recorded_steps(self, db_pool, unique_queue):
        """One past the last step is legal — that is "run the next one".
        Two past is a typo, and the refusal says how many there are."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        assert (await db.fork_job(db_pool, source, from_step=4))["steps_copied"] == 3

        with pytest.raises(db.ForkRefused) as refused:
            await db.fork_job(db_pool, source, from_step=5)

        assert str(refused.value) == (
            f"job {source} recorded 3 step(s), so a fork may start at step 4 "
            f"at the latest; got 5"
        )

    async def test_a_refused_fork_writes_nothing(self, db_pool, unique_queue):
        """The guard is inside the statement, so a refusal cannot leave a
        half-made job behind."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 2)
        before = await db_pool.fetchval("SELECT count(*) FROM jorb")

        with pytest.raises(db.ForkRefused):
            await db.fork_job(db_pool, source, from_step=9)

        assert await db_pool.fetchval("SELECT count(*) FROM jorb") == before

    async def test_from_failure_with_no_failure(self, db_pool, unique_queue):
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        with pytest.raises(db.ForkRefused) as refused:
            await db.fork_job_from_failure(db_pool, source)

        assert str(refused.value) == (
            f"job {source} has no failed step recorded, so there is no failure "
            f"to fork from — `pj-admin jobs steps {source}` shows what ran"
        )

    async def test_from_failure_on_a_missing_job(self, db_pool):
        with pytest.raises(db.ForkRefused) as refused:
            await db.fork_job_from_failure(db_pool, 999_999_999)

        assert str(refused.value) == (
            "job 999999999 not found, so there is nothing to fork"
        )


class TestForkFromFailure:
    async def test_it_starts_at_the_first_recorded_error(self, db_pool, unique_queue):
        """The FIRST failure, not the last step: everything before it
        succeeded and must not run again."""
        source = await make_source(db_pool, unique_queue, state="crashed")
        await record_steps(db_pool, source, 4, error_at=3)

        fork = await db.fork_job_from_failure(db_pool, source)

        assert fork["from_step"] == 3
        assert fork["steps_copied"] == 2
        assert [s["step_seq"] for s in await steps_of(db_pool, fork["job_id"])] == [
            1,
            2,
        ]

    async def test_a_step_that_failed_and_then_succeeded_is_not_a_failure(
        self, db_pool, unique_queue
    ):
        """A recorded error is the step's OUTCOME, not its history: step 2
        failed and a retry succeeded, so the failure to fork from is step 3 —
        the one still standing."""
        source = await make_source(db_pool, unique_queue, state="crashed")
        await record_steps(db_pool, source, 3, error_at=2)
        await db_pool.execute(  # the retry that got past step 2
            "UPDATE jorb_step SET error = NULL WHERE job_id = $1 AND step_seq = 2",
            source,
        )
        await db_pool.execute(
            "UPDATE jorb_step SET error = 'RuntimeError: boom' "
            "WHERE job_id = $1 AND step_seq = 3",
            source,
        )

        fork = await db.fork_job_from_failure(db_pool, source)

        assert fork["from_step"] == 3


# ============================================================================
# what a fork actually executes
# ============================================================================


class TestForkExecution:
    """Live workers, and the ledger rather than the checkpoint table.

    "The prefix fast-forwarded" is only worth asserting from outside: a
    checkpoint row proves what was recorded, and the whole question is
    whether the code ran.
    """

    async def test_a_fork_from_the_last_step_runs_only_that_step(
        self, db_pool, job_client, live_worker, unique_queue
    ):
        await ensure_effects_table(db_pool)
        await live_worker()
        source = await job_client.enqueue(
            "tests.test_job_fork.ForkLedgerJob", queue=unique_queue, tag=unique_queue
        )
        await wait_for_job_state(db_pool, source, ("finished",))

        fork = await db.fork_job(db_pool, source, from_step=3)
        await wait_for_job_state(db_pool, fork["job_id"], ("finished",))

        # the ledger: the source ran all three, the fork ran only 'three'
        assert await effect_counts_per_job(db_pool, unique_queue) == {
            (source, "one"): 1,
            (source, "two"): 1,
            (source, "three"): 1,
            (fork["job_id"], "three"): 1,
        }
        # and the fast-forwarded values are the SOURCE's, handed back whole
        result = await db_pool.fetchval(
            "SELECT result FROM jorb WHERE id = $1", fork["job_id"]
        )
        assert result["one"] == {"by": source}
        assert result["two"] == {"by": source}
        assert result["three"] == {"by": fork["job_id"]}

    async def test_the_forks_own_step_table_shows_what_it_ran(
        self, db_pool, job_client, live_worker, unique_queue
    ):
        """Copied rows keep epoch 0; the steps this job really executed carry
        the epoch of the attempt that ran them."""
        await ensure_effects_table(db_pool)
        await live_worker()
        source = await job_client.enqueue(
            "tests.test_job_fork.ForkLedgerJob", queue=unique_queue, tag=unique_queue
        )
        await wait_for_job_state(db_pool, source, ("finished",))

        fork = await db.fork_job(db_pool, source, from_step=3)
        await wait_for_job_state(db_pool, fork["job_id"], ("finished",))

        steps = await steps_of(db_pool, fork["job_id"])
        assert [s["name"] for s in steps] == ["one", "two", "three"]
        assert [s["run_epoch"] for s in steps[:2]] == [0, 0]
        assert steps[2]["run_epoch"] >= 1

    async def test_a_fork_from_step_one_runs_everything(
        self, db_pool, job_client, live_worker, unique_queue
    ):
        await ensure_effects_table(db_pool)
        await live_worker()
        source = await job_client.enqueue(
            "tests.test_job_fork.ForkLedgerJob", queue=unique_queue, tag=unique_queue
        )
        await wait_for_job_state(db_pool, source, ("finished",))

        fork = await db.fork_job(db_pool, source, from_step=1)
        await wait_for_job_state(db_pool, fork["job_id"], ("finished",))

        assert await effect_counts_per_job(db_pool, unique_queue) == {
            (source, "one"): 1,
            (source, "two"): 1,
            (source, "three"): 1,
            (fork["job_id"], "one"): 1,
            (fork["job_id"], "two"): 1,
            (fork["job_id"], "three"): 1,
        }

    async def test_forking_a_crashed_job_from_its_failure_finishes_it(
        self, db_pool, job_client, live_worker, unique_queue
    ):
        """The incident, end to end: the job crashes at 'gate', the fix is
        deployed, the fork starts AT the failure — and 'prepare' does not
        run a second time while the original stays crashed."""
        await ensure_effects_table(db_pool)
        await live_worker()
        source = await job_client.enqueue(
            "tests.dxe_jobs.GatedStepJob",
            queue=unique_queue,
            tag=unique_queue,
            max_retries=0,
        )
        await wait_for_job_state(db_pool, source, ("crashed",))

        await record_effect(db_pool, unique_queue, source, "fixed")  # the "deploy"
        fork = await db.fork_job_from_failure(db_pool, source)
        await wait_for_job_state(db_pool, fork["job_id"], ("finished",))

        assert fork["from_step"] == 2
        assert fork["steps_copied"] == 1
        counts = await effect_counts_per_job(db_pool, unique_queue)
        assert counts[(source, "prepare")] == 1
        assert (fork["job_id"], "prepare") not in counts
        assert counts[(fork["job_id"], "gate")] == 1
        assert (
            await db_pool.fetchval("SELECT state FROM jorb WHERE id = $1", source)
            == "crashed"
        )


class TestForkAndStreams:
    """A stream is the SOURCE's output, and a fork does not inherit output.

    The consequence has to be stated rather than discovered: a copied
    ``dxe.stream:*`` checkpoint fast-forwards, which by definition appends
    nothing — so the fork's stream holds only what the steps it really ran
    produced, starting at position 0 of its own dense sequence.
    """

    async def test_the_fork_streams_only_what_it_reran(
        self, db_pool, job_client, live_worker, unique_queue
    ):
        await ensure_effects_table(db_pool)
        await live_worker()
        source = await job_client.enqueue(
            "tests.test_job_fork.ForkStreamJob", queue=unique_queue, tag=unique_queue
        )
        await wait_for_job_state(db_pool, source, ("finished",))
        assert len(await stream_rows(db_pool, source)) == 3  # two values + the close

        fork = await db.fork_job(db_pool, source, from_step=2)
        await wait_for_job_state(db_pool, fork["job_id"], ("finished",))

        rows = await stream_rows(db_pool, fork["job_id"])
        assert [r["value"] for r in rows] == [
            {"phase": "second", "by": fork["job_id"]},
            None,
        ]
        assert [r["seq"] for r in rows] == [0, 1]
        assert [r["closed"] for r in rows] == [False, True]
        # the source's stream is exactly as it was
        assert [r["value"] for r in await stream_rows(db_pool, source)] == [
            {"phase": "first", "by": source},
            {"phase": "second", "by": source},
            None,
        ]

    async def test_events_and_mail_are_not_copied(self, db_pool, unique_queue):
        """The other two per-job outputs, pinned by the same rule."""
        source = await make_source(db_pool, unique_queue)
        await db_pool.execute(
            "INSERT INTO jorb_event (job_id, key, value) VALUES ($1, 'phase', '1')",
            source,
        )
        await db_pool.execute(
            "INSERT INTO jorb_mailbox (dest_job_id, message) VALUES ($1, '{}')",
            source,
        )

        fork = await db.fork_job(db_pool, source)

        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_event WHERE job_id = $1", fork["job_id"]
            )
            == 0
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id = $1",
                fork["job_id"],
            )
            == 0
        )


# ============================================================================
# the surfaces
# ============================================================================


class TestTheSurfacesAgree:
    """Client and admin API are the same core; neither may be more
    permissive than the other."""

    async def test_the_client_forks(self, db_pool, job_client, unique_queue):
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 2)

        fork = await job_client.fork_job(source, from_step=2)

        assert fork["steps_copied"] == 1
        assert (
            await db_pool.fetchval(
                "SELECT forked_from FROM jorb WHERE id = $1", fork["job_id"]
            )
            == source
        )

    async def test_the_client_refuses_what_the_core_refuses(
        self, db_pool, job_client, unique_queue
    ):
        source = await make_source(db_pool, unique_queue)

        with pytest.raises(db.ForkRefused):
            await job_client.fork_job(source, from_step=3)

    async def test_a_priority_no_worker_could_claim_is_refused(
        self, db_pool, job_client, unique_queue
    ):
        """The same guard enqueue and set-priority make: a fork above every
        live worker's ceiling is never claimed, never fails, and never shows
        up anywhere — and it is refused before the row exists."""
        source = await make_source(db_pool, unique_queue)
        before = await db_pool.fetchval("SELECT count(*) FROM jorb")

        with pytest.raises(ValueError, match="ceiling"):
            await job_client.fork_job(source, priority=DEFAULT_PRIO_CEILING + 1)

        assert await db_pool.fetchval("SELECT count(*) FROM jorb") == before

    async def test_the_admin_api_makes_the_same_refusal(
        self, db_connection, db_pool, unique_queue
    ):
        api = AdminAPI(db_connection)
        source = await make_source(db_pool, unique_queue)

        with pytest.raises(ValueError, match="ceiling"):
            await api.fork_job_from_failure(source, priority=DEFAULT_PRIO_CEILING + 1)

    async def test_the_admin_api_forks_and_lists_lineage(
        self, db_connection, db_pool, unique_queue
    ):
        api = AdminAPI(db_connection)
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 2)

        first = await api.fork_job(source, from_step=2)
        second = await api.fork_job(source)

        assert await api.list_forks(source) == [first["job_id"], second["job_id"]]
        assert await api.list_forks(first["job_id"]) == []
        assert (await api.get_job(first["job_id"]))["forked_from"] == source

    async def test_the_admin_api_forks_from_failure(
        self, db_connection, db_pool, unique_queue
    ):
        api = AdminAPI(db_connection)
        source = await make_source(db_pool, unique_queue, state="crashed")
        await record_steps(db_pool, source, 3, error_at=2)

        fork = await api.fork_job_from_failure(source, queue=f"{unique_queue}_alt")

        assert fork["from_step"] == 2
        assert fork["queue"] == f"{unique_queue}_alt"


class TestTheCliRendersLineage:
    """`pj-admin` is where an operator meets a fork, so the words matter."""

    async def test_fork_prints_the_new_id_and_what_it_copied(
        self, dsn, db_pool, unique_queue
    ):
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        result = await run_cli(
            "--dsn", dsn, "jobs", "fork", str(source), "--from-step", "3"
        )

        assert result.exit_code == 0, result.output
        forked = await db_pool.fetchval(
            "SELECT id FROM jorb WHERE forked_from = $1", source
        )
        assert f"Job {forked} forked from job {source}" in result.output
        assert "starts at step 3 (2 checkpoint(s) copied, fast-forwarded)" in (
            result.output
        )
        assert f"queue {unique_queue}  priority 100" in result.output

    async def test_fork_says_when_it_copied_nothing(self, dsn, db_pool, unique_queue):
        source = await make_source(db_pool, unique_queue)

        result = await run_cli("--dsn", dsn, "jobs", "fork", str(source))

        assert result.exit_code == 0, result.output
        assert "no checkpoints copied: re-runs from the start" in result.output

    async def test_inspect_shows_lineage_in_both_directions(
        self, dsn, db_pool, unique_queue
    ):
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)
        fork = await db.fork_job(db_pool, source, from_step=3)

        on_fork = await run_cli("--dsn", dsn, "jobs", "inspect", str(fork["job_id"]))
        on_source = await run_cli("--dsn", dsn, "jobs", "inspect", str(source))

        assert (
            f"Forked From:     job {source} at step 3 (2 checkpoint(s) copied)"
            in on_fork.output
        )
        assert f"Forked Into:     {fork['job_id']}" in on_source.output

    async def test_inspect_still_answers_when_the_source_is_gone(
        self, dsn, db_pool, unique_queue
    ):
        """The column is NULL after retention reaps the source, and "step 3
        of a job since deleted" is a better answer than silence."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)
        fork = await db.fork_job(db_pool, source, from_step=3)
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", source)

        result = await run_cli("--dsn", dsn, "jobs", "inspect", str(fork["job_id"]))

        assert (
            "Forked From:     a job since deleted at step 3 (2 checkpoint(s) copied)"
            in result.output
        )

    async def test_history_names_the_origin_under_the_trail(
        self, dsn, db_pool, unique_queue
    ):
        """There is no 'forked' event — this table records state transitions
        — so the origin rides on the row that IS the fork's creation."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)
        fork = await db.fork_job(db_pool, source, from_step=3)

        result = await run_cli("--dsn", dsn, "jobs", "history", str(fork["job_id"]))

        assert "enqueued" in result.output
        assert (
            f"Forked from job {source} at step 3 (2 checkpoint(s) copied)"
            in result.output
        )


class TestConcurrentForks:
    """Nothing about a fork serialises against the source, so several
    operators forking the same incident get several jobs and no waiting."""

    async def test_many_forks_of_one_source_all_land(self, db_pool, unique_queue):
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        forks = await asyncio.gather(
            *(db.fork_job(db_pool, source, from_step=3) for _ in range(5))
        )

        ids = {f["job_id"] for f in forks}
        assert len(ids) == 5
        for job_id in ids:
            assert len(await steps_of(db_pool, job_id)) == 2


class TestTheSourceDeletedUnderneath:
    """`forked_from REFERENCES jorb (id)`, so the source has to still exist
    when the insert's FK check runs -- and nothing serialises a fork against a
    retention sweep or an operator delete."""

    async def test_a_concurrent_delete_is_a_refusal_and_not_a_driver_error(
        self, db_pool, db_params, unique_queue
    ):
        """The fork's snapshot found the source, and by the time the INSERT's
        foreign key was checked it was gone.

        Before the fix this surfaced as a raw
        ``asyncpg.ForeignKeyViolationError`` naming ``jorb_forked_from_fkey``
        and a column no caller ever passed -- an operator running
        ``jobs fork`` during an incident got a driver traceback about
        referential integrity instead of "the job you named is not there any
        more". It is the same condition ``_no_such_job`` reports, only observed
        later, so it gets the same kind of answer.

        Deterministic by construction: the DELETE is held in an open
        transaction until the fork is provably blocked on its row lock.
        """
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        deleter = await db.connect(**db_params)
        forker = await db.connect(**db_params)
        try:
            deleting = deleter.transaction()
            await deleting.start()
            await deleter.execute("DELETE FROM jorb WHERE id = $1", source)

            forking = asyncio.create_task(db.fork_job(forker, source, from_step=3))
            await wait_until_blocked_on_a_transaction(db_pool)
            assert not forking.done(), "the fork must wait out the open delete"

            await deleting.commit()

            with pytest.raises(db.ForkRefused, match="was deleted while forking"):
                await asyncio.wait_for(forking, timeout=20)
        finally:
            await forker.close()
            await deleter.close()

        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE forked_from = $1", source
            )
            == 0
        ), "nothing may be written when the fork is refused"

    async def test_a_delete_that_rolls_back_lets_the_fork_land(
        self, db_pool, db_params, unique_queue
    ):
        """The positive control: the refusal must come from the source really
        going away, not merely from having waited for a lock."""
        source = await make_source(db_pool, unique_queue)
        await record_steps(db_pool, source, 3)

        deleter = await db.connect(**db_params)
        forker = await db.connect(**db_params)
        try:
            deleting = deleter.transaction()
            await deleting.start()
            await deleter.execute("DELETE FROM jorb WHERE id = $1", source)

            forking = asyncio.create_task(db.fork_job(forker, source, from_step=3))
            await wait_until_blocked_on_a_transaction(db_pool)

            await deleting.rollback()
            fork = await asyncio.wait_for(forking, timeout=20)
        finally:
            await forker.close()
            await deleter.close()

        assert fork["steps_copied"] == 2
        assert (
            await db_pool.fetchval(
                "SELECT forked_from FROM jorb WHERE id = $1", fork["job_id"]
            )
            == source
        )
