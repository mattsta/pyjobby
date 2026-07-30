#!/usr/bin/env python3
"""``pj-scheduler``: the daemon that turns cron expressions into jobs.

It polls ``jorb_schedule`` for rows whose ``next_run`` has arrived, enqueues
one job per due tick, and advances ``next_run``. Nothing else in the platform
mints work on a timer.

SEVERAL INSTANCES ARE SAFE, and that is the design constraint everything here
answers to: a schedule row is LOCKED while it is being fired, so exactly one
scheduler owns a tick, and the enqueue itself carries a deadline key so even
a lost lock cannot produce two jobs for one tick. Running one is enough;
running two is how the schedule survives a host.

A SCHEDULE IS A GENERATOR, WHICH IS WHY IT NEEDS BRAKES. Every other producer
in the system is a caller who will notice; a schedule fires whether or not
anything is consuming, forever, so a misconfigured one is an unbounded stream
rather than a bad request. Hence four refusals. Every one of them records
what it did in ``jorb_schedule_log``, and they split by whether the next poll
could plausibly answer differently.

SKIP THIS TICK, try again next poll -- the condition is transient:

* **max_concurrent_jobs** -- this schedule's own jobs still in flight
  (``CONCURRENCY_COUNT_SQL``); a job that takes longer than its period would
  otherwise pile up its own successors.
* **backpressure_threshold** -- the target queue's depth
  (``BACKPRESSURE_COUNT_SQL``); firing into a queue nobody is draining just
  makes the backlog it is measuring worse.

DISABLE THE SCHEDULE -- polling again cannot change the answer, and a
schedule left enabled in these states produces one failure per poll forever:

* **circuit_breaker_threshold** -- consecutive failures; a schedule whose
  jobs always crash stops rather than filling the DLQ on a timer.
* **the fleet's priority ceiling** -- a priority above what any worker claims
  would mint a job that is never claimed, never fails and never reaches the
  DLQ. (An unevaluatable cron expression or timezone is disabled by the same
  path and for the same reason -- see ``disable_unevaluatable``.)

``jitter_seconds`` spreads the fire time of schedules that share a minute;
``backfill_limit`` (0 by default, and it stays 0) decides how many ticks
missed while nothing was running are caught up rather than skipped.

Both counting statements are module constants because ``pj-bench plans``
EXPLAINs them: they run once per firing of every schedule, and each has a
partial index whose predicate they must match literally -- see each constant.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

from .client import ENQUEUE_SQL, JobClient
from .cron import missed_cron_runs, next_cron_run
from .db import utcnow
from .enqueue_rules import DEFAULT_PRIO_CEILING
from .lifecycle import IN_FLIGHT_STATES_SQL, LIVE_STATES_SQL

#: "How many of this schedule's jobs are still in flight?", asked once per
#: firing of every schedule by ``ScheduleSafetyManager.check_concurrency``.
#:
#: A module constant, and not an inline string, for the same reason the
#: monitor's sweeps are: ``tests/test_scale_plans.py`` EXPLAINs THIS statement.
#: A plan gate that reads a copy of the query certifies one nobody runs the
#: moment the two drift.
#:
#: The state list is spelled out rather than passed as a parameter because it
#: has to be *syntactically identical* to the predicate of
#: ``jorb_schedule_id_idx``. PostgreSQL proves a partial index usable only by
#: proving the query's own clauses imply its predicate, and it does that by
#: matching expressions -- it cannot derive the list from a bound array
#: parameter. Written as ``state = ANY($2)`` this query is correct, index-less
#: and a sequential scan of the whole job table, which is precisely the defect
#: the column and the index were added to fix.
CONCURRENCY_COUNT_SQL = f"""
    SELECT count(*) FROM jorb
     WHERE schedule_id = $1
       AND state IN ({LIVE_STATES_SQL})
"""

#: The backpressure depth count, split into one arm per partial index.
#: A single predicate spanning queued AND claimed/running matches neither
#: ``jorb_claim_idx`` (partial on queued) nor ``jorb_inflight_idx`` (partial
#: on claimed/running) and collapses into a sequential scan — the identical
#: defect diagnosed and fixed at CONCURRENCY_COUNT_SQL above, in the
#: monitor's terminal-states predicate, and in the websocket snapshot. The
#: queued arm walks the queue's own backlog (the number being measured);
#: the in-flight arm walks fleet-wide in-flight work and discards other
#: queues', bounded by workers, never by the table.
BACKPRESSURE_COUNT_SQL = f"""
    SELECT (SELECT count(*) FROM jorb
             WHERE queue = $1 AND state = 'queued')
         + (SELECT count(*) FROM jorb
             WHERE state IN ({IN_FLIGHT_STATES_SQL}) AND queue = $1)
"""


@dataclass
class ScheduleExecutionResult:
    """Result of schedule execution attempt"""

    result: str  # 'success', 'failure', 'skipped'
    job_id: int | None = None
    skip_reason: str | None = None
    error_message: str | None = None
    jitter_applied: int = 0
    queue_depth: int | None = None
    concurrent_jobs: int | None = None
    duration_ms: int | None = None


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
        self, schedule_id: int, max_concurrent: int
    ) -> tuple[bool, int]:
        """
        Check if schedule has reached max concurrent jobs limit.

        Args:
            schedule_id: Schedule ID
            max_concurrent: Maximum concurrent jobs allowed

        Returns:
            Tuple of (is_safe: bool, current_count: int)
        """
        count = await self.conn.fetchval(
            CONCURRENCY_COUNT_SQL,
            schedule_id,
        )

        is_safe = count < max_concurrent

        logger.debug(
            f"Concurrency check: {count}/{max_concurrent} jobs running (safe: {is_safe})"
        )

        return is_safe, count

    async def check_backpressure(
        self, queue: str, threshold: int | None
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

        # Count jobs in queue that are not finished (see the constant for
        # why this is two index-backed arms rather than one predicate)
        depth = await self.conn.fetchval(BACKPRESSURE_COUNT_SQL, queue)

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

    async def check_circuit_breaker(self, schedule: dict[str, Any]) -> tuple[bool, str]:
        """
        Check if circuit breaker should be triggered.

        If schedule has reached failure threshold, disable it and return False.

        Args:
            schedule: Schedule record dict

        Returns:
            Tuple of (is_safe: bool, reason: str)
        """
        consecutive_failures = schedule["consecutive_failures"]
        threshold = schedule["circuit_breaker_threshold"]

        if consecutive_failures >= threshold:
            # Circuit breaker triggered! Disable schedule
            await self.conn.execute(
                """
                UPDATE jorb_schedule
                SET enabled = false,
                    updated = NOW()
                WHERE id = $1
            """,
                schedule["id"],
            )

            reason = (
                f"Circuit breaker triggered: {consecutive_failures} "
                f"consecutive failures (threshold: {threshold})"
            )

            logger.error(
                f"Schedule '{schedule['name']}' disabled: {reason}",
                extra={
                    "schedule_id": schedule["id"],
                    "schedule_name": schedule["name"],
                    "consecutive_failures": consecutive_failures,
                    "threshold": threshold,
                },
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
    def calculate_next_run(
        cron_expr: str, timezone: str = "UTC", after: datetime | None = None
    ) -> datetime:
        """
        Calculate next run time from cron expression.

        Args:
            cron_expr: Standard cron expression (minute hour day month weekday)
            timezone: Timezone name (default: UTC)
            after: The instant to advance FROM, exclusive. Defaults to this
                process's clock, which is correct only where nothing has
                already judged the schedule against another one.

        **PASS ``after`` FROM THE DATABASE CLOCK ON THE FIRING PATH.**
        Due-ness is decided by ``next_run <= NOW()`` -- the DATABASE's now --
        and every advance has to be measured against that same clock or the
        two disagree by the host's skew. A scheduler whose clock lags the
        database by more than the fired job's time-to-claim advances
        ``next_run`` to an instant the database ALREADY considers due, fires
        the same tick again on the next poll, and keeps doing it: the per-tick
        ``deadline_key`` only collapses duplicates while the earlier job is
        still queued (``jorb_deadline_idx`` is partial on ``state='queued'``),
        so an ordinary fast worker re-arms the key by claiming. Reproduced at a
        120s skew: four jobs for one cron tick. One clock domain per decision,
        the rule ``pj.STMTS['now']`` states for durable sleep.

        Returns:
            Next execution time as datetime

        Raises:
            ValueError: If cron expression is invalid
        """
        next_run = next_cron_run(cron_expr, timezone, after=after)
        logger.debug(
            f"Calculated next run: {next_run} (cron: {cron_expr}, tz: {timezone})"
        )
        return next_run

    async def create_schedule(
        self, name: str, job_class: str, cron_expr: str, **kwargs: Any
    ) -> int:
        """
        Create new recurring schedule.

        Args:
            name: Unique schedule name
            job_class: Python job class to execute
            cron_expr: Cron expression
            **kwargs: Additional schedule fields (queue, priority, kwargs,
                capability, timezone, enabled, ...). The API vocabulary is
                ``priority``, as it is on enqueue and on AdminAPI; ``prio``
                is the COLUMN name and stays on the SQL side of the INSERT
                below.

        Returns:
            Schedule ID

        Raises:
            ValueError: If validation fails
        """
        # Calculate initial next_run, against the DATABASE's clock: the
        # scheduler judges due-ness with `next_run <= NOW()`, so a creating
        # host whose clock lags writes a schedule that is already due and fires
        # the instant it is created. One clock domain per decision, exactly as
        # on the firing path (see calculate_next_run).
        timezone = kwargs.get("timezone", "UTC")
        next_run = self.calculate_next_run(
            cron_expr, timezone, after=await self.conn.fetchval("SELECT now()")
        )

        # Insert schedule
        schedule_id: int = await self.conn.fetchval(
            """
            INSERT INTO jorb_schedule (
                name, description,
                job_class, kwargs, queue, prio, capability,
                cron_expr, timezone, enabled,
                max_concurrent_jobs, jitter_seconds, backfill_limit,
                backpressure_threshold, circuit_breaker_threshold,
                next_run, created_by
            ) VALUES (
                $1, $2,
                $3, $4, $5, $6, $7,
                $8, $9, $10,
                $11, $12, $13,
                $14, $15,
                $16, $17
            )
            RETURNING id
        """,
            name,
            kwargs.get("description"),
            job_class,
            kwargs.get("kwargs", {}),
            kwargs.get("queue", "default"),
            kwargs.get("priority", 100),
            kwargs.get("capability"),
            cron_expr,
            timezone,
            kwargs.get("enabled", True),
            kwargs.get("max_concurrent_jobs", 1),
            kwargs.get("jitter_seconds", 0),
            kwargs.get("backfill_limit", 0),
            kwargs.get("backpressure_threshold", 1000),
            kwargs.get("circuit_breaker_threshold", 5),
            next_run,
            kwargs.get("created_by"),
        )

        logger.info(
            f"Created schedule '{name}' (ID: {schedule_id})",
            extra={
                "schedule_id": schedule_id,
                "schedule_name": name,
                "job_class": job_class,
                "cron_expr": cron_expr,
                "next_run": next_run.isoformat(),
            },
        )

        return schedule_id

    async def set_next_run(self, schedule_id: int, next_run: datetime) -> None:
        """
        Store a schedule's next_run timestamp.

        Takes an already-computed time rather than a cron expression: the
        caller must be able to evaluate the expression BEFORE it starts
        firing the schedule, because a cron/timezone that only fails at this
        point would roll back the whole firing transaction and leave next_run
        in the past forever.

        Args:
            schedule_id: Schedule ID
            next_run: When the schedule should fire next
        """
        await self.conn.execute(
            """
            UPDATE jorb_schedule
            SET next_run = $1,
                updated = NOW()
            WHERE id = $2
        """,
            next_run,
            schedule_id,
        )

        logger.debug(f"Updated schedule {schedule_id} next_run to {next_run}")

    async def record_execution_success(self, schedule_id: int) -> None:
        """
        Record successful execution (reset consecutive failures).

        Args:
            schedule_id: Schedule ID
        """
        await self.conn.execute(
            """
            UPDATE jorb_schedule
            SET run_count = run_count + 1,
                success_count = success_count + 1,
                consecutive_failures = 0,
                last_run = NOW(),
                last_success = NOW(),
                updated = NOW()
            WHERE id = $1
        """,
            schedule_id,
        )

    async def record_execution_failure(self, schedule_id: int) -> None:
        """
        Record failed execution (increment consecutive failures).

        Args:
            schedule_id: Schedule ID
        """
        await self.conn.execute(
            """
            UPDATE jorb_schedule
            SET run_count = run_count + 1,
                failure_count = failure_count + 1,
                consecutive_failures = consecutive_failures + 1,
                last_run = NOW(),
                last_failure = NOW(),
                updated = NOW()
            WHERE id = $1
        """,
            schedule_id,
        )

    async def record_execution_skip(self, schedule_id: int, reason: str) -> None:
        """
        Record skipped execution.

        Args:
            schedule_id: Schedule ID
            reason: Why execution was skipped
        """
        await self.conn.execute(
            """
            UPDATE jorb_schedule
            SET skip_count = skip_count + 1,
                updated = NOW()
            WHERE id = $1
        """,
            schedule_id,
        )

    async def disable_unevaluatable(self, schedule_id: int, error: str) -> None:
        """
        Disable a schedule whose cron expression or timezone cannot be
        evaluated, and count it as a failure.

        Such a schedule can never get a new next_run, so leaving it enabled
        makes it due forever: the scheduler would re-select it on every poll,
        fail on every poll, and record nothing (the failing transaction rolls
        its own bookkeeping back). Disabling it is the only outcome that both
        stops the spin and is visible to an operator.

        Args:
            schedule_id: Schedule ID
            error: Why the schedule cannot be evaluated
        """
        await self.conn.execute(
            """
            UPDATE jorb_schedule
            SET enabled = false,
                run_count = run_count + 1,
                failure_count = failure_count + 1,
                consecutive_failures = consecutive_failures + 1,
                last_run = NOW(),
                last_failure = NOW(),
                updated = NOW()
            WHERE id = $1
        """,
            schedule_id,
        )

        logger.error(f"Schedule {schedule_id} disabled: {error}")


class SchedulerWorker:
    """
    Main scheduler worker that executes recurring schedules.

    Polls database every minute for due schedules and creates jobs with
    comprehensive safety checks and logging.
    """

    def __init__(
        self,
        conn: asyncpg.Connection,
        poll_interval: int = 60,
        prio_ceiling: int | None = None,
    ):
        """
        Initialize scheduler worker.

        Args:
            conn: Active database connection
            poll_interval: Seconds between polls (default: 60)
        """
        self.conn = conn
        self.poll_interval = poll_interval
        #: The worker fleet's priority ceiling (`pj --max-prio`). Checked at
        #: FIRE time, not only at schedule creation: a schedule mints a job
        #: on every firing, and a job above every worker's ceiling is never
        #: claimed — one bad number becomes an unbounded stream of jobs
        #: nobody runs. A firing refused by the ceiling DISABLES the
        #: schedule with the reason, exactly like an unevaluatable cron.
        self.prio_ceiling = (
            DEFAULT_PRIO_CEILING if prio_ceiling is None else prio_ceiling
        )
        self.safety = ScheduleSafetyManager(conn)
        self.manager = ScheduleManager(conn)
        #: Connection parameters for reconnecting after a lost connection.
        #: None means the caller manages the connection's lifetime and the
        #: scheduler cannot rebuild it (tests driving a single conn).
        self.db_params: dict[str, Any] | None = None
        self.stop_requested = False
        # Set by stop(); cuts the poll sleep short so SIGTERM does not have
        # to wait out a whole poll interval before the loop exits.
        self._stop_event = asyncio.Event()

        # Metrics
        self.executions_total = 0
        self.successes_total = 0
        self.failures_total = 0
        self.skips_total = 0

    async def find_due_schedules(self) -> list[dict[str, Any]]:
        """
        Find all schedules that are due to run.

        Returns:
            List of schedule records
        """
        # Plain read: actual cross-instance mutual exclusion happens in run(),
        # which re-locks each row with FOR UPDATE SKIP LOCKED inside a real
        # transaction. (A lock taken here would be released as soon as this
        # statement's implicit transaction ends, protecting nothing.)
        records = await self.conn.fetch("""
            SELECT * FROM jorb_schedule
            WHERE enabled = true
              AND next_run <= NOW()
            ORDER BY next_run
        """)

        schedules = [dict(r) for r in records]

        logger.debug(f"Found {len(schedules)} schedules due to run")

        return schedules

    async def create_scheduled_job(
        self,
        schedule: dict[str, Any],
        scheduled_time: datetime,
        jitter_seconds: float = 0,
    ) -> int | None:
        """
        Create job for schedule with deadline key.

        Args:
            schedule: Schedule record
            scheduled_time: When job should have run
            jitter_seconds: Load-spreading offset added to the job's run_after
                (jitter is applied to when the job may START, not by sleeping
                the scheduler, so one jittery schedule never stalls the others)

        Returns:
            Job ID if created, None if duplicate

        Raises:
            Exception: If job creation fails
        """
        # Generate deadline key to prevent duplicates
        deadline_key = f"schedule:{schedule['id']}:{scheduled_time.isoformat()}"

        # Which schedule made this job is a COLUMN (jorb.schedule_id), not an
        # admin_data key: it is the one thing about a scheduled job anybody
        # queries by, and no index could serve it while it lived in jsonb.
        # What stays here is the descriptive half, which nothing filters on.
        admin_data = {
            "schedule_name": schedule["name"],
            "scheduled_time": scheduled_time.isoformat(),
        }

        # run_after is timestamptz: aware datetimes pass through unchanged
        run_after_time = scheduled_time

        if jitter_seconds > 0:
            run_after_time = run_after_time + timedelta(seconds=jitter_seconds)

        try:
            # Nested transaction = savepoint when run() already holds a
            # transaction, so a UniqueViolationError here cannot poison the
            # outer transaction's later statements (log_execution etc).
            #
            # THE shared enqueue path — the same row construction and the
            # same INSERT as every client enqueue. The scheduler used to
            # hand-roll its own INSERT, which silently skipped priority
            # validation and every option the row builder handles; a second
            # enqueue path is a second place for the two to disagree.
            args = JobClient.build_enqueue_row(
                schedule["job_class"],
                queue=schedule["queue"],
                priority=schedule["prio"],
                run_after=run_after_time,
                capability=schedule["capability"],
                deadline_key=deadline_key,
                admin_data=admin_data,
                schedule_id=schedule["id"],
                job_kwargs=dict(schedule["kwargs"] or {}),
                prio_ceiling=self.prio_ceiling,
            )
            async with self.conn.transaction():
                job_id: int = await self.conn.fetchval(ENQUEUE_SQL, *args)

            logger.info(
                f"Created job {job_id} for schedule '{schedule['name']}'",
                extra={
                    "schedule_id": schedule["id"],
                    "schedule_name": schedule["name"],
                    "job_id": job_id,
                    "job_class": schedule["job_class"],
                    "queue": schedule["queue"],
                },
            )

            return job_id

        except asyncpg.UniqueViolationError:
            # Job already created (duplicate prevented by deadline key)
            logger.warning(
                f"Schedule '{schedule['name']}': job already exists (duplicate prevented)",
                extra={
                    "schedule_id": schedule["id"],
                    "schedule_name": schedule["name"],
                    "deadline_key": deadline_key,
                },
            )
            return None

    async def log_execution(
        self,
        schedule: dict[str, Any],
        scheduled_time: datetime,
        result: ScheduleExecutionResult,
    ) -> None:
        """
        Log execution to jorb_schedule_log.

        Args:
            schedule: Schedule record
            scheduled_time: When job should have run
            result: Execution result
        """
        await self.conn.execute(
            """
            INSERT INTO jorb_schedule_log (
                schedule_id, schedule_name,
                scheduled_time, actual_time,
                result, skip_reason,
                job_id, error_message,
                duration_ms, queue_depth_at_run,
                concurrent_jobs_at_run, jitter_applied_seconds
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
            schedule["id"],
            schedule["name"],
            scheduled_time,
            utcnow(),
            result.result,
            result.skip_reason,
            result.job_id,
            result.error_message,
            result.duration_ms,
            result.queue_depth,
            result.concurrent_jobs,
            result.jitter_applied,
        )

    async def execute_schedule(
        self,
        schedule: dict[str, Any],
        scheduled_time: datetime | None = None,
        *,
        allow_jitter: bool = True,
    ) -> ScheduleExecutionResult:
        """
        Execute single schedule with all safety checks.

        Args:
            schedule: Schedule record
            scheduled_time: The tick being fired. Defaults to the schedule's
                own ``next_run``, the tick it is currently due for; a
                :meth:`backfill_missed_ticks` catch-up passes one of the
                instants the schedule missed instead. Everything else about
                the firing -- every safety check, the shared enqueue path, the
                per-tick deadline key -- is identical, deliberately: a
                recovery burst that could route around the safety limits would
                be a way to defeat them by taking the scheduler down.
            allow_jitter: False suppresses jitter. Jitter spreads a thundering
                herd of ON-TIME fires across a window; a backfilled tick is
                already late, so delaying it further buys nothing.

        Returns:
            Execution result
        """
        start_time = time.time()
        if scheduled_time is None:
            scheduled_time = schedule["next_run"]

        logger.debug(
            f"Executing schedule '{schedule['name']}'",
            extra={
                "schedule_id": schedule["id"],
                "schedule_name": schedule["name"],
                "scheduled_time": scheduled_time.isoformat(),
            },
        )

        # Safety check 1: Circuit breaker
        circuit_ok, circuit_reason = await self.safety.check_circuit_breaker(schedule)
        if not circuit_ok:
            result = ScheduleExecutionResult(
                result="skipped", skip_reason="circuit_breaker"
            )
            await self.manager.record_execution_skip(schedule["id"], "circuit_breaker")
            return result

        # Safety check 2: Concurrency limit
        concurrency_ok, concurrent_count = await self.safety.check_concurrency(
            schedule["id"], schedule["max_concurrent_jobs"]
        )
        if not concurrency_ok:
            logger.warning(
                f"Schedule '{schedule['name']}' skipped: max_concurrent limit "
                f"({concurrent_count}/{schedule['max_concurrent_jobs']})",
                extra={
                    "schedule_id": schedule["id"],
                    "schedule_name": schedule["name"],
                    "concurrent_jobs": concurrent_count,
                    "max_concurrent": schedule["max_concurrent_jobs"],
                },
            )
            result = ScheduleExecutionResult(
                result="skipped",
                skip_reason="max_concurrent",
                concurrent_jobs=concurrent_count,
            )
            await self.manager.record_execution_skip(schedule["id"], "max_concurrent")
            return result

        # Safety check 3: Backpressure
        backpressure_ok, queue_depth = await self.safety.check_backpressure(
            schedule["queue"], schedule["backpressure_threshold"]
        )
        if not backpressure_ok:
            logger.warning(
                f"Schedule '{schedule['name']}' skipped: backpressure "
                f"(queue depth: {queue_depth}, threshold: {schedule['backpressure_threshold']})",
                extra={
                    "schedule_id": schedule["id"],
                    "schedule_name": schedule["name"],
                    "queue_depth": queue_depth,
                    "backpressure_threshold": schedule["backpressure_threshold"],
                },
            )
            result = ScheduleExecutionResult(
                result="skipped", skip_reason="backpressure", queue_depth=queue_depth
            )
            await self.manager.record_execution_skip(schedule["id"], "backpressure")
            return result

        # Safety feature 4: Apply jitter as a run_after offset on the created
        # job (never by sleeping here — with N schedules a serial sleep would
        # stall every schedule behind this one).
        jitter = (
            self.safety.calculate_jitter(schedule["jitter_seconds"])
            if allow_jitter
            else 0
        )
        if jitter > 0:
            logger.debug(
                f"Schedule '{schedule['name']}' applying jitter: {jitter}s",
                extra={
                    "schedule_id": schedule["id"],
                    "schedule_name": schedule["name"],
                    "jitter_seconds": jitter,
                },
            )

        # Create job
        try:
            job_id = await self.create_scheduled_job(
                schedule, scheduled_time, jitter_seconds=jitter
            )

            if job_id is not None:
                # Success!
                await self.manager.record_execution_success(schedule["id"])

                duration_ms = int((time.time() - start_time) * 1000)

                logger.info(
                    f"Schedule '{schedule['name']}' executed successfully (job_id: {job_id})",
                    extra={
                        "schedule_id": schedule["id"],
                        "schedule_name": schedule["name"],
                        "job_id": job_id,
                        "duration_ms": duration_ms,
                        "jitter_applied": jitter,
                    },
                )

                return ScheduleExecutionResult(
                    result="success",
                    job_id=job_id,
                    jitter_applied=jitter,
                    queue_depth=queue_depth,
                    concurrent_jobs=concurrent_count,
                    duration_ms=duration_ms,
                )
            else:
                # Duplicate (deadline key collision)
                # Transaction may already be aborted from UniqueViolationError
                # in test environments with transaction isolation
                with contextlib.suppress(asyncpg.InFailedSQLTransactionError):
                    await self.manager.record_execution_skip(
                        schedule["id"], "duplicate"
                    )

                return ScheduleExecutionResult(
                    result="skipped", skip_reason="duplicate"
                )

        except ValueError as e:
            # The shared row builder refused the firing — priority above the
            # fleet's ceiling, or an invalid option. Retrying next poll
            # cannot change the answer, so DISABLE the schedule with the
            # reason (the same treatment an unevaluatable cron gets) instead
            # of minting one failure per poll forever.
            await self.manager.record_execution_failure(schedule["id"])
            await self.manager.disable_unevaluatable(schedule["id"], str(e))
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(
                f"Schedule '{schedule['name']}' DISABLED: {e}",
                extra={"schedule_id": schedule["id"], "duration_ms": duration_ms},
            )
            return ScheduleExecutionResult(
                result="failure", error_message=str(e), duration_ms=duration_ms
            )

        except Exception as e:
            # Failure!
            await self.manager.record_execution_failure(schedule["id"])

            duration_ms = int((time.time() - start_time) * 1000)

            logger.error(
                f"Schedule '{schedule['name']}' failed: {e}",
                extra={
                    "schedule_id": schedule["id"],
                    "schedule_name": schedule["name"],
                    "error": str(e),
                    "duration_ms": duration_ms,
                },
            )

            return ScheduleExecutionResult(
                result="failure", error_message=str(e), duration_ms=duration_ms
            )

    def record_metrics(self, result: ScheduleExecutionResult) -> None:
        """Count one firing in this worker's own totals.

        A backfilled tick counts exactly like an on-time one: a recovery that
        fired three ticks and dropped seven has to be visible in the numbers
        the process reports, not only in a table somebody has to go and read.
        """
        self.executions_total += 1
        if result.result == "success":
            self.successes_total += 1
        elif result.result == "failure":
            self.failures_total += 1
        elif result.result == "skipped":
            self.skips_total += 1

    async def backfill_missed_ticks(
        self, schedule: dict[str, Any], *, until: datetime | None = None
    ) -> None:
        """Fire the recent ticks this schedule missed, up to its own bound.

        ``backfill_limit`` is BOTH the opt-in and the bound, and 0 is the
        default: a scheduler that was down skips the ticks it missed and
        ``next_run`` advances from now, which is what this platform has always
        done and what most schedules want (nobody needs last Tuesday's
        report). ``N > 0`` fires the N most RECENT missed ticks -- the freshest
        ones, because the value of a late fire decays -- and records the older
        excess rather than firing it.

        Never more than N + 1 enqueues per recovery, however long the outage:
        the current due tick, which the caller fires, plus at most N. That
        hard ceiling is the whole design. Backfilling without one turns a
        day-long outage of a minutely schedule into 1,440 jobs landing at once
        on a queue that is already behind, which is the failure mode this
        feature exists to make impossible to ask for.

        The dropped ticks are recorded as ONE summary row, not one row apiece:
        a per-second schedule down for a day misses 86,400 ticks, and writing
        that many rows into ``jorb_schedule_log`` to describe an outage is its
        own denial of service. The row carries the count and the window it
        covers, because silence is how unbounded backfill hides -- an operator
        must be able to see exactly what was dropped and decide whether the
        bound was set too low.

        Every backfilled tick goes through :meth:`execute_schedule`, so
        ``max_concurrent`` and ``backpressure_threshold`` refuse it exactly as
        they refuse an on-time fire (each refusal recorded as the skip it is),
        the circuit breaker counts an enqueue failure, and the per-tick
        deadline key makes two schedulers recovering at once converge on one
        job per tick. Jitter is not applied -- see :meth:`execute_schedule`.

        THE KEPT TICKS FIRE NEWEST FIRST, and that is not cosmetic. Those
        safety refusals BIND: ``max_concurrent_jobs`` defaults to 1, and the
        current due tick the caller just fired already occupies that one slot,
        so at the default a backfill burst is refused from its second fire
        onward. Firing oldest-first spent the budget on the STALEST ticks and
        skipped the freshest -- the exact inversion of the principle this
        feature is built on ("the value of a late fire decays", which is why
        only the newest ``keep`` are collected at all). Newest-first means a
        refusal sacrifices the oldest tick, so whatever DOES get fired is the
        most useful thing available. Sizing rule for an operator who wants the
        whole burst to land: ``max_concurrent_jobs >= backfill_limit + 1``
        (the +1 is the due tick), which `schedule add` warns about at creation
        time and docs/RECURRING_SCHEDULER.md spells out.
        """
        limit = schedule["backfill_limit"]
        if limit <= 0:
            return

        # The window is (next_run, until]. next_run itself is EXCLUDED because
        # it is the tick the schedule is currently due for, which the caller has
        # already fired: a backfill therefore only ever ADDS fires, and raising
        # backfill_limit never moves or removes the fire a schedule was due
        # for. `until` is the DATABASE's clock, handed down by ``run()`` from
        # the same read that anchors the next_run advance -- so the backfilled
        # window ends exactly where the next tick begins, and a scheduler host
        # whose clock is skewed cannot open a gap between them (backfilling
        # ticks the advance then skips) or an overlap (backfilling a tick the
        # advance is about to fire). It falls back to this process's clock only
        # for a caller that has no transaction to read the database's in.
        missed = missed_cron_runs(
            schedule["cron_expr"],
            schedule["timezone"],
            after=schedule["next_run"],
            until=until if until is not None else utcnow(),
            keep=limit,
        )

        # NEWEST FIRST -- see the docstring. missed.kept is ascending because
        # the window is walked forward; the fires are the reverse of it, so a
        # safety refusal costs the OLDEST tick rather than the freshest.
        for tick in reversed(missed.kept):
            result = await self.execute_schedule(schedule, tick, allow_jitter=False)
            # Logged against the tick's SCHEDULED time, never against now:
            # `actual_time` far ahead of `scheduled_time` is the honest and
            # only marker that a fire was a backfill, and rewriting
            # scheduled_time to hide the gap would erase the outage.
            await self.log_execution(schedule, tick, result)
            self.record_metrics(result)

        # THE SUMMARY IS WRITTEN LAST, after the fires it describes. It used to
        # come first, which made a crash mid-burst re-record it on the next
        # recovery pass and inflate skip_count by the same dropped count again,
        # every time -- a counter that grew with the number of crashes rather
        # than with the number of dropped ticks, which is the one thing it is
        # read for.
        #
        # What makes the move sufficient is the CALL PATH: `run()` drives this
        # whole backfill inside one `self.conn.transaction()`, together with
        # the enqueues, their log rows and the `next_run` advance. So a crash
        # mid-burst does not leave a partial burst behind to be reconciled --
        # every fire, every log row and this summary roll back as one, and the
        # next pass finds `next_run` unmoved and redoes the burst from the same
        # starting point. Written first, this row was the one statement whose
        # effect the recovery pass would then have written TWICE within one
        # consistent history; written last, there is nothing about the
        # abandoned attempt left anywhere for the retry to duplicate.
        if missed.dropped_window is not None:
            oldest, newest = missed.dropped_window
            detail = (
                f"{missed.dropped} older missed tick(s) not backfilled "
                f"(backfill_limit={limit}): "
                f"{oldest.isoformat()} .. {newest.isoformat()}"
            )
            logger.warning(
                f"Schedule '{schedule['name']}': {detail}",
                extra={
                    "schedule_id": schedule["id"],
                    "schedule_name": schedule["name"],
                    "backfill_limit": limit,
                    "dropped_ticks": missed.dropped,
                },
            )
            # One skip, one log row: every skip in the system is 1:1 with a row
            # in jorb_schedule_log, and reconciling skip_count against that
            # table is how an operator checks the log is not lying. How many
            # ticks this one row stood for is in the row's own detail. Its
            # scheduled_time is the OLDEST dropped tick, so the row sorts into
            # the history where the dropped window begins.
            summary = ScheduleExecutionResult(
                result="skipped",
                skip_reason="backfill_limit",
                error_message=detail,
            )
            await self.manager.record_execution_skip(schedule["id"], "backfill_limit")
            await self.log_execution(schedule, oldest, summary)
            self.record_metrics(summary)

    async def run(self) -> None:
        """Main scheduler loop"""
        logger.info(f"Scheduler worker started (poll interval: {self.poll_interval}s)")

        while not self.stop_requested:
            try:
                # Find due schedules
                schedules = await self.find_due_schedules()

                # Execute each schedule
                for schedule in schedules:
                    try:
                        async with self.conn.transaction():
                            # Re-lock the row inside a real transaction so a
                            # concurrent scheduler instance skips it — and
                            # re-check due-ness, since another instance may
                            # have already advanced next_run.
                            locked = await self.conn.fetchrow(
                                """
                                SELECT * FROM jorb_schedule
                                WHERE id = $1
                                  AND enabled = true
                                  AND next_run <= NOW()
                                FOR UPDATE SKIP LOCKED
                                """,
                                schedule["id"],
                            )
                            if not locked:
                                continue
                            schedule = dict(locked)

                            # THE DATABASE'S CLOCK, read inside the same
                            # transaction that holds the row lock, and the one
                            # clock every decision in this block is made
                            # against. `next_run <= NOW()` above judged
                            # due-ness on it; the advance below has to be
                            # measured on it too, or a skewed scheduler host
                            # advances next_run to an instant the database
                            # already considers due and re-fires the same tick
                            # (see calculate_next_run).
                            db_now = await self.conn.fetchval("SELECT now()")

                            # Resolve the following fire time BEFORE firing:
                            # if the expression is unevaluatable, firing
                            # would only be rolled back by the failure to
                            # advance next_run, and the schedule would spin
                            # on every poll forever.
                            #
                            # GREATEST(db_now, next_run): the locked re-check
                            # already guarantees next_run <= db_now, so this is
                            # a no-op that states the invariant -- the advance
                            # never goes BACKWARDS past the tick just fired,
                            # whatever a future caller does to the guard.
                            try:
                                next_run = self.manager.calculate_next_run(
                                    schedule["cron_expr"],
                                    schedule["timezone"],
                                    after=max(db_now, schedule["next_run"]),
                                )
                            except ValueError as e:
                                unevaluatable = ScheduleExecutionResult(
                                    result="failure", error_message=str(e)
                                )
                                await self.manager.disable_unevaluatable(
                                    schedule["id"], str(e)
                                )
                                await self.log_execution(
                                    schedule, schedule["next_run"], unevaluatable
                                )
                                self.record_metrics(unevaluatable)
                                continue

                            # Execute schedule (the tick it is due for)
                            result = await self.execute_schedule(schedule)

                            # Log execution
                            await self.log_execution(
                                schedule, schedule["next_run"], result
                            )

                            # Update metrics
                            self.record_metrics(result)

                            # Catch up on the ticks missed while nothing was
                            # firing this schedule. A no-op at backfill_limit 0,
                            # which is the default and stays the default. Given
                            # the same database clock the advance uses, so the
                            # backfill window and the next tick cannot overlap
                            # or leave a gap between them.
                            await self.backfill_missed_ticks(schedule, until=db_now)

                            # Update next_run
                            await self.manager.set_next_run(schedule["id"], next_run)

                    except Exception as e:
                        logger.error(
                            f"Failed to execute schedule '{schedule['name']}': {e}",
                            exc_info=True,
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
                await self._sleep(self.poll_interval)

            except (
                asyncpg.PostgresConnectionError,
                asyncpg.InterfaceError,
                OSError,
            ) as e:
                # A lost connection is permanent unless somebody rebuilds it,
                # and a scheduler that stops firing after a database restart
                # has failed at its one job — reconnect with backoff until
                # the database is back or we are told to stop.
                logger.error(f"Scheduler lost its database connection: {e}")
                await self._reconnect()
            except Exception as e:
                logger.error(f"Scheduler main loop error: {e}", exc_info=True)
                await self._sleep(10)  # Shorter sleep on error

        logger.info("Scheduler worker stopped")

    async def _reconnect(self) -> None:
        """Rebuild the connection (and every component holding it), retrying
        with backoff until it works or stop() is called. With no db_params
        (a caller-managed connection) there is nothing to rebuild: log that
        loudly and back off so the loop does not spin."""
        from . import db, migrations

        if self.db_params is None:
            logger.error(
                "Scheduler has no db_params to reconnect with; schedules "
                "will not fire until the caller-provided connection returns"
            )
            await self._sleep(10)
            return

        while not self.stop_requested:
            try:
                with contextlib.suppress(Exception):
                    await self.conn.close()
                self.conn = await db.connect(**self.db_params)
                self.safety.conn = self.conn
                self.manager.conn = self.conn
                logger.info("Scheduler database connection re-established")
                return
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
                # The hint is the whole point of logging this twice a minute:
                # a network blip and a database with no schema produce the
                # same line otherwise, and only the second one has an answer.
                hint = migrations.schema_error_hint(e)
                logger.warning(
                    f"Scheduler reconnect failed ({e}); retrying in 5s"
                    + (f". {hint}" if hint else "")
                )
                await self._sleep(5)

    async def _sleep(self, seconds: float) -> None:
        """Wait ``seconds``, or until stop() is called — whichever is first."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)

    def stop(self) -> None:
        """Request graceful shutdown"""
        logger.info("Scheduler stop requested")
        self.stop_requested = True
        self._stop_event.set()


async def run_scheduler(
    db_params: dict[str, Any],
    poll_interval: int = 60,
    prio_ceiling: int | None = None,
) -> None:
    """Connect and run a SchedulerWorker until SIGTERM/SIGINT."""
    import signal

    from . import db

    conn = await db.connect(**db_params)
    worker = SchedulerWorker(
        conn, poll_interval=poll_interval, prio_ceiling=prio_ceiling
    )
    worker.db_params = db_params  # enables reconnect after a lost connection

    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop)

    try:
        await worker.run()
    finally:
        # worker.conn, NOT the local `conn`: _reconnect rebinds worker.conn
        # after a lost connection, so closing the original here would leak
        # the live session and close an already-dead handle.
        with contextlib.suppress(Exception):
            await worker.conn.close()


def main() -> None:
    """CLI entry point: the ``pj-scheduler`` console script."""
    import click

    @click.command()
    @click.option(
        "--config",
        "-c",
        default="./pyjobby.toml",
        show_default=True,
        help="Config file path (must define db_params)",
    )
    @click.option(
        "--poll-interval",
        default=60,
        show_default=True,
        help="Seconds between schedule polls",
    )
    @click.option(
        "--max-prio",
        default=None,
        type=int,
        help="The worker fleet's priority ceiling (`pj --max-prio`). A "
        "schedule whose priority is above it is DISABLED at fire time "
        "rather than minting jobs nobody will ever claim. Defaults to the "
        "config file's prio_ceiling, else 1000.",
    )
    def cli(config: str, poll_interval: int, max_prio: int | None) -> None:
        """Run the recurring (cron) schedule executor.

        Polls jorb_schedule for due schedules and enqueues their jobs.
        Safe to run multiple instances: schedules are row-locked while
        being fired and duplicate jobs are prevented by deadline keys.
        """
        import sys

        from . import migrations
        from .configloader import load_config_from_file

        cfg = load_config_from_file(config, keys=["db_params", "prio_ceiling"])
        db_params = cfg.get("db_params")
        if not db_params:
            raise click.ClickException(f"No db_params found in config: {config}")

        # precedence: explicit flag > config file's prio_ceiling > default
        ceiling = max_prio if max_prio is not None else cfg.get("prio_ceiling")

        # Ask the question every poll is about to ask, once, before the loop --
        # the same startup precondition pj and pj-monitor answer with exit code
        # 2, and the same failure without it: against a schema-less database
        # this daemon polls forever, logging one failure per cycle, while every
        # health check reports a healthy scheduler and no schedule ever fires.
        problem = asyncio.run(migrations.preflight_problem(db_params))
        if problem is not None:
            click.echo(problem, err=True)
            sys.exit(2)

        asyncio.run(
            run_scheduler(db_params, poll_interval=poll_interval, prio_ceiling=ceiling)
        )

    cli()


if __name__ == "__main__":
    main()
