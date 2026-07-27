"""Client-library semantics that only show up with real jobs and real state.

Every test here pins down a behavior the client library got wrong at some
point, so they are written from the application developer's side: enqueue
through the client, let a REAL worker run the job, and read the answer back
through the same client API an application would use.

Covered:

- results are whatever the job returned — falsy values and plain strings are
  results, not "no result"
- get_job_result() and wait_for_result() must agree about the same job
- bulk_cancel() is cancel_job() applied to a list (running jobs included)
- a closed client opens nothing new (no leaked LISTEN connection)
- an unstorable argument is the enqueuer's error, not a worker's
- a priority above the workers' claim ceiling is refused at every door
  rather than becoming a job nothing will ever claim
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from pyjobby import Job, JobClient, db

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Jobs whose results are legitimately falsy / non-dict. Module scope so the
# worker resolves them by dotted path (tests.test_client_semantics.<Name>).
# ---------------------------------------------------------------------------


class CountJob(Job):
    """Returns a number — which may legitimately be 0."""

    async def task(self, n: int = 0) -> int:
        return n


class FlagJob(Job):
    """Returns a bool — which may legitimately be False."""

    async def task(self, flag: bool = False) -> bool:
        return flag


class ListJob(Job):
    """Returns a list — which may legitimately be empty."""

    async def task(self, items: list | None = None) -> list:
        return items or []


class StringJob(Job):
    """Returns a bare string (not a JSON document)."""

    async def task(self, text: str = "done") -> str:
        return text


@pytest_asyncio.fixture
async def client(db_pool):
    return JobClient(db_pool)


async def run_to_completion(client: JobClient, db_pool, queue: str, **options) -> int:
    """Enqueue, wait for the worker to finish it, return the job id."""
    job_id = await client.enqueue(queue=queue, **options)
    await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
    return job_id


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class TestJobResults:
    async def test_zero_is_a_result_not_a_missing_one(
        self, client, db_pool, live_worker, unique_queue
    ):
        """A job that returns 0 has a result of 0. Reporting None here made
        every falsy return value indistinguishable from 'nothing stored'."""
        await live_worker()
        job_id = await run_to_completion(
            client,
            db_pool,
            unique_queue,
            job_class="tests.test_client_semantics.CountJob",
            n=0,
        )

        assert await client.get_job_result(job_id) == 0
        # ...and the waiting API must not disagree with the reading one
        assert await client.wait_for_result(job_id, timeout=10) == 0

    @pytest.mark.parametrize(
        ("job_class", "kwargs", "expected"),
        [
            ("FlagJob", {"flag": False}, False),
            ("ListJob", {"items": []}, []),
            ("StringJob", {"text": ""}, ""),
        ],
    )
    async def test_falsy_results_survive_the_round_trip(
        self, client, db_pool, live_worker, unique_queue, job_class, kwargs, expected
    ):
        await live_worker()
        job_id = await run_to_completion(
            client,
            db_pool,
            unique_queue,
            job_class=f"tests.test_client_semantics.{job_class}",
            **kwargs,
        )

        assert await client.get_job_result(job_id) == expected
        assert await client.wait_for_result(job_id, timeout=10) == expected

    async def test_string_result_is_returned_verbatim(
        self, client, db_pool, live_worker, unique_queue
    ):
        """A string result is the job's string. Re-parsing it as JSON blew up
        (JSONDecodeError) on every job that returned plain text."""
        await live_worker()
        job_id = await run_to_completion(
            client,
            db_pool,
            unique_queue,
            job_class="tests.test_client_semantics.StringJob",
            text="woke",
        )

        assert await client.get_job_result(job_id) == "woke"
        assert await client.wait_for_result(job_id, timeout=10) == "woke"

    async def test_unfinished_and_missing_jobs_have_no_result(
        self, client, unique_queue
    ):
        job_id = await client.enqueue(
            "tests.test_client_semantics.CountJob", queue=unique_queue
        )
        assert await client.get_job_result(job_id) is None
        assert await client.get_job_result(job_id + 10_000_000) is None


# ---------------------------------------------------------------------------
# Bulk cancel
# ---------------------------------------------------------------------------


class TestBulkCancel:
    async def test_running_jobs_get_a_cancellation_request(
        self, client, db_pool, live_worker, unique_queue
    ):
        """bulk_cancel is cancel_job for a list: a running job is asked to
        stop, not silently skipped."""
        await live_worker()
        running = await client.enqueue(
            "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=30
        )
        await wait_for_job_state(db_pool, running, ("running",))
        queued = await client.enqueue(
            "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=30, priority=200
        )

        assert await client.bulk_cancel([queued, running]) == 2

        assert (await client.get_job(queued)).state == "cancelled"
        row = await wait_for_job_state(db_pool, running, ("cancelled",))
        assert row["state"] == "cancelled"

    async def test_cancelled_job_is_stamped_finished(
        self, client, db_pool, unique_queue
    ):
        """The single-job verb records when the job stopped; the bulk one
        must not leave a terminal job with finished IS NULL."""
        job_id = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=1)

        assert await client.bulk_cancel([job_id]) == 1

        row = await db_pool.fetchrow(
            "SELECT state, finished FROM jorb WHERE id = $1", job_id
        )
        assert row["state"] == "cancelled"
        assert row["finished"] is not None

    async def test_terminal_and_missing_jobs_are_not_counted(
        self, client, db_pool, unique_queue
    ):
        job_id = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=1)
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
        )

        assert await client.bulk_cancel([job_id, job_id + 10_000_000]) == 0
        assert (await client.get_job(job_id)).state == "finished"


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


class TestClientLifecycle:
    async def test_closed_client_opens_no_listener(self, db_pool, db_params):
        """A wait attempted after close() used to open a fresh LISTEN
        connection that nothing would ever close — one leak per call."""
        # a pool of our own: closing the client closes its pool
        pool = await asyncpg.create_pool(**db_params, min_size=1, max_size=2)
        client = JobClient(pool, db_params=db_params)

        assert await client._ensure_listener() is True
        await client.close()

        before = await db_pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        assert await client._ensure_listener() is False
        assert client._listener_conn is None

        with pytest.raises(Exception, match="closed"):
            await client.wait_for_result(1, timeout=5)

        after = await db_pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        assert after <= before


class TestEnqueueValidation:
    async def test_unserializable_kwargs_fail_at_enqueue(self, client, unique_queue):
        """A value the database cannot store must be the caller's error, not
        a crash inside a worker minutes later."""
        with pytest.raises(Exception) as excinfo:
            await client.enqueue(
                "tests.test_client_semantics.CountJob",
                queue=unique_queue,
                n={"a", "set"},
            )
        assert type(excinfo.value).__name__ in ("TypeError", "DataError")
        assert await client.queue_depth(unique_queue) == 0


class TestPriorityCeiling:
    """A priority above the workers' ceiling is refused at every door.

    Workers claim `prio <= their ceiling` (`pj --max-prio`, default 1000),
    and LOWER is MORE urgent — so "low priority, whenever" gets written as a
    big number and means NEVER: queued forever, no error, no retry, no DLQ,
    nothing in `doctor`. The client cannot see the fleet's ceiling, so it
    takes the deployment's word for it and refuses everything above.
    """

    async def test_enqueue_refuses_and_writes_nothing(self, client, unique_queue):
        with pytest.raises(ValueError) as refused:
            await client.enqueue(
                "tests.test_client_semantics.CountJob",
                queue=unique_queue,
                priority=1001,
                n=1,
            )
        assert "priority 1001 is above the worker priority ceiling (1000)" in str(
            refused.value
        )
        assert await client.queue_depth(unique_queue) == 0

        # the ceiling itself is claimable, and therefore allowed
        job_id = await client.enqueue(
            "tests.test_client_semantics.CountJob",
            queue=unique_queue,
            priority=1000,
            n=1,
        )
        assert (
            await client.pool.fetchval("SELECT prio FROM jorb WHERE id = $1", job_id)
        ) == 1000

    async def test_a_batch_is_refused_before_any_row_is_written(
        self, client, unique_queue
    ):
        with pytest.raises(ValueError) as refused:
            await client.enqueue_batch(
                [("tests.test_client_semantics.CountJob", {"n": i}) for i in range(50)],
                queue=unique_queue,
                priority=5000,
            )
        assert "above the worker priority ceiling (1000)" in str(refused.value)
        assert await client.queue_depth(unique_queue) == 0

    async def test_changing_a_priority_cannot_hide_a_job_either(
        self, client, unique_queue
    ):
        """The same black hole through a different door: a queued job moved
        above the ceiling is exactly as unclaimable as one enqueued there."""
        job_id = await client.enqueue(
            "tests.test_client_semantics.CountJob",
            queue=unique_queue,
            priority=100,
            n=1,
        )

        with pytest.raises(ValueError):
            await client.update_job_priority(job_id, 2000)
        with pytest.raises(ValueError):
            await client.bulk_update_priority([job_id], 2000)
        assert (
            await client.pool.fetchval("SELECT prio FROM jorb WHERE id = $1", job_id)
        ) == 100

        # a legal move still works
        assert await client.update_job_priority(job_id, 900) is True
        assert (
            await client.pool.fetchval("SELECT prio FROM jorb WHERE id = $1", job_id)
        ) == 900

    async def test_a_declared_ceiling_permits_exactly_what_it_declares(
        self, client, unique_queue
    ):
        """`prio_ceiling` is the deployment saying what its workers run with:
        it moves the line, it does not remove it."""
        loud = JobClient(pool=client.pool, prio_ceiling=5000)
        job_id = await loud.enqueue(
            "tests.test_client_semantics.CountJob",
            queue=unique_queue,
            priority=5000,
            n=1,
        )
        assert (
            await client.pool.fetchval("SELECT prio FROM jorb WHERE id = $1", job_id)
        ) == 5000

        with pytest.raises(ValueError) as refused:
            await loud.enqueue(
                "tests.test_client_semantics.CountJob",
                queue=unique_queue,
                priority=5001,
                n=1,
            )
        assert "above the worker priority ceiling (5000)" in str(refused.value)

        # ...and one call can declare it without changing the client
        one_off = await client.enqueue(
            "tests.test_client_semantics.CountJob",
            queue=unique_queue,
            priority=5000,
            prio_ceiling=5000,
            n=1,
        )
        assert (
            await client.pool.fetchval("SELECT prio FROM jorb WHERE id = $1", one_off)
        ) == 5000

    async def test_the_outbox_path_gets_the_platform_default(
        self, db_params, unique_queue
    ):
        """`enqueue_in_transaction` is static — there is no client to hold a
        declared ceiling, so the platform default applies and the caller's
        transaction never sees an INSERT."""
        conn = await db.connect(**db_params)
        try:
            with pytest.raises(ValueError):
                async with conn.transaction():
                    await JobClient.enqueue_in_transaction(
                        conn,
                        "tests.test_client_semantics.CountJob",
                        queue=unique_queue,
                        priority=5000,
                        n=1,
                    )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM jorb WHERE queue = $1", unique_queue
                )
                == 0
            )
        finally:
            await conn.close()
