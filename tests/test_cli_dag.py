"""Operator surface for DAGs: `pj-admin dag list | show | visualize`.

DAG orchestration is a headline feature whose only operator view is these
three commands, so what they report has to be true: the progress percentage,
the state a partly-crashed DAG is in, the topological levels, and -- the one
that matters under pressure -- whether a broken DAG is reported as broken or
quietly rendered as if it were fine.

Rows are inserted directly rather than built through the DAG API: these tests
are about what the commands REPORT, so the data they report on is fixed and
visible in the test.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from pyjobby.cli import cli

pytestmark = pytest.mark.asyncio

MISSING_ID = 999_999_999


def dsn_for(db_params: dict) -> str:
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
    )


@pytest.fixture
def dsn(db_params: dict) -> str:
    return dsn_for(db_params)


async def run_cli(*args: str):
    """Invoke pj-admin in a worker thread (the CLI owns its own event loop)."""

    def _invoke():
        return CliRunner().invoke(cli, list(args))

    return await asyncio.to_thread(_invoke)


async def make_dag(pool, name: str) -> int:
    return await pool.fetchval(
        "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id", name
    )


async def make_dag_job(
    pool,
    dag_id: int,
    queue: str,
    state: str,
    job_class: str = "tests.dxe_jobs.OkJob",
    waitfor_job: int | None = None,
) -> int:
    return await pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, state, prio,
                             dag_id, waitfor_job)
           VALUES ($1, '{}', $2, $3::jorbstate, 100, $4, $5) RETURNING id""",
        job_class,
        queue,
        state,
        dag_id,
        waitfor_job,
    )


async def add_edge(pool, job_id: int, depends_on: int) -> None:
    await pool.execute(
        "INSERT INTO jorb_dependencies (job_id, depends_on) VALUES ($1, $2)",
        job_id,
        depends_on,
    )


class TestDagList:
    """`dag list` -- the fleet view."""

    async def test_empty_is_not_an_error(self, dsn):
        """No DAGs is a normal state, not a failure."""
        result = await run_cli("--dsn", dsn, "dag", "list")

        assert result.exit_code == 0, result.output
        assert "No DAGs found" in result.output

    async def test_reports_progress_and_counts(self, dsn, db_pool, unique_queue):
        dag_id = await make_dag(db_pool, "etl-nightly")
        await make_dag_job(db_pool, dag_id, unique_queue, "finished")
        await make_dag_job(db_pool, dag_id, unique_queue, "finished")
        await make_dag_job(db_pool, dag_id, unique_queue, "queued")
        await make_dag_job(db_pool, dag_id, unique_queue, "running")

        result = await run_cli("--dsn", dsn, "dag", "list")

        assert result.exit_code == 0, result.output
        assert "etl-nightly" in result.output
        assert "2/4" in result.output
        assert "50%" in result.output
        assert "running" in result.output
        assert "Showing 1 DAG(s)" in result.output

    async def test_a_dag_with_a_crashed_job_reads_as_failed(
        self, dsn, db_pool, unique_queue
    ):
        """A DAG that lost a job is failed, not merely incomplete.

        'crashed' is terminal, so nothing will move this DAG forward on its
        own -- reporting it as still running would tell an operator to wait
        for something that is never going to happen.
        """
        dag_id = await make_dag(db_pool, "with-a-crash")
        await make_dag_job(db_pool, dag_id, unique_queue, "finished")
        await make_dag_job(db_pool, dag_id, unique_queue, "crashed")

        result = await run_cli("--dsn", dsn, "dag", "list")

        assert result.exit_code == 0, result.output
        assert "failed" in result.output

    async def test_json_output_is_machine_readable(self, dsn, db_pool, unique_queue):
        dag_id = await make_dag(db_pool, "json-dag")
        await make_dag_job(db_pool, dag_id, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, "dag", "list", "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert [d["name"] for d in payload] == ["json-dag"]
        assert payload[0]["total_jobs"] == 1
        assert payload[0]["finished_jobs"] == 1

    async def test_limit_bounds_the_result_set(self, dsn, db_pool, unique_queue):
        for n in range(3):
            dag_id = await make_dag(db_pool, f"dag-{n}")
            await make_dag_job(db_pool, dag_id, unique_queue, "queued")

        result = await run_cli("--dsn", dsn, "dag", "list", "--limit", "2", "--json")

        assert result.exit_code == 0, result.output
        assert len(json.loads(result.output)) == 2


class TestDagShow:
    """`dag show` -- one DAG in detail."""

    async def test_unknown_dag_fails(self, dsn):
        result = await run_cli("--dsn", dsn, "dag", "show", str(MISSING_ID))

        assert result.exit_code == 1, result.output
        assert f"DAG {MISSING_ID} not found" in result.output + result.stderr

    async def test_reports_every_count_and_lists_the_jobs(
        self, dsn, db_pool, unique_queue
    ):
        dag_id = await make_dag(db_pool, "detailed")
        first = await make_dag_job(db_pool, dag_id, unique_queue, "finished")
        await make_dag_job(db_pool, dag_id, unique_queue, "queued", waitfor_job=first)
        await make_dag_job(db_pool, dag_id, unique_queue, "crashed")
        await make_dag_job(db_pool, dag_id, unique_queue, "cancelled")

        result = await run_cli("--dsn", dsn, "dag", "show", str(dag_id))

        assert result.exit_code == 0, result.output
        assert f"DAG: detailed (ID: {dag_id})" in result.output
        assert "Total:       4" in result.output
        assert "Finished:    1" in result.output
        assert "Crashed:     1" in result.output
        assert "Cancelled:   1" in result.output
        # the dependency edge is visible, so an operator can see what is
        # holding a queued job back
        assert f"job:{first}" in result.output

    async def test_json_output_carries_dag_and_jobs(self, dsn, db_pool, unique_queue):
        dag_id = await make_dag(db_pool, "json-detail")
        job_id = await make_dag_job(db_pool, dag_id, unique_queue, "finished")

        result = await run_cli("--dsn", dsn, "dag", "show", str(dag_id), "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dag"]["dag_id"] == dag_id
        assert [j["id"] for j in payload["jobs"]] == [job_id]


class TestDagVisualize:
    """`dag visualize` -- the structure, level by level."""

    async def test_unknown_dag_fails(self, dsn):
        result = await run_cli("--dsn", dsn, "dag", "visualize", str(MISSING_ID))

        assert result.exit_code == 1, result.output
        assert "not found or has no jobs" in result.output + result.stderr

    async def test_levels_follow_the_dependency_edges(self, dsn, db_pool, unique_queue):
        """A → B → C must render as three levels in order."""
        dag_id = await make_dag(db_pool, "chain")
        a = await make_dag_job(db_pool, dag_id, unique_queue, "finished")
        b = await make_dag_job(db_pool, dag_id, unique_queue, "queued")
        c = await make_dag_job(db_pool, dag_id, unique_queue, "queued")
        await add_edge(db_pool, b, a)
        await add_edge(db_pool, c, b)

        result = await run_cli("--dsn", dsn, "dag", "visualize", str(dag_id))

        assert result.exit_code == 0, result.output
        assert "Total: 3 level(s), 3 job(s)" in result.output
        # each job appears under a later level than the one it depends on
        positions = {job: result.output.index(f"Job {job}:") for job in (a, b, c)}
        assert positions[a] < positions[b] < positions[c]
        assert "Depends on: none" in result.output

    async def test_independent_jobs_share_one_level(self, dsn, db_pool, unique_queue):
        dag_id = await make_dag(db_pool, "fan")
        for _ in range(3):
            await make_dag_job(db_pool, dag_id, unique_queue, "queued")

        result = await run_cli("--dsn", dsn, "dag", "visualize", str(dag_id))

        assert result.exit_code == 0, result.output
        assert "Total: 1 level(s), 3 job(s)" in result.output

    async def test_a_cycle_is_reported_as_a_failure(self, dsn, db_pool, unique_queue):
        """A cyclic DAG can never run, so visualize must FAIL on it.

        The command detects the cycle either way; the point is the exit
        status. Rendering the acyclic part and exiting 0 tells a script the
        DAG is fine, and tells an operator scanning output that the levels
        shown are the whole picture.
        """
        dag_id = await make_dag(db_pool, "cyclic")
        a = await make_dag_job(db_pool, dag_id, unique_queue, "queued")
        b = await make_dag_job(db_pool, dag_id, unique_queue, "queued")
        await add_edge(db_pool, b, a)
        await add_edge(db_pool, a, b)

        result = await run_cli("--dsn", dsn, "dag", "visualize", str(dag_id))

        assert result.exit_code == 1, result.output
        combined = result.output + result.stderr
        assert "Cycle detected" in combined
        # and it names the jobs still stuck in the cycle, which is what an
        # operator needs in order to break it
        assert str(a) in combined and str(b) in combined

    async def test_json_emits_the_same_levels_as_data(self, dsn, db_pool, unique_queue):
        """The rendering is a topological sort, not a diagram language, so
        the JSON form is the levels themselves."""
        dag_id = await make_dag(db_pool, "chain-json")
        a = await make_dag_job(db_pool, dag_id, unique_queue, "finished")
        b = await make_dag_job(db_pool, dag_id, unique_queue, "queued")
        await add_edge(db_pool, b, a)

        result = await run_cli("--dsn", dsn, "dag", "visualize", str(dag_id), "--json")

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["dag_id"] == dag_id
        assert payload["name"] == "chain-json"
        assert [[j["job_id"] for j in level] for level in payload["levels"]] == [
            [a],
            [b],
        ]
        assert payload["levels"][1][0]["depends_on"] == [a]
        assert payload["levels"][0][0]["depends_on"] == []

    async def test_json_on_an_unknown_dag_still_fails(self, dsn):
        result = await run_cli(
            "--dsn", dsn, "dag", "visualize", str(MISSING_ID), "--json"
        )

        assert result.exit_code == 1
        assert result.stdout.strip() == ""
