"""Shared database helpers for pyjobby.

Every pyjobby component talks to PostgreSQL through the helpers here so that
connection behavior is uniform everywhere:

- ``json``/``jsonb`` columns always encode/decode via orjson, so Python dicts
  go in and come out of every connection identically (workers, client library,
  CLI, web admin, websocket server, timeout monitor, scheduler).
- Job states are the ``JobState`` enum instead of scattered string literals.
"""

from __future__ import annotations

import contextlib
import datetime
import enum
from collections.abc import AsyncIterator
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import orjson

from . import lifecycle
from .enqueue_rules import validate_app_version


class JobState(enum.StrEnum):
    """All states a job row can be in (mirrors the ``jorbstate`` enum)."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    WAITING = "waiting"
    FINISHED = "finished"
    CRASHED = "crashed"  # terminal: the dead letter queue
    CANCELLED = "cancelled"


# =========================================================================
# Worker liveness
# =========================================================================
# The heartbeat cadence and the staleness threshold judged against it. The
# PAIR lives here, in the module every process imports, because they are one
# agreement between separate programs: the worker writes on one, the monitor
# and every operator surface read on the other, and they are only meaningful
# relative to each other.

#: Seconds between worker registry heartbeats (``pj --heartbeat-interval``).
#: It lives HERE because two different processes have to agree about it: the
#: worker writes ``jorb_worker.last_seen`` on this cadence, and the monitor's
#: ``--liveness-grace`` judges staleness against it. A grace below the
#: heartbeat interval makes every LIVE worker look dead between beats -- the
#: monitor then requeues in-flight jobs from workers that are fine, over and
#: over, and no job longer than the grace can ever finish. The monitor warns
#: at startup when the two are configured into that state.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 10.0

#: Seconds without a heartbeat before a worker counts as dead -- THE liveness
#: threshold's DEFAULT, defined once.
#:
#: A default, and not the answer: the answer is the deployment's, and it is
#: ``liveness_grace_seconds`` in pyjobby.toml (see ``configloader``). This is
#: what every surface falls back to when nothing configured one.
#:
#: The monitor SWEEPS by that number -- it is the monitor that requeues a dead
#: worker's jobs -- and doctor, /metrics, the dashboard and the workers page
#: only REPORT by it. They must agree, because a fleet where the monitor
#: considers a worker alive and every UI calls it dead (or the reverse) gives
#: the operator no true reading anywhere. When this was written out six
#: separate times, raising ``--liveness-grace`` on the monitor left every UI
#: still calling those workers dead; the config key is the fix for the same
#: defect one level up, since a flag on one daemon was never going to reach
#: the other four processes.
DEFAULT_LIVENESS_GRACE_SECONDS: Final[float] = 60.0


def resolve_liveness_grace(flag: float | None, configured: Any = None) -> float:
    """The liveness threshold this process judges by: the flag, else the config
    file's ``liveness_grace_seconds``, else :data:`DEFAULT_LIVENESS_GRACE_SECONDS`.

    THE SAME PRECEDENCE AS ``prio_ceiling`` AND ``app_version``, and it is
    shared with the other processes for the same reason theirs are: a liveness
    threshold has one half that ACTS on it (the monitor's sweep) and four that
    REPORT by it (doctor, /metrics, the dashboard, the workers page), and they
    have to agree. Only ``pj-monitor`` has a flag for it -- the reporting
    surfaces take the file's value or the default -- so the file is the only
    place a deployment can state it once and have every process hear it.

    ``is not None``, not ``or``: a configured ``0`` is a real threshold
    ("anything but a heartbeat this instant is dead"), not an unset one. It is
    a bad threshold, and the monitor warns about it at startup rather than
    silently substituting a different number.
    """
    if flag is not None:
        return float(flag)
    if configured is not None:
        return float(configured)
    return DEFAULT_LIVENESS_GRACE_SECONDS


# =========================================================================
# NOTIFY channels
# =========================================================================
# Every channel the schema can emit on, spelled once. The names are declared
# by ``pyjobby/sql/schema/90_notify.sql`` -- as the trigger's TG_ARGV[0] and
# as the branch of ``jorb_notify()`` that builds that channel's payload --
# and a LISTENer that types one of them slightly differently gets no error
# from PostgreSQL at all: LISTEN accepts any identifier, so a typo is a
# subscription to a channel nothing will ever send on, reporting a confident
# zero forever. Named constants make that a NameError at import instead.
#
# ``pj-bench notify`` asserts these against the running database's triggers.

#: A claimable job appeared on a queue some worker has published demand for
#: (payload: the queue name). The wakeup an idle worker sleeps on.
CHANNEL_ENQUEUED: Final[str] = "jorb_enqueued"

#: A job somebody is waiting on reached a terminal state (payload:
#: {"id", "state"}). Gated on ``jorb.awaited``, which is what makes
#: wait_for_result() cost nothing when nobody is waiting.
CHANNEL_DONE: Final[str] = "jorb_done"

#: A job published an event key (payload: {"job_id", "key"}), gated on the
#: job being awaited.
CHANNEL_EVENT: Final[str] = "jorb_event"

#: A job appended to one of its durable streams (payload: {"job_id", "key"}),
#: gated on the job being awaited exactly like ``jorb_event``. The wakeup a
#: ``read_stream()`` reader parks on between rows.
CHANNEL_STREAM: Final[str] = "jorb_stream"

#: A RUNNING job was asked to stop (payload: the job id). The executing
#: worker cancels the task at its next await point.
CHANNEL_CANCEL: Final[str] = "jorb_cancel"

#: A recurring schedule fired (payload: {"schedule_id", "schedule_name",
#: "result", "job_id"}). Ungated: its consumer has no polling fallback.
CHANNEL_SCHEDULE_EXECUTED: Final[str] = "schedule_executed"


def _orjson_encode(obj: Any) -> str:
    # orjson.dumps returns bytes, but asyncpg expects str for text-format types
    return orjson.dumps(obj).decode("utf-8")


async def register_json_codecs(conn: asyncpg.Connection) -> None:
    """Use orjson for ``json``/``jsonb`` values on this connection.

    Safe to use directly as an asyncpg pool ``init`` hook.
    """
    for typename in ("json", "jsonb"):
        await conn.set_type_codec(
            typename,
            encoder=_orjson_encode,
            decoder=orjson.loads,
            schema="pg_catalog",
        )


def utcnow() -> datetime.datetime:
    """The current instant as an aware UTC datetime (the platform's only
    Python-side time representation; every schema column is timestamptz)."""
    return datetime.datetime.now(datetime.UTC)


#: The SET-clause fragment every statement that puts a job BACK into 'queued'
#: has to carry, spelled once so the rule is one rule and not six.
#:
#: A deadline_key and a debounce_key are held by a row that is QUEUED --
#: jorb_deadline_idx is partial on ``state = 'queued'`` and jorb_debounce_idx
#: on ``state = 'queued' AND run_count = 0`` -- so a key's collapse duty ends
#: the first time its row leaves 'queued', and duplicates enqueued afterwards
#: open a NEW window on a NEW row. Every statement that returns the row to
#: 'queued' therefore has to drop the keys, or the row re-enters indexes it has
#: no business being in and takes the new window's row's slot: with a later
#: burst holding the key, the requeue raises a unique violation instead of
#: requeueing. That is not a corner case, it is a batch-poisoner -- one such
#: row aborted the whole statement, so the monitor's dead-worker sweep and a
#: bulk `jobs retry` failed for every OTHER job in the batch too, every cycle,
#: forever.
#:
#: Carried by: the operator requeue (retry, rerun, DLQ retry), the worker's
#: same-row retry, the worker's RESCHEDULE (Job.reschedule() and durable
#: sleep -- a sleeping job is a queued job), and the monitor's timeout-retry,
#: dead-worker and stuck-claim sweeps. A waiter's wake carries the smaller
#: WAKE_CLEARS_KEYS below.
#:
#: Kept out of it: identity_key (jorb_identity_idx has no state predicate; the
#: row holds that key for life, which is the promise) and partition_key (a lane
#: label, not a dedupe key). Statements that requeue from an ATTEMPT rather
#: than from a terminal state cannot violate jorb_debounce_idx -- they ran, so
#: run_count >= 1 -- but they clear the same three columns anyway, because the
#: rule an operator has to hold is "leaving 'queued' ends the key", not "leaving
#: 'queued' ends the key except along these two edges".
REQUEUE_CLEARS_KEYS: Final = """deadline_key = NULL,
                debounce_key = NULL,
                debounce_deadline = NULL,"""

#: What a WAITER's wake has to carry, and it is a strictly smaller set.
#:
#: 'waiting' is outside jorb_deadline_idx, so two waiting rows may legally hold
#: the same deadline_key -- and the wake is ONE UPDATE over every waiter of the
#: upstream, so waking both would violate the index and roll the whole statement
#: back, leaving every other waiter of that upstream parked as well. Level-
#: triggered, so it fails again on the monitor's next pass, forever.
#:
#: No debounce columns: a debounced enqueue with waitfor_job/waitfor_group is
#: refused at the door (client._NO_DEBOUNCE_WAITFOR), precisely because the
#: collapse window is held by a QUEUED row, so a waiting row never carries one.
WAKE_CLEARS_KEYS: Final = "deadline_key = NULL,"


@contextlib.asynccontextmanager
async def _transaction(
    conn: asyncpg.Connection | asyncpg.Pool,
) -> AsyncIterator[asyncpg.Connection]:
    """Run a block on ONE connection inside ONE transaction, pool or not.

    Every ``db`` verb takes ``asyncpg.Connection | asyncpg.Pool`` because its
    callers are a mix (the client holds a pool, the admin API and the worker
    hold a connection, and a caller inside its own transaction hands that
    connection in). A pool has no ``transaction()``, and acquiring one per
    statement would put a multi-statement verb on two different connections --
    which is exactly the atomicity a transaction is for.

    A connection ALREADY inside a transaction gets a savepoint from asyncpg's
    nested ``transaction()``, so the block still commits or rolls back as a
    unit and still joins the caller's outer transaction. That is the behaviour
    an ``enqueue_in_transaction``-style caller wants: their commit is the one
    that decides.
    """
    if isinstance(conn, asyncpg.Pool):
        async with conn.acquire() as acquired, acquired.transaction():
            yield acquired
    else:
        async with conn.transaction():
            yield conn


#: Discard the durable state of the jobs whose ids statement 1 returned.
#:
#: A SEPARATE STATEMENT from the requeue, and the pair runs inside one explicit
#: transaction (``requeue_job``). It used to be a CTE hanging off the requeue --
#: one statement, so "the wipes and the requeue commit together and no re-claim
#: can land between them" -- and that reasoning was right about the re-claim and
#: wrong about the writer already in flight. Every CTE of a statement reads ONE
#: snapshot, taken when the statement began. A rerun that has to WAIT on the row
#: lock (an append is mid-transaction, holding the job row ``FOR SHARE``) still
#: deletes against the snapshot it took before the wait, so the rows that writer
#: commits while the UPDATE blocks survive the wipe -- and the "fresh" run then
#: appends after them, which is exactly the concatenated-stream failure the wipe
#: exists to prevent. Reproduced.
#:
#: Two statements in one transaction fixes it without giving anything up. The
#: requeue's row lock is held until COMMIT, so no re-claim can land between them
#: -- that guarantee came from the lock, never from the statement count. And
#: statement 2 takes a FRESH READ COMMITTED snapshot, so it sees every write that
#: committed while statement 1 was waiting for the lock those writers held.
WIPE_DURABLE_STATE_SQL: Final = (
    "DELETE FROM jorb_step WHERE job_id = ANY($1::bigint[])",
    "DELETE FROM jorb_stream WHERE job_id = ANY($1::bigint[])",
)


def build_requeue_sql(
    allowed_states: tuple[str, ...] = ("crashed",),
    *,
    many: bool = False,
) -> str:
    """SQL that puts a terminal/in-flight job back in the queue.

    Jobs keep ONE row for life: a retry (automatic or operator-driven)
    requeues the same row, the per-attempt audit trail lives in
    jorb_history, and run_epoch fences any stale execution out of writing
    results or checkpoints.

    The requeue bumps run_epoch itself rather than leaving that to the next
    claim. Otherwise the abandoned execution keeps the current epoch for the
    whole window between requeue and re-claim, and statements guarded ONLY by
    the epoch -- recording a DXE checkpoint, setting a timeout -- would still
    apply, letting a job the platform has given up on write checkpoints for
    the attempt that replaces it. Terminal writes were never exposed: they
    also guard on ``lifecycle.IN_FLIGHT_STATES``. Checkpoints are loaded
    without an epoch filter, so bumping costs no resume capability.

    THIS IS STATEMENT 1 OF THE "FRESH" RERUN, whose second statement is
    ``WIPE_DURABLE_STATE_SQL`` and whose transaction is opened by
    ``requeue_job``. A resume replays checkpoints regardless of epoch, so a
    re-RUN ("do it again anyway", repeating side effects) must discard them or
    the durable job would fast-forward over the very work it was asked to redo.
    The streams go with them because a stream position is assigned as "one past
    the highest this key holds": keeping the old rows would have the fresh run's
    first ``stream_write`` land at seq N instead of 0, so every reader of the
    re-run would be handed the previous run's output with the new run appended
    to it -- one stream claiming to be two runs. Retry and ``rerun --resume``
    leave both (that IS resume: the fast-forwarded ``stream_write`` checkpoints
    append nothing, so the rows the first attempt wrote are the only copy there
    will ever be).

    A JOB WITH AN UNMET DEPENDENCY GOES BACK TO 'waiting', NOT TO 'queued'.
    ``cancelled`` and ``crashed`` are both retryable, and a row parked in
    'waiting' can reach either -- an operator cancels it, or the monitor's
    unsatisfiable-waiter sweep does (monitor.CANCEL_UNSATISFIABLE_WAITERS_SQL).
    Requeueing such a row straight into 'queued' hands it to ``claim_jorb``,
    which never reads the waitfor columns: the job then RUNS while its upstream
    is still running, or while its upstream does not exist at all. Reproduced.
    So the target state is a CASE: a row that still carries a ``waitfor_job``
    or a ``waitfor_group`` re-enters 'waiting' and is released by the SAME
    machinery that released it the first time -- the worker's edge-triggered
    wake (``STMTS['enqueue-next-self-finished']`` /
    ``['enqueue-next-if-peer-group-is-finished']``) when the upstream
    finishes, or the monitor's level-triggered
    ``sweep_stranded_waiters`` when the edge has already fired -- and
    re-cancelled by the unsatisfiable sweep when the upstream is gone for
    good.

    The waitfor columns are NOT consulted for whether the dependency is
    already SATISFIED, deliberately. A retried waiter whose upstream finished
    long ago parks for at most one monitor cycle (10s by default) before the
    level sweep queues it, and paying that costs one correlated subquery per
    requeued row instead of a column read. The alternative -- deciding
    satisfaction here -- would put a second copy of the wake predicate in a
    statement that is not the wake, and the two would drift.

    THE DEDUPE KEYS ARE CLEARED, always. A deadline_key and a debounce_key are
    held by a QUEUED row and their collapse duty is over the first time the row
    leaves 'queued' (jorb_deadline_idx and jorb_debounce_idx say so with their
    predicates). A requeue puts the row BACK into 'queued', so a row that
    carried its key across would re-enter those unique indexes -- and if a
    later burst has since opened a new window on the same key, the requeue
    itself raises a unique violation, inside a failure handler or in the middle
    of a batch. Cleared here, a retry can never be refused for a key whose job
    it already is, and the new window's row is left alone. (identity_key is NOT
    cleared: its index has no state predicate, the row holds it for life, and
    the promise is that no second row exists while this one does.)

    Parameters: $1 job_id, $2 delay (interval), $3 reset_errors (bool).
    """
    states = ", ".join(f"'{s}'" for s in allowed_states)
    target = "id = ANY($1::bigint[])" if many else "id = $1::bigint"
    requeue = f"""UPDATE jorb
            SET state = CASE
                    WHEN waitfor_job IS NOT NULL OR waitfor_group IS NOT NULL
                    THEN 'waiting'::jorbstate
                    ELSE 'queued'::jorbstate
                END,
                run_epoch = run_epoch + 1,
                run_after = now() + $2::interval,
                error_count = CASE WHEN $3 THEN 0 ELSE error_count END,
                error_message = CASE WHEN $3 THEN NULL ELSE error_message END,
                error_backtrace = CASE WHEN $3 THEN NULL ELSE error_backtrace END,
                result = NULL,
                finished = NULL,
                timeout_at = NULL,
                cancel_requested = FALSE,
                {REQUEUE_CLEARS_KEYS}
                updated = now()
            WHERE {target}
              AND state IN ({states})
            RETURNING id"""
    return requeue


#: States a RETRY may start from. Retry means "this job did not succeed;
#: run it again", so a job that already finished is deliberately excluded —
#: re-running successful work risks duplicate side effects and must be an
#: explicit decision (see ``rerun_job``).
RETRYABLE_STATES: tuple[str, ...] = ("crashed", "cancelled")

#: States a RE-RUN may start from: any terminal state, including success.
#: This is the operator's "do it again anyway" verb.
RERUNNABLE_STATES: tuple[str, ...] = ("crashed", "cancelled", "finished")


#: Re-prioritise a job that has not been matched to a worker yet.
#:
#: NOT wrapped in a verb the way the app_version twin below it is, and the
#: asymmetry is the point: a priority is checked against the CALLER's
#: deployment ceiling (a client's declared ``prio_ceiling``, an AdminAPI's,
#: the CLI's), and this module is handed a connection and no deployment. So
#: the SQL is shared -- the state guard is the part that must not drift -- and
#: the validation stays with whoever knows the ceiling.
UPDATE_PRIORITY_SQL: Final = f"""UPDATE jorb SET prio = $2
             WHERE id = $1 AND state IN ({lifecycle.PRE_CLAIM_STATES_SQL})"""

#: Re-prioritise a LIST of jobs, one statement -- derived from the single-row
#: form by the same ``.replace`` idiom :data:`CANCEL_MANY_SQL` uses, so the
#: state guard cannot be right in one and wrong in the other. A bulk edit
#: getting a different guard from the single edit is not a hypothetical: it is
#: how ``JobClient.bulk_update_priority`` came to carry its own copy.
UPDATE_PRIORITY_MANY_SQL: Final = UPDATE_PRIORITY_SQL.replace(
    "WHERE id = $1", "WHERE id = ANY($1::bigint[])"
)

#: Re-pin (or unpin) a job that has not been matched to a worker yet. The
#: state guard is ``lifecycle.PRE_CLAIM_STATES``, which is where the argument
#: for it lives -- ``app_version`` is a CLAIM GATE, so editing it after the
#: claim decides nothing and editing a terminal job's rewrites history.
UPDATE_APP_VERSION_SQL: Final = f"""UPDATE jorb SET app_version = $2
             WHERE id = $1 AND state IN ({lifecycle.PRE_CLAIM_STATES_SQL})"""


async def update_job_app_version(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    app_version: str | None,
) -> bool:
    """Re-pin (or unpin) a job that has not been claimed yet.

    THE re-pin verb for every surface -- ``JobClient``, ``AdminAPI``, the CLI
    -- so no surface can validate differently or guard on different states,
    which is the model ``retry_job`` sets for the requeue verbs. ``None``
    CLEARS the pin, making the job claimable by every live worker again: the
    remedy for a job stranded by a deploy that has moved on.

    THE VALIDATOR'S RETURN VALUE IS WHAT GETS WRITTEN, not the argument. The
    validator is the one place that decides what a version pin may be, and a
    caller that passed the argument through instead would drift from every
    other surface the day it normalises anything.

    Unlike the priority twin there is no ceiling to check against: nothing the
    platform can read says which builds a fleet is ABOUT to run, so a version
    no worker advertises yet is a legitimate pin -- that is what a deploy in
    progress looks like. What makes it safe is that the stranding is loud:
    doctor's unclaimable sweep, ``jobs why`` and every idle worker's log all
    name it.

    Returns True if the row was updated, False if the job does not exist or
    has already left the queue. Raises ValueError for a version an enqueue
    would also refuse (see ``enqueue_rules.validate_app_version``).
    """
    result: str = await conn.execute(
        UPDATE_APP_VERSION_SQL, job_id, validate_app_version(app_version)
    )
    return result != "UPDATE 0"


async def retry_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
) -> int | None:
    """Retry a job that did not succeed (crashed or cancelled).

    THE retry verb for every surface — client, admin API, CLI, websocket —
    so no surface can be more permissive than another. Returns the job id,
    or None if the job was not in a retryable state.
    """
    return await requeue_job(
        conn,
        job_id,
        delay=delay,
        reset_errors=reset_errors,
        allowed_states=RETRYABLE_STATES,
    )


async def retry_jobs(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_ids: list[int],
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
) -> list[int]:
    """retry_job() over a list, as ONE statement.

    Same guard, same semantics, same bumped fence — the only difference is
    `id = ANY($1)` instead of a round trip per id, which is what makes a
    thousand-job DLQ retry one statement instead of a thousand. Returns the
    ids actually requeued (jobs keep their id across retries), omitting any
    that were not in a retryable state.
    """
    if not job_ids:
        return []
    if delay is None:
        delay = datetime.timedelta(0)
    rows = await conn.fetch(
        build_requeue_sql(RETRYABLE_STATES, many=True), job_ids, delay, reset_errors
    )
    return [r["id"] for r in rows]


async def rerun_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
    fresh: bool = True,
) -> int | None:
    """Run a terminal job again, INCLUDING one that already finished.

    Separate from :func:`retry_job` on purpose: re-running successful work
    repeats its side effects, so callers must ask for it by name.

    ``fresh`` (the default) discards the job's DXE checkpoint log AND its
    durable streams so the run actually re-executes -- a durable job's
    checkpoints are replayed with no epoch filter, so without the wipe a rerun
    would fast-forward over the very steps it was asked to redo and repeat
    nothing. The streams go with the checkpoints because a stream position is
    "one past the highest this key holds": left in place, the fresh run's first
    ``stream_write`` would land after the previous run's rows and every reader
    would be handed both runs concatenated as one. Pass ``fresh=False`` to keep
    both, i.e. RESUME an interrupted durable job from where it stopped rather
    than restart it -- the completed ``stream_write`` checkpoints fast-forward
    and append nothing, so the first attempt's rows are the run's only copy.

    The requeue and the wipe run as two statements in ONE transaction, so a
    writer that was mid-append when the rerun started is waited out and then
    wiped along with everything else (``WIPE_DURABLE_STATE_SQL``).
    """
    return await requeue_job(
        conn,
        job_id,
        delay=delay,
        reset_errors=reset_errors,
        allowed_states=RERUNNABLE_STATES,
        wipe_checkpoints=fresh,
    )


async def requeue_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    delay: datetime.timedelta | None = None,
    reset_errors: bool = True,
    allowed_states: tuple[str, ...] = RETRYABLE_STATES,
    wipe_checkpoints: bool = False,
) -> int | None:
    """Low-level requeue used by :func:`retry_job` and :func:`rerun_job`,
    and by the monitor (which requeues in-flight states). Prefer the named
    verbs; pass ``allowed_states`` only for a genuinely different guard.

    ``wipe_checkpoints`` discards the job's DXE checkpoint log and its durable
    streams so the next attempt re-executes from the start and streams from
    seq 0; retry leaves both to resume. The requeue and the wipe are TWO
    statements in ONE transaction -- ``WIPE_DURABLE_STATE_SQL`` says why that
    is stronger than the single CTE it replaced.

    ``allowed_states`` IS A BOUNDARY, not a preference. Every state in it must
    be one a requeue may legally leave (``lifecycle.LEGAL_TRANSITIONS``), and a
    caller that widens it to an in-flight state is asserting that the execution
    it is stepping on is genuinely gone -- the epoch bump fences that execution
    out of writing, but nothing here waits for it to notice. The monitor's
    sweeps pass in-flight states on exactly that basis (a dead heartbeat, an
    expired grace period); an operator surface should not.

    Returns the job id, or None if it wasn't in an allowed state."""
    if delay is None:
        delay = datetime.timedelta(0)
    sql = build_requeue_sql(allowed_states)
    if not wipe_checkpoints:
        plain: int | None = await conn.fetchval(sql, job_id, delay, reset_errors)
        return plain
    async with _transaction(conn) as cxn:
        requeued: int | None = await cxn.fetchval(sql, job_id, delay, reset_errors)
        if requeued is None:
            return None
        for wipe in WIPE_DURABLE_STATE_SQL:
            await cxn.execute(wipe, [requeued])
        return requeued


# =========================================================================
# Fork: a NEW job from an existing job's checkpoint prefix
# =========================================================================
# Retry and re-run keep ONE row (see build_requeue_sql). A FORK does not: it
# inserts a second row that re-executes the source's work from step N, with
# steps 1..N-1 copied in as its own checkpoints so they fast-forward. The
# source is never touched -- not its state, not its epoch, not its steps --
# which is what makes forking a RUNNING job safe and forking a FINISHED one
# non-destructive.
#
# ONE STATEMENT, so the row and its checkpoint prefix commit together and no
# claim can land in between: a fork whose row existed before its checkpoints
# would be claimable by a worker that then re-executed the very steps the
# fork was asked to skip. It also means every read here -- the source row,
# the step count, the prefix itself -- comes from ONE snapshot, which is the
# whole of what "as of the fork" means when the source is still running.
#
# THE GUARD IS IN THE SQL, not in a preceding SELECT: `from_step` is checked
# against the same snapshot that copies, so a source that gains a step
# between two round trips cannot make the refusal (or the acceptance) a lie.
# When the guard refuses, `forked` inserts nothing, `copied` copies nothing,
# and the outer SELECT still reports the recorded step count -- so the
# caller can name it in the error.
#
# COPIED CHECKPOINTS GET run_epoch 0, not the epoch that produced them. A
# jorb_step row's run_epoch says which attempt of ITS job wrote it, and no
# attempt of the fork wrote these -- 0 is below every epoch the fork will
# ever run at (the first claim bumps it to 1), so the prefix reads as
# "predates this job's first attempt", which is exactly what it is. Carrying
# the source's epochs over would have the fork's step table claim attempts
# that never happened. Nothing depends on the value: LOAD_STEPS_SQL replays
# checkpoints with no epoch filter, the same property `rerun --resume`
# relies on. Provenance lives in jorb.forked_from and in the fork's own
# history row.
#
# NOTHING ELSE IS COPIED. Streams, events and mailbox messages are the
# SOURCE's output and stay with it (docs/DXE.md spells out what that means
# for a fast-forwarded stream write).
#
# Params: $1 source job id, $2 from_step (1-based), $3 queue override or
# NULL, $4 priority override or NULL, $5 kwargs override or NULL, $6 the
# fork's own app_version or NULL.
FORK_JOB_SQL = """
    WITH src AS (
        SELECT * FROM jorb WHERE id = $1
    ), recorded AS (
        SELECT COALESCE(max(step_seq), 0)::int AS steps,
               (count(*) FILTER (WHERE step_seq < $2::int))::int AS prefix
          FROM jorb_step WHERE job_id = $1
    ), forked AS (
        INSERT INTO jorb (
            job_class, kwargs, queue, prio, capability, uid, tags, admin_data,
            state, forked_from, partition_key, app_version
        )
        SELECT src.job_class,
               COALESCE($5::jsonb, src.kwargs),
               COALESCE($3::text, src.queue),
               COALESCE($4::int, src.prio),
               src.capability,
               src.uid,
               src.tags,
               (src.admin_data - 'fork') || jsonb_build_object(
                   'fork', jsonb_build_object(
                       'from_step', $2::int, 'steps_copied', recorded.prefix)),
               'queued'::jorbstate,
               src.id,
               -- INHERITED, unlike the three dedupe keys below it in the
               -- docstring: a partition_key says WHOSE work this is, not
               -- WHICH piece of work it is, so a tenant's fork is still that
               -- tenant's job and still counts against that tenant's lane.
               -- Same reasoning that carries uid and tags across.
               src.partition_key,
               -- NOT INHERITED, and the contrast with the line above is the
               -- point: a partition_key says whose work this is, an
               -- app_version says which BUILD may run it -- and the main
               -- reason to fork is to re-run the work under new code. A fork
               -- that inherited the source's pin would be stranded by the
               -- deploy the operator just made, which is the failure this
               -- whole feature exists to make impossible to miss. So the fork
               -- is unpinned unless the caller pins it, and the caller who
               -- wants the source's pin passes the source's version.
               $6::text
          FROM src, recorded
         WHERE $2::int <= recorded.steps + 1
        RETURNING id, queue, prio
    ), copied AS (
        INSERT INTO jorb_step (job_id, step_seq, name, output, error,
                               run_epoch, started, finished)
        SELECT forked.id, s.step_seq, s.name, s.output, s.error,
               0, s.started, s.finished
          FROM jorb_step s, forked
         WHERE s.job_id = $1 AND s.step_seq < $2::int
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM src)::int      AS source_exists,
           (SELECT steps FROM recorded)         AS recorded_steps,
           (SELECT id FROM forked)              AS job_id,
           (SELECT queue FROM forked)           AS queue,
           (SELECT prio FROM forked)            AS prio,
           (SELECT count(*) FROM copied)::int   AS steps_copied
"""

#: The first step whose checkpoint recorded a FAILURE — where "fork from the
#: failure" starts. A step that failed and then succeeded on a later attempt
#: has no error recorded (RECORD_STEP_SQL never lets an error overwrite a
#: committed success), so this finds the step whose recorded OUTCOME is a
#: failure and not merely one that ever raised.
#:
#: Asked together with "does this job exist at all?", because a job with no
#: steps and a job with no such id both answer NULL here and they need
#: different messages.
FIRST_FAILED_STEP_SQL = """
    SELECT (SELECT count(*) FROM jorb WHERE id = $1)::int AS source_exists,
           (SELECT min(step_seq)::int FROM jorb_step
             WHERE job_id = $1 AND error IS NOT NULL) AS failed_step
"""


class ForkRefused(ValueError):
    """A fork was asked for that the platform will not create.

    A ValueError because every case is the caller's argument being wrong
    about a fact the database holds — no such job, a step the source never
    recorded, a failure to fork from that never happened — and each carries
    the number that makes it actionable.
    """


def _no_such_job(job_id: int) -> ForkRefused:
    """The refusal both fork verbs make about an id that is not there."""
    return ForkRefused(f"job {job_id} not found, so there is nothing to fork")


async def fork_job(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    from_step: int = 1,
    queue: str | None = None,
    priority: int | None = None,
    kwargs_override: dict[str, Any] | None = None,
    app_version: str | None = None,
) -> dict[str, Any]:
    """Fork ``job_id`` into a NEW job that starts at ``from_step``.

    THE fork verb for every surface (client, admin API, `pj-admin jobs fork`).
    WHAT THAT DOES AND DOES NOT COVER, exactly, because "no surface can fork on
    terms another would refuse" was an overstatement:

    * ENFORCED HERE, for every surface: ``from_step >= 1``, the source existing,
      ``from_step`` within the source's recorded steps + 1, the source not being
      deleted underneath the fork, and ``app_version`` (empty and over-long,
      via :func:`enqueue_rules.validate_app_version` -- one call, here, rather
      than one per wrapper).
    * LEFT TO THE WRAPPERS: ``priority``. The ceiling a priority is checked
      against is a property of the CALLER's deployment (a client's declared
      ``prio_ceiling``, an AdminAPI's, the CLI's ``--max-prio`` or config), and
      this function is handed a connection and no deployment. Moving the check
      here would mean either inventing a default ceiling that silently differs
      from the caller's or plumbing one through every call -- so it stays where
      the number lives. Every wrapper does check it, with the same
      ``enqueue_rules.validate_priority``.

    ``from_step`` is 1-based and names the step the fork EXECUTES first:
    ``1`` (the default) copies no checkpoints and re-runs the whole job under
    a new id, ``4`` copies steps 1-3 and fast-forwards them.

    The source may be in any state; a fork never touches it. Forking a job
    that is still running is allowed and takes the prefix as of this
    statement's snapshot — the source's step log may keep growing afterwards,
    and the fork will not see the later rows.

    What the new row inherits: job_class, kwargs (or ``kwargs_override``),
    queue and prio (or the overrides), capability, ``uid``, tags,
    ``partition_key`` (all three say WHOSE work it is, so a tenant's fork is
    still that tenant's job and stays in their fair-share lane), and
    admin_data —
    the retry/timeout policy describes the WORK, so the fork runs under the
    same rules. What it does not: deadline_key, identity_key and
    debounce_key
    (identity and dedupe; two live rows sharing an idempotency key would make
    it mean nothing, and an identity_key promises exactly that there is only
    one), schedule_id (no schedule fired this), dag_id / waitfor_* / run_group (a
    fork belongs to no DAG, group or dependency edge of the original's), and
    every execution counter — it starts queued at run_epoch 0 with no errors
    and no result.

    ``app_version`` is the fork's OWN pin and defaults to None, unpinned —
    the source's is deliberately not inherited, because a fork usually exists
    to re-run the work under NEW code and inheriting the pin would strand it
    on the build that just went away. A caller who really wants the source's
    pin reads it and passes it.

    Returns ``{"job_id", "source_job_id", "from_step", "steps_copied",
    "queue", "priority"}``.

    Raises ForkRefused when there is no such job, when ``from_step`` is below
    1, when it exceeds the source's recorded step count + 1 (there is no
    prefix to copy that far, so the request is a typo rather than a fork), or
    when the source is DELETED while the fork is being written. Raises
    ValueError for an empty or over-long ``app_version``.
    """
    if from_step < 1:
        raise ForkRefused(
            f"from_step must be at least 1 (steps are numbered from 1); got {from_step}"
        )
    try:
        row = await conn.fetchrow(
            FORK_JOB_SQL,
            job_id,
            from_step,
            queue,
            priority,
            kwargs_override,
            validate_app_version(app_version),
        )
    except asyncpg.ForeignKeyViolationError as e:
        # `forked_from REFERENCES jorb (id)`, and the only way the reference can
        # fail is a concurrent DELETE of the very source this statement read: a
        # retention sweep or `jobs delete` committing between this snapshot
        # (which found the source, so `src` had a row) and the FK check at the
        # end of the insert. Reported as the refusal it is -- the caller's
        # argument turned out not to name a job any more, which is exactly what
        # `_no_such_job` says, only later -- rather than as a raw
        # ForeignKeyViolationError naming a column no caller passed.
        raise ForkRefused(
            f"source job {job_id} was deleted while forking, so the fork has "
            f"nothing to descend from and nothing was written; the source was "
            f"there when this statement began (retention or an operator delete "
            f"committed underneath it)"
        ) from e
    if not row["source_exists"]:
        raise _no_such_job(job_id)
    if row["job_id"] is None:
        recorded = row["recorded_steps"]
        raise ForkRefused(
            f"job {job_id} recorded {recorded} step(s), so a fork may start at "
            f"step {recorded + 1} at the latest; got {from_step}"
        )
    return {
        "job_id": row["job_id"],
        "source_job_id": job_id,
        "from_step": from_step,
        "steps_copied": row["steps_copied"],
        "queue": row["queue"],
        "priority": row["prio"],
    }


async def fork_job_from_failure(
    conn: asyncpg.Connection | asyncpg.Pool,
    job_id: int,
    *,
    queue: str | None = None,
    priority: int | None = None,
    kwargs_override: dict[str, Any] | None = None,
    app_version: str | None = None,
) -> dict[str, Any]:
    """:func:`fork_job` from the first step that FAILED.

    The incident verb: fix the code, fork the job from the step that broke,
    and every step before it fast-forwards from its recorded output instead
    of running again.

    Raises ForkRefused when no step of ``job_id`` has a recorded error —
    including when the job crashed OUTSIDE any step, which is a real
    condition with a different answer (`jobs steps` shows what ran; name the
    step with ``from_step``).
    """
    row = await conn.fetchrow(FIRST_FAILED_STEP_SQL, job_id)
    if not row["source_exists"]:
        raise _no_such_job(job_id)
    failed: int | None = row["failed_step"]
    if failed is None:
        raise ForkRefused(
            f"job {job_id} has no failed step recorded, so there is no failure "
            f"to fork from — `pj-admin jobs steps {job_id}` shows what ran"
        )
    return await fork_job(
        conn,
        job_id,
        from_step=failed,
        queue=queue,
        priority=priority,
        kwargs_override=kwargs_override,
        app_version=app_version,
    )


#: THE queue-statistics query. Every surface that reports per-queue,
#: per-state counts reads it from here (client ``queue_stats``/``list_queues``,
#: ``AdminAPI.queue_stats``) so no two surfaces can disagree about what a
#: number means. Parameters: $1 = the recency window as an interval,
#: $2 = a queue name, or NULL for every queue. Every arm returns
#: ``(queue, state text, n bigint)``.
#:
#: TWO THINGS THE SHAPE ENCODES.
#:
#: 1. LIVE STATES EXACTLY, TERMINAL STATES WITHIN A WINDOW. Live work
#:    (queued/scheduled/claimed/running/waiting) is bounded by work in
#:    progress however big the table gets; the terminal states are bounded by
#:    nothing at all. "How many jobs finished, EVER" is an audit question for
#:    SQL, not a number a dashboard asks for on a timer, so the terminal arm
#:    counts only what landed inside $1. One arm per partial index, rather
#:    than a single GROUP BY, is also what keeps this off a scan of the whole
#:    table: a predicate spanning several states matches none of the partial
#:    indexes and collapses into a sequential scan.
#:
#: 2. 'queued' IS SPLIT FROM 'scheduled'. A job whose ``run_after`` is still
#:    in the future -- a retry backoff, an enqueue-at -- is not backlog. It is
#:    exactly where it was asked to be, and counting it as queued makes a
#:    healthy install look like a stuck one, which is the number an operator
#:    pages on. So:
#:
#:      state="queued"     claimable RIGHT NOW.
#:      state="scheduled"  deliberately parked in the future. Not a backlog.
#:
#:    The split is also what gives BOTH halves a real index condition against
#:    ``jorb_claim_idx`` (queue, prio, run_after) instead of a bare
#:    ``state = 'queued'`` filter whose index-only-ness depends on when
#:    autovacuum last ran. ``web_admin.PROM_SQL_LIVE_STATES`` and
#:    ``websocket_server.SNAPSHOT_SQL`` ask the same two questions with the
#:    same predicates and emit the same two state names (their strings stay
#:    separate because their plans are pinned and they carry extra columns);
#:    web_admin's comment has the full planner history.
#:
#: $2 is written as an OR-NULL predicate, which trades the index *condition*
#: for a filter over each partial index's bounded live set -- the price of one
#: query shape serving both "this queue" and "all queues".
QUEUE_STATS_SQL = f"""
    SELECT queue, 'queued' AS state, COUNT(*)::bigint AS n
      FROM jorb
     WHERE state = 'queued' AND run_after <= now()
       AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue
    UNION ALL
    SELECT queue, 'scheduled', COUNT(*)::bigint
      FROM jorb
     WHERE state = 'queued' AND run_after > now()
       AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue
    UNION ALL
    SELECT queue, state::text, COUNT(*)::bigint
      FROM jorb
     WHERE state IN ({lifecycle.IN_FLIGHT_STATES_SQL})
       AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue, state
    UNION ALL
    SELECT queue, 'waiting', COUNT(*)::bigint
      FROM jorb
     WHERE state = 'waiting' AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue
    UNION ALL
    SELECT queue, state::text, COUNT(*)::bigint
      FROM jorb
     WHERE state IN ('finished', 'crashed', 'cancelled')
       AND COALESCE(finished, updated) >= now() - $1::interval
       AND ($2::text IS NULL OR queue = $2)
     GROUP BY queue, state
"""

#: Every queue's control row, in name order: the pause flag and the limits the
#: claim path enforces.
#:
#: Here rather than in ``AdminAPI`` because it has TWO readers that cannot
#: share an object: ``AdminAPI.list_queue_controls`` (which holds a
#: Connection) and ``WebSocketServer.get_queue_controls`` (which holds a
#: Pool). The dashboard server deliberately does not import the admin API --
#: its dependencies are this module, ``client`` and ``lifecycle``, and nothing
#: it does is an admin operation -- so making it construct an AdminAPI to read
#: four columns would invert that arrow for a single SELECT. A constant both
#: import inverts nothing and still leaves one statement.
#:
#: ``SELECT *`` on purpose: this is a small control table read by name, both
#: callers project the columns they want in Python, and a column list here
#: would be a third place to update when the control plane grows a knob.
QUEUE_CONTROLS_SQL: Final = "SELECT * FROM jorb_queue ORDER BY name"

#: The reported state names :data:`QUEUE_STATS_SQL` can emit: every
#: ``jorbstate`` label plus the ``scheduled`` split of ``queued``. Callers
#: zero-fill their result dicts from this so a quiet queue reports 0 rather
#: than a missing key.
QUEUE_STATS_STATES: tuple[str, ...] = (*lifecycle.JOB_STATES, "scheduled")


CANCEL_SQL = f"""UPDATE jorb
        SET state = CASE WHEN state IN ({lifecycle.PRE_CLAIM_STATES_SQL})
                         THEN 'cancelled'::jorbstate ELSE state END,
            cancel_requested = CASE WHEN state IN ({lifecycle.IN_FLIGHT_STATES_SQL})
                                    THEN TRUE ELSE cancel_requested END,
            finished = CASE WHEN state IN ({lifecycle.PRE_CLAIM_STATES_SQL})
                            THEN now() ELSE finished END,
            updated = now()
        WHERE id = $1
          AND state IN ({lifecycle.LIVE_STATES_SQL})
        RETURNING state, cancel_requested"""

#: cancel over a list, one statement — identical CASE logic to CANCEL_SQL.
CANCEL_MANY_SQL = CANCEL_SQL.replace("WHERE id = $1", "WHERE id = ANY($1::bigint[])")


async def cancel_job(
    conn: asyncpg.Connection | asyncpg.Pool, job_id: int
) -> str | None:
    """Cancel a job wherever it is in its lifecycle (the one cancel path
    shared by the client, admin API, and websocket server).

    Queued/waiting jobs are cancelled immediately. Claimed/running jobs get
    cancel_requested set — the jorb_cancel NOTIFY reaches the executing
    worker, which cancels the task at its next await point.

    Returns 'cancelled', 'cancel_requested', or None (job not cancellable).
    """
    row = await conn.fetchrow(CANCEL_SQL, job_id)
    if row is None:
        return None
    if row["state"] == "cancelled":
        return "cancelled"
    return "cancel_requested"


async def cancel_jobs(
    conn: asyncpg.Connection | asyncpg.Pool, job_ids: list[int]
) -> int:
    """cancel_job() over a list, as ONE statement.

    Returns how many jobs the cancel reached (cancelled outright or
    cancel-requested); ids not in a cancellable state are simply not
    counted, matching the single verb returning None for them.
    """
    if not job_ids:
        return 0
    rows = await conn.fetch(CANCEL_MANY_SQL, job_ids)
    return len(rows)


async def connect(*args: Any, **kwargs: Any) -> asyncpg.Connection:
    """``asyncpg.connect`` with pyjobby's JSON codecs registered."""
    conn = await asyncpg.connect(*args, **kwargs)
    await register_json_codecs(conn)
    return conn


async def create_pool(*args: Any, **kwargs: Any) -> asyncpg.Pool:
    """``asyncpg.create_pool`` with pyjobby's JSON codecs registered.

    A caller-provided ``init`` hook is still honored; codecs register first.
    """
    caller_init = kwargs.pop("init", None)

    async def _init(conn: asyncpg.Connection) -> None:
        await register_json_codecs(conn)
        if caller_init is not None:
            await caller_init(conn)

    return await asyncpg.create_pool(*args, init=_init, **kwargs)
