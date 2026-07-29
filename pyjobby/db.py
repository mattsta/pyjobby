"""Shared database helpers for pyjobby.

Every pyjobby component talks to PostgreSQL through the helpers here so that
connection behavior is uniform everywhere:

- ``json``/``jsonb`` columns always encode/decode via orjson, so Python dicts
  go in and come out of every connection identically (workers, client library,
  CLI, web admin, websocket server, timeout monitor, scheduler).
- Job states are the ``JobState`` enum instead of scattered string literals.
"""

from __future__ import annotations

import datetime
import enum
from typing import Any, Final

import asyncpg  # type: ignore[import-untyped]
import orjson

from . import lifecycle


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

#: Seconds between worker registry heartbeats (``pj --heartbeat-interval``).
#: It lives HERE because two different processes have to agree about it: the
#: worker writes ``jorb_worker.last_seen`` on this cadence, and the monitor's
#: ``--liveness-grace`` judges staleness against it. A grace below the
#: heartbeat interval makes every LIVE worker look dead between beats -- the
#: monitor then requeues in-flight jobs from workers that are fine, over and
#: over, and no job longer than the grace can ever finish. The monitor warns
#: at startup when the two are configured into that state.
DEFAULT_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 10.0

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


def build_requeue_sql(
    allowed_states: tuple[str, ...] = ("crashed",),
    *,
    many: bool = False,
    wipe_checkpoints: bool = False,
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
    also guard on state IN ('claimed','running'). Checkpoints are loaded
    without an epoch filter, so bumping costs no resume capability.

    ``wipe_checkpoints`` deletes the job's jorb_step rows in the same
    statement: a resume replays checkpoints regardless of epoch, so a re-RUN
    ("do it again anyway", repeating side effects) must discard them or the
    durable job would fast-forward over the very work it was asked to redo.
    Retry leaves them (that IS resume). One statement, so the wipe and the
    requeue commit together and no re-claim can land between them.

    Parameters: $1 job_id, $2 delay (interval), $3 reset_errors (bool).
    """
    states = ", ".join(f"'{s}'" for s in allowed_states)
    target = "id = ANY($1::bigint[])" if many else "id = $1::bigint"
    requeue = f"""UPDATE jorb
            SET state = 'queued',
                run_epoch = run_epoch + 1,
                run_after = now() + $2::interval,
                error_count = CASE WHEN $3 THEN 0 ELSE error_count END,
                error_message = CASE WHEN $3 THEN NULL ELSE error_message END,
                error_backtrace = CASE WHEN $3 THEN NULL ELSE error_backtrace END,
                result = NULL,
                finished = NULL,
                timeout_at = NULL,
                cancel_requested = FALSE,
                updated = now()
            WHERE {target}
              AND state IN ({states})
            RETURNING id"""
    if not wipe_checkpoints:
        return requeue
    return f"""WITH bumped AS (
            {requeue}
        ), wiped AS (
            DELETE FROM jorb_step WHERE job_id IN (SELECT id FROM bumped)
        )
        SELECT id FROM bumped"""


#: States a RETRY may start from. Retry means "this job did not succeed;
#: run it again", so a job that already finished is deliberately excluded —
#: re-running successful work risks duplicate side effects and must be an
#: explicit decision (see ``rerun_job``).
RETRYABLE_STATES: tuple[str, ...] = ("crashed", "cancelled")

#: States a RE-RUN may start from: any terminal state, including success.
#: This is the operator's "do it again anyway" verb.
RERUNNABLE_STATES: tuple[str, ...] = ("crashed", "cancelled", "finished")


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

    ``fresh`` (the default) discards the job's DXE checkpoint log so the run
    actually re-executes -- a durable job's checkpoints are replayed with no
    epoch filter, so without the wipe a rerun would fast-forward over the
    very steps it was asked to redo and repeat nothing. Pass ``fresh=False``
    to keep the checkpoints, i.e. RESUME an interrupted durable job from
    where it stopped rather than restart it.
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

    ``wipe_checkpoints`` discards the job's DXE checkpoint log so the next
    attempt re-executes from the start; retry leaves it to resume.

    Returns the job id, or None if it wasn't in an allowed state."""
    if delay is None:
        delay = datetime.timedelta(0)
    requeued: int | None = await conn.fetchval(
        build_requeue_sql(allowed_states, wipe_checkpoints=wipe_checkpoints),
        job_id,
        delay,
        reset_errors,
    )
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
# NULL, $4 priority override or NULL, $5 kwargs override or NULL.
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
            state, forked_from
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
               src.id
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
) -> dict[str, Any]:
    """Fork ``job_id`` into a NEW job that starts at ``from_step``.

    THE fork verb for every surface (client, admin API, `pj-admin jobs
    fork`), so no surface can fork on terms another one would refuse.

    ``from_step`` is 1-based and names the step the fork EXECUTES first:
    ``1`` (the default) copies no checkpoints and re-runs the whole job under
    a new id, ``4`` copies steps 1-3 and fast-forwards them.

    The source may be in any state; a fork never touches it. Forking a job
    that is still running is allowed and takes the prefix as of this
    statement's snapshot — the source's step log may keep growing afterwards,
    and the fork will not see the later rows.

    What the new row inherits: job_class, kwargs (or ``kwargs_override``),
    queue and prio (or the overrides), capability, tags, and admin_data —
    the retry/timeout policy describes the WORK, so the fork runs under the
    same rules. What it does not: uid, deadline_key and identity_key
    (identity and dedupe; two live rows sharing an idempotency key would make
    it mean nothing, and an identity_key promises exactly that there is only
    one), schedule_id (no schedule fired this), dag_id / waitfor_* / run_group (a
    fork belongs to no DAG, group or dependency edge of the original's), and
    every execution counter — it starts queued at run_epoch 0 with no errors
    and no result.

    Returns ``{"job_id", "source_job_id", "from_step", "steps_copied",
    "queue", "priority"}``.

    Raises ForkRefused when there is no such job, when ``from_step`` is below
    1, or when it exceeds the source's recorded step count + 1 (there is no
    prefix to copy that far, so the request is a typo rather than a fork).
    """
    if from_step < 1:
        raise ForkRefused(
            f"from_step must be at least 1 (steps are numbered from 1); got {from_step}"
        )
    row = await conn.fetchrow(
        FORK_JOB_SQL, job_id, from_step, queue, priority, kwargs_override
    )
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
QUEUE_STATS_SQL = """
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
     WHERE state IN ('claimed', 'running') AND ($2::text IS NULL OR queue = $2)
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

#: The reported state names :data:`QUEUE_STATS_SQL` can emit: every
#: ``jorbstate`` label plus the ``scheduled`` split of ``queued``. Callers
#: zero-fill their result dicts from this so a quiet queue reports 0 rather
#: than a missing key.
QUEUE_STATS_STATES: tuple[str, ...] = (*lifecycle.JOB_STATES, "scheduled")


CANCEL_SQL = """UPDATE jorb
        SET state = CASE WHEN state IN ('queued', 'waiting')
                         THEN 'cancelled'::jorbstate ELSE state END,
            cancel_requested = CASE WHEN state IN ('claimed', 'running')
                                    THEN TRUE ELSE cancel_requested END,
            finished = CASE WHEN state IN ('queued', 'waiting')
                            THEN now() ELSE finished END,
            updated = now()
        WHERE id = $1
          AND state IN ('queued', 'waiting', 'claimed', 'running')
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
