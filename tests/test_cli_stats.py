"""Live-database tests for the pj-admin statistics commands.

`jobs retry-stats` and `jobs timeout-stats` bypass AdminAPI and run their
own SQL, so the mock-based CLI suite cannot cover them: these drive the
real commands against a real database through CliRunner with --dsn.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from pyjobby.cli import cli

pytestmark = pytest.mark.asyncio


@pytest.fixture
def dsn(db_params: dict) -> str:
    """A DSN string for --dsn, built from the session's db params."""
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
    )


async def run_cli(dsn: str, *args: str) -> dict:
    """Invoke a pj-admin command with --json and return the parsed payload.

    The CLI drives its own asyncio.run(), so it runs in a worker thread to
    stay clear of the test's event loop."""

    def _invoke() -> dict:
        result = CliRunner().invoke(cli, ["--dsn", dsn, *args, "--json"])
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    return await asyncio.to_thread(_invoke)


async def make_job(
    conn,
    queue: str,
    *,
    job_class: str = "tests.dxe_jobs.OkJob",
    state: str = "queued",
    admin_data: dict | None = None,
    error_message: str | None = None,
    timeout_at: datetime | None = None,
) -> int:
    return await conn.fetchval(
        """INSERT INTO jorb (job_class, queue, state, admin_data,
                             error_message, timeout_at)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        job_class,
        queue,
        state,
        admin_data or {},
        error_message,
        timeout_at,
    )


async def record_attempts(conn, job_id: int, attempts: int) -> None:
    """Record N execution attempts in the history trail.

    An attempt is a 'running' event; retries reuse the same job row, so
    attempt count lives in jorb_history rather than in duplicated rows."""
    for _ in range(attempts):
        await conn.execute(
            "INSERT INTO jorb_history (job_id, event, detail) VALUES ($1, 'running', '{}')",
            job_id,
        )


class TestRetryStats:
    """`pj-admin jobs retry-stats` reads the jorb_history attempt trail."""

    async def test_reports_retried_jobs_by_class(self, db_pool, unique_queue, dsn):
        retried = await make_job(db_pool, unique_queue, state="finished")
        await record_attempts(db_pool, retried, 3)

        failed = await make_job(
            db_pool,
            unique_queue,
            job_class="tests.dxe_jobs.FailJob",
            state="crashed",
            error_message="boom",
        )
        await record_attempts(db_pool, failed, 2)

        # a job that succeeded first try must NOT appear (attempts == 1)
        once = await make_job(db_pool, unique_queue, state="finished")
        await record_attempts(db_pool, once, 1)

        payload = await run_cli(dsn, "jobs", "retry-stats", "-q", unique_queue)

        by_class = {s["job_class"]: s for s in payload["stats_by_job_class"]}
        assert set(by_class) == {
            "tests.dxe_jobs.OkJob",
            "tests.dxe_jobs.FailJob",
        }
        assert by_class["tests.dxe_jobs.OkJob"]["max_attempts"] == 3
        assert by_class["tests.dxe_jobs.OkJob"]["eventually_succeeded"] == 1
        assert by_class["tests.dxe_jobs.FailJob"]["permanently_failed"] == 1

        top = {j["id"]: j for j in payload["top_retries"]}
        assert top[retried]["attempts"] == 3
        assert once not in top

    async def test_queue_filter_scopes_results(self, db_pool, unique_queue, dsn):
        other_queue = f"{unique_queue}_other"
        mine = await make_job(db_pool, unique_queue, state="finished")
        theirs = await make_job(db_pool, other_queue, state="finished")
        await record_attempts(db_pool, mine, 2)
        await record_attempts(db_pool, theirs, 2)

        payload = await run_cli(dsn, "jobs", "retry-stats", "-q", unique_queue)

        ids = {j["id"] for j in payload["top_retries"]}
        assert mine in ids
        assert theirs not in ids

    async def test_no_retries_reports_empty(self, db_pool, unique_queue, dsn):
        job_id = await make_job(db_pool, unique_queue, state="finished")
        await record_attempts(db_pool, job_id, 1)

        payload = await run_cli(dsn, "jobs", "retry-stats", "-q", unique_queue)

        assert payload["stats_by_job_class"] == []
        assert payload["top_retries"] == []


class TestTimeoutStats:
    """`pj-admin jobs timeout-stats` summarizes timeout configuration/outcomes."""

    async def test_summarizes_timeout_outcomes(self, db_pool, unique_queue, dsn):
        await make_job(
            db_pool,
            unique_queue,
            state="finished",
            admin_data={"timeout_seconds": 30, "on_timeout": "retry"},
        )
        timed_out = await make_job(
            db_pool,
            unique_queue,
            state="crashed",
            admin_data={"timeout_seconds": 60, "on_timeout": "fail"},
            error_message="Timeout exceeded - dead-lettered (on_timeout=fail)",
        )
        overdue = await make_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data={"timeout_seconds": 10, "on_timeout": "retry"},
            timeout_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        # a job with no timeout configured is outside these stats entirely
        await make_job(db_pool, unique_queue, state="finished")

        payload = await run_cli(dsn, "jobs", "timeout-stats", "-q", unique_queue)

        summary = payload["summary"]
        assert summary["total_with_timeout"] == 3
        assert summary["completed"] == 1
        assert summary["timed_out"] == 1
        assert summary["currently_timed_out"] == 1
        assert float(summary["avg_timeout_seconds"]) == pytest.approx(33.33, abs=0.1)

        violations = {v["id"] for v in payload["current_violations"]}
        assert overdue in violations

        recent = {j["id"]: j for j in payload["recent_timeouts"]}
        assert timed_out in recent
        assert recent[timed_out]["on_timeout"] == "fail"

    async def test_running_job_within_deadline_is_not_a_violation(
        self, db_pool, unique_queue, dsn
    ):
        healthy = await make_job(
            db_pool,
            unique_queue,
            state="running",
            admin_data={"timeout_seconds": 3600},
            timeout_at=datetime.now(UTC) + timedelta(hours=1),
        )

        payload = await run_cli(dsn, "jobs", "timeout-stats", "-q", unique_queue)

        assert payload["summary"]["currently_timed_out"] == 0
        assert healthy not in {v["id"] for v in payload["current_violations"]}
