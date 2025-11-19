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
    # Custom codec init function
    async def init_connection(conn):
        try:
            import orjson

            def orjson_encoder(obj):
                return orjson.dumps(obj).decode('utf-8')

            def orjson_decoder(s):
                return orjson.loads(s)

            await conn.set_type_codec(
                "json",
                encoder=orjson_encoder,
                decoder=orjson_decoder,
                schema="pg_catalog",
                format="text"
            )
            await conn.set_type_codec(
                "jsonb",
                encoder=orjson_encoder,
                decoder=orjson_decoder,
                schema="pg_catalog",
                format="text"
            )
        except ImportError:
            pass

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, init=init_connection)

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
                import traceback
                logger.error(
                    f"Timeout monitor error: {e}\n"
                    f"Full traceback: {traceback.format_exc()}"
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
        '--dsn',
        envvar='PYJOBBY_DSN',
        required=False,
        help='PostgreSQL DSN (or use PYJOBBY_DSN env var)'
    )
    @click.option(
        '--config',
        type=click.Path(exists=True),
        help='Path to pyjobby.conf.py'
    )
    @click.option(
        '--check-interval',
        default=10,
        help='Check interval in seconds (default: 10)'
    )
    def main(dsn: str, config: str, check_interval: int) -> None:
        """Start pyjobby timeout monitor process."""
        import asyncio

        if not dsn:
            if config:
                from .configloader import load_config_from_file
                cfg = load_config_from_file(config, keys=["db_params"])
                db_params = cfg.get("db_params", {})
                # Build DSN from params
                dsn = f"postgresql://{db_params.get('user')}:{db_params.get('password')}@{db_params.get('host')}:{db_params.get('port', 5432)}/{db_params.get('database')}"
            else:
                click.echo("Error: Must provide --dsn or --config", err=True)
                sys.exit(1)

        click.echo(f"Starting timeout monitor (check every {check_interval}s)...")
        click.echo(f"DSN: {dsn.split('@')[1] if '@' in dsn else dsn}")  # Don't show password

        asyncio.run(timeout_monitor(dsn, check_interval=check_interval))

    main()


if __name__ == '__main__':
    cli()
