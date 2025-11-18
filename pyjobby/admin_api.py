#!/usr/bin/env python3
"""
Pyjobby Admin API

Clean, well-encapsulated administrative API for managing jobs, queues, and workers.
Designed to be used by both CLI tools and web interfaces.

All methods are async and return structured data (dicts/lists).
"""

import asyncpg
from typing import Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict


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
    capability: Optional[str] = None
    uid: Optional[int] = None
    run_group: Optional[int] = None
    waitfor_job: Optional[int] = None
    waitfor_group: Optional[int] = None
    deadline_key: Optional[str] = None
    worker_pid: Optional[int] = None
    worker_host: Optional[str] = None
    result: Optional[dict] = None
    error_message: Optional[str] = None
    error_backtrace: Optional[str] = None
    admin_data: Optional[dict] = None

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "JobInfo":
        """Create JobInfo from asyncpg Record"""
        return cls(**dict(record))

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary with datetime serialization"""
        data = asdict(self)
        # Convert datetimes to ISO strings for JSON serialization
        for key in ['run_after', 'created', 'updated']:
            if data[key]:
                data[key] = data[key].isoformat()
        return data


@dataclass
class QueueStats:
    """Queue statistics"""
    queue: str
    queued: int = 0
    claimed: int = 0
    running: int = 0
    waiting: int = 0
    finished: int = 0
    crashed: int = 0
    cancelled: int = 0
    total: int = 0
    oldest_queued_age_seconds: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkerInfo:
    """Worker information"""
    worker_host: str
    worker_pid: int
    job_id: int
    job_class: str
    state: str
    started_at: datetime

    @classmethod
    def from_record(cls, record: asyncpg.Record) -> "WorkerInfo":
        return cls(**dict(record))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data['started_at']:
            data['started_at'] = data['started_at'].isoformat()
        return data


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
        queue: Optional[str] = None,
        state: Optional[str] = None,
        job_class: Optional[str] = None,
        uid: Optional[int] = None,
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
        params = []
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
            "id", "created", "updated", "run_after", "prio", "state", "queue"
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

    async def get_job(self, job_id: int) -> Optional[dict[str, Any]]:
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
        Retry a crashed or failed job by creating a new retry job.

        Args:
            job_id: ID of job to retry

        Returns:
            Dictionary with original_job_id and new_job_id

        Raises:
            ValueError: If job not found or not in retriable state
        """
        # Check if job exists and is in a retriable state
        job = await self.conn.fetchrow(
            "SELECT * FROM jorb WHERE id = $1", job_id
        )

        if not job:
            raise ValueError(f"Job {job_id} not found")

        if job['state'] not in ['crashed', 'cancelled']:
            raise ValueError(
                f"Job {job_id} is in state '{job['state']}', "
                f"can only retry crashed or cancelled jobs"
            )

        # Create retry job using same logic as automatic retry
        new_job_id = await self.conn.fetchval("""
            INSERT INTO jorb (
                job_class, kwargs, queue, prio, uid, capability,
                run_after, run_group, admin_data, state, error_count
            )
            SELECT
                job_class, kwargs, queue, prio, uid, capability,
                TIMEZONE('utc', clock_timestamp()) as run_after,
                run_group,
                jsonb_set(
                    COALESCE(admin_data::jsonb, '{}'::jsonb),
                    '{parent_job_id}',
                    to_jsonb($1::bigint)
                )::json,
                'queued' as state,
                0 as error_count
            FROM jorb
            WHERE id = $1
            RETURNING id
        """, job_id)

        return {
            "original_job_id": job_id,
            "new_job_id": new_job_id,
            "status": "retry_queued"
        }

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
                results.append({
                    "original_job_id": job_id,
                    "status": "error",
                    "error": str(e)
                })
        return results

    async def cancel_job(self, job_id: int) -> dict[str, Any]:
        """
        Cancel a queued or waiting job.

        Args:
            job_id: ID of job to cancel

        Returns:
            Dictionary with job_id and status

        Raises:
            ValueError: If job not found or not cancellable
        """
        result = await self.conn.fetchrow("""
            UPDATE jorb
            SET state = 'cancelled',
                updated = TIMEZONE('utc', clock_timestamp())
            WHERE id = $1
              AND state IN ('queued', 'waiting')
            RETURNING id, state
        """, job_id)

        if not result:
            # Check if job exists
            job = await self.conn.fetchrow(
                "SELECT id, state FROM jorb WHERE id = $1", job_id
            )
            if not job:
                raise ValueError(f"Job {job_id} not found")
            else:
                raise ValueError(
                    f"Job {job_id} is in state '{job['state']}', "
                    f"can only cancel queued or waiting jobs"
                )

        return {
            "job_id": job_id,
            "status": "cancelled"
        }

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
                results.append({
                    "job_id": job_id,
                    "status": "error",
                    "error": str(e)
                })
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
        result = await self.conn.execute(
            "DELETE FROM jorb WHERE id = $1", job_id
        )
        # asyncpg returns "DELETE N" where N is number of rows deleted
        return result == "DELETE 1"

    async def delete_jobs(
        self,
        queue: Optional[str] = None,
        state: Optional[str] = None,
        older_than_days: Optional[int] = None,
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
        params = []
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
            where_clauses.append(
                f"updated < (TIMEZONE('utc', clock_timestamp()) - ${param_idx}::interval)"
            )
            params.append(timedelta(days=older_than_days))
            param_idx += 1

        if not where_clauses:
            raise ValueError(
                "Must specify at least one filter (queue, state, or older_than_days)"
            )

        where_sql = "WHERE " + " AND ".join(where_clauses)

        result = await self.conn.execute(
            f"DELETE FROM jorb {where_sql}",
            *params
        )

        # Parse "DELETE N" to get count
        deleted_count = int(result.split()[-1])
        return deleted_count

    # =========================================================================
    # Queue Management
    # =========================================================================

    async def list_queues(self) -> list[str]:
        """
        List all queue names in the system.

        Returns:
            List of unique queue names
        """
        records = await self.conn.fetch(
            "SELECT DISTINCT queue FROM jorb ORDER BY queue"
        )
        return [r['queue'] for r in records]

    async def queue_stats(self, queue: Optional[str] = None) -> list[dict[str, Any]]:
        """
        Get statistics for queues.

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
                    THEN EXTRACT(EPOCH FROM (NOW() - created))
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
            q = r['queue']
            if q not in queue_stats_map:
                queue_stats_map[q] = QueueStats(queue=q)

            stats = queue_stats_map[q]
            state = r['state']
            count = r['count']

            if state == 'queued':
                stats.queued = count
                stats.oldest_queued_age_seconds = r['oldest_queued_age_seconds']
            elif state == 'claimed':
                stats.claimed = count
            elif state == 'running':
                stats.running = count
            elif state == 'waiting':
                stats.waiting = count
            elif state == 'finished':
                stats.finished = count
            elif state == 'crashed':
                stats.crashed = count
            elif state == 'cancelled':
                stats.cancelled = count

            stats.total += count

        return [stats.to_dict() for stats in queue_stats_map.values()]

    async def clear_queue(
        self,
        queue: str,
        state: Optional[str] = None,
        older_than_days: Optional[int] = None,
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
            queue=queue,
            state=state,
            older_than_days=older_than_days
        )

    # =========================================================================
    # Worker Management
    # =========================================================================

    async def list_workers(self) -> list[dict[str, Any]]:
        """
        List currently active workers (jobs in claimed or running state).

        Returns:
            List of worker information dictionaries
        """
        records = await self.conn.fetch("""
            SELECT
                worker_host,
                worker_pid,
                id as job_id,
                job_class,
                state,
                updated as started_at
            FROM jorb
            WHERE state IN ('claimed', 'running')
            ORDER BY worker_host, worker_pid, updated
        """)

        return [WorkerInfo.from_record(r).to_dict() for r in records]

    async def worker_stats(self) -> dict[str, Any]:
        """
        Get overall worker statistics.

        Returns:
            Dictionary with worker stats
        """
        # Count active workers
        worker_count = await self.conn.fetchval("""
            SELECT COUNT(DISTINCT (worker_host, worker_pid))
            FROM jorb
            WHERE state IN ('claimed', 'running')
        """)

        # Count jobs by worker
        jobs_by_worker = await self.conn.fetch("""
            SELECT
                worker_host,
                worker_pid,
                COUNT(*) as job_count,
                MIN(updated) as oldest_job_started
            FROM jorb
            WHERE state IN ('claimed', 'running')
            GROUP BY worker_host, worker_pid
            ORDER BY worker_host, worker_pid
        """)

        return {
            "active_workers": worker_count or 0,
            "workers": [
                {
                    "host": r['worker_host'],
                    "pid": r['worker_pid'],
                    "job_count": r['job_count'],
                    "oldest_job_started": r['oldest_job_started'].isoformat()
                        if r['oldest_job_started'] else None
                }
                for r in jobs_by_worker
            ]
        }

    # =========================================================================
    # Metrics & Monitoring
    # =========================================================================

    async def get_metrics(
        self,
        since: Optional[datetime] = None,
        queue: Optional[str] = None,
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
            since = datetime.utcnow() - timedelta(hours=24)

        where_clauses = ["updated >= $1"]
        params: list[Any] = [since]
        param_idx = 2

        if queue:
            where_clauses.append(f"queue = ${param_idx}")
            params.append(queue)
            param_idx += 1

        where_sql = "WHERE " + " AND ".join(where_clauses)

        # Overall counts by state
        state_counts = await self.conn.fetch(f"""
            SELECT state, COUNT(*) as count
            FROM jorb
            {where_sql}
            GROUP BY state
        """, *params)

        # Job completion rate
        completion_stats = await self.conn.fetchrow(f"""
            SELECT
                COUNT(*) FILTER (WHERE state = 'finished') as finished_count,
                COUNT(*) FILTER (WHERE state = 'crashed') as crashed_count,
                AVG(EXTRACT(EPOCH FROM (updated - created)))
                    FILTER (WHERE state = 'finished') as avg_duration_seconds
            FROM jorb
            {where_sql}
        """, *params)

        # Top error job classes
        top_errors = await self.conn.fetch(f"""
            SELECT
                job_class,
                COUNT(*) as error_count,
                MAX(error_message) as latest_error
            FROM jorb
            {where_sql} AND state = 'crashed'
            GROUP BY job_class
            ORDER BY error_count DESC
            LIMIT 10
        """, *params)

        return {
            "period_start": since.isoformat(),
            "period_end": datetime.utcnow().isoformat(),
            "queue": queue,
            "state_counts": {r['state']: r['count'] for r in state_counts},
            "finished_count": completion_stats['finished_count'] or 0,
            "crashed_count": completion_stats['crashed_count'] or 0,
            "avg_duration_seconds": float(completion_stats['avg_duration_seconds'] or 0),
            "top_errors": [
                {
                    "job_class": r['job_class'],
                    "error_count": r['error_count'],
                    "latest_error": r['latest_error']
                }
                for r in top_errors
            ]
        }

    # =========================================================================
    # Dead Letter Queue
    # =========================================================================

    async def list_dlq(self, limit: int = 100) -> list[dict[str, Any]]:
        """
        List jobs in Dead Letter Queue (permanently failed).

        Currently identifies DLQ jobs as crashed jobs with high error counts.
        Future: May use dedicated 'dead_letter' state.

        Args:
            limit: Maximum number of results

        Returns:
            List of DLQ job dictionaries
        """
        records = await self.conn.fetch("""
            SELECT * FROM jorb
            WHERE state = 'crashed'
              AND error_count >= 10
            ORDER BY updated DESC
            LIMIT $1
        """, limit)

        return [JobInfo.from_record(r).to_dict() for r in records]

    async def retry_from_dlq(self, job_id: int) -> dict[str, Any]:
        """
        Retry a job from the Dead Letter Queue.

        Args:
            job_id: ID of DLQ job to retry

        Returns:
            Dictionary with original_job_id and new_job_id
        """
        # Same as regular retry, but reset error_count
        job = await self.conn.fetchrow(
            "SELECT * FROM jorb WHERE id = $1", job_id
        )

        if not job:
            raise ValueError(f"Job {job_id} not found")

        if job['state'] != 'crashed':
            raise ValueError(
                f"Job {job_id} is not in DLQ (state: {job['state']})"
            )

        # Create retry job with error_count reset to 0
        new_job_id = await self.conn.fetchval("""
            INSERT INTO jorb (
                job_class, kwargs, queue, prio, uid, capability,
                run_after, run_group, admin_data, state, error_count
            )
            SELECT
                job_class, kwargs, queue, prio, uid, capability,
                TIMEZONE('utc', clock_timestamp()) as run_after,
                run_group,
                jsonb_set(
                    COALESCE(admin_data::jsonb, '{}'::jsonb),
                    '{dlq_retry_from}',
                    to_jsonb($1::bigint)
                )::json,
                'queued' as state,
                0 as error_count
            FROM jorb
            WHERE id = $1
            RETURNING id
        """, job_id)

        return {
            "original_job_id": job_id,
            "new_job_id": new_job_id,
            "status": "retry_queued_from_dlq"
        }
