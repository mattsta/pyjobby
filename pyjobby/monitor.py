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

Requeues bump nothing themselves: the next claim increments ``run_epoch``,
which fences any still-running stale execution out of the row.

Run it: ``pj-monitor --config ./pyjobby.conf.py`` (one instance is enough;
several are safe — every sweep is a single atomic statement or a
transaction holding its row locks).
"""

from __future__ import annotations

import asyncio
import datetime

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

        logger.info(f"Job {job_id} requeued for retry in {retry_delay.total_seconds()}s")
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

    Single atomic statements; safe with concurrent monitor instances."""
    grace = datetime.timedelta(seconds=liveness_grace_seconds)

    requeued = await pool.fetch(
        """
        UPDATE jorb
        SET state = 'queued',
            run_after = now(),
            timeout_at = NULL,
            updated = now()
        WHERE id IN (
            SELECT j.id FROM jorb j
            JOIN jorb_worker w ON w.id = j.claimed_by
            WHERE j.state IN ('claimed', 'running')
              AND w.shutdown_at IS NULL
              AND w.last_seen < now() - $1::interval
            FOR UPDATE OF j SKIP LOCKED
            LIMIT $2
        )
          AND state IN ('claimed', 'running')
        RETURNING id, job_class, worker_host, claimed_by
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
        UPDATE jorb
        SET state = 'queued',
            run_after = now(),
            timeout_at = NULL,
            updated = now()
        WHERE id IN (
            SELECT id FROM jorb
            WHERE state = 'claimed'
              AND claimed_by IS NULL
              AND updated < now() - $1::interval
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
          AND state = 'claimed'
        RETURNING id, job_class, worker_host
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


async def monitor(
    dsn: str,
    check_interval: float = 10,
    batch_size: int = 100,
    liveness_grace_seconds: float = 60,
    claimed_grace_seconds: float = 300,
) -> None:
    """Run all sweeps every ``check_interval`` seconds, forever."""
    pool = await db.create_pool(dsn, min_size=1, max_size=2)

    logger.info(
        f"Monitor started (interval {check_interval}s, "
        f"liveness grace {liveness_grace_seconds}s)"
    )

    try:
        while True:
            try:
                timed_out = await sweep_timed_out_jobs(pool, batch_size)
                if timed_out:
                    logger.info(f"Handled {timed_out} timed-out jobs")

                await sweep_dead_workers(pool, liveness_grace_seconds)
                await sweep_unregistered_claims(pool, claimed_grace_seconds)

            except Exception as e:
                import traceback

                logger.error(
                    f"Monitor error: {e}\nFull traceback: {traceback.format_exc()}"
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
    def main(
        dsn: str | None,
        config: str | None,
        check_interval: float,
        liveness_grace: float,
        claimed_grace: float,
    ) -> None:
        """Run the pyjobby monitor (timeouts + dead-worker recovery)."""
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

        asyncio.run(
            monitor(
                dsn,
                check_interval=check_interval,
                liveness_grace_seconds=liveness_grace,
                claimed_grace_seconds=claimed_grace,
            )
        )

    main()


if __name__ == "__main__":
    cli()
