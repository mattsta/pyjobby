#!/usr/bin/env python3
"""
Pyjobby Client Library

Clean, well-encapsulated client for job submission and management.
Provides a high-level interface that hides SQL complexity while supporting
all pyjobby features.

Features:
- Type hints and auto-completion
- Connection pooling for high performance
- Support for all job features (scheduling, pipelines, priorities, deadlines)
- Batch operations for high throughput
- Context manager support
- Both sync and async interfaces

Example:
    async with JobClient.from_config('./pyjobby.conf.py') as client:
        # Simple job
        job_id = await client.enqueue('myapp.jobs.SendEmail', to='user@example.com')

        # Scheduled job
        job_id = await client.enqueue(
            'myapp.jobs.Report',
            run_after=datetime.now() + timedelta(hours=1)
        )

        # Pipeline
        job1 = await client.enqueue('Step1', data=x)
        job2 = await client.enqueue('Step2', waitfor_job=job1)

        # Batch
        jobs = await client.enqueue_batch([
            ('Job1', {'arg': 1}),
            ('Job2', {'arg': 2}),
        ])
"""

import asyncpg
import json
from typing import Optional, Any, List, Tuple, Dict, Union
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from contextlib import asynccontextmanager
import asyncio


@dataclass
class JobOptions:
    """
    Options for job creation.

    Attributes:
        queue: Queue name (default: 'default')
        priority: Job priority, higher = more urgent (default: 100)
        run_after: When to run (default: now)
        capability: Required worker capability (default: None)
        uid: User/tenant ID (default: None)
        run_group: Group ID for pipeline tracking (default: None)
        waitfor_job: Wait for this job to finish first (default: None)
        waitfor_group: Wait for all jobs in this group (default: None)
        deadline_key: Idempotency key (default: None)
        admin_data: Metadata dict (default: None)
    """
    queue: str = 'default'
    priority: int = 100
    run_after: Optional[datetime] = None
    capability: Optional[str] = None
    uid: Optional[int] = None
    run_group: Optional[int] = None
    waitfor_job: Optional[int] = None
    waitfor_group: Optional[int] = None
    deadline_key: Optional[str] = None
    admin_data: Optional[Dict[str, Any]] = None


@dataclass
class JobInfo:
    """Information about an enqueued job"""
    id: int
    job_class: str
    queue: str
    priority: int
    state: str
    created: datetime


class JobClient:
    """
    High-level client for Pyjobby job queue.

    Provides a clean interface for job submission and management with
    connection pooling, type hints, and support for all pyjobby features.

    Usage:
        # Context manager (recommended)
        async with JobClient.from_config('./pyjobby.conf.py') as client:
            job_id = await client.enqueue('MyJob', arg=123)

        # Manual lifecycle
        client = JobClient(pool)
        try:
            job_id = await client.enqueue('MyJob', arg=123)
        finally:
            await client.close()
    """

    def __init__(self, pool: asyncpg.Pool):
        """
        Initialize client with connection pool.

        Args:
            pool: asyncpg connection pool

        Note: Use JobClient.create() or JobClient.from_config() instead
        """
        self.pool = pool
        self._closed = False

    @classmethod
    async def create(
        cls,
        host: str = 'localhost',
        port: int = 5432,
        database: str = 'pyjobby',
        user: str = 'postgres',
        password: Optional[str] = None,
        min_size: int = 5,
        max_size: int = 20,
        **kwargs
    ) -> 'JobClient':
        """
        Create client with new connection pool.

        Args:
            host: PostgreSQL host (default: localhost)
            port: PostgreSQL port (default: 5432)
            database: Database name (default: pyjobby)
            user: Database user (default: postgres)
            password: Database password (default: None)
            min_size: Minimum pool size (default: 5)
            max_size: Maximum pool size (default: 20)
            **kwargs: Additional asyncpg.create_pool parameters

        Returns:
            JobClient instance

        Example:
            client = await JobClient.create(
                host='db.example.com',
                database='jobs',
                user='app',
                password='secret'
            )
        """
        pool = await asyncpg.create_pool(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            min_size=min_size,
            max_size=max_size,
            **kwargs
        )
        return cls(pool)

    @classmethod
    async def from_config(cls, config_path: str, min_size: int = 5, max_size: int = 20) -> 'JobClient':
        """
        Create client from pyjobby config file.

        Args:
            config_path: Path to pyjobby.conf.py
            min_size: Minimum pool size (default: 5)
            max_size: Maximum pool size (default: 20)

        Returns:
            JobClient instance

        Example:
            client = await JobClient.from_config('./pyjobby.conf.py')
        """
        from .configloader import load_config_from_file

        config = load_config_from_file(config_path, keys=["db_params"])
        db_params = config.get("db_params", {})

        pool = await asyncpg.create_pool(
            min_size=min_size,
            max_size=max_size,
            **db_params
        )
        return cls(pool)

    async def close(self) -> None:
        """Close connection pool"""
        if not self._closed:
            await self.pool.close()
            self._closed = True

    async def __aenter__(self) -> 'JobClient':
        """Context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit"""
        await self.close()

    # =========================================================================
    # Job Enqueueing
    # =========================================================================

    async def enqueue(
        self,
        job_class: str,
        *,
        queue: str = 'default',
        priority: int = 100,
        run_after: Optional[datetime] = None,
        capability: Optional[str] = None,
        uid: Optional[int] = None,
        run_group: Optional[int] = None,
        waitfor_job: Optional[int] = None,
        waitfor_group: Optional[int] = None,
        deadline_key: Optional[str] = None,
        admin_data: Optional[Dict[str, Any]] = None,
        # Phase 2: Result Storage & Passing
        save_result: bool = False,
        use_result_from: Optional[int] = None,
        # Phase 2: Retry Strategies
        retry_strategy: str = "exponential",
        max_retries: int = 10,
        initial_retry_delay: int = 1,
        max_retry_delay: int = 3600,
        # Phase 2: Timeout Enforcement
        timeout_seconds: Optional[int] = None,
        on_timeout: str = "retry",
        **kwargs: Any
    ) -> int:
        """
        Enqueue a job.

        Args:
            job_class: Python class path (e.g., 'myapp.jobs.SendEmail')
            queue: Queue name (default: 'default')
            priority: Priority (higher = more urgent, default: 100)
            run_after: When to run (default: now)
            capability: Required worker capability (default: None)
            uid: User/tenant ID (default: None)
            run_group: Group ID for pipeline tracking (default: None)
            waitfor_job: Wait for this job ID to complete (default: None)
            waitfor_group: Wait for all jobs in this group (default: None)
            deadline_key: Idempotency key (default: None)
            admin_data: Metadata dict (default: None)
            save_result: Store job result in database (Phase 2, default: False)
            use_result_from: Inject result from this job ID into kwargs (Phase 2)
            retry_strategy: 'exponential', 'linear', 'fibonacci', 'fixed' (Phase 2)
            max_retries: Maximum retry attempts (Phase 2, default: 10)
            initial_retry_delay: Starting retry delay in seconds (Phase 2, default: 1)
            max_retry_delay: Maximum retry delay cap (Phase 2, default: 3600)
            timeout_seconds: Job execution timeout in seconds (Phase 2, default: None)
            on_timeout: 'retry' or 'fail' (Phase 2, default: 'retry')
            **kwargs: Job arguments (passed to job class)

        Returns:
            Job ID

        Raises:
            asyncpg.UniqueViolationError: If deadline_key already exists
            ValueError: If both waitfor_job and waitfor_group specified

        Examples:
            # Simple job
            job_id = await client.enqueue('myapp.jobs.SendEmail', to='user@example.com')

            # Scheduled job (run in 1 hour)
            job_id = await client.enqueue(
                'myapp.jobs.Report',
                run_after=datetime.now() + timedelta(hours=1),
                report_type='daily'
            )

            # High priority job
            job_id = await client.enqueue(
                'myapp.jobs.UrgentTask',
                priority=500,
                task_id=123
            )

            # Job requiring specific worker capability
            job_id = await client.enqueue(
                'myapp.jobs.GPUTask',
                capability='gpu',
                model='resnet50'
            )

            # Idempotent job (safe to retry)
            job_id = await client.enqueue(
                'myapp.jobs.ProcessPayment',
                deadline_key=f'payment:{payment_id}',
                payment_id=payment_id
            )

            # Pipeline with result passing (Phase 2)
            job1 = await client.enqueue('FetchData', url='...', save_result=True)
            job2 = await client.enqueue('ProcessData', waitfor_job=job1, use_result_from=job1)

            # Job with timeout and exponential backoff (Phase 2)
            job_id = await client.enqueue(
                'ApiCall',
                timeout_seconds=30,
                retry_strategy='exponential',
                max_retries=15,
                on_timeout='retry'
            )
        """
        # Validate parameters
        if waitfor_job and waitfor_group:
            raise ValueError("Cannot specify both waitfor_job and waitfor_group")

        # Phase 2: Fetch upstream result if requested
        if use_result_from:
            async with self.pool.acquire() as conn:
                upstream = await conn.fetchrow(
                    "SELECT result FROM jorb WHERE id = $1",
                    use_result_from
                )
                if upstream and upstream['result']:
                    kwargs['upstream_result'] = upstream['result']

        # Default run_after to now if not specified
        if run_after is None:
            run_after = datetime.utcnow()

        # Determine initial state
        if waitfor_job or waitfor_group:
            state = 'waiting'
        else:
            state = 'queued'

        # Phase 2: Build admin_data with Phase 2 features
        if admin_data is None:
            admin_data = {}

        # Add save_result flag if requested
        if save_result:
            admin_data['save_result'] = True

        # Add retry strategy configuration
        admin_data['retry_strategy'] = retry_strategy
        admin_data['max_retries'] = max_retries
        admin_data['initial_retry_delay'] = initial_retry_delay
        admin_data['max_retry_delay'] = max_retry_delay

        # Add timeout configuration if specified
        if timeout_seconds:
            admin_data['timeout_seconds'] = timeout_seconds
            admin_data['on_timeout'] = on_timeout

        # Execute INSERT
        async with self.pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (
                    job_class, kwargs, queue, prio, run_after,
                    capability, uid, run_group,
                    waitfor_job, waitfor_group,
                    deadline_key, admin_data, state
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                RETURNING id
            """,
                job_class,
                kwargs,  # Dict - custom codec handles conversion
                queue,
                priority,
                run_after,
                capability,
                uid,
                run_group,
                waitfor_job,
                waitfor_group,
                deadline_key,
                admin_data,  # Dict - custom codec handles conversion
                state
            )

        return job_id

    async def enqueue_batch(
        self,
        jobs: List[Tuple[str, Dict[str, Any]]],
        queue: str = 'default',
        priority: int = 100,
        run_after: Optional[datetime] = None,
        run_group: Optional[int] = None,
    ) -> List[int]:
        """
        Enqueue multiple jobs efficiently in a single transaction.

        Args:
            jobs: List of (job_class, kwargs) tuples
            queue: Queue name for all jobs (default: 'default')
            priority: Priority for all jobs (default: 100)
            run_after: When to run all jobs (default: now)
            run_group: Group ID for all jobs (default: None)

        Returns:
            List of job IDs

        Example:
            # Enqueue 1000 jobs efficiently
            jobs = [
                ('myapp.jobs.ProcessItem', {'item_id': i})
                for i in range(1000)
            ]
            job_ids = await client.enqueue_batch(jobs, queue='processing')

            # Pipeline: enqueue all at once, they'll wait for previous group
            job_ids = await client.enqueue_batch([
                ('Step1', {'data': x}),
                ('Step2', {'data': y}),
                ('Step3', {'data': z}),
            ], run_group=123)
        """
        if not jobs:
            return []

        if run_after is None:
            run_after = datetime.utcnow()

        # Prepare values for batch insert
        values = []
        for job_class, kwargs in jobs:
            values.append((
                job_class,
                json.dumps(kwargs),
                queue,
                priority,
                run_after,
                run_group,
            ))

        # Execute batch INSERT
        async with self.pool.acquire() as conn:
            # Use unnest for efficient bulk insert
            job_ids = await conn.fetch("""
                INSERT INTO jorb (
                    job_class, kwargs, queue, prio, run_after, run_group, state
                )
                SELECT
                    job_class,
                    kwargs::jsonb,
                    queue,
                    prio,
                    run_after,
                    run_group,
                    'queued'::jorbstate as state
                FROM UNNEST(
                    $1::text[],
                    $2::text[],
                    $3::text[],
                    $4::int[],
                    $5::timestamptz[],
                    $6::bigint[]
                ) AS t(job_class, kwargs, queue, prio, run_after, run_group)
                RETURNING id
            """,
                [v[0] for v in values],  # job_class
                [v[1] for v in values],  # kwargs
                [v[2] for v in values],  # queue
                [v[3] for v in values],  # prio
                [v[4] for v in values],  # run_after
                [v[5] for v in values],  # run_group
            )

        return [row['id'] for row in job_ids]

    # =========================================================================
    # Job Inspection & Management
    # =========================================================================

    async def get_job(self, job_id: int) -> Optional[JobInfo]:
        """
        Get job information.

        Args:
            job_id: Job ID

        Returns:
            JobInfo or None if not found

        Example:
            job = await client.get_job(12345)
            if job:
                print(f"Job {job.id} is {job.state}")
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, job_class, queue, prio as priority, state, created
                FROM jorb
                WHERE id = $1
            """, job_id)

        if not row:
            return None

        return JobInfo(**dict(row))

    async def cancel_job(self, job_id: int) -> bool:
        """
        Cancel a job (if not already running).

        Args:
            job_id: Job ID

        Returns:
            True if cancelled, False if not found or already running

        Example:
            if await client.cancel_job(12345):
                print("Job cancelled")
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE jorb
                SET state = 'cancelled'
                WHERE id = $1
                  AND state IN ('queued', 'waiting')
            """, job_id)

        return result != "UPDATE 0"

    async def retry_job(self, job_id: int) -> Optional[int]:
        """
        Retry a failed/crashed job (creates a new job).

        Args:
            job_id: Job ID to retry

        Returns:
            New job ID, or None if original job not found

        Example:
            new_job_id = await client.retry_job(12345)
            if new_job_id:
                print(f"Created retry job: {new_job_id}")
        """
        async with self.pool.acquire() as conn:
            new_job_id = await conn.fetchval("""
                INSERT INTO jorb (
                    job_class, kwargs, queue, prio, uid, capability,
                    run_after, run_group, admin_data, state
                )
                SELECT
                    job_class, kwargs, queue, prio, uid, capability,
                    TIMEZONE('utc', clock_timestamp()) as run_after,
                    run_group,
                    (COALESCE(admin_data::text::jsonb, '{}'::jsonb) || jsonb_build_object('retry_of', $1::bigint))::json as admin_data,
                    'queued' as state
                FROM jorb
                WHERE id = $1::bigint
                  AND state IN ('crashed', 'finished')
                RETURNING id
            """, job_id)

        return new_job_id

    # =========================================================================
    # Queue Operations
    # =========================================================================

    async def queue_depth(self, queue: str = 'default') -> int:
        """
        Get number of queued jobs in a queue.

        Args:
            queue: Queue name (default: 'default')

        Returns:
            Number of queued jobs

        Example:
            depth = await client.queue_depth('emails')
            print(f"Queue has {depth} jobs waiting")
        """
        async with self.pool.acquire() as conn:
            return await conn.fetchval("""
                SELECT COUNT(*)
                FROM jorb
                WHERE queue = $1
                  AND state = 'queued'
            """, queue)

    async def queue_stats(self, queue: str = 'default') -> Dict[str, int]:
        """
        Get statistics for a queue.

        Args:
            queue: Queue name (default: 'default')

        Returns:
            Dict with counts by state

        Example:
            stats = await client.queue_stats('emails')
            print(f"Queued: {stats['queued']}, Running: {stats['running']}")
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT state, COUNT(*) as count
                FROM jorb
                WHERE queue = $1
                GROUP BY state
            """, queue)

        stats = {row['state']: row['count'] for row in rows}

        # Ensure all states are present
        for state in ['queued', 'claimed', 'running', 'waiting', 'finished', 'crashed', 'cancelled']:
            stats.setdefault(state, 0)

        return stats

    async def list_queues(self) -> List[Dict[str, Any]]:
        """
        List all queues with statistics.

        Returns:
            List of dicts with queue name and stats

        Example:
            queues = await client.list_queues()
            for q in queues:
                print(f"{q['queue']}: {q['queued']} queued, {q['running']} running")
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    queue,
                    COUNT(*) FILTER (WHERE state = 'queued') as queued,
                    COUNT(*) FILTER (WHERE state = 'claimed') as claimed,
                    COUNT(*) FILTER (WHERE state = 'running') as running,
                    COUNT(*) FILTER (WHERE state = 'waiting') as waiting,
                    COUNT(*) FILTER (WHERE state = 'finished') as finished,
                    COUNT(*) FILTER (WHERE state = 'crashed') as crashed,
                    COUNT(*) FILTER (WHERE state = 'cancelled') as cancelled,
                    COUNT(*) as total
                FROM jorb
                GROUP BY queue
                ORDER BY queue
            """)

        return [dict(row) for row in rows]

    async def purge_queue(self, queue: str, states: Optional[List[str]] = None) -> int:
        """
        Delete jobs from a queue.

        Args:
            queue: Queue name
            states: List of states to delete (default: ['queued', 'waiting'])

        Returns:
            Number of jobs deleted

        Example:
            # Delete all queued/waiting jobs
            deleted = await client.purge_queue('emails')

            # Delete only finished jobs
            deleted = await client.purge_queue('emails', states=['finished'])
        """
        if states is None:
            states = ['queued', 'waiting']

        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM jorb
                WHERE queue = $1
                  AND state = ANY($2::jorbstate[])
            """, queue, states)

        # Extract row count from result like "DELETE 42"
        return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    # =========================================================================
    # Extended Job Management
    # =========================================================================

    async def get_job_full(self, job_id: int) -> Optional[Dict[str, Any]]:
        """
        Get complete job details including kwargs, result, etc.

        Args:
            job_id: Job ID

        Returns:
            Dict with all job fields, or None if not found

        Example:
            job = await client.get_job_full(12345)
            if job:
                print(f"Job kwargs: {job['kwargs']}")
                print(f"Result: {job['result']}")
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT *
                FROM jorb
                WHERE id = $1
            """, job_id)

        if not row:
            return None

        return dict(row)

    async def get_job_result(self, job_id: int) -> Optional[Any]:
        """
        Get job result.

        Args:
            job_id: Job ID

        Returns:
            Job result (parsed from JSON), or None if not finished or no result

        Example:
            result = await client.get_job_result(12345)
            if result:
                print(f"Job returned: {result}")
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT result, state
                FROM jorb
                WHERE id = $1
            """, job_id)

        if not row or row['state'] != 'finished' or not row['result']:
            return None

        # Result is stored as JSON
        result = row['result']
        if isinstance(result, str):
            return json.loads(result)
        return result

    async def delete_job(self, job_id: int) -> bool:
        """
        Delete a job from the database.

        Args:
            job_id: Job ID

        Returns:
            True if deleted, False if not found

        Example:
            if await client.delete_job(12345):
                print("Job deleted")
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM jorb
                WHERE id = $1
            """, job_id)

        return result != "DELETE 0"

    async def update_job_priority(self, job_id: int, new_priority: int) -> bool:
        """
        Update job priority (only for queued/waiting jobs).

        Args:
            job_id: Job ID
            new_priority: New priority value

        Returns:
            True if updated, False if not found or already running

        Example:
            # Make job higher priority
            if await client.update_job_priority(12345, 500):
                print("Priority updated")
        """
        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE jorb
                SET prio = $2
                WHERE id = $1
                  AND state IN ('queued', 'waiting')
            """, job_id, new_priority)

        return result != "UPDATE 0"

    async def get_jobs(
        self,
        queue: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = 'created',
        ascending: bool = False
    ) -> List[Dict[str, Any]]:
        """
        List jobs with filtering and pagination.

        Args:
            queue: Filter by queue (default: all queues)
            state: Filter by state (default: all states)
            limit: Maximum number of jobs to return (default: 100)
            offset: Number of jobs to skip (default: 0)
            order_by: Field to sort by (default: 'created')
            ascending: Sort ascending if True, descending if False (default: False)

        Returns:
            List of job dicts

        Example:
            # Get latest 50 queued jobs
            jobs = await client.get_jobs(state='queued', limit=50)

            # Get jobs from specific queue
            jobs = await client.get_jobs(queue='emails', limit=20)
        """
        # Build WHERE clause
        where_clauses = []
        params = []
        param_num = 1

        if queue:
            where_clauses.append(f"queue = ${param_num}")
            params.append(queue)
            param_num += 1

        if state:
            where_clauses.append(f"state = ${param_num}::jorbstate")
            params.append(state)
            param_num += 1

        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

        # Validate order_by to prevent SQL injection
        valid_fields = ['id', 'created', 'prio', 'run_after', 'started', 'finished', 'queue', 'state']
        if order_by not in valid_fields:
            order_by = 'created'

        direction = 'ASC' if ascending else 'DESC'

        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT *
                FROM jorb
                WHERE {where_sql}
                ORDER BY {order_by} {direction}
                LIMIT ${param_num}
                OFFSET ${param_num + 1}
            """, *params)

        return [dict(row) for row in rows]

    async def search_jobs(
        self,
        job_class: Optional[str] = None,
        min_priority: Optional[int] = None,
        max_priority: Optional[int] = None,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        uid: Optional[int] = None,
        run_group: Optional[int] = None,
        capability: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Search jobs by various criteria.

        Args:
            job_class: Filter by job class (supports wildcards with %)
            min_priority: Minimum priority (inclusive)
            max_priority: Maximum priority (inclusive)
            created_after: Jobs created after this datetime
            created_before: Jobs created before this datetime
            uid: Filter by user/tenant ID
            run_group: Filter by run group
            capability: Filter by required capability
            limit: Maximum number of results (default: 100)

        Returns:
            List of matching job dicts

        Example:
            # Find high-priority email jobs created today
            jobs = await client.search_jobs(
                job_class='%Email%',
                min_priority=200,
                created_after=datetime.now() - timedelta(days=1)
            )
        """
        where_clauses = []
        params = []
        param_num = 1

        if job_class:
            where_clauses.append(f"job_class LIKE ${param_num}")
            params.append(job_class)
            param_num += 1

        if min_priority is not None:
            where_clauses.append(f"prio >= ${param_num}")
            params.append(min_priority)
            param_num += 1

        if max_priority is not None:
            where_clauses.append(f"prio <= ${param_num}")
            params.append(max_priority)
            param_num += 1

        if created_after:
            where_clauses.append(f"created >= ${param_num}")
            params.append(created_after)
            param_num += 1

        if created_before:
            where_clauses.append(f"created <= ${param_num}")
            params.append(created_before)
            param_num += 1

        if uid is not None:
            where_clauses.append(f"uid = ${param_num}")
            params.append(uid)
            param_num += 1

        if run_group is not None:
            where_clauses.append(f"run_group = ${param_num}")
            params.append(run_group)
            param_num += 1

        if capability:
            where_clauses.append(f"capability = ${param_num}")
            params.append(capability)
            param_num += 1

        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"
        params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT *
                FROM jorb
                WHERE {where_sql}
                ORDER BY created DESC
                LIMIT ${param_num}
            """, *params)

        return [dict(row) for row in rows]

    async def get_failed_jobs(self, queue: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get crashed/failed jobs.

        Args:
            queue: Filter by queue (default: all queues)
            limit: Maximum number of jobs (default: 100)

        Returns:
            List of failed job dicts

        Example:
            failed = await client.get_failed_jobs(queue='processing', limit=50)
            for job in failed:
                print(f"Job {job['id']} failed: {job['error']}")
        """
        where = "state = 'crashed'"
        params = []

        if queue:
            where += " AND queue = $1"
            params.append(queue)
            params.append(limit)
        else:
            params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT *
                FROM jorb
                WHERE {where}
                ORDER BY finished DESC
                LIMIT ${len(params)}
            """, *params)

        return [dict(row) for row in rows]

    async def get_waiting_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get jobs waiting on dependencies.

        Args:
            limit: Maximum number of jobs (default: 100)

        Returns:
            List of waiting job dicts

        Example:
            waiting = await client.get_waiting_jobs()
            for job in waiting:
                print(f"Job {job['id']} waiting for {job['waitfor_job'] or job['waitfor_group']}")
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT *
                FROM jorb
                WHERE state = 'waiting'
                ORDER BY created DESC
                LIMIT $1
            """, limit)

        return [dict(row) for row in rows]

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    async def bulk_cancel(self, job_ids: List[int]) -> int:
        """
        Cancel multiple jobs.

        Args:
            job_ids: List of job IDs to cancel

        Returns:
            Number of jobs cancelled

        Example:
            cancelled = await client.bulk_cancel([123, 456, 789])
            print(f"Cancelled {cancelled} jobs")
        """
        if not job_ids:
            return 0

        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE jorb
                SET state = 'cancelled'
                WHERE id = ANY($1::bigint[])
                  AND state IN ('queued', 'waiting')
            """, job_ids)

        return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    async def bulk_retry(self, job_ids: List[int]) -> List[int]:
        """
        Retry multiple failed jobs.

        Args:
            job_ids: List of job IDs to retry

        Returns:
            List of new job IDs

        Example:
            new_job_ids = await client.bulk_retry([123, 456, 789])
            print(f"Created {len(new_job_ids)} retry jobs")
        """
        if not job_ids:
            return []

        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                INSERT INTO jorb (
                    job_class, kwargs, queue, prio, uid, capability,
                    run_after, run_group, admin_data, state
                )
                SELECT
                    job_class, kwargs, queue, prio, uid, capability,
                    TIMEZONE('utc', clock_timestamp()) as run_after,
                    run_group,
                    (COALESCE(admin_data::text::jsonb, '{}'::jsonb) || jsonb_build_object('retry_of', id))::json as admin_data,
                    'queued' as state
                FROM jorb
                WHERE id = ANY($1::bigint[])
                  AND state IN ('crashed', 'finished')
                RETURNING id
            """, job_ids)

        return [row['id'] for row in rows]

    async def bulk_delete(self, job_ids: List[int]) -> int:
        """
        Delete multiple jobs.

        Args:
            job_ids: List of job IDs to delete

        Returns:
            Number of jobs deleted

        Example:
            deleted = await client.bulk_delete([123, 456, 789])
            print(f"Deleted {deleted} jobs")
        """
        if not job_ids:
            return 0

        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM jorb
                WHERE id = ANY($1::bigint[])
            """, job_ids)

        return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    async def bulk_update_priority(self, job_ids: List[int], new_priority: int) -> int:
        """
        Update priority for multiple jobs.

        Args:
            job_ids: List of job IDs
            new_priority: New priority value

        Returns:
            Number of jobs updated

        Example:
            updated = await client.bulk_update_priority([123, 456], 500)
            print(f"Updated {updated} jobs to priority 500")
        """
        if not job_ids:
            return 0

        async with self.pool.acquire() as conn:
            result = await conn.execute("""
                UPDATE jorb
                SET prio = $2
                WHERE id = ANY($1::bigint[])
                  AND state IN ('queued', 'waiting')
            """, job_ids, new_priority)

        return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    # =========================================================================
    # Advanced Features
    # =========================================================================

    async def create_pipeline(
        self,
        steps: List[Tuple[str, Dict[str, Any]]],
        queue: str = 'default',
        priority: int = 100,
    ) -> List[int]:
        """
        Create a job pipeline where each step waits for the previous.

        Args:
            steps: List of (job_class, kwargs) tuples
            queue: Queue name (default: 'default')
            priority: Priority for all jobs (default: 100)

        Returns:
            List of job IDs

        Example:
            # Data processing pipeline
            job_ids = await client.create_pipeline([
                ('myapp.jobs.FetchData', {'source': 'api'}),
                ('myapp.jobs.TransformData', {'format': 'json'}),
                ('myapp.jobs.LoadData', {'destination': 'db'}),
            ])

            # job_ids[1] waits for job_ids[0]
            # job_ids[2] waits for job_ids[1]
        """
        if not steps:
            return []

        job_ids = []
        previous_job = None

        for job_class, kwargs in steps:
            job_id = await self.enqueue(
                job_class,
                queue=queue,
                priority=priority,
                waitfor_job=previous_job,
                **kwargs
            )
            job_ids.append(job_id)
            previous_job = job_id

        return job_ids

    async def create_fan_out(
        self,
        job_class: str,
        items: List[Dict[str, Any]],
        queue: str = 'default',
        priority: int = 100,
        run_group: Optional[int] = None,
    ) -> Tuple[List[int], int]:
        """
        Create fan-out pattern: process many items in parallel.

        Args:
            job_class: Job class to run for each item
            items: List of kwargs dicts, one per job
            queue: Queue name (default: 'default')
            priority: Priority (default: 100)
            run_group: Group ID (default: auto-generated)

        Returns:
            Tuple of (job_ids, run_group)

        Example:
            # Process 1000 orders in parallel
            orders = [{'order_id': i} for i in range(1000)]
            job_ids, group_id = await client.create_fan_out(
                'myapp.jobs.ProcessOrder',
                orders,
                queue='processing'
            )

            # Later, create a job that waits for all of them
            summary_job = await client.enqueue(
                'myapp.jobs.SummarizeOrders',
                waitfor_group=group_id
            )
        """
        if run_group is None:
            # Auto-generate group ID
            async with self.pool.acquire() as conn:
                run_group = await conn.fetchval("SELECT nextval('jorb_id_seq')")

        jobs = [(job_class, kwargs) for kwargs in items]
        job_ids = await self.enqueue_batch(
            jobs,
            queue=queue,
            priority=priority,
            run_group=run_group
        )

        return job_ids, run_group

    async def health_check(self) -> bool:
        """
        Check if database connection is healthy.

        Returns:
            True if healthy, False otherwise

        Example:
            if not await client.health_check():
                print("Database connection unhealthy!")
        """
        try:
            async with self.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # =========================================================================
    # Phase 2: DAG Support
    # =========================================================================

    def dag(self, name: Optional[str] = None, **common_options) -> 'DAGBuilder':
        """
        Create a DAG (Directed Acyclic Graph) builder.

        Args:
            name: Optional DAG name for debugging/monitoring
            **common_options: Options applied to all jobs (queue, priority, etc.)

        Returns:
            DAGBuilder instance

        Example:
            # Simple DAG
            dag = client.dag(name='ETL Pipeline', queue='data')
            fetch = dag.add('FetchData', {'source': 'api'})
            process = dag.add('ProcessData', depends_on=[fetch])
            load = dag.add('LoadData', depends_on=[process])

            # Execute DAG
            node_to_job = await dag.execute(client)

            # Complex DAG with parallelism
            dag = client.dag(name='ML Training')
            fetch_train = dag.add('FetchTrainData')
            fetch_test = dag.add('FetchTestData')
            preprocess = dag.add('Preprocess', depends_on=[fetch_train, fetch_test])
            train = dag.add('TrainModel', depends_on=[preprocess])
            evaluate = dag.add('Evaluate', depends_on=[train])
            deploy = dag.add('Deploy', depends_on=[evaluate])

            node_to_job = await dag.execute(client)
        """
        from .dag import DAGBuilder
        return DAGBuilder(name=name, **common_options)

    async def execute_dag(self, dag: 'DAGBuilder') -> Dict:
        """
        Execute a DAG and return node->job_id mapping.

        Args:
            dag: DAGBuilder instance

        Returns:
            Dict mapping DAGNode to job_id

        Example:
            from pyjobby.dag import DAGBuilder

            dag = DAGBuilder(name='Pipeline')
            step1 = dag.add('Step1')
            step2 = dag.add('Step2', depends_on=[step1])

            node_to_job = await client.execute_dag(dag)
            print(f"Step1 job ID: {node_to_job[step1]}")
        """
        return await dag.execute(self)

    async def get_dag_status(self, dag_id: int) -> Dict[str, Any]:
        """
        Get DAG execution status.

        Args:
            dag_id: DAG ID

        Returns:
            Dict with DAG status information

        Example:
            status = await client.get_dag_status(123)
            print(f"DAG state: {status['dag_state']}")
            print(f"Completed: {status['finished_jobs']}/{status['total_jobs']}")
        """
        from .dag import get_dag_status
        return await get_dag_status(self.pool, dag_id)

    async def wait_for_dag(self, dag_id: int, timeout: int = 3600) -> bool:
        """
        Wait for DAG to complete.

        Args:
            dag_id: DAG ID
            timeout: Maximum wait time in seconds (default: 3600)

        Returns:
            True if DAG completed successfully, False if failed or timeout

        Example:
            # Execute DAG
            dag = client.dag(name='Pipeline')
            # ... build DAG ...
            node_to_job = await dag.execute(client)

            # Get DAG ID from any job
            dag_id = await client.pool.fetchval(
                "SELECT dag_id FROM jorb WHERE id = $1",
                list(node_to_job.values())[0]
            )

            # Wait for completion
            if await client.wait_for_dag(dag_id, timeout=1800):
                print("DAG completed successfully!")
            else:
                print("DAG failed or timed out")
        """
        from .dag import wait_for_dag
        return await wait_for_dag(self.pool, dag_id, timeout)

    # =========================================================================
    # Phase 2: Pipeline with Result Passing
    # =========================================================================

    async def create_pipeline_with_results(
        self,
        stages: List[Tuple[str, dict, bool]],
        queue: str = 'default',
        priority: int = 100,
        **common_options
    ) -> List[int]:
        """
        Create a linear pipeline where each stage can receive the previous stage's result.

        Args:
            stages: List of (job_class, kwargs, save_result) tuples
            queue: Queue name (default: 'default')
            priority: Priority for all jobs (default: 100)
            **common_options: Additional options for all jobs

        Returns:
            List of job IDs

        Example:
            # Pipeline with result passing
            job_ids = await client.create_pipeline_with_results([
                ('FetchData', {'url': 'https://...'}, True),     # Save result
                ('ProcessData', {}, True),                        # Save result
                ('StoreResults', {}, False),                      # Don't save
            ])

            # Each job receives previous job's result in kwargs['upstream_result']
        """
        job_ids = []
        previous_job = None
        previous_saved_result = False

        for job_class, kwargs, save_result in stages:
            job_id = await self.enqueue(
                job_class,
                **kwargs,
                queue=queue,
                priority=priority,
                save_result=save_result,
                use_result_from=previous_job if previous_saved_result else None,
                waitfor_job=previous_job,
                **common_options
            )
            job_ids.append(job_id)
            previous_job = job_id
            previous_saved_result = save_result

        return job_ids
