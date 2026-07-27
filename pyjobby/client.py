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
    async with await JobClient.from_config('./pyjobby.conf.py') as client:
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
from typing import TYPE_CHECKING, Any, Final

import asyncpg  # type: ignore[import-untyped]

from . import db, fsm, lifecycle

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

# The states from which no further event will ever be published, so a waiter
# on a value that has not arrived can stop rather than time out. Imported
# rather than restated: `pyjobby.lifecycle` is the declaration, and it has no
# imports of its own, so the client can read it without a cycle.
_TERMINAL_JOB_STATES = frozenset(lifecycle.TERMINAL_STATES)

# The single enqueue INSERT shared by every enqueue path (pool-based
# enqueue(), caller-transaction enqueue_in_transaction(), handles).
_ENQUEUE_SQL = """
    INSERT INTO jorb (
        job_class, kwargs, queue, prio, run_after,
        capability, uid, run_group,
        waitfor_job, waitfor_group,
        deadline_key, admin_data, tags, state
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
    RETURNING id
"""

# What a tag value may be. Tags exist to be FILTERED on, and filtering goes
# through `tags @> '{"key": value}'` against a GIN index, so a value has to be
# something a caller can write down in a query -- and, one layer out, in a
# `pj-admin jobs list --tag key=value` argument. Containment against a nested
# object or an array is a different question with different (surprising)
# semantics, so those are refused at the door instead of being accepted and
# then silently unfilterable.
_TAG_VALUE_TYPES = (str, int, float, bool, type(None))

# What `on_timeout` may say. The worker asks `on_timeout == 'retry'` and
# treats everything else as terminal (pj.py `_handle_failure`), so an
# unrecognized value is not ignored -- it dead-letters the job on its first
# overrun. Checked at enqueue, where the caller is still there to be told.
_ON_TIMEOUT_POLICIES = frozenset({"retry", "fail"})

# The priority ceiling a worker claims under, and the default for `pj
# --max-prio`. `claim_jorb()` takes only jobs whose `prio <= the claiming
# worker's ceiling`, so a job above every live worker's ceiling is never
# claimed, never fails, never reaches the DLQ and never shows up in
# `doctor`: it is simply `queued` forever. The number lives HERE, on the
# enqueue side, because this is the only place a caller can still be told --
# and `pj` imports it for `JobSystem.prio` and its own flag default, so the
# two halves of the contract cannot drift apart.
DEFAULT_PRIO_CEILING: Final = 1000

#: Where `start_machine()` puts a machine unless told otherwise. Machines park
#: on `recv()` waiting for events, so a machine on the default queue is a
#: worker slot held indefinitely against ordinary work. Defaulting them
#: elsewhere makes the safe arrangement the one you get without reading
#: anything; `queue=` overrides it.
DEFAULT_MACHINE_QUEUE: Final = "machines"


def validate_priority(priority: int, ceiling: int = DEFAULT_PRIO_CEILING) -> int:
    """Refuse a priority no worker at `ceiling` could ever claim.

    The ordering is inverted from the intuition -- LOWER is MORE urgent --
    so "low priority, whenever you get to it" is written as a big number by
    everyone who has not read the schema, and a big number is not slow: it
    is *unclaimable*, permanently, with no signal anywhere.

    This is deliberately checked against a number the client was TOLD rather
    than one it can observe: the ceiling belongs to the worker fleet
    (``pj --max-prio``) and nothing about it is visible from a connection.
    A deployment that raises it says so once, when it builds the client
    (``JobClient(pool, prio_ceiling=N)``), which is where deployment facts
    already live. The asymmetry is what settles it: a wrong refusal is loud,
    immediate and a one-line fix at the call site, while a wrong acceptance
    is a job that is silently never run.
    """
    if priority > ceiling:
        raise ValueError(
            f"priority {priority} is above the worker priority ceiling "
            f"({ceiling}): workers claim only jobs with prio <= their "
            f"ceiling, so this job would sit 'queued' forever -- no error, "
            f"no retry, no DLQ. LOWER numbers are MORE urgent, so "
            f"least-urgent work wants a priority just UNDER the ceiling "
            f"(e.g. {ceiling - 100}), not a large one. If this deployment "
            f"really runs its workers with `pj --max-prio {priority}` (or "
            f"higher), declare it once: JobClient(pool, "
            f"prio_ceiling={priority})."
        )
    return priority


def validate_tags(tags: dict[str, Any] | None) -> dict[str, Any]:
    """Check caller-supplied tags and return a copy safe to store.

    Copied rather than used in place for the same reason admin_data is: the
    row we build must not be a live view of a dict the caller still holds.
    """
    if not tags:
        return {}
    if not isinstance(tags, dict):
        raise ValueError(f"tags must be a dict, got {type(tags).__name__}")
    for key, value in tags.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"tag keys must be non-empty strings, got {key!r}")
        if not isinstance(value, _TAG_VALUE_TYPES):
            raise ValueError(
                f"tag {key!r} has value of type {type(value).__name__}; tag "
                "values must be a string, number, boolean or None (nested "
                "objects and arrays cannot be filtered with --tag key=value)"
            )
    return dict(tags)


def tags_filter_sql(param: int) -> str:
    """The WHERE fragment for "job carries these tags", built to be INDEXED.

    Two clauses, and neither is optional:

    * `tags @> $n` is containment, which is the operator the GIN index
      supports. The obvious-looking `tags->>'k' = 'v'` is not indexable by
      it and reads the whole table.
    * `tags <> '{}'` looks redundant beside it and is not. `jorb_tags_idx`
      is PARTIAL on that predicate, and PostgreSQL uses a partial index only
      when the query's clauses IMPLY the predicate -- an implication it
      proves syntactically. It cannot derive "these tags are not empty" from
      "these tags contain customer=acme", so a query without this clause is
      still correct and falls back to a sequential scan: measured at 20,000
      rows as a Seq Scan discarding 19,980 of them.

    Shared by JobClient.search_jobs and AdminAPI.list_jobs so the two cannot
    drift into one being indexed and the other not.
    """
    return f"tags <> '{{}}' AND tags @> ${param}"


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


class UnhandledEventError(JobError):
    """An event was refused because the machine's current state has no edge
    for it — raised BEFORE the message is sent.

    This is the whole reason `MachineHandle.send()` checks. Once a message
    reaches the mailbox, the machine's `recv()` consumes it and checkpoints
    the consumption whether or not any transition fires, so an event sent to
    the wrong state is not queued, not deferred and not returned: it is gone.
    In-process FSM libraries can afford to raise on the machine's own thread
    and leave the caller's event intact; a durable mailbox cannot.
    """

    def __init__(self, job_id: int, state: str, event: str, accepted: list[str]):
        self.job_id = job_id
        self.state = state
        self.event = event
        self.accepted = accepted
        super().__init__(
            f"machine {job_id} is in {state!r}, which has no transition for "
            f"{event!r}"
            + (f"; it accepts {accepted}" if accepted else " (a final state)")
        )


@dataclass
class MachineHandle:
    """A durable state machine, driven from outside the worker.

    Everything here is built on the ordinary client API — `enqueue`,
    `send_message`, `get_event` — because a machine *is* an ordinary job.
    What this adds is the vocabulary: it knows the mailbox topic, the payload
    field naming the event, and the reserved state key, so callers do not
    have to hold those three strings correctly at every call site.

    Pass `machine=YourMachineClass` and it can also answer from the
    declaration, locally and without a round trip: which states exist, what
    the diagram is, and — the one that matters — whether an event would be
    accepted right now, checked before the send rather than discovered
    afterwards by its absence.
    """

    id: int
    client: JobClient
    machine: type[Any] | None = None

    @property
    def _state_key(self) -> str:
        return self.machine.state_key if self.machine is not None else fsm.STATE_KEY

    @property
    def _topic(self) -> str:
        return self.machine.topic if self.machine is not None else fsm.EVENT_TOPIC

    async def state(self, timeout: float | None = None) -> str:
        """The machine's current state.

        With `timeout=None` this returns immediately if the state has been
        published and waits forever if it has not — a machine that has been
        enqueued but not yet claimed has no state row at all. Pass a timeout
        to bound that wait.
        """
        published = await self.client.get_event(
            self.id, self._state_key, timeout=timeout
        )
        state = _machine_state_of(published)
        if state is None:
            raise JobError(
                f"job {self.id} published {self._state_key!r} as {published!r}, "
                f"which is not a machine state"
            )
        return state

    async def wait_for_state(self, *states: str, timeout: float | None = None) -> str:
        """Block until the machine is in one of `states`, and return which.

        Waits on a *state*, not on a transition: a caller waiting for
        "shipped" wants to stop when the machine IS shipped, including when it
        got there before this call — which an edge subscription would miss
        forever.

        The predicate goes down into the client's notification wait rather
        than being checked in a loop up here. That difference is not
        cosmetic: a loop calling `state()` re-registers demand on every pass,
        and demand registration is an `UPDATE` on the `jorb` row, so a 4 Hz
        waiter would write to the hottest table in the system four times a
        second to ask something a NOTIFY answers for free.
        """
        wanted = set(states)
        value = await self.client.wait_for_event(
            self.id,
            self._state_key,
            accept=lambda published: _machine_state_of(published) in wanted,
            timeout=timeout,
        )
        return str(_machine_state_of(value))

    async def may(self, event: str) -> bool:
        """Would `event` be accepted in the machine's current state?

        Requires the declaration (`machine=`); without it there is nothing to
        check against, because the transition table lives in the code rather
        than in a row.
        """
        if self.machine is None:
            raise ValueError(
                "may() needs the machine class: MachineHandle(..., machine=Order)"
            )
        return bool(self.machine.may(await self.state(), event))

    async def send(self, event: str, *, check: bool = True, **payload: Any) -> int:
        """Deliver a transition event, refusing one the current state drops.

        `check` is on by default and needs the declaration; it costs one read
        of the state event. Turn it off for a machine you do not hold the
        class for, or when racing the machine deliberately — but understand
        what you are turning off: an unaccepted event is consumed and
        discarded, so without the check a typo in an event name is silent.
        """
        if check and self.machine is not None:
            current = await self.state()
            if not self.machine.may(current, event):
                raise UnhandledEventError(
                    self.id,
                    current,
                    event,
                    sorted(self.machine.edges.get(current, {})),
                )
        return await self.client.send_message(
            self.id, {fsm.EVENT_FIELD: event, **payload}, topic=self._topic
        )

    async def history(self) -> list[dict[str, Any]]:
        """The machine's own transition log, oldest first.

        Read from `jorb_step`, not `jorb_history`: the latter records the
        JOB's lifecycle (claimed, running, queued...), which for a machine is
        mostly the wake/sleep cycle. The transitions are the checkpointed
        actions, named `source--event->target` by the loop.

        Compaction discards steps once they can no longer be replayed, so this
        is the log of the CURRENT turn, not of all time. A machine that needs
        a permanent audit trail should publish one — as its own events, or in
        its own table from inside a `transaction()`.
        """
        return await self.client.get_steps(self.id)

    async def result(self, timeout: float | None = None) -> Any:
        """Wait for the machine to reach a final state and return its result."""
        return await self.client.wait_for_result(self.id, timeout=timeout)

    async def cancel(self) -> str | None:
        """Stop the machine wherever it is. Its last state stays published."""
        return await self.client.cancel_job(self.id)

    def diagram(self) -> str:
        """The declaration as Mermaid. Local, needs no database."""
        if self.machine is None:
            raise ValueError(
                "diagram() needs the machine class: MachineHandle(..., machine=Order)"
            )
        return str(self.machine.to_mermaid())


def _machine_state_of(published: Any) -> str | None:
    """The state name out of a published `machine.state` value.

    Tolerant on purpose: the key is reserved but writable, so a caller may
    find something that is not the `{"state": ...}` shape a machine writes.
    Returning None makes that a state no predicate matches, rather than a
    `TypeError` from inside a notification callback where it would be hard to
    attribute.
    """
    if isinstance(published, dict):
        state = published.get("state")
        return None if state is None else str(state)
    return None


@dataclass
class SyncMachine:
    """Blocking mirror of `MachineHandle`, for scripts and cron jobs.

    Written out rather than generated, because a synchronous API whose
    methods only exist at runtime is one no editor can complete and no type
    checker can check — which defeats the point of having a declaration in
    the first place.
    """

    handle: MachineHandle
    _run: Callable[[Awaitable[Any]], Any]

    @property
    def id(self) -> int:
        return self.handle.id

    def state(self, timeout: float | None = None) -> str:
        """Blocking MachineHandle.state()."""
        return str(self._run(self.handle.state(timeout=timeout)))

    def wait_for_state(self, *states: str, timeout: float | None = None) -> str:
        """Blocking MachineHandle.wait_for_state()."""
        return str(self._run(self.handle.wait_for_state(*states, timeout=timeout)))

    def may(self, event: str) -> bool:
        """Blocking MachineHandle.may()."""
        return bool(self._run(self.handle.may(event)))

    def send(self, event: str, *, check: bool = True, **payload: Any) -> int:
        """Blocking MachineHandle.send()."""
        return int(self._run(self.handle.send(event, check=check, **payload)))

    def history(self) -> list[dict[str, Any]]:
        """Blocking MachineHandle.history()."""
        rows: list[dict[str, Any]] = self._run(self.handle.history())
        return rows

    def result(self, timeout: float | None = None) -> Any:
        """Blocking MachineHandle.result()."""
        return self._run(self.handle.result(timeout=timeout))

    def cancel(self) -> str | None:
        """Blocking MachineHandle.cancel()."""
        state: str | None = self._run(self.handle.cancel())
        return state

    def diagram(self) -> str:
        """The declaration as Mermaid. Local, needs no database or loop."""
        return self.handle.diagram()


class JobClient:
    """
    High-level client for Pyjobby job queue.

    Provides a clean interface for job submission and management with
    connection pooling, type hints, and support for all pyjobby features.

    Usage:
        # Context manager (recommended)
        async with await JobClient.from_config('./pyjobby.conf.py') as client:
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
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
    ):
        """
        Initialize client with connection pool.

        Args:
            pool: asyncpg connection pool. It remains the CALLER's: close()
                will not close a pool it did not create (create() and
                from_config() build their own and do close them).
            db_params: optional connection parameters — a dict of
                asyncpg.connect kwargs or a DSN string — used to open the
                shared LISTEN connection that powers wait_for_result() and
                get_event(). When omitted (pool-only construction) those
                methods still work but fall back to pure polling.
            prio_ceiling: the priority ceiling THIS deployment's workers run
                with (`pj --max-prio`, default 1000). Every enqueue and
                priority change through this client is refused above it,
                because a job above the fleet's ceiling is never claimed and
                says so nowhere. Raise it only to match workers you actually
                run at that ceiling.

        Note: Use JobClient.create() or JobClient.from_config() instead
        """
        self.pool = pool
        self.prio_ceiling = prio_ceiling
        self._closed = False
        # A pool handed to the constructor belongs to the CALLER — a web app
        # routinely shares one pool between its ORM and this client, and
        # close() closing it would take the whole process's database access
        # down with one client. The create()/from_config() constructors set
        # this True for the pools they build themselves.
        self._owns_pool = False
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
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
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
            prio_ceiling: this fleet's worker priority ceiling
                (`pj --max-prio`, default 1000); enqueueing above it is
                refused. See JobClient.__init__.
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
        client = cls(pool, db_params=db_params, prio_ceiling=prio_ceiling)
        client._owns_pool = True
        return client

    @classmethod
    async def from_config(
        cls,
        config_path: str,
        min_size: int = 5,
        max_size: int = 20,
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
    ) -> JobClient:
        """
        Create client from pyjobby config file.

        Args:
            config_path: Path to pyjobby.conf.py
            min_size: Minimum pool size (default: 5)
            max_size: Maximum pool size (default: 20)
            prio_ceiling: this fleet's worker priority ceiling
                (`pj --max-prio`, default 1000); enqueueing above it is
                refused. See JobClient.__init__.

        Returns:
            JobClient instance

        Example:
            client = await JobClient.from_config('./pyjobby.conf.py')
        """
        from .configloader import load_config_from_file

        config = load_config_from_file(config_path, keys=["db_params"])
        db_params = config.get("db_params", {})

        pool = await db.create_pool(min_size=min_size, max_size=max_size, **db_params)
        client = cls(pool, db_params=db_params, prio_ceiling=prio_ceiling)
        client._owns_pool = True
        return client

    async def close(self) -> None:
        """Close the shared LISTEN connection (if open), and the pool IF
        this client created it.

        A pool passed to the constructor is the caller's: it may be shared
        with the rest of their application, so closing it here would take
        that application's database access down with one client. Pools built
        by create()/from_config() are this client's own and are closed.

        Holds the listener lock so a wait starting concurrently cannot open a
        replacement listener that nothing would ever close.
        """
        if not self._closed:
            self._closed = True
            async with self._listener_lock:
                if self._listener_conn is not None:
                    with contextlib.suppress(Exception):
                        await self._listener_conn.close()
                    self._listener_conn = None
            if self._owns_pool:
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
        tags: dict[str, Any] | None = None,
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
        prio_ceiling: int | None = None,
        **kwargs: Any,
    ) -> int:
        """
        Enqueue a job.

        Args:
            job_class: Python class path (e.g., 'myapp.jobs.SendEmail')
            queue: Queue name (default: 'default')
            priority: Priority — LOWER numbers are more urgent; workers only
                claim jobs with priority <= their own ceiling (default: 100).
                Above the ceiling this client was built with (see
                prio_ceiling) the enqueue is REFUSED rather than accepted
                into a job nothing would ever claim.
            prio_ceiling: override this client's ceiling for this one call
                (default: the client's, itself defaulting to 1000)
            run_after: When to run (default: now)
            capability: Required worker capability (default: None)
            uid: User/tenant ID (default: None)
            run_group: Group ID for pipeline tracking (default: None)
            waitfor_job: Wait for this job ID to complete (default: None)
            waitfor_group: Wait for all jobs in this group (default: None)
            deadline_key: Idempotency key (default: None)
            admin_data: Metadata dict (default: None)
            tags: The caller's OWN labels — customer, tenant, region, batch —
                as a flat dict of string keys to scalar values, filterable
                later via search_jobs(tags=...) / `pj-admin jobs list --tag`.
                Distinct from admin_data, which is the platform's execution
                config (retries, timeouts) and is not indexed (default: None)
            save_result: Store job result in database (default: True; pass
                False to discard results of large/uninteresting jobs)
            use_result_from: Inject the (run-time) result of this job ID into
                this job's kwargs as 'upstream_result' when it executes.
                Combine with waitfor_job so the upstream has finished first.
            retry_strategy: 'exponential', 'linear', 'fibonacci', 'fixed' (Phase 2)
            max_retries: Maximum retry attempts (Phase 2, default: 10)
            initial_retry_delay: Starting retry delay in seconds (Phase 2, default: 1)
            max_retry_delay: Maximum retry delay cap (Phase 2, default: 3600)
            timeout_seconds: This job's deadline in seconds, overriding the
                job class's `timeout` attribute and the worker's
                --default-timeout. 0 means "no deadline at all"; None (the
                default) defers to the class, then the worker.
            on_timeout: What a blown deadline means — 'retry' (default: spend
                the retry budget) or 'fail' (terminal on the first overrun).
                Applies to WHICHEVER deadline binds: timeout_seconds above,
                the job class's `timeout`, or the worker default. Any other
                value raises ValueError.
            **kwargs: Job arguments (passed to job class)

        Returns:
            Job ID

        Raises:
            asyncpg.UniqueViolationError: If deadline_key already exists
            ValueError: If both waitfor_job and waitfor_group specified, if
                on_timeout is neither 'retry' nor 'fail', if priority is
                above this client's worker priority ceiling, or if tags are
                not a flat dict of string keys to scalar values

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
                tags=tags,
                save_result=save_result,
                use_result_from=use_result_from,
                retry_strategy=retry_strategy,
                max_retries=max_retries,
                initial_retry_delay=initial_retry_delay,
                max_retry_delay=max_retry_delay,
                timeout_seconds=timeout_seconds,
                on_timeout=on_timeout,
                prio_ceiling=(
                    self.prio_ceiling if prio_ceiling is None else prio_ceiling
                ),
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

    async def start_machine(
        self, machine: type[Any] | str, **options: Any
    ) -> MachineHandle:
        """Start a durable state machine and return a handle to drive it.

        `machine` is the class itself when the caller can import it — which is
        the better way round, because the handle can then check events against
        the declaration before sending them. A dotted string works too, for a
        caller that only knows the name.

        Machines default to their own queue for the reason in
        `pyjobby.statemachine`: they park on `recv()` waiting for events, and
        a worker parked on a machine is a worker not running ordinary jobs.
        Pass `queue=` to override.

        Example:
            from myapp.orders import Order

            order = await client.start_machine(Order, kwargs={'customer': 42})
            await order.send('paid', amount=100)
            await order.wait_for_state('shipped', timeout=300)
        """
        if isinstance(machine, str):
            job_class, declaration = machine, None
        else:
            job_class = f"{machine.__module__}.{machine.__qualname__}"
            declaration = machine
        options.setdefault("queue", DEFAULT_MACHINE_QUEUE)
        job_id = await self.enqueue(job_class, **options)
        return MachineHandle(id=job_id, client=self, machine=declaration)

    def machine(self, job_id: int, machine: type[Any] | None = None) -> MachineHandle:
        """A handle for a machine that is already running.

        Cheap and synchronous: a handle is an id, a client and an optional
        declaration. Nothing is read until a method is called.
        """
        return MachineHandle(id=job_id, client=self, machine=machine)

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

        Being static, there is no client here holding this deployment's
        declared worker priority ceiling, so `priority` is checked against
        the platform default (see validate_priority); a fleet running a
        raised ceiling passes `prio_ceiling=` with the call.

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
        tags: dict[str, Any] | None = None,
        save_result: bool = True,
        use_result_from: int | None = None,
        retry_strategy: str = "exponential",
        max_retries: int = 10,
        initial_retry_delay: int = 1,
        max_retry_delay: int = 3600,
        timeout_seconds: int | None = None,
        on_timeout: str = "retry",
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
        job_kwargs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """Validate enqueue options and build the parameter row for
        _ENQUEUE_SQL — the single construction path shared by enqueue(),
        enqueue_batch() and enqueue_in_transaction().

        The job's payload arrives one of two ways: as the leftover **kwargs
        (enqueue()'s historical shared namespace), or explicitly as
        ``job_kwargs`` — which keeps payload and options in separate
        namespaces, so a payload key named like an option is delivered
        instead of colliding. When ``job_kwargs`` is given, leftover
        **kwargs can only be misspelled options and are refused by name.

        ``prio_ceiling`` is the fleet's worker ceiling; enqueue() passes the
        client's, and the static/outbox path (which has no client) gets the
        platform default. See validate_priority."""
        if job_kwargs is not None and kwargs:
            raise ValueError(
                f"unknown enqueue options: {sorted(kwargs)} — with a "
                f"kwargs dict provided, job arguments go in it and options "
                f"are passed by name"
            )
        if waitfor_job and waitfor_group:
            raise ValueError("Cannot specify both waitfor_job and waitfor_group")

        validate_priority(priority, prio_ceiling)

        if on_timeout not in _ON_TIMEOUT_POLICIES:
            raise ValueError(
                f"on_timeout must be one of {sorted(_ON_TIMEOUT_POLICIES)}, "
                f"got {on_timeout!r} — the worker treats anything that is not "
                f"'retry' as 'fail', so a typo dead-letters silently"
            )

        # Default run_after to now if not specified
        if run_after is None:
            run_after = datetime.now(UTC)

        # Determine initial state
        state = "waiting" if waitfor_job or waitfor_group else "queued"

        # Build admin_data (copy so we never mutate the caller's dict)
        admin_data = dict(admin_data) if admin_data else {}

        # The caller's own labels stay in their own column: admin_data below
        # is about to be filled with retry/timeout bookkeeping nobody filters
        # on, and mixing the two is what makes the index unaffordable.
        job_tags = validate_tags(tags)

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

        # A deadline supplied HERE, overriding the job class and the worker
        # default. `0` is a real value ("no deadline, whatever the class or
        # the worker says"), so the test is against None, not truthiness.
        if timeout_seconds is not None:
            admin_data["timeout_seconds"] = timeout_seconds

        # The policy is about ANY deadline, not just one passed above: the
        # job class's `timeout` attribute and the worker's --default-timeout
        # are equally deadlines, and neither is visible from here. Recording
        # it only alongside timeout_seconds silently turned `on_timeout=
        # 'fail'` into a retry for the other two. setdefault so an explicit
        # admin_data entry still wins, as with every retry knob above.
        admin_data.setdefault("on_timeout", on_timeout)

        return [
            job_class,
            kwargs if job_kwargs is None else job_kwargs,  # codec converts
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
            job_tags,  # Dict - custom codec handles conversion
            state,
        ]

    async def enqueue_batch(
        self,
        jobs: list[tuple[Any, ...]],
        prio_ceiling: int | None = None,
        **options: Any,
    ) -> list[int]:
        """
        Enqueue multiple jobs in one INSERT, with the SAME option set as
        enqueue().

        Every row is built by the same construction path as a single
        enqueue, so a batch job loses nothing by being batched: retry
        strategy, timeout policy, tags, deadline_key, capability — all of
        it applies. (An earlier version wrote only six columns, so batched
        jobs silently ran with worker-default retry/timeout policy and no
        deadline_key; converting a loop of enqueue() calls into a batch for
        speed must not change what the jobs mean.)

        Args:
            jobs: a list of ``(job_class, kwargs)`` tuples, or
                ``(job_class, kwargs, per_job_options)`` — the third element
                is a dict of enqueue() options applying to that job only,
                layered over the shared ones. Per-job options are how a
                batch carries per-item ``deadline_key``/``tags``/``uid``.
            prio_ceiling: override this client's worker priority ceiling for
                this call (default: the client's; see validate_priority)
            **options: any enqueue() option (queue, priority, run_after,
                run_group, tags, retry_strategy, timeout_seconds, ...),
                applied to every job in the batch.

        Returns:
            List of job IDs, in the order given

        Raises:
            ValueError: an invalid option — priority above the worker
                ceiling, bad tag shape, unknown on_timeout — reported
                before ANY row is written

        Example:
            # 1000 jobs, each with its own idempotency key
            job_ids = await client.enqueue_batch(
                [
                    ('myapp.jobs.ProcessItem', {'item_id': i},
                     {'deadline_key': f'item:{i}'})
                    for i in range(1000)
                ],
                queue='processing',
                max_retries=5,
            )
        """
        if not jobs:
            return []

        ceiling = self.prio_ceiling if prio_ceiling is None else prio_ceiling
        rows = []
        for item in jobs:
            job_class, kwargs, *rest = item
            per_job = rest[0] if rest else {}
            rows.append(
                self._build_enqueue_row(
                    job_class,
                    prio_ceiling=ceiling,
                    job_kwargs=kwargs,
                    **{**options, **per_job},
                )
            )

        columns = list(zip(*rows, strict=True))
        async with self.pool.acquire() as conn:
            job_ids = await conn.fetch(
                """
                INSERT INTO jorb (
                    job_class, kwargs, queue, prio, run_after,
                    capability, uid, run_group,
                    waitfor_job, waitfor_group,
                    deadline_key, admin_data, tags, state
                )
                SELECT * FROM UNNEST(
                    $1::text[], $2::jsonb[], $3::text[], $4::int[],
                    $5::timestamptz[], $6::text[], $7::bigint[],
                    $8::bigint[], $9::bigint[], $10::bigint[],
                    $11::text[], $12::jsonb[], $13::jsonb[],
                    $14::jorbstate[]
                )
                RETURNING id
            """,
                *columns,
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
        Retry a job that did not succeed (crashed or cancelled).

        A job that already FINISHED is deliberately not retriable — re-running
        successful work repeats its side effects (see db.rerun_job, the
        operator verb for that).

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
        was constructed without db_params (pure-polling mode) or has been
        closed — a closed client must never open new connections.
        """
        if self._db_params is None or self._closed:
            return False
        if self._listener_conn is not None and not self._listener_conn.is_closed():
            return True
        async with self._listener_lock:
            if self._closed:
                return False
            if self._listener_conn is None or self._listener_conn.is_closed():
                if isinstance(self._db_params, str):
                    conn = await db.connect(self._db_params)
                else:
                    conn = await db.connect(**self._db_params)
                try:
                    await conn.add_listener("jorb_done", self._on_jorb_done)
                    await conn.add_listener("jorb_event", self._on_jorb_event)
                except BaseException:
                    # never leak the half-registered connection
                    with contextlib.suppress(Exception):
                        await conn.close()
                    raise
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

    # Registering demand for the gated notification channels. jorb_done and
    # jorb_event are only emitted for a job somebody is waiting on, so
    # waiting means SAYING SO FIRST — this is the client half of the
    # ordering argument written out in sql/schema.sql. `AND NOT awaited`
    # makes every registration after the first a no-op at the server, and
    # the flag is deliberately never withdrawn: it is a per-job latch that
    # dies with the row, not a refcount to leak.
    _REGISTER_DEMAND_SQL = (
        "UPDATE jorb SET awaited = TRUE WHERE id = $1 AND NOT awaited"
    )

    async def _poll_until(
        self,
        waiters: dict[Any, list[asyncio.Event]],
        key: Any,
        check: Callable[[], Awaitable[Any]],
        timeout: float | None,
        what: str,
        job_id: int,
    ) -> Any:
        """Run `check` until it returns something other than _PENDING.

        Between checks, wait for a NOTIFY dispatched to `waiters[key]` (with
        a 2s fallback poll), or plain-sleep when no listener is configured.
        The check ALWAYS runs once before any waiting — the condition may
        already hold.

        Demand for `job_id` is registered before that first check and only
        when a listener exists: a pure-polling client depends on no
        notification, so it asks for none.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        waiter: asyncio.Event | None = None
        if await self._ensure_listener():
            # BEFORE the first check, never after: a terminal state reached
            # between the check and the registration would be one this
            # client is neither told about nor has already seen.
            await self.pool.execute(self._REGISTER_DEMAND_SQL, job_id)
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
            self._done_waiters,
            job_id,
            check,
            timeout,
            f"job {job_id} to finish",
            job_id=job_id,
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
            JobError: the job does not exist, or ended without ever
                publishing this key — in both cases nothing will ever
                publish it, so waiting (the default is forever) only delays
                the same answer

        Example:
            phase = await client.get_event(job_id, 'phase', timeout=30)
        """

        async def check() -> Any:
            # One snapshot for all three answers, so a job cannot look
            # absent or terminal while its event is still readable.
            row = await self.pool.fetchrow(
                """SELECT EXISTS (SELECT 1 FROM jorb_event
                                   WHERE job_id = $1 AND key = $2) AS present,
                          (SELECT value FROM jorb_event
                            WHERE job_id = $1 AND key = $2) AS value,
                          (SELECT state FROM jorb WHERE id = $1) AS job_state""",
                job_id,
                key,
            )
            if row["present"]:
                return row["value"]
            job_state = row["job_state"]
            if job_state is None:
                raise JobError(
                    f"job {job_id} does not exist, so event {key!r} will "
                    f"never be published"
                )
            if job_state in _TERMINAL_JOB_STATES:
                raise JobError(
                    f"job {job_id} ended in {job_state!r} without publishing "
                    f"event {key!r}"
                )
            return _PENDING

        return await self._poll_until(
            self._event_waiters,
            (job_id, key),
            check,
            timeout,
            f"event {key!r} on job {job_id}",
            job_id=job_id,
        )

    async def wait_for_event(
        self,
        job_id: int,
        key: str,
        accept: Callable[[Any], bool] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Wait until a job's event exists *and* its value satisfies `accept`.

        `get_event()` answers "has this key been published yet", which is the
        right question for a key written once. It is the wrong question for one
        written repeatedly — a machine's state, a progress counter — because it
        returns on the first publish and every later caller has to loop.

        Looping outside is expensive in a way that is not obvious: each pass
        through `get_event()` re-registers demand, which is an `UPDATE` on the
        `jorb` row. A 4 Hz caller doing that is writing to the hottest table in
        the system four times a second to ask a question that a NOTIFY would
        have answered. Passing the predicate in keeps the wait inside
        `_poll_until`, where it sleeps on the notification and falls back to a
        2-second poll instead.

        Raises `JobError` if the job reaches a terminal state without ever
        satisfying `accept` — otherwise a caller waiting on a state a crashed
        job will now never reach waits for its whole timeout, or forever.
        Both values are read in ONE query, so they are a consistent snapshot:
        a job cannot appear terminal alongside a stale event.
        """

        async def check() -> Any:
            row = await self.pool.fetchrow(
                """SELECT EXISTS (SELECT 1 FROM jorb_event
                                   WHERE job_id = $1 AND key = $2) AS present,
                          (SELECT value FROM jorb_event
                            WHERE job_id = $1 AND key = $2) AS value,
                          (SELECT state FROM jorb WHERE id = $1) AS job_state""",
                job_id,
                key,
            )
            value = row["value"]
            # Row PRESENCE is what "published" means, not non-null value: a
            # job may legitimately publish None (set_event(key, None)), and
            # a waiter keyed on `value is not None` would starve on an event
            # that was published long ago.
            if row["present"] and (accept is None or accept(value)):
                return value
            job_state = row["job_state"]
            # Terminal without a match: nothing will publish this key again.
            if job_state in _TERMINAL_JOB_STATES:
                raise JobError(
                    f"job {job_id} ended in {job_state!r} without "
                    f"event {key!r} reaching an accepted value "
                    f"(last: {value!r})"
                )
            # No row at all — a bad id, or retention removed the job. Nothing
            # will ever publish, so waiting the full timeout only delays the
            # same answer. The event value comes from the same snapshot, so a
            # job cannot look absent while its event is still readable.
            if job_state is None:
                raise JobError(
                    f"job {job_id} does not exist, so event {key!r} will "
                    f"never be published"
                )
            return _PENDING

        return await self._poll_until(
            self._event_waiters,
            (job_id, key),
            check,
            timeout,
            f"event {key!r} on job {job_id}",
            job_id=job_id,
        )

    async def get_steps(self, job_id: int) -> list[dict[str, Any]]:
        """A job's recorded DXE checkpoints, oldest first.

        Note that `compact()` discards checkpoints a job can no longer replay,
        so for a long-lived job this is the current stretch of work rather than
        its whole history. See `docs/DXE.md`.
        """
        rows = await self.pool.fetch(
            """SELECT step_seq, name, output, error, started, finished
                 FROM jorb_step WHERE job_id = $1 ORDER BY step_seq""",
            job_id,
        )
        return [dict(row) for row in rows]

    async def send_message(
        self, dest_job_id: int, message: Any, topic: str | None = None
    ) -> int:
        """
        Send a durable message to a job's mailbox.

        Plain INSERT into jorb_mailbox; the receiving job's `recv()` polls
        for it (there is no mailbox NOTIFY — see sql/schema.sql). External
        senders are not replayed on retry, so no checkpointing is needed on
        this side.

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
        Get a finished job's stored result without waiting.

        The stored result is whatever the job returned, so it may legitimately
        be falsy (0, False, [], "") or None — those are returned as-is, the
        same values wait_for_result() yields. None therefore means "no result
        to read" only when the job is absent or not finished; use get_job()
        to tell the two apart.

        Args:
            job_id: Job ID

        Returns:
            The job's result, or None if the job does not exist / has not
            finished

        Example:
            result = await client.get_job_result(12345)
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

        if row is None or row["state"] != "finished":
            return None

        # jsonb comes back already decoded (every pyjobby connection registers
        # the JSON codecs), so a string result is the job's string, not JSON
        # text waiting to be parsed a second time.
        return row["result"]

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

        Raises:
            ValueError: If new_priority is above this client's worker
                priority ceiling — the same black hole as enqueueing there,
                reached by a different door (see validate_priority)

        Example:
            # Make job higher priority
            if await client.update_job_priority(12345, 500):
                print("Priority updated")
        """
        validate_priority(new_priority, self.prio_ceiling)

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
        tags: dict[str, Any] | None = None,
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
            tags: Match jobs whose tags CONTAIN all of these pairs; extra
                tags on the job do not disqualify it. Answered by the
                partial GIN index on jorb.tags.
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

        if tags:
            # Containment, not equality, so a caller asking for one tag is
            # not defeated by a job that carries three (see tags_filter_sql).
            where_clauses.append(tags_filter_sql(param_num))
            params.append(validate_tags(tags))
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
        Cancel multiple jobs — cancel_job() applied to each id.

        Claimed/running jobs get a cancellation request delivered to their
        worker exactly as the single-job verb does; only terminal and missing
        jobs are skipped.

        Args:
            job_ids: List of job IDs to cancel

        Returns:
            How many jobs accepted cancellation (cancelled outright or
            cancellation requested)

        Example:
            cancelled = await client.bulk_cancel([123, 456, 789])
            print(f"Cancelled {cancelled} jobs")
        """
        if not job_ids:
            return 0

        async with self.pool.acquire() as conn:
            outcomes = [await db.cancel_job(conn, job_id) for job_id in job_ids]

        return sum(outcome is not None for outcome in outcomes)

    async def bulk_retry(self, job_ids: list[int]) -> list[int]:
        """
        Retry multiple jobs — retry_job() applied to each id.

        Args:
            job_ids: List of job IDs to retry

        Returns:
            The ids that were requeued (jobs keep their id across retries),
            omitting any that were not in a retryable state

        Example:
            requeued = await client.bulk_retry([123, 456, 789])
            print(f"Requeued {len(requeued)} jobs")
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

        Raises:
            ValueError: If new_priority is above this client's worker
                priority ceiling (see validate_priority)

        Example:
            updated = await client.bulk_update_priority([123, 456], 500)
            print(f"Updated {updated} jobs to priority 500")
        """
        if not job_ids:
            return 0

        validate_priority(new_priority, self.prio_ceiling)

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

    async def wait_for_dag(self, dag_id: int, timeout: float | None = None) -> bool:
        """
        Wait for a DAG to reach its outcome.

        Returns True when every job finished, False when a job crashed or
        was cancelled (the DAG cannot complete; get_dag_status() has the
        counts). Raises TimeoutError if `timeout` elapses first — a timeout
        is not an outcome, the DAG is still running — and LookupError for a
        dag_id that does not exist.

        Args:
            dag_id: DAG ID — `dag.dag_id` after `execute()`
            timeout: Maximum wait in seconds (default: wait forever)

        Example:
            dag = client.dag(name='Pipeline')
            # ... build DAG ...
            node_to_job = await dag.execute(client)

            if await client.wait_for_dag(dag.dag_id, timeout=1800):
                print("DAG completed successfully!")
            else:
                status = await client.get_dag_status(dag.dag_id)
                print(f"DAG failed: {status['crashed_jobs']} crashed")
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
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
        **connect_kwargs: Any,
    ):
        """
        Args:
            dsn: PostgreSQL DSN string, or None to use **connect_kwargs
            min_size: pool minimum size (default: 1)
            max_size: pool maximum size (default: 4)
            prio_ceiling: this fleet's worker priority ceiling
                (`pj --max-prio`, default 1000); enqueueing above it is
                refused. Named explicitly rather than left to
                **connect_kwargs, which would hand it to asyncpg.
            **connect_kwargs: asyncpg.connect kwargs (host, port, database,
                user, password, ...) used when no DSN is given
        """
        self._loop = asyncio.new_event_loop()
        self._closed = False
        self._client: JobClient = self._loop.run_until_complete(
            self._create(dsn, connect_kwargs, min_size, max_size, prio_ceiling)
        )

    @staticmethod
    async def _create(
        dsn: str | None,
        connect_kwargs: dict[str, Any],
        min_size: int,
        max_size: int,
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
    ) -> JobClient:
        if dsn is not None:
            pool = await db.create_pool(dsn, min_size=min_size, max_size=max_size)
            return JobClient(pool, db_params=dsn, prio_ceiling=prio_ceiling)
        pool = await db.create_pool(
            min_size=min_size, max_size=max_size, **connect_kwargs
        )
        return JobClient(
            pool, db_params=dict(connect_kwargs), prio_ceiling=prio_ceiling
        )

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

    # ---------------------------------------------------------------------
    # State machines
    # ---------------------------------------------------------------------

    def start_machine(self, machine: type[Any] | str, **options: Any) -> SyncMachine:
        """Synchronous JobClient.start_machine()."""
        handle: MachineHandle = self._run(
            self._client.start_machine(machine, **options)
        )
        return SyncMachine(handle, self._run)

    def machine(self, job_id: int, machine: type[Any] | None = None) -> SyncMachine:
        """Synchronous JobClient.machine()."""
        return SyncMachine(self._client.machine(job_id, machine), self._run)

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
