"""The baseline the rest of tests/ops builds on: a real fleet works.

OPERATIONS.md's opening promises, verified with real processes: the
documented commands start a working fleet, a job flows through it, and
``pj-admin doctor`` reports the documented lines and exit code.
"""

from __future__ import annotations

import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import registered_workers, wait_until

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]


class TestFleetSmoke:
    async def test_fleet_runs_a_job_and_doctor_reports_healthy(
        self, fleet, admin, db_pool, unique_queue
    ):
        fleet.monitor()
        fleet.worker(unique_queue)

        await wait_until(
            lambda: registered_workers(db_pool, unique_queue),
            describe="worker registered",
            timeout=30,
        )

        client = JobClient(pool=db_pool)
        job_id = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=21)
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
        assert row["result"] == {"doubled": 42}

        report = admin("doctor")
        assert report.returncode == 0, report.stdout + report.stderr
        out = report.stdout
        assert "PASS database: connected" in out
        assert "PASS schema: installed" in out
        assert "PASS triggers: all schema triggers present (7)" in out
        assert "PASS notify-queue:" in out
        assert "PASS workers:" in out and "live worker" in out
        assert "PASS dlq: empty" in out
