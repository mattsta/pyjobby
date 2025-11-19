"""
Timeout Monitor - Phase 2 Job Timeout Enforcement

Monitors and enforces job timeouts. This is a safety mechanism for cases where:
1. Worker crashes mid-execution
2. asyncio.wait_for() fails to enforce timeout
3. Worker is killed ungracefully

Runs as a separate process, checks every 10 seconds.
"""

import asyncio
import asyncpg
import datetime
from loguru import logger
from typing import Optional


async def handle_timed_out_job(
    pool: asyncpg.Pool,
    job_id: int,
    job_class: str,
    admin_data: Optional[dict],
    error_count: int
) -> None:
    """
    Handle a job that has exceeded its timeout.

    Args:
        pool: Database connection pool
        job_id: ID of timed-out job
        job_class: Job class name
        admin_data: Job's admin_data
        error_count: Current error count
    """
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
                run_after = NOW() + $3::interval
            WHERE id = $1
            """,
            job_id,
            "Timeout exceeded - retrying",
            retry_delay
        )

        logger.info(
            f"Job {job_id} requeued for retry in {retry_delay.total_seconds()}s"
        )
    else:
        # Mark as crashed (exceeded max retries or on_timeout='fail')
        reason = "max retries exceeded" if (error_count + 1) >= max_retries else "on_timeout=fail"

        await pool.execute(
            """
            UPDATE jorb
            SET state = 'crashed',
                timeout_at = NULL,
                error_count = error_count + 1,
                error_message = $2
            WHERE id = $1
            """,
            job_id,
            f"Timeout exceeded - marked as failed ({reason})"
        )

        logger.error(
            f"Job {job_id} marked as crashed: {reason}"
        )


async def timeout_monitor(
    dsn: str,
    check_interval: int = 10,
    batch_size: int = 100
) -> None:
    """
    Monitor and enforce job timeouts.

    Checks every check_interval seconds for jobs that have exceeded their
    timeout_at deadline.

    Args:
        dsn: PostgreSQL connection string
        check_interval: How often to check for timeouts (seconds)
        batch_size: Maximum jobs to process per check
    """
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)

    logger.info(
        f"Timeout monitor started (check every {check_interval}s, "
        f"batch size {batch_size})"
    )

    try:
        while True:
            try:
                # Find jobs that exceeded timeout
                timed_out = await pool.fetch(
                    """
                    SELECT id, job_class, timeout_at, admin_data, error_count
                    FROM jorb
                    WHERE state = 'running'
                      AND timeout_at IS NOT NULL
                      AND timeout_at < NOW()
                    FOR UPDATE SKIP LOCKED
                    LIMIT $1
                    """,
                    batch_size
                )

                for job in timed_out:
                    await handle_timed_out_job(
                        pool,
                        job['id'],
                        job['job_class'],
                        job['admin_data'],
                        job['error_count']
                    )

                if timed_out:
                    logger.info(
                        f"Timeout monitor: Handled {len(timed_out)} timed-out jobs"
                    )

            except Exception as e:
                logger.error(f"Timeout monitor error: {e}", exc_info=True)

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


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m pyjobby.timeout_monitor <dsn>")
        sys.exit(1)

    dsn = sys.argv[1]
    run_timeout_monitor(dsn)
