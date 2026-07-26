"""The pyjobby monitor: the platform's single background reaper.

Every safety-net sweep lives here — one daemon, one place to reason about
recovery:

1. **Timeout enforcement**: running jobs past ``timeout_at`` are retried or
   dead-lettered according to their ``on_timeout`` policy. Workers enforce
   timeouts in-process too; this catches workers that died mid-job or sync
   tasks that could not be interrupted.
2. **Dead-worker reclaim**: jobs claimed by workers whose registry heartbeat
   (``jorb_worker.last_seen``) went stale are requeued, on any host. Workers
   heartbeat on a dedicated connection, so a stale heartbeat means the
   process is gone (or partitioned — in which case run_epoch fencing keeps
   the zombie from writing results after the job is requeued).
3. **Unregistered-claim reclaim**: jobs stuck in 'claimed' with no registry
   reference past a grace period (a worker died between claim and register,
   or the registry was unavailable).
4. **Job retention**: terminal jobs older than ``--retention-days`` (default
   30) are deleted, taking their checkpoints, history, events and mailbox
   with them via ON DELETE CASCADE. On by default: a platform that runs
   indefinitely accumulates every completed job forever otherwise, and a
   retention policy nobody remembers to switch on is not a policy. Pass
   ``--retention-days 0`` to keep everything forever.
5. **Checkpoint retention**: ``jorb_step`` rows of terminal jobs older than
   ``--checkpoint-retention-days`` (default 1) are deleted while the job row
   itself stays. Checkpoints exist to make a job RESUMABLE; the moment it
   reaches a terminal state resume is impossible, so they are the bulkiest
   thing on the row with the shortest useful life. They outlive the job's
   terminal transition only far enough to debug it. ``0`` keeps them for as
   long as the job.

Both retention sweeps DRAIN: a cycle keeps taking batches until it is caught
up or spends ``--retention-max-seconds``, then yields. One batch per cycle
would be a fixed deletion rate that a busy install simply outruns, leaving
retention switched on and the table still growing; the budget keeps a backlog
from delaying the latency-critical sweeps above.

Requeues bump nothing themselves: the next claim increments ``run_epoch``,
which fences any still-running stale execution out of the row.

Run it: ``pj-monitor --config ./pyjobby.conf.py`` (one instance is enough;
several are safe — every sweep is a single atomic statement or a
transaction holding its row locks).
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Awaitable, Callable

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

from . import db


async def handle_timed_out_job(
    conn: asyncpg.Pool | asyncpg.Connection,
    job_id: int,
    job_class: str,
    admin_data: dict | None,
    error_count: int,
) -> None:
    """Retry or dead-letter one job that exceeded its timeout."""
    admin = admin_data or {}
    on_timeout = admin.get("on_timeout", "retry")
    max_retries = admin.get("max_retries", 10)
    attempt = error_count + 1

    logger.warning(
        f"Job {job_id} ({job_class}) exceeded timeout, "
        f"action: {on_timeout}, attempt {attempt}/{max_retries}"
    )

    if on_timeout == "retry" and attempt < max_retries:
        from .retry_strategies import calculate_retry_from_job

        job = await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        if not job:
            return

        retry_delay = calculate_retry_from_job(dict(job), attempt)

        await conn.execute(
            """
            UPDATE jorb
            SET state = 'queued',
                run_epoch = run_epoch + 1,
                timeout_at = NULL,
                error_count = error_count + 1,
                error_message = $2,
                run_after = now() + $3::interval,
                updated = now()
            WHERE id = $1
              AND state = 'running'
            """,
            job_id,
            "Timeout exceeded - retrying",
            retry_delay,
        )

        logger.info(
            f"Job {job_id} requeued for retry in {retry_delay.total_seconds()}s"
        )
    else:
        reason = "max retries exceeded" if attempt >= max_retries else "on_timeout=fail"

        await conn.execute(
            """
            UPDATE jorb
            SET state = 'crashed',
                timeout_at = NULL,
                error_count = error_count + 1,
                error_message = $2,
                finished = now(),
                updated = now()
            WHERE id = $1
              AND state = 'running'
            """,
            job_id,
            f"Timeout exceeded - dead-lettered ({reason})",
        )

        logger.error(f"Job {job_id} dead-lettered: {reason}")


async def sweep_timed_out_jobs(pool: asyncpg.Pool, batch_size: int = 100) -> int:
    """Find running jobs past their deadline and retry/dead-letter them.

    The SELECT ... FOR UPDATE SKIP LOCKED and the per-job updates share one
    transaction so the row locks are held while handling the jobs: multiple
    monitor instances cannot double-process, and a worker finishing a job
    concurrently is serialized against us (its epoch-fenced update then
    no-ops)."""
    async with pool.acquire() as conn, conn.transaction():
        timed_out = await conn.fetch(
            """
            SELECT id, job_class, timeout_at, admin_data, error_count
            FROM jorb
            WHERE state = 'running'
              AND timeout_at IS NOT NULL
              AND timeout_at < now()
            FOR UPDATE SKIP LOCKED
            LIMIT $1
            """,
            batch_size,
        )

        for job in timed_out:
            await handle_timed_out_job(
                conn,
                job["id"],
                job["job_class"],
                job["admin_data"],
                job["error_count"],
            )

    return len(timed_out)


async def sweep_dead_workers(
    pool: asyncpg.Pool,
    liveness_grace_seconds: float = 60,
    batch_size: int = 500,
) -> int:
    """Requeue in-flight jobs owned by workers whose heartbeat went stale,
    and retire those workers from the registry.

    Liveness is the heartbeat and nothing else: a job still 'claimed' or
    'running' behind a stale ``last_seen`` is orphaned whether or not the
    worker managed to stamp ``shutdown_at`` on the way out. Requiring the
    worker to still look live would strand exactly the jobs that need
    recovery — those of a worker that deregistered (or was retired by an
    earlier sweep) while a job was still in flight.

    Single atomic statements; safe with concurrent monitor instances."""
    grace = datetime.timedelta(seconds=liveness_grace_seconds)

    requeued = await pool.fetch(
        """
        WITH doomed AS MATERIALIZED (
            SELECT j.id FROM jorb j
            JOIN jorb_worker w ON w.id = j.claimed_by
            WHERE j.state IN ('claimed', 'running')
              AND w.last_seen < now() - $1::interval
            FOR UPDATE OF j SKIP LOCKED
            LIMIT $2
        )
        UPDATE jorb
        SET state = 'queued',
            run_epoch = run_epoch + 1,
            run_after = now(),
            timeout_at = NULL,
            updated = now()
        FROM doomed
        WHERE jorb.id = doomed.id
          AND jorb.state IN ('claimed', 'running')
        RETURNING jorb.id, jorb.job_class, jorb.worker_host, jorb.claimed_by
        """,
        grace,
        batch_size,
    )

    for row in requeued:
        logger.warning(
            f"Requeued job {row['id']} ({row['job_class']}) from dead worker "
            f"{row['claimed_by']} on {row['worker_host']}"
        )

    # retire workers that stopped heartbeating so they aren't rescanned
    # (and so the operator surface shows them as gone, not alive)
    retired = await pool.execute(
        """
        UPDATE jorb_worker
        SET shutdown_at = now()
        WHERE shutdown_at IS NULL
          AND last_seen < now() - $1::interval
        """,
        grace,
    )
    if retired != "UPDATE 0":
        logger.warning(f"Retired stale workers from registry: {retired}")

    return len(requeued)


async def sweep_unregistered_claims(
    pool: asyncpg.Pool,
    claimed_grace_seconds: float = 300,
    batch_size: int = 100,
) -> int:
    """Requeue jobs stuck in 'claimed' with no registry reference.

    Covers the rare gap where a worker claimed a job while the registry was
    unavailable and then died: nothing heartbeats for it, so age is the only
    signal. A healthy worker moves claimed -> running almost immediately."""
    grace = datetime.timedelta(seconds=claimed_grace_seconds)
    requeued = await pool.fetch(
        """
        WITH doomed AS MATERIALIZED (
            SELECT id FROM jorb
            WHERE state = 'claimed'
              AND claimed_by IS NULL
              AND updated < now() - $1::interval
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        UPDATE jorb
        SET state = 'queued',
            run_epoch = run_epoch + 1,
            run_after = now(),
            timeout_at = NULL,
            updated = now()
        FROM doomed
        WHERE jorb.id = doomed.id
          AND jorb.state = 'claimed'
        RETURNING jorb.id, jorb.job_class, jorb.worker_host
        """,
        grace,
        batch_size,
    )

    for row in requeued:
        logger.warning(
            f"Requeued unregistered stale claim {row['id']} ({row['job_class']}) "
            f"from {row['worker_host']}"
        )

    return len(requeued)


#: The states a job never leaves. Only these are ever eligible for deletion:
#: a queued/claimed/running/waiting job is live work however old it looks
#: (a job parked on a dependency can legitimately outlive any window).
TERMINAL_STATES = ("finished", "crashed", "cancelled")

#: The same list inlined into SQL, because ``jorb_retention_idx`` is PARTIAL
#: on exactly this predicate. A bound ``state = ANY($1)`` reads fine under the
#: custom plan of the first few executions and then silently falls off the
#: index: asyncpg prepares its statements, and once PostgreSQL switches to a
#: GENERIC plan it can no longer prove an unknown parameter implies the index
#: predicate. Measured on 20k terminal rows with nothing expired -- generic
#: plan, bound states: Seq Scan + Sort, 617 buffers; literal states: Index
#: Scan, 2 buffers. Interpolating a module constant is not user input.
_TERMINAL_STATES_SQL = ", ".join(f"'{state}'" for state in TERMINAL_STATES)


async def sweep_expired_jobs(
    pool: asyncpg.Pool,
    retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete terminal jobs whose retention window has elapsed.

    Eligibility is state first, age second: only ``TERMINAL_STATES`` rows are
    considered, and their age is ``finished`` — the terminal timestamp every
    completion, crash and cancellation path stamps. ``updated`` stands in when
    ``finished`` is somehow NULL so a terminal row can never become immortal
    and leak.

    Jobs an unfinished job is still parked on are kept regardless of age:
    ``waitfor_job``/``waitfor_group`` carry no foreign key, so deleting the
    upstream would strand the waiter in 'waiting' forever — nothing but the
    upstream's own terminal transition ever wakes it.

    Every child table — jorb_step, jorb_event, jorb_mailbox, jorb_history —
    follows via ON DELETE CASCADE, so deleting the job row is the whole job.

    Bounded and batched: one bite of ``batch_size`` per call, holding only
    those rows' locks. FOR UPDATE SKIP LOCKED makes concurrent monitors
    partition the backlog instead of colliding on it. ``_drain`` calls this
    repeatedly so a cycle can catch up rather than nibble.

    Ordered by the retention expression, never by id. Oldest-first is the
    semantically right order to reap in, and it is also the only order that
    ``jorb_retention_idx`` can serve: ORDER BY id makes the planner prefer a
    pkey scan to avoid a sort and then filter the entire terminal backlog
    (measured on 20k rows with nothing expired: 366 buffers and 20k rows
    discarded, against 2 buffers and no sort). That gap grows with the table,
    so the sweep that exists to stop unbounded growth would itself get slower
    forever. Returns the number of jobs deleted."""
    retention = datetime.timedelta(days=retention_days)

    async with pool.acquire() as conn, conn.transaction():
        expired = await conn.fetch(
            f"""
            SELECT j.id
            FROM jorb j
            WHERE j.state IN ({_TERMINAL_STATES_SQL})
              AND COALESCE(j.finished, j.updated) < now() - $1::interval
              AND NOT EXISTS (
                  SELECT 1 FROM jorb w
                  WHERE w.state = 'waiting' AND w.waitfor_job = j.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM jorb w
                  WHERE w.state = 'waiting' AND w.waitfor_group = j.run_group
              )
            ORDER BY COALESCE(j.finished, j.updated)
            FOR UPDATE OF j SKIP LOCKED
            LIMIT $2
            """,  # noqa: S608 - interpolates a module constant, never input
            retention,
            batch_size,
        )
        if not expired:
            return 0

        job_ids = [row["id"] for row in expired]
        await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)

    return len(job_ids)


async def sweep_completed_checkpoints(
    pool: asyncpg.Pool,
    checkpoint_retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete the DXE checkpoints of jobs that terminated long enough ago.

    Checkpoints exist for one reason: so a resumed attempt fast-forwards past
    steps it already ran. A terminal job is never resumed, so from the instant
    it finishes its ``jorb_step`` rows are pure audit — readable through
    ``pj-admin jobs steps <id>`` and nothing else. They are also the bulkiest
    thing hanging off a job (one row per step, each with the step's output),
    which is why they get their own, much shorter window instead of living as
    long as the job row.

    Only the checkpoints go. The job row and its history, events and mailbox
    are ``sweep_expired_jobs``' business on the longer window — the two are
    deliberately independent lifetimes.

    A checkpoint of a NON-terminal job is never touched at any age. A durable
    sleep parks a job in 'queued' for months holding the very checkpoint that
    records when to wake; deleting it would silently re-run completed steps.
    So state is the gate and age only decides among terminal jobs.

    ``FOR UPDATE OF s`` locks the checkpoint rows and not the jobs: a worker
    writing to a live job must never queue behind retention.

    Driven from ``jorb_retention_idx`` in oldest-terminated-first order — the
    job side is what has an index worth using, and ordering by ``s.job_id``
    instead would trade it for a full scan of jorb_step. The batch boundary
    within one job is therefore unordered; nothing depends on which of a
    doomed job's checkpoints go first. Returns the number of rows deleted."""
    retention = datetime.timedelta(days=checkpoint_retention_days)

    deleted = await pool.fetch(
        f"""
        WITH doomed AS MATERIALIZED (
            SELECT s.job_id, s.step_seq
            FROM jorb_step s
            JOIN jorb j ON j.id = s.job_id
            WHERE j.state IN ({_TERMINAL_STATES_SQL})
              AND COALESCE(j.finished, j.updated) < now() - $1::interval
            ORDER BY COALESCE(j.finished, j.updated)
            FOR UPDATE OF s SKIP LOCKED
            LIMIT $2
        )
        DELETE FROM jorb_step s
        USING doomed d
        WHERE s.job_id = d.job_id AND s.step_seq = d.step_seq
        RETURNING s.job_id, s.step_seq
        """,  # noqa: S608 - interpolates a module constant, never input
        retention,
        batch_size,
    )

    return len(deleted)


async def sweep_consumed_mailbox(
    pool: asyncpg.Pool,
    retention_days: float,
    batch_size: int = 1000,
) -> int:
    """Delete consumed mailbox messages older than the retention window.

    The job-scoped cascade cannot reach these: ``recv`` only stamps
    ``consumed_at``, so a long-lived job — a durable workflow that runs for
    months, or a never-terminating one — keeps every message it ever read.
    A consumed message is unreadable (``recv`` filters ``consumed_at IS
    NULL``) and referenced by nothing, so age is the only thing to decide on.

    The victims are picked in an explicitly MATERIALIZED CTE, not an
    ``IN (SELECT ... LIMIT n)`` subquery: the planner is free to re-execute an
    un-materialized subquery once per outer row, and each re-execution of a
    FOR UPDATE SKIP LOCKED scan returns a *different* set — so the LIMIT stops
    bounding the delete and the batch overruns (observed: batch_size=2
    deleting 5 rows). MATERIALIZED forces exactly one evaluation.

    Ordered by ``consumed_at``, which jorb_mailbox_consumed_idx provides
    directly: the probe walks the index in order and stops at the batch size,
    with no sort even when a large backlog matches. Ordering by id instead
    would have to sort every matching row first, which is the cost that grows.

    Single atomic statement; safe with concurrent monitor instances."""
    retention = datetime.timedelta(days=retention_days)

    deleted = await pool.fetch(
        """
        WITH doomed AS MATERIALIZED (
            SELECT id FROM jorb_mailbox
            WHERE consumed_at IS NOT NULL
              AND consumed_at < now() - $1::interval
            ORDER BY consumed_at
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        DELETE FROM jorb_mailbox m
        USING doomed d
        WHERE m.id = d.id
        RETURNING m.id
        """,
        retention,
        batch_size,
    )

    return len(deleted)


async def _run_sweep(name: str, sweep: Callable[[], Awaitable[int]]) -> int:
    """Run one sweep, containing its failure to itself.

    The sweeps are independent safety nets: a database error while handling a
    timed-out job must not also cancel dead-worker recovery for that cycle
    (nor kill the daemon), so each one reports and returns 0 on failure."""
    try:
        return await sweep()
    except Exception:
        logger.exception(f"Monitor sweep {name} failed")
        return 0


async def _drain(
    name: str,
    sweep: Callable[[], Awaitable[int]],
    batch_size: int,
    max_seconds: float,
) -> int:
    """Run one batched retention sweep until it is caught up or out of time.

    One batch per cycle is a rate limit, not a policy: ``batch_size`` per
    ``check_interval`` is a hard ceiling on deletions per second, and any
    platform ingesting faster than that outruns retention permanently — the
    dashboard says retention is enabled while the table grows forever, which
    is the exact defect this feature exists to prevent. So a cycle keeps
    taking batches until one comes back short (nothing left to delete).

    ``max_seconds`` is the other half. Retention is the only sweep here that
    is not latency-critical: timeout enforcement and dead-worker recovery
    decide how long a stuck job stays stuck, and they must not queue behind an
    unbounded backlog. So a cycle yields when its budget is spent and picks up
    where it left off on the next one.

    Stopping early is logged at WARNING with the count, because a retention
    sweep that cannot keep up has to be visible: silence would read exactly
    like being caught up. Returns the number of rows deleted this cycle."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds
    total = 0

    while True:
        deleted = await sweep()
        total += deleted

        if deleted < batch_size:
            if total:
                logger.info(f"Retention {name}: deleted {total}, caught up")
            return total

        if loop.time() >= deadline:
            logger.warning(
                f"Retention {name}: deleted {total} and stopped on its "
                f"{max_seconds}s budget with a backlog still pending — "
                f"retention is falling behind"
            )
            return total


async def _run_retention(
    name: str,
    sweep: Callable[[], Awaitable[int]],
    batch_size: int,
    max_seconds: float,
) -> int:
    """Drain one retention sweep with the same failure isolation as the rest."""
    return await _run_sweep(name, lambda: _drain(name, sweep, batch_size, max_seconds))


async def monitor(
    dsn: str,
    check_interval: float = 10,
    batch_size: int = 100,
    liveness_grace_seconds: float = 60,
    claimed_grace_seconds: float = 300,
    retention_days: float = 30.0,
    checkpoint_retention_days: float = 1.0,
    retention_batch_size: int = 1000,
    retention_max_seconds: float = 5.0,
) -> None:
    """Run all sweeps every ``check_interval`` seconds, forever.

    Retention is on by default and the two windows are independent:
    ``retention_days`` deletes whole terminal jobs, ``checkpoint_retention_days``
    deletes just the checkpoints of terminal jobs much sooner. Either set to
    ``0`` means keep forever — that sweep does not run at all.

    Each retention sweep drains its backlog within a ``retention_max_seconds``
    budget per cycle, so it can catch up on a busy install without ever
    delaying the latency-critical sweeps above it."""
    pool = await db.create_pool(dsn, min_size=1, max_size=2)

    def window(days: float) -> str:
        return f"{days}d" if days else "forever"

    logger.info(
        f"Monitor started (interval {check_interval}s, "
        f"liveness grace {liveness_grace_seconds}s, "
        f"job retention {window(retention_days)}, "
        f"checkpoint retention {window(checkpoint_retention_days)})"
    )

    try:
        while True:
            timed_out = await _run_sweep(
                "timed-out jobs", lambda: sweep_timed_out_jobs(pool, batch_size)
            )
            if timed_out:
                logger.info(f"Handled {timed_out} timed-out jobs")

            await _run_sweep(
                "dead workers",
                lambda: sweep_dead_workers(pool, liveness_grace_seconds),
            )
            await _run_sweep(
                "unregistered claims",
                lambda: sweep_unregistered_claims(pool, claimed_grace_seconds),
            )

            if checkpoint_retention_days:
                await _run_retention(
                    "completed checkpoints",
                    lambda: sweep_completed_checkpoints(
                        pool, checkpoint_retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )

            if retention_days:
                await _run_retention(
                    "expired jobs",
                    lambda: sweep_expired_jobs(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )
                await _run_retention(
                    "consumed mailbox",
                    lambda: sweep_consumed_mailbox(
                        pool, retention_days, retention_batch_size
                    ),
                    retention_batch_size,
                    retention_max_seconds,
                )

            await asyncio.sleep(check_interval)

    finally:
        await pool.close()
        logger.info("Monitor stopped")


def cli() -> None:
    """CLI entry point: the ``pj-monitor`` console script."""
    import sys

    import click

    @click.command()
    @click.option(
        "--dsn",
        envvar="PYJOBBY_DSN",
        required=False,
        help="PostgreSQL DSN (or use PYJOBBY_DSN env var)",
    )
    @click.option(
        "--config", type=click.Path(exists=True), help="Path to pyjobby.conf.py"
    )
    @click.option(
        "--check-interval", default=10.0, show_default=True, help="Sweep interval (s)"
    )
    @click.option(
        "--liveness-grace",
        default=60.0,
        show_default=True,
        help="Seconds without a heartbeat before a worker counts as dead",
    )
    @click.option(
        "--claimed-grace",
        default=300.0,
        show_default=True,
        help="Age before an unregistered 'claimed' job counts as abandoned",
    )
    @click.option(
        "--retention-days",
        type=float,
        default=30.0,
        show_default=True,
        help="Delete terminal jobs (with their history, events, mailbox and "
        "checkpoints) older than this. 0 keeps every job forever.",
    )
    @click.option(
        "--checkpoint-retention-days",
        type=float,
        default=1.0,
        show_default=True,
        help="Delete the DXE checkpoints of terminal jobs this long after they "
        "terminated, keeping the job itself. 0 keeps checkpoints as long as "
        "the job.",
    )
    @click.option(
        "--retention-batch-size",
        default=1000,
        show_default=True,
        help="Rows deleted per retention batch (a cycle drains many batches)",
    )
    @click.option(
        "--retention-max-seconds",
        type=float,
        default=5.0,
        show_default=True,
        help="Time budget per retention sweep per cycle. It drains its backlog "
        "until caught up or this runs out, so it can never delay timeout and "
        "dead-worker recovery.",
    )
    def main(
        dsn: str | None,
        config: str | None,
        check_interval: float,
        liveness_grace: float,
        claimed_grace: float,
        retention_days: float,
        checkpoint_retention_days: float,
        retention_batch_size: int,
        retention_max_seconds: float,
    ) -> None:
        """Run the pyjobby monitor: timeouts, dead-worker recovery, and
        retention (on by default; --retention-days 0 keeps everything)."""
        import asyncio

        if not dsn:
            if config:
                from urllib.parse import quote

                from .configloader import load_config_from_file

                cfg = load_config_from_file(config, keys=["db_params"])
                db_params = cfg.get("db_params", {})
                user = quote(str(db_params.get("user", "")), safe="")
                password = quote(str(db_params.get("password", "")), safe="")
                dsn = (
                    f"postgresql://{user}:{password}"
                    f"@{db_params.get('host')}:{db_params.get('port', 5432)}"
                    f"/{db_params.get('database')}"
                )
            else:
                click.echo("Error: Must provide --dsn or --config", err=True)
                sys.exit(1)

        click.echo(f"Starting monitor (check every {check_interval}s)...")
        click.echo(f"DSN: {dsn.split('@')[1] if '@' in dsn else dsn}")
        click.echo(
            "Retention: jobs "
            + (f"older than {retention_days}d" if retention_days else "kept forever")
            + ", checkpoints "
            + (
                f"{checkpoint_retention_days}d after the job terminates"
                if checkpoint_retention_days
                else "kept as long as the job"
            )
        )

        asyncio.run(
            monitor(
                dsn,
                check_interval=check_interval,
                liveness_grace_seconds=liveness_grace,
                claimed_grace_seconds=claimed_grace,
                retention_days=retention_days,
                checkpoint_retention_days=checkpoint_retention_days,
                retention_batch_size=retention_batch_size,
                retention_max_seconds=retention_max_seconds,
            )
        )

    main()


if __name__ == "__main__":
    cli()
