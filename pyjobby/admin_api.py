#!/usr/bin/env python3
"""
Pyjobby Admin API

Clean, well-encapsulated administrative API for managing jobs, queues, and workers.
Designed to be used by both CLI tools and web interfaces.

All methods are async and return structured data (dicts/lists).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]

from . import db
from .client import (
    DEFAULT_PRIO_CEILING,
    tags_filter_sql,
    validate_priority,
    validate_tags,
)
from .cron import next_cron_run
from .lifecycle import TERMINAL_STATES_SQL
from .monitor import DEFAULT_LIVENESS_GRACE_SECONDS


class Unset:
    """Sentinel type for 'argument not provided' where None is meaningful."""


UNSET = Unset()

#: What `clear_queue` deletes when the caller does not say: work that has not
#: started. The same default `JobClient.purge_queue` has, and for the same
#: reason -- a CLAIMED or RUNNING row belongs to a worker that is executing
#: it right now, and deleting the row does not stop the worker, it strands
#: the run (the completion write then matches zero rows). Reaching those
#: states has to be a decision somebody typed.
CLEAR_QUEUE_STATES: Final[tuple[str, ...]] = ("queued", "waiting")

# Jobs in flight this long without a state change are reported separately from
# "busy": at 278 jobs/sec a worker that has held one job for five minutes is
# not slow, it is wedged.
DEFAULT_STUCK_AFTER_SECONDS = 300.0

# Tables whose footprint an operator has to watch: jorb is the hot table,
# jorb_history is the biggest (one row per transition), jorb_step is written
# once per DXE checkpoint.
FOOTPRINT_TABLES = ("jorb", "jorb_history", "jorb_step")

#: How long the oldest CLAIMABLE job has been waiting, per queue. The state
#: counts beside it come from :data:`pyjobby.db.QUEUE_STATS_SQL` — this is
#: the one thing the admin view needs that per-state counts cannot express,
#: kept as its own small query so the shared statistics query keeps exactly
#: one meaning for everybody.
#:
#: Measured from run_after over RUNNABLE rows only, matching that query's
#: 'queued' arm: a job deferred to next week is not "old" (it is 'scheduled',
#: not backlog), and measuring from created made deliberate deferrals read as
#: backlog age. $1 optionally narrows to one queue (NULL = all).
ADMIN_OLDEST_QUEUED_AGE_SQL = """
    SELECT queue,
           EXTRACT(EPOCH FROM (now() - MIN(run_after)))::float8
               AS oldest_queued_age_seconds
      FROM jorb
     WHERE state = 'queued' AND run_after <= now()
       AND ($1::text IS NULL OR queue = $1)
     GROUP BY queue
"""


def _rate(count: int | None, window_seconds: float) -> float:
    """Per-second rate, comparable across window sizes.

    A zero-length window (`since` == now) has no rate to report -- returning
    0.0 beats dividing by ~0 and publishing a meaningless spike.
    """
    if window_seconds <= 0:
        return 0.0
    return (count or 0) / window_seconds


@dataclass
class JobInfo:
    """Structured job information"""

    id: int
    state: str
    queue: str
    job_class: str
    kwargs: dict
    prio: int
    run_after: datetime
    created: datetime
    updated: datetime
    run_count: int
    error_count: int
    capability: str | None = None
    uid: int | None = None
    # the caller's own labels (customer/tenant/region/batch); '{}' when unset
    tags: dict | None = None
    run_group: int | None = None
    waitfor_job: int | None = None
    waitfor_group: int | None = None
    deadline_key: str | None = None
    worker_pid: int | None = None
    worker_host: str | None = None
    result: dict | None = None
    error_message: str | None = None
    error_backtrace: str | None = None
    admin_data: dict | None = None
    started: datetime | None = None
    finished: datetime | None = None
    timeout_at: datetime | None = None
    dag_id: int | None = None
    run_epoch: int = 0
    cancel_requested: bool = False
    claimed_by: int | None = None
    claimed_at: datetime | None = None
    # someone has waited on this job; the demand signal that switches its
    # jorb_done/jorb_event notifications on (see sql/schema/90_notify.sql)
    awaited: bool = False
    # the recurring schedule that fired this job, NULL for a job anyone
    # enqueued directly (see sql/schema/10_jobs.sql)
    schedule_id: int | None = None

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> JobInfo:
        """Build a JobInfo from a `SELECT * FROM jorb` row.

        Unknown columns are ignored rather than raising. This mirrors a
        `SELECT *`, so `cls(**dict(record))` meant that adding ANY column to
        jorb broke every endpoint that returns a job -- list, get, and the
        DLQ -- at runtime, in production, far from the change that caused it.
        That has happened twice.

        Silently dropping a column would hide the drift instead, so
        tests/test_admin_api.py asserts JobInfo covers every column jorb has:
        the schema and this dataclass are kept in step by a test that fails
        loudly at the right moment, not by an exception in a live request.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in record.items() if k in known})

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary with datetime serialization"""
        data = asdict(self)
        # Convert datetimes to ISO strings for JSON serialization
        for key in [
            "claimed_at",
            "run_after",
            "created",
            "updated",
            "started",
            "finished",
            "timeout_at",
        ]:
            if data.get(key):
                data[key] = data[key].isoformat()
        return data


@dataclass
class QueueStats:
    """Queue statistics (depths plus the jorb_queue control-plane row)"""

    queue: str
    queued: int = 0
    #: queued jobs parked in the future (retry backoff, enqueue-at). Split
    #: out of `queued` because deferred work is not backlog — see
    #: :data:`pyjobby.db.QUEUE_STATS_SQL`.
    scheduled: int = 0
    claimed: int = 0
    running: int = 0
    waiting: int = 0
    finished: int = 0
    crashed: int = 0
    cancelled: int = 0
    total: int = 0
    oldest_queued_age_seconds: float | None = None
    # control plane (jorb_queue); absent row = unpaused / unlimited
    paused: bool = False
    max_concurrency: int | None = None
    rate_limit: int | None = None
    rate_period_seconds: float = 60.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AdminAPI:
    """
    Administrative API for managing pyjobby jobs, queues, and workers.

    This API provides a clean abstraction layer for management operations,
    usable by both CLI tools and web interfaces.

    Usage:
        conn = await asyncpg.connect(**db_params)
        api = AdminAPI(conn)
        jobs = await api.list_jobs(queue='default', state='crashed')
    """

    def __init__(
        self, conn: asyncpg.Connection, prio_ceiling: int = DEFAULT_PRIO_CEILING
    ):
        """
        Initialize AdminAPI with database connection.

        Args:
            conn: Active asyncpg connection
            prio_ceiling: the priority ceiling this deployment's workers run
                with (`pj --max-prio`, default 1000). Schedules are refused
                above it, because a schedule mints a job on every firing and
                a job above every worker's ceiling is never claimed -- one
                bad number becomes an unbounded stream of jobs nobody runs.
                Declared here for the same reason `JobClient` takes it: the
                ceiling belongs to the worker fleet and nothing about it is
                visible from a connection.
        """
        self.conn = conn
        self.prio_ceiling = prio_ceiling

    # =========================================================================
    # Job Management
    # =========================================================================

    async def list_jobs(
        self,
        queue: str | None = None,
        state: str | None = None,
        job_class: str | None = None,
        uid: int | None = None,
        tags: dict[str, Any] | None = None,
        limit: int = 50,
        offset: int = 0,
        order_by: str = "created",
        order_dir: str = "DESC",
    ) -> list[dict[str, Any]]:
        """
        List jobs with optional filtering.

        Args:
            queue: Filter by queue name
            state: Filter by job state (queued, claimed, running, etc.)
            job_class: Filter by job class name (supports LIKE patterns)
            uid: Filter by user ID
            tags: Match jobs whose tags CONTAIN every pair given. Extra tags
                on the job do not disqualify it, so `{'region': 'eu'}` finds
                a job tagged region+customer+batch. Answered by the partial
                GIN index on jorb.tags.
            limit: Maximum number of results (default: 50)
            offset: Offset for pagination (default: 0)
            order_by: Column to order by (default: created)
            order_dir: Order direction (ASC or DESC, default: DESC)

        Returns:
            List of job dictionaries
        """
        # Build WHERE clauses dynamically
        where_clauses = []
        params: list[Any] = []
        param_idx = 1

        if queue:
            where_clauses.append(f"queue = ${param_idx}")
            params.append(queue)
            param_idx += 1

        if state:
            where_clauses.append(f"state = ${param_idx}")
            params.append(state)
            param_idx += 1

        if job_class:
            where_clauses.append(f"job_class LIKE ${param_idx}")
            params.append(f"%{job_class}%")
            param_idx += 1

        if uid is not None:
            where_clauses.append(f"uid = ${param_idx}")
            params.append(uid)
            param_idx += 1

        if tags:
            # Shared with the client library so the two filters cannot drift
            # into one being indexed and the other not; tags_filter_sql
            # explains why it is two clauses and not one.
            where_clauses.append(tags_filter_sql(param_idx))
            params.append(validate_tags(tags))
            param_idx += 1

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        # Validate order_by to prevent SQL injection
        allowed_columns = [
            "id",
            "created",
            "updated",
            "run_after",
            "prio",
            "state",
            "queue",
        ]
        if order_by not in allowed_columns:
            order_by = "created"

        order_dir = "DESC" if order_dir.upper() == "DESC" else "ASC"

        query = f"""
            SELECT * FROM jorb
            {where_sql}
            ORDER BY {order_by} {order_dir}
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
        """
        params.extend([limit, offset])

        records = await self.conn.fetch(query, *params)
        return [JobInfo.from_record(r).to_dict() for r in records]

    async def get_job(self, job_id: int) -> dict[str, Any] | None:
        """
        Get detailed information about a specific job.

        Args:
            job_id: Job ID

        Returns:
            Job dictionary or None if not found
        """
        record = await self.conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)
        if not record:
            return None
        return JobInfo.from_record(record).to_dict()

    async def retry_job(self, job_id: int) -> dict[str, Any]:
        """
        Retry a crashed or cancelled job by requeuing it.

        The job keeps its id: retries reuse the same row (the per-attempt
        audit trail lives in jorb_history).

        Args:
            job_id: ID of job to retry

        Returns:
            {"job_id", "status"} where status is 'requeued' or
            'not_retriable'. A job that does not exist is 'not_retriable'
            too: absence and a state that forbids the retry are the same
            answer to the caller ("this job was not requeued"), and neither
            is an exception.
        """
        requeued = await db.retry_job(self.conn, job_id)

        return {
            "job_id": job_id,
            "status": "requeued" if requeued else "not_retriable",
        }

    async def retry_jobs(self, job_ids: list[int]) -> list[dict[str, Any]]:
        """
        Retry multiple jobs in bulk.

        Args:
            job_ids: List of job IDs to retry

        Returns:
            One retry_job() result per id, in order — refusals carry
            status 'not_retriable' rather than interrupting the batch
        """
        return [await self.retry_job(job_id) for job_id in job_ids]

    async def cancel_job(self, job_id: int) -> dict[str, Any]:
        """
        Cancel a job wherever it is in its lifecycle.

        Queued/waiting jobs are cancelled immediately; claimed/running jobs
        get a cancellation request delivered to their worker.

        Args:
            job_id: ID of job to cancel

        Returns:
            {"job_id", "status"} where status is 'cancelled',
            'cancel_requested', or 'not_cancellable'. A job that does not
            exist is 'not_cancellable' too, not an exception: it is not
            running either way.
        """
        outcome = await db.cancel_job(self.conn, job_id)

        return {"job_id": job_id, "status": outcome or "not_cancellable"}

    async def cancel_jobs(self, job_ids: list[int]) -> list[dict[str, Any]]:
        """
        Cancel multiple jobs in bulk.

        Args:
            job_ids: List of job IDs to cancel

        Returns:
            One cancel_job() result per id, in order — refusals carry
            status 'not_cancellable' rather than interrupting the batch
        """
        return [await self.cancel_job(job_id) for job_id in job_ids]

    async def delete_job(self, job_id: int) -> bool:
        """
        Delete a job from the database.

        WARNING: This permanently deletes the job. Use with caution.

        Args:
            job_id: ID of job to delete

        Returns:
            True if deleted, False if not found
        """
        result: str = await self.conn.execute("DELETE FROM jorb WHERE id = $1", job_id)
        # asyncpg returns "DELETE N" where N is number of rows deleted
        return result == "DELETE 1"

    async def update_job_priority(self, job_id: int, new_priority: int) -> bool:
        """Re-prioritise a job that has not been claimed yet.

        Only queued/waiting jobs: a claimed or running job's priority no
        longer decides anything, and a terminal job's is history. Refuses a
        priority above this deployment's worker ceiling for the same reason
        enqueue does -- a job no worker will claim is a silent black hole
        (see validate_priority).

        Returns True if the row was updated, False if the job does not exist
        or has already left the queue.
        """
        validate_priority(new_priority, self.prio_ceiling)
        result: str = await self.conn.execute(
            """
            UPDATE jorb SET prio = $2
             WHERE id = $1 AND state IN ('queued', 'waiting')
            """,
            job_id,
            new_priority,
        )
        return result != "UPDATE 0"

    async def delete_jobs(
        self,
        queue: str | None = None,
        state: str | Sequence[str] | None = None,
        not_updated_for_days: int | None = None,
    ) -> int:
        """
        Bulk delete jobs matching criteria.

        WARNING: This permanently deletes jobs. Use with caution.

        Args:
            queue: Only delete jobs in this queue
            state: Only delete jobs in this state, or in any of these states
            not_updated_for_days: Only delete jobs whose last state change was
                more than N days ago (quiesced work -- the filter is on
                `updated`, not on when the job was created)

        Returns:
            Number of jobs deleted
        """
        where_clauses = []
        params: list[Any] = []
        param_idx = 1

        if queue:
            where_clauses.append(f"queue = ${param_idx}")
            params.append(queue)
            param_idx += 1

        if state is not None:
            # One state or several, in one parameter shape each: `= ANY(...)`
            # for a list keeps the caller from having to build a predicate,
            # and the enum cast is what makes an unknown label an error here
            # instead of a silent no-match.
            if isinstance(state, str):
                where_clauses.append(f"state = ${param_idx}")
                params.append(state)
            else:
                where_clauses.append(f"state = ANY(${param_idx}::jorbstate[])")
                params.append(list(state))
            param_idx += 1

        if not_updated_for_days:
            where_clauses.append(f"updated < (now() - ${param_idx}::interval)")
            params.append(timedelta(days=not_updated_for_days))
            param_idx += 1

        if not where_clauses:
            raise ValueError(
                "Must specify at least one filter (queue, state, or "
                "not_updated_for_days)"
            )

        where_sql = "WHERE " + " AND ".join(where_clauses)

        result = await self.conn.execute(f"DELETE FROM jorb {where_sql}", *params)

        # Parse "DELETE N" to get count
        deleted_count = int(result.split()[-1])
        return deleted_count

    # =========================================================================
    # Queue Management
    # =========================================================================

    async def list_queues(self) -> list[dict[str, Any]]:
        """
        List all queues: every queue with jobs plus every jorb_queue
        control row, with paused/limit settings alongside.

        Returns:
            List of dicts with name, paused, max_concurrency, rate_limit,
            rate_period_seconds (control fields are defaults when no
            jorb_queue row exists).
        """
        records = await self.conn.fetch("""
            SELECT COALESCE(j.queue, q.name) AS name,
                   COALESCE(q.paused, FALSE) AS paused,
                   q.max_concurrency,
                   q.rate_limit,
                   COALESCE(q.rate_period_seconds, 60) AS rate_period_seconds
            FROM (SELECT DISTINCT queue FROM jorb) j
            FULL OUTER JOIN jorb_queue q ON q.name = j.queue
            ORDER BY 1
        """)
        return [dict(r) for r in records]

    async def queue_stats(self, queue: str | None = None) -> list[dict[str, Any]]:
        """
        Get statistics for queues, joined with the jorb_queue control plane
        so operators see paused/limits alongside depths.

        The counts come from :data:`pyjobby.db.QUEUE_STATS_SQL`, the one home
        for what they mean: live states counted exactly, finished/crashed/
        cancelled within the LAST HOUR (recent activity, not an all-time
        audit — the all-time count grows with the install's whole history and
        belongs to SQL), and 'queued' = claimable NOW with deferred jobs
        reported separately as 'scheduled'. oldest_queued_age_seconds covers
        those same RUNNABLE rows only: a job deferred to next week is
        deliberately not old.

        Args:
            queue: Specific queue name, or None for all queues

        Returns:
            List of queue statistics dictionaries
        """
        records = await self.conn.fetch(db.QUEUE_STATS_SQL, timedelta(hours=1), queue)
        ages = await self.conn.fetch(ADMIN_OLDEST_QUEUED_AGE_SQL, queue)

        # Aggregate by queue
        queue_stats_map: dict[str, QueueStats] = {}

        for r in records:
            q = r["queue"]
            if q not in queue_stats_map:
                queue_stats_map[q] = QueueStats(queue=q)

            stats = queue_stats_map[q]
            state = r["state"]
            count = r["n"]

            if state == "queued":
                stats.queued = count
            elif state == "scheduled":
                stats.scheduled = count
            elif state == "claimed":
                stats.claimed = count
            elif state == "running":
                stats.running = count
            elif state == "waiting":
                stats.waiting = count
            elif state == "finished":
                stats.finished = count
            elif state == "crashed":
                stats.crashed = count
            elif state == "cancelled":
                stats.cancelled = count

            stats.total += count

        for a in ages:
            stats = queue_stats_map.setdefault(a["queue"], QueueStats(queue=a["queue"]))
            stats.oldest_queued_age_seconds = a["oldest_queued_age_seconds"]

        # Merge in the control plane (a control row without jobs still shows)
        control_where = "WHERE name = $1" if queue else ""
        control_args = [queue] if queue else []
        controls = await self.conn.fetch(
            f"SELECT * FROM jorb_queue {control_where} ORDER BY name", *control_args
        )
        for c in controls:
            stats = queue_stats_map.setdefault(c["name"], QueueStats(queue=c["name"]))
            stats.paused = c["paused"]
            stats.max_concurrency = c["max_concurrency"]
            stats.rate_limit = c["rate_limit"]
            stats.rate_period_seconds = c["rate_period_seconds"]

        return [queue_stats_map[name].to_dict() for name in sorted(queue_stats_map)]

    async def clear_queue(
        self,
        queue: str,
        states: Sequence[str] = CLEAR_QUEUE_STATES,
        not_updated_for_days: int | None = None,
    ) -> int:
        """
        Clear (delete) jobs from a queue.

        Args:
            queue: Queue name to clear
            states: Which states to clear (default: queued and waiting -- see
                CLEAR_QUEUE_STATES; deleting live claimed/running work is an
                explicit choice, never the default)
            not_updated_for_days: Only clear jobs quiesced this long (no state
                change for N days)

        Returns:
            Number of jobs deleted
        """
        return await self.delete_jobs(
            queue=queue, state=states, not_updated_for_days=not_updated_for_days
        )

    # =========================================================================
    # Queue Control Plane (jorb_queue: pause / concurrency / rate limits)
    # =========================================================================

    @staticmethod
    def _queue_control_dict(record: asyncpg.Record) -> dict[str, Any]:
        data = dict(record)
        for key in ("created", "updated"):
            if data.get(key):
                data[key] = data[key].isoformat()
        return data

    async def list_queue_controls(self) -> list[dict[str, Any]]:
        """
        List all jorb_queue control rows.

        Queues without a row are unpaused/unlimited (defaults).

        Returns:
            List of control dictionaries
        """
        records = await self.conn.fetch("SELECT * FROM jorb_queue ORDER BY name")
        return [self._queue_control_dict(r) for r in records]

    async def get_queue_control(self, name: str) -> dict[str, Any] | None:
        """
        Get the control row for one queue.

        Args:
            name: Queue name

        Returns:
            Control dictionary, or None when no row exists (defaults apply)
        """
        record = await self.conn.fetchrow(
            "SELECT * FROM jorb_queue WHERE name = $1", name
        )
        return self._queue_control_dict(record) if record else None

    async def set_queue_control(
        self,
        name: str,
        *,
        paused: bool | None = None,
        max_concurrency: int | None | Unset = UNSET,
        rate_limit: int | None | Unset = UNSET,
        rate_period_seconds: float | None = None,
    ) -> dict[str, Any]:
        """
        Upsert the jorb_queue control row, updating only the provided
        fields. The worker's claim statement enforces these live.

        Args:
            name: Queue name
            paused: Pause (True) / resume (False); None leaves it alone
            max_concurrency: Claimed+running cap; None means unlimited
                (pass explicitly to clear); omit to leave alone
            rate_limit: Max starts per rate period; None means unlimited
                (pass explicitly to clear); omit to leave alone
            rate_period_seconds: Rate window in seconds; None leaves it alone

        Returns:
            The resulting control dictionary
        """
        mc: int | None = None if isinstance(max_concurrency, Unset) else max_concurrency
        rl: int | None = None if isinstance(rate_limit, Unset) else rate_limit
        set_mc = not isinstance(max_concurrency, Unset)
        set_rl = not isinstance(rate_limit, Unset)

        record = await self.conn.fetchrow(
            """
            INSERT INTO jorb_queue
                (name, paused, max_concurrency, rate_limit, rate_period_seconds)
            VALUES ($1, COALESCE($2, FALSE), $3, $4, COALESCE($5, 60))
            ON CONFLICT (name) DO UPDATE SET
                paused = COALESCE($2, jorb_queue.paused),
                max_concurrency = CASE WHEN $6 THEN $3
                                       ELSE jorb_queue.max_concurrency END,
                rate_limit = CASE WHEN $7 THEN $4
                                  ELSE jorb_queue.rate_limit END,
                rate_period_seconds =
                    COALESCE($5, jorb_queue.rate_period_seconds),
                updated = now()
            RETURNING *
            """,
            name,
            paused,
            mc,
            rl,
            rate_period_seconds,
            set_mc,
            set_rl,
        )
        return self._queue_control_dict(record)

    async def pause_queue(self, name: str) -> dict[str, Any]:
        """
        Pause a queue: workers stop claiming from it immediately.

        Args:
            name: Queue name

        Returns:
            The resulting control dictionary
        """
        return await self.set_queue_control(name, paused=True)

    async def resume_queue(self, name: str) -> dict[str, Any]:
        """
        Resume a paused queue.

        Args:
            name: Queue name

        Returns:
            The resulting control dictionary
        """
        return await self.set_queue_control(name, paused=False)

    # =========================================================================
    # Worker Management
    # =========================================================================

    async def list_workers(
        self,
        stale_after_seconds: float = DEFAULT_LIVENESS_GRACE_SECONDS,
        include_dead_for_seconds: float = 3600.0,
    ) -> list[dict[str, Any]]:
        """
        List workers from the jorb_worker registry: live workers plus
        recently-shut-down ones, with their currently claimed job (if any).

        A worker is live when shutdown_at IS NULL and its heartbeat
        (last_seen) is recent; heartbeats arrive every ~10s.

        `not_claiming` is the live worker that is nonetheless doing nothing:
        abandoned job threads fill its pool, so it refuses to claim while
        heartbeating perfectly. It is derived here rather than left to every
        caller because `live` alone reads as healthy, which is the whole
        problem -- see `job_thread_stats`.

        Args:
            stale_after_seconds: Heartbeat age past which a worker counts
                as stale rather than live (default: 60)
            include_dead_for_seconds: Show workers that shut down within
                this window (default: 3600)

        Returns:
            List of worker dictionaries
        """
        records = await self.conn.fetch(
            """
            SELECT w.id, w.host, w.pid, w.queue, w.capabilities, w.version,
                   w.started, w.last_seen, w.shutdown_at,
                   w.job_threads, w.job_threads_abandoned,
                   EXTRACT(EPOCH FROM (now() - w.last_seen))::float
                       AS last_seen_age_seconds,
                   (w.shutdown_at IS NULL
                    AND w.last_seen > now() - make_interval(secs => $1))
                       AS live,
                   (w.shutdown_at IS NULL
                    AND w.last_seen > now() - make_interval(secs => $1)
                    AND w.job_threads > 0
                    AND w.job_threads_abandoned >= w.job_threads)
                       AS not_claiming,
                   j.id AS current_job_id,
                   j.job_class AS current_job_class,
                   j.state AS current_job_state
            FROM jorb_worker w
            LEFT JOIN LATERAL (
                SELECT id, job_class, state FROM jorb
                WHERE claimed_by = w.id AND state IN ('claimed', 'running')
                ORDER BY id
                LIMIT 1
            ) j ON TRUE
            WHERE w.shutdown_at IS NULL
               OR w.shutdown_at > now() - make_interval(secs => $2)
            ORDER BY w.id
            """,
            stale_after_seconds,
            include_dead_for_seconds,
        )

        workers = []
        for r in records:
            data = dict(r)
            data["capabilities"] = list(data["capabilities"] or [])
            for key in ("started", "last_seen", "shutdown_at"):
                if data.get(key):
                    data[key] = data[key].isoformat()
            workers.append(data)
        return workers

    async def worker_stats(
        self, stale_after_seconds: float = DEFAULT_LIVENESS_GRACE_SECONDS
    ) -> dict[str, Any]:
        """
        Aggregate worker registry statistics.

        Args:
            stale_after_seconds: Heartbeat age past which a worker counts
                as stale rather than live (default: 60)

        Returns:
            Dictionary with live/stale/shutdown counts and per-queue live
            worker counts
        """
        summary = await self.conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_registered,
                COUNT(*) FILTER (
                    WHERE shutdown_at IS NULL
                      AND last_seen > now() - make_interval(secs => $1)
                ) AS live,
                COUNT(*) FILTER (
                    WHERE shutdown_at IS NULL
                      AND last_seen <= now() - make_interval(secs => $1)
                ) AS stale,
                COUNT(*) FILTER (WHERE shutdown_at IS NOT NULL) AS shutdown
            FROM jorb_worker
            """,
            stale_after_seconds,
        )

        per_queue = await self.conn.fetch(
            """
            SELECT queue, COUNT(*) AS live
            FROM jorb_worker
            WHERE shutdown_at IS NULL
              AND last_seen > now() - make_interval(secs => $1)
            GROUP BY queue
            ORDER BY queue
            """,
            stale_after_seconds,
        )

        return {
            "live_workers": summary["live"] or 0,
            "stale_workers": summary["stale"] or 0,
            "shutdown_workers": summary["shutdown"] or 0,
            "total_registered": summary["total_registered"] or 0,
            "per_queue": {r["queue"]: r["live"] for r in per_queue},
        }

    # =========================================================================
    # Metrics & Monitoring
    # =========================================================================

    async def backlog_stats(self, queue: str | None = None) -> dict[str, Any]:
        """
        Claimable backlog: how much work is waiting, and how long the head
        of each queue has been waiting for it.

        Depth alone does not say whether the fleet is keeping up -- a deep
        queue that drains in seconds is healthy, a shallow one whose oldest
        job has sat for 40 minutes is not. Both numbers together do.

        Only CLAIMABLE work counts: `run_after` gates claimability, so a job
        deliberately scheduled for next week is not backlog and must not
        raise the alarm.

        Age is measured from `run_after`, not `created`, for the same reason
        the count is: it answers "how long has the head of this queue been
        READY and unclaimed". A job enqueued for next week that came due two
        seconds ago has been waiting two seconds, not seven days -- and a
        retry that just finished its backoff has been waiting since the
        backoff ended, not since its first attempt. For a plain enqueue the
        two are identical, because `run_after` defaults to the insert time.

        Cost: `queue` and `run_after` both live in the partial index
        `jorb_claim_idx (queue, prio, run_after) WHERE state = 'queued'`, so
        this is an INDEX-ONLY scan (measured at 20k rows: 12 buffers, zero
        heap fetches, against 572 buffers for the sequential scan that
        `MIN(created)` forces -- `created` is not in any claim index, so
        asking for it means visiting the heap row of every queued job).
        Work is proportional to the backlog, never to table size: the
        hundreds of millions of terminal rows are not in this index at all.

        Args:
            queue: Restrict to one queue (optional)

        Returns:
            Dictionary with per-queue depth/age plus the fleet-wide totals
        """
        params: list[Any] = []
        queue_sql = ""
        if queue:
            params.append(queue)
            queue_sql = "AND queue = $1"

        rows = await self.conn.fetch(
            f"""
            SELECT queue,
                   COUNT(*)                                        AS depth,
                   EXTRACT(EPOCH FROM (now() - MIN(run_after)))::float8
                                                                   AS oldest_age
            FROM jorb
            WHERE state = 'queued' AND run_after <= now() {queue_sql}
            GROUP BY queue
            ORDER BY queue
            """,
            *params,
        )

        per_queue = {
            r["queue"]: {
                "depth": r["depth"],
                "oldest_age_seconds": max(float(r["oldest_age"] or 0.0), 0.0),
            }
            for r in rows
        }
        return {
            "per_queue": per_queue,
            "depth": sum(q["depth"] for q in per_queue.values()),
            "oldest_age_seconds": max(
                (q["oldest_age_seconds"] for q in per_queue.values()), default=0.0
            ),
        }

    async def inflight_stats(
        self,
        queue: str | None = None,
        stuck_after_seconds: float = DEFAULT_STUCK_AFTER_SECONDS,
    ) -> dict[str, Any]:
        """
        Work a worker is holding right now, and how much of it looks wedged.

        In-flight count alone cannot tell "busy" from "wedged": both look
        like a big number. Splitting out the jobs whose last state change is
        older than `stuck_after_seconds` does.

        Cost: every column referenced (state, updated) lives in the partial
        index `jorb_inflight_idx (state, updated) WHERE state IN ('claimed',
        'running')`, so this is an index-only scan bounded by the size of
        the worker fleet -- not by the table. Passing `queue` adds a heap
        recheck (queue is not in that index) but keeps the same bound.

        Args:
            queue: Restrict to one queue (optional)
            stuck_after_seconds: In-flight age past which a job counts as stuck

        Returns:
            Dictionary with in-flight, stuck, and oldest in-flight age
        """
        params: list[Any] = [stuck_after_seconds]
        queue_sql = ""
        if queue:
            params.append(queue)
            queue_sql = "AND queue = $2"

        row = await self.conn.fetchrow(
            f"""
            SELECT COUNT(*)                                          AS inflight,
                   COUNT(*) FILTER (
                       WHERE updated <= now() - make_interval(secs => $1)
                   )                                                 AS stuck,
                   EXTRACT(EPOCH FROM (now() - MIN(updated)))::float8
                                                                     AS oldest_age
            FROM jorb
            WHERE state IN ('claimed', 'running') {queue_sql}
            """,
            *params,
        )

        return {
            "inflight": row["inflight"] or 0,
            "stuck": row["stuck"] or 0,
            "stuck_after_seconds": float(stuck_after_seconds),
            "oldest_age_seconds": max(float(row["oldest_age"] or 0.0), 0.0),
        }

    async def job_thread_stats(
        self, stale_after_seconds: float = DEFAULT_LIVENESS_GRACE_SECONDS
    ) -> dict[str, Any]:
        """
        Workers that are alive and claiming nothing, and how close the rest
        of the fleet is to joining them.

        This is the saturation signal none of the others can carry. A
        synchronous job that exceeds its deadline leaves a thread that
        nothing can interrupt; enough of them fill the worker's pool, and the
        worker then refuses to claim rather than admit a job it could not
        start. It keeps heartbeating throughout -- so `worker_stats` counts
        it live, `inflight_stats` sees no work from it (indistinguishable
        from an idle worker), and `backlog_stats` shows only the queue
        backing up, with nothing to say which worker stopped pulling from it.

        `max_abandoned` is the approach, and it is the number worth alerting
        on before `not_claiming` moves: a worker holding 7 abandoned threads
        of 8 is one timed-out job away from doing nothing at all.

        Cost: `jorb_worker` has one row per worker process, so this is
        bounded by the size of the fleet at any table size, and the live
        predicate is the one `jorb_worker_live_idx` is built for. No job rows
        are read.

        Args:
            stale_after_seconds: Heartbeat age past which a worker is not
                counted here at all (default: 60) -- a worker that stopped
                beating is the monitor's problem, not this one's

        Returns:
            Dictionary with the live workers reporting a pool, how many of
            them are refusing to claim, and the abandoned-thread totals
        """
        row = await self.conn.fetchrow(
            """
            SELECT COUNT(*)                                    AS workers,
                   COUNT(*) FILTER (
                       WHERE job_threads_abandoned >= job_threads
                   )                                           AS not_claiming,
                   COALESCE(SUM(job_threads_abandoned), 0)     AS abandoned,
                   COALESCE(MAX(job_threads_abandoned), 0)     AS max_abandoned
            FROM jorb_worker
            WHERE shutdown_at IS NULL
              AND last_seen > now() - make_interval(secs => $1)
              AND job_threads > 0
            """,
            stale_after_seconds,
        )

        return {
            "workers": row["workers"] or 0,
            "not_claiming": row["not_claiming"] or 0,
            "abandoned": int(row["abandoned"] or 0),
            "max_abandoned": int(row["max_abandoned"] or 0),
        }

    async def storage_stats(self) -> dict[str, Any]:
        """
        On-disk footprint and autovacuum health for the job tables.

        At a million jobs an hour the platform churns roughly four million
        dead tuples an hour, and whether autovacuum is keeping up with that
        is a survival question, not a curiosity: once the dead-tuple ratio
        runs away the hot table bloats, the indexes stop fitting in cache,
        and the claim path -- which is the whole product -- slows down.

        Cost: catalog and statistics views only (`pg_stat_user_tables`,
        `pg_total_relation_size`). No job rows are read at any table size.

        Returns:
            Dictionary with per-table byte counts and jorb's dead-tuple ratio
        """
        rows = await self.conn.fetch(
            """
            SELECT relname::text                     AS table_name,
                   pg_total_relation_size(relid)     AS total_bytes,
                   pg_table_size(relid)              AS table_bytes,
                   pg_indexes_size(relid)            AS index_bytes,
                   n_live_tup                        AS live_tuples,
                   n_dead_tup                        AS dead_tuples,
                   last_autovacuum,
                   last_autoanalyze
            FROM pg_stat_user_tables
            WHERE relname = ANY($1::text[])
            """,
            list(FOOTPRINT_TABLES),
        )

        tables: dict[str, Any] = {}
        for r in rows:
            live = r["live_tuples"] or 0
            dead = r["dead_tuples"] or 0
            total_tup = live + dead
            tables[r["table_name"]] = {
                "total_bytes": r["total_bytes"] or 0,
                "table_bytes": r["table_bytes"] or 0,
                "index_bytes": r["index_bytes"] or 0,
                "live_tuples": live,
                "dead_tuples": dead,
                "dead_tuple_ratio": (dead / total_tup) if total_tup else 0.0,
                "last_autovacuum": (
                    r["last_autovacuum"].isoformat() if r["last_autovacuum"] else None
                ),
                "last_autoanalyze": (
                    r["last_autoanalyze"].isoformat() if r["last_autoanalyze"] else None
                ),
            }

        jorb = tables.get("jorb", {})
        return {
            "tables": tables,
            "total_bytes": sum(t["total_bytes"] for t in tables.values()),
            "dead_tuple_ratio": float(jorb.get("dead_tuple_ratio", 0.0)),
        }

    async def notify_queue_usage(self) -> float:
        """
        Fraction (0.0-1.0) of PostgreSQL's shared async-NOTIFY queue in use.

        This is the platform's sharpest cliff. pyjobby fires ~5 notifications
        per job lifecycle (enqueue, claim, start, state-change feed,
        completion) -- about 1,400/second at a million jobs an hour -- and
        the shared queue drains only as fast as the SLOWEST connected
        listener. One wedged dashboard or websocket client that stops
        reading fills it, and at 1.0 EVERY transaction that issues a NOTIFY
        fails: no job can be enqueued or completed anywhere in the system.
        An observability client takes down job processing.

        It is a cliff, not a gradient -- fine until it is a total outage --
        so it is worth a metric of its own even though it never appears in
        latency or throughput until the moment everything stops.

        Cost: one C function call. No table or catalog access.

        Returns:
            Usage fraction, 0.0 (empty) to 1.0 (full; NOTIFY now failing)
        """
        usage = await self.conn.fetchval("SELECT pg_notification_queue_usage()")
        return float(usage or 0.0)

    async def get_metrics(
        self,
        since: datetime | None = None,
        queue: str | None = None,
        stuck_after_seconds: float = DEFAULT_STUCK_AFTER_SECONDS,
    ) -> dict[str, Any]:
        """
        Get system metrics.

        Two kinds of number live here and they must not be confused:

        * RATES (``*_per_second``) are measured over the ``since`` window and
          are therefore comparable across window sizes. The pair that
          matters most is ``throughput_per_second`` versus
          ``arrival_rate_per_second``: sustained arrivals above completions
          is the definition of falling behind, and no single number can say
          that.
        * LEVELS (backlog, in-flight, footprint, NOTIFY usage) are instants,
          not window aggregates -- "how deep is it right now".

        The window is applied through the two indexes the schema actually
        keeps, because /metrics is scraped on a timer against a table with
        hundreds of millions of rows and a scan here turns the monitoring
        into the outage:

        * completions are found by ``COALESCE(finished, updated)`` over the
          terminal states, which is exactly `jorb_retention_idx`;
        * arrivals are found by ``created``, which is `jorb_created_idx`.

        Neither is `updated`: the schema deliberately does NOT index it
        (every state transition rewrites it, so an index there taxes the
        write path forever to speed up one read per scrape).

        Args:
            since: Start of the reporting window (default: last 24h)
            queue: Filter by queue (optional)
            stuck_after_seconds: In-flight age past which a job counts as stuck

        Returns:
            Dictionary with metrics
        """
        if since is None:
            since = db.utcnow() - timedelta(hours=24)

        params: list[Any] = [since]
        queue_sql = ""
        if queue:
            params.append(queue)
            queue_sql = "AND queue = $2"

        # Completions: every job that REACHED a terminal state in the window.
        #
        # Terminal, not finished-only: a crash loop keeps the fleet perfectly
        # busy while `finished` collapses, and calling that "throughput
        # collapse" sends the operator to look for missing workers. What the
        # arrival rate must be compared against is the rate at which work
        # LEAVES the system, however it leaves.
        #
        # The two latencies are the ones an operator acts on separately: how
        # long jobs WAIT to be picked up (a capacity signal) versus how long
        # they RUN once picked up (a code signal). Measuring either as
        # `updated - created` would blend them together -- along with the
        # backoff between every retry -- and hide which is the problem.
        #
        # `run_count - 1` is the attempts a job burned beyond its first, so
        # summing it over the window's completions is retry pressure in the
        # same unit as everything else here: work per second the fleet did
        # twice.
        completion_stats = await self.conn.fetchrow(
            f"""
            SELECT
                COUNT(*) as terminal_count,
                COUNT(*) FILTER (WHERE state = 'finished') as finished_count,
                COUNT(*) FILTER (WHERE state = 'crashed') as crashed_count,
                COUNT(*) FILTER (WHERE state = 'cancelled') as cancelled_count,
                COALESCE(SUM(GREATEST(run_count - 1, 0)), 0) as retry_count,
                AVG(EXTRACT(EPOCH FROM (finished - started)))
                    FILTER (WHERE state = 'finished'
                            AND started IS NOT NULL) as avg_duration_seconds,
                AVG(EXTRACT(EPOCH FROM (claimed_at - run_after)))
                    FILTER (WHERE claimed_at IS NOT NULL) as avg_wait_seconds,
                MAX(EXTRACT(EPOCH FROM (claimed_at - run_after)))
                    FILTER (WHERE claimed_at IS NOT NULL) as max_wait_seconds
            FROM jorb
            WHERE state IN ({TERMINAL_STATES_SQL})
              AND COALESCE(finished, updated) >= $1 {queue_sql}
        """,
            *params,
        )

        # Arrivals: the jobs that ENTERED the system in the window, grouped
        # by where they are now. That cohort view is what makes the state
        # counts actionable at a scrape interval ("of the million that
        # arrived this hour, 40k are still queued") rather than a census of
        # all history.
        state_counts = await self.conn.fetch(
            f"""
            SELECT state, COUNT(*) as count
            FROM jorb
            WHERE created >= $1 {queue_sql}
            GROUP BY state
        """,
            *params,
        )

        # Top error job classes, over the same completion window as the
        # crash count they explain.
        top_errors = await self.conn.fetch(
            f"""
            SELECT
                job_class,
                COUNT(*) as error_count,
                MAX(error_message) as latest_error
            FROM jorb
            WHERE state = 'crashed'
              AND COALESCE(finished, updated) >= $1 {queue_sql}
            GROUP BY job_class
            ORDER BY error_count DESC
            LIMIT 10
        """,
            *params,
        )

        backlog = await self.backlog_stats(queue=queue)
        inflight = await self.inflight_stats(
            queue=queue, stuck_after_seconds=stuck_after_seconds
        )
        storage = await self.storage_stats()
        job_threads = await self.job_thread_stats()
        notify_usage = await self.notify_queue_usage()

        period_end = db.utcnow()
        window_seconds = max((period_end - since).total_seconds(), 0.0)
        arrival_count = sum(r["count"] for r in state_counts)

        return {
            "period_start": since.isoformat(),
            "period_end": period_end.isoformat(),
            "window_seconds": window_seconds,
            "queue": queue,
            "state_counts": {r["state"]: r["count"] for r in state_counts},
            "finished_count": completion_stats["finished_count"] or 0,
            "crashed_count": completion_stats["crashed_count"] or 0,
            "cancelled_count": completion_stats["cancelled_count"] or 0,
            # --- rates over `since`..now, per second ---
            # Throughput against arrivals is THE question at a million jobs
            # an hour: sustained arrivals above completions is the definition
            # of falling behind, and neither number alone can say it.
            "terminal_count": completion_stats["terminal_count"] or 0,
            "throughput_per_second": _rate(
                completion_stats["terminal_count"], window_seconds
            ),
            "arrival_count": arrival_count,
            "arrival_rate_per_second": _rate(arrival_count, window_seconds),
            # Attempts beyond the first, burned by jobs that completed in the
            # window. Counted at completion because that is when the total is
            # known; a job still cycling through retries lands in this number
            # when it finally settles.
            "retry_count": int(completion_stats["retry_count"] or 0),
            "retry_rate_per_second": _rate(
                int(completion_stats["retry_count"] or 0), window_seconds
            ),
            # 'crashed' is terminal and IS the dead letter queue, so crashes
            # inside the window are exactly DLQ growth -- the earliest signal
            # of a bad deploy.
            "dlq_growth_per_second": _rate(
                completion_stats["crashed_count"], window_seconds
            ),
            "avg_duration_seconds": float(
                completion_stats["avg_duration_seconds"] or 0
            ),
            "avg_wait_seconds": float(completion_stats["avg_wait_seconds"] or 0),
            "max_wait_seconds": float(completion_stats["max_wait_seconds"] or 0),
            # --- levels, measured now (not window aggregates) ---
            "backlog": backlog,
            "inflight": inflight,
            "storage": storage,
            # Capacity that is registered and heartbeating but claiming
            # nothing. Sits beside the other levels because it explains them:
            # a backlog that will not drain with a live fleet is this.
            "job_threads": job_threads,
            "notify_queue_usage": notify_usage,
            "top_errors": [
                {
                    "job_class": r["job_class"],
                    "error_count": r["error_count"],
                    "latest_error": r["latest_error"],
                }
                for r in top_errors
            ],
        }

    # =========================================================================
    # Dead Letter Queue
    # =========================================================================

    async def list_dlq(self, limit: int = 100) -> list[dict[str, Any]]:
        """
        List jobs in the Dead Letter Queue.

        'crashed' is the terminal dead-letter state (retries exhausted), so
        the DLQ is simply every crashed job — no error-count heuristic.

        Args:
            limit: Maximum number of results

        Returns:
            List of DLQ job dictionaries
        """
        records = await self.conn.fetch(
            """
            SELECT * FROM jorb
            WHERE state = 'crashed'
            ORDER BY updated DESC
            LIMIT $1
        """,
            limit,
        )

        return [JobInfo.from_record(r).to_dict() for r in records]

    async def retry_from_dlq(self, job_id: int) -> dict[str, Any]:
        """
        Retry a job from the Dead Letter Queue.

        Requeues the SAME row (jobs keep one id for life) with the error
        budget reset, so the operator-driven re-run gets fresh attempts.

        Args:
            job_id: ID of DLQ (crashed) job to retry

        Returns:
            {"job_id", "status"} where status is 'requeued_from_dlq', or
            'not_retriable' when the job is not in the DLQ (not crashed, or
            no such job) — the same refusal shape as retry_job()
        """
        # Same as regular retry, but errors reset to zero (fresh attempt
        # budget for the operator-driven re-run) and the guard is narrower:
        # the DLQ is the crashed jobs, so a cancelled job is not in it.
        requeued = await db.requeue_job(
            self.conn,
            job_id,
            reset_errors=True,
            allowed_states=("crashed",),  # DLQ is crashed by definition
        )

        if requeued is None:
            return {"job_id": job_id, "status": "not_retriable"}

        return {"job_id": job_id, "status": "requeued_from_dlq"}

    # =========================================================================
    # History & DXE Steps
    # =========================================================================

    async def get_job_history(self, job_id: int) -> list[dict[str, Any]]:
        """
        Get the full transition trail for a job, oldest first.

        Every state transition is trigger-recorded in jorb_history, so this
        includes per-attempt detail (worker, epoch, errors) across retries
        of the same row.

        Args:
            job_id: Job ID

        Returns:
            List of history dictionaries (at serialized as ISO string)
        """
        records = await self.conn.fetch(
            """
            SELECT id, job_id, at, event, detail
            FROM jorb_history
            WHERE job_id = $1
            ORDER BY id
            """,
            job_id,
        )

        history = []
        for r in records:
            data = dict(r)
            data["at"] = data["at"].isoformat()
            history.append(data)
        return history

    async def get_job_steps(self, job_id: int) -> list[dict[str, Any]]:
        """
        Get a job's DXE step checkpoints, ordered by step sequence.

        Args:
            job_id: Job ID

        Returns:
            List of step dictionaries (timestamps serialized as ISO
            strings; duration_seconds computed for finished steps)
        """
        records = await self.conn.fetch(
            """
            SELECT job_id, step_seq, name, output, error, run_epoch,
                   started, finished,
                   EXTRACT(EPOCH FROM (finished - started))::float
                       AS duration_seconds
            FROM jorb_step
            WHERE job_id = $1
            ORDER BY step_seq
            """,
            job_id,
        )

        steps = []
        for r in records:
            data = dict(r)
            for key in ("started", "finished"):
                if data.get(key):
                    data[key] = data[key].isoformat()
            steps.append(data)
        return steps

    async def rerun_job(self, job_id: int, fresh: bool = True) -> dict[str, Any]:
        """
        RE-RUN a terminal job — including one that already FINISHED, whose
        side effects it will repeat. That is what the verb means everywhere
        (db.rerun_job, the websocket rerun action, `pj-admin jobs rerun`);
        `retry` is the verb that refuses finished jobs.

        By default the run is fresh: the job's DXE checkpoints are deleted
        so it actually re-executes from step 1. Pass fresh=False to RESUME
        instead — checkpoints are kept and completed steps fast-forward,
        which is how an interrupted durable job is continued. Either way the
        same row is requeued with its error budget reset.

        Args:
            job_id: Job ID
            fresh: True (default) restarts from step 1; False resumes from
                the recorded checkpoints

        Returns:
            {"job_id", "status", "fresh"} where status is 'requeued' or
            'not_rerunnable' (not in a terminal state, or no such job);
            `fresh` reports which mode was asked for
        """
        requeued = await db.rerun_job(self.conn, job_id, reset_errors=True, fresh=fresh)

        return {
            "job_id": job_id,
            "status": "requeued" if requeued else "not_rerunnable",
            "fresh": fresh,
        }

    # =========================================================================
    # Schedule Management
    # =========================================================================

    async def list_schedules(
        self,
        enabled: bool | None = None,
        queue: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        List recurring schedules with optional filtering.

        Args:
            enabled: Filter by enabled status (True/False/None for all)
            queue: Filter by queue name
            limit: Maximum number of results (default: 100)
            offset: Offset for pagination (default: 0)

        Returns:
            List of schedule dictionaries
        """
        where_clauses = []
        params: list[Any] = []
        param_idx = 1

        if enabled is not None:
            where_clauses.append(f"enabled = ${param_idx}")
            params.append(enabled)
            param_idx += 1

        if queue:
            where_clauses.append(f"queue = ${param_idx}")
            params.append(queue)
            param_idx += 1

        where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

        query = f"""
            SELECT * FROM jorb_schedule
            {where_sql}
            ORDER BY name ASC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
        """
        params.extend([limit, offset])

        records = await self.conn.fetch(query, *params)
        return [dict(r) for r in records]

    async def get_schedule(
        self, schedule_id: int | None = None, name: str | None = None
    ) -> dict[str, Any] | None:
        """
        Get single schedule by ID or name.

        Args:
            schedule_id: Schedule ID (optional)
            name: Schedule name (optional)

        Returns:
            Schedule dictionary or None if not found

        Raises:
            ValueError: If neither lookup key was given. "Not given" means
            None -- an id of 0 or an empty name is a lookup that finds
            nothing, not a missing argument.
        """
        if schedule_id is not None:
            record = await self.conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
        elif name is not None:
            record = await self.conn.fetchrow(
                "SELECT * FROM jorb_schedule WHERE name = $1", name
            )
        else:
            raise ValueError("Must provide either schedule_id or name")

        return dict(record) if record else None

    async def create_schedule(
        self,
        name: str,
        job_class: str,
        cron_expr: str,
        queue: str = "default",
        kwargs: dict | None = None,
        priority: int = 100,
        capability: str | None = None,
        timezone: str = "UTC",
        enabled: bool = True,
        max_concurrent_jobs: int = 1,
        jitter_seconds: int = 0,
        backpressure_threshold: int | None = 1000,
        circuit_breaker_threshold: int = 5,
        description: str | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """
        Create new recurring schedule.

        Args:
            name: Unique schedule name
            job_class: Python class to execute
            cron_expr: Cron expression (e.g., "0 2 * * *" for 2am daily)
            queue: Target queue (default: 'default')
            kwargs: Job arguments (default: {})
            prio: Job priority (default: 100)
            capability: Required worker capability (optional)
            timezone: Schedule timezone (default: 'UTC')
            enabled: Is schedule active? (default: True)
            max_concurrent_jobs: Max jobs running at once (default: 1)
            jitter_seconds: Random delay 0-N seconds (default: 0)
            backpressure_threshold: Skip if queue depth > N (default: 1000)
            circuit_breaker_threshold: Consecutive failures to disable (default: 5)
            description: Human-readable description (optional)
            created_by: Who created this (optional)

        Returns:
            Created schedule dictionary
        """
        # Refuse an unclaimable priority before the row exists, for the same
        # reason the cron expression is checked here: a schedule that fires
        # into nothing is worse than one that never existed. `JobClient`
        # already refuses this at enqueue, and this is the same check against
        # the same imported ceiling -- a schedule writes `jorb.prio` on every
        # firing without ever passing through the client.
        validate_priority(priority, self.prio_ceiling)

        # Reject the expression here rather than at fire time: a schedule
        # that cannot be evaluated is a schedule that silently never runs.
        next_run = next_cron_run(cron_expr, timezone)

        # Create schedule
        record = await self.conn.fetchrow(
            """
            INSERT INTO jorb_schedule (
                name, description, job_class, kwargs, queue, prio, capability,
                cron_expr, timezone, enabled,
                max_concurrent_jobs, jitter_seconds,
                backpressure_threshold, circuit_breaker_threshold,
                next_run, created_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10,
                $11, $12, $13, $14,
                $15, $16
            )
            RETURNING *
        """,
            name,
            description,
            job_class,
            kwargs or {},
            queue,
            priority,
            capability,
            cron_expr,
            timezone,
            enabled,
            max_concurrent_jobs,
            jitter_seconds,
            backpressure_threshold,
            circuit_breaker_threshold,
            next_run,
            created_by,
        )

        return dict(record)

    async def update_schedule(self, schedule_id: int, **updates: Any) -> dict[str, Any]:
        """
        Update existing schedule.

        Args:
            schedule_id: Schedule ID
            **updates: Fields to update (name, description, cron_expr, etc.)

        Returns:
            Updated schedule dictionary
        """
        # The API vocabulary is `priority` (like enqueue); the COLUMN is
        # `prio`, an SQL-side name that stays on the SQL side of the
        # boundary. Translate before the allow-list so callers never need to
        # know the column.
        if "priority" in updates:
            updates["prio"] = updates.pop("priority")

        # Allowed fields for update
        allowed_fields = {
            "name",
            "description",
            "job_class",
            "kwargs",
            "queue",
            "prio",
            "capability",
            "cron_expr",
            "timezone",
            "enabled",
            "max_concurrent_jobs",
            "jitter_seconds",
            "backpressure_threshold",
            "circuit_breaker_threshold",
            "consecutive_failures",  # Allow resetting failure counter
        }

        # Filter to only allowed fields
        updates = {k: v for k, v in updates.items() if k in allowed_fields}

        if not updates:
            raise ValueError("No valid fields to update")

        # Priority is an updatable field, so this is the second door onto
        # jorb_schedule.prio and it gets the same lock as create_schedule:
        # raising an existing schedule out of every worker's reach mints the
        # same unbounded stream of unclaimable jobs.
        if "prio" in updates:
            validate_priority(updates["prio"], self.prio_ceiling)

        # If cron_expr or timezone changed, recalculate next_run
        if "cron_expr" in updates or "timezone" in updates:
            schedule = await self.get_schedule(schedule_id=schedule_id)
            if not schedule:
                raise ValueError(f"Schedule {schedule_id} not found")

            cron_expr = updates.get("cron_expr", schedule["cron_expr"])
            timezone = updates.get("timezone", schedule["timezone"])

            updates["next_run"] = next_cron_run(cron_expr, timezone)

        # Build UPDATE query dynamically
        set_clauses = []
        params = []
        param_idx = 1

        for field, value in updates.items():
            set_clauses.append(f"{field} = ${param_idx}")
            params.append(value)
            param_idx += 1

        # Always update 'updated' timestamp (jorb_schedule.updated is
        # timestamptz, so NOW() is the correct value here)
        set_clauses.append("updated = NOW()")

        params.append(schedule_id)

        query = f"""
            UPDATE jorb_schedule
            SET {", ".join(set_clauses)}
            WHERE id = ${param_idx}
            RETURNING *
        """

        record = await self.conn.fetchrow(query, *params)

        if not record:
            raise ValueError(f"Schedule {schedule_id} not found")

        return dict(record)

    async def delete_schedule(self, schedule_id: int) -> dict[str, str]:
        """
        Delete recurring schedule.

        Args:
            schedule_id: Schedule ID

        Returns:
            Status dictionary
        """
        result = await self.conn.execute(
            "DELETE FROM jorb_schedule WHERE id = $1", schedule_id
        )

        if result == "DELETE 0":
            raise ValueError(f"Schedule {schedule_id} not found")

        return {"status": "deleted", "schedule_id": str(schedule_id)}

    async def enable_schedule(self, schedule_id: int) -> dict[str, Any]:
        """
        Enable a disabled schedule.

        Args:
            schedule_id: Schedule ID

        Returns:
            Updated schedule dictionary
        """
        return await self.update_schedule(
            schedule_id,
            enabled=True,
            consecutive_failures=0,  # Reset failure counter
        )

    async def disable_schedule(self, schedule_id: int) -> dict[str, Any]:
        """
        Disable an enabled schedule.

        Args:
            schedule_id: Schedule ID

        Returns:
            Updated schedule dictionary
        """
        return await self.update_schedule(schedule_id, enabled=False)

    async def get_schedule_history(
        self,
        schedule_id: int,
        limit: int = 100,
        offset: int = 0,
        result_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get execution history for a schedule.

        Args:
            schedule_id: Schedule ID
            limit: Maximum number of results (default: 100)
            offset: Offset for pagination (default: 0)
            result_filter: Filter by result ('success', 'failure', 'skipped')

        Returns:
            List of execution log dictionaries
        """
        where_clauses = ["schedule_id = $1"]
        params: list[Any] = [schedule_id]
        param_idx = 2

        if result_filter:
            where_clauses.append(f"result = ${param_idx}")
            params.append(result_filter)
            param_idx += 1

        where_sql = "WHERE " + " AND ".join(where_clauses)

        query = f"""
            SELECT * FROM jorb_schedule_log
            {where_sql}
            ORDER BY id DESC
            LIMIT ${param_idx} OFFSET ${param_idx + 1}
        """
        params.extend([limit, offset])

        records = await self.conn.fetch(query, *params)
        return [dict(r) for r in records]

    async def get_schedule_stats(self) -> list[dict[str, Any]]:
        """
        Get execution statistics for all schedules.

        Returns:
            List of schedule statistics
        """
        records = await self.conn.fetch("""
            SELECT
                id,
                name,
                enabled,
                cron_expr,
                queue,
                next_run,
                last_run,
                last_success,
                last_failure,
                run_count,
                success_count,
                failure_count,
                skip_count,
                consecutive_failures,
                CASE
                    WHEN success_count + failure_count = 0 THEN NULL
                    ELSE ROUND(
                        (success_count::numeric / (success_count + failure_count)) * 100,
                        2
                    )
                END as success_rate_pct
            FROM jorb_schedule
            ORDER BY name ASC
        """)

        return [dict(r) for r in records]
