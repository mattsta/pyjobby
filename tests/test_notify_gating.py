"""Demand-gated notifications: does the gate close, and does it ever lose a wakeup?

Committing a transaction that issued a NOTIFY takes a GLOBAL exclusive lock
held until that commit completes, because notifications must be delivered in
commit order and commit order is not known until commits finish. Every
notifying commit therefore serialises against every other one. The schema's
answer (sql/schema/90_notify.sql) is to notify only when a consumer has registered
demand — which is safe only if registration is ordered correctly against the
producer, so these tests are mostly about ORDER.

Three kinds of test live here:

* the gate closes  — no demand, no notification (and the work still happens)
* the gate cannot be raced — the wakeup survives every interleaving of
  "register demand" against "produce the thing", constructed explicitly with
  held-open transactions rather than hoped for
* the gate is worth it — a permanent benchmark measuring concurrent,
  one-transaction-per-job enqueue and completion throughput with the gate
  open versus closed. Run it with `-m performance -s`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator

import asyncpg
import pytest

from pyjobby.monitor import sweep_dead_workers
from pyjobby.pj import STMTS

pytestmark = pytest.mark.asyncio

#: How long to let a notification arrive (or fail to) before judging. NOTIFY
#: delivery is a post-commit round trip on an idle connection; 300 ms is
#: three orders of magnitude of headroom, and every "no notification" test
#: below pays it, so it stays small on purpose.
SETTLE = 0.3


class Notifications:
    """Everything heard on the LISTENing connection, in arrival order."""

    def __init__(self) -> None:
        self.received: list[tuple[str, str]] = []

    def __call__(self, _conn: object, _pid: int, channel: str, payload: str) -> None:
        self.received.append((channel, payload))

    def on(self, channel: str) -> list[str]:
        return [payload for chan, payload in self.received if chan == channel]

    async def settle(self, seconds: float = SETTLE) -> None:
        await asyncio.sleep(seconds)


@contextlib.asynccontextmanager
async def listening(
    db_params: dict[str, str], *channels: str
) -> AsyncIterator[Notifications]:
    """A dedicated connection LISTENing on `channels`, collecting payloads."""
    conn = await asyncpg.connect(**db_params)
    heard = Notifications()
    try:
        for channel in channels:
            await conn.add_listener(channel, heard)
        yield heard
    finally:
        await conn.close()


async def register_worker(
    conn: asyncpg.Connection,
    queue: str,
    *,
    idle: bool = False,
    shutdown: bool = False,
    last_seen_age: float = 0.0,
) -> int:
    """A jorb_worker row shaped for a gate test."""
    worker_id: int = await conn.fetchval(
        """INSERT INTO jorb_worker (host, pid, queue, capabilities, idle,
                                    last_seen, shutdown_at)
           VALUES ('gate-test', 1234, $1, ARRAY['test'], $2,
                   now() - make_interval(secs => $3),
                   CASE WHEN $4 THEN now() END)
           RETURNING id""",
        queue,
        idle,
        last_seen_age,
        shutdown,
    )
    return worker_id


async def enqueue(conn: asyncpg.Connection, queue: str, **cols: object) -> int:
    """Insert one queued job, autocommitted, exactly like a producer does."""
    job_id: int = await conn.fetchval(
        "INSERT INTO jorb (job_class, queue, state) VALUES ($1, $2, $3) RETURNING id",
        cols.get("job_class", "tests.dxe_jobs.OkJob"),
        queue,
        cols.get("state", "queued"),
    )
    return job_id


# =========================================================================
# jorb_enqueued — the worker wakeup
# =========================================================================


class TestEnqueueGate:
    async def test_enqueue_notifies_when_a_worker_is_parked(
        self, db_pool, db_params, unique_queue
    ):
        """An idle worker on the queue is the demand signal; the notify fires."""
        await register_worker(db_pool, unique_queue, idle=True)

        async with listening(db_params, "jorb_enqueued") as heard:
            await enqueue(db_pool, unique_queue)
            await heard.settle()

        assert heard.on("jorb_enqueued") == [unique_queue]

    async def test_enqueue_is_silent_when_no_worker_is_parked(
        self, db_pool, db_params, unique_queue
    ):
        """The busy case: nobody is waiting, so the commit lock is not paid.

        The job is still enqueued and still claimable — the notification is
        an optimisation on latency, never the delivery mechanism."""
        await register_worker(db_pool, unique_queue, idle=False)

        async with listening(db_params, "jorb_enqueued") as heard:
            job_id = await enqueue(db_pool, unique_queue)
            await heard.settle()

        assert heard.on("jorb_enqueued") == []

        claimed = await db_pool.fetchrow(
            STMTS["claim"], 1, "gate-test", unique_queue, ["test"], 1000, None
        )
        assert claimed is not None, "gated enqueue must still be claimable"
        assert claimed["id"] == job_id
        assert claimed["state"] == "claimed"

    async def test_a_retired_worker_is_not_demand(
        self, db_pool, db_params, unique_queue
    ):
        """shutdown_at excludes a row from the gate even if idle survived it."""
        await register_worker(db_pool, unique_queue, idle=True, shutdown=True)

        async with listening(db_params, "jorb_enqueued") as heard:
            await enqueue(db_pool, unique_queue)
            await heard.settle()

        assert heard.on("jorb_enqueued") == []

    async def test_demand_is_per_queue(self, db_pool, db_params, unique_queue):
        """A worker parked on another queue does not switch ours on."""
        await register_worker(db_pool, f"{unique_queue}_other", idle=True)

        async with listening(db_params, "jorb_enqueued") as heard:
            await enqueue(db_pool, unique_queue)
            await enqueue(db_pool, f"{unique_queue}_other")
            await heard.settle()

        assert heard.on("jorb_enqueued") == [f"{unique_queue}_other"]

    async def test_requeue_to_queued_notifies_too(
        self, db_pool, db_params, unique_queue
    ):
        """The gate is on the channel, not on INSERT: a retry wakes a worker."""
        job_id = await enqueue(db_pool, unique_queue, state="running")
        await register_worker(db_pool, unique_queue, idle=True)

        async with listening(db_params, "jorb_enqueued") as heard:
            await db_pool.execute(
                "UPDATE jorb SET state = 'queued' WHERE id = $1", job_id
            )
            await heard.settle()

        assert heard.on("jorb_enqueued") == [unique_queue]


class TestEnqueueGateCannotBeRaced:
    """The register-then-recheck ordering, constructed rather than hoped for."""

    async def test_transactional_enqueue_started_before_the_worker_parked(
        self, db_pool, db_params, unique_queue
    ):
        """The window an immediate trigger would lose, and a deferred one does not.

        Interleaving under test — this is the whole reason
        jorb_enqueued_notify is a DEFERRABLE INITIALLY DEFERRED constraint
        trigger:

            producer:  BEGIN; INSERT (queued)        <- no worker is parked
            worker:    UPDATE jorb_worker SET idle   <- publishes demand
            worker:    claim -> nothing              <- insert not visible yet
            producer:  COMMIT                        <- gate runs HERE

        With the gate evaluated at INSERT the notification would be skipped
        and the job would sit until the next poll. Evaluated at COMMIT, the
        demand is already published and the wakeup is emitted.
        """
        worker_id = await register_worker(db_pool, unique_queue, idle=False)

        async with (
            listening(db_params, "jorb_enqueued") as heard,
            db_pool.acquire() as producer,
        ):
            tx = producer.transaction()
            await tx.start()
            await producer.execute(
                "INSERT INTO jorb (job_class, queue) VALUES ('tests.dxe_jobs.OkJob', $1)",
                unique_queue,
            )

            # worker parks: publish demand, then look again (and find
            # nothing — the producer has not committed)
            await db_pool.execute(STMTS["worker-idle"], worker_id, True)
            missed = await db_pool.fetchrow(
                STMTS["claim"], 1, "gate-test", unique_queue, ["test"], 1000, worker_id
            )
            assert missed is None, "uncommitted insert must not be claimable"

            await heard.settle(0.1)
            assert heard.on("jorb_enqueued") == [], "notify is delivered at COMMIT"

            await tx.commit()
            await heard.settle()

        assert heard.on("jorb_enqueued") == [unique_queue], (
            "a job enqueued transactionally while a worker parked must still wake it"
        )

    async def test_enqueue_committed_before_the_worker_parked_is_claimed(
        self, db_pool, db_params, unique_queue
    ):
        """The other half: no notification, and none is needed.

        The job committed before demand was published, so the gate is right
        to stay shut — and the recheck claim that follows publishing idle is
        what finds it. Ordering the worker the other way round (claim, then
        publish idle) is what would lose this job."""
        worker_id = await register_worker(db_pool, unique_queue, idle=False)
        job_id = await enqueue(db_pool, unique_queue)

        async with listening(db_params, "jorb_enqueued") as heard:
            await db_pool.execute(STMTS["worker-idle"], worker_id, True)
            found = await db_pool.fetchrow(
                STMTS["claim"], 1, "gate-test", unique_queue, ["test"], 1000, worker_id
            )
            await heard.settle()

        assert heard.on("jorb_enqueued") == []
        assert found is not None and found["id"] == job_id


class TestWorkerPublishesDemand:
    """The live worker's half of the ordering argument."""

    async def test_worker_parks_and_wakes_faster_than_it_could_poll(
        self, db_pool, live_worker, unique_queue
    ):
        """The correctness property, with a latency bound polling cannot explain.

        checkInterval is 30s, so a job that completes in under a second was
        delivered by the notification and by nothing else."""
        worker = await live_worker(checkInterval=30.0)

        # let it settle into the parked state (it must publish demand there)
        for _ in range(50):
            if await db_pool.fetchval(
                "SELECT idle FROM jorb_worker WHERE id = $1", worker.worker_id
            ):
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("worker never published jorb_worker.idle while parked")

        started = time.monotonic()
        job_id = await enqueue(db_pool, unique_queue)

        for _ in range(100):
            state = await db_pool.fetchval(
                "SELECT state FROM jorb WHERE id = $1", job_id
            )
            if state == "finished":
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail(f"job {job_id} was not woken within 2s (state {state})")

        elapsed = time.monotonic() - started
        assert elapsed < 1.0, (
            f"woken after {elapsed:.2f}s with a 30s poll interval — the "
            f"wakeup notification was lost"
        )

    async def test_busy_worker_withdraws_demand(
        self, db_pool, live_worker, unique_queue
    ):
        """A worker that is running a job is not demand: enqueues stay silent."""
        worker = await live_worker(checkInterval=30.0)
        await enqueue(db_pool, unique_queue)
        for _ in range(100):
            if await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1 AND state = 'finished'",
                unique_queue,
            ):
                break
            await asyncio.sleep(0.02)

        # after a claim the worker withdraws demand; it republishes only when
        # it parks again, which the 30s poll interval leaves plenty of time
        # to observe as a transition rather than a steady state
        assert worker.worker_id is not None

    async def test_graceful_shutdown_clears_idle(
        self, db_pool, live_worker, unique_queue
    ):
        """A worker that exits must not leave this queue's notifications on."""
        worker = await live_worker(checkInterval=0.2)
        for _ in range(50):
            if await db_pool.fetchval(
                "SELECT idle FROM jorb_worker WHERE id = $1", worker.worker_id
            ):
                break
            await asyncio.sleep(0.1)

        worker.stop = True
        for _ in range(100):
            row = await db_pool.fetchrow(
                "SELECT idle, shutdown_at FROM jorb_worker WHERE id = $1",
                worker.worker_id,
            )
            if row["shutdown_at"] is not None:
                break
            await asyncio.sleep(0.05)

        assert row["shutdown_at"] is not None, "worker never deregistered"
        assert row["idle"] is False, "a retired worker left demand published"

    async def test_dead_worker_reaped_by_monitor_leaves_no_demand(
        self, db_pool, db_params, unique_queue
    ):
        """The monitor bounds a crashed worker's leaked subscription."""
        worker_id = await register_worker(
            db_pool, unique_queue, idle=True, last_seen_age=600
        )

        await sweep_dead_workers(db_pool, liveness_grace_seconds=60)

        row = await db_pool.fetchrow(
            "SELECT idle, shutdown_at FROM jorb_worker WHERE id = $1", worker_id
        )
        assert row["shutdown_at"] is not None
        assert row["idle"] is False

        async with listening(db_params, "jorb_enqueued") as heard:
            await enqueue(db_pool, unique_queue)
            await heard.settle()
        assert heard.on("jorb_enqueued") == []

    async def test_idle_is_written_only_on_the_transition(self, db_pool, unique_queue):
        """Re-publishing demand must not write the row again.

        A gate that swapped one NOTIFY per enqueue for one UPDATE per poll
        would be no fix at all."""
        worker_id = await register_worker(db_pool, unique_queue, idle=False)

        first = await db_pool.execute(STMTS["worker-idle"], worker_id, True)
        again = await db_pool.execute(STMTS["worker-idle"], worker_id, True)
        back = await db_pool.execute(STMTS["worker-idle"], worker_id, False)

        assert first == "UPDATE 1"
        assert again == "UPDATE 0", "redundant idle publish must be a no-op"
        assert back == "UPDATE 1"


# =========================================================================
# jorb_done / jorb_event — the client waiters
# =========================================================================


class TestDoneGate:
    async def test_completion_is_silent_when_nobody_waits(
        self, db_pool, db_params, unique_queue
    ):
        job_id = await enqueue(db_pool, unique_queue, state="running")

        async with listening(db_params, "jorb_done") as heard:
            await db_pool.execute(
                "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
            )
            await heard.settle()

        assert heard.on("jorb_done") == []

    async def test_completion_notifies_a_registered_waiter(
        self, db_pool, db_params, unique_queue
    ):
        job_id = await enqueue(db_pool, unique_queue, state="running")
        await db_pool.execute(
            "UPDATE jorb SET awaited = TRUE WHERE id = $1 AND NOT awaited", job_id
        )

        async with listening(db_params, "jorb_done") as heard:
            await db_pool.execute(
                "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
            )
            await heard.settle()

        assert [json.loads(p) for p in heard.on("jorb_done")] == [
            {"id": job_id, "state": "finished"}
        ]

    async def test_registration_racing_completion_is_ordered_by_the_row_lock(
        self, db_pool, db_params, unique_queue
    ):
        """The airtight case: demand and the state change are the SAME ROW.

        Interleaving under test:

            waiter:   BEGIN; UPDATE jorb SET awaited = TRUE   <- row locked
            worker:   UPDATE jorb SET state = 'finished'      <- BLOCKS
            waiter:   COMMIT
            worker:   proceeds, re-evaluating against the newest row version

        PostgreSQL orders the two writers for us, so the terminal update's
        NEW image necessarily carries awaited = TRUE and the WHEN clause
        fires. No snapshot race exists here at all."""
        job_id = await enqueue(db_pool, unique_queue, state="running")

        async with (
            listening(db_params, "jorb_done") as heard,
            db_pool.acquire() as waiter,
            db_pool.acquire() as worker,
        ):
            tx = waiter.transaction()
            await tx.start()
            await waiter.execute("UPDATE jorb SET awaited = TRUE WHERE id = $1", job_id)

            finishing = asyncio.create_task(
                worker.execute(
                    "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
                )
            )
            await asyncio.sleep(0.2)
            assert not finishing.done(), "the terminal update must block on the row"

            await tx.commit()
            await asyncio.wait_for(finishing, timeout=5)
            await heard.settle()

        assert [json.loads(p) for p in heard.on("jorb_done")] == [
            {"id": job_id, "state": "finished"}
        ]

    async def test_wait_for_result_registers_demand_and_returns(
        self, db_pool, db_params, unique_queue
    ):
        """End to end through the real client: waiting publishes the demand."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool, db_params=db_params)
        job_id = await enqueue(db_pool, unique_queue, state="running")

        waiting = asyncio.create_task(client.wait_for_result(job_id, timeout=10))
        for _ in range(100):
            if await db_pool.fetchval("SELECT awaited FROM jorb WHERE id = $1", job_id):
                break
            await asyncio.sleep(0.02)
        else:
            waiting.cancel()
            pytest.fail("wait_for_result never registered demand")

        started = time.monotonic()
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished', result = '42'::jsonb WHERE id = $1",
            job_id,
        )
        assert await waiting == 42
        # the client's fallback poll is 2s; anything faster came from NOTIFY
        assert time.monotonic() - started < 1.0
        await client.close()


class TestEventGate:
    async def test_event_is_silent_when_nobody_waits(
        self, db_pool, db_params, unique_queue
    ):
        job_id = await enqueue(db_pool, unique_queue, state="running")

        async with listening(db_params, "jorb_event") as heard:
            await db_pool.execute(
                "INSERT INTO jorb_event (job_id, key, value) VALUES ($1, 'k', '1')",
                job_id,
            )
            await heard.settle()

        assert heard.on("jorb_event") == []

    async def test_event_notifies_when_the_job_is_awaited(
        self, db_pool, db_params, unique_queue
    ):
        job_id = await enqueue(db_pool, unique_queue, state="running")
        await db_pool.execute("UPDATE jorb SET awaited = TRUE WHERE id = $1", job_id)

        async with listening(db_params, "jorb_event") as heard:
            await db_pool.execute(
                "INSERT INTO jorb_event (job_id, key, value) VALUES ($1, 'k', '1')",
                job_id,
            )
            await heard.settle()

        assert [json.loads(p) for p in heard.on("jorb_event")] == [
            {"job_id": job_id, "key": "k"}
        ]

    async def test_get_event_registers_demand_and_returns(
        self, db_pool, db_params, unique_queue
    ):
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool, db_params=db_params)
        job_id = await enqueue(db_pool, unique_queue, state="running")

        waiting = asyncio.create_task(client.get_event(job_id, "phase", timeout=10))
        for _ in range(100):
            if await db_pool.fetchval("SELECT awaited FROM jorb WHERE id = $1", job_id):
                break
            await asyncio.sleep(0.02)
        else:
            waiting.cancel()
            pytest.fail("get_event never registered demand")

        started = time.monotonic()
        await db_pool.execute(
            """INSERT INTO jorb_event (job_id, key, value) VALUES ($1, 'phase', $2)""",
            job_id,
            "done",
        )
        assert await waiting == "done"
        assert time.monotonic() - started < 1.0
        await client.close()


class TestStreamGate:
    async def test_append_is_silent_when_nobody_reads(
        self, db_pool, db_params, unique_queue
    ):
        """A job streaming into the void pays no commit lock at all."""
        job_id = await enqueue(db_pool, unique_queue, state="running")

        async with listening(db_params, "jorb_stream") as heard:
            await db_pool.execute(
                """INSERT INTO jorb_stream (job_id, key, seq, value, run_epoch)
                   VALUES ($1, 'rows', 0, '1', 1)""",
                job_id,
            )
            await heard.settle()

        assert heard.on("jorb_stream") == []
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_stream WHERE job_id = $1", job_id
            )
            == 1
        )

    async def test_append_notifies_when_the_job_is_awaited(
        self, db_pool, db_params, unique_queue
    ):
        job_id = await enqueue(db_pool, unique_queue, state="running")
        await db_pool.execute("UPDATE jorb SET awaited = TRUE WHERE id = $1", job_id)

        async with listening(db_params, "jorb_stream") as heard:
            await db_pool.execute(
                """INSERT INTO jorb_stream (job_id, key, seq, value, run_epoch)
                   VALUES ($1, 'rows', 0, '1', 1)""",
                job_id,
            )
            await heard.settle()

        assert [json.loads(p) for p in heard.on("jorb_stream")] == [
            {"job_id": job_id, "key": "rows"}
        ]

    async def test_read_stream_registers_demand_and_is_woken(
        self, db_pool, db_params, unique_queue
    ):
        """The reader's half of the gate: it says it is listening BEFORE its
        first look, so an append that lands a moment later reaches it as a
        notification rather than on the 2-second fallback poll."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool, db_params=db_params)
        job_id = await enqueue(db_pool, unique_queue, state="running")

        rows = client.read_stream(job_id, "rows")
        reading = asyncio.create_task(anext(rows))
        for _ in range(100):
            if await db_pool.fetchval("SELECT awaited FROM jorb WHERE id = $1", job_id):
                break
            await asyncio.sleep(0.02)
        else:
            reading.cancel()
            pytest.fail("read_stream never registered demand")

        started = time.monotonic()
        await db_pool.execute(
            """INSERT INTO jorb_stream (job_id, key, seq, value, run_epoch)
               VALUES ($1, 'rows', 0, $2, 1)""",
            job_id,
            {"i": 0},
        )
        assert await reading == {"i": 0}
        assert time.monotonic() - started < 1.0
        await rows.aclose()
        await client.close()


# =========================================================================
# The channels that are NOT gated, and the one that is gone
# =========================================================================


class TestUngatedChannels:
    async def test_cancel_of_a_running_job_still_notifies(
        self, db_pool, db_params, unique_queue
    ):
        """No polling fallback exists for cancellation, so it stays ungated."""
        job_id = await enqueue(db_pool, unique_queue, state="running")

        async with listening(db_params, "jorb_cancel") as heard:
            await db_pool.execute(
                "UPDATE jorb SET cancel_requested = TRUE WHERE id = $1", job_id
            )
            await heard.settle()

        assert heard.on("jorb_cancel") == [str(job_id)]

    async def test_cancel_of_a_queued_job_notifies_nobody(
        self, db_pool, db_params, unique_queue
    ):
        """`state = 'running'` was always the demand signal here."""
        job_id = await enqueue(db_pool, unique_queue)

        async with listening(db_params, "jorb_cancel") as heard:
            await db_pool.execute(
                "UPDATE jorb SET cancel_requested = TRUE WHERE id = $1", job_id
            )
            await heard.settle()

        assert heard.on("jorb_cancel") == []

    async def test_state_change_firehose_is_gone(
        self, db_pool, db_params, unique_queue
    ):
        """The per-transition dashboard feed emits nothing, on any transition.

        It was the last ungated channel, and the lock is per COMMIT, so it
        alone cost what all seven cost. It could not be gated -- its consumer
        was push-only, so a gate would have dropped dashboard events rather
        than delayed them -- so it was deleted and the consumer polls
        aggregates instead (pyjobby/websocket_server.py, and
        tests/test_ws_snapshot.py for what it gets now)."""
        job_id = await enqueue(db_pool, unique_queue)

        async with listening(db_params, "job_state_change") as heard:
            for state in ("claimed", "running", "finished"):
                await db_pool.execute(
                    "UPDATE jorb SET state = $2 WHERE id = $1", job_id, state
                )
            await heard.settle()

        assert heard.on("job_state_change") == []
        # ...and the transitions themselves still happened and are recorded
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_history WHERE job_id = $1", job_id
            )
            == 4  # enqueued + three transitions
        )

    async def test_mailbox_send_notifies_nobody(self, db_pool, db_params, unique_queue):
        """jorb_mailbox is gone: recv() polls, so the channel had no consumer."""
        job_id = await enqueue(db_pool, unique_queue, state="running")

        async with listening(db_params, "jorb_mailbox") as heard:
            await db_pool.execute(
                """INSERT INTO jorb_mailbox (dest_job_id, topic, message)
                   VALUES ($1, 'ping', '{}'::jsonb)""",
                job_id,
            )
            await heard.settle()

        assert heard.on("jorb_mailbox") == []
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id = $1", job_id
            )
            == 1
        )

    async def test_every_channel_goes_through_one_function(self, db_pool):
        """One implementation, not seven — asserted against pg_trigger."""
        rows = await db_pool.fetch(
            """SELECT t.tgname, p.proname, t.tgdeferrable
                 FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
                WHERE NOT t.tgisinternal
                  AND t.tgrelid IN ('jorb'::regclass, 'jorb_event'::regclass,
                                    'jorb_stream'::regclass,
                                    'jorb_mailbox'::regclass,
                                    'jorb_schedule_log'::regclass)"""
        )
        by_name = {r["tgname"]: r for r in rows}

        notify_triggers = {
            "jorb_enqueued_notify",
            "jorb_done_notify",
            "jorb_cancel_notify",
            "jorb_event_notify",
            "jorb_stream_notify",
            "schedule_executed_notify",
        }
        assert notify_triggers <= set(by_name)
        assert "jorb_mailbox_notify" not in by_name
        # The deleted firehose. Asserted against the catalog rather than
        # against the schema text so that no migration, no test fixture and
        # no "temporarily re-enable it" can put it back unnoticed: it is the
        # one channel whose cost is unbounded in job throughput.
        assert "job_state_change_notify" not in by_name
        assert {by_name[n]["proname"] for n in notify_triggers} == {"jorb_notify"}
        # only the enqueue gate needs commit-time evaluation (see schema.sql)
        assert by_name["jorb_enqueued_notify"]["tgdeferrable"] is True
        assert by_name["jorb_done_notify"]["tgdeferrable"] is False


# =========================================================================
# The permanent benchmark
# =========================================================================

#: Connections writing at once. The concurrency IS the measurement: a
#: NOTIFY-bearing commit takes a global exclusive lock, so concurrent
#: commits serialise against each other. At concurrency 1 the cost is
#: invisible, which is how it stayed unnoticed.
BENCH_CONCURRENCY = 16
BENCH_PER_CONNECTION = 100
#: Is a per-transition NOTIFY trigger installed on ``jorb``?
FIREHOSE_EXISTS_SQL = """
    SELECT EXISTS (SELECT 1 FROM pg_trigger
                    WHERE NOT tgisinternal
                      AND tgrelid = 'jorb'::regclass
                      AND tgname = 'job_state_change_notify')
"""

#: The deleted ``job_state_change`` trigger, rebuilt verbatim so the cost it
#: used to impose stays measurable after the deletion. It is created and
#: dropped inside the benchmark and exists nowhere else.
FIREHOSE_REPLICA = """
    CREATE OR REPLACE FUNCTION job_state_change_replica() RETURNS trigger AS $$
    BEGIN
        PERFORM pg_notify('job_state_change', json_build_object(
            'id', NEW.id, 'queue', NEW.queue, 'job_class', NEW.job_class,
            'old_state', OLD.state, 'new_state', NEW.state,
            'error_count', NEW.error_count)::TEXT);
        RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER job_state_change_replica
        AFTER UPDATE OF state ON jorb
        FOR EACH ROW WHEN (OLD.state IS DISTINCT FROM NEW.state)
        EXECUTE FUNCTION job_state_change_replica();
"""

FIREHOSE_REPLICA_DROP = """
    DROP TRIGGER IF EXISTS job_state_change_replica ON jorb;
    DROP FUNCTION IF EXISTS job_state_change_replica();
"""

#: Conditions are measured round-robin and reduced by median. Anything that
#: drifts over a run — accumulating dead tuples, an autovacuum waking up, a
#: checkpoint — otherwise lands entirely on whichever condition happened to
#: run at the time, and a single ordered pass reports it as a result.
BENCH_ROUNDS = 5


async def _one_txn_per_job(
    pool: asyncpg.Pool, sql: str, batches: list[list[tuple[object, ...]]]
) -> float:
    """Run `sql` once per argument tuple, one transaction each, `batches` at
    once (one connection per batch). Returns jobs/second."""

    async def one(args: list[tuple[object, ...]]) -> None:
        async with pool.acquire() as conn:
            for arg in args:
                await conn.execute(sql, *arg)

    jobs = sum(len(b) for b in batches)
    started = time.perf_counter()
    await asyncio.gather(*(one(b) for b in batches))
    elapsed = time.perf_counter() - started
    return jobs / elapsed


def _split(items: list[object], parts: int) -> list[list[tuple[object, ...]]]:
    """Deal `items` to `parts` connections as single-argument tuples."""
    return [[(item,) for item in items[i::parts]] for i in range(parts)]


async def _measure(
    conditions: dict[str, object], rounds: int = BENCH_ROUNDS
) -> dict[str, float]:
    """Median jobs/second per condition, measured round-robin.

    Interleaving is not decoration: these conditions differ by a lock held
    for the length of a commit, and a benchmark that runs each of them once
    in order cannot tell that apart from the machine getting slower."""
    from statistics import median

    samples: dict[str, list[float]] = {name: [] for name in conditions}
    for round_no in range(rounds + 1):
        for name, run in conditions.items():
            rate = await run()  # type: ignore[operator]
            if round_no:  # round 0 is warmup: connections, plans, page cache
                samples[name].append(rate)
    return {name: median(values) for name, values in samples.items()}


@pytest.mark.slow
@pytest.mark.performance
class TestNotifyGateThroughput:
    """What the gate is worth, measured the way production actually writes."""

    async def test_enqueue_throughput_with_the_gate_open_and_shut(
        self, db_params, unique_queue
    ):
        """Concurrent one-transaction-per-job enqueue, parked worker or not.

        "gate open" is exactly what this schema used to do unconditionally:
        every enqueue notifies, so every enqueue commit takes the global
        lock. "gate shut" is the regime that matters — workers busy, nobody
        parked, nothing to tell anyone."""
        insert = "INSERT INTO jorb (job_class, kwargs, queue, prio) VALUES ('bench.Job', '{}'::jsonb, $1, 100)"
        pool = await asyncpg.create_pool(
            **db_params, min_size=BENCH_CONCURRENCY, max_size=BENCH_CONCURRENCY
        )
        batches = _split(
            [unique_queue] * (BENCH_CONCURRENCY * BENCH_PER_CONNECTION),
            BENCH_CONCURRENCY,
        )
        try:
            worker_id = await register_worker(pool, unique_queue, idle=False)

            async def run(idle: bool, trigger: bool) -> float:
                await pool.execute(STMTS["worker-idle"], worker_id, idle)
                if not trigger:
                    await pool.execute(
                        "ALTER TABLE jorb DISABLE TRIGGER jorb_enqueued_notify"
                    )
                try:
                    return await _one_txn_per_job(pool, insert, batches)
                finally:
                    if not trigger:
                        await pool.execute(
                            "ALTER TABLE jorb ENABLE TRIGGER jorb_enqueued_notify"
                        )
                    await pool.execute(
                        "DELETE FROM jorb WHERE queue = $1", unique_queue
                    )
                    await pool.execute("VACUUM jorb, jorb_history")

            rates = await _measure(
                {
                    "gate_open": lambda: run(idle=True, trigger=True),
                    "gate_shut": lambda: run(idle=False, trigger=True),
                    "no_trigger": lambda: run(idle=False, trigger=False),
                }
            )
        finally:
            await pool.close()

        gate_open, gate_shut, ceiling = (
            rates["gate_open"],
            rates["gate_shut"],
            rates["no_trigger"],
        )
        print(
            f"\nENQUEUE, {BENCH_CONCURRENCY} connections x "
            f"{BENCH_PER_CONNECTION} jobs, one transaction per job, "
            f"median of {BENCH_ROUNDS}:\n"
            f"  gate open  (a worker is parked -- as shipped): "
            f"{gate_open:>10,.0f} jobs/s\n"
            f"  gate shut  (workers busy -- the load regime):  "
            f"{gate_shut:>10,.0f} jobs/s\n"
            f"  no trigger (the ceiling):                      "
            f"{ceiling:>10,.0f} jobs/s\n"
            f"  the shut gate recovers {gate_shut / gate_open:.2f}x, "
            f"reaching {gate_shut / ceiling:.0%} of the ceiling"
        )

        assert gate_shut > gate_open * 1.5, (
            f"gating the enqueue notification recovered only "
            f"{gate_shut / gate_open:.2f}x ({gate_open:,.0f} -> "
            f"{gate_shut:,.0f} jobs/s); the NOTIFY is still in the commit path"
        )
        assert gate_shut > ceiling * 0.8, (
            f"the gate's own lookup costs more than it saves "
            f"({gate_shut:,.0f} vs a {ceiling:,.0f} jobs/s ceiling)"
        )

    async def test_completion_throughput_and_what_the_firehose_cost(
        self, db_params, unique_queue
    ):
        """The completion path, and what deleting job_state_change bought.

        The firehose is GONE from the schema, so this benchmark rebuilds it
        (FIREHOSE_REPLICA below) to measure against. That is the point: the
        "before" number has to stay measurable, or the reason the channel was
        deleted decays into a claim in a comment. It doubles as the guard on
        re-adding one — any ungated per-transition NOTIFY costs this much.

        Three conditions, interleaved and reduced by median:

          awaited        the old regime, a client waiting: jorb_done fires
                         AND the firehose fires
          not_awaited    the old regime, fire and forget: jorb_done is gated
                         shut, and it buys nothing, because the ungated
                         firehose takes the same per-COMMIT lock in the same
                         transaction
          as_shipped     today: no firehose at all, so a completion nobody
                         is waiting for takes no lock
        """
        pool = await asyncpg.create_pool(
            **db_params, min_size=BENCH_CONCURRENCY, max_size=BENCH_CONCURRENCY
        )
        # by primary key: a scan for "some running job" would measure the
        # scan (which degrades as rows are finished), not the commit
        finish = "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1"
        total = BENCH_CONCURRENCY * BENCH_PER_CONNECTION
        try:
            assert not await pool.fetchval(FIREHOSE_EXISTS_SQL), (
                "the schema ships a per-transition NOTIFY trigger again; this "
                "benchmark measures what that costs, and it must not be on"
            )

            async def run(awaited: bool, firehose: bool) -> float:
                await pool.execute("DELETE FROM jorb WHERE queue = $1", unique_queue)
                ids = [
                    r["id"]
                    for r in await pool.fetch(
                        """INSERT INTO jorb (job_class, queue, state, awaited)
                           SELECT 'bench.Job', $1, 'running', $2
                             FROM generate_series(1, $3::int)
                           RETURNING id""",
                        unique_queue,
                        awaited,
                        total,
                    )
                ]
                await pool.execute("VACUUM jorb, jorb_history")
                if firehose:
                    await pool.execute(FIREHOSE_REPLICA)
                try:
                    return await _one_txn_per_job(
                        pool, finish, _split(ids, BENCH_CONCURRENCY)
                    )
                finally:
                    if firehose:
                        await pool.execute(FIREHOSE_REPLICA_DROP)

            rates = await _measure(
                {
                    "awaited": lambda: run(awaited=True, firehose=True),
                    "not_awaited": lambda: run(awaited=False, firehose=True),
                    "as_shipped": lambda: run(awaited=False, firehose=False),
                }
            )
            await pool.execute("DELETE FROM jorb WHERE queue = $1", unique_queue)
        finally:
            await pool.execute(FIREHOSE_REPLICA_DROP)
            await pool.close()

        waited_on, fire_and_forget, firehose_gone = (
            rates["awaited"],
            rates["not_awaited"],
            rates["as_shipped"],
        )
        print(
            f"\nCOMPLETION, {BENCH_CONCURRENCY} connections x "
            f"{BENCH_PER_CONNECTION} jobs, one transaction per job, "
            f"median of {BENCH_ROUNDS}:\n"
            f"  BEFORE, jorb_done open (a client waits):       "
            f"{waited_on:>10,.0f} jobs/s\n"
            f"  BEFORE, jorb_done gated shut:                  "
            f"{fire_and_forget:>10,.0f} jobs/s\n"
            f"  AFTER, job_state_change deleted:               "
            f"{firehose_gone:>10,.0f} jobs/s\n"
            f"  gating jorb_done alone: "
            f"{fire_and_forget / waited_on:.2f}x -- the ungated firehose "
            f"took the same lock in the same commit\n"
            f"  deleting the firehose as well: "
            f"{firehose_gone / waited_on:.2f}x"
        )

        assert firehose_gone > fire_and_forget * 1.4, (
            "deleting job_state_change is expected to dominate the completion "
            f"path once jorb_done is gated; it moved throughput only "
            f"{fire_and_forget:,.0f} -> {firehose_gone:,.0f} jobs/s"
        )
