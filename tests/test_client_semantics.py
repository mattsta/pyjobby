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

import asyncio

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
        # a pool of our own, closed at the end: a constructor-passed pool is
        # the caller's, so client.close() deliberately leaves it open
        pool = await asyncpg.create_pool(**db_params, min_size=1, max_size=2)
        client = JobClient(pool, db_params=db_params)

        assert await client._ensure_listener() is True
        await client.close()

        before = await db_pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        assert await client._ensure_listener() is False
        assert client._listener_conn is None

        # the wait still degrades safely: no listener is opened (asserted
        # above), and the caller's still-open pool answers the existence
        # probe, which fails fast on a job that is not there
        with pytest.raises(Exception, match="does not exist"):
            await client.wait_for_result(1, timeout=5)

        after = await db_pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        )
        assert after <= before
        await pool.close()

    async def test_close_leaves_a_caller_provided_pool_open(
        self, db_pool, db_params
    ):
        """A pool handed to the constructor is the CALLER's — an application
        routinely shares one pool between its ORM and this client, and
        close() closing it would take the whole process's database access
        down with one client."""
        pool = await asyncpg.create_pool(**db_params, min_size=1, max_size=2)
        try:
            client = JobClient(pool)
            await client.close()
            # the caller's pool still works after the client is gone
            assert await pool.fetchval("SELECT 41 + 1") == 42
        finally:
            await pool.close()

    async def test_close_closes_the_pool_it_created_itself(self, db_params):
        """Pools built by create() are the client's own and ARE closed —
        the caller never saw the pool, so nobody else can close it."""
        client = await JobClient.create(**db_params, min_size=1, max_size=2)
        pool = client.pool
        await client.close()
        with pytest.raises(asyncpg.exceptions.InterfaceError):
            await pool.fetchval("SELECT 1")


class TestEventReads:
    """get_event()/wait_for_event() answer 'never' as an error, not a hang.

    Both default to timeout=None (wait forever), so a condition under which
    the event can never be published must raise — otherwise a stale job id
    blocks a caller forever with no exception and no log line.
    """

    async def test_get_event_on_a_nonexistent_job_fails_fast(
        self, db_pool, client
    ):
        ghost = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('x.Gone', '{}', 'q', 'finished') RETURNING id"""
        )
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", ghost)

        with pytest.raises(Exception, match="does not exist"):
            await client.get_event(ghost, "anything")

    async def test_get_event_on_a_job_that_ended_without_the_key_fails_fast(
        self, db_pool, client, unique_queue
    ):
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, finished)
               VALUES ('x.Done', '{}', $1, 'finished', now()) RETURNING id""",
            unique_queue,
        )

        with pytest.raises(Exception, match="without publishing"):
            await client.get_event(job_id, "never_published")

    async def test_get_event_still_reads_an_event_of_a_finished_job(
        self, db_pool, client, unique_queue
    ):
        """Terminal-with-the-key is the normal late read and must keep
        working: events outlive their publisher on purpose."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, finished)
               VALUES ('x.Done', '{}', $1, 'finished', now()) RETURNING id""",
            unique_queue,
        )
        await db_pool.execute(
            "INSERT INTO jorb_event (job_id, key, value) VALUES ($1, $2, $3)",
            job_id,
            "phase",
            {"at": "end"},
        )

        assert await client.get_event(job_id, "phase") == {"at": "end"}

    async def test_wait_for_event_accepts_a_published_null(
        self, db_pool, client, unique_queue
    ):
        """Row PRESENCE is what 'published' means: set_event(key, None) is a
        legitimate publish, and a waiter keyed on non-null value starved on
        an event that had been there all along."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('x.Running', '{}', $1, 'running') RETURNING id""",
            unique_queue,
        )
        # what set_event(key, None) really writes: jsonb 'null' (the column
        # is NOT NULL), which decodes back to Python None
        await db_pool.execute(
            "INSERT INTO jorb_event (job_id, key, value)"
            " VALUES ($1, $2, 'null'::jsonb)",
            job_id,
            "empty",
        )

        assert await client.wait_for_event(job_id, "empty", timeout=5) is None


class TestEnqueueBatchFidelity:
    """A batched enqueue means exactly what the same single enqueue means.

    The batch used to write six columns and silently drop everything else —
    retry policy, deadline_key, tags — so converting a loop of enqueue()
    calls into a batch (the documented performance advice) changed what the
    jobs meant. Every row now goes through _build_enqueue_row.
    """

    async def test_batch_rows_carry_the_full_option_set(
        self, db_pool, client, unique_queue
    ):
        ids = await client.enqueue_batch(
            [
                ("x.A", {"n": 1}, {"deadline_key": f"{unique_queue}:1"}),
                ("x.B", {"n": 2}, {"priority": 5, "tags": {"tenant": "t2"}}),
            ],
            queue=unique_queue,
            max_retries=3,
            timeout_seconds=120,
            tags={"tenant": "t1"},
        )
        rows = await db_pool.fetch(
            "SELECT * FROM jorb WHERE id = ANY($1) ORDER BY id", ids
        )

        assert [r["job_class"] for r in rows] == ["x.A", "x.B"]
        assert [r["kwargs"] for r in rows] == [{"n": 1}, {"n": 2}]
        assert rows[0]["deadline_key"] == f"{unique_queue}:1"
        assert [r["prio"] for r in rows] == [100, 5]
        assert [r["tags"] for r in rows] == [{"tenant": "t1"}, {"tenant": "t2"}]
        for r in rows:
            assert r["admin_data"]["max_retries"] == 3
            assert r["admin_data"]["timeout_seconds"] == 120
            assert r["admin_data"]["retry_strategy"] == "exponential"

    async def test_batch_dependency_rows_are_inserted_waiting(
        self, db_pool, client, unique_queue
    ):
        upstream = await client.enqueue("x.Up", queue=unique_queue)
        (waiter,) = await client.enqueue_batch(
            [("x.Down", {}, {"waitfor_job": upstream})],
            queue=unique_queue,
        )

        assert (
            await db_pool.fetchval("SELECT state FROM jorb WHERE id = $1", waiter)
        ) == "waiting"

    async def test_batch_payload_keys_never_collide_with_options(
        self, db_pool, client, unique_queue
    ):
        """The batch keeps payload and options in separate namespaces: a job
        whose task() takes an argument named `queue` receives it."""
        (job_id,) = await client.enqueue_batch(
            [("x.A", {"queue": "payload-value", "priority": "mine"})],
            queue=unique_queue,
        )
        row = await db_pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

        assert row["queue"] == unique_queue
        assert row["kwargs"] == {"queue": "payload-value", "priority": "mine"}

    async def test_batch_refuses_a_misspelled_option_by_name(
        self, client, unique_queue
    ):
        """With the payload in its own dict, a leftover keyword can only be
        a misspelled option — refused before any row is written."""
        with pytest.raises(ValueError, match="max_retrys"):
            await client.enqueue_batch(
                [("x.A", {})],
                queue=unique_queue,
                max_retrys=3,
            )


class TestOneCallWorkflows:
    """The request/response shapes users otherwise hand-roll."""

    async def test_run_enqueues_and_returns_the_result(
        self, live_worker, unique_queue, client
    ):
        await live_worker()
        result = await client.run(
            f"{__name__}.CountJob", queue=unique_queue, timeout=20, n=7
        )
        assert result == 7

    async def test_handle_result_waits_like_the_machine_one(
        self, live_worker, unique_queue, client
    ):
        """`await handle.result()` means the same thing on both handle
        kinds now; the non-blocking peek is get_job_result()."""
        await live_worker()
        handle = await client.enqueue_handle(
            f"{__name__}.CountJob", queue=unique_queue, n=3
        )
        assert await handle.result(timeout=20) == 3

    async def test_wait_for_group_returns_when_every_member_finishes(
        self, live_worker, unique_queue, client, db_pool
    ):
        await live_worker()
        leader = await client.enqueue(f"{__name__}.CountJob", queue=unique_queue, n=1)
        await db_pool.execute(
            "UPDATE jorb SET run_group = $1 WHERE id = $1", leader
        )
        await client.enqueue(
            f"{__name__}.CountJob", queue=unique_queue, run_group=leader, n=2
        )

        assert await client.wait_for_group(leader, timeout=20) == 2

    async def test_wait_for_group_raises_when_a_member_fails(
        self, unique_queue, client, db_pool
    ):
        leader = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('x.Gone', '{}', $1, 'crashed') RETURNING id""",
            unique_queue,
        )
        await db_pool.execute(
            "UPDATE jorb SET run_group = $1 WHERE id = $1", leader
        )

        with pytest.raises(Exception, match="cannot finish"):
            await client.wait_for_group(leader, timeout=5)

    async def test_wait_for_group_with_no_members_fails_fast(self, client):
        with pytest.raises(LookupError):
            await client.wait_for_group(2**40, timeout=5)


class TestErrorSurface:
    async def test_job_errors_carry_the_job_id(self, client, db_pool):
        ghost = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('x.Gone', '{}', 'q', 'finished') RETURNING id"""
        )
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", ghost)

        from pyjobby import JobError

        with pytest.raises(JobError) as raised:
            await client.wait_for_result(ghost, timeout=5)
        assert raised.value.job_id == ghost

    async def test_get_jobs_refuses_an_unknown_order_by(self, client):
        """It used to fall back to created-order silently — rows in the
        wrong order with nothing to say so."""
        with pytest.raises(ValueError, match="order_by"):
            await client.get_jobs(order_by="cleverness")

    async def test_get_jobs_accepts_priority_as_the_api_name_for_prio(
        self, client, unique_queue, db_pool
    ):
        low = await client.enqueue("x.A", queue=unique_queue, priority=900)
        high = await client.enqueue("x.B", queue=unique_queue, priority=5)

        rows = await client.get_jobs(
            queue=unique_queue, order_by="priority", ascending=True
        )
        assert [r["id"] for r in rows] == [high, low]


class TestSyncFacadeParity:
    async def test_every_public_async_method_has_a_sync_counterpart(self):
        """SyncJobClient is written out by hand, so it can fall behind.

        Deliberate metaprogramming over the class dictionaries, like the
        SyncMachine mirror test: a method added to JobClient without a sync
        wrapper is invisible until a script author calls it, and scripts
        are exactly the callers least likely to be covered by tests.
        """
        import inspect

        from pyjobby.client import SyncJobClient

        excluded = {
            # takes the CALLER's asyncpg connection mid-transaction — there
            # is no synchronous shape for someone else's async transaction
            "enqueue_in_transaction",
            # JobHandle's methods are coroutines bound to the async client;
            # run()/wait_for_result() are the sync shapes of that workflow
            "enqueue_handle",
        }
        async_public = {
            name
            for name, member in vars(JobClient).items()
            if not name.startswith("_") and inspect.iscoroutinefunction(member)
        }
        sync_names = set(vars(SyncJobClient))
        missing = sorted(async_public - excluded - sync_names)
        assert not missing, (
            f"JobClient methods with no SyncJobClient wrapper: {missing}"
        )

    async def test_from_config_builds_a_working_sync_client(
        self, db_params, tmp_path
    ):
        """Scripts and cron jobs are exactly where a config file lives, and
        the sync facade is the class built for them."""
        from pyjobby.client import SyncJobClient
        from pyjobby.procs import write_config_toml

        config = write_config_toml(tmp_path / "pyjobby.toml", db_params)

        def _drive() -> bool:
            with SyncJobClient.from_config(str(config)) as client:
                return client.health_check()

        assert await asyncio.to_thread(_drive) is True

    async def test_construction_failure_does_not_leak_the_event_loop(self):
        """A bad DSN raises out of __init__, which leaves no object to
        close() — so the loop must be closed before the exception
        propagates, or a retry loop leaks one loop (epoll fd + self-pipe)
        per attempt."""
        from pyjobby.client import SyncJobClient

        def _count_loops() -> int:
            import asyncio as _a
            import gc

            return sum(
                1 for o in gc.get_objects() if isinstance(o, _a.AbstractEventLoop)
            )

        def _attempt() -> None:
            before = _count_loops()
            for _ in range(5):
                with pytest.raises(Exception):
                    SyncJobClient(dsn="postgresql://nobody@127.0.0.1:1/none")
            import gc

            gc.collect()
            after = _count_loops()
            assert after <= before + 1, f"leaked loops: {before} -> {after}"

        await asyncio.to_thread(_attempt)


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
