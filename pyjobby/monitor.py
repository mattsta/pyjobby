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
4. **Retention**: terminal jobs (and their checkpoints, history, events and
   mailbox) older than ``--retention-days`` are deleted. Opt-in and off by
   default — a platform that runs indefinitely otherwise accumulates every
   checkpoint and every state transition forever, but no fresh install may
   silently start destroying an operator's audit trail.

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

    jorb_step, jorb_event and jorb_mailbox follow via ON DELETE CASCADE.
    jorb_history does NOT — it references jorb.job_id with no foreign key at
    all (it outlives the job on purpose in the schema), so it is deleted here
    explicitly, in the same transaction, or retention would free the small
    tables and leave the largest one growing.

    Bounded and batched like the other sweeps: one bite of ``batch_size`` per
    call, holding only those rows' locks. FOR UPDATE SKIP LOCKED makes
    concurrent monitors partition the backlog instead of colliding on it.
    Returns the number of jobs deleted."""
    retention = datetime.timedelta(days=retention_days)

    async with pool.acquire() as conn, conn.transaction():
        expired = await conn.fetch(
            """
            SELECT j.id
            FROM jorb j
            WHERE j.state = ANY($1::jorbstate[])
              AND COALESCE(j.finished, j.updated) < now() - $2::interval
              AND NOT EXISTS (
                  SELECT 1 FROM jorb w
                  WHERE w.state = 'waiting' AND w.waitfor_job = j.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM jorb w
                  WHERE w.state = 'waiting' AND w.waitfor_group = j.run_group
              )
            ORDER BY j.id
            FOR UPDATE OF j SKIP LOCKED
            LIMIT $3
            """,
            TERMINAL_STATES,
            retention,
            batch_size,
        )
        if not expired:
            return 0

        job_ids = [row["id"] for row in expired]
        await conn.execute(
            "DELETE FROM jorb_history WHERE job_id = ANY($1::bigint[])", job_ids
        )
        await conn.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)

    logger.info(f"Retention deleted {len(job_ids)} expired jobs")
    return len(job_ids)


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

    Single atomic statement; safe with concurrent monitor instances."""
    retention = datetime.timedelta(days=retention_days)

    deleted = await pool.fetch(
        """
        WITH doomed AS MATERIALIZED (
            SELECT id FROM jorb_mailbox
            WHERE consumed_at IS NOT NULL
              AND consumed_at < now() - $1::interval
            ORDER BY id
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

    if deleted:
        logger.info(f"Retention deleted {len(deleted)} consumed mailbox messages")
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


async def monitor(
    dsn: str,
    check_interval: float = 10,
    batch_size: int = 100,
    liveness_grace_seconds: float = 60,
    claimed_grace_seconds: float = 300,
    retention_days: float | None = None,
    retention_batch_size: int = 1000,
) -> None:
    """Run all sweeps every ``check_interval`` seconds, forever.

    ``retention_days=None`` (the default) disables the retention sweeps
    entirely — nothing is deleted unless an operator asks for it."""
    pool = await db.create_pool(dsn, min_size=1, max_size=2)

    logger.info(
        f"Monitor started (interval {check_interval}s, "
        f"liveness grace {liveness_grace_seconds}s, retention "
        f"{f'{retention_days}d' if retention_days is not None else 'disabled'})"
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

            if retention_days is not None:
                days = retention_days
                await _run_sweep(
                    "expired jobs",
                    lambda: sweep_expired_jobs(pool, days, retention_batch_size),
                )
                await _run_sweep(
                    "consumed mailbox",
                    lambda: sweep_consumed_mailbox(pool, days, retention_batch_size),
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
        default=None,
        help="Delete terminal jobs (and their history/checkpoints/events/"
        "mailbox) older than this. Omit to keep everything forever.",
    )
    @click.option(
        "--retention-batch-size",
        default=1000,
        show_default=True,
        help="Maximum jobs deleted per retention sweep",
    )
    def main(
        dsn: str | None,
        config: str | None,
        check_interval: float,
        liveness_grace: float,
        claimed_grace: float,
        retention_days: float | None,
        retention_batch_size: int,
    ) -> None:
        """Run the pyjobby monitor (timeouts, dead-worker recovery, and —
        only with --retention-days — deletion of expired terminal jobs)."""
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
        if retention_days is not None:
            click.echo(
                f"Retention: deleting terminal jobs older than {retention_days}d"
            )

        asyncio.run(
            monitor(
                dsn,
                check_interval=check_interval,
                liveness_grace_seconds=liveness_grace,
                claimed_grace_seconds=claimed_grace,
                retention_days=retention_days,
                retention_batch_size=retention_batch_size,
            )
        )

    main()


if __name__ == "__main__":
    cli()
