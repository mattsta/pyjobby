"""Client-library tests for the schema-v1 DXE surface.

Covers wait_for_result (LISTEN + polling modes), get_event, send_message,
JobHandle, the transactional-outbox enqueue_in_transaction, the typed @job
registry, and the SyncJobClient facade. Live-worker tests drive REAL
JobSystem workers via the ``live_worker`` fixture and real jobs from
``tests.dxe_jobs``.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from pyjobby import (
    Job,
    JobCancelledError,
    JobClient,
    JobFailedError,
    JobHandle,
    SyncJobClient,
    db,
    job,
)
from pyjobby.registry import registry

from .conftest import wait_for_job_state

# ---------------------------------------------------------------------------
# Registered jobs for the typed-registry tests (module scope so their dotted
# paths are stable: tests.test_dxe_client.<Name>)
# ---------------------------------------------------------------------------


@job
class RegisteredAdd(Job):
    """@job-decorated Job subclass exercising typed enqueue validation."""

    async def task(self, a: int, b: int = 1) -> dict[str, Any]:
        return {"sum": a + b}


@job
async def registered_double(x: int) -> dict[str, Any]:
    """Plain async function wrapped into a generated Job subclass."""
    return {"doubled": x * 2}


# ---------------------------------------------------------------------------
# Client fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def listen_client(db_pool, db_params):
    """JobClient with LISTEN support (shared listener connection)."""
    client = JobClient(db_pool, db_params=db_params)
    yield client
    await client.close()


@pytest_asyncio.fixture
async def poll_client(db_pool):
    """Pool-only JobClient: no db_params, so waits are pure polling."""
    return JobClient(db_pool)


# ---------------------------------------------------------------------------
# wait_for_result
# ---------------------------------------------------------------------------


async def test_wait_for_result_returns_result(listen_client, live_worker, unique_queue):
    await live_worker()

    job_id = await listen_client.enqueue(
        "tests.dxe_jobs.OkJob", queue=unique_queue, x=21
    )
    result = await listen_client.wait_for_result(job_id, timeout=20)
    assert result == {"doubled": 42}


async def test_wait_for_result_pure_polling_mode(
    poll_client, live_worker, unique_queue
):
    """A pool-only client (no db_params) still waits, via polling."""
    await live_worker()

    job_id = await poll_client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=2)
    result = await poll_client.wait_for_result(job_id, timeout=20)
    assert result == {"doubled": 4}


async def test_wait_for_result_crashed_raises(listen_client, live_worker, unique_queue):
    await live_worker()

    job_id = await listen_client.enqueue(
        "tests.dxe_jobs.FailJob",
        queue=unique_queue,
        max_retries=1,
        initial_retry_delay=0,
    )
    with pytest.raises(JobFailedError) as excinfo:
        await listen_client.wait_for_result(job_id, timeout=20)
    assert excinfo.value.job_id == job_id
    assert "intentional failure" in (excinfo.value.error_message or "")


# ---------------------------------------------------------------------------
# JobHandle
# ---------------------------------------------------------------------------


async def test_handle_wait_cancel_roundtrip(
    listen_client, live_worker, unique_queue, db_pool
):
    await live_worker()

    handle = await listen_client.enqueue_handle(
        "tests.dxe_jobs.SlowJob", queue=unique_queue, seconds=30
    )
    assert isinstance(handle, JobHandle)

    await wait_for_job_state(db_pool, handle.id, ("running",))
    result = await handle.cancel()
    assert result["job_id"] == handle.id
    assert result["status"] in ("cancelled", "cancel_requested")

    with pytest.raises(JobCancelledError):
        await handle.wait(timeout=20)
    assert await handle.status() == "cancelled"


# ---------------------------------------------------------------------------
# get_event / send_message
# ---------------------------------------------------------------------------


async def test_get_event_returns_published_value(
    listen_client, live_worker, unique_queue
):
    await live_worker()

    job_id = await listen_client.enqueue(
        "tests.dxe_jobs.SleeperJob", queue=unique_queue, seconds=1
    )
    value = await listen_client.get_event(job_id, "phase", timeout=20)
    assert value["at"] in ("before-sleep", "after-sleep")


async def test_get_event_times_out(poll_client, unique_queue):
    # no worker on the queue: the event is never published
    job_id = await poll_client.enqueue("tests.dxe_jobs.SleeperJob", queue=unique_queue)
    with pytest.raises(TimeoutError):
        await poll_client.get_event(job_id, "phase", timeout=0.8)


async def test_send_message_reaches_receiving_job(
    listen_client, live_worker, unique_queue, db_pool
):
    await live_worker()

    job_id = await listen_client.enqueue(
        "tests.dxe_jobs.PongJob", queue=unique_queue, timeout=20
    )
    await wait_for_job_state(db_pool, job_id, ("running",))
    await listen_client.send_message(job_id, {"ping": True}, topic="game")

    result = await listen_client.wait_for_result(job_id, timeout=20)
    assert result == {"got": {"ping": True}}


# ---------------------------------------------------------------------------
# Transactional outbox: enqueue_in_transaction
# ---------------------------------------------------------------------------


async def test_enqueue_in_transaction_commit_and_rollback(
    db_params, db_pool, unique_queue
):
    conn = await db.connect(**db_params)
    try:
        # commits with the caller's transaction
        async with conn.transaction():
            committed_id = await JobClient.enqueue_in_transaction(
                conn, "tests.dxe_jobs.OkJob", queue=unique_queue, x=1
            )
        row = await db_pool.fetchrow(
            "SELECT state, queue, kwargs FROM jorb WHERE id = $1", committed_id
        )
        assert row is not None
        assert row["state"] == "queued"
        assert row["queue"] == unique_queue
        assert row["kwargs"] == {"x": 1}

        # rolls back with the caller's transaction
        rolled_back_id: int | None = None
        with pytest.raises(RuntimeError, match="outbox boom"):
            async with conn.transaction():
                rolled_back_id = await JobClient.enqueue_in_transaction(
                    conn, "tests.dxe_jobs.OkJob", queue=unique_queue, x=2
                )
                assert isinstance(rolled_back_id, int)
                raise RuntimeError("outbox boom")
        gone = await db_pool.fetchrow(
            "SELECT 1 FROM jorb WHERE id = $1", rolled_back_id
        )
        assert gone is None
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Typed registry (@job)
# ---------------------------------------------------------------------------


async def test_registry_class_enqueues_validated_kwargs(
    listen_client, unique_queue, db_pool
):
    job_id = await RegisteredAdd.enqueue(
        listen_client, queue=unique_queue, priority=7, a=2
    )
    row = await db_pool.fetchrow(
        "SELECT job_class, kwargs, queue, prio FROM jorb WHERE id = $1", job_id
    )
    assert row["job_class"] == "tests.test_dxe_client.RegisteredAdd"
    assert row["kwargs"] == {"a": 2}
    assert row["queue"] == unique_queue
    assert row["prio"] == 7

    assert registry.resolve("tests.test_dxe_client.RegisteredAdd") is RegisteredAdd
    assert "tests.test_dxe_client.RegisteredAdd" in registry.all_jobs()


async def test_registry_rejects_bad_kwargs(listen_client, unique_queue):
    with pytest.raises(TypeError, match="unknown parameters.*bogus"):
        await RegisteredAdd.enqueue(listen_client, queue=unique_queue, a=1, bogus=5)
    with pytest.raises(TypeError, match="missing required parameters.*'a'"):
        await RegisteredAdd.enqueue(listen_client, queue=unique_queue, b=3)


async def test_registry_function_job(listen_client, unique_queue, db_pool):
    # the decorator replaced the function with a generated Job subclass
    assert isinstance(registered_double, type)
    assert issubclass(registered_double, Job)
    assert (
        registry.resolve("tests.test_dxe_client.registered_double") is registered_double
    )

    handle = await registered_double.enqueue_handle(
        listen_client, queue=unique_queue, x=4
    )
    assert isinstance(handle, JobHandle)
    row = await db_pool.fetchrow(
        "SELECT job_class, kwargs FROM jorb WHERE id = $1", handle.id
    )
    assert row["job_class"] == "tests.test_dxe_client.registered_double"
    assert row["kwargs"] == {"x": 4}

    with pytest.raises(TypeError, match="unknown parameters"):
        await registered_double.enqueue(listen_client, queue=unique_queue, y=1)


# ---------------------------------------------------------------------------
# SyncJobClient (no worker: unit-level smoke on its own event loop)
# ---------------------------------------------------------------------------


def test_sync_job_client_smoke(db_params, unique_queue):
    with SyncJobClient(**db_params) as client:
        job_id = client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=3)
        info = client.get_job(job_id)
        assert info is not None
        assert info.state == "queued"
        assert info.queue == unique_queue

        # terminal transitions surface synchronously too
        assert client.cancel_job(job_id) == {"job_id": job_id, "status": "cancelled"}
        with pytest.raises(JobCancelledError):
            client.wait_for_result(job_id, timeout=5)
