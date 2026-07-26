"""
Timeout Monitor - Phase 2 Job Timeout Enforcement

Monitors and enforces job timeouts. This is a safety mechanism for cases where:
1. Worker crashes mid-execution
2. asyncio.wait_for() fails to enforce timeout
3. Worker is killed ungracefully

Runs as a separate process, checks every 10 seconds.
"""

from __future__ import annotations

import asyncio

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

from . import db


async def handle_timed_out_job(
    pool: asyncpg.Pool | asyncpg.Connection,
    job_id: int,
    job_class: str,
    admin_data: dict | None,
    error_count: int,
) -> None:
    """
    Handle a job that has exceeded its timeout.

    Args:
        pool: Database connection or pool to execute against
        job_id: ID of timed-out job
        job_class: Job class name
        admin_data: Job's admin_data (can be dict or JSON string)
        error_count: Current error count
    """
    # admin_data is automatically decoded by asyncpg custom codec
    on_timeout = (admin_data or {}).get("on_timeout", "retry")
    max_retries = (admin_data or {}).get("max_retries", 10)

    logger.warning(
        f"Job {job_id} ({job_class}) exceeded timeout, "
        f"action: {on_timeout}, attempt {error_count + 1}/{max_retries}"
    )

    if on_timeout == "retry" and (error_count + 1) < max_retries:
        # Reset to queued for retry with exponential backoff
        from .retry_strategies import calculate_retry_from_job

        # Get the original job to calculate retry delay
        job = await pool.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        if not job:
            return

        retry_delay = calculate_retry_from_job(job, error_count + 1)

        await pool.execute(
            """
            UPDATE jorb
            SET state = 'queued',
                timeout_at = NULL,
                error_count = error_count + 1,
                error_message = $2,
                run_after = TIMEZONE('utc', clock_timestamp()) + $3::interval,
                updated = TIMEZONE('utc', clock_timestamp())
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
        # Mark as crashed (exceeded max retries or on_timeout='fail')
        reason = (
            "max retries exceeded"
            if (error_count + 1) >= max_retries
            else "on_timeout=fail"
        )

        await pool.execute(
            """
            UPDATE jorb
            SET state = 'crashed',
                timeout_at = NULL,
                error_count = error_count + 1,
                error_message = $2,
                finished = clock_timestamp(),
                updated = TIMEZONE('utc', clock_timestamp())
            WHERE id = $1
              AND state = 'running'
            """,
            job_id,
            f"Timeout exceeded - marked as failed ({reason})",
        )

        logger.error(f"Job {job_id} marked as crashed: {reason}")


async def sweep_timed_out_jobs(pool: asyncpg.Pool, batch_size: int = 100) -> int:
    """Find running jobs past their deadline and retry/fail them.

    The SELECT ... FOR UPDATE SKIP LOCKED and the per-job updates run inside
    one transaction so the row locks are actually held while handling the
    jobs — multiple monitor instances cannot double-process a job, and a
    worker finishing a job concurrently is serialized against us (the state
    guards in the UPDATEs then make the late writer a no-op).
    """
    async with pool.acquire() as conn, conn.transaction():
        timed_out = await conn.fetch(
            """
                SELECT id, job_class, timeout_at, admin_data, error_count
                FROM jorb
                WHERE state = 'running'
                  AND timeout_at IS NOT NULL
                  AND timeout_at < NOW()
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


async def sweep_stale_claimed_jobs(
    pool: asyncpg.Pool, claimed_grace_seconds: int = 300, batch_size: int = 100
) -> int:
    """Requeue jobs stuck in 'claimed' whose worker died before starting them.

    A healthy worker moves a job claimed -> running almost immediately, so a
    job sitting in 'claimed' long past that window belonged to a worker that
    died (on any host — this is the global safety net; workers additionally
    recover their own host's jobs by pid-liveness at startup).

    Single atomic UPDATE, so concurrent monitor instances are safe.
    """
    import datetime

    grace = datetime.timedelta(seconds=claimed_grace_seconds)
    requeued = await pool.fetch(
        """
        UPDATE jorb
        SET state = 'queued',
            run_after = TIMEZONE('utc', clock_timestamp()),
            timeout_at = NULL,
            updated = TIMEZONE('utc', clock_timestamp())
        WHERE id IN (
            SELECT id FROM jorb
            WHERE state = 'claimed'
              AND updated < TIMEZONE('utc', clock_timestamp()) - $1::interval
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
            f"Requeued stale claimed job {row['id']} ({row['job_class']}) "
            f"abandoned by worker on {row['worker_host']}"
        )

    return len(requeued)


async def timeout_monitor(
    dsn: str,
    check_interval: int = 10,
    batch_size: int = 100,
    claimed_grace_seconds: int = 300,
) -> None:
    """
    Monitor and enforce job timeouts, and reap jobs abandoned by dead workers.

    Every check_interval seconds:
    1. running jobs past timeout_at are retried or failed per their
       on_timeout configuration;
    2. jobs stuck in 'claimed' longer than claimed_grace_seconds are
       requeued (their worker died between claiming and starting).

    Args:
        dsn: PostgreSQL connection string
        check_interval: How often to check for timeouts (seconds)
        batch_size: Maximum jobs to process per check
        claimed_grace_seconds: Age before a 'claimed' job counts as abandoned
    """

    pool = await db.create_pool(dsn, min_size=1, max_size=2)

    logger.info(
        f"Timeout monitor started (check every {check_interval}s, batch size {batch_size})"
    )

    try:
        while True:
            try:
                handled = await sweep_timed_out_jobs(pool, batch_size)
                if handled:
                    logger.info(f"Timeout monitor: Handled {handled} timed-out jobs")

                await sweep_stale_claimed_jobs(pool, claimed_grace_seconds, batch_size)

            except Exception as e:
                import traceback

                logger.error(
                    f"Timeout monitor error: {e}\nFull traceback: {traceback.format_exc()}"
                )

            await asyncio.sleep(check_interval)

    finally:
        await pool.close()
        logger.info("Timeout monitor stopped")


def run_timeout_monitor(dsn: str) -> None:
    """
    Run timeout monitor (blocking).

    This is the entry point for running the monitor as a separate process.

    Args:
        dsn: PostgreSQL connection string
    """
    asyncio.run(timeout_monitor(dsn))


def cli() -> None:
    """CLI entry point for timeout monitor."""
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
        "--check-interval", default=10, help="Check interval in seconds (default: 10)"
    )
    @click.option(
        "--claimed-grace",
        default=300,
        help="Seconds before a 'claimed' job counts as abandoned (default: 300)",
    )
    def main(dsn: str, config: str, check_interval: int, claimed_grace: int) -> None:
        """Start pyjobby timeout monitor process."""
        import asyncio

        if not dsn:
            if config:
                from urllib.parse import quote

                from .configloader import load_config_from_file

                cfg = load_config_from_file(config, keys=["db_params"])
                db_params = cfg.get("db_params", {})
                # Build DSN from params (URL-encode credentials)
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

        click.echo(f"Starting timeout monitor (check every {check_interval}s)...")
        click.echo(
            f"DSN: {dsn.split('@')[1] if '@' in dsn else dsn}"
        )  # Don't show password

        asyncio.run(
            timeout_monitor(
                dsn,
                check_interval=check_interval,
                claimed_grace_seconds=claimed_grace,
            )
        )

    main()


if __name__ == "__main__":
    cli()
