#!/usr/bin/env python3
"""
Pyjobby Admin API

Clean, well-encapsulated administrative API for managing jobs, queues, and workers.
Designed to be used by both CLI tools and web interfaces.

All methods are async and return structured data (dicts/lists).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from . import db


class Unset:
    """Sentinel type for 'argument not provided' where None is meaningful."""


UNSET = Unset()


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

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> JobInfo:
        """Create JobInfo from asyncpg Record"""
        return cls(**dict(record))

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

    def __init__(self, conn: asyncpg.Connection):
        """
        Initialize AdminAPI with database connection.

        Args:
            conn: Active asyncpg connection
        """
        self.conn = conn

    # =========================================================================
    # Job Management
    # =========================================================================

    async def list_jobs(
        self,
        queue: str | None = None,
        state: str | None = None,
        job_class: str | None = None,
        uid: int | None = None,
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
            Dictionary with job_id and status

        Raises:
            ValueError: If job not found or not in retriable state
        """
        # Check if job exists and is in a retriable state
        job = await self.conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

        if not job:
            raise ValueError(f"Job {job_id} not found")

        if job["state"] not in ["crashed", "cancelled"]:
            raise ValueError(
                f"Job {job_id} is in state '{job['state']}', "
                f"can only retry crashed or cancelled jobs"
            )

        await db.retry_job(self.conn, job_id)

        return {"job_id": job_id, "status": "requeued"}

    async def retry_jobs(self, job_ids: list[int]) -> list[dict[str, Any]]:
        """
        Retry multiple jobs in bulk.

        Args:
            job_ids: List of job IDs to retry

        Returns:
            List of retry results
        """
        results = []
        for job_id in job_ids:
            try:
                result = await self.retry_job(job_id)
                results.append(result)
            except ValueError as e:
                results.append({"job_id": job_id, "status": "error", "error": str(e)})
        return results

    async def cancel_job(self, job_id: int) -> dict[str, Any]:
        """
        Cancel a job wherever it is in its lifecycle.

        Queued/waiting jobs are cancelled immediately; claimed/running jobs
        get a cancellation request delivered to their worker.

        Args:
            job_id: ID of job to cancel

        Returns:
            Dictionary with job_id and status ('cancelled' or
            'cancel_requested')

        Raises:
            ValueError: If job not found or already terminal
        """
        outcome = await db.cancel_job(self.conn, job_id)

        if outcome is None:
            job = await self.conn.fetchrow(
                "SELECT id, state FROM jorb WHERE id = $1", job_id
            )
            if not job:
                raise ValueError(f"Job {job_id} not found")
            raise ValueError(
                f"Job {job_id} is in state '{job['state']}' and cannot be cancelled"
            )

        return {"job_id": job_id, "status": outcome}

    async def cancel_jobs(self, job_ids: list[int]) -> list[dict[str, Any]]:
        """
        Cancel multiple jobs in bulk.

        Args:
            job_ids: List of job IDs to cancel

        Returns:
            List of cancellation results
        """
        results = []
        for job_id in job_ids:
            try:
                result = await self.cancel_job(job_id)
                results.append(result)
            except ValueError as e:
                results.append({"job_id": job_id, "status": "error", "error": str(e)})
        return results

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

    async def delete_jobs(
        self,
        queue: str | None = None,
        state: str | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """
        Bulk delete jobs matching criteria.

        WARNING: This permanently deletes jobs. Use with caution.

        Args:
            queue: Only delete jobs in this queue
            state: Only delete jobs in this state
            older_than_days: Only delete jobs older than N days

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

        if state:
            where_clauses.append(f"state = ${param_idx}")
            params.append(state)
            param_idx += 1

        if older_than_days:
            where_clauses.append(f"updated < (now() - ${param_idx}::interval)")
            params.append(timedelta(days=older_than_days))
            param_idx += 1

        if not where_clauses:
            raise ValueError(
                "Must specify at least one filter (queue, state, or older_than_days)"
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

        Args:
            queue: Specific queue name, or None for all queues

        Returns:
            List of queue statistics dictionaries
        """
        where_sql = ""
        params = []

        if queue:
            where_sql = "WHERE queue = $1"
            params.append(queue)

        # Get counts by state for each queue
        query = f"""
            SELECT
                queue,
                state,
                COUNT(*) as count,
                MIN(CASE WHEN state = 'queued'
                    THEN EXTRACT(EPOCH FROM (now() - created))
                    ELSE NULL END) as oldest_queued_age_seconds
            FROM jorb
            {where_sql}
            GROUP BY queue, state
            ORDER BY queue, state
        """

        records = await self.conn.fetch(query, *params)

        # Aggregate by queue
        queue_stats_map: dict[str, QueueStats] = {}

        for r in records:
            q = r["queue"]
            if q not in queue_stats_map:
                queue_stats_map[q] = QueueStats(queue=q)

            stats = queue_stats_map[q]
            state = r["state"]
            count = r["count"]

            if state == "queued":
                stats.queued = count
                stats.oldest_queued_age_seconds = r["oldest_queued_age_seconds"]
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

        # Merge in the control plane (a control row without jobs still shows)
        control_where = "WHERE name = $1" if queue else ""
        controls = await self.conn.fetch(
            f"SELECT * FROM jorb_queue {control_where} ORDER BY name", *params
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
        state: str | None = None,
        older_than_days: int | None = None,
    ) -> int:
        """
        Clear (delete) jobs from a queue.

        Args:
            queue: Queue name to clear
            state: Only clear jobs in this state (optional)
            older_than_days: Only clear jobs older than N days (optional)

        Returns:
            Number of jobs deleted
        """
        return await self.delete_jobs(
            queue=queue, state=state, older_than_days=older_than_days
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
        stale_after_seconds: float = 60.0,
        include_dead_for_seconds: float = 3600.0,
    ) -> list[dict[str, Any]]:
        """
        List workers from the jorb_worker registry: live workers plus
        recently-shut-down ones, with their currently claimed job (if any).

        A worker is live when shutdown_at IS NULL and its heartbeat
        (last_seen) is recent; heartbeats arrive every ~10s.

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
                   EXTRACT(EPOCH FROM (now() - w.last_seen))::float
                       AS last_seen_age_seconds,
                   (w.shutdown_at IS NULL
                    AND w.last_seen > now() - make_interval(secs => $1))
                       AS live,
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

    async def worker_stats(self, stale_after_seconds: float = 60.0) -> dict[str, Any]:
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

    async def get_metrics(
        self,
        since: datetime | None = None,
        queue: str | None = None,
    ) -> dict[str, Any]:
        """
        Get system metrics.

        Args:
            since: Only include jobs updated since this time (default: last 24h)
            queue: Filter by queue (optional)

        Returns:
            Dictionary with metrics
        """
        if since is None:
            since = db.utcnow() - timedelta(hours=24)

        where_clauses = ["updated >= $1"]
        params: list[Any] = [since]
        param_idx = 2

        if queue:
            where_clauses.append(f"queue = ${param_idx}")
            params.append(queue)
            param_idx += 1

        where_sql = "WHERE " + " AND ".join(where_clauses)

        # Overall counts by state
        state_counts = await self.conn.fetch(
            f"""
            SELECT state, COUNT(*) as count
            FROM jorb
            {where_sql}
            GROUP BY state
        """,
            *params,
        )

        # Completion rate, plus the two latencies an operator acts on:
        # how long jobs WAIT to be picked up (a capacity signal) versus how
        # long they RUN once picked up (a code signal). Measuring either one
        # as `updated - created` would blend them together -- along with the
        # backoff between every retry -- and hide which is the problem.
        completion_stats = await self.conn.fetchrow(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE state = 'finished') as finished_count,
                COUNT(*) FILTER (WHERE state = 'crashed') as crashed_count,
                AVG(EXTRACT(EPOCH FROM (finished - started)))
                    FILTER (WHERE state = 'finished'
                            AND started IS NOT NULL) as avg_duration_seconds,
                AVG(EXTRACT(EPOCH FROM (claimed_at - run_after)))
                    FILTER (WHERE claimed_at IS NOT NULL) as avg_wait_seconds,
                MAX(EXTRACT(EPOCH FROM (claimed_at - run_after)))
                    FILTER (WHERE claimed_at IS NOT NULL) as max_wait_seconds
            FROM jorb
            {where_sql}
        """,
            *params,
        )

        # Top error job classes
        top_errors = await self.conn.fetch(
            f"""
            SELECT
                job_class,
                COUNT(*) as error_count,
                MAX(error_message) as latest_error
            FROM jorb
            {where_sql} AND state = 'crashed'
            GROUP BY job_class
            ORDER BY error_count DESC
            LIMIT 10
        """,
            *params,
        )

        return {
            "period_start": since.isoformat(),
            "period_end": db.utcnow().isoformat(),
            "queue": queue,
            "state_counts": {r["state"]: r["count"] for r in state_counts},
            "finished_count": completion_stats["finished_count"] or 0,
            "crashed_count": completion_stats["crashed_count"] or 0,
            "avg_duration_seconds": float(
                completion_stats["avg_duration_seconds"] or 0
            ),
            "avg_wait_seconds": float(completion_stats["avg_wait_seconds"] or 0),
            "max_wait_seconds": float(completion_stats["max_wait_seconds"] or 0),
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
            Dictionary with job_id and status
        """
        # Same as regular retry, but errors reset to zero (fresh attempt
        # budget for the operator-driven re-run)
        job = await self.conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)

        if not job:
            raise ValueError(f"Job {job_id} not found")

        if job["state"] != "crashed":
            raise ValueError(f"Job {job_id} is not in DLQ (state: {job['state']})")

        await db.requeue_job(
            self.conn, job_id, reset_errors=True, allowed_states=("crashed",)  # DLQ is crashed by definition
        )

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

    async def requeue_job(self, job_id: int, fresh: bool = False) -> dict[str, Any]:
        """
        Requeue a terminal job for another run — also how an interrupted
        durable job is RESUMED.

        By default the job's DXE checkpoints are kept, so completed steps
        fast-forward on the next run (resume). With fresh=True the
        checkpoints are deleted first and the job restarts from step 1.
        Either way the same row is requeued with its error budget reset.

        Args:
            job_id: Job ID
            fresh: Delete jorb_step checkpoints before requeuing

        Returns:
            Dictionary with job_id, status, and fresh flag

        Raises:
            ValueError: If job not found or not in a requeueable state
        """
        job = await self.conn.fetchrow(
            "SELECT id, state FROM jorb WHERE id = $1", job_id
        )
        if not job:
            raise ValueError(f"Job {job_id} not found")

        async with self.conn.transaction():
            if fresh:
                await self.conn.execute(
                    "DELETE FROM jorb_step WHERE job_id = $1", job_id
                )
            requeued = await db.rerun_job(self.conn, job_id, reset_errors=True)
            if requeued is None:
                raise ValueError(
                    f"Job {job_id} is in state '{job['state']}' and cannot "
                    f"be requeued (must be crashed, cancelled, or finished)"
                )

        return {"job_id": job_id, "status": "requeued", "fresh": fresh}

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
        prio: int = 100,
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
        import pytz  # type: ignore[import-untyped]
        from croniter import croniter  # type: ignore[import-untyped]

        # Validate cron expression
        try:
            tz = pytz.timezone(timezone)
            now = datetime.now(tz)
            cron = croniter(cron_expr, now)
            next_run = cron.get_next(datetime)
        except Exception as e:
            raise ValueError(f"Invalid cron expression or timezone: {e}")

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
            prio,
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

        # If cron_expr or timezone changed, recalculate next_run
        if "cron_expr" in updates or "timezone" in updates:
            schedule = await self.get_schedule(schedule_id=schedule_id)
            if not schedule:
                raise ValueError(f"Schedule {schedule_id} not found")

            import pytz
            from croniter import croniter

            cron_expr = updates.get("cron_expr", schedule["cron_expr"])
            timezone = updates.get("timezone", schedule["timezone"])

            try:
                tz = pytz.timezone(timezone)
                now = datetime.now(tz)
                cron = croniter(cron_expr, now)
                next_run = cron.get_next(datetime)
                updates["next_run"] = next_run
            except Exception as e:
                raise ValueError(f"Invalid cron expression or timezone: {e}")

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
