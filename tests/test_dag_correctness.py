"""Regression tests for DAG orchestration correctness.

Each test here pins a property that DAGBuilder got wrong: a dependent that
could be released before its dependencies finished, a graph that was written
to the database piecemeal, dependency edges that were never recorded, and a
"success" verdict for a DAG that never ran to completion.
"""

from __future__ import annotations

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
