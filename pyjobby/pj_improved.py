#!/usr/bin/env python3

"""
Pyjobby Improvements - Validated fixes for confirmed issues

This file contains improvements to address:
1. Broken retry mechanism (reschedule overwritten by crash)
2. Missing worker crash recovery
3. Job execution timeout
4. Max retry limits

All changes are based on careful code analysis and TODO comments in original.
"""

# Changes to STMTS dict:

# NEW: Create retry job without overwriting crashed original
STMTS["create-retry"] = """
    INSERT INTO jorb (
        job_class, kwargs, queue, prio, uid, capability,
        run_after, run_group, waitfor_job, waitfor_group,
        deadline_key, admin_data, state
    )
    SELECT
        job_class, kwargs, queue, prio, uid, capability,
        TIMEZONE('utc', clock_timestamp()) + $2::interval as run_after,
        run_group, waitfor_job, waitfor_group,
        NULL as deadline_key,  -- Don't copy deadline_key for retries
        admin_data, 'queued' as state
    FROM jorb
    WHERE id = $1
    RETURNING id
"""

# Modified crash to NOT reschedule (we'll create separate retry job)
STMTS["crash-with-retry-tracking"] = """
    UPDATE jorb
    SET state = 'crashed',
        error_message = $2,
        error_backtrace = $3,
        error_count = error_count + 1,
        updated = TIMEZONE('utc', clock_timestamp())
    WHERE id = $1
    RETURNING error_count
"""

# NEW: Recover jobs stuck in running/claimed when worker restarts
STMTS["recover-abandoned"] = """
    UPDATE jorb
    SET state = 'queued',
        run_after = TIMEZONE('utc', clock_timestamp()),
        updated = TIMEZONE('utc', clock_timestamp())
    WHERE worker_host = $1
      AND state IN ('claimed', 'running')
      AND updated < TIMEZONE('utc', clock_timestamp()) - INTERVAL '5 minutes'
    RETURNING id, job_class, state
"""


# Modifications to JobSystem class:

class JobSystem:
    """Enhanced JobSystem with proper retry and recovery"""

    # Add configuration
    max_retries: int = 10  # Maximum retry attempts
    job_timeout: int = 3600  # Job timeout in seconds (1 hour default)

    async def run(self) -> None:
        """Run with crash recovery on startup"""
        # ... existing setup code ...

        # ADDITION: Recover abandoned jobs on startup
        abandoned = await self.recover_abandoned_jobs()
        if abandoned:
            logger.warning(
                f"Recovered {len(abandoned)} abandoned jobs from previous crash"
            )

        # ... rest of existing run() code ...

    async def recover_abandoned_jobs(self) -> list:
        """Recover jobs that were running when worker crashed"""
        try:
            recovered = await self.cxn.fetch(
                """
                UPDATE jorb
                SET state = 'queued',
                    run_after = TIMEZONE('utc', clock_timestamp()),
                    updated = TIMEZONE('utc', clock_timestamp())
                WHERE worker_host = $1
                  AND state IN ('claimed', 'running')
                RETURNING id, job_class, state as old_state
                """,
                self.node
            )

            for job in recovered:
                logger.info(
                    f"Recovered abandoned job {job['id']} "
                    f"({job['job_class']}) from state '{job['old_state']}'"
                )

            return list(recovered)
        except Exception as e:
            logger.error(f"Failed to recover abandoned jobs: {e}")
            return []

    async def execute_with_timeout(self, job, klass):
        """Execute job with timeout protection"""
        import asyncio

        # Get job-specific timeout or use default
        timeout = getattr(klass, 'timeout', self.job_timeout)

        try:
            startJobTime = time.perf_counter()
            resultStageA = klass.run()

            if asyncio.iscoroutine(resultStageA):
                result = await asyncio.wait_for(resultStageA, timeout=timeout)
            elif inspect.isasyncgen(resultStageA):
                result = []
                async def collect_with_timeout():
                    return [x async for x in resultStageA]
                result = await asyncio.wait_for(collect_with_timeout(), timeout=timeout)
            else:
                result = resultStageA

            totalJobTime = time.perf_counter() - startJobTime
            return result, totalJobTime

        except asyncio.TimeoutError:
            logger.error(f"Job {job['id']} timed out after {timeout}s")
            raise TimeoutError(f"Job exceeded timeout of {timeout}s")

    async def handle_job_failure(self, job, klass, exception, traceback_str):
        """Handle job failure with proper retry logic"""

        # Mark original job as crashed (for audit trail)
        result = await self.cxn.fetchrow(
            """
            UPDATE jorb
            SET state = 'crashed',
                error_message = $2,
                error_backtrace = $3,
                error_count = error_count + 1,
                updated = TIMEZONE('utc', clock_timestamp())
            WHERE id = $1
            RETURNING error_count
            """,
            job["id"],
            str(exception),
            traceback_str
        )

        error_count = result['error_count'] if result else 0

        # Check if we should retry
        if error_count < self.max_retries:
            # Calculate backoff delay
            rescheduleFor = await klass.rescheduleBackoff(error_count)

            # Create NEW job for retry (don't modify crashed job)
            retry_job_id = await self.cxn.fetchval(
                """
                INSERT INTO jorb (
                    job_class, kwargs, queue, prio, uid, capability,
                    run_after, run_group, admin_data, state, error_count
                )
                SELECT
                    job_class, kwargs, queue, prio, uid, capability,
                    TIMEZONE('utc', clock_timestamp()) + $2::interval as run_after,
                    run_group,
                    jsonb_set(COALESCE(admin_data, '{}'::jsonb), '{parent_job_id}', to_jsonb($1::bigint)),
                    'queued' as state,
                    $3 as error_count
                FROM jorb
                WHERE id = $1
                RETURNING id
                """,
                job["id"],
                rescheduleFor,
                error_count
            )

            logger.info(
                f"[job {job['id']}] Created retry job {retry_job_id} "
                f"(attempt {error_count + 1}/{self.max_retries}) "
                f"scheduled for {rescheduleFor.total_seconds() / 60:.1f} minutes"
            )
        else:
            logger.error(
                f"[job {job['id']}] Max retries ({self.max_retries}) exceeded, "
                f"permanently failed"
            )


# Example of modified exception handler in run() loop:

"""
REPLACE the exception handler (lines 466-496) with:

            except asyncio.TimeoutError as e:
                logger.error(f"[job {job['id']}] Timeout after {self.job_timeout}s")
                await self.handle_job_failure(
                    job, klass, e,
                    f"Job timed out after {self.job_timeout}s"
                )
                error += 1

            except Exception as e:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                logger.exception(
                    "[job {}:{}] Error in {}: {}",
                    job["id"], jname, job["job_class"], e
                )

                traceback_str = "Traceback:\n" + "".join(
                    traceback.format_tb(exc_traceback)
                )

                await self.handle_job_failure(job, klass, e, traceback_str)
                error += 1
"""

# Usage example for jobs with custom timeout:
"""
class LongRunningJob(Job):
    timeout = 7200  # 2 hours

    async def task(self, **kwargs):
        # This job can run up to 2 hours
        await do_long_operation()
"""

# Usage example for jobs with custom max retries:
"""
# In JobSystem configuration:
runner = JobSystem(
    dsn=db_params,
    qname="default",
    capabilities=caps,
    workerId=0,
    max_retries=5,  # Only retry 5 times
    job_timeout=1800,  # 30 minute timeout
)
"""
