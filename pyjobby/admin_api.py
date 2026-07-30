#!/usr/bin/env python3
"""
Pyjobby Admin API

Clean, well-encapsulated administrative API for managing jobs, queues, and workers.
Designed to be used by both CLI tools and web interfaces.

All methods are async and return structured data (dicts/lists).
"""

from __future__ import annotations

import re
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
from .lifecycle import TERMINAL_STATES, TERMINAL_STATES_SQL
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
# once per DXE checkpoint, and jorb_stream once per streamed VALUE -- the only
# child table whose size an application can drive without adding jobs.
FOOTPRINT_TABLES = ("jorb", "jorb_history", "jorb_step", "jorb_stream")

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


# ============================================================================
# Why a job is not running: the reasons, and what each one maps to
# ============================================================================
# `claim_jorb()` (pyjobby/sql/schema/30_claim.sql) is the ONLY thing that ever
# admits a job, so the set of answers `explain_job` can give is not a design
# choice -- it is fixed by that function. Every way it declines a row gets a
# reason code here, plus the states a row can be in before it is a candidate
# at all. The mapping is written down so the two cannot drift in silence: a
# condition added to claim_jorb with no entry here becomes a job the verb
# cheerfully reports as "claimable now" while nothing claims it, which is the
# exact failure this whole verb exists to end.
#
# TWO WAYS claim_jorb DECLINES THAT ARE DELIBERATELY NOT REASON CODES, because
# neither is a property of the job and neither survives long enough to be
# observed after the fact:
#
#   * `claim_queue_lock()` timing out (50 ms) on a queue that has
#     max_concurrency or rate_limit set. A claimer that loses the lock is
#     behind another claimer, not blocked -- the queue is being drained, just
#     not by that caller this millisecond.
#   * `FOR UPDATE OF j SKIP LOCKED` skipping the head row because a concurrent
#     claimer holds it. Same shape: the row is being claimed, by somebody else.
#
# Both are contention on a queue that IS working, so both land in `claimable`,
# whose details carry `queue_serialised` to say that claims on this queue take
# the lock at all.
EXPLAIN_REASONS: dict[str, str] = {
    # --- states a row is in before claim_jorb's predicate can apply ---
    "finished": "state = 'finished' (terminal; the row is not a candidate)",
    "crashed": "state = 'crashed' (terminal; this IS the dead letter queue)",
    "cancelled": "state = 'cancelled' (terminal; the row is not a candidate)",
    "claimed": "state = 'claimed' -- already admitted, not yet started",
    "running": "state = 'running' -- a worker is executing it right now",
    "waiting_on_job": "state = 'waiting': the predicate wants 'queued'. Woken "
    "only when waitfor_job reaches 'finished'",
    "waiting_on_group": "state = 'waiting': the predicate wants 'queued'. Woken "
    "only when NO member of waitfor_group is unfinished",
    "waiting_unblocked": "state = 'waiting' with neither waitfor_job nor "
    "waitfor_group set -- no completion can ever wake it",
    # --- the row-level predicate inside claim_jorb's SELECT ---
    "deferred": "j.run_after <= now() is false",
    "above_worker_ceiling": "j.prio <= p_max_prio is false for EVERY live "
    "worker's ceiling (jorb_worker.max_prio)",
    "capability_unmet": "j.capability = ANY(p_capabilities) is false for every "
    "live worker on the queue (and j.capability IS NOT NULL)",
    "app_version_unmet": "j.app_version = p_app_version is false for every "
    "live worker on the queue (and j.app_version IS NOT NULL) -- the job is "
    "pinned to a build nobody is running",
    "no_live_workers": "j.queue = p_queue never runs, because nothing on this "
    "queue is calling claim_jorb at all",
    # --- the jorb_queue control plane, checked before the SELECT ---
    "queue_paused": "COALESCE(q.paused, FALSE) -> RETURN",
    # The two counter reasons are SCOPE-AGNOSTIC on purpose. With
    # q.partition_limits the very same control declines the row, only counted
    # over this job's partition_key instead of over the queue -- so it is the
    # same reason with a different denominator, and minting a second code for
    # it would split one condition into two an operator has to learn are the
    # same. Which one applied is in the details (`partition_limits`,
    # `partition_key`) and named in the summary.
    "queue_at_max_concurrency": "q.max_concurrency <= count(claimed+running "
    "on the queue, or on this job's partition_key when q.partition_limits) "
    "-> RETURN (per queue) / skip this lane (per partition)",
    "rate_limited": "q.rate_limit <= count(claimed_at inside "
    "q.rate_period_seconds, per queue or per partition_key as above) "
    "-> RETURN / skip this lane",
    # --- nothing declines it ---
    "claimable": "every condition above passes: the row is admissible now, "
    "and its position in ORDER BY prio, run_after is how soon",
}

#: How far `explain_job` counts into the claim queue ahead of a claimable job.
#: The count is what makes "claimable now" actionable ("behind 40,000 jobs" is
#: a different answer from "next"), but the honest unbounded version is a scan
#: of the whole queue depth, and this verb must stay cheap on a large table.
#: Past the bound the answer is reported as capped rather than as a number.
EXPLAIN_AHEAD_LIMIT = 1000

#: Same bound, for the unfinished members of a `waitfor_group`. A group is as
#: wide as its fan-out, and the operator's question ("is anything still
#: running?") is answered by the first few either way.
EXPLAIN_GROUP_LIMIT = 1000

#: Most distinct capabilities `explain_job` lists as live on a queue. A fleet
#: advertising more than this has an answer that no longer fits on a line.
#: Shared with the app_version arm, which lists the same kind of thing (what
#: the live fleet advertises) and stops fitting on a line at the same size.
EXPLAIN_CAPABILITY_LIMIT = 50

#: How many unclaimable jobs `unclaimable_jobs` counts per queue per cause.
#: Same bound and same reason as :data:`EXPLAIN_AHEAD_LIMIT`: the operator's
#: question is "does this exist, and how big is it", which "1000+" answers as
#: well as an exact number, and the exact number costs a scan of the queue's
#: whole claimable depth. Past the bound the count is reported as capped.
UNCLAIMABLE_SCAN_LIMIT = 1000

#: How many job ids `unclaimable_jobs` names per queue per cause. Examples to
#: paste into `pj-admin jobs why`, not a work list -- the fix is per queue.
UNCLAIMABLE_SAMPLE_LIMIT = 5

#: The reasons `unclaimable_jobs` can report, in the order it reports them.
#: All three are :data:`EXPLAIN_REASONS` keys, and deliberately so: this is the
#: fleet-wide sweep for the conditions `explain_job` answers one job at a
#: time, and an operator who reads "above_worker_ceiling" in a doctor line and
#: then in a `jobs why` answer is reading about the same thing.
#:
#: THE ORDER IS THE DISJOINTNESS RULE, and it is `explain_job`'s order: a job
#: that trips more than one of these is counted under the first, so the two
#: verbs never send an operator to different remedies for the same row.
UNCLAIMABLE_REASONS: Final[tuple[str, ...]] = (
    "above_worker_ceiling",
    "capability_unmet",
    "app_version_unmet",
)


#: Default page size for the per-job trails (get_job_history, get_job_steps).
#: Matches web_admin.MAX_PAGE_LIMIT, which is the bound the HTTP surface
#: enforces on the same two routes.
DEFAULT_HISTORY_LIMIT = 1000

#: Longest dotted path `validate_job_class` accepts. Real module paths are
#: nowhere near this; the cap exists so the column cannot be used as storage.
MAX_JOB_CLASS_LENGTH = 255

#: A dotted path of Python identifiers with at least one dot -- `module.Class`
#: at minimum. Anchored, so no leading or trailing dot, no empty segment
#: (`a..b`), no whitespace, no path separator, no url, no shell.
_JOB_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$")


def validate_job_class(job_class: str) -> str:
    """Refuse a `job_class` that is not shaped like an importable dotted path.

    `jorb_schedule.job_class` is not data: every firing hands it to a worker,
    which imports the module named by everything before the last dot. Import
    runs module-level code -- before, and regardless of, the `issubclass(...,
    Job)` check that follows it. So the column is the input to an import, and
    a column an anonymous HTTP client can write should not reach one shaped
    like anything at all.

    WHAT THIS DOES AND DOES NOT BOUND. It bounds the SHAPE only. Resolving an
    arbitrary dotted path is the deliberate feature -- there is no allowlist
    of modules and there is not going to be one, because the whole point is
    that a deployment names its own job classes. Whoever can create a schedule
    can still name any class importable on the workers' path, and that is the
    documented trust model: the admin surface is as privileged as the workers
    (see docs/ADMIN_TOOLS.md). What the check removes is the class of input
    that was never a job class in the first place -- an empty string, a
    filesystem path, a URL, a 4 KB blob -- so a typo or a probe fails here,
    at the door, with a message, instead of inside an import on a worker.
    """
    # Length before the pattern, so a megabyte of input is refused by a
    # comparison rather than by a regex engine walking all of it.
    if not isinstance(job_class, str) or len(job_class) > MAX_JOB_CLASS_LENGTH:
        raise ValueError(
            f"invalid job_class: an import path is at most "
            f"{MAX_JOB_CLASS_LENGTH} characters"
        )
    if not _JOB_CLASS_RE.match(job_class):
        raise ValueError(
            f"invalid job_class {job_class!r}: a job class is the dotted "
            f"import path of the class the workers run, like "
            f"'myapp.jobs.SendEmail' -- module path, a dot, then the class "
            f"name (identifiers only, at least one dot)"
        )
    return job_class


def _rate(count: int | None, window_seconds: float) -> float:
    """Per-second rate, comparable across window sizes.

    A zero-length window (`since` == now) has no rate to report -- returning
    0.0 beats dividing by ~0 and publishing a meaningless spike.
    """
    if window_seconds <= 0:
        return 0.0
    return (count or 0) / window_seconds


def _iso(value: datetime | None) -> str | None:
    """A timestamp as an ISO string, or None: what a JSON-bound dict wants."""
    return value.isoformat() if value else None


def _span(seconds: float) -> str:
    """A span of time in the coarsest unit that still reads honestly.

    Used only inside the human `summary` lines -- every number a script would
    branch on is in `details`, in seconds, unrounded.
    """
    seconds = abs(seconds)
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.1f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


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
    # the code version this job is PINNED to: only a worker advertising the
    # same one claims it, NULL for unpinned work (see sql/schema/10_jobs.sql)
    app_version: str | None = None
    uid: int | None = None
    # the caller's own labels (customer/tenant/region/batch); '{}' when unset
    tags: dict | None = None
    run_group: int | None = None
    waitfor_job: int | None = None
    waitfor_group: int | None = None
    deadline_key: str | None = None
    # the caller's at-most-once name for this work, unique across every state
    # for as long as the row survives retention (see sql/schema/10_jobs.sql)
    identity_key: str | None = None
    # the caller's name for a burst of enqueues collapsed onto this one row;
    # held only while the row is queued (see sql/schema/10_jobs.sql)
    debounce_key: str | None = None
    # the ceiling collapse may not defer this row past, NULL when the caller
    # accepted unbounded deferral (see sql/schema/10_jobs.sql)
    debounce_deadline: datetime | None = None
    # the caller's fair-share LANE (a tenant, an account), enforced only on a
    # queue with jorb_queue.partition_limits (see sql/schema/10_jobs.sql)
    partition_key: str | None = None
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
    # the job this one was forked from, NULL for every job that is not a
    # fork AND for a fork whose source has since been reaped (the reference
    # is ON DELETE SET NULL — see sql/schema/10_jobs.sql)
    forked_from: int | None = None

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
        """This job as JSON-serialisable data: every datetime as ISO 8601.

        DRIVEN BY THE VALUE'S TYPE, not by a list of which fields are
        timestamps. The list was hand-kept beside a dataclass that gains a
        field whenever ``jorb`` gains a column, and the two drifted the way a
        hand-kept list always does: a new timestamp column serialised as a
        ``datetime`` object, which every JSON response then died on -- at
        runtime, in the endpoint, far from the field that was added.
        ``from_record`` above already refuses to be hand-kept for the same
        reason; this is the other half of it.
        """
        return {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in asdict(self).items()
        }


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
    #: The two limits above are counted PER jorb.partition_key, not per queue
    partition_limits: bool = False

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
        identity_key: str | None = None,
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
            identity_key: The caller's at-most-once key, matched exactly.
                Returns at most one job, in whatever state it reached, since
                a unique index holds the key for the row's whole life; an
                empty result means the identity was never enqueued or its
                job has aged out of retention. Answered by
                `jorb_identity_idx` — equality is strict, so the clause
                implies the index's `identity_key IS NOT NULL` predicate
                without restating it (unlike the tag filter below).
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

        if identity_key is not None:
            where_clauses.append(f"identity_key = ${param_idx}")
            params.append(identity_key)
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

    # =========================================================================
    # Why is this job not running?
    # =========================================================================

    async def explain_job(
        self,
        job_id: int,
        stale_after_seconds: float = DEFAULT_LIVENESS_GRACE_SECONDS,
    ) -> dict[str, Any] | None:
        """Why job `job_id` is not running, as one structured answer.

        The reasons are exactly :data:`EXPLAIN_REASONS` -- every way
        ``claim_jorb()`` declines a row, plus the states a row can be in
        before it is a candidate at all -- so this cannot answer "no idea",
        and it cannot invent a cause the claim path does not have.

        THE ORDER THE CONDITIONS ARE TESTED IN is deliberately NOT
        claim_jorb's. That function checks the cheap queue-wide gates before
        it touches the job table, which is the right order for a statement
        run thousands of times a second and the wrong one for a human asking
        about ONE job: several conditions usually hold at once, and the
        useful answer is the most DURABLE of them. So this walks from the
        facts that will still be true tomorrow to the ones that are true this
        second -- the job's own row, then what an operator switched on
        (paused), then the shape of the fleet (no workers / ceiling /
        capability), and only then the two counters a draining queue moves on
        its own (concurrency, rate). Every condition is still reported; only
        which one gets to be the headline changes.

        Cost: bounded at every step, because a stuck-job question gets asked
        against the largest table in the system. The job is a primary-key
        lookup; the claim-order position counts through
        ``jorb_claim_idx (queue, prio, run_after) WHERE state = 'queued'`` and
        stops at :data:`EXPLAIN_AHEAD_LIMIT`; unfinished group members come
        from ``jorb_group_unfinished_idx`` and stop at
        :data:`EXPLAIN_GROUP_LIMIT`; in-flight and rate counts are the same
        aggregates ``inflight_stats`` and claim_jorb itself use
        (``jorb_inflight_idx``, ``jorb_claimed_at_idx``); everything about
        workers reads ``jorb_worker``, which holds one row per worker process.
        Nothing here scans a job table, and no index exists for this verb.

        Args:
            job_id: the job to explain
            stale_after_seconds: heartbeat age past which a worker is not
                counted as live (default: 60), matching `list_workers`

        Returns:
            ``{job_id, state, queue, job_class, prio, capability, app_version,
            identity_key, run_after, created, updated, reason, summary,
            details}``, or None when no
            such job exists -- absence is the caller's to report, exactly as
            `get_job` leaves it.
        """
        # now() once, in the database, for every span this answer reports:
        # the row's own age and the claim path's clock have to agree, and the
        # caller's wall clock is not that clock.
        row = await self.conn.fetchrow(
            """
            SELECT j.id, j.state::text AS state, j.queue, j.job_class, j.prio,
                   j.capability, j.app_version, j.run_after, j.created,
                   j.updated,
                   j.started, j.finished, j.claimed_at, j.timeout_at,
                   j.claimed_by, j.worker_host, j.worker_pid,
                   j.run_count, j.error_count, j.error_message,
                   j.cancel_requested, j.waitfor_job, j.waitfor_group,
                   j.identity_key, j.debounce_key, j.debounce_deadline,
                   j.partition_key,
                   EXTRACT(EPOCH FROM (j.run_after - now()))::float8
                       AS run_after_in_seconds,
                   EXTRACT(EPOCH FROM (now() - j.updated))::float8
                       AS in_state_seconds,
                   EXTRACT(EPOCH FROM (j.timeout_at - now()))::float8
                       AS timeout_in_seconds
              FROM jorb j
             WHERE j.id = $1
            """,
            job_id,
        )
        if row is None:
            return None

        state: str = row["state"]
        if state in TERMINAL_STATES:
            reason, summary, details = self._explain_terminal(row)
        elif state in ("claimed", "running"):
            reason, summary, details = await self._explain_inflight(
                row, stale_after_seconds
            )
        elif state == "waiting":
            reason, summary, details = await self._explain_waiting(row)
        else:
            reason, summary, details = await self._explain_queued(
                row, stale_after_seconds
            )

        return {
            "job_id": row["id"],
            "state": state,
            "queue": row["queue"],
            "job_class": row["job_class"],
            "prio": row["prio"],
            "capability": row["capability"],
            # Reported for every answer, not only the app_version_unmet one:
            # a pin is invisible in every other view of a job, and "this job
            # is pinned to a build" changes how an operator reads a `deferred`
            # or `claimable` answer too -- the next deploy may strand it.
            "app_version": row["app_version"],
            # Reported because it changes what the operator should DO about
            # the answer: a job holding an identity_key cannot be replaced by
            # enqueueing the same work again -- that call returns this very
            # job -- so "just re-submit it" is not available until this row
            # is gone. NULL for every job that was enqueued without one.
            "identity_key": row["identity_key"],
            "run_after": _iso(row["run_after"]),
            "created": _iso(row["created"]),
            "updated": _iso(row["updated"]),
            "reason": reason,
            "summary": summary,
            "details": details,
        }

    @staticmethod
    def _explain_terminal(row: asyncpg.Record) -> tuple[str, str, dict[str, Any]]:
        """A job that is not running because it is over."""
        # `finished` is the timestamp of the terminal write; `updated` is the
        # fallback for a row whose terminal transition predates it being set.
        when = _iso(row["finished"]) or _iso(row["updated"])
        details: dict[str, Any] = {
            "terminal_at": when,
            "run_count": row["run_count"],
            "error_count": row["error_count"],
        }
        state: str = row["state"]
        if state == "crashed":
            details["error_message"] = row["error_message"]
            return (
                "crashed",
                f"Crashed at {when} after {row['run_count']} attempt(s) and is "
                f"in the dead letter queue"
                + (f": {row['error_message']}" if row["error_message"] else "")
                + ". Requeue it with `pj-admin jobs retry`.",
                details,
            )
        if state == "cancelled":
            return (
                "cancelled",
                f"Cancelled at {when}. Requeue it with `pj-admin jobs retry`.",
                details,
            )
        return (
            "finished",
            f"Finished successfully at {when} after {row['run_count']} "
            f"attempt(s) — there is nothing left to run. `pj-admin jobs rerun` "
            f"runs it again (and repeats its side effects).",
            details,
        )

    async def _explain_inflight(
        self, row: asyncpg.Record, stale_after_seconds: float
    ) -> tuple[str, str, dict[str, Any]]:
        """A job that IS running, or has been admitted and is about to.

        Whether its worker is still alive is the whole question for a job
        that has sat in `claimed` for an hour, so the registry row is read
        (one primary-key lookup) rather than left for the operator to chase
        through `workers list`.
        """
        worker = None
        if row["claimed_by"] is not None:
            worker = await self.conn.fetchrow(
                """
                SELECT (shutdown_at IS NULL
                        AND last_seen > now() - make_interval(secs => $2))
                           AS live,
                       EXTRACT(EPOCH FROM (now() - last_seen))::float8
                           AS last_seen_age_seconds
                  FROM jorb_worker
                 WHERE id = $1
                """,
                row["claimed_by"],
                stale_after_seconds,
            )

        held = float(row["in_state_seconds"] or 0.0)
        details: dict[str, Any] = {
            "worker_id": row["claimed_by"],
            "worker_host": row["worker_host"],
            "worker_pid": row["worker_pid"],
            "worker_live": worker["live"] if worker else None,
            "worker_last_seen_age_seconds": (
                worker["last_seen_age_seconds"] if worker else None
            ),
            "claimed_at": _iso(row["claimed_at"]),
            "started": _iso(row["started"]),
            "timeout_at": _iso(row["timeout_at"]),
            "timeout_in_seconds": row["timeout_in_seconds"],
            "in_state_seconds": held,
        }

        who = (
            f"worker {row['claimed_by']} ({row['worker_host']}:{row['worker_pid']})"
            if row["claimed_by"] is not None
            else f"an UNREGISTERED worker ({row['worker_host']}:{row['worker_pid']})"
        )
        # A registered worker the registry no longer calls live is the answer
        # to "claimed an hour ago and nothing happened": the monitor requeues
        # this job once the grace expires, and saying so beats an operator
        # deciding the platform lost it.
        dead = worker is not None and not worker["live"]
        deadline = (
            f", deadline {_iso(row['timeout_at'])}"
            f" ({_span(row['timeout_in_seconds'])}"
            f"{' ago' if (row['timeout_in_seconds'] or 0) < 0 else ' away'})"
            if row["timeout_at"]
            else ", no deadline"
        )

        state: str = row["state"]
        if state == "running":
            summary = (
                f"Running on {who} since {_iso(row['started'])} "
                f"({_span(held)} in this state){deadline}."
            )
        else:
            summary = (
                f"Claimed by {who} at {_iso(row['claimed_at'])} "
                f"({_span(held)} ago) and has not started executing yet"
                f"{deadline}."
            )
        if dead:
            summary += (
                " That worker is NOT heartbeating: the monitor requeues its "
                "in-flight jobs once --liveness-grace expires."
            )
        return (state, summary, details)

    async def _explain_waiting(
        self, row: asyncpg.Record
    ) -> tuple[str, str, dict[str, Any]]:
        """A job parked on a dependency.

        A `waiting` row is woken by the completion path of what it waits on
        (pj.py's `enqueue-next-self-finished` / `-if-peer-group-is-finished`),
        and BOTH wakes fire only on 'finished'. So an upstream that crashed or
        was cancelled leaves this job parked forever, which is the thing worth
        saying out loud rather than leaving as "waiting".
        """
        if row["waitfor_job"] is not None:
            blocker = await self.conn.fetchrow(
                "SELECT state::text AS state, job_class FROM jorb WHERE id = $1",
                row["waitfor_job"],
            )
            blocking_state = blocker["state"] if blocker else None
            details: dict[str, Any] = {
                "blocking_job_id": row["waitfor_job"],
                "blocking_job_state": blocking_state,
                "blocking_job_class": blocker["job_class"] if blocker else None,
                "waiting_seconds": row["in_state_seconds"],
            }
            if blocking_state is None:
                tail = (
                    " — that job NO LONGER EXISTS, so nothing can ever wake "
                    "this one; requeue it with `pj-admin jobs retry`."
                )
            elif blocking_state in ("crashed", "cancelled"):
                tail = (
                    f" — that job is {blocking_state}, and the wake fires only "
                    f"on 'finished', so this one will never start on its own."
                )
            elif blocking_state == "finished":
                tail = (
                    " — that job is already finished, so the wake was missed; "
                    "requeue this job with `pj-admin jobs retry`."
                )
            else:
                tail = f" — that job is {blocking_state}."
            return (
                "waiting_on_job",
                f"Waiting on job {row['waitfor_job']}{tail}",
                details,
            )

        if row["waitfor_group"] is not None:
            # Bounded: the group is as wide as its fan-out, and the answer
            # ("is anything still unfinished?") does not need the exact tail.
            # Served by jorb_group_unfinished_idx, whose predicate this
            # subquery spells out so it can be used at all.
            unfinished = await self.conn.fetchval(
                """
                SELECT count(*) FROM (
                    SELECT 1 FROM jorb
                     WHERE run_group = $1
                       AND run_group IS NOT NULL
                       AND state != 'finished'
                     LIMIT $2
                ) m
                """,
                row["waitfor_group"],
                EXPLAIN_GROUP_LIMIT,
            )
            capped = unfinished >= EXPLAIN_GROUP_LIMIT
            details = {
                "blocking_group": row["waitfor_group"],
                "unfinished_members": unfinished,
                "unfinished_members_capped": capped,
                "waiting_seconds": row["in_state_seconds"],
            }
            return (
                "waiting_on_group",
                f"Waiting on group {row['waitfor_group']}: "
                f"{unfinished}{'+' if capped else ''} member(s) are not "
                f"finished yet. The whole group must reach 'finished' — a "
                f"member that crashes holds this job forever.",
                details,
            )

        return (
            "waiting_unblocked",
            "Waiting on nothing: the row is 'waiting' with neither "
            "waitfor_job nor waitfor_group set, so no completion anywhere can "
            "wake it. Requeue it with `pj-admin jobs retry`.",
            {"waiting_seconds": row["in_state_seconds"]},
        )

    async def _explain_queued(
        self, row: asyncpg.Record, stale_after_seconds: float
    ) -> tuple[str, str, dict[str, Any]]:
        """A row claim_jorb could admit — so why has nothing admitted it?

        Everything here is a condition in that function (see
        :data:`EXPLAIN_REASONS`); the order is the one the docstring of
        `explain_job` explains, and each query runs only if the answer has
        not already been found.
        """
        queue = row["queue"]

        if (row["run_after_in_seconds"] or 0.0) > 0:
            wait = float(row["run_after_in_seconds"])
            details: dict[str, Any] = {
                "run_after": _iso(row["run_after"]),
                "seconds_until_run_after": wait,
            }
            summary = (
                f"Deferred: run_after is {_span(wait)} in the future "
                f"({_iso(row['run_after'])}). This is how retry backoff, "
                f"`enqueue_at` and durable sleep are implemented — nothing is "
                f"wrong."
            )
            # A debounced row is deferred for a reason the operator can ACT
            # on, unlike backoff: the wait is being pushed out by producers
            # that are still enqueuing, so "it has been queued for ten
            # minutes" is the feature working. Say which key, and say what
            # bounds it — an uncapped window can be deferred indefinitely,
            # and that is the one case where the answer is "look at the
            # producers", not "wait".
            # `run_count == 0` is jorb_debounce_idx's predicate: a retried
            # debounced job is queued and deferred by BACKOFF, not by a
            # collapse window that closed at its first claim, and telling an
            # operator that producers are pushing it out would be a lie.
            #
            # DEFENCE IN DEPTH now rather than the only thing standing between
            # the operator and that lie: every statement that returns a row to
            # 'queued' NULLs debounce_key with it (db.REQUEUE_CLEARS_KEYS), so
            # a requeued row does not carry the key here to be misread in the
            # first place. Kept because this reads a row, not a statement --
            # a hand-written UPDATE, a row from before that rule, or a fifth
            # requeue path that forgets it all arrive here looking exactly the
            # same, and the cost of the guard is one comparison.
            if row["debounce_key"] and row["run_count"] == 0:
                capped = _iso(row["debounce_deadline"])
                details["debounce_key"] = row["debounce_key"]
                details["debounce_deadline"] = capped or "none"
                summary += (
                    f" This job is DEBOUNCED on key {row['debounce_key']!r}: "
                    f"every duplicate enqueue of that key moves run_after "
                    f"further out and replaces this row's arguments, so it "
                    f"fires once the burst stops"
                )
                summary += (
                    f" — and no later than {capped}, its cap."
                    if capped
                    else ", and nothing caps that — an unbroken stream of "
                    "producers can defer it indefinitely."
                )
            return ("deferred", summary, details)

        control = await self.conn.fetchrow(
            """
            SELECT paused, max_concurrency, rate_limit, rate_period_seconds,
                   partition_limits, updated
              FROM jorb_queue WHERE name = $1
            """,
            queue,
        )
        # No control row is the common case and means unpaused/unlimited.
        paused = bool(control["paused"]) if control else False
        max_concurrency = control["max_concurrency"] if control else None
        rate_limit = control["rate_limit"] if control else None
        rate_period = float(control["rate_period_seconds"]) if control else 60.0
        # The LANE this job is in, and whether its queue counts by lane at
        # all. `partition_limits` on a queue with neither limit set re-scopes
        # nothing, so the two counts below stay queue-wide exactly as they
        # were -- the flag never turns a limit on.
        partitioned = bool(control["partition_limits"]) if control else False
        lane = row["partition_key"]
        lane_name = f"partition {lane!r}" if lane is not None else "the NULL partition"

        if paused:
            return (
                "queue_paused",
                f"Queue {queue!r} is PAUSED: claim_jorb returns without "
                f"looking at any row, for every claimer. Resume it with "
                f"`pj-admin queues resume {queue}`.",
                {
                    "queue": queue,
                    "paused": True,
                    "paused_since": _iso(control["updated"]) if control else None,
                },
            )

        # One pass over the registry, which holds one row per worker process:
        # how many are live on this queue, the highest ceiling among them, and
        # how many of them would accept THIS job's prio, capability and pin.
        fleet = await self.conn.fetchrow(
            """
            SELECT count(*)                                   AS live,
                   max(max_prio)                              AS max_ceiling,
                   count(*) FILTER (WHERE max_prio >= $3)     AS at_prio,
                   count(*) FILTER (
                       WHERE $4::text IS NULL OR $4 = ANY(capabilities)
                   )                                          AS capable,
                   count(*) FILTER (
                       WHERE $5::text IS NULL OR app_version = $5
                   )                                          AS versioned
              FROM jorb_worker
             WHERE queue = $1
               AND shutdown_at IS NULL
               AND last_seen > now() - make_interval(secs => $2)
            """,
            queue,
            stale_after_seconds,
            row["prio"],
            row["capability"],
            row["app_version"],
        )
        live = fleet["live"] or 0

        if live == 0:
            return (
                "no_live_workers",
                f"No live worker is on queue {queue!r} at all, so nothing is "
                f"calling claim_jorb for it. Start one with "
                f"`pj --queue {queue}`, and check `pj-admin workers list` for "
                f"workers that registered and stopped heartbeating.",
                {
                    "queue": queue,
                    "live_workers": 0,
                    "liveness_grace_seconds": stale_after_seconds,
                },
            )

        if fleet["at_prio"] == 0:
            # The platform's quietest failure: the row is perfectly healthy
            # and every live worker is blind to it. Only reachable because
            # jorb_worker.max_prio is published at registration.
            return (
                "above_worker_ceiling",
                f"This job's prio is {row['prio']}, above the ceiling of "
                f"EVERY live worker on {queue!r} (highest is "
                f"{fleet['max_ceiling']}) — claim_jorb admits only "
                f"prio <= the claiming worker's --max-prio, so every one of "
                f"the {live} live worker(s) is blind to it. Lower the job "
                f"(`pj-admin jobs set-priority`) or run a worker with a "
                f"higher --max-prio. Remember lower prio = more urgent.",
                {
                    "prio": row["prio"],
                    "max_live_ceiling": fleet["max_ceiling"],
                    "live_workers": live,
                    "workers_at_or_above_prio": 0,
                },
            )

        if row["capability"] is not None and fleet["capable"] == 0:
            advertised = await self.conn.fetchval(
                """
                SELECT coalesce(array_agg(DISTINCT c ORDER BY c), '{}')
                  FROM (
                    SELECT unnest(capabilities) AS c
                      FROM jorb_worker
                     WHERE queue = $1
                       AND shutdown_at IS NULL
                       AND last_seen > now() - make_interval(secs => $2)
                     LIMIT $3
                  ) s
                """,
                queue,
                stale_after_seconds,
                EXPLAIN_CAPABILITY_LIMIT,
            )
            advertised = list(advertised or [])
            return (
                "capability_unmet",
                f"This job requires capability {row['capability']!r} and none "
                f"of the {live} live worker(s) on {queue!r} advertises it "
                f"(they advertise: "
                f"{', '.join(advertised) if advertised else 'nothing'}). "
                f"Start a worker with `pj --queue {queue} --cap "
                f"{row['capability']}`.",
                {
                    "capability": row["capability"],
                    "live_workers": live,
                    "workers_with_capability": 0,
                    "advertised_capabilities": advertised,
                },
            )

        if row["app_version"] is not None and fleet["versioned"] == 0:
            # Third in the same order the sweep uses, so a job that is both
            # above the ceiling and pinned to a dead build gets the ceiling
            # answer from BOTH verbs (see UNCLAIMABLE_REASONS).
            #
            # The versions the fleet DOES advertise are the actionable half:
            # 'v3, v4' says the deploy moved past this job, and 'nothing' says
            # no worker here was started with --app-version at all -- which
            # are different mistakes with different fixes.
            advertised_versions = await self.conn.fetchval(
                """
                SELECT coalesce(array_agg(DISTINCT v ORDER BY v), '{}')
                  FROM (
                    SELECT app_version AS v
                      FROM jorb_worker
                     WHERE queue = $1
                       AND shutdown_at IS NULL
                       AND last_seen > now() - make_interval(secs => $2)
                       AND app_version IS NOT NULL
                     LIMIT $3
                  ) s
                """,
                queue,
                stale_after_seconds,
                EXPLAIN_CAPABILITY_LIMIT,
            )
            advertised_versions = list(advertised_versions or [])
            return (
                "app_version_unmet",
                f"This job is PINNED to app version {row['app_version']!r} and "
                f"none of the {live} live worker(s) on {queue!r} advertises it "
                f"(they advertise: "
                f"{', '.join(advertised_versions) if advertised_versions else 'nothing'})"
                f" — claim_jorb admits a pinned job only to a worker running "
                f"the SAME version, so it stays queued, never fails and never "
                f"reaches the DLQ. Start a worker with `pj --queue {queue} "
                f"--app-version {row['app_version']}`, or repin the job: "
                f"`pj-admin jobs set-app-version {row['id']} "
                f"[VERSION|--clear]`.",
                {
                    "app_version": row["app_version"],
                    "live_workers": live,
                    "workers_with_app_version": 0,
                    "advertised_app_versions": advertised_versions,
                },
            )

        if max_concurrency is not None:
            # The same count claim_jorb takes under the queue lock -- and on a
            # partitioned queue that count is scoped to THIS JOB'S LANE,
            # because that is the only count that can decline this row. The
            # queue-wide total is irrelevant there and reporting it would send
            # the operator to raise a cap that is not the one binding.
            inflight = await self.conn.fetchval(
                """
                SELECT count(*) FROM jorb
                 WHERE queue = $1 AND state IN ('claimed', 'running')
                   AND (NOT $2 OR partition_key IS NOT DISTINCT FROM $3)
                """,
                queue,
                partitioned,
                lane,
            )
            if max_concurrency <= inflight:
                scope = (
                    f"{lane_name} of queue {queue!r}"
                    if partitioned
                    else (f"Queue {queue!r}")
                )
                return (
                    "queue_at_max_concurrency",
                    f"{scope} is at its concurrency cap: "
                    f"{inflight} job(s) claimed or running against a cap of "
                    f"{max_concurrency}"
                    + (
                        " — this queue counts its limits PER partition_key, so "
                        "other partitions keep draining and only this one is "
                        "held back"
                        if partitioned
                        else ""
                    )
                    + f". This job is admitted as soon as one "
                    f"of them finishes; raise the cap with "
                    f"`pj-admin queues limits {queue} --max-concurrency N`.",
                    {
                        "queue": queue,
                        "max_concurrency": max_concurrency,
                        "inflight": inflight,
                        "partition_limits": partitioned,
                        "partition_key": lane,
                    },
                )

        if rate_limit is not None:
            # Admissions, not starts: claim_jorb counts claimed_at, because
            # `started` is written after the claim commits (see jorb.claimed_at).
            # Per lane on a partitioned queue, for the reason above.
            admissions = await self.conn.fetchval(
                """
                SELECT count(*) FROM jorb
                 WHERE queue = $1
                   AND claimed_at > now() - make_interval(secs => $2)
                   AND (NOT $3 OR partition_key IS NOT DISTINCT FROM $4)
                """,
                queue,
                rate_period,
                partitioned,
                lane,
            )
            if rate_limit <= admissions:
                scope = (
                    f"{lane_name} of queue {queue!r}"
                    if partitioned
                    else (f"Queue {queue!r}")
                )
                return (
                    "rate_limited",
                    f"{scope} is rate limited: {admissions} job(s) "
                    f"admitted in the last {rate_period:g}s against a limit of "
                    f"{rate_limit}"
                    + (
                        " — this queue counts its limits PER partition_key, so "
                        "every other partition has its own window"
                        if partitioned
                        else ""
                    )
                    + ". Claims resume as the window rolls forward; "
                    f"change it with `pj-admin queues limits {queue} "
                    f"--rate-limit N`.",
                    {
                        "queue": queue,
                        "rate_limit": rate_limit,
                        "rate_period_seconds": rate_period,
                        "recent_admissions": admissions,
                        "partition_limits": partitioned,
                        "partition_key": lane,
                    },
                )

        # Nothing declines it. How soon it runs is its position in claim
        # order (ORDER BY prio, run_after), counted through jorb_claim_idx and
        # bounded -- the row comparison is exactly that index's leading
        # columns, and the LIMIT stops the count at a queue depth no operator
        # needs counted exactly.
        #
        # Every claimable row on the queue counts, including ones THIS job's
        # claimer could not take (a capability it lacks, a prio above its
        # ceiling): claim order is a property of the queue, but which rows a
        # given worker may take is a property of that worker, and a number
        # computed for one worker would be wrong for the fleet.
        ahead = await self.conn.fetchval(
            """
            SELECT count(*) FROM (
                SELECT 1 FROM jorb
                 WHERE queue = $1
                   AND state = 'queued'
                   AND run_after <= now()
                   AND (prio, run_after) < ($2, $3)
                 LIMIT $4
            ) q
            """,
            queue,
            row["prio"],
            row["run_after"],
            EXPLAIN_AHEAD_LIMIT,
        )
        capped = ahead >= EXPLAIN_AHEAD_LIMIT
        serialised = max_concurrency is not None or rate_limit is not None
        position = (
            "it is next in claim order"
            if ahead == 0
            else f"{ahead}{'+' if capped else ''} claimable job(s) sort ahead of it"
        )
        return (
            "claimable",
            f"Nothing is blocking it: this job is claimable RIGHT NOW on "
            f"{queue!r} ({live} live worker(s)), and {position}. If it is "
            f"still sitting here, the fleet is behind — check "
            f"`pj-admin metrics` and `pj-admin doctor`.",
            {
                "queue": queue,
                "live_workers": live,
                "jobs_ahead": ahead,
                "jobs_ahead_capped": capped,
                # Claims on a controlled queue serialise on an advisory lock
                # and can also lose a row to SKIP LOCKED. Neither is a reason
                # (see EXPLAIN_REASONS), but both are why a claimable job on
                # such a queue can take a poll or two longer than one here.
                "queue_serialised": serialised,
                # Which lane this row would be admitted into, and whether
                # anything counts by lane -- reported even when nothing is
                # blocking, because "partition_limits is off" is the answer to
                # "why is one tenant still starving the others?".
                "partition_limits": partitioned,
                "partition_key": lane,
            },
        )

    async def unclaimable_jobs(
        self,
        stale_after_seconds: float = DEFAULT_LIVENESS_GRACE_SECONDS,
        scan_limit: int = UNCLAIMABLE_SCAN_LIMIT,
        sample_limit: int = UNCLAIMABLE_SAMPLE_LIMIT,
    ) -> list[dict[str, Any]]:
        """Every job no live worker on its queue could ever claim, per queue.

        `explain_job` answers this for ONE job an operator already suspects.
        This is the sweep: the same conditions, asked of the whole fleet
        at once, so the condition can be FOUND rather than confirmed. It is
        the platform's quietest failure -- a job above every live worker's
        ceiling, wanting a capability none of them advertises, or pinned to an
        app_version none of them runs, stays 'queued' forever. It never fails,
        never retries, never reaches the DLQ, and every other health signal
        (queue depth, worker liveness, throughput) reads normal while it sits
        there.

        THE CAUSES ARE :data:`UNCLAIMABLE_REASONS`, all of them
        :data:`EXPLAIN_REASONS` keys, and they are made DISJOINT here in the
        order that tuple declares -- which is the order `explain_job`
        headlines them: a job that is above the ceiling AND wants an
        unadvertised capability AND is pinned to a dead build is counted only
        under `above_worker_ceiling`. Two verbs that disagreed about which
        cause a job has would send the operator to the wrong remedy.

        A QUEUE WITH NO LIVE WORKERS IS DELIBERATELY NOT REPORTED. It cannot
        be: "no live worker could claim it" is trivially true of every job on
        such a queue, so including them would make this check restate the
        worker check for every idle queue in the install, and drown the
        condition it exists to find. The two are also different remedies --
        "nothing is running" is fixed by starting a worker, "workers are
        running and blind to this work" is fixed by changing what they accept
        -- and the platform already reports the first one twice (doctor's
        `workers` check, and `no_live_workers` from `jobs why`). This verb
        answers the question neither of those can: the fleet is up, and the
        work is still invisible to it. The `fleet` CTE below is what enforces
        it -- a queue with no live rows produces no group, so it never
        reaches the job table at all.

        Cost: bounded, and it never scans the job table. Live workers come
        from ``jorb_worker_live_idx``; each cause is then one LATERAL per
        queue-with-workers over ``jorb_claim_idx (queue, prio, run_after)
        WHERE state = 'queued'``, stopping at `scan_limit` rows. The ceiling
        arm is a pure index range scan (prio > ceiling is that index's second
        column), so it reads only rows it returns. The capability arm has no
        index for its predicate and walks the queue's claimable rows -- still
        strictly less than the per-queue backlog aggregate `doctor` already
        runs beside it, and no index exists or should exist for a column
        almost no job sets. The app_version arm DOES have one
        (``jorb_app_version_idx``, partial on the pinned queued rows), because
        every idle worker asks the same question on a timer and only an
        operator runs this; from there it reads the queue's pinned rows and
        sorts them into claim order for the sample.

        Args:
            stale_after_seconds: heartbeat age past which a worker is not
                counted as live (default: 60), matching `list_workers`
            scan_limit: most jobs counted per queue per cause
            sample_limit: most job ids named per queue per cause

        Returns:
            One record per (queue, cause) with at least one job, ordered by
            queue then cause::

                {queue, reason, count, count_capped, live_workers,
                 sample_job_ids, details}

            `details` carries the numbers the remedy needs, under the same
            keys `explain_job` uses for the same facts. Empty list is the
            healthy answer.
        """
        rows = await self.conn.fetch(
            """
            WITH fleet AS (
                -- One row per queue that HAS live workers, holding the whole
                -- of what those workers will accept: the highest ceiling
                -- among them, the union of their advertised capabilities and
                -- the union of the app versions they advertise.
                -- The LEFT JOIN keeps a worker that advertises nothing.
                SELECT w.queue,
                       count(DISTINCT w.id)                    AS live_workers,
                       max(w.max_prio)                         AS max_ceiling,
                       coalesce(array_agg(DISTINCT caps.cap)
                                    FILTER (WHERE caps.cap IS NOT NULL),
                                '{}'::text[])                  AS advertised,
                       coalesce(array_agg(DISTINCT w.app_version)
                                    FILTER (WHERE w.app_version IS NOT NULL),
                                '{}'::text[])                  AS versions
                  FROM jorb_worker w
                  LEFT JOIN LATERAL unnest(w.capabilities) AS caps(cap)
                       ON TRUE
                 WHERE w.shutdown_at IS NULL
                   AND w.last_seen > now() - make_interval(secs => $1)
                 GROUP BY w.queue
            ),
            blocked AS (
                SELECT f.queue, f.live_workers, f.max_ceiling, f.advertised,
                       f.versions,
                       'above_worker_ceiling'::text AS reason,
                       j.id, j.prio, j.capability, j.app_version
                  FROM fleet f
                 CROSS JOIN LATERAL (
                     SELECT id, prio, capability, app_version
                       FROM jorb
                      WHERE queue = f.queue
                        AND state = 'queued'
                        AND run_after <= now()
                        AND prio > f.max_ceiling
                      ORDER BY prio, run_after
                      LIMIT $2
                 ) j
                UNION ALL
                SELECT f.queue, f.live_workers, f.max_ceiling, f.advertised,
                       f.versions,
                       'capability_unmet'::text,
                       j.id, j.prio, j.capability, j.app_version
                  FROM fleet f
                 CROSS JOIN LATERAL (
                     SELECT id, prio, capability, app_version
                       FROM jorb
                      WHERE queue = f.queue
                        AND state = 'queued'
                        AND run_after <= now()
                        -- disjoint from the arm above, on purpose
                        AND prio <= f.max_ceiling
                        AND capability IS NOT NULL
                        AND NOT (capability = ANY (f.advertised))
                      ORDER BY prio, run_after
                      LIMIT $2
                 ) j
                UNION ALL
                SELECT f.queue, f.live_workers, f.max_ceiling, f.advertised,
                       f.versions,
                       'app_version_unmet'::text,
                       j.id, j.prio, j.capability, j.app_version
                  FROM fleet f
                 CROSS JOIN LATERAL (
                     SELECT id, prio, capability, app_version
                       FROM jorb
                      WHERE queue = f.queue
                        AND state = 'queued'
                        AND run_after <= now()
                        -- disjoint from BOTH arms above: a job that is also
                        -- above the ceiling or wants an unadvertised
                        -- capability is reported there, so the complement of
                        -- each of those predicates is restated here
                        AND prio <= f.max_ceiling
                        AND (capability IS NULL
                             OR capability = ANY (f.advertised))
                        AND app_version IS NOT NULL
                        AND NOT (app_version = ANY (f.versions))
                      ORDER BY prio, run_after
                      LIMIT $2
                 ) j
            )
            SELECT queue, reason, live_workers, max_ceiling, advertised,
                   versions,
                   count(*)::int                              AS blocked_count,
                   (array_agg(id ORDER BY prio, id))[1:$3::int]
                                                              AS sample_job_ids,
                   min(prio)                                  AS lowest_prio,
                   max(prio)                                  AS highest_prio,
                   coalesce(array_agg(DISTINCT capability)
                                FILTER (WHERE capability IS NOT NULL),
                            '{}'::text[])                     AS missing_caps,
                   coalesce(array_agg(DISTINCT app_version)
                                FILTER (WHERE app_version IS NOT NULL),
                            '{}'::text[])                     AS missing_versions
              FROM blocked
             GROUP BY queue, reason, live_workers, max_ceiling, advertised,
                      versions
             -- Ordered by UNCLAIMABLE_REASONS itself, not alphabetically:
             -- that tuple IS the precedence the arms above are disjoint by,
             -- and reporting in a different order than the one the disjointness
             -- rule is written in is how the two drift apart.
             ORDER BY queue, array_position($4::text[], reason)
            """,
            stale_after_seconds,
            scan_limit,
            sample_limit,
            list(UNCLAIMABLE_REASONS),
        )

        report: list[dict[str, Any]] = []
        for row in rows:
            reason: str = row["reason"]
            if reason == "above_worker_ceiling":
                details: dict[str, Any] = {
                    "max_live_ceiling": row["max_ceiling"],
                    "lowest_blocked_prio": row["lowest_prio"],
                    "highest_blocked_prio": row["highest_prio"],
                }
            elif reason == "app_version_unmet":
                # Same keys `explain_job` uses for the same facts, so a doctor
                # line and a `jobs why --json` answer read alike.
                details = {
                    "missing_app_versions": list(row["missing_versions"] or []),
                    "advertised_app_versions": list(row["versions"] or []),
                }
            else:
                details = {
                    "missing_capabilities": list(row["missing_caps"] or []),
                    "advertised_capabilities": list(row["advertised"] or []),
                }
            count: int = row["blocked_count"]
            report.append(
                {
                    "queue": row["queue"],
                    "reason": reason,
                    "count": count,
                    "count_capped": count >= scan_limit,
                    "live_workers": row["live_workers"],
                    "sample_job_ids": list(row["sample_job_ids"] or []),
                    "details": details,
                }
            )
        return report

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
            db.UPDATE_PRIORITY_SQL, job_id, new_priority
        )
        return result != "UPDATE 0"

    async def update_job_app_version(
        self, job_id: int, app_version: str | None
    ) -> bool:
        """Re-pin (or unpin) a job that has not been claimed yet.

        The twin of `update_job_priority`, refusing the same states for the
        same reason (`lifecycle.PRE_CLAIM_STATES`): the version is a claim
        gate, and a job that has already been claimed has already passed
        through it. `None` CLEARS the pin, which is the remedy for a job
        stranded by a deploy that moved on.

        A thin wrapper over `db.update_job_app_version`, which is THE verb --
        the same one `JobClient` calls -- so the two surfaces cannot validate
        differently or guard on different states. Read its docstring for why
        there is no ceiling to check against here.
        """
        return await db.update_job_app_version(self.conn, job_id, app_version)

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
            rate_period_seconds, partition_limits (control fields are
            defaults when no jorb_queue row exists).
        """
        records = await self.conn.fetch("""
            SELECT COALESCE(j.queue, q.name) AS name,
                   COALESCE(q.paused, FALSE) AS paused,
                   q.max_concurrency,
                   q.rate_limit,
                   COALESCE(q.rate_period_seconds, 60) AS rate_period_seconds,
                   COALESCE(q.partition_limits, FALSE) AS partition_limits
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
            stats.partition_limits = c["partition_limits"]

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
        partition_limits: bool | None = None,
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
            partition_limits: Count the two limits above PER
                jorb.partition_key instead of per queue; None leaves it
                alone. It re-scopes the limits and adds none of its own, so
                turning it on for a queue with neither limit set changes
                nothing.

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
                (name, paused, max_concurrency, rate_limit, rate_period_seconds,
                 partition_limits)
            VALUES ($1, COALESCE($2, FALSE), $3, $4, COALESCE($5, 60),
                    COALESCE($8, FALSE))
            ON CONFLICT (name) DO UPDATE SET
                paused = COALESCE($2, jorb_queue.paused),
                max_concurrency = CASE WHEN $6 THEN $3
                                       ELSE jorb_queue.max_concurrency END,
                rate_limit = CASE WHEN $7 THEN $4
                                  ELSE jorb_queue.rate_limit END,
                rate_period_seconds =
                    COALESCE($5, jorb_queue.rate_period_seconds),
                partition_limits = COALESCE($8, jorb_queue.partition_limits),
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
            partition_limits,
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
            SELECT w.id, w.host, w.pid, w.queue, w.capabilities,
                   w.app_version, w.version,
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
        #
        # `scheduled` IS SPLIT OUT OF `queued`, exactly as db.QUEUE_STATS_SQL
        # splits it, because they are the same word everywhere else in the
        # platform and mean opposite things to an operator. A `queued` row with
        # `run_after` in the future is PARKED -- it is retry backoff, an
        # `enqueue_at`, a durable sleep -- and nothing is wrong with it; a
        # `queued` row that is due and unclaimed is BACKLOG, and something may
        # be. Folded together, "40k queued" made a fleet with 40k timers look
        # like a fleet 40k jobs behind, and this endpoint was the only surface
        # that did it: `pj-admin queues`, the web queues table and QUEUE_STATS
        # all reported them apart.
        #
        # In the GROUP BY rather than as a second statement, so the split and
        # the counts come from ONE snapshot -- two statements can disagree by a
        # row that moved between them, and a `queued` count arithmetic'd down
        # by a number from a later snapshot can go negative.
        state_counts = await self.conn.fetch(
            f"""
            SELECT CASE WHEN state = 'queued' AND run_after > now()
                        THEN 'scheduled' ELSE state::text END AS state,
                   COUNT(*) as count
            FROM jorb
            WHERE created >= $1 {queue_sql}
            GROUP BY 1
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

    async def get_job_history(
        self, job_id: int, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[dict[str, Any]]:
        """
        Get the transition trail for a job, oldest first.

        Every state transition is trigger-recorded in jorb_history, so this
        includes per-attempt detail (worker, epoch, errors) across retries
        of the same row.

        Args:
            job_id: Job ID
            limit: Most transitions to return, oldest first (default
                :data:`DEFAULT_HISTORY_LIMIT`). One job's history is not a
                bound: a durable machine parked for a month accumulates
                transitions without limit, and a caller that asks for "the
                history" should not be able to ask for a gigabyte of it by
                accident. Pass a bigger number deliberately.

        Returns:
            List of history dictionaries (at serialized as ISO string)
        """
        records = await self.conn.fetch(
            """
            SELECT id, job_id, at, event, detail
            FROM jorb_history
            WHERE job_id = $1
            ORDER BY id
            LIMIT $2
            """,
            job_id,
            limit,
        )

        history = []
        for r in records:
            data = dict(r)
            data["at"] = data["at"].isoformat()
            history.append(data)
        return history

    async def get_job_steps(
        self, job_id: int, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[dict[str, Any]]:
        """
        Get a job's DXE step checkpoints, ordered by step sequence.

        Args:
            job_id: Job ID
            limit: Most checkpoints to return, in step order (default
                :data:`DEFAULT_HISTORY_LIMIT`). Bounded for the same reason
                as get_job_history: a long-running durable machine writes a
                checkpoint per step, forever.

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
            LIMIT $2
            """,
            job_id,
            limit,
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

        By default the run is fresh: the job's DXE checkpoints AND its durable
        streams are deleted so it actually re-executes from step 1 and streams
        from seq 0 (a position is one past the highest that key holds, so
        keeping the rows would concatenate the two runs into one stream). Pass
        fresh=False to RESUME instead — both are kept, completed steps
        fast-forward and their stream writes append nothing, which is how an
        interrupted durable job is continued. Either way the same row is
        requeued with its error budget reset.

        Args:
            job_id: Job ID
            fresh: True (default) restarts from step 1 with an empty stream;
                False resumes from the recorded checkpoints and streams

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

    async def fork_job(
        self,
        job_id: int,
        *,
        from_step: int = 1,
        queue: str | None = None,
        priority: int | None = None,
        kwargs_override: dict[str, Any] | None = None,
        app_version: str | None = None,
    ) -> dict[str, Any]:
        """
        FORK a job: create a NEW job that re-executes this one's work from
        `from_step`, with steps 1..from_step-1 copied in as checkpoints so
        they fast-forward instead of running again.

        The third verb, and the only one that does not reuse the row:
        `retry` and `rerun` requeue the SAME job (same id, same history),
        while a fork leaves the source completely alone — any state, running
        included — and gives the work a new identity. That is what makes it
        the incident verb: fix the code, fork the crashed job from the step
        that broke, and the expensive prefix is not paid twice.

        Args:
            job_id: the job to fork FROM
            from_step: 1-based step the fork EXECUTES first; 1 (the default)
                copies nothing and re-runs everything under a new id
            queue: run the fork on another queue (default: the source's)
            priority: run the fork at another priority (default: the source's)
            kwargs_override: replace the job's arguments wholesale (default:
                the source's kwargs)
            app_version: pin the FORK to a code version (default: None —
                unpinned, whatever the source was). Not inherited on purpose:
                a fork is how work is re-run under NEW code, so the source's
                pin would strand it (see db.fork_job)

        Returns:
            {"job_id", "source_job_id", "from_step", "steps_copied",
             "queue", "priority"} — job_id is the NEW job

        Raises:
            db.ForkRefused: no such job, from_step below 1, or from_step past
                the source's recorded step count + 1
            ValueError: a priority above this deployment's worker ceiling, or
                an empty/over-long app_version
        """
        if priority is not None:
            # Same guard as enqueue and set-priority, for the same reason: a
            # job above every live worker's ceiling is never claimed, never
            # fails and never shows up anywhere. A fork is enqueued work.
            validate_priority(priority, self.prio_ceiling)
        return await db.fork_job(
            self.conn,
            job_id,
            from_step=from_step,
            queue=queue,
            priority=priority,
            kwargs_override=kwargs_override,
            app_version=app_version,
        )

    async def fork_job_from_failure(
        self,
        job_id: int,
        *,
        queue: str | None = None,
        priority: int | None = None,
        kwargs_override: dict[str, Any] | None = None,
        app_version: str | None = None,
    ) -> dict[str, Any]:
        """
        fork_job() from the first step whose checkpoint recorded an error.

        The common case of the incident verb, so the operator does not have
        to read `jobs steps` and count. Refuses (db.ForkRefused) when no step
        recorded a failure — a job that crashed outside its steps has no
        failing step to start from, and guessing one would fast-forward work
        that never ran.
        """
        if priority is not None:
            validate_priority(priority, self.prio_ceiling)
        return await db.fork_job_from_failure(
            self.conn,
            job_id,
            queue=queue,
            priority=priority,
            kwargs_override=kwargs_override,
            app_version=app_version,
        )

    async def list_forks(
        self, job_id: int, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[int]:
        """
        The ids forked FROM this job, oldest first.

        The other direction of `jorb.forked_from`, which is the direction an
        operator asks in ("did anyone already fork this incident?"). Bounded
        like every other per-job listing, and served by the partial
        `jorb_forked_from_idx` without spelling its predicate out: equality
        is strict, so `forked_from = $1` already implies
        `forked_from IS NOT NULL` (unlike the containment operator that
        forces `tags_filter_sql` to emit both clauses).

        Best-effort, exactly like the column: a fork whose source was reaped
        by retention has its `forked_from` set to NULL and stops being
        listed here, while its own history row keeps the source id.
        """
        return [
            r["id"]
            for r in await self.conn.fetch(
                "SELECT id FROM jorb WHERE forked_from = $1 ORDER BY id LIMIT $2",
                job_id,
                limit,
            )
        ]

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
        backfill_limit: int = 0,
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
            priority: Job priority (default: 100)
            capability: Required worker capability (optional)
            timezone: Schedule timezone (default: 'UTC')
            enabled: Is schedule active? (default: True)
            max_concurrent_jobs: Max jobs running at once (default: 1)
            jitter_seconds: Random delay 0-N seconds (default: 0)
            backfill_limit: How many MISSED ticks a recovering scheduler may
                catch up on (default: 0 -- never backfill, missed ticks are
                skipped). N > 0 fires the N most recent missed ticks and
                records the older excess as one summary skip.
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

        # The workers import this string. Shape-check it at the door -- see
        # validate_job_class for what that does and does not buy.
        validate_job_class(job_class)

        # Reject the expression here rather than at fire time: a schedule
        # that cannot be evaluated is a schedule that silently never runs.
        next_run = next_cron_run(cron_expr, timezone)

        # Create schedule
        record = await self.conn.fetchrow(
            """
            INSERT INTO jorb_schedule (
                name, description, job_class, kwargs, queue, prio, capability,
                cron_expr, timezone, enabled,
                max_concurrent_jobs, jitter_seconds, backfill_limit,
                backpressure_threshold, circuit_breaker_threshold,
                next_run, created_by
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10,
                $11, $12, $13, $14, $15,
                $16, $17
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
            backfill_limit,
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
            "backfill_limit",
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

        # The second door onto jorb_schedule.job_class, and it gets the same
        # shape check as create_schedule: a schedule pointed at a new class by
        # update is just as imported as one created pointing at it.
        if "job_class" in updates:
            validate_job_class(updates["job_class"])

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
