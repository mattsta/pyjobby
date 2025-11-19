#!/usr/bin/env python3
"""
Pyjobby Recurring Scheduler

Production-grade recurring job scheduler with comprehensive safety features:
- Max concurrent jobs per schedule
- Random jitter to prevent thundering herd
- Backpressure handling (skip if queue overloaded)
- Circuit breaker (disable after consecutive failures)
- Comprehensive logging and metrics
"""

import asyncio
import asyncpg
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import random
import json
from dataclasses import dataclass, asdict
from loguru import logger
import time
import pytz


@dataclass
class ScheduleExecutionResult:
    """Result of schedule execution attempt"""
    result: str  # 'success', 'failure', 'skipped'
    job_id: Optional[int] = None
    skip_reason: Optional[str] = None
    error_message: Optional[str] = None
    jitter_applied: int = 0
    queue_depth: Optional[int] = None
    concurrent_jobs: Optional[int] = None
    duration_ms: Optional[int] = None


class ScheduleSafetyManager:
    """
    Encapsulates all safety checks for recurring schedules.

    Prevents runaway job creation through multiple safety mechanisms:
    - Concurrency limiting
    - Backpressure detection
    - Circuit breaker pattern
    - Jitter calculation
    """

    def __init__(self, conn: asyncpg.Connection):
        """
        Initialize safety manager.

        Args:
            conn: Active database connection
        """
        self.conn = conn

    async def check_concurrency(
        self,
        schedule_id: int,
        max_concurrent: int
    ) -> tuple[bool, int]:
        """
        Check if schedule has reached max concurrent jobs limit.

        Args:
            schedule_id: Schedule ID
            max_concurrent: Maximum concurrent jobs allowed

        Returns:
            Tuple of (is_safe: bool, current_count: int)
        """
        # Count jobs from this schedule that are still running
        count = await self.conn.fetchval("""
            SELECT COUNT(*) FROM jorb
            WHERE admin_data->>'schedule_id' = $1
              AND state IN ('queued', 'claimed', 'running', 'waiting')
        """, str(schedule_id))

        is_safe = count < max_concurrent

        logger.debug(
            f"Concurrency check: {count}/{max_concurrent} jobs running "
            f"(safe: {is_safe})"
        )

        return is_safe, count

    async def check_backpressure(
        self,
        queue: str,
        threshold: Optional[int]
    ) -> tuple[bool, int]:
        """
        Check if queue is overloaded (backpressure).

        Args:
            queue: Queue name
            threshold: Max queue depth (None = no limit)

        Returns:
            Tuple of (is_safe: bool, queue_depth: int)
        """
        if threshold is None:
            return True, 0

        # Count jobs in queue that are not finished
        depth = await self.conn.fetchval("""
            SELECT COUNT(*) FROM jorb
            WHERE queue = $1
              AND state IN ('queued', 'claimed', 'running')
        """, queue)

        is_safe = depth < threshold

        logger.debug(
            f"Backpressure check for queue '{queue}': {depth} jobs "
            f"(threshold: {threshold}, safe: {is_safe})"
        )

        return is_safe, depth

    def calculate_jitter(self, jitter_seconds: int) -> int:
        """
        Calculate random jitter delay.

        Args:
            jitter_seconds: Maximum jitter (0 to N seconds)

        Returns:
            Random delay in seconds (0 to jitter_seconds)
        """
        if jitter_seconds <= 0:
            return 0

        jitter = random.randint(0, jitter_seconds)
        logger.debug(f"Jitter calculated: {jitter}s (max: {jitter_seconds}s)")

        return jitter

    async def check_circuit_breaker(
        self,
        schedule: Dict[str, Any]
    ) -> tuple[bool, str]:
        """
        Check if circuit breaker should be triggered.

        If schedule has reached failure threshold, disable it and return False.

        Args:
            schedule: Schedule record dict

        Returns:
            Tuple of (is_safe: bool, reason: str)
        """
        consecutive_failures = schedule['consecutive_failures']
        threshold = schedule['circuit_breaker_threshold']

        if consecutive_failures >= threshold:
            # Circuit breaker triggered! Disable schedule
            await self.conn.execute("""
                UPDATE jorb_schedule
                SET enabled = false,
                    updated = NOW()
                WHERE id = $1
            """, schedule['id'])

            reason = (
                f"Circuit breaker triggered: {consecutive_failures} "
                f"consecutive failures (threshold: {threshold})"
            )

            logger.error(
                f"Schedule '{schedule['name']}' disabled: {reason}",
                extra={
                    'schedule_id': schedule['id'],
                    'schedule_name': schedule['name'],
                    'consecutive_failures': consecutive_failures,
                    'threshold': threshold
                }
            )

            return False, reason

        return True, ""


class ScheduleManager:
    """
    Business logic for schedule management.

    Handles:
    - Creating/updating/deleting schedules
    - Calculating next run times
    - Validating cron expressions
    - Managing schedule state
    """

    def __init__(self, conn: asyncpg.Connection):
        """
        Initialize schedule manager.

        Args:
            conn: Active database connection
        """
        self.conn = conn

    @staticmethod
    def calculate_next_run(cron_expr: str, timezone: str = 'UTC') -> datetime:
        """
        Calculate next run time from cron expression.

        Args:
            cron_expr: Standard cron expression (minute hour day month weekday)
            timezone: Timezone name (default: UTC)

        Returns:
            Next execution time as datetime

        Raises:
            ValueError: If cron expression is invalid
        """
        try:
            from croniter import croniter
            import pytz

            # Get timezone
            tz = pytz.timezone(timezone)

            # Get current time in schedule's timezone
            now = datetime.now(tz)

            # Calculate next run
            cron = croniter(cron_expr, now)
            next_run = cron.get_next(datetime)

            logger.debug(
                f"Calculated next run: {next_run} (cron: {cron_expr}, tz: {timezone})"
            )

            return next_run

        except Exception as e:
            raise ValueError(f"Invalid cron expression '{cron_expr}': {e}")

    async def create_schedule(
        self,
        name: str,
        job_class: str,
        cron_expr: str,
        **kwargs
    ) -> int:
        """
        Create new recurring schedule.

        Args:
            name: Unique schedule name
            job_class: Python job class to execute
            cron_expr: Cron expression
            **kwargs: Additional schedule fields (queue, kwargs, etc.)

        Returns:
            Schedule ID

        Raises:
            ValueError: If validation fails
        """
        # Calculate initial next_run
        timezone = kwargs.get('timezone', 'UTC')
        next_run = self.calculate_next_run(cron_expr, timezone)

        # Insert schedule
        schedule_id = await self.conn.fetchval("""
            INSERT INTO jorb_schedule (
                name, description,
                job_class, kwargs, queue, prio, capability,
                cron_expr, timezone, enabled,
                max_concurrent_jobs, jitter_seconds,
                backpressure_threshold, circuit_breaker_threshold,
                next_run, created_by
            ) VALUES (
                $1, $2,
                $3, $4, $5, $6, $7,
                $8, $9, $10,
                $11, $12,
                $13, $14,
                $15, $16
            )
            RETURNING id
        """,
            name,
            kwargs.get('description'),
            job_class,
            json.dumps(kwargs.get('kwargs', {})),
            kwargs.get('queue', 'default'),
            kwargs.get('prio', 100),
            kwargs.get('capability'),
            cron_expr,
            timezone,
            kwargs.get('enabled', True),
            kwargs.get('max_concurrent_jobs', 1),
            kwargs.get('jitter_seconds', 0),
            kwargs.get('backpressure_threshold', 1000),
            kwargs.get('circuit_breaker_threshold', 5),
            next_run,
            kwargs.get('created_by')
        )

        logger.info(
            f"Created schedule '{name}' (ID: {schedule_id})",
            extra={
                'schedule_id': schedule_id,
                'schedule_name': name,
                'job_class': job_class,
                'cron_expr': cron_expr,
                'next_run': next_run.isoformat()
            }
        )

        return schedule_id

    async def update_schedule_next_run(
        self,
        schedule_id: int,
        cron_expr: str,
        timezone: str
    ) -> None:
        """
        Update schedule's next_run timestamp.

        Args:
            schedule_id: Schedule ID
            cron_expr: Cron expression
            timezone: Timezone name
        """
        next_run = self.calculate_next_run(cron_expr, timezone)

        await self.conn.execute("""
            UPDATE jorb_schedule
            SET next_run = $1,
                updated = NOW()
            WHERE id = $2
        """, next_run, schedule_id)

        logger.debug(
            f"Updated schedule {schedule_id} next_run to {next_run}"
        )

    async def record_execution_success(
        self,
        schedule_id: int
    ) -> None:
        """
        Record successful execution (reset consecutive failures).

        Args:
            schedule_id: Schedule ID
        """
        await self.conn.execute("""
            UPDATE jorb_schedule
            SET run_count = run_count + 1,
                success_count = success_count + 1,
                consecutive_failures = 0,
                last_run = NOW(),
                last_success = NOW(),
                updated = NOW()
            WHERE id = $1
        """, schedule_id)

    async def record_execution_failure(
        self,
        schedule_id: int
    ) -> None:
        """
        Record failed execution (increment consecutive failures).

        Args:
            schedule_id: Schedule ID
        """
        await self.conn.execute("""
            UPDATE jorb_schedule
            SET run_count = run_count + 1,
                failure_count = failure_count + 1,
                consecutive_failures = consecutive_failures + 1,
                last_run = NOW(),
                last_failure = NOW(),
                updated = NOW()
            WHERE id = $1
        """, schedule_id)

    async def record_execution_skip(
        self,
        schedule_id: int,
        reason: str
    ) -> None:
        """
        Record skipped execution.

        Args:
            schedule_id: Schedule ID
            reason: Why execution was skipped
        """
        await self.conn.execute("""
            UPDATE jorb_schedule
            SET skip_count = skip_count + 1,
                updated = NOW()
            WHERE id = $1
        """, schedule_id)


class SchedulerWorker:
    """
    Main scheduler worker that executes recurring schedules.

    Polls database every minute for due schedules and creates jobs with
    comprehensive safety checks and logging.
    """

    def __init__(
        self,
        conn: asyncpg.Connection,
        poll_interval: int = 60
    ):
        """
        Initialize scheduler worker.

        Args:
            conn: Active database connection
            poll_interval: Seconds between polls (default: 60)
        """
        self.conn = conn
        self.poll_interval = poll_interval
        self.safety = ScheduleSafetyManager(conn)
        self.manager = ScheduleManager(conn)
        self.stop_requested = False

        # Metrics
        self.executions_total = 0
        self.successes_total = 0
        self.failures_total = 0
        self.skips_total = 0

    async def find_due_schedules(self) -> List[Dict[str, Any]]:
        """
        Find all schedules that are due to run.

        Returns:
            List of schedule records
        """
        records = await self.conn.fetch("""
            SELECT * FROM jorb_schedule
            WHERE enabled = true
              AND next_run <= NOW()
            ORDER BY next_run
            FOR UPDATE SKIP LOCKED
        """)

        schedules = [dict(r) for r in records]

        logger.debug(f"Found {len(schedules)} schedules due to run")

        return schedules

    async def create_scheduled_job(
        self,
        schedule: Dict[str, Any],
        scheduled_time: datetime
    ) -> Optional[int]:
        """
        Create job for schedule with deadline key.

        Args:
            schedule: Schedule record
            scheduled_time: When job should have run

        Returns:
            Job ID if created, None if duplicate

        Raises:
            Exception: If job creation fails
        """
        # Generate deadline key to prevent duplicates
        deadline_key = f"schedule:{schedule['id']}:{scheduled_time.isoformat()}"

        # Prepare admin_data with schedule metadata
        admin_data = {
            'schedule_id': str(schedule['id']),  # Store as string for consistency
            'schedule_name': schedule['name'],
            'scheduled_time': scheduled_time.isoformat()
        }

        # Convert scheduled_time to naive UTC if it's timezone-aware
        run_after_time = scheduled_time
        if hasattr(scheduled_time, 'tzinfo') and scheduled_time.tzinfo is not None:
            # Convert to UTC and remove timezone info
            run_after_time = scheduled_time.astimezone(pytz.UTC).replace(tzinfo=None)

        try:
            job_id = await self.conn.fetchval("""
                INSERT INTO jorb (
                    job_class, kwargs, queue, prio, capability,
                    deadline_key, run_after, admin_data, state
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'queued')
                RETURNING id
            """,
                schedule['job_class'],
                schedule['kwargs'],  # Dict - custom codec handles conversion
                schedule['queue'],
                schedule['prio'],
                schedule['capability'],
                deadline_key,
                run_after_time,
                admin_data  # Dict - custom codec handles conversion
            )

            logger.info(
                f"Created job {job_id} for schedule '{schedule['name']}'",
                extra={
                    'schedule_id': schedule['id'],
                    'schedule_name': schedule['name'],
                    'job_id': job_id,
                    'job_class': schedule['job_class'],
                    'queue': schedule['queue']
                }
            )

            return job_id

        except asyncpg.UniqueViolationError:
            # Job already created (duplicate prevented by deadline key)
            logger.warning(
                f"Schedule '{schedule['name']}': job already exists (duplicate prevented)",
                extra={
                    'schedule_id': schedule['id'],
                    'schedule_name': schedule['name'],
                    'deadline_key': deadline_key
                }
            )
            return None

    async def log_execution(
        self,
        schedule: Dict[str, Any],
        scheduled_time: datetime,
        result: ScheduleExecutionResult
    ) -> None:
        """
        Log execution to jorb_schedule_log.

        Args:
            schedule: Schedule record
            scheduled_time: When job should have run
            result: Execution result
        """
        await self.conn.execute("""
            INSERT INTO jorb_schedule_log (
                schedule_id, schedule_name,
                scheduled_time, actual_time,
                result, skip_reason,
                job_id, error_message,
                duration_ms, queue_depth_at_run,
                concurrent_jobs_at_run, jitter_applied_seconds
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
            schedule['id'],
            schedule['name'],
            scheduled_time,
            datetime.utcnow(),
            result.result,
            result.skip_reason,
            result.job_id,
            result.error_message,
            result.duration_ms,
            result.queue_depth,
            result.concurrent_jobs,
            result.jitter_applied
        )

    async def execute_schedule(
        self,
        schedule: Dict[str, Any]
    ) -> ScheduleExecutionResult:
        """
        Execute single schedule with all safety checks.

        Args:
            schedule: Schedule record

        Returns:
            Execution result
        """
        start_time = time.time()
        scheduled_time = schedule['next_run']

        logger.debug(
            f"Executing schedule '{schedule['name']}'",
            extra={
                'schedule_id': schedule['id'],
                'schedule_name': schedule['name'],
                'scheduled_time': scheduled_time.isoformat()
            }
        )

        # Safety check 1: Circuit breaker
        circuit_ok, circuit_reason = await self.safety.check_circuit_breaker(schedule)
        if not circuit_ok:
            result = ScheduleExecutionResult(
                result='skipped',
                skip_reason='circuit_breaker'
            )
            await self.manager.record_execution_skip(schedule['id'], 'circuit_breaker')
            return result

        # Safety check 2: Concurrency limit
        concurrency_ok, concurrent_count = await self.safety.check_concurrency(
            schedule['id'],
            schedule['max_concurrent_jobs']
        )
        if not concurrency_ok:
            logger.warning(
                f"Schedule '{schedule['name']}' skipped: max_concurrent limit "
                f"({concurrent_count}/{schedule['max_concurrent_jobs']})",
                extra={
                    'schedule_id': schedule['id'],
                    'schedule_name': schedule['name'],
                    'concurrent_jobs': concurrent_count,
                    'max_concurrent': schedule['max_concurrent_jobs']
                }
            )
            result = ScheduleExecutionResult(
                result='skipped',
                skip_reason='max_concurrent',
                concurrent_jobs=concurrent_count
            )
            await self.manager.record_execution_skip(schedule['id'], 'max_concurrent')
            return result

        # Safety check 3: Backpressure
        backpressure_ok, queue_depth = await self.safety.check_backpressure(
            schedule['queue'],
            schedule['backpressure_threshold']
        )
        if not backpressure_ok:
            logger.warning(
                f"Schedule '{schedule['name']}' skipped: backpressure "
                f"(queue depth: {queue_depth}, threshold: {schedule['backpressure_threshold']})",
                extra={
                    'schedule_id': schedule['id'],
                    'schedule_name': schedule['name'],
                    'queue_depth': queue_depth,
                    'backpressure_threshold': schedule['backpressure_threshold']
                }
            )
            result = ScheduleExecutionResult(
                result='skipped',
                skip_reason='backpressure',
                queue_depth=queue_depth
            )
            await self.manager.record_execution_skip(schedule['id'], 'backpressure')
            return result

        # Safety feature 4: Apply jitter
        jitter = self.safety.calculate_jitter(schedule['jitter_seconds'])
        if jitter > 0:
            logger.debug(
                f"Schedule '{schedule['name']}' applying jitter: {jitter}s",
                extra={
                    'schedule_id': schedule['id'],
                    'schedule_name': schedule['name'],
                    'jitter_seconds': jitter
                }
            )
            await asyncio.sleep(jitter)

        # Create job
        try:
            job_id = await self.create_scheduled_job(schedule, scheduled_time)

            if job_id:
                # Success!
                await self.manager.record_execution_success(schedule['id'])

                duration_ms = int((time.time() - start_time) * 1000)

                logger.info(
                    f"Schedule '{schedule['name']}' executed successfully "
                    f"(job_id: {job_id})",
                    extra={
                        'schedule_id': schedule['id'],
                        'schedule_name': schedule['name'],
                        'job_id': job_id,
                        'duration_ms': duration_ms,
                        'jitter_applied': jitter
                    }
                )

                return ScheduleExecutionResult(
                    result='success',
                    job_id=job_id,
                    jitter_applied=jitter,
                    queue_depth=queue_depth,
                    concurrent_jobs=concurrent_count,
                    duration_ms=duration_ms
                )
            else:
                # Duplicate (deadline key collision)
                try:
                    await self.manager.record_execution_skip(schedule['id'], 'duplicate')
                except asyncpg.InFailedSQLTransactionError:
                    # Transaction already aborted from UniqueViolationError
                    # This can happen in test environments with transaction isolation
                    pass

                return ScheduleExecutionResult(
                    result='skipped',
                    skip_reason='duplicate'
                )

        except Exception as e:
            # Failure!
            await self.manager.record_execution_failure(schedule['id'])

            duration_ms = int((time.time() - start_time) * 1000)

            logger.error(
                f"Schedule '{schedule['name']}' failed: {e}",
                extra={
                    'schedule_id': schedule['id'],
                    'schedule_name': schedule['name'],
                    'error': str(e),
                    'duration_ms': duration_ms
                }
            )

            return ScheduleExecutionResult(
                result='failure',
                error_message=str(e),
                duration_ms=duration_ms
            )

    async def run(self) -> None:
        """Main scheduler loop"""
        logger.info(
            f"Scheduler worker started (poll interval: {self.poll_interval}s)"
        )

        while not self.stop_requested:
            try:
                # Find due schedules
                schedules = await self.find_due_schedules()

                # Execute each schedule
                for schedule in schedules:
                    try:
                        # Execute schedule
                        result = await self.execute_schedule(schedule)

                        # Log execution
                        await self.log_execution(
                            schedule,
                            schedule['next_run'],
                            result
                        )

                        # Update metrics
                        self.executions_total += 1
                        if result.result == 'success':
                            self.successes_total += 1
                        elif result.result == 'failure':
                            self.failures_total += 1
                        elif result.result == 'skipped':
                            self.skips_total += 1

                        # Update next_run
                        await self.manager.update_schedule_next_run(
                            schedule['id'],
                            schedule['cron_expr'],
                            schedule['timezone']
                        )

                    except Exception as e:
                        logger.error(
                            f"Failed to execute schedule '{schedule['name']}': {e}",
                            exc_info=True
                        )

                # Log metrics every 10 iterations
                if self.executions_total % 10 == 0 and self.executions_total > 0:
                    logger.info(
                        f"Scheduler metrics: {self.executions_total} executions "
                        f"({self.successes_total} success, "
                        f"{self.failures_total} failures, "
                        f"{self.skips_total} skipped)"
                    )

                # Sleep until next poll
                await asyncio.sleep(self.poll_interval)

            except Exception as e:
                logger.error(f"Scheduler main loop error: {e}", exc_info=True)
                await asyncio.sleep(10)  # Shorter sleep on error

        logger.info("Scheduler worker stopped")

    def stop(self) -> None:
        """Request graceful shutdown"""
        logger.info("Scheduler stop requested")
        self.stop_requested = True
