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
- Async context manager support

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

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any

import asyncpg  # type: ignore[import-untyped]

from . import db

if TYPE_CHECKING:
    from .dag import DAGBuilder


class JobError(Exception):
    """Base class for job-outcome errors raised by the client library."""


class JobFailedError(JobError):
    """The awaited job reached the terminal 'crashed' state (the DLQ)."""

    def __init__(self, job_id: int, error_message: str | None = None):
        self.job_id = job_id
        self.error_message = error_message
        super().__init__(f"job {job_id} crashed: {error_message or 'unknown error'}")


class JobCancelledError(JobError):
    """The awaited job reached the terminal 'cancelled' state."""

    def __init__(self, job_id: int):
        self.job_id = job_id
        super().__init__(f"job {job_id} was cancelled")


# Sentinel returned by poll callbacks when the awaited condition is not yet
# satisfied (None is a legitimate job result / event value).
_PENDING: Any = object()

# The single enqueue INSERT shared by every enqueue path (pool-based
# enqueue(), caller-transaction enqueue_in_transaction(), handles).
_ENQUEUE_SQL = """
    INSERT INTO jorb (
        job_class, kwargs, queue, prio, run_after,
        capability, uid, run_group,
        waitfor_job, waitfor_group,
        deadline_key, admin_data, state
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    RETURNING id
"""


@dataclass
class JobInfo:
    """Lightweight job summary returned by JobClient.get_job().

    (The admin API has its own, richer JobInfo covering every jorb column
    with ISO-serialized datetimes — that one is for operations tooling;
    this one is the minimal client-facing view.)"""

    id: int
    job_class: str
    queue: str
    priority: int
    state: str
    created: datetime


@dataclass
class JobHandle:
    """A job id paired with the client that enqueued it.

    Returned by JobClient.enqueue_handle() (plain enqueue() still returns a
    bare int for simple use). Every method delegates to the client, so a
    handle stays valid for the job's whole life — retries keep the same id.
    """

    id: int
    client: JobClient

    async def wait(self, timeout: float | None = None) -> Any:
        """Wait for the terminal state; see JobClient.wait_for_result()."""
        return await self.client.wait_for_result(self.id, timeout=timeout)

    async def status(self) -> str | None:
        """Current state, or None if the row no longer exists."""
        info = await self.client.get_job(self.id)
        return info.state if info else None

    async def result(self) -> Any | None:
        """Stored result if finished (no waiting); see get_job_result()."""
        return await self.client.get_job_result(self.id)

    async def cancel(self) -> str | None:
        """Cancel the job; see JobClient.cancel_job()."""
        return await self.client.cancel_job(self.id)

    async def event(self, key: str, timeout: float | None = None) -> Any:
        """Wait for a jorb_event published by this job; see get_event()."""
        return await self.client.get_event(self.id, key, timeout=timeout)


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

    def __init__(
        self,
        pool: asyncpg.Pool,
        db_params: dict[str, Any] | str | None = None,
    ):
        """
        Initialize client with connection pool.

        Args:
            pool: asyncpg connection pool
            db_params: optional connection parameters — a dict of
                asyncpg.connect kwargs or a DSN string — used to open the
                shared LISTEN connection that powers wait_for_result() and
                get_event(). When omitted (pool-only construction) those
                methods still work but fall back to pure polling.

        Note: Use JobClient.create() or JobClient.from_config() instead
        """
        self.pool = pool
        self._closed = False
        self._db_params = db_params
        self._listener_conn: asyncpg.Connection | None = None
        self._listener_lock = asyncio.Lock()
        # waiters keyed by job id ('jorb_done') / (job_id, key) ('jorb_event')
        self._done_waiters: dict[int, list[asyncio.Event]] = {}
        self._event_waiters: dict[tuple[int, str], list[asyncio.Event]] = {}

    @classmethod
    async def create(
        cls,
        host: str = "localhost",
        port: int = 5432,
        database: str = "pyjobby",
        user: str = "postgres",
        password: str | None = None,
        min_size: int = 5,
        max_size: int = 20,
        **kwargs: Any,
    ) -> JobClient:
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
        pool = await db.create_pool(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            min_size=min_size,
            max_size=max_size,
            **kwargs,
        )
        db_params: dict[str, Any] = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password,
        }
        return cls(pool, db_params=db_params)

    @classmethod
    async def from_config(
        cls, config_path: str, min_size: int = 5, max_size: int = 20
    ) -> JobClient:
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

        pool = await db.create_pool(min_size=min_size, max_size=max_size, **db_params)
        return cls(pool, db_params=db_params)

    async def close(self) -> None:
        """Close the shared LISTEN connection (if open) and the pool."""
        if not self._closed:
            self._closed = True
            if self._listener_conn is not None:
                with contextlib.suppress(Exception):
                    await self._listener_conn.close()
                self._listener_conn = None
            await self.pool.close()

    async def __aenter__(self) -> JobClient:
        """Context manager entry"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Context manager exit"""
        await self.close()

    # =========================================================================
    # Job Enqueueing
    # =========================================================================

    async def enqueue(
        self,
        job_class: str,
        *,
        queue: str = "default",
        priority: int = 100,
        run_after: datetime | None = None,
        capability: str | None = None,
        uid: int | None = None,
        run_group: int | None = None,
        waitfor_job: int | None = None,
        waitfor_group: int | None = None,
        deadline_key: str | None = None,
        admin_data: dict[str, Any] | None = None,
        # Phase 2: Result Storage & Passing
        save_result: bool = True,
        use_result_from: int | None = None,
        # Phase 2: Retry Strategies
        retry_strategy: str = "exponential",
        max_retries: int = 10,
        initial_retry_delay: int = 1,
        max_retry_delay: int = 3600,
        # Phase 2: Timeout Enforcement
        timeout_seconds: int | None = None,
        on_timeout: str = "retry",
        **kwargs: Any,
    ) -> int:
        """
        Enqueue a job.

        Args:
            job_class: Python class path (e.g., 'myapp.jobs.SendEmail')
            queue: Queue name (default: 'default')
            priority: Priority — LOWER numbers are more urgent; workers only
                claim jobs with priority <= their own ceiling (default: 100)
            run_after: When to run (default: now)
            capability: Required worker capability (default: None)
            uid: User/tenant ID (default: None)
            run_group: Group ID for pipeline tracking (default: None)
            waitfor_job: Wait for this job ID to complete (default: None)
            waitfor_group: Wait for all jobs in this group (default: None)
            deadline_key: Idempotency key (default: None)
            admin_data: Metadata dict (default: None)
            save_result: Store job result in database (default: True; pass
                False to discard results of large/uninteresting jobs)
            use_result_from: Inject the (run-time) result of this job ID into
                this job's kwargs as 'upstream_result' when it executes.
                Combine with waitfor_job so the upstream has finished first.
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

            # High priority job (lower number = claimed first)
            job_id = await client.enqueue(
                'myapp.jobs.UrgentTask',
                priority=1,
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
        async with self.pool.acquire() as conn:
            return await self.enqueue_in_transaction(
                conn,
                job_class,
                queue=queue,
                priority=priority,
                run_after=run_after,
                capability=capability,
                uid=uid,
                run_group=run_group,
                waitfor_job=waitfor_job,
                waitfor_group=waitfor_group,
                deadline_key=deadline_key,
                admin_data=admin_data,
                save_result=save_result,
                use_result_from=use_result_from,
                retry_strategy=retry_strategy,
                max_retries=max_retries,
                initial_retry_delay=initial_retry_delay,
                max_retry_delay=max_retry_delay,
                timeout_seconds=timeout_seconds,
                on_timeout=on_timeout,
                **kwargs,
            )

    async def enqueue_handle(self, job_class: str, **options: Any) -> JobHandle:
        """Enqueue a job (same keyword arguments as enqueue()) and return a
        JobHandle instead of a bare id.

        Example:
            handle = await client.enqueue_handle('myapp.jobs.Report', day='mon')
            result = await handle.wait(timeout=60)
        """
        job_id = await self.enqueue(job_class, **options)
        return JobHandle(id=job_id, client=self)

    @staticmethod
    async def enqueue_in_transaction(
        conn: asyncpg.Connection, job_class: str, **options: Any
    ) -> int:
        """Enqueue a job on a CALLER-provided connection/transaction.

        Transactional-outbox helper: run the exact same INSERT as enqueue()
        inside a transaction the caller controls, so the job becomes visible
        if and only if the surrounding transaction commits.

        Accepts the same keyword arguments as enqueue() (queue, priority,
        run_after, ..., plus job kwargs). The connection must have pyjobby's
        JSON codecs registered (any connection from pyjobby.db does).

        Example:
            async with conn.transaction():
                await conn.execute("INSERT INTO orders ...")
                job_id = await JobClient.enqueue_in_transaction(
                    conn, 'myapp.jobs.FulfillOrder', order_id=42
                )
        """
        args = JobClient._build_enqueue_row(job_class, **options)
        job_id: int = await conn.fetchval(_ENQUEUE_SQL, *args)
        return job_id

    @staticmethod
    def _build_enqueue_row(
        job_class: str,
        *,
        queue: str = "default",
        priority: int = 100,
        run_after: datetime | None = None,
        capability: str | None = None,
        uid: int | None = None,
        run_group: int | None = None,
        waitfor_job: int | None = None,
        waitfor_group: int | None = None,
        deadline_key: str | None = None,
        admin_data: dict[str, Any] | None = None,
        save_result: bool = True,
        use_result_from: int | None = None,
        retry_strategy: str = "exponential",
        max_retries: int = 10,
        initial_retry_delay: int = 1,
        max_retry_delay: int = 3600,
        timeout_seconds: int | None = None,
        on_timeout: str = "retry",
        **kwargs: Any,
    ) -> list[Any]:
        """Validate enqueue options and build the parameter row for
        _ENQUEUE_SQL — the single construction path shared by enqueue()
        and enqueue_in_transaction()."""
        if waitfor_job and waitfor_group:
            raise ValueError("Cannot specify both waitfor_job and waitfor_group")

        # Default run_after to now if not specified
        if run_after is None:
            run_after = datetime.now(UTC)

        # Determine initial state
        state = "waiting" if waitfor_job or waitfor_group else "queued"

        # Build admin_data (copy so we never mutate the caller's dict)
        admin_data = dict(admin_data) if admin_data else {}

        # Results are saved by default; record only an explicit opt-out
        if not save_result:
            admin_data["save_result"] = False

        # Result passing is resolved by the WORKER at execution time (the
        # upstream job usually hasn't run yet when we enqueue), so only
        # record which job's result to inject.
        if use_result_from:
            admin_data["use_result_from"] = use_result_from

        # Add retry strategy configuration without clobbering any values the
        # caller already put in admin_data explicitly
        admin_data.setdefault("retry_strategy", retry_strategy)
        admin_data.setdefault("max_retries", max_retries)
        admin_data.setdefault("initial_retry_delay", initial_retry_delay)
        admin_data.setdefault("max_retry_delay", max_retry_delay)

        # Add timeout configuration if specified
        if timeout_seconds:
            admin_data["timeout_seconds"] = timeout_seconds
            admin_data.setdefault("on_timeout", on_timeout)

        return [
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
            state,
        ]

    async def enqueue_batch(
        self,
        jobs: list[tuple[str, dict[str, Any]]],
        queue: str = "default",
        priority: int = 100,
        run_after: datetime | None = None,
        run_group: int | None = None,
    ) -> list[int]:
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
            run_after = datetime.now(UTC)

        # Prepare values for batch insert
        values = []
        for job_class, kwargs in jobs:
            values.append(
                (
                    job_class,
                    json.dumps(kwargs),
                    queue,
                    priority,
                    run_after,
                    run_group,
                )
            )

        # Execute batch INSERT
        async with self.pool.acquire() as conn:
            # Use unnest for efficient bulk insert
            job_ids = await conn.fetch(
                """
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

        return [row["id"] for row in job_ids]

    # =========================================================================
    # Job Inspection & Management
    # =========================================================================

    async def get_job(self, job_id: int) -> JobInfo | None:
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
            row = await conn.fetchrow(
                """
                SELECT id, job_class, queue, prio as priority, state, created
                FROM jorb
                WHERE id = $1
            """,
                job_id,
            )

        if not row:
            return None

        return JobInfo(**dict(row))

    async def cancel_job(self, job_id: int) -> str | None:
        """
        Cancel a job wherever it is in its lifecycle.

        Queued/waiting jobs are cancelled immediately. Claimed/running jobs
        get a cancellation request delivered to their worker, which cancels
        the task at its next await point.

        Args:
            job_id: Job ID

        Returns:
            'cancelled' (done now), 'cancel_requested' (running; delivery in
            progress), or None (not found / already terminal)

        Example:
            outcome = await client.cancel_job(12345)
            if outcome:
                print(f"Cancel: {outcome}")
        """
        async with self.pool.acquire() as conn:
            return await db.cancel_job(conn, job_id)

    async def retry_job(self, job_id: int) -> int | None:
        """
        Retry a crashed/cancelled/finished job by requeuing it.

        The job keeps its id (retries reuse the same row; per-attempt
        history lives in jorb_history).

        Args:
            job_id: Job ID to retry

        Returns:
            The job id if requeued, or None if it wasn't retriable

        Example:
            if await client.retry_job(12345):
                print("Job requeued")
        """
        async with self.pool.acquire() as conn:
            requeued = await db.retry_job(conn, job_id)

        return requeued

    # =========================================================================
    # Waiting on jobs (LISTEN/NOTIFY with polling fallback)
    # =========================================================================

    # While LISTENing, a poll every 2s covers the race between the initial
    # state check and the LISTEN registration (and any lost notification).
    _LISTEN_POLL_INTERVAL = 2.0
    # Pool-only clients (no db_params) have no LISTEN connection: poll faster.
    _PURE_POLL_INTERVAL = 0.5

    async def _ensure_listener(self) -> bool:
        """Lazily open the single shared LISTEN connection.

        Returns True when the listener is available, False when the client
        was constructed without db_params (pure-polling mode).
        """
        if self._db_params is None:
            return False
        if self._listener_conn is not None and not self._listener_conn.is_closed():
            return True
        async with self._listener_lock:
            if self._listener_conn is None or self._listener_conn.is_closed():
                if isinstance(self._db_params, str):
                    conn = await db.connect(self._db_params)
                else:
                    conn = await db.connect(**self._db_params)
                await conn.add_listener("jorb_done", self._on_jorb_done)
                await conn.add_listener("jorb_event", self._on_jorb_event)
                self._listener_conn = conn
        return True

    def _on_jorb_done(self, _conn: Any, _pid: int, _channel: str, payload: str) -> None:
        """NOTIFY 'jorb_done' payload: {"id": N, "state": "..."}."""
        with contextlib.suppress(Exception):
            data = json.loads(payload)
            for waiter in self._done_waiters.get(data["id"], ()):
                waiter.set()

    def _on_jorb_event(
        self, _conn: Any, _pid: int, _channel: str, payload: str
    ) -> None:
        """NOTIFY 'jorb_event' payload: {"job_id": N, "key": K}."""
        with contextlib.suppress(Exception):
            data = json.loads(payload)
            for waiter in self._event_waiters.get((data["job_id"], data["key"]), ()):
                waiter.set()

    async def _poll_until(
        self,
        waiters: dict[Any, list[asyncio.Event]],
        key: Any,
        check: Callable[[], Awaitable[Any]],
        timeout: float | None,
        what: str,
    ) -> Any:
        """Run `check` until it returns something other than _PENDING.

        Between checks, wait for a NOTIFY dispatched to `waiters[key]` (with
        a 2s fallback poll), or plain-sleep when no listener is configured.
        The check ALWAYS runs once before any waiting — the condition may
        already hold.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        waiter: asyncio.Event | None = None
        if await self._ensure_listener():
            waiter = asyncio.Event()
            waiters.setdefault(key, []).append(waiter)

        try:
            while True:
                value = await check()
                if value is not _PENDING:
                    return value

                interval = (
                    self._LISTEN_POLL_INTERVAL
                    if waiter is not None
                    else self._PURE_POLL_INTERVAL
                )
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"timed out after {timeout}s waiting for {what}"
                        )
                    interval = min(interval, remaining)

                if waiter is not None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(waiter.wait(), interval)
                    waiter.clear()
                else:
                    await asyncio.sleep(interval)
        finally:
            if waiter is not None:
                entries = waiters.get(key)
                if entries is not None:
                    with contextlib.suppress(ValueError):
                        entries.remove(waiter)
                    if not entries:
                        waiters.pop(key, None)

    async def wait_for_result(self, job_id: int, timeout: float | None = None) -> Any:
        """
        Wait until a job reaches a terminal state and return its result.

        Waits on the shared 'jorb_done' LISTEN connection when the client
        was built with db_params (create()/from_config() do this), with an
        immediate state check first and a 2-second fallback poll to cover
        LISTEN races. Pool-only clients fall back to pure polling.

        Args:
            job_id: Job ID
            timeout: Max seconds to wait (default: wait forever)

        Returns:
            The finished job's result (may be None)

        Raises:
            JobFailedError: job crashed (terminal DLQ); carries error_message
            JobCancelledError: job was cancelled
            JobError: job row does not exist
            TimeoutError: `timeout` elapsed before a terminal state

        Example:
            job_id = await client.enqueue('myapp.jobs.Sum', xs=[1, 2, 3])
            total = await client.wait_for_result(job_id, timeout=60)
        """

        async def check() -> Any:
            row = await self.pool.fetchrow(
                "SELECT state, result, error_message FROM jorb WHERE id = $1",
                job_id,
            )
            if row is None:
                raise JobError(f"job {job_id} does not exist")
            state = row["state"]
            if state == "finished":
                return row["result"]
            if state == "crashed":
                raise JobFailedError(job_id, row["error_message"])
            if state == "cancelled":
                raise JobCancelledError(job_id)
            return _PENDING

        return await self._poll_until(
            self._done_waiters, job_id, check, timeout, f"job {job_id} to finish"
        )

    async def get_event(
        self, job_id: int, key: str, timeout: float | None = None
    ) -> Any:
        """
        Return the value of a job's published event, waiting until it exists.

        Jobs publish events with `await self.set_event(key, value)`; this is
        the client-side reader. Waits on the shared 'jorb_event' LISTEN
        connection (same connection as wait_for_result) with an immediate
        fetch first and a 2-second fallback poll; pool-only clients poll.

        Args:
            job_id: Publishing job's ID
            key: Event key
            timeout: Max seconds to wait (default: wait forever)

        Returns:
            The event's value (JSON-decoded)

        Raises:
            TimeoutError: `timeout` elapsed before the event was published

        Example:
            phase = await client.get_event(job_id, 'phase', timeout=30)
        """

        async def check() -> Any:
            row = await self.pool.fetchrow(
                "SELECT value FROM jorb_event WHERE job_id = $1 AND key = $2",
                job_id,
                key,
            )
            return row["value"] if row is not None else _PENDING

        return await self._poll_until(
            self._event_waiters,
            (job_id, key),
            check,
            timeout,
            f"event {key!r} on job {job_id}",
        )

    async def send_message(
        self, dest_job_id: int, message: Any, topic: str | None = None
    ) -> int:
        """
        Send a durable message to a job's mailbox.

        Plain INSERT into jorb_mailbox (the NOTIFY trigger wakes the
        receiving job's `recv()`). External senders are not replayed on
        retry, so no checkpointing is needed on this side.

        Args:
            dest_job_id: Receiving job's ID
            message: JSON-serializable message payload
            topic: Optional topic the receiver filters on

        Returns:
            The mailbox row id

        Example:
            await client.send_message(job_id, {'approve': True}, topic='review')
        """
        async with self.pool.acquire() as conn:
            message_id: int = await conn.fetchval(
                """
                INSERT INTO jorb_mailbox (dest_job_id, topic, message)
                VALUES ($1, $2, $3)
                RETURNING id
            """,
                dest_job_id,
                topic,
                message,
            )
        return message_id

    # =========================================================================
    # Queue Operations
    # =========================================================================

    async def queue_depth(self, queue: str = "default") -> int:
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
            depth: int = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM jorb
                WHERE queue = $1
                  AND state = 'queued'
            """,
                queue,
            )
            return depth

    async def queue_stats(self, queue: str = "default") -> dict[str, int]:
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
            rows = await conn.fetch(
                """
                SELECT state, COUNT(*) as count
                FROM jorb
                WHERE queue = $1
                GROUP BY state
            """,
                queue,
            )

        stats = {row["state"]: row["count"] for row in rows}

        # Ensure all states are present
        for state in [
            "queued",
            "claimed",
            "running",
            "waiting",
            "finished",
            "crashed",
            "cancelled",
        ]:
            stats.setdefault(state, 0)

        return stats

    async def list_queues(self) -> list[dict[str, Any]]:
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

    async def purge_queue(self, queue: str, states: list[str] | None = None) -> int:
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
            states = ["queued", "waiting"]

        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM jorb
                WHERE queue = $1
                  AND state = ANY($2::jorbstate[])
            """,
                queue,
                states,
            )

        # Extract row count from result like "DELETE 42"
        return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    # =========================================================================
    # Extended Job Management
    # =========================================================================

    async def get_job_full(self, job_id: int) -> dict[str, Any] | None:
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
            row = await conn.fetchrow(
                """
                SELECT *
                FROM jorb
                WHERE id = $1
            """,
                job_id,
            )

        if not row:
            return None

        return dict(row)

    async def get_job_result(self, job_id: int) -> Any | None:
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
            row = await conn.fetchrow(
                """
                SELECT result, state
                FROM jorb
                WHERE id = $1
            """,
                job_id,
            )

        if not row or row["state"] != "finished" or not row["result"]:
            return None

        # Result is stored as JSON
        result = row["result"]
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
            result: str = await conn.execute(
                """
                DELETE FROM jorb
                WHERE id = $1
            """,
                job_id,
            )

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
            result: str = await conn.execute(
                """
                UPDATE jorb
                SET prio = $2
                WHERE id = $1
                  AND state IN ('queued', 'waiting')
            """,
                job_id,
                new_priority,
            )

        return result != "UPDATE 0"

    async def get_jobs(
        self,
        queue: str | None = None,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order_by: str = "created",
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
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
        params: list[Any] = []
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
        valid_fields = [
            "id",
            "created",
            "prio",
            "run_after",
            "started",
            "finished",
            "queue",
            "state",
        ]
        if order_by not in valid_fields:
            order_by = "created"

        direction = "ASC" if ascending else "DESC"

        params.extend([limit, offset])

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM jorb
                WHERE {where_sql}
                ORDER BY {order_by} {direction}
                LIMIT ${param_num}
                OFFSET ${param_num + 1}
            """,
                *params,
            )

        return [dict(row) for row in rows]

    async def search_jobs(
        self,
        job_class: str | None = None,
        min_priority: int | None = None,
        max_priority: int | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        uid: int | None = None,
        run_group: int | None = None,
        capability: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
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
        params: list[Any] = []
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
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM jorb
                WHERE {where_sql}
                ORDER BY created DESC
                LIMIT ${param_num}
            """,
                *params,
            )

        return [dict(row) for row in rows]

    async def get_failed_jobs(
        self, queue: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
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
        params: list[Any] = []

        if queue:
            where += " AND queue = $1"
            params.append(queue)
            params.append(limit)
        else:
            params.append(limit)

        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT *
                FROM jorb
                WHERE {where}
                ORDER BY finished DESC
                LIMIT ${len(params)}
            """,
                *params,
            )

        return [dict(row) for row in rows]

    async def get_waiting_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
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
            rows = await conn.fetch(
                """
                SELECT *
                FROM jorb
                WHERE state = 'waiting'
                ORDER BY created DESC
                LIMIT $1
            """,
                limit,
            )

        return [dict(row) for row in rows]

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    async def bulk_cancel(self, job_ids: list[int]) -> int:
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
            result = await conn.execute(
                """
                UPDATE jorb
                SET state = 'cancelled'
                WHERE id = ANY($1::bigint[])
                  AND state IN ('queued', 'waiting')
            """,
                job_ids,
            )

        return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    async def bulk_retry(self, job_ids: list[int]) -> list[int]:
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

        # one shared requeue statement per job (see db.build_requeue_sql):
        # jobs keep their ids across retries
        requeued_ids = []
        async with self.pool.acquire() as conn:
            for job_id in job_ids:
                requeued = await db.retry_job(conn, job_id)
                if requeued is not None:
                    requeued_ids.append(requeued)

        return requeued_ids

    async def bulk_delete(self, job_ids: list[int]) -> int:
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
            result = await conn.execute(
                """
                DELETE FROM jorb
                WHERE id = ANY($1::bigint[])
            """,
                job_ids,
            )

        return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    async def bulk_update_priority(self, job_ids: list[int], new_priority: int) -> int:
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
            result = await conn.execute(
                """
                UPDATE jorb
                SET prio = $2
                WHERE id = ANY($1::bigint[])
                  AND state IN ('queued', 'waiting')
            """,
                job_ids,
                new_priority,
            )

        return int(result.split()[-1]) if result.split()[-1].isdigit() else 0

    # =========================================================================
    # Advanced Features
    # =========================================================================

    async def create_pipeline(
        self,
        steps: list[tuple[str, dict[str, Any]]],
        queue: str = "default",
        priority: int = 100,
    ) -> list[int]:
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
                **kwargs,
            )
            job_ids.append(job_id)
            previous_job = job_id

        return job_ids

    async def create_fan_out(
        self,
        job_class: str,
        items: list[dict[str, Any]],
        queue: str = "default",
        priority: int = 100,
        run_group: int | None = None,
    ) -> tuple[list[int], int]:
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
            jobs, queue=queue, priority=priority, run_group=run_group
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

    def dag(self, name: str | None = None, **common_options: Any) -> DAGBuilder:
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

    async def execute_dag(self, dag: DAGBuilder) -> dict:
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

    async def get_dag_status(self, dag_id: int) -> dict[str, Any]:
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
        stages: list[tuple[str, dict, bool]],
        queue: str = "default",
        priority: int = 100,
        **common_options: Any,
    ) -> list[int]:
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
                **common_options,
            )
            job_ids.append(job_id)
            previous_job = job_id
            previous_saved_result = save_result

        return job_ids


class SyncJobClient:
    """Synchronous facade over JobClient for scripts and cron jobs.

    Owns a private event loop (created in the constructor) and runs each
    call to completion on it, so plain synchronous code can enqueue and
    await jobs without any asyncio plumbing:

        client = SyncJobClient(host='localhost', database='pyjobby',
                               user='app', password='secret')
        try:
            job_id = client.enqueue('myapp.jobs.Report', day='mon')
            result = client.wait_for_result(job_id, timeout=300)
        finally:
            client.close()

    NOT thread-safe (one private loop, no locking) and must not be used
    from async code — use JobClient there. Also usable as a context
    manager (`with SyncJobClient(...) as client:`).
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        min_size: int = 1,
        max_size: int = 4,
        **connect_kwargs: Any,
    ):
        """
        Args:
            dsn: PostgreSQL DSN string, or None to use **connect_kwargs
            min_size: pool minimum size (default: 1)
            max_size: pool maximum size (default: 4)
            **connect_kwargs: asyncpg.connect kwargs (host, port, database,
                user, password, ...) used when no DSN is given
        """
        self._loop = asyncio.new_event_loop()
        self._closed = False
        self._client: JobClient = self._loop.run_until_complete(
            self._create(dsn, connect_kwargs, min_size, max_size)
        )

    @staticmethod
    async def _create(
        dsn: str | None,
        connect_kwargs: dict[str, Any],
        min_size: int,
        max_size: int,
    ) -> JobClient:
        if dsn is not None:
            pool = await db.create_pool(dsn, min_size=min_size, max_size=max_size)
            return JobClient(pool, db_params=dsn)
        pool = await db.create_pool(
            min_size=min_size, max_size=max_size, **connect_kwargs
        )
        return JobClient(pool, db_params=dict(connect_kwargs))

    def _run(self, coro: Awaitable[Any]) -> Any:
        if self._closed:
            raise RuntimeError("SyncJobClient is closed")
        return self._loop.run_until_complete(coro)

    def enqueue(self, job_class: str, **options: Any) -> int:
        """Synchronous JobClient.enqueue()."""
        job_id: int = self._run(self._client.enqueue(job_class, **options))
        return job_id

    def get_job(self, job_id: int) -> JobInfo | None:
        """Synchronous JobClient.get_job()."""
        info: JobInfo | None = self._run(self._client.get_job(job_id))
        return info

    def wait_for_result(self, job_id: int, timeout: float | None = None) -> Any:
        """Synchronous JobClient.wait_for_result()."""
        return self._run(self._client.wait_for_result(job_id, timeout=timeout))

    def cancel_job(self, job_id: int) -> str | None:
        """Synchronous JobClient.cancel_job()."""
        outcome: str | None = self._run(self._client.cancel_job(job_id))
        return outcome

    def retry_job(self, job_id: int) -> int | None:
        """Synchronous JobClient.retry_job()."""
        requeued: int | None = self._run(self._client.retry_job(job_id))
        return requeued

    def get_event(self, job_id: int, key: str, timeout: float | None = None) -> Any:
        """Synchronous JobClient.get_event()."""
        return self._run(self._client.get_event(job_id, key, timeout=timeout))

    def send_message(
        self, dest_job_id: int, message: Any, topic: str | None = None
    ) -> int:
        """Synchronous JobClient.send_message()."""
        message_id: int = self._run(
            self._client.send_message(dest_job_id, message, topic=topic)
        )
        return message_id

    def close(self) -> None:
        """Close the underlying client (pool + listener) and the loop."""
        if not self._closed:
            self._loop.run_until_complete(self._client.close())
            self._loop.close()
            self._closed = True

    def __enter__(self) -> SyncJobClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
