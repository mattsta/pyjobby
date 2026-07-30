"""Regression tests for multi-job orchestration correctness.

Each test here pins a property that a graph builder got wrong: a dependent
that could be released before its dependencies finished, a graph written to
the database piecemeal, dependency edges that were never recorded, and a
"success" verdict for a DAG that never ran to completion.

The pipeline and fan-out builders live here too, next to DAGBuilder: they
are the linear and the flat graph, they owe the same all-or-nothing property
for the same reason, and the reason is worth stating once.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from pyjobby.client import JobClient
from pyjobby.dag import DAGBuilder, wait_for_dag

pytestmark = pytest.mark.asyncio


def _dag_name() -> str:
    return f"correctness-{uuid.uuid4().hex[:8]}"


async def _jobs(db_pool, job_ids):
    rows = await db_pool.fetch(
        """
        SELECT id, job_class, state, run_group, waitfor_group, waitfor_job
        FROM jorb WHERE id = ANY($1)
        """,
        list(job_ids),
    )
    return {r["id"]: dict(r) for r in rows}


async def _queue_count(db_pool, queue) -> int:
    count: int = await db_pool.fetchval(
        "SELECT count(*) FROM jorb WHERE queue = $1", queue
    )
    return count


class TestFanInGrouping:
    """waitfor_group must actually cover every dependency."""

    async def test_shared_dependency_is_not_stolen_by_another_fan_in(
        self, db_pool, client, unique_queue
    ):
        """Two fan-in nodes sharing a dependency must not corrupt each other.

        run_group is a single column, so 'the group is the first dependency's
        job id, stamped onto every dependency' loses: with C <- {A, B} and
        D <- {B, E}, stamping D's group onto B removed B from C's group, and
        C was then released as soon as A finished -- before B had even been
        claimed.
        """
        dag = DAGBuilder(name=_dag_name(), queue=unique_queue)
        a = dag.add("A")
        b = dag.add("B")
        e = dag.add("E")
        c = dag.add("C", depends_on=[a, b])
        d = dag.add("D", depends_on=[b, e])

        mapping = await dag.execute(client)
        jobs = await _jobs(db_pool, mapping.values())

        for waiter, deps in ((c, (a, b)), (d, (b, e))):
            group = jobs[mapping[waiter]]["waitfor_group"]
            assert group is not None, f"{waiter.job_class} has no wait group"
            for dep in deps:
                assert jobs[mapping[dep]]["run_group"] == group, (
                    f"{waiter.job_class} would start without "
                    f"{dep.job_class} having finished"
                )

    async def test_every_group_member_is_upstream_of_its_waiter(
        self, db_pool, client, unique_queue
    ):
        """Merging groups must never put a waiter's own descendant in it."""
        dag = DAGBuilder(name=_dag_name(), queue=unique_queue)
        a = dag.add("A")
        b = dag.add("B")
        c = dag.add("C", depends_on=[a, b])
        dag.add("X", depends_on=[b, c])

        # X's group must merge {A, B} with {B, C} -- which contains C, the
        # node that waits on {A, B}. Nothing could ever finish; reject it.
        with pytest.raises(ValueError, match="fan-in"):
            await dag.execute(client)

    async def test_duplicate_dependency_is_one_edge(
        self, client, db_pool, unique_queue
    ):
        """The same upstream listed twice is one dependency, not a cycle.

        topological_sort() counted both, then could only ever decrement once,
        and reported the DAG as cyclic.
        """
        dag = DAGBuilder(name=_dag_name(), queue=unique_queue)
        a = dag.add("A")
        b = dag.add("B", depends_on=[a, a])

        assert b.depends_on == [a]
        assert dag.topological_sort() == [[a], [b]]

        mapping = await dag.execute(client)
        jobs = await _jobs(db_pool, mapping.values())
        # One dependency -> the direct waitfor_job path, not a group of one
        assert jobs[mapping[b]]["waitfor_job"] == mapping[a]
        assert jobs[mapping[b]]["waitfor_group"] is None


class TestDAGCreationIsAtomic:
    async def test_failure_midway_leaves_no_partial_dag(
        self, db_pool, client, unique_queue, monkeypatch
    ):
        """A DAG is created in one transaction or not at all.

        Jobs used to be inserted one statement at a time, each immediately
        visible to workers. A level-0 job that finished before its dependents
        existed left them 'waiting' forever (the wake-up is issued by the
        worker finishing the upstream job, not by a trigger), and any error
        partway through left an orphan jorb_dag row plus a half graph.
        """
        name = _dag_name()
        dag = DAGBuilder(name=name, queue=unique_queue)
        first = dag.add("First")
        second = dag.add("Second", depends_on=[first])
        dag.add("Third", depends_on=[second])

        real_build = JobClient.build_enqueue_row
        calls = {"n": 0}

        def explode(job_class, **options):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("enqueue exploded")
            return real_build(job_class, **options)

        monkeypatch.setattr(JobClient, "build_enqueue_row", staticmethod(explode))

        with pytest.raises(RuntimeError, match="exploded"):
            await dag.execute(client)

        assert calls["n"] == 3, "test did not reach the failing enqueue"
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
            )
            == 0
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_dag WHERE name = $1", name
            )
            == 0
        )

    async def test_dependency_edges_are_recorded(self, db_pool, client, unique_queue):
        """jorb_dependencies is the platform's dependency record.

        `pj dag visualize` reads it (UNION waitfor_job), so a fan-in node with
        nothing in the table showed up with no incoming edges at all.
        """
        dag = DAGBuilder(name=_dag_name(), queue=unique_queue)
        a = dag.add("A")
        b = dag.add("B")
        c = dag.add("C", depends_on=[a, b])

        mapping = await dag.execute(client)

        edges = await db_pool.fetch(
            "SELECT job_id, depends_on FROM jorb_dependencies WHERE job_id = $1",
            mapping[c],
        )
        assert {r["depends_on"] for r in edges} == {mapping[a], mapping[b]}


class TestChainCreationIsAtomic:
    """create_pipeline / create_pipeline_with_results / create_fan_out owe
    the property TestDAGCreationIsAtomic pins for DAGBuilder.

    They are the linear and the flat graph builders, and they used to write
    one job per pool connection: stage 0 committed on its own, immediately
    claimable, while the stages that depend on it were still being written.
    A failure after that left a head with no tail — and a fan-out left a
    run_group whose waiter can never be satisfied, because the members that
    would have completed it do not exist.
    """

    async def test_pipeline_failure_midway_leaves_no_jobs(
        self, db_pool, client, unique_queue
    ):
        """The second stage is refused by validation; the first must not
        survive it.

        No monkeypatching: `on_timeout` is a real enqueue option, and a bad
        one is refused by build_enqueue_row — after stage 0's INSERT has
        already run inside the transaction, which is exactly the window the
        transaction exists to close.
        """
        with pytest.raises(ValueError, match="on_timeout"):
            await client.create_pipeline(
                [
                    ("chain.First", {}),
                    ("chain.Second", {"on_timeout": "explode"}),
                    ("chain.Third", {}),
                ],
                queue=unique_queue,
            )

        assert await _queue_count(db_pool, unique_queue) == 0

    async def test_pipeline_with_results_failure_midway_leaves_no_jobs(
        self, db_pool, client, unique_queue, monkeypatch
    ):
        """Same property for the result-passing verb, failing the way the DAG
        test does: the third row builder raises, so two jobs are already
        written when it happens."""
        real_build = JobClient.build_enqueue_row
        calls = {"n": 0}

        def explode(job_class, **options):
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("enqueue exploded")
            return real_build(job_class, **options)

        monkeypatch.setattr(JobClient, "build_enqueue_row", staticmethod(explode))

        with pytest.raises(RuntimeError, match="exploded"):
            await client.create_pipeline_with_results(
                [
                    ("chain.Fetch", {}, True),
                    ("chain.Process", {}, True),
                    ("chain.Store", {}, False),
                ],
                queue=unique_queue,
            )

        assert calls["n"] == 3, "test did not reach the failing stage"
        assert await _queue_count(db_pool, unique_queue) == 0

    async def test_fan_out_failure_midway_leaves_no_group(
        self, db_pool, client, unique_queue, monkeypatch
    ):
        """A half-written group is worse than none: `waitfor_group` is
        satisfied by the members that exist, so a waiter on a truncated group
        runs early rather than never."""
        real_build = JobClient.build_enqueue_row
        calls = {"n": 0}

        def explode(job_class, **options):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("fan-out exploded")
            return real_build(job_class, **options)

        monkeypatch.setattr(JobClient, "build_enqueue_row", staticmethod(explode))

        with pytest.raises(RuntimeError, match="exploded"):
            await client.create_fan_out(
                "chain.Item",
                [{"item": 1}, {"item": 2}, {"item": 3}],
                queue=unique_queue,
            )

        assert await _queue_count(db_pool, unique_queue) == 0

    async def test_pipeline_still_links_every_stage_to_the_previous(
        self, db_pool, client, unique_queue
    ):
        """The happy path is unchanged by the transaction: stage 0 queued,
        every later stage waiting on the one before it."""
        job_ids = await client.create_pipeline(
            [("chain.A", {}), ("chain.B", {}), ("chain.C", {})],
            queue=unique_queue,
            priority=42,
        )

        rows = await _jobs(db_pool, job_ids)
        assert len(job_ids) == 3
        assert rows[job_ids[0]]["waitfor_job"] is None
        assert rows[job_ids[0]]["state"] == "queued"
        assert rows[job_ids[1]]["waitfor_job"] == job_ids[0]
        assert rows[job_ids[2]]["waitfor_job"] == job_ids[1]
        assert rows[job_ids[1]]["state"] == "waiting"
        assert rows[job_ids[2]]["state"] == "waiting"

        priorities = await db_pool.fetch(
            "SELECT prio FROM jorb WHERE id = ANY($1)", job_ids
        )
        assert {r["prio"] for r in priorities} == {42}

    async def test_pipeline_with_results_threads_use_result_from(
        self, db_pool, client, unique_queue
    ):
        """Result passing survives the move into one transaction: each stage
        reads the previous stage's result exactly when that stage saved it."""
        job_ids = await client.create_pipeline_with_results(
            [
                ("chain.Fetch", {}, True),
                ("chain.Process", {}, False),
                ("chain.Store", {}, True),
            ],
            queue=unique_queue,
        )

        admin = {
            r["id"]: r["admin_data"]
            for r in await db_pool.fetch(
                "SELECT id, admin_data FROM jorb WHERE id = ANY($1)", job_ids
            )
        }
        # stage 0 saves and has no upstream; stage 1 reads stage 0's result;
        # stage 2 has an upstream that did NOT save, so it reads nothing.
        assert "use_result_from" not in admin[job_ids[0]]
        assert admin[job_ids[1]]["use_result_from"] == job_ids[0]
        assert "use_result_from" not in admin[job_ids[2]]
        assert admin[job_ids[1]]["save_result"] is False

    async def test_fan_out_members_share_one_group(self, db_pool, client, unique_queue):
        """The happy path still returns (ids, group) with every member in it."""
        job_ids, run_group = await client.create_fan_out(
            "chain.Item",
            [{"item": 1}, {"item": 2}, {"item": 3}],
            queue=unique_queue,
        )

        rows = await _jobs(db_pool, job_ids)
        assert len(job_ids) == 3
        assert {r["run_group"] for r in rows.values()} == {run_group}


class TestWaitForDAG:
    async def test_cancelled_job_is_not_success(self, db_pool, client, unique_queue):
        """A DAG containing a cancelled job did not complete successfully."""
        dag = DAGBuilder(name=_dag_name(), queue=unique_queue)
        done = dag.add("Done")
        gone = dag.add("Gone")

        mapping = await dag.execute(client)
        dag_id = await db_pool.fetchval(
            "SELECT dag_id FROM jorb WHERE id = $1", mapping[done]
        )

        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", mapping[done]
        )
        await db_pool.execute(
            "UPDATE jorb SET state = 'cancelled' WHERE id = $1", mapping[gone]
        )

        assert await wait_for_dag(db_pool, dag_id, timeout=1) is False


class TestDAGCompletionStamp:
    """`jorb_dag.completed` is what an operator reads to know a DAG is done,
    so it has to be right under concurrency and it has to stay right.

    Both properties are the trigger's (`sql/schema/91_dag_complete.sql`): no
    writer of `jorb` knows about DAGs, which is the point of recording it
    there.
    """

    async def _dag_with_members(self, db_pool, n: int) -> tuple[int, list[int]]:
        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id", _dag_name()
        )
        members = [
            await db_pool.fetchval(
                "INSERT INTO jorb (job_class, state, dag_id) "
                "VALUES ('M', 'running', $1) RETURNING id",
                dag_id,
            )
            for _ in range(n)
        ]
        return dag_id, members

    async def test_two_concurrent_last_finishers_stamp_it_exactly_once(
        self, db_pool, db_params
    ):
        """The write skew, interleaved on purpose.

        Two READ COMMITTED transactions each finish one of the DAG's last two
        jobs, and neither commits until both have written. Counting unfinished
        members against each transaction's own snapshot, each one saw the
        OTHER still running -- so neither stamped, both committed, and the DAG
        sat at completed = NULL forever with every member terminal.
        """
        from pyjobby import db

        dag_id, (a, b) = await self._dag_with_members(db_pool, 2)

        first = await db.connect(**db_params)
        second = await db.connect(**db_params)
        try:
            t1 = first.transaction()
            t2 = second.transaction()
            await t1.start()
            await t2.start()
            await first.execute("UPDATE jorb SET state='finished' WHERE id=$1", a)

            # The two finish DIFFERENT jorb rows, so nothing about the jobs
            # themselves orders them. What orders them is the DAG row the
            # first one's trigger is holding, and this is where it shows: the
            # second finisher waits for it rather than deciding, alone and
            # against a stale snapshot, that the DAG is not done.
            blocked = asyncio.create_task(
                second.execute("UPDATE jorb SET state='finished' WHERE id=$1", b)
            )
            await asyncio.sleep(0.3)
            assert not blocked.done(), (
                "the second finisher did not serialise against the first: "
                "each will count the other as still running"
            )

            await t1.commit()
            await blocked
            await t2.commit()
        finally:
            await first.close()
            await second.close()

        row = await db_pool.fetchrow(
            "SELECT d.completed, s.pending_jobs FROM jorb_dag d "
            "JOIN jorb_dag_status s ON s.dag_id = d.id WHERE d.id = $1",
            dag_id,
        )
        assert row["pending_jobs"] == 0
        assert row["completed"] is not None, (
            "every member is terminal and the DAG was never stamped complete: "
            "the two finishers each decided the other was still running"
        )

    async def test_a_serial_finish_still_stamps_it(self, db_pool):
        """The control: nothing about the locking changed the ordinary path."""
        dag_id, members = await self._dag_with_members(db_pool, 2)
        for job_id in members:
            await db_pool.execute(
                "UPDATE jorb SET state='finished' WHERE id=$1", job_id
            )
        assert (
            await db_pool.fetchval("SELECT completed FROM jorb_dag WHERE id=$1", dag_id)
            is not None
        )

    async def test_requeueing_a_member_unstamps_the_dag(self, db_pool):
        """A DAG with work pending again is not a completed DAG.

        `completed` was write-once, so a retried member left the row saying
        the DAG finished while `jorb_dag_status` reported pending_jobs > 0 --
        and nothing ever corrected it, because the member's eventual second
        completion only re-stamps a row that is already stamped.
        """
        dag_id, members = await self._dag_with_members(db_pool, 2)
        for job_id in members:
            await db_pool.execute(
                "UPDATE jorb SET state='finished' WHERE id=$1", job_id
            )
        assert await db_pool.fetchval(
            "SELECT completed FROM jorb_dag WHERE id=$1", dag_id
        )

        await db_pool.execute(
            "UPDATE jorb SET state='queued', run_epoch = run_epoch + 1 WHERE id=$1",
            members[-1],
        )

        row = await db_pool.fetchrow(
            "SELECT d.completed, s.pending_jobs FROM jorb_dag d "
            "JOIN jorb_dag_status s ON s.dag_id = d.id WHERE d.id = $1",
            dag_id,
        )
        assert row["pending_jobs"] == 1
        assert row["completed"] is None, (
            "the DAG reports completed with a member back in the queue"
        )

        # ...and it is stamped again, with a NEW timestamp, when that member
        # finishes for the second time
        await db_pool.execute(
            "UPDATE jorb SET state='finished' WHERE id=$1", members[-1]
        )
        assert await db_pool.fetchval(
            "SELECT completed FROM jorb_dag WHERE id=$1", dag_id
        )
