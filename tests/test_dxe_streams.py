"""Durable streams: ordered, per-(job, key) output a client reads live.

Two halves, for two different claims:

* the WRITER's, tested against a real worker connection with real prepared
  statements (the ``prepared_worker`` fixture) — positions are dense and
  0-based, the closing marker is a column and never a value, a superseded
  execution appends nothing, and a replayed write returns its recorded
  position instead of appending a second copy;
* the READER's, tested end to end against live workers — values arrive in
  order while the job is still running, the reader stops on the marker, on a
  crash, and on a cancellation, and it can start anywhere in the sequence.

The exactly-once claim is the one worth stating precisely: ``stream_write``
runs through ``transaction()``, so the row and its checkpoint are one commit
and a retried job continues its stream rather than repeating it. That is what
``test_a_retried_job_continues_its_stream_instead_of_repeating_it`` proves,
against a job that really does fail after its writes.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
import pytest_asyncio

from pyjobby import dxe
from pyjobby.client import JobClient, JobError
from pyjobby.db import requeue_job, rerun_job
from pyjobby.pj import Job

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio


# ===========================================================================
# helpers
# ===========================================================================


async def running_job(pool, queue: str, *, epoch: int = 1) -> int:
    """A job row owned by an attempt at ``epoch`` — what a writer needs."""
    job_id: int = await pool.fetchval(
        """INSERT INTO jorb (job_class, queue, state, run_epoch)
           VALUES ('tests.dxe_jobs.OkJob', $1, 'running', $2) RETURNING id""",
        queue,
        epoch,
    )
    return job_id


def writer(system, job_id: int, *, epoch: int = 1) -> Job:
    """A Job bound to ``job_id`` with an empty checkpoint log."""
    job = Job(s=system, job={"id": job_id, "run_epoch": epoch})
    job._dxe_bind([], epoch)
    return job


async def stream_rows(pool, job_id: int, key: str = "rows") -> list[dict]:
    return [
        dict(r)
        for r in await pool.fetch(
            """SELECT seq, value, closed, run_epoch FROM jorb_stream
                WHERE job_id = $1 AND key = $2 ORDER BY seq""",
            job_id,
            key,
        )
    ]


async def step_names(pool, job_id: int) -> list[str]:
    return [
        r["name"]
        for r in await pool.fetch(
            "SELECT name FROM jorb_step WHERE job_id = $1 ORDER BY step_seq", job_id
        )
    ]


@pytest_asyncio.fixture
async def reader(db_pool, db_params):
    """A JobClient that can ride LISTEN/NOTIFY, closed at teardown.

    Built with db_params on purpose: a stream reader parked between rows is
    exactly the caller the gated jorb_stream channel exists for, and a
    pool-only client would prove only that the 0.5s fallback poll works.
    """
    client = JobClient(pool=db_pool, db_params=db_params)
    yield client
    await client.close()


# ===========================================================================
# the writer: positions, markers, fencing, replay
# ===========================================================================


async def test_positions_are_dense_and_zero_based(
    prepared_worker, unique_queue, db_pool
):
    job_id = await running_job(db_pool, unique_queue)
    job = writer(prepared_worker, job_id)

    seqs = [await job.stream_write("rows", {"i": i}) for i in range(4)]

    assert seqs == [0, 1, 2, 3]
    assert [r["value"] for r in await stream_rows(db_pool, job_id)] == [
        {"i": i} for i in range(4)
    ]


async def test_each_key_has_its_own_sequence(prepared_worker, unique_queue, db_pool):
    """Positions are per (job, key): two streams do not interleave."""
    job_id = await running_job(db_pool, unique_queue)
    job = writer(prepared_worker, job_id)

    assert await job.stream_write("left", "a") == 0
    assert await job.stream_write("right", "A") == 0
    assert await job.stream_write("left", "b") == 1

    assert [r["seq"] for r in await stream_rows(db_pool, job_id, "left")] == [0, 1]
    assert [r["seq"] for r in await stream_rows(db_pool, job_id, "right")] == [0]


async def test_concurrent_appends_never_share_a_position(unique_queue, db_pool):
    """The primary key is the arbiter, not a retry loop.

    A job appends on ONE connection, so concurrency here is not a state the
    platform produces — but the guarantee must not depend on that. Fired
    concurrently, every append that COMMITS takes a distinct position and the
    sequence stays dense; a loser collides with the key and raises, which is
    an error the caller sees rather than a duplicate or a hole nobody does.
    """
    job_id = await running_job(db_pool, unique_queue)

    async def append(i: int) -> int | None:
        try:
            # FOR SHARE on the job row, so the eight appends do NOT serialise
            # on the fence -- a shared lock is what the fence needs (it must
            # only conflict with the epoch bump's exclusive one) and this is
            # the case that would notice if it were FOR UPDATE.
            row = await db_pool.fetchrow(
                dxe.STREAM_APPEND_SQL, job_id, "rows", {"i": i}, False, 1
            )
            assert row["fenced"] and not row["already_closed"]
            return int(row["seq"])
        except asyncpg.UniqueViolationError:
            return None

    results = await asyncio.gather(*(append(i) for i in range(8)))

    landed = sorted(seq for seq in results if seq is not None)
    assert landed, "every concurrent append lost, which is not contention"
    assert landed == list(range(len(landed))), "positions must stay dense"
    assert [r["seq"] for r in await stream_rows(db_pool, job_id)] == landed


async def test_end_of_stream_is_a_column_so_null_stays_streamable(
    prepared_worker, unique_queue, db_pool, reader
):
    """The whole reason `closed` is not a sentinel value.

    A job that streams None is streaming a value; the marker is a different
    row, distinguished by a column outside the value domain. The reader
    yields the first and stops at the second — which an in-band sentinel
    could not tell apart at all.
    """
    job_id = await running_job(db_pool, unique_queue)
    job = writer(prepared_worker, job_id)

    await job.stream_write("rows", None)
    await job.stream_close("rows")

    rows = await stream_rows(db_pool, job_id)
    assert [(r["value"], r["closed"]) for r in rows] == [(None, False), (None, True)]

    seen = [value async for value in reader.read_stream(job_id, "rows")]
    assert seen == [None]
    assert await reader.get_stream(job_id, "rows") == {
        "values": [None],
        "closed": True,
    }


async def test_close_is_checkpointed_under_its_own_name(
    prepared_worker, unique_queue, db_pool
):
    """What an operator meets in `pj-admin jobs steps`."""
    job_id = await running_job(db_pool, unique_queue)
    job = writer(prepared_worker, job_id)

    await job.stream_write("rows", 1)
    await job.stream_write("rows", 2)
    await job.stream_close("rows")

    assert await step_names(db_pool, job_id) == [
        "dxe.stream:rows",
        "dxe.stream:rows",
        "dxe.stream-close:rows",
    ]


async def test_a_superseded_writer_appends_nothing(
    prepared_worker, unique_queue, db_pool
):
    """A stale execution cannot write into a stream a live attempt owns —
    a reader has no way to tell two writers apart."""
    job_id = await running_job(db_pool, unique_queue, epoch=1)
    job = writer(prepared_worker, job_id, epoch=1)
    assert await job.stream_write("rows", {"i": 0}) == 0

    # the monitor requeues the job out from under this execution
    await requeue_job(
        db_pool, job_id, allowed_states=("claimed", "running"), reset_errors=False
    )

    with pytest.raises(dxe.StaleExecutionError):
        await job.stream_write("rows", {"i": 1})
    with pytest.raises(dxe.StaleExecutionError):
        await job.stream_close("rows")

    assert [r["seq"] for r in await stream_rows(db_pool, job_id)] == [0]


async def test_writing_after_close_raises_instead_of_landing_unreachably(
    prepared_worker, unique_queue, db_pool
):
    """A closed stream refuses further rows, loudly.

    Every reader stops at the closing marker, so a row appended after it is a
    row nothing will ever read: the write "succeeded" and its data was gone.
    Silence there turns a caller bug into missing output, which is why this is
    an error and not a no-op.
    """
    job_id = await running_job(db_pool, unique_queue)
    job = writer(prepared_worker, job_id)

    await job.stream_write("rows", {"i": 0})
    await job.stream_close("rows")

    with pytest.raises(dxe.StreamClosedError) as raised:
        await job.stream_write("rows", {"i": 1})

    assert str(job_id) in str(raised.value) and "rows" in str(raised.value)
    assert [(r["seq"], r["closed"]) for r in await stream_rows(db_pool, job_id)] == [
        (0, False),
        (1, True),
    ]
    # ...and the OTHER key is untouched: the refusal is per (job, key)
    assert await job.stream_write("other", {"i": 0}) == 0


async def test_closing_a_stream_twice_raises_rather_than_being_idempotent(
    prepared_worker, unique_queue, db_pool
):
    """Deliberately not idempotent.

    A close is already exactly-once per CALL SITE -- its checkpoint
    fast-forwards on every replay -- so a second close is never a retry. It is
    two call sites both believing they own the end of this stream, which is
    exactly the state that produces writes past the marker. Hiding it would
    hide the bug the test above catches.
    """
    job_id = await running_job(db_pool, unique_queue)
    job = writer(prepared_worker, job_id)

    await job.stream_close("rows")

    with pytest.raises(dxe.StreamClosedError, match="already closed"):
        await job.stream_close("rows")

    assert len(await stream_rows(db_pool, job_id)) == 1


async def test_the_refused_write_is_recorded_as_that_steps_failure(
    prepared_worker, unique_queue, db_pool
):
    """Ordinary step semantics, which is why StreamClosedError is not a
    DXEError: the attempt is checkpointed with its error (in a transaction of
    its own, since the append's transaction rolled back), so
    `pj-admin jobs steps` names the call that did it and the job's retry budget
    applies to it like any other failure."""
    job_id = await running_job(db_pool, unique_queue)
    job = writer(prepared_worker, job_id)

    await job.stream_close("rows")
    with pytest.raises(dxe.StreamClosedError):
        await job.stream_write("rows", {"i": 1})

    steps = await db_pool.fetch(
        "SELECT step_seq, name, error FROM jorb_step WHERE job_id = $1 "
        "ORDER BY step_seq",
        job_id,
    )
    assert [(r["name"], r["error"] is None) for r in steps] == [
        ("dxe.stream-close:rows", True),
        ("dxe.stream:rows", False),
    ]
    assert "StreamClosedError" in steps[1]["error"]


async def test_a_replayed_write_returns_its_position_without_appending(
    prepared_worker, unique_queue, db_pool
):
    """Exactly-once, stated as the replay that must NOT happen.

    The recorded checkpoint carries the position, so a resumed attempt hands
    the same number back and the row is not written twice.
    """
    job_id = await running_job(db_pool, unique_queue)
    first = writer(prepared_worker, job_id)
    assert await first.stream_write("rows", {"i": 0}) == 0
    assert await first.stream_write("rows", {"i": 1}) == 1

    recorded = await db_pool.fetch(
        "SELECT step_seq, name, output, error FROM jorb_step "
        "WHERE job_id = $1 ORDER BY step_seq",
        job_id,
    )
    resumed = Job(s=prepared_worker, job={"id": job_id, "run_epoch": 1})
    resumed._dxe_bind(recorded, 1)

    assert await resumed.stream_write("rows", {"i": 0}) == 0
    assert await resumed.stream_write("rows", {"i": 1}) == 1
    # ...and only the writes past the recorded prefix really append
    assert await resumed.stream_write("rows", {"i": 2}) == 2

    assert [r["value"] for r in await stream_rows(db_pool, job_id)] == [
        {"i": 0},
        {"i": 1},
        {"i": 2},
    ]


# ===========================================================================
# the reader, against live workers
# ===========================================================================


async def test_a_client_reads_the_stream_while_the_job_runs(
    live_worker, unique_queue, db_pool, reader
):
    """The point of the feature: values arrive during the run, in order.

    The producer spaces its writes out, so a reader that only worked after
    the job finished would time out here rather than pass slowly.
    """
    await live_worker()

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.StreamProducerJob",
        {"key": "rows", "n": 5, "delay": 0.1},
        unique_queue,
    )

    seen = []
    async with asyncio.timeout(30):
        async for value in reader.read_stream(job_id, "rows"):
            seen.append(value)
            if len(seen) == 1:
                # the first row is readable before the job is done, which is
                # the claim a snapshot read could not make
                state = await db_pool.fetchval(
                    "SELECT state FROM jorb WHERE id = $1", job_id
                )
                assert state in ("claimed", "running")

    assert seen == [{"i": i} for i in range(5)]

    row = await wait_for_job_state(db_pool, job_id, ("finished",))
    assert row["result"] == {"wrote": 5}
    # the reader stopped at the marker, and the marker carries no value
    assert await reader.get_stream(job_id, "rows") == {
        "values": [{"i": i} for i in range(5)],
        "closed": True,
    }


async def test_a_reader_can_start_at_an_offset(
    live_worker, unique_queue, db_pool, reader
):
    """Positions are dense, so an offset is all a resuming reader needs."""
    await live_worker()

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.StreamProducerJob",
        {"key": "rows", "n": 4},
        unique_queue,
    )
    await wait_for_job_state(db_pool, job_id, ("finished",))

    seen = []
    async with asyncio.timeout(30):
        async for value in reader.read_stream(job_id, "rows", offset=2):
            seen.append(value)

    assert seen == [{"i": 2}, {"i": 3}]


async def test_a_reader_stops_when_the_job_crashes_mid_stream(
    live_worker, unique_queue, db_pool, reader
):
    """No marker is ever written, so the terminal state is what ends the read.

    A cancel or a timeout terminalises a job out of band while a fenced-out
    execution may still believe it is writing; the reader's rule is the job's
    state, not the writer's intent.
    """
    await live_worker()

    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
           VALUES ($1,$2,$3,$4) RETURNING id""",
        "tests.dxe_jobs.StreamThenCrashJob",
        {"key": "rows", "n": 3},
        unique_queue,
        {"max_retries": 0, "initial_retry_delay": 0},
    )

    seen = []
    async with asyncio.timeout(30):
        async for value in reader.read_stream(job_id, "rows"):
            seen.append(value)

    assert seen == [{"i": i} for i in range(3)]
    row = await db_pool.fetchrow("SELECT state FROM jorb WHERE id = $1", job_id)
    assert row["state"] == "crashed"
    # the stream was never closed: the reader ended on the job, not a marker
    assert await reader.get_stream(job_id, "rows") == {
        "values": [{"i": i} for i in range(3)],
        "closed": False,
    }


async def test_a_retried_job_continues_its_stream_instead_of_repeating_it(
    live_worker, unique_queue, db_pool, reader
):
    """Exactly-once per call site, end to end.

    The job writes three rows and then fails; the retry fast-forwards all
    three completed `stream_write` checkpoints and closes. A reader watching
    across the retry sees three values, not six.
    """
    await live_worker()

    job_id = await db_pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
           VALUES ($1,$2,$3,$4) RETURNING id""",
        "tests.dxe_jobs.StreamRetryJob",
        {"key": "rows", "n": 3},
        unique_queue,
        {"max_retries": 3, "initial_retry_delay": 0},
    )

    seen = []
    async with asyncio.timeout(30):
        async for value in reader.read_stream(job_id, "rows"):
            seen.append(value)

    row = await wait_for_job_state(db_pool, job_id, ("finished",))
    assert row["error_count"] == 1  # it really did fail once
    assert seen == [{"i": i} for i in range(3)]
    assert [r["seq"] for r in await stream_rows(db_pool, job_id)] == [0, 1, 2, 3]


async def test_a_fresh_rerun_starts_the_stream_over_at_seq_zero(
    live_worker, unique_queue, db_pool, reader
):
    """`rerun` (fresh, the default) wipes the streams with the checkpoints.

    A position is assigned as "one past the highest this key holds". Left in
    place, the second run's first `stream_write` took seq 3 instead of 0, so
    `get_stream`/`read_stream` handed every reader the FIRST run's rows with
    the second run's appended -- one stream claiming to be two runs, and no
    way for a reader to see the boundary. Wiping them is the only answer that
    keeps a stream a description of the run that is happening.
    """
    await live_worker()

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.StreamProducerJob",
        {"key": "rows", "n": 3},
        unique_queue,
    )
    await wait_for_job_state(db_pool, job_id, ("finished",))
    first = await reader.get_stream(job_id, "rows")
    assert first == {"values": [{"i": i} for i in range(3)], "closed": True}

    assert await rerun_job(db_pool, job_id) == job_id
    # nothing survives the requeue: the wipe and the requeue are two
    # statements in ONE transaction, so they commit together and no re-claim
    # can land between them (see db.WIPE_DURABLE_STATE_SQL for why that is
    # the lock's guarantee rather than the statement count's)
    assert await stream_rows(db_pool, job_id) == []

    await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)

    rows = await stream_rows(db_pool, job_id)
    assert [r["seq"] for r in rows] == [0, 1, 2, 3], "the new run streams from 0"
    assert await reader.get_stream(job_id, "rows") == first
    assert [value async for value in reader.read_stream(job_id, "rows")] == [
        {"i": i} for i in range(3)
    ], "a reader sees the NEW run's output, not both runs concatenated"


async def test_rerun_resume_keeps_the_stream_it_already_wrote(
    live_worker, unique_queue, db_pool
):
    """The other half of the rule, and the reason the wipe is on `fresh` only.

    `--resume` means "continue this run": the completed `stream_write`
    checkpoints fast-forward and append nothing, so the rows the interrupted
    attempt wrote are the only copy that run will ever have. Wiping them would
    leave a resumed job's stream permanently missing its own prefix.
    """
    await live_worker()

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.StreamProducerJob",
        {"key": "rows", "n": 3},
        unique_queue,
    )
    await wait_for_job_state(db_pool, job_id, ("finished",))
    before = await stream_rows(db_pool, job_id)

    assert await rerun_job(db_pool, job_id, fresh=False) == job_id

    assert await stream_rows(db_pool, job_id) == before
    await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
    assert await stream_rows(db_pool, job_id) == before


async def test_a_stream_of_a_job_that_does_not_exist_fails_fast(reader):
    """Nothing will ever append, so waiting only delays the same answer."""
    with pytest.raises(JobError, match="does not exist"):
        async for _ in reader.read_stream(2_000_000_000, "rows"):
            pass


async def test_get_stream_of_an_unwritten_key_is_empty_and_open(
    unique_queue, db_pool, reader
):
    """A snapshot is a query, not a wait: there is nothing to fail on."""
    job_id = await running_job(db_pool, unique_queue)

    assert await reader.get_stream(job_id, "rows") == {"values": [], "closed": False}


async def test_the_sync_client_reads_a_finished_stream(
    live_worker, unique_queue, db_pool, db_params, tmp_path
):
    """The sync twin is a hand-written generator, so it is driven for real.

    Scripts are the callers least likely to be covered by anything else, and
    `read_stream` is the one wrapper here that is not a plain `_run` of a
    coroutine — it turns each `__anext__` into one turn of the wrapped loop.
    """
    from pyjobby.client import SyncJobClient
    from pyjobby.procs import write_config_toml

    await live_worker()
    config = write_config_toml(tmp_path / "pyjobby.toml", db_params)

    job_id = await db_pool.fetchval(
        "INSERT INTO jorb (job_class, kwargs, queue) VALUES ($1,$2,$3) RETURNING id",
        "tests.dxe_jobs.StreamProducerJob",
        {"key": "rows", "n": 3},
        unique_queue,
    )
    await wait_for_job_state(db_pool, job_id, ("finished",))

    def _drive() -> tuple[list, dict, list]:
        with SyncJobClient.from_config(str(config)) as client:
            values = list(client.read_stream(job_id, "rows"))
            snapshot = client.get_stream(job_id, "rows")
            # a caller that breaks out early closes the async generator with
            # it, rather than leaving a registered waiter behind
            partial = []
            for value in client.read_stream(job_id, "rows"):
                partial.append(value)
                break
            return values, snapshot, partial

    values, snapshot, partial = await asyncio.to_thread(_drive)

    assert values == [{"i": i} for i in range(3)]
    assert snapshot == {"values": values, "closed": True}
    assert partial == [{"i": 0}]
