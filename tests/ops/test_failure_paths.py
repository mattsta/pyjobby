"""Jobs failing every documented way, and every documented surface noticing.

The claims induced here: a timed-out synchronous job leaves an abandoned
thread the worker accounts for, refuses claims over, and recovers from;
retries exhaust into `crashed` -- THE dead letter queue -- and `dlq retry`
grants a fresh budget; retry refuses a finished job while rerun accepts
it; an unimportable job class dead-letters with the search path in its
error; a cancel reaches a running job in about a second; and a waiter
whose upstream crashed shows up in exactly one place, doctor's
blocked-waiters sweep, while the monitor leaves it alone.
"""

from __future__ import annotations

import asyncio

import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import wait_until

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]


class TestAbandonedJobThreads:
    async def test_timed_out_sync_job_fills_the_pool_and_the_worker_says_so(
        self, fleet, admin, db_pool, unique_queue
    ):
        worker = fleet.worker(
            unique_queue, "--job-threads", "1", "--default-timeout", "1"
        )
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.SyncBlockFirstAttemptJob",
            queue=unique_queue,
            seconds=8,
            initial_retry_delay=1,
            max_retry_delay=1,
        )

        # The deadline fires on time; the thread cannot be stopped; the
        # worker publishes the saturation on its own registry row.
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb_worker WHERE queue = $1 "
                "AND job_threads = 1 AND job_threads_abandoned = 1",
                unique_queue,
            ),
            describe="abandoned thread published on the heartbeat",
            timeout=30,
        )

        report = admin("doctor")
        assert report.returncode == 0, "lost capacity is a WARN, not a FAIL"
        assert "WARN job-threads:" in report.stdout
        assert "not claiming" in report.stdout

        listing = admin("workers", "list")
        assert "not claiming" in listing.stdout
        assert "1/1" in listing.stdout

        assert "NOT CLAIMING: 1 abandoned job thread(s)" in worker.log_text()

        # Self-healing, end to end: the thread drains, the worker resumes,
        # the retried attempt sails through.
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=60)
        assert row["result"] == "done"
        assert row["run_count"] == 2
        assert "Claiming again after" in worker.log_text()
        assert "WARN job-threads" not in admin("doctor").stdout


class TestDeadLetterQueue:
    async def test_exhausted_retries_land_in_crashed_and_dlq_retry_resets_the_budget(
        self, fleet, admin, db_pool, unique_queue
    ):
        fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.FailJob",
            queue=unique_queue,
            max_retries=2,
            initial_retry_delay=1,
            max_retry_delay=1,
        )
        row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=60)
        assert row["error_count"] == 2
        assert "intentional failure" in row["error_message"]

        listing = admin("dlq", "list")
        assert str(job_id) in listing.stdout

        history = admin("jobs", "history", str(job_id))
        assert history.stdout.count("intentional failure") >= 2

        retried = admin("dlq", "retry", str(job_id))
        assert retried.returncode == 0

        # Same row, same bug: it dead-letters again -- and the numbers can
        # only work out this way if the budget was genuinely reset. Without
        # the reset the second run would have no attempts at all; with it,
        # the terminal row shows a full fresh budget spent (error_count 2,
        # not 4) while the history keeps every attempt from both lives.
        row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=60)
        assert row["error_count"] == 2
        history = admin("jobs", "history", str(job_id))
        assert history.stdout.count("intentional failure") >= 4


class TestRetryVersusRerun:
    async def test_retry_refuses_a_finished_job_and_rerun_accepts_it(
        self, fleet, admin, db_pool, unique_queue
    ):
        fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, x=21
        )
        await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)

        refused = admin("jobs", "retry", str(job_id))
        assert refused.returncode != 0
        assert "rerun" in (refused.stdout + refused.stderr), (
            "the refusal should hand the operator the verb that would work"
        )

        rerun = admin("jobs", "rerun", str(job_id))
        assert rerun.returncode == 0
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE id = $1 AND state = 'finished' "
                "AND run_count = 2",
                job_id,
            ),
            describe="rerun executed the finished job again",
            timeout=30,
        )


class TestUnimportableJobClass:
    async def test_missing_class_dead_letters_with_the_search_path_in_the_error(
        self, fleet, db_pool, unique_queue
    ):
        fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.no_such_module.Nope",
            queue=unique_queue,
            max_retries=1,
            initial_retry_delay=1,
        )
        row = await wait_for_job_state(db_pool, job_id, ("crashed",), timeout=60)
        assert "Job class not found: tests.no_such_module.Nope" in row["error_message"]
        assert "search path" in row["error_message"]


class TestCancel:
    async def test_cancel_reaches_a_running_job_within_about_a_second(
        self, fleet, admin, db_pool, unique_queue
    ):
        fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=600
        )
        await wait_for_job_state(db_pool, job_id, ("running",), timeout=30)

        cancelled = admin("jobs", "cancel", str(job_id))
        assert cancelled.returncode == 0
        # "within about a second"; the admin round trip gets a little slack.
        row = await wait_for_job_state(db_pool, job_id, ("cancelled",), timeout=5)
        assert row["state"] == "cancelled"


class TestBlockedWaiters:
    async def test_waiter_on_a_crashed_upstream_surfaces_only_in_doctor(
        self, fleet, admin, db_pool, unique_queue
    ):
        fleet.monitor()
        fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        upstream = await client.enqueue(
            "tests.dxe_jobs.FailJob",
            queue=unique_queue,
            max_retries=1,
            initial_retry_delay=1,
        )
        waiter = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, x=1, waitfor_job=upstream
        )
        await wait_for_job_state(db_pool, upstream, ("crashed",), timeout=60)

        # The monitor leaves the waiter alone on purpose...
        await asyncio.sleep(2)
        assert (
            await db_pool.fetchval("SELECT state FROM jorb WHERE id = $1", waiter)
            == "waiting"
        )
        # ...so doctor's sweep is the only place it shows up.
        report = admin("doctor")
        assert report.returncode == 0
        assert "WARN blocked-waiters:" in report.stdout
