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
    async with await JobClient.from_config('./pyjobby.toml') as client:
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
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

from . import db, fsm, lifecycle
from .retry_strategies import (
    DEFAULT_INITIAL_RETRY_DELAY,
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_RETRY_DELAY,
    DEFAULT_RETRY_STRATEGY,
    RetryStrategy,
)

if TYPE_CHECKING:
    from .dag import DAGBuilder


class JobError(Exception):
    """Base class for job-outcome errors raised by the client library.

    Carries ``job_id`` when the error is about one job, so handlers can
    route on it without parsing the message.
    """

    def __init__(self, message: str, job_id: int | None = None):
        super().__init__(message)
        self.job_id = job_id


class JobFailedError(JobError):
    """The awaited job reached the terminal 'crashed' state (the DLQ)."""

    def __init__(self, job_id: int, error_message: str | None = None):
        super().__init__(
            f"job {job_id} crashed: {error_message or 'unknown error'}", job_id
        )
        self.error_message = error_message


class JobCancelledError(JobError):
    """The awaited job reached the terminal 'cancelled' state."""

    def __init__(self, job_id: int):
        super().__init__(f"job {job_id} was cancelled", job_id)


# Sentinel returned by poll callbacks when the awaited condition is not yet
# satisfied (None is a legitimate job result / event value).
_PENDING: Any = object()

# The states from which no further event will ever be published, so a waiter
# on a value that has not arrived can stop rather than time out. Imported
# rather than restated: `pyjobby.lifecycle` is the declaration, and it has no
# imports of its own, so the client can read it without a cycle.
_TERMINAL_JOB_STATES = frozenset(lifecycle.TERMINAL_STATES)

# The single enqueue INSERT shared by every enqueue path (pool-based
# enqueue(), caller-transaction enqueue_in_transaction(), handles) — and by
# the scheduler, which enqueues a job on every firing. Public, because a
# second component using it is not a private reach-in: it is the platform's
# one enqueue statement, and the alternative (a hand-rolled INSERT next
# door) is what it exists to prevent.
ENQUEUE_SQL = """
    INSERT INTO jorb (
        job_class, kwargs, queue, prio, run_after,
        capability, uid, run_group,
        waitfor_job, waitfor_group,
        deadline_key, admin_data, tags, state, schedule_id,
        identity_key, debounce_key, debounce_deadline, partition_key,
        app_version
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
            $16, $17, $18, $19, $20)
    RETURNING id
"""

#: Where the job's kwargs sit in build_enqueue_row's parameter row, i.e.
#: ENQUEUE_SQL's $2. The debounce bounce writes THAT value into the row it
#: collapses onto, so "this call's kwargs" means exactly what it would have
#: meant had the call inserted instead — including the job_kwargs/**kwargs
#: resolution the builder did.
_ROW_KWARGS: Final = 1

# The identified form of ENQUEUE_SQL: same columns, same row, but the write
# is "claim this identity or tell me who holds it". ONE statement, because
# read-then-insert has a race with exactly the shape the feature exists to
# rule out -- two callers both read "no such identity" and both insert, and
# only the unique index stops the second, as an error the caller did not ask
# for. Here the loser of the race takes the second branch and gets the
# winner's id, so both callers are told the same thing.
#
# ON CONFLICT infers jorb_identity_idx, and inference against a PARTIAL
# unique index requires its predicate restated (`WHERE identity_key IS NOT
# NULL`) -- without it PostgreSQL refuses the statement rather than guessing.
#
# DO NOTHING, deliberately not DO UPDATE. DO UPDATE would return the
# existing row directly, but it takes a ROW LOCK on it and writes a new
# version of it: a duplicate enqueue would then contend with the worker
# running that very job, and leave a dead tuple behind for every duplicate.
# An identity collision must cost the holder nothing.
#
# THIS STATEMENT CAN RETURN NO ROWS AT ALL, and that is not an error. The
# sequence, measured (tests/test_job_identity.py pins each step):
#
#   1. another transaction has inserted this identity and not committed;
#   2. the speculative insert here finds the conflicting tuple and WAITS for
#      that transaction -- ON CONFLICT does not skip an in-progress
#      conflict, it blocks on it, so this never answers early or twice;
#   3. that transaction commits, so DO NOTHING correctly inserts nothing;
#   4. the second branch runs under THIS statement's snapshot, which was
#      taken back at step 1 and therefore cannot see the row that committed
#      at step 3.
#
# Nothing was written and nothing can be reported: the answer is "ask
# again", with a new snapshot. _enqueue_identity is the loop that does.
#
# Params: ENQUEUE_SQL's, with $16 the identity_key (never NULL here).
ENQUEUE_IDENTIFIED_SQL = """
    WITH claimed AS (
        INSERT INTO jorb (
            job_class, kwargs, queue, prio, run_after,
            capability, uid, run_group,
            waitfor_job, waitfor_group,
            deadline_key, admin_data, tags, state, schedule_id,
            identity_key, debounce_key, debounce_deadline, partition_key,
            app_version
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18, $19, $20)
        ON CONFLICT (identity_key) WHERE identity_key IS NOT NULL DO NOTHING
        RETURNING id, job_class
    )
    SELECT id, job_class, TRUE AS created FROM claimed
    UNION ALL
    SELECT held.id, held.job_class, FALSE
      FROM jorb held
     WHERE held.identity_key = $16
       AND NOT EXISTS (SELECT 1 FROM claimed)
"""

# The bounce half of debounce(): collapse this call onto the row that already
# holds the key, if there is one.
#
# ONE statement doing two things, because they have to agree about which row
# they are talking about. `parked` is the row the key belongs to right now;
# `bounced` is that same row, updated -- and the UPDATE RESTATES the whole
# predicate instead of joining `parked` on id, which is the load-bearing
# detail. Under READ COMMITTED an UPDATE that finds its target row locked
# waits, then re-evaluates its OWN quals against the version that committed
# (EvalPlanQual). A qual of `id = parked.id` is satisfied by every version of
# that row, including one a worker has just claimed -- so the statement would
# move run_after and rewrite the kwargs of a job that is already RUNNING.
# Restated, the recheck fails, nothing is bounced, and the caller falls
# through to the insert. That fall-through IS the bounce-vs-claim race, and
# it resolves correctly: the claimed row has left jorb_debounce_idx, so the
# fresh insert opens the next collapse window rather than conflicting.
#
# `run_count = 0` is jorb_debounce_idx's predicate, restated for the same
# reason and carrying the same meaning: queued and never claimed, i.e. the
# window is still open. It is what keeps a RETRIED debounced job -- queued
# again, but long past its window -- from being bounced by a burst that has
# nothing to do with it.
#
# LEAST clamps to debounce_deadline, the cap the FIRST enqueue of this key
# wrote (NULL means the caller accepted unbounded deferral -- 'infinity'
# makes that the same expression rather than a second statement). A bounce
# never rewrites the deadline: the cap belongs to the window, not to the call.
#
# The class check is a QUAL, not a check on the returned row, so a key used
# for two different job classes updates nothing rather than leaving one class
# running with the other's arguments. `parked` still reports the holder, so
# the caller can name it in the refusal; a NULL `fires_at` with a MATCHING
# class is the lost race above, and means "insert instead".
#
# Params: $1 debounce_key, $2 the new run_after, $3 the new kwargs,
#         $4 the job_class the caller is debouncing.
DEBOUNCE_BOUNCE_SQL = """
    WITH parked AS (
        SELECT id, job_class
          FROM jorb
         WHERE debounce_key = $1
           AND state = 'queued'
           AND run_count = 0
    ), bounced AS (
        UPDATE jorb
           SET run_after = LEAST(
                   $2::timestamptz,
                   COALESCE(debounce_deadline, 'infinity'::timestamptz)),
               kwargs = $3,
               updated = now()
         WHERE debounce_key = $1
           AND state = 'queued'
           AND run_count = 0
           AND job_class = $4
        RETURNING id, run_after
    )
    SELECT parked.id, parked.job_class, bounced.run_after AS fires_at
      FROM parked LEFT JOIN bounced ON bounced.id = parked.id
"""

# The insert half: open a new collapse window, or discover that somebody else
# opened it first.
#
# ENQUEUE_SQL's columns and parameters exactly, plus the conflict clause --
# and the four-step sequence written out on ENQUEUE_IDENTIFIED_SQL above
# applies here VERBATIM, down to the reason the answer can be no rows at all:
# a speculative insert BLOCKS on an uncommitted conflicting tuple, and the
# row that commits underneath it is then newer than this statement's
# snapshot. The loop that re-asks with a fresh snapshot is JobClient.debounce,
# and its next attempt bounces the row it could not see.
#
# Inference restates jorb_debounce_idx's predicate for the same reason
# ENQUEUE_IDENTIFIED_SQL restates jorb_identity_idx's: PostgreSQL refuses to
# guess which partial index an ON CONFLICT means.
#
# Params: ENQUEUE_SQL's, with $17 the debounce_key (never NULL here) and $18
# the cap, or NULL for unbounded deferral.
ENQUEUE_DEBOUNCED_SQL = """
    INSERT INTO jorb (
        job_class, kwargs, queue, prio, run_after,
        capability, uid, run_group,
        waitfor_job, waitfor_group,
        deadline_key, admin_data, tags, state, schedule_id,
        identity_key, debounce_key, debounce_deadline, partition_key,
        app_version
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
            $16, $17, $18, $19, $20)
    ON CONFLICT (debounce_key)
        WHERE debounce_key IS NOT NULL AND state = 'queued' AND run_count = 0
        DO NOTHING
    RETURNING id, run_after
"""

# The multi-row form of ENQUEUE_SQL: the same columns, filled from one array
# per column, so a batch is ONE statement — and therefore all-or-nothing
# without a transaction of its own. Every multi-row writer (enqueue_batch,
# create_fan_out) goes through this, so "a batch job loses nothing by being
# batched" stays true of the SQL as well as of the row construction.
ENQUEUE_BATCH_SQL = """
    INSERT INTO jorb (
        job_class, kwargs, queue, prio, run_after,
        capability, uid, run_group,
        waitfor_job, waitfor_group,
        deadline_key, admin_data, tags, state, schedule_id,
        identity_key, debounce_key, debounce_deadline, partition_key,
        app_version
    )
    SELECT * FROM UNNEST(
        $1::text[], $2::jsonb[], $3::text[], $4::int[],
        $5::timestamptz[], $6::text[], $7::bigint[],
        $8::bigint[], $9::bigint[], $10::bigint[],
        $11::text[], $12::jsonb[], $13::jsonb[],
        $14::jorbstate[], $15::bigint[], $16::text[],
        $17::text[], $18::timestamptz[], $19::text[],
        $20::text[]
    )
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

#: The retry strategies enqueue accepts, derived from the enum so a strategy
#: added to retry_strategies.py is accepted here the moment it exists.
_RETRY_STRATEGIES = frozenset(RetryStrategy)

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

#: How long `run()` waits for its best-effort cancellation after the caller
#: has already stopped waiting. Bounded because that cleanup runs inside an
#: exception handler on the way OUT: an exhausted pool or a database that
#: went away made it wait forever, so the TimeoutError the caller was told to
#: catch never arrived and the call hung on the tidying instead of the work.
#: Short on purpose — the cancel is best effort, the exception is not.
_RUN_CANCEL_TIMEOUT: Final = 5.0

#: How many times a SPECULATIVE ENQUEUE re-runs its statement when that
#: statement comes back empty -- shared by the identified and the debounced
#: paths, which face the same semantics because they are the same shape:
#: INSERT ... ON CONFLICT DO NOTHING against a caller-chosen key.
#:
#: Empty means the key was committed by another transaction AFTER this
#: statement's snapshot was taken, so the statement could neither insert over
#: the row nor see it (ENQUEUE_IDENTIFIED_SQL's comment walks through the
#: four steps). Re-running takes a FRESH SNAPSHOT, which is why this is a
#: loop and not a longer statement, and one retry is normally the whole of
#: it: the writer we waited for has already committed.
#:
#: More than one is only needed if ANOTHER writer claims the key in the gap,
#: so the budget is a backstop against a pathological stream of them and not
#: a wait for anything. It is deliberately NOT the answer to a transaction
#: that never commits -- that case blocks inside PostgreSQL at step 2 and
#: never reaches this loop at all.
_SPECULATIVE_ATTEMPTS: Final = 5

#: Pause between those attempts, growing linearly. Nonzero so a pile-up of
#: writers is not spun on, and tiny because there is nothing to wait for:
#: the commit we lost to has already happened.
_SPECULATIVE_BACKOFF: Final = 0.01

#: Why a batch cannot carry identity keys. A batch is ONE multi-row INSERT
#: whose contract is "the ids, in the order given" -- and the identified
#: write resolves each conflict into an id to hand back, which a single
#: RETURNING cannot do for the rows it did not insert. Accepting the option
#: and silently dropping collided rows would break that contract in the way
#: hardest to notice: a shorter list, misaligned with the input. So it is
#: refused at the door, and the caller loops enqueue_identified().
_NO_BATCH_IDENTITY: Final = (
    "identity_key is not a batch option: a batch is one INSERT returning one "
    "id per row IN ORDER, and an identity that already exists has no row in "
    "it to return. Enqueue identified jobs one at a time with "
    "enqueue_identified(), which tells you which ones were already there."
)

#: Why a batch cannot carry debounce keys either, and it is a different
#: reason. A batch is a plain multi-row INSERT: it has no bounce statement in
#: front of it, so a key already held would not collapse -- the row would
#: simply violate jorb_debounce_idx and take the whole batch down with it.
#: Every guarantee debounce makes lives in JobClient.debounce()'s
#: bounce-or-insert pair, so the option is refused where it would silently
#: mean nothing.
_NO_BATCH_DEBOUNCE: Final = (
    "debounce_key is not a batch option: collapsing a burst is a "
    "bounce-or-insert pair of statements, and a batch is one INSERT with no "
    "bounce in front of it -- a key already held would fail the batch rather "
    "than collapse into the job holding it. Call debounce() per key."
)

#: Why the three enqueue-side keys cannot be combined. Each one answers "what
#: happens to a DUPLICATE enqueue?" and they answer it differently: an
#: identity_key hands back the existing job untouched, a deadline_key raises
#: and leaves it untouched, a debounce_key moves it later and rewrites its
#: arguments. A row carrying two of them would have to do two of those at
#: once, so the combination is a design error in the caller and is refused
#: loudly rather than resolved by whichever statement happens to run.
_KEYS_CONTRADICT: Final = (
    "debounce_key cannot be combined with {other}: they promise different "
    "things about a duplicate enqueue -- debounce_key defers the existing "
    "job and replaces its kwargs, identity_key returns it untouched, and "
    "deadline_key refuses the duplicate outright. Pick the one whose "
    "promise you want (docs/writing-jobs.md, 'Choosing your dedupe "
    "primitive')."
)

#: The same contradiction between the OTHER two keys, which the comment above
#: promised was mutually exclusive and which nothing enforced. A row carrying
#: both would have the identified statement resolve the conflict by handing the
#: existing job back while jorb_deadline_idx was meanwhile refusing duplicates
#: of the same work outright -- so which promise a caller got depended on which
#: index the row happened to collide with first.
_IDENTITY_AND_DEADLINE: Final = (
    "identity_key cannot be combined with deadline_key: they promise opposite "
    "things about a duplicate enqueue -- identity_key hands back the existing "
    "job (at most once, for the life of the row), deadline_key raises and then "
    "RE-ARMS the moment the job is claimed, so tomorrow's submission is a new "
    "job. Those cannot both be true of one row. Pick the one whose promise you "
    "want (docs/writing-jobs.md, 'Choosing your dedupe primitive')."
)

#: Why an identified enqueue cannot also carry a dependency edge.
#:
#: An identified enqueue's whole contract is that it may return a job it did
#: NOT create. That job has whatever dependency the enqueue which really made
#: it asked for -- a different upstream, a different group, or none at all, and
#: it may have finished months ago. So the caller's `waitfor_job=X` is silently
#: not applied: nothing raises, nothing waits, and the ordering the caller
#: asked for simply does not exist. Refused, because the failure is invisible
#: at the call site and shows up as work that ran too early.
_NO_IDENTITY_WAITFOR: Final = (
    "identity_key cannot be combined with waitfor_job/waitfor_group: an "
    "identified enqueue may return a job it did not create, and that job "
    "already has whatever dependency (or none) the enqueue that really made it "
    "asked for -- so this dependency would silently not be applied and the "
    "work would run unordered. Give the identity to the job that does the "
    "work and let an unidentified waiter depend on it, or key the identity to "
    "include the upstream."
)

#: Why a DAG node cannot carry an identity_key.
#:
#: A DAG node is enqueued and then WIRED: `execute()` rewrites dag_id and
#: run_group on the ids it just got back. An identity that already existed
#: hands back somebody else's job, so the wiring rewrites a PRE-EXISTING row --
#: taking it out of the DAG it belongs to and into this one, mid-flight, with
#: its old DAG left reporting a member it no longer has. Observed, not
#: theorised. There is nothing to resolve here: a graph is a set of jobs
#: created together, and a node that might already exist is not one of them.
_NO_DAG_IDENTITY: Final = (
    "identity_key is not a DAG node option: a DAG enqueues its nodes and then "
    "stamps dag_id and run_group onto the ids it got back, and an identity "
    "that already exists hands back a job this DAG did not create -- so the "
    "stamp would STEAL a live job out of its own DAG and rewire it into this "
    "one. Enqueue the identified job on its own and have the DAG depend on it."
)

#: Why the plain enqueue paths refuse a debounce_key, and it is the batch's
#: reason (see _NO_BATCH_DEBOUNCE) reached by a different door: enqueue() and
#: enqueue_in_transaction() run the plain INSERT, with no bounce statement in
#: front of it. A key already held therefore does not collapse -- it raises a
#: unique violation, which in the outbox case aborts the CALLER's transaction
#: and takes their application write with it -- and a key not yet held silently
#: writes a row with no ``debounce_deadline``, an uncapped collapse window that
#: nothing will ever clamp and that later bounces will defer forever.
#:
#: One constant for both because they are one statement: enqueue() IS
#: enqueue_in_transaction() on a pooled connection. Every guarantee the option
#: implies lives in JobClient.debounce()'s bounce-or-insert pair, which is what
#: the schema's own COMMENT on jorb.debounce_key has always said ("Set only by
#: JobClient.debounce()").
_NO_OUTBOX_DEBOUNCE: Final = (
    "debounce_key is not an enqueue() / enqueue_in_transaction() option: "
    "collapsing a burst is a bounce-or-insert pair of statements and these "
    "paths run the plain INSERT -- a key already held would raise instead of "
    "collapsing (aborting the caller's transaction, in the outbox case), and a "
    "key not yet held would open a collapse window with no cap to clamp it. "
    "Call debounce(key=..., period=..., cap=...), which owns that pair."
)

#: Longest ``app_version`` an enqueue accepts.
#:
#: A version string is a build identifier -- a tag, a git sha, a release date,
#: at worst all three -- and it is compared for EQUALITY by every claim on the
#: queue and carried in operator-facing messages that have to stay one line.
#: 128 characters is past every real one and short enough that neither is a
#: problem. Bounded at the door for the same reason ``partition_key`` is: past
#: the enqueue there is no caller left to tell.
MAX_APP_VERSION_LENGTH: Final = 128

#: Why an empty ``app_version`` is refused rather than stored.
#:
#: NULL is how a job says "not pinned", and it is the default. An empty string
#: is a DIFFERENT value that no worker can ever advertise (`pj --app-version
#: ""` is the same as passing nothing), so a row carrying one is pinned to a
#: version that cannot exist -- unclaimable forever, and reported as wanting
#: version ''. It is almost always a variable that came back empty: an unset
#: ``$GIT_SHA``, a build stamp the CI step did not write. Refused here, where
#: the caller is still around to hear about it.
_EMPTY_APP_VERSION: Final = (
    "app_version is empty: NULL/None is how a job says it is not pinned to a "
    "code version (and is the default), while '' would pin it to a version no "
    "worker can advertise -- the job would sit 'queued' forever. This is "
    "usually an unset build variable; omit the argument to enqueue unpinned "
    "work."
)


def validate_app_version(app_version: str | None) -> str | None:
    """Check an ``app_version`` and return it, or None for unpinned work.

    One home for the two ways a version pin goes wrong before it is written --
    empty (a build variable that came back blank, pinning the job to a version
    nothing can advertise) and unbounded (a string in the claim's equality
    test and in every message about the job) -- so the enqueue paths and
    ``update_job_app_version`` refuse the same values with the same words.
    """
    if app_version is None:
        return None
    if not app_version.strip():
        raise ValueError(_EMPTY_APP_VERSION)
    if len(app_version) > MAX_APP_VERSION_LENGTH:
        raise ValueError(
            f"app_version is {len(app_version)} characters, above the "
            f"{MAX_APP_VERSION_LENGTH} the platform accepts: it names a BUILD "
            f"(a tag, a sha, a release stamp), is compared for equality by "
            f"every claim on the queue, and is printed in the messages that "
            f"say why a job is not running"
        )
    return app_version


#: Longest any caller-chosen key an enqueue accepts may be, and the shortest.
#:
#: One bound for all four (deadline_key, identity_key, debounce_key,
#: partition_key) because they are the same KIND of thing: a name the caller
#: chose, stored in a column something INDEXES or GROUPS BY, never a payload.
#: partition_key documented the reasoning first (MAX_PARTITION_KEY_LENGTH,
#: which this unifies) and the argument transfers unchanged: an unbounded key
#: is an unbounded string in a btree the enqueue path writes and the claim path
#: reads. 256 characters is far past every real one -- an order id, a tenant, a
#: date-stamped digest name, a ULID -- and short enough that a saturated queue's
#: worth of them is still small.
MAX_KEY_LENGTH: Final = 256

#: Longest ``partition_key`` an enqueue accepts.
#:
#: A partition key is a GROUPING KEY read inside the serialised claim
#: section, not a payload: on a queue with ``partition_limits`` every
#: saturated lane's key is carried in an array that the claim's per-row test
#: probes, so an unbounded key would put an unbounded string into the one
#: critical section that sets a capped queue's whole ceiling. Refused at the
#: door, where the caller can still be told, rather than accepted and paid for
#: on every claim forever.
#:
#: The SAME bound as every other caller-chosen key, and named separately only
#: because the name is public API; :data:`MAX_KEY_LENGTH` is where the number
#: and the reasoning live.
MAX_PARTITION_KEY_LENGTH: Final = MAX_KEY_LENGTH


def validate_key(name: str, value: str | None) -> str | None:
    """Check one caller-chosen key column and return it, or None if unset.

    THE one validator for deadline_key, identity_key, debounce_key and
    partition_key, so no key can be refused on one path and accepted on
    another. None means "not using this feature" and is always fine; anything
    else has to be a name, which means non-empty and bounded.

    An EMPTY key is refused rather than stored because it is not the same thing
    as no key at all and behaves nothing like it: `''` is a real value, so it
    takes a slot in that column's unique index and every OTHER caller who
    passed an empty key collides with it -- unrelated jobs deduplicating
    against each other, or (for partition_key) sharing one fair-share lane
    while the NULL lane sits beside them. It is almost always a variable that
    came back blank: an f-string over a missing id, a config value the
    deployment did not set. Refused here, where the caller is still around to
    hear about it.
    """
    if value is None:
        return None
    if not value.strip():
        raise ValueError(
            f"{name} is empty: None is how a job says it is not using this "
            f"feature (and is the default), while '' is a real key -- it takes "
            f"a slot in that column's index, so every other caller who passed "
            f"an empty {name} would collide with this job. This is usually an "
            f"f-string over a value that was missing; omit the argument "
            f"instead."
        )
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError(
            f"{name} is {len(value)} characters, above the {MAX_KEY_LENGTH} the "
            f"platform accepts: it is a NAME the caller chose, stored in a "
            f"column the enqueue path indexes and the claim path reads, not a "
            f"payload — key it to an id, a tenant or a date stamp rather than "
            f"to the data itself"
        )
    return value


#: Why a debounced job cannot also wait on something. `waitfor_job` /
#: `waitfor_group` insert the row as 'waiting', and jorb_debounce_idx covers
#: QUEUED rows only -- so the key would not be held, no duplicate would ever
#: find the row to collapse onto, and every call in the burst would write
#: another job. Refused rather than silently degrading to no debouncing at all.
_NO_DEBOUNCE_WAITFOR: Final = (
    "debounce_key cannot be combined with waitfor_job/waitfor_group: a "
    "dependent job is inserted 'waiting', and the collapse window is held by "
    "a QUEUED row -- so nothing would ever collapse and every call would "
    "write another job. Debounce the work that runs after the wait instead."
)


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


#: The projection that fills a :class:`JobInfo`, spelled once. Every column
#: here is a field of the dataclass and every field is a column here --
#: ``JobInfo(**dict(row))`` is the constructor, so the two lists have to match
#: exactly or the call raises. Written twice (get_job, get_job_by_identity) it
#: was two chances to add a field and update one of them.
_JOB_INFO_SELECT: Final = (
    "SELECT id, job_class, queue, prio as priority, state, created FROM jorb"
)


@dataclass
class JobHandle:
    """A job id paired with the client that enqueued it.

    Returned by JobClient.enqueue_handle() (plain enqueue() still returns a
    bare int for simple use). Every method delegates to the client, so a
    handle stays valid for the job's whole life — retries keep the same id.
    """

    id: int
    client: JobClient

    async def status(self) -> str | None:
        """Current state, or None if the row no longer exists."""
        info = await self.client.get_job(self.id)
        return info.state if info else None

    async def result(self, timeout: float | None = None) -> Any:
        """WAIT for the result — the same contract as MachineHandle.result(),
        so `await handle.result()` means one thing across both handle kinds.
        For a non-blocking peek that returns None until the job finishes, use
        get_job_result()."""
        return await self.client.wait_for_result(self.id, timeout=timeout)

    #: The two spellings are ONE method: `wait()` reads better at a call site
    #: that ignores the value, `result()` at one that uses it, and a second
    #: body would be a second thing to keep in step.
    wait = result

    async def cancel(self) -> dict[str, Any]:
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
        super().__init__(
            f"machine {job_id} is in {state!r}, which has no transition for "
            f"{event!r}"
            + (f"; it accepts {accepted}" if accepted else " (a final state)"),
            job_id,
        )
        self.state = state
        self.event = event
        self.accepted = accepted


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
                f"which is not a machine state",
                job_id=self.id,
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

    async def cancel(self) -> dict[str, Any]:
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
        async with await JobClient.from_config('./pyjobby.toml') as client:
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
        app_version: str | None = None,
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
            app_version: the APPLICATION code version to stamp on every
                enqueue through this client (default: None -- unpinned work,
                claimable by any worker). A stamped job is claimed ONLY by a
                worker advertising the same version (`pj --app-version`), so
                declaring it here pins this deployment's work to matching
                code. Per-call `app_version=` overrides it; a deployment that
                wants MOST work unpinned leaves this unset and pins the
                individual jobs instead.

        Note: Use JobClient.create() or JobClient.from_config() instead
        """
        self.pool = pool
        self.prio_ceiling = prio_ceiling
        self.app_version = validate_app_version(app_version)
        self._closed = False
        # A pool handed to the constructor belongs to the CALLER — a web app
        # routinely shares one pool between its ORM and this client, and
        # close() closing it would take the whole process's database access
        # down with one client. The create()/from_config() constructors set
        # this True for the pools they build themselves.
        self._owns_pool = False
        self._polling_reported = False
        self._db_params = db_params
        self._listener_conn: asyncpg.Connection | None = None
        self._listener_lock = asyncio.Lock()
        # waiters keyed by job id ('jorb_done') / (job_id, key) ('jorb_event',
        # 'jorb_stream')
        self._done_waiters: dict[int, list[asyncio.Event]] = {}
        self._event_waiters: dict[tuple[int, str], list[asyncio.Event]] = {}
        self._stream_waiters: dict[tuple[int, str], list[asyncio.Event]] = {}

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
        app_version: str | None = None,
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
            app_version: code version to stamp on every enqueue through this
                client (default: None — unpinned). See JobClient.__init__.
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
        client = cls(
            pool,
            db_params=db_params,
            prio_ceiling=prio_ceiling,
            app_version=app_version,
        )
        client._owns_pool = True
        return client

    @classmethod
    async def from_config(
        cls,
        config_path: str,
        min_size: int = 5,
        max_size: int = 20,
        prio_ceiling: int | None = None,
        app_version: str | None = None,
    ) -> JobClient:
        """
        Create client from pyjobby config file.

        Args:
            config_path: Path to pyjobby.toml
            min_size: Minimum pool size (default: 5)
            max_size: Maximum pool size (default: 20)
            prio_ceiling: this fleet's worker priority ceiling
                (`pj --max-prio`); enqueueing above it is refused. See
                JobClient.__init__. Left unset (the default), the config
                file's own ``prio_ceiling`` is used, and 1000 if the file
                does not declare one — the ceiling is a deployment fact,
                declared once in the file every daemon already reads.
            app_version: code version to stamp on every enqueue through this
                client. Left unset (the default), the config file's own
                ``app_version`` is used, and None if the file does not declare
                one. Declared in that file for the same reason the ceiling is,
                and it is the SAME key ``pj --app-version`` defaults to — the
                two halves of a version pin have to agree, so they read one
                string from one place.

        Raises:
            ConfigError: the file declares no db_params. Falling back to
                asyncpg's environment defaults would connect to whatever
                PGHOST/PGDATABASE happen to say — a DIFFERENT database
                than the one the operator wrote down, discovered later.

        Returns:
            JobClient instance

        Example:
            client = await JobClient.from_config('./pyjobby.toml')
        """
        from .configloader import ConfigError, load_config_from_file

        config = load_config_from_file(
            config_path, keys=["db_params", "prio_ceiling", "app_version"]
        )
        db_params = config.get("db_params")
        if not db_params:
            raise ConfigError(f"No db_params found in config file: {config_path}")

        # `is not None`, not `or`: an explicit prio_ceiling of 0 (a ceiling
        # admitting only prio-0 work) is a real value, not "unset".
        if prio_ceiling is None:
            configured = config.get("prio_ceiling")
            prio_ceiling = (
                DEFAULT_PRIO_CEILING if configured is None else int(configured)
            )

        if app_version is None:
            app_version = config.get("app_version")

        pool = await db.create_pool(min_size=min_size, max_size=max_size, **db_params)
        client = cls(
            pool,
            db_params=db_params,
            prio_ceiling=prio_ceiling,
            app_version=app_version,
        )
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
        identity_key: str | None = None,
        partition_key: str | None = None,
        app_version: str | None = None,
        admin_data: dict[str, Any] | None = None,
        tags: dict[str, Any] | None = None,
        # result storage & passing
        save_result: bool = True,
        use_result_from: int | None = None,
        # retry strategy
        retry_strategy: str = DEFAULT_RETRY_STRATEGY,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_retry_delay: int = DEFAULT_INITIAL_RETRY_DELAY,
        max_retry_delay: int = DEFAULT_MAX_RETRY_DELAY,
        # timeout enforcement
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
            deadline_key: Idempotency key that collapses duplicate
                submissions of work that has not started: one QUEUED row per
                (deadline_key, queue), so a second enqueue while the first is
                still queued raises UniqueViolationError — and the key
                RE-ARMS once the job is claimed (default: None)
            identity_key: This exact work happens AT MOST ONCE. Unique across
                every state, so if a job with this key already exists — queued,
                running, finished, crashed — the enqueue returns THAT job's id
                instead of writing a second row, and never raises. Bounded by
                retention: reaping the terminal row frees the key, so scope
                keys to a time you can name (an order id, a date stamp) if you
                need uniqueness beyond `--retention-days`. Use
                enqueue_identified() when you must know which of the two
                happened (default: None)
            partition_key: The FAIR-SHARE LANE this job belongs to — a
                tenant, an account, an api key. Inert unless the job's queue
                has `partition_limits` set (`pj-admin queues limits QUEUE
                --partition-limits`), and on such a queue the queue's
                max_concurrency and rate_limit are counted PER lane, so one
                tenant cannot starve the rest. Jobs with no key form ONE lane
                of their own: never hidden, never refused for being
                unlabelled. Inherited by a fork, like uid and tags. Max
                MAX_PARTITION_KEY_LENGTH characters (default: None)
            app_version: PIN this job to a code version: only a worker
                advertising the same `pj --app-version` will claim it, and it
                stays 'queued' while none is running. Default None means this
                CLIENT's declared version (JobClient(pool, app_version=...) or
                the config file's `app_version`), and unpinned when the client
                declared none — an unpinned job is claimed by every worker,
                versioned ones included. For a rolling deploy that must not
                resume a job's checkpoints on new code. NOT inherited by a
                fork unless the fork asks; kept across retry and rerun, which
                re-execute the same row. Max MAX_APP_VERSION_LENGTH
                characters; '' is refused (default: None)
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
            retry_strategy: 'exponential', 'linear', 'quadratic',
                'fibonacci', 'fixed' — anything else is refused (an unknown
                strategy would silently fall back to exponential)
            max_retries: Maximum retry attempts (default: 10)
            initial_retry_delay: Starting retry delay in seconds (default: 1)
            max_retry_delay: Maximum retry delay cap (default: 3600)
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
            Job ID — of the job this call created, or, when identity_key
            names a job that already exists, of that one.

        Raises:
            asyncpg.UniqueViolationError: If deadline_key already exists
            ValueError: If both waitfor_job and waitfor_group specified, if
                on_timeout is neither 'retry' nor 'fail', if priority is
                above this client's worker priority ceiling, if tags are
                not a flat dict of string keys to scalar values, if
                partition_key is longer than MAX_PARTITION_KEY_LENGTH, or if
                identity_key names an existing job of a DIFFERENT job_class

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

            # At-most-once work: every call returns the SAME job id for as
            # long as retention keeps the row, whatever state it is in
            job_id = await client.enqueue(
                'myapp.jobs.ShipOrder',
                identity_key=f'order:{order_id}:ship',
                order_id=order_id
            )

            # Pipeline with result passing
            job1 = await client.enqueue('FetchData', url='...', save_result=True)
            job2 = await client.enqueue('ProcessData', waitfor_job=job1, use_result_from=job1)

            # Job with timeout and exponential backoff
            job_id = await client.enqueue(
                'ApiCall',
                timeout_seconds=30,
                retry_strategy='exponential',
                max_retries=15,
                on_timeout='retry'
            )
        """
        async with self.pool.acquire() as conn:
            try:
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
                    identity_key=identity_key,
                    partition_key=partition_key,
                    app_version=self._app_version(app_version),
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
            except asyncpg.UndefinedTableError as e:
                raise self._unmigrated_database_error() from e

    def _unmigrated_database_error(self) -> RuntimeError:
        """Turn asyncpg's `relation "jorb" does not exist` into an answer.

        The first thing an application does against a database nobody
        migrated is enqueue, and the driver's message is true and useless --
        it names no database and no fix. Say both; the caller chains the
        original so the traceback keeps the SQL detail.

        Worded for any verb, not just enqueue: every client method that touches
        a jorb table routes its UndefinedTableError through here (enqueue,
        enqueue_identified, debounce, the pipeline, the fork pair), and the
        answer is the same one for all of them.
        """
        from . import migrations
        from .configloader import describe_db_target

        return RuntimeError(
            f"The pyjobby schema is not installed in "
            f"{describe_db_target(self._db_params)}: {migrations.SCHEMA_REMEDY}"
        )

    def _app_version(self, override: str | None) -> str | None:
        """The version to stamp on one enqueue: the call's, else this client's.

        The same precedence rule as `prio_ceiling`, and it exists as a method
        because every enqueue path has to apply it identically -- a path that
        forgot would silently write UNPINNED work from a client whose whole
        purpose is pinning, and nothing downstream could tell that apart from
        a deliberate choice.
        """
        return self.app_version if override is None else override

    async def enqueue_identified(
        self, job_class: str, *, identity_key: str, **options: Any
    ) -> tuple[int, bool]:
        """Enqueue at-most-once work and say whether THIS call created it.

        Same job, same keyword arguments and same outcome as
        ``enqueue(..., identity_key=...)`` — the only difference is the
        return shape: ``(job_id, created)``, where ``created`` is False when
        the id belongs to a job that already existed. Plain enqueue stays a
        bare int because most callers do not care which of the two happened;
        this exists for the ones that genuinely cannot tell otherwise, since
        the id alone never says (both calls return the same one).

        `created` is a fact about this call, not about the job: exactly one
        of two racing callers gets True, and the other's False is the
        truthful answer even though its INSERT and the winner's were in
        flight at the same time.

        Example:
            job_id, created = await client.enqueue_identified(
                'myapp.jobs.ShipOrder',
                identity_key=f'order:{order_id}:ship',
                order_id=order_id,
            )
            if not created:
                log.info("shipment %s was already under way as job %s",
                         order_id, job_id)
            result = await client.wait_for_result(job_id)
        """
        # enqueue()'s ceiling and version rules, applied here too: this
        # client's declared worker ceiling and code version unless the call
        # overrides either for itself.
        ceiling = options.pop("prio_ceiling", None)
        options["prio_ceiling"] = self.prio_ceiling if ceiling is None else ceiling
        options["app_version"] = self._app_version(options.pop("app_version", None))
        async with self.pool.acquire() as conn:
            try:
                return await self._enqueue_row(
                    conn, job_class, identity_key=identity_key, **options
                )
            except asyncpg.UndefinedTableError as e:
                raise self._unmigrated_database_error() from e

    async def debounce(
        self,
        job_class: str,
        *,
        key: str,
        period: float,
        cap: float | None = None,
        **options: Any,
    ) -> tuple[int, bool]:
        """Collapse a burst of equivalent enqueues onto ONE job that runs
        once the burst stops.

        The first call with a quiet ``key`` enqueues an ordinary job parked
        ``period`` seconds in the future — a queued row with a future
        ``run_after``, which is the same durable-sleep the claim path already
        implements, so nothing about claiming changes. Every call after it
        while that row is still queued BOUNCES it: ``run_after`` moves to now
        + ``period`` and the row's kwargs are REPLACED with this call's.

        Returns ``(job_id, created)`` — the same shape as
        enqueue_identified(), and ``created`` means the same thing: this call
        wrote the row, rather than joining the window somebody else opened.

        LAST WRITER WINS, and that is the feature. The collapsed job runs
        with the freshest arguments, which is right for "re-index document
        7", "recompute cart 991's totals", "the config changed, reload" — and
        wrong for anything whose arguments must be the ones first submitted.
        That work wants ``deadline_key``.

        ``period`` RESTATES the wait rather than extending it: it is the
        caller's current quiet window, so a bounce that asks for a shorter
        period pulls the job in. Callers in a fleet that disagree about the
        period therefore each get their own answer for as long as they keep
        bouncing, and the last one to bounce decides when it fires.

        ``cap`` bounds the collapse. Without it, a key bounced faster than
        its own period is deferred forever — a legitimate choice for work
        that is worthless until the burst really stops, and a starvation bug
        otherwise. With it, ``run_after`` never moves past the FIRST call's
        now + ``cap``: the ceiling is written to the row, so bounces from
        other processes respect it even though they never saw the cap. The
        first caller sets it; later ones passing a different ``cap`` do not
        change the window they joined.

        THE KEY IS RELEASED AT THE CLAIM, not at the enqueue. A worker taking
        the job frees the key, so a burst arriving while it runs opens a new
        window and the two rows coexist. A duplicate arriving after
        ``run_after`` has passed but before any worker claimed the row still
        collapses onto it: the work has not started.

        Accepts the rest of enqueue()'s options and the job's kwargs.
        ``identity_key``, ``deadline_key`` and ``waitfor_job``/
        ``waitfor_group`` are refused — the first two promise something else
        about a duplicate, and a 'waiting' row does not hold the key.

        Example:
            # one re-index per document, fired 5s after the edits stop, and
            # never more than 30s after the first one
            job_id, created = await client.debounce(
                "myapp.jobs.ReindexDocument",
                key=f"reindex:{doc_id}",
                period=5.0,
                cap=30.0,
                doc_id=doc_id,
                revision=revision,   # the LAST revision is the one indexed
            )
        """
        if period <= 0:
            raise ValueError(
                f"period must be a positive number of seconds, got {period!r}: "
                f"a debounce with no quiet window collapses nothing"
            )
        if cap is not None and cap <= 0:
            raise ValueError(
                f"cap must be a positive number of seconds, got {cap!r}: "
                f"pass None for a collapse window with no ceiling"
            )

        # enqueue()'s ceiling and version rules, applied here too (see
        # enqueue_identified). A collapsed burst is one job like any other, and
        # it carries the pin the client declared; the bounce never rewrites it,
        # because the version belongs to the row the window opened, not to
        # whichever call bounced it last.
        ceiling = options.pop("prio_ceiling", None)
        options["prio_ceiling"] = self.prio_ceiling if ceiling is None else ceiling
        options["app_version"] = self._app_version(options.pop("app_version", None))

        # ONE clock for both halves. The bounce compares its new run_after
        # against a debounce_deadline some earlier call computed, and the
        # insert writes both from here -- so they are the same clock as every
        # other run_after in the platform (the caller's), and the database's
        # now() appears nowhere in the comparison.
        now = datetime.now(UTC)
        deadline = now + timedelta(seconds=cap) if cap is not None else None
        fires_at = now + timedelta(seconds=period)
        # A cap below the period is not an error: the ceiling is what the
        # caller asked to be bounded by, so the very first parking already
        # honours it rather than overshooting once and then clamping.
        if deadline is not None and deadline < fires_at:
            fires_at = deadline

        args = self.build_enqueue_row(
            job_class,
            run_after=fires_at,
            debounce_key=key,
            debounce_deadline=deadline,
            **options,
        )
        async with self.pool.acquire() as conn:
            try:
                return await self._debounce_on(conn, args, job_class, key, fires_at)
            except asyncpg.UndefinedTableError as e:
                raise self._unmigrated_database_error() from e

    @staticmethod
    async def _debounce_on(
        conn: asyncpg.Connection,
        args: list[Any],
        job_class: str,
        key: str,
        fires_at: datetime,
    ) -> tuple[int, bool]:
        """Bounce the row holding ``key``, or open the window ourselves.

        The loop is here for the reason _enqueue_identity's is, and it is the
        same reason: ENQUEUE_DEBOUNCED_SQL is a speculative insert, and a
        speculative insert that WAITED for the conflicting transaction
        answers with no row at all — the winner committed after this
        statement's snapshot was taken. Re-asking takes a fresh snapshot, and
        on the next pass the bounce finds the row that was invisible. Both
        loops therefore converge only under READ COMMITTED; the budget is a
        backstop against an unbroken stream of writers, not a wait.

        Two statements rather than one because the common case is the bounce
        and it costs one round trip. The gap between them is exactly the race
        the loop exists for, and it is not a new one: it is the same window
        the insert would have to survive anyway.
        """
        for attempt in range(_SPECULATIVE_ATTEMPTS):
            held = await conn.fetchrow(
                DEBOUNCE_BOUNCE_SQL, key, fires_at, args[_ROW_KWARGS], job_class
            )
            if held is not None:
                if held["fires_at"] is not None:
                    return held["id"], False
                # The row holding the key is real but was not bounced. Either
                # it is somebody else's job class -- a caller error, and the
                # only one this verb can detect -- or a worker claimed it out
                # from under the UPDATE, which is not an error at all: the key
                # is free now, so fall through and open the next window.
                if held["job_class"] != job_class:
                    raise ValueError(
                        f"debounce key {key!r} already names job "
                        f"{held['id']}, which is a {held['job_class']} — not "
                        f"the requested {job_class}. A debounce key names one "
                        f"burst of one kind of work, and bouncing this row "
                        f"would leave it running the other class's arguments."
                    )

            created = await conn.fetchrow(ENQUEUE_DEBOUNCED_SQL, *args)
            if created is not None:
                return created["id"], True
            await asyncio.sleep(_SPECULATIVE_BACKOFF * (attempt + 1))

        raise RuntimeError(
            f"debounce key {key!r} was claimed by another transaction after "
            f"each of {_SPECULATIVE_ATTEMPTS} attempts' snapshots, so this "
            f"call could neither bounce the collapse window nor open one. "
            f"Nothing was written. Either an unbroken stream of writers is "
            f"opening and claiming this one window, or this call is running "
            f"at REPEATABLE READ or higher, where a retry reuses the "
            f"transaction's snapshot and can never see the row."
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

    async def run(
        self, job_class: str, timeout: float | None = None, **options: Any
    ) -> Any:
        """Enqueue a job and wait for its result — request/response in one
        call. Same keyword arguments as enqueue(); raises exactly what
        wait_for_result() raises (JobFailedError, JobCancelledError,
        TimeoutError).

        ``timeout`` is how long THIS CALL waits, not the job's execution
        deadline -- for that, pass ``timeout_seconds`` (an enqueue option) in
        ``options``. They are different clocks: the wait can give up while the
        job runs on. When the wait DOES give up (TimeoutError), the job is
        best-effort cancelled before the error propagates, so an abandoned
        request/response call does not leave work running unattended.

        Example:
            # give up waiting after 60s; the job itself may run up to 120s
            report = await client.run(
                'myapp.jobs.Report', day='mon', timeout=60, timeout_seconds=120
            )
        """
        job_id = await self.enqueue(job_class, **options)
        try:
            return await self.wait_for_result(job_id, timeout=timeout)
        except TimeoutError, asyncio.CancelledError:
            # The caller has stopped waiting; do not orphan the job. This
            # catches BOTH give-up paths: our own timeout, and the ordinary
            # async abandonments -- asyncio.timeout()/wait_for around this
            # call, or the whole task cancelled on client disconnect -- which
            # arrive as CancelledError. The cancel RPC is shielded so the
            # in-flight cancellation cannot kill the cleanup it triggered,
            # and BOUNDED by _RUN_CANCEL_TIMEOUT so the cleanup cannot
            # outlive the failure it is tidying up after.
            #
            # BaseException, not Exception: a second cancellation arriving
            # while this handler runs raises CancelledError, which
            # suppress(Exception) let through -- replacing the caller's
            # TimeoutError with a CancelledError they were never told to
            # expect, and losing the cleanup as well. Everything the cleanup
            # can raise (including its own timeout, which cancels the shield's
            # waiter and leaves the shielded cancel running) is swallowed
            # here, and the bare `raise` re-raises the ORIGINAL exception:
            # the caller always sees the one this method documents.
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(
                    asyncio.shield(self.cancel_job(job_id)),
                    timeout=_RUN_CANCEL_TIMEOUT,
                )
            raise

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
        raised ceiling passes `prio_ceiling=` with the call. For the same
        reason there is no declared `app_version` to inherit: this path pins
        nothing unless the call names `app_version=` itself. Called through a
        client (`client.enqueue_in_transaction(...)`) it is still the static
        method, so the client's version does NOT apply -- pass it, or use
        `enqueue()` when the transaction is not the caller's.

        ``debounce_key`` is REFUSED here (see _NO_OUTBOX_DEBOUNCE): this path
        runs the plain INSERT with no bounce statement in front of it, so a key
        already held would abort the caller's whole transaction rather than
        collapse, and one not yet held would open a window with no cap. The
        refusal covers ``enqueue()`` as well, which is this method on a pooled
        connection and has exactly the same two failures.
        ``identity_key`` IS accepted and does what it does everywhere: the
        identified statement runs inside the caller's transaction, returns the
        existing job's id when the key is already held, and discards the row it
        would have created. The retry loop it may enter therefore re-runs inside
        that transaction -- which is fine at READ COMMITTED (each attempt is a
        new statement snapshot) and cannot converge above it, exactly as the
        loop's own docstring says.

        Example:
            async with conn.transaction():
                await conn.execute("INSERT INTO orders ...")
                job_id = await JobClient.enqueue_in_transaction(
                    conn, 'myapp.jobs.FulfillOrder', order_id=42
                )
        """
        if options.get("debounce_key") is not None:
            raise ValueError(_NO_OUTBOX_DEBOUNCE)
        job_id, _ = await JobClient._enqueue_row(conn, job_class, **options)
        return job_id

    @staticmethod
    async def _enqueue_row(
        conn: asyncpg.Connection, job_class: str, **options: Any
    ) -> tuple[int, bool]:
        """THE single-row write behind every enqueue path, and the one place
        that knows an identified enqueue is a different statement.

        Returns ``(job_id, created)``. Without an identity_key `created` is
        always True — an ordinary enqueue writes a row or raises — and every
        caller but enqueue_identified() throws it away.

        The unidentified path is left EXACTLY as it was, a bare INSERT ...
        RETURNING id, because it is the hot one: identity costs a CTE, a
        conflict-inferring index probe and a second branch, and no job that
        does not ask for identity should pay any of it.
        """
        identity_key = options.get("identity_key")
        args = JobClient.build_enqueue_row(job_class, **options)
        if identity_key is None:
            job_id: int = await conn.fetchval(ENQUEUE_SQL, *args)
            return job_id, True
        return await JobClient._enqueue_identity(conn, args, job_class, identity_key)

    @staticmethod
    async def _enqueue_identity(
        conn: asyncpg.Connection,
        args: list[Any],
        job_class: str,
        identity_key: str,
    ) -> tuple[int, bool]:
        """Claim ``identity_key`` for this row, or return the job that holds it.

        The loop is the whole reason this is a method and not a fetchrow.
        ENQUEUE_IDENTIFIED_SQL answers in one statement EXCEPT when it loses
        a race it had to wait out: it blocks on the conflicting transaction,
        that transaction commits, and the row is then newer than this
        statement's snapshot -- so it returns nothing at all rather than a
        wrong answer. Each attempt is a new statement and therefore a new
        snapshot (READ COMMITTED, the default and what every pyjobby
        connection runs at), which is why the retry sees what the first
        attempt could not.

        A caller running at REPEATABLE READ would loop without converging,
        because the snapshot is the transaction's rather than the
        statement's. That is the same isolation level at which a plain
        enqueue's UniqueViolationError could not be retried either, and the
        bounded budget turns it into an error rather than a hang.

        THE CLASS CHECK IS AFTER THE RACE, not before it: it reads the
        job_class the winning row actually has, so two callers disagreeing
        about what an identity means are BOTH told, whichever of them
        inserted. Checking a row read beforehand would let the loser insert
        against a row that no longer says what it read.
        """
        for attempt in range(_SPECULATIVE_ATTEMPTS):
            row = await conn.fetchrow(ENQUEUE_IDENTIFIED_SQL, *args)
            if row is not None:
                held: str = row["job_class"]
                if held != job_class:
                    raise ValueError(
                        f"identity_key {identity_key!r} already names job "
                        f"{row['id']}, which is a {held} — not the requested "
                        f"{job_class}. An identity names ONE piece of work, so "
                        f"this is either the wrong key for this job or the "
                        f"wrong job for this key; the platform will not "
                        f"silently hand back a job of the other class."
                    )
                return row["id"], row["created"]
            await asyncio.sleep(_SPECULATIVE_BACKOFF * (attempt + 1))
        raise RuntimeError(
            f"identity_key {identity_key!r} was claimed by another "
            f"transaction after each of {_SPECULATIVE_ATTEMPTS} attempts' "
            f"snapshots, so this call can neither create the job nor name the "
            f"one that holds it. Nothing was written. Either an unbroken "
            f"stream of writers is claiming this one identity, or this "
            f"enqueue is running at REPEATABLE READ or higher, where a retry "
            f"reuses the transaction's snapshot and can never see the row."
        )

    @staticmethod
    def build_enqueue_row(
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
        identity_key: str | None = None,
        debounce_key: str | None = None,
        debounce_deadline: datetime | None = None,
        partition_key: str | None = None,
        app_version: str | None = None,
        admin_data: dict[str, Any] | None = None,
        tags: dict[str, Any] | None = None,
        save_result: bool = True,
        use_result_from: int | None = None,
        retry_strategy: str = DEFAULT_RETRY_STRATEGY,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_retry_delay: int = DEFAULT_INITIAL_RETRY_DELAY,
        max_retry_delay: int = DEFAULT_MAX_RETRY_DELAY,
        timeout_seconds: int | None = None,
        on_timeout: str = "retry",
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
        job_kwargs: dict[str, Any] | None = None,
        schedule_id: int | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        """Validate enqueue options and build the parameter row for
        ENQUEUE_SQL — the single construction path shared by enqueue(),
        enqueue_batch(), enqueue_in_transaction() and the scheduler.

        The job's payload arrives one of two ways: as the leftover **kwargs
        (enqueue()'s historical shared namespace), or explicitly as
        ``job_kwargs`` — which keeps payload and options in separate
        namespaces, so a payload key named like an option is delivered
        instead of colliding. When ``job_kwargs`` is given, leftover
        **kwargs can only be misspelled options and are refused by name.

        ``prio_ceiling`` is the fleet's worker ceiling; enqueue() passes the
        client's, and the static/outbox path (which has no client) gets the
        platform default. See validate_priority.

        ``app_version`` is taken as given: this is a static method, so the
        client's declared version has already been resolved by whichever
        enqueue path called it (see JobClient._app_version). A caller reaching
        this directly -- the scheduler does -- therefore enqueues UNPINNED work
        unless it names a version itself, which is what schedules want."""
        if job_kwargs is not None and kwargs:
            raise ValueError(
                f"unknown enqueue options: {sorted(kwargs)} — with a "
                f"kwargs dict provided, job arguments go in it and options "
                f"are passed by name"
            )
        if waitfor_job and waitfor_group:
            raise ValueError("Cannot specify both waitfor_job and waitfor_group")

        # The three enqueue-side keys, checked HERE rather than in debounce()
        # because this is the one construction path every writer goes
        # through: a row that reached the INSERT carrying two of them would
        # have been assembled somewhere this check is not.
        if debounce_key is not None:
            conflicting = [
                name
                for name, value in (
                    ("identity_key", identity_key),
                    ("deadline_key", deadline_key),
                )
                if value is not None
            ]
            if conflicting:
                raise ValueError(
                    _KEYS_CONTRADICT.format(other=" and ".join(conflicting))
                )
            if waitfor_job or waitfor_group:
                raise ValueError(_NO_DEBOUNCE_WAITFOR)
        elif debounce_deadline is not None:
            raise ValueError(
                "debounce_deadline without debounce_key: the cap bounds a "
                "collapse window, and there is no window without a key"
            )

        # ...and the same completeness for identity_key, whose exclusivity the
        # comment on _KEYS_CONTRADICT has always claimed and which nothing
        # checked. Beside the debounce arm above, not in enqueue_identified():
        # the scheduler, the batch, the outbox path and the DAG all build rows
        # through here and none of them goes through that method.
        if identity_key is not None:
            if deadline_key is not None:
                raise ValueError(_IDENTITY_AND_DEADLINE)
            if waitfor_job or waitfor_group:
                raise ValueError(_NO_IDENTITY_WAITFOR)

        # Every caller-chosen key, bounded and non-empty by ONE rule. Here
        # because here is the last place a caller exists to be told: past this
        # point the key is a row, and its cost is paid by whatever reads it --
        # a claim, an index probe -- rather than by the enqueue that wrote it.
        validate_key("deadline_key", deadline_key)
        validate_key("identity_key", identity_key)
        validate_key("debounce_key", debounce_key)
        validate_key("partition_key", partition_key)

        # Here for the same reason, and it is the ONE place every writer's
        # version pin is checked: the pool enqueue, the caller's transaction,
        # the batch and the scheduler all build their rows through this.
        app_version = validate_app_version(app_version)

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

        # Validate the MERGED value, not just the on_timeout parameter: a
        # caller who passed admin_data={"on_timeout": "typo"} bypassed the
        # parameter check above, and the worker treats anything but 'retry'
        # as terminal -- so the very "typo dead-letters silently" the
        # parameter check exists to prevent still got through.
        if admin_data["on_timeout"] not in _ON_TIMEOUT_POLICIES:
            raise ValueError(
                f"admin_data['on_timeout'] must be one of "
                f"{sorted(_ON_TIMEOUT_POLICIES)}, got "
                f"{admin_data['on_timeout']!r} — the worker treats anything "
                f"that is not 'retry' as 'fail', so a typo dead-letters silently"
            )

        # Same rationale as on_timeout: calculate_retry_delay treats any
        # unrecognised strategy as exponential, so a typo'd strategy would be
        # accepted here and silently produce the wrong backoff forever.
        merged_strategy = admin_data.get("retry_strategy", retry_strategy)
        if merged_strategy not in _RETRY_STRATEGIES:
            raise ValueError(
                f"retry_strategy must be one of {sorted(_RETRY_STRATEGIES)}, "
                f"got {merged_strategy!r} — an unknown strategy would fall "
                f"back to exponential silently"
            )

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
            schedule_id,
            identity_key,
            debounce_key,
            debounce_deadline,
            partition_key,
            app_version,
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
        strategy, timeout policy, tags, deadline_key, capability — all of it
        applies, so converting a loop of enqueue() calls into a batch does
        not change what the jobs mean.

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

        rows = self._build_batch_rows(jobs, prio_ceiling, **options)
        async with self.pool.acquire() as conn:
            return await self._insert_batch_rows(conn, rows)

    def _build_batch_rows(
        self, jobs: list[tuple[Any, ...]], prio_ceiling: int | None, **options: Any
    ) -> list[list[Any]]:
        """Validate a batch and build one ENQUEUE_BATCH_SQL row per job.

        Split out from enqueue_batch so every multi-row writer (the batch
        itself, create_fan_out) validates and constructs identically and
        only differs in the connection the INSERT runs on.
        """
        # These three are supplied by the batch builder itself — job_class
        # and kwargs from each tuple, prio_ceiling from the call. Naming any
        # of them again in the shared options or a per-job dict would reach
        # build_enqueue_row as a duplicate argument and raise a bare
        # "multiple values for keyword argument" TypeError from deep in the
        # call; caught here it becomes a message that says which key and where.
        reserved = {"job_class", "job_kwargs", "prio_ceiling"}
        collision = reserved & set(options)
        if collision:
            raise ValueError(
                f"enqueue_batch() shared options may not set {sorted(collision)}: "
                f"job_class and kwargs come from each job tuple, and "
                f"prio_ceiling is enqueue_batch's own argument"
            )
        if "identity_key" in options:
            raise ValueError(_NO_BATCH_IDENTITY)
        if "debounce_key" in options:
            raise ValueError(_NO_BATCH_DEBOUNCE)

        ceiling = self.prio_ceiling if prio_ceiling is None else prio_ceiling
        rows = []
        for index, item in enumerate(jobs):
            job_class, kwargs, *rest = item
            per_job = rest[0] if rest else {}
            per_job_collision = reserved & set(per_job)
            if per_job_collision:
                raise ValueError(
                    f"job {index}'s per-job options may not set "
                    f"{sorted(per_job_collision)}: job_class and kwargs are the "
                    f"tuple's first two elements, and prio_ceiling is a "
                    f"batch-level argument"
                )
            if "identity_key" in per_job:
                raise ValueError(f"job {index}: {_NO_BATCH_IDENTITY}")
            if "debounce_key" in per_job:
                raise ValueError(f"job {index}: {_NO_BATCH_DEBOUNCE}")
            layered = {**options, **per_job}
            # enqueue()'s version rule, per row: the per-job option, else the
            # shared one, else this client's declared version. Resolved here
            # rather than merged under the option dicts so that an explicit
            # `app_version=None` means the same thing it means everywhere else
            # (take the client's), not "pin nothing".
            layered["app_version"] = self._app_version(layered.get("app_version"))
            rows.append(
                self.build_enqueue_row(
                    job_class,
                    prio_ceiling=ceiling,
                    job_kwargs=kwargs,
                    **layered,
                )
            )
        return rows

    @staticmethod
    async def _insert_batch_rows(
        conn: asyncpg.Connection, rows: list[list[Any]]
    ) -> list[int]:
        """Write pre-built rows with ENQUEUE_BATCH_SQL on the given
        connection, returning the ids in order."""
        columns = list(zip(*rows, strict=True))
        inserted = await conn.fetch(ENQUEUE_BATCH_SQL, *columns)
        return [row["id"] for row in inserted]

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
                f"{_JOB_INFO_SELECT} WHERE id = $1",
                job_id,
            )

        if not row:
            return None

        return JobInfo(**dict(row))

    async def get_job_by_identity(self, identity_key: str) -> JobInfo | None:
        """The job holding ``identity_key``, or None if nothing holds it.

        The same view get_job() returns, found by the caller's own name for
        the work instead of by an id it would have had to keep. There is at
        most one, in any state, because jorb_identity_idx says so.

        None is the honest answer to two different questions and does not
        distinguish them: this identity was never enqueued, or its job was
        enqueued, finished, and has since been reaped by retention. Both mean
        the same thing for what happens next — enqueueing it now creates a
        new job — which is exactly the horizon the key is bounded by.

        Example:
            job = await client.get_job_by_identity(f"order:{order_id}:ship")
            print("not shipped yet" if job is None else f"shipment {job.state}")
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                f"{_JOB_INFO_SELECT} WHERE identity_key = $1",
                identity_key,
            )

        if not row:
            return None

        return JobInfo(**dict(row))

    async def cancel_job(self, job_id: int) -> dict[str, Any]:
        """
        Cancel a job wherever it is in its lifecycle.

        Queued/waiting jobs are cancelled immediately. Claimed/running jobs
        get a cancellation request delivered to their worker, which cancels
        the task at its next await point.

        Args:
            job_id: Job ID

        Returns:
            {"job_id", "status"} where status is 'cancelled' (done now),
            'cancel_requested' (running; delivery in progress), or
            'not_cancellable'. A job that does not exist is
            'not_cancellable' too, not an exception: the caller asked for
            the job to be stopped, and it is not running either way.

        Example:
            result = await client.cancel_job(12345)
            if result["status"] != "not_cancellable":
                print(f"Cancel: {result['status']}")
        """
        async with self.pool.acquire() as conn:
            outcome = await db.cancel_job(conn, job_id)
        return {"job_id": job_id, "status": outcome or "not_cancellable"}

    #: cancel_and_wait's default wait when the caller passes none. Finite on
    #: purpose: cancellation lands at the worker's next await point, and a job
    #: that never reaches one (a tight synchronous loop) would hang an
    #: unbounded wait forever. The call returns the current state instead.
    _CANCEL_WAIT_TIMEOUT = 30.0

    async def cancel_and_wait(
        self, job_id: int, timeout: float | None = None
    ) -> str | None:
        """Cancel a job and wait until the cancellation has actually LANDED.

        'cancel_requested' is a promise, not an outcome: the running worker
        cancels at its next await point, and a synchronous task may outrun
        the request entirely and finish. This waits for the terminal state
        and returns it ('cancelled', or 'finished'/'crashed' when the job
        beat the cancel), or None when there was nothing to cancel.

        Args:
            job_id: the job to cancel.
            timeout: how long to wait for the cancellation to land. None uses
                a finite default (``_CANCEL_WAIT_TIMEOUT``), NOT an unbounded
                wait: a worker in a tight synchronous loop never reaches an
                await point, and waiting forever for it would hang the caller.

        Returns:
            A STATE string, not the {job_id, status} dict cancel_job()
            returns: the job's terminal state, or -- if the wait elapses
            before the cancel lands -- its current NON-terminal state
            ('running', 'claimed'), which tells the caller the request is
            still in flight. None only when there was nothing to cancel or
            the job has since been deleted. Never raises TimeoutError: a
            timeout is reported as the still-live state, not as an exception.
        """
        status = (await self.cancel_job(job_id))["status"]
        if status == "not_cancellable":
            return None
        if status == "cancelled":
            return "cancelled"
        wait = self._CANCEL_WAIT_TIMEOUT if timeout is None else timeout
        with contextlib.suppress(JobError, TimeoutError):
            await self.wait_for_result(job_id, timeout=wait)
        info = await self.get_job(job_id)
        return info.state if info else None

    async def rerun_job(self, job_id: int, *, fresh: bool = True) -> dict[str, Any]:
        """
        RE-RUN a terminal job — including one that already FINISHED, whose
        side effects it will repeat. Asked for by name, never implied:
        `retry_job` is the verb that refuses finished jobs.

        By default the run is fresh (DXE checkpoints AND durable streams
        wiped, re-executes from step 1 and streams from seq 0). Pass
        fresh=False to RESUME an interrupted durable job from its recorded
        checkpoints instead, keeping the stream it had already written.

        Returns:
            {"job_id", "status", "fresh"} where status is 'requeued' or
            'not_rerunnable' (not in a terminal state, or no such job);
            `fresh` reports which mode was asked for.
        """
        async with self.pool.acquire() as conn:
            requeued = await db.rerun_job(conn, job_id, fresh=fresh)
        return {
            "job_id": job_id,
            "status": "requeued" if requeued else "not_rerunnable",
            "fresh": fresh,
        }

    async def retry_job(self, job_id: int) -> dict[str, Any]:
        """
        Retry a job that did not succeed (crashed or cancelled).

        A job that already FINISHED is deliberately not retriable — re-running
        successful work repeats its side effects (see rerun_job, the
        verb for that).

        The job keeps its id (retries reuse the same row; per-attempt
        history lives in jorb_history).

        Args:
            job_id: Job ID to retry

        Returns:
            {"job_id", "status"} where status is 'requeued' or
            'not_retriable' (wrong state, or no such job)

        Example:
            if (await client.retry_job(12345))["status"] == "requeued":
                print("Job requeued")
        """
        async with self.pool.acquire() as conn:
            requeued = await db.retry_job(conn, job_id)

        return {
            "job_id": job_id,
            "status": "requeued" if requeued else "not_retriable",
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
        FORK a job into a NEW one that starts at `from_step`.

        The third verb, and the only one that does not reuse the row.
        `retry_job` and `rerun_job` requeue the SAME job — same id, same
        history, same identity. A fork inserts a second row that re-executes
        the source's work from step `from_step`, with steps 1..from_step-1
        copied in as its own checkpoints so they fast-forward rather than
        run again, and leaves the source completely alone (any state,
        including running).

        `from_step` is 1-based and names the step the fork EXECUTES first:
        1 (the default) copies nothing and re-runs everything under a new
        id; 4 copies steps 1-3.

        The fork inherits job_class, kwargs (or `kwargs_override`), queue and
        priority (or the overrides), capability, the retry/timeout policy in
        admin_data, and everything that says WHOSE work it is: uid, tags and
        partition_key, so a tenant's fork is still that tenant's job and
        still counts against that tenant's lane. It does NOT inherit
        identity or structure: deadline_key, identity_key, debounce_key,
        schedule_id, dag_id, run_group and the
        waitfor edges are all left unset, because two live rows sharing an
        idempotency key (or a DAG slot) would make that key mean nothing —
        and an identity_key most of all, since ITS whole promise is that the
        row holding it is the only one.

        Streams, events and mailbox messages are NOT copied — they are the
        SOURCE's output. A fast-forwarded `stream_write` checkpoint therefore
        appends nothing to the fork's stream: the fork's stream holds only
        what the steps it really ran produced (docs/DXE.md).

        Args:
            job_id: the job to fork FROM
            from_step: 1-based step the fork executes first (default: 1)
            queue: run the fork elsewhere (default: the source's queue)
            priority: run the fork at another priority (default: the source's)
            kwargs_override: replace the arguments wholesale (default: the
                source's kwargs)
            app_version: pin the FORK to a code version (default: None —
                unpinned, whatever the source was). The source's pin is
                deliberately not inherited and neither is this client's
                declared one: a fork is usually how work is re-run under NEW
                code, so inheriting either would strand the fork on a build
                that is going away. Pass the version explicitly when the fork
                really does belong to one.

        Returns:
            {"job_id", "source_job_id", "from_step", "steps_copied",
             "queue", "priority"} — `job_id` is the NEW job

        Raises:
            db.ForkRefused: no such job, `from_step` below 1, or `from_step`
                past the source's recorded step count + 1
            ValueError: `priority` above this client's worker ceiling —
                the same refusal enqueue makes, for the same reason (a job
                no worker will claim is a silent black hole)

        Example:
            fork = await client.fork_job(12345, from_step=4)
            result = await client.wait_for_result(fork["job_id"])
        """
        if priority is not None:
            validate_priority(priority, self.prio_ceiling)
        async with self.pool.acquire() as conn:
            try:
                return await db.fork_job(
                    conn,
                    job_id,
                    from_step=from_step,
                    queue=queue,
                    priority=priority,
                    kwargs_override=kwargs_override,
                    app_version=app_version,
                )
            except asyncpg.UndefinedTableError as e:
                # Same translation every other client verb makes: an
                # unmigrated database answers `relation "jorb" does not exist`,
                # which names neither the database nor the fix.
                raise self._unmigrated_database_error() from e

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

        The incident shape of the verb: deploy the fix, fork the crashed job
        from the step that broke, and the completed prefix is not paid for
        twice. Raises db.ForkRefused when no step recorded a failure — a job
        that crashed outside its steps has no failing step to start from, and
        guessing one would fast-forward work that never ran.

        ``app_version`` pins the fork, unpinned by default -- the incident
        shape is "deploy the fix, then fork", so the fork belongs to the NEW
        build if it belongs to any (see fork_job).
        """
        if priority is not None:
            validate_priority(priority, self.prio_ceiling)
        async with self.pool.acquire() as conn:
            try:
                return await db.fork_job_from_failure(
                    conn,
                    job_id,
                    queue=queue,
                    priority=priority,
                    kwargs_override=kwargs_override,
                    app_version=app_version,
                )
            except asyncpg.UndefinedTableError as e:
                raise self._unmigrated_database_error() from e

    # =========================================================================
    # Waiting on jobs (LISTEN/NOTIFY with polling fallback)
    # =========================================================================

    # While LISTENing, a poll every 2s covers the race between the initial
    # state check and the LISTEN registration (and any lost notification).
    _LISTEN_POLL_INTERVAL = 2.0
    # Pool-only clients (no db_params) have no LISTEN connection: poll faster.
    _PURE_POLL_INTERVAL = 0.5

    @property
    def listening(self) -> bool:
        """True when this client can ride LISTEN/NOTIFY for its waits.

        A client built without db_params (pool-only) has no LISTEN
        connection and every wait_* polls at {_PURE_POLL_INTERVAL}s by
        design — construct via create()/from_config(), or pass db_params,
        to get push latency."""
        return self._db_params is not None and not self._closed

    async def wait_for_group(self, run_group: int, timeout: float | None = None) -> int:
        """Wait until EVERY job in `run_group` has finished; returns the
        member count. Raises JobError naming the failed members if any
        member crashed or was cancelled (the group can then never finish),
        LookupError for a group with no members, TimeoutError on timeout.

        This is the client-side await for create_fan_out(): before it, a
        fan-out could only be waited on by chaining another job with
        waitfor_group. Polls at the fallback interval (group completion has
        no NOTIFY channel; membership is N jobs and demand-latching all of
        them would write N rows).
        """

        async def check() -> Any:
            row = await self.pool.fetchrow(
                """SELECT count(*) AS members,
                          count(*) FILTER (WHERE state = 'finished') AS done,
                          array_agg(id) FILTER (
                              WHERE state IN ('crashed', 'cancelled')
                          ) AS failed
                   FROM jorb WHERE run_group = $1""",
                run_group,
            )
            if not row["members"]:
                raise LookupError(
                    f"run_group {run_group} has no jobs, so it can never finish"
                )
            if row["failed"]:
                raise JobError(
                    f"run_group {run_group} cannot finish: jobs "
                    f"{sorted(row['failed'])} crashed or were cancelled"
                )
            if row["done"] == row["members"]:
                return int(row["members"])
            return _PENDING

        members: int = await self._poll_until(
            self._done_waiters,
            ("group", run_group),
            check,
            timeout,
            f"run_group {run_group} to finish",
            job_id=run_group,
            # a group has no per-job NOTIFY channel and run_group is not a
            # job id: pure-poll, do not latch awaited on an unrelated row
            register_demand=False,
        )
        return members

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
                    await conn.add_listener(db.CHANNEL_DONE, self._on_jorb_done)
                    await conn.add_listener(db.CHANNEL_EVENT, self._on_jorb_event)
                    await conn.add_listener(db.CHANNEL_STREAM, self._on_jorb_stream)
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

    def _on_jorb_stream(
        self, _conn: Any, _pid: int, _channel: str, payload: str
    ) -> None:
        """NOTIFY 'jorb_stream' payload: {"job_id": N, "key": K}."""
        with contextlib.suppress(Exception):
            data = json.loads(payload)
            for waiter in self._stream_waiters.get((data["job_id"], data["key"]), ()):
                waiter.set()

    # Registering demand for the gated notification channels. jorb_done,
    # jorb_event and jorb_stream are only emitted for a job somebody is
    # waiting on, so waiting means SAYING SO FIRST — the client half of the
    # ordering argument written out in sql/schema/90_notify.sql. `AND NOT awaited`
    # makes every registration after the first a no-op at the server. The
    # latch is not a refcount: for a job that terminates it simply dies
    # with the row, and for a job that never terminates (a durable state
    # machine) compact() clears it at each turn boundary — otherwise ONE
    # wait_for_state ever would make every future publish a NOTIFY-bearing
    # commit forever. A wait that is IN FLIGHT across a compact is not
    # broken by the clearing, only degraded: its 2s fallback poll still
    # answers, and every NEW wait re-registers here first. Deliberately no
    # per-beat re-arm — a waiter that wrote to the hottest row in the
    # system every fallback interval would be the polling this design
    # exists to avoid (test_waiting_for_a_state_does_not_poll counts it).
    _REGISTER_DEMAND_SQL = (
        "UPDATE jorb SET awaited = TRUE WHERE id = $1 AND NOT awaited"
    )

    async def _register_demand(
        self,
        waiters: dict[Any, list[asyncio.Event]],
        key: Any,
        job_id: int,
        register_demand: bool = True,
    ) -> asyncio.Event | None:
        """Say this client is waiting, and return the Event it parks on.

        None when the client has no listener (pure-polling mode) or the
        caller asked for no registration. ONE implementation, because every
        waiting API here — the single-answer `_poll_until` and the streaming
        reader — has to register in the same order and release the same way,
        and two copies of that would drift.

        Deliberately a pair of plain methods rather than one async context
        manager: `read_stream` is an async GENERATOR, and closing one throws
        `GeneratorExit` at its `yield` — which an `async with` around that
        yield cannot unwind cleanly. A `try/finally` can.

        Demand is registered BEFORE the caller's first look at the state,
        never after: a change landing between the look and the registration
        would be one this client is neither told about nor has already seen.
        `register_demand=False` is for a wait with no per-job NOTIFY channel
        (a group wait, whose `job_id` is a run_group and not a job id): it
        then pure-polls without writing to `jorb` and without a dead waiter
        Event nothing dispatches to.
        """
        waiter: asyncio.Event | None = None
        if register_demand and not self.listening and not self._polling_reported:
            # Once per client, not per wait: a pool-only client polls every
            # wait at _PURE_POLL_INTERVAL by design, and nothing else ever
            # says so — a team following the shared-pool construction found
            # out from pg_stat_activity.
            self._polling_reported = True
            logger.info(
                "JobClient was built without db_params: waits poll at "
                f"{self._PURE_POLL_INTERVAL}s instead of riding "
                "LISTEN/NOTIFY. Pass db_params (or use create()/"
                "from_config()) for push latency."
            )
        if register_demand and await self._ensure_listener():
            await self.pool.execute(self._REGISTER_DEMAND_SQL, job_id)
            waiter = asyncio.Event()
            waiters.setdefault(key, []).append(waiter)
        return waiter

    def _release_demand(
        self,
        waiters: dict[Any, list[asyncio.Event]],
        key: Any,
        waiter: asyncio.Event | None,
    ) -> None:
        """Drop this wait's Event, and the key with it when it was the last.

        The `jorb.awaited` latch is deliberately NOT cleared: it is not a
        refcount (see `_REGISTER_DEMAND_SQL`), and writing to the job row on
        every finished wait is the polling this design exists to avoid.
        """
        if waiter is None:
            return
        entries = waiters.get(key)
        if entries is not None:
            with contextlib.suppress(ValueError):
                entries.remove(waiter)
            if not entries:
                waiters.pop(key, None)

    async def _wait_beat(
        self, waiter: asyncio.Event | None, budget: float | None = None
    ) -> None:
        """Sleep until the next notification, the fallback poll, or `budget`.

        The fallback is what makes every gated channel safe to miss: a
        notification the demand race dropped costs latency, never an answer.
        A client with no listener has nothing to be woken by and polls
        faster instead.
        """
        interval = (
            self._LISTEN_POLL_INTERVAL
            if waiter is not None
            else self._PURE_POLL_INTERVAL
        )
        if budget is not None:
            interval = min(interval, budget)
        if waiter is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(waiter.wait(), interval)
            waiter.clear()
        else:
            await asyncio.sleep(interval)

    async def _poll_until(
        self,
        waiters: dict[Any, list[asyncio.Event]],
        key: Any,
        check: Callable[[], Awaitable[Any]],
        timeout: float | None,
        what: str,
        job_id: int,
        register_demand: bool = True,
    ) -> Any:
        """Run `check` until it returns something other than _PENDING.

        Between checks, wait for a NOTIFY dispatched to `waiters[key]` (with
        a 2s fallback poll), or plain-sleep when no listener is configured.
        The check ALWAYS runs once before any waiting — the condition may
        already hold.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        waiter = await self._register_demand(waiters, key, job_id, register_demand)
        try:
            while True:
                value = await check()
                if value is not _PENDING:
                    return value

                remaining = None
                if deadline is not None:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"timed out after {timeout}s waiting for {what}"
                        )

                await self._wait_beat(waiter, remaining)
        finally:
            self._release_demand(waiters, key, waiter)

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
                raise JobError(f"job {job_id} does not exist", job_id=job_id)
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
                    f"never be published",
                    job_id=job_id,
                )
            if job_state in _TERMINAL_JOB_STATES:
                raise JobError(
                    f"job {job_id} ended in {job_state!r} without publishing "
                    f"event {key!r}",
                    job_id=job_id,
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
                    f"(last: {value!r})",
                    job_id=job_id,
                )
            # No row at all — a bad id, or retention removed the job. Nothing
            # will ever publish, so waiting the full timeout only delays the
            # same answer. The event value comes from the same snapshot, so a
            # job cannot look absent while its event is still readable.
            if job_state is None:
                raise JobError(
                    f"job {job_id} does not exist, so event {key!r} will "
                    f"never be published",
                    job_id=job_id,
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

    # Rows one read takes at a time. A reader that has fallen behind a fast
    # producer (or started at offset 0 on a long stream) drains in batches
    # rather than materialising the whole stream in one fetch — the loop
    # keeps reading while a batch comes back full, so batching costs a round
    # trip per batch and bounds memory instead of the other way round.
    _STREAM_READ_BATCH: Final[int] = 1000

    #: A page of one job's stream from `offset` upward. Served as a range
    #: scan of jorb_stream's primary key (job_id, key, seq), which is why the
    #: sequence is dense: a reader resumes by NUMBER, holding no cursor and
    #: no server-side state between beats.
    _READ_STREAM_SQL = """SELECT seq, value, closed
             FROM jorb_stream
            WHERE job_id = $1 AND key = $2 AND seq >= $3
            ORDER BY seq
            LIMIT $4"""

    async def read_stream(
        self, job_id: int, key: str, *, offset: int = 0
    ) -> AsyncGenerator[Any]:
        """Yield a job's stream values in order, from `offset` upward, as they
        are written.

        Jobs append with `await self.stream_write(key, value)`; this is the
        client-side reader. It rides the shared 'jorb_stream' LISTEN
        connection (the same one as `wait_for_result`) with a 2-second
        fallback poll, and pool-only clients poll.

        The reader is RESUMABLE because positions are dense and 0-based:
        count what you consumed and pass it back as `offset` to pick up
        exactly where you stopped, with no cursor held anywhere.

        It stops on the first of:

        * the closing marker `stream_close(key)` wrote — end of stream,
          declared by the job;
        * the job reaching a terminal state. A cancel or a timeout ends a job
          out of band while a fenced-out execution may still believe it is
          writing, so a terminal job's stream is over whether or not it was
          closed. One final read happens after the terminal state is
          observed, so a row committed just before the end is still
          delivered.

        `offset` past the end is not an error: the reader simply waits there
        for rows that may never come, exactly as it would at position 0 of a
        stream the job has not started writing.

        Demand is registered ONCE for the whole read, not once per row —
        which is the difference between this and a loop around
        `get_stream()`: that loop would `UPDATE` the `jorb` row on every
        pass, writing to the hottest table in the system to ask a question a
        notification already answers.

        Raises `JobError` if the job does not exist (a bad id, or retention
        removed it) — nothing will ever append, so waiting only delays the
        same answer. Bound a read with `asyncio.timeout()` around the loop;
        a stream has no deadline of its own because the job's is the real one.

        Example:
            async for row in client.read_stream(job_id, 'progress'):
                print(row)
        """
        next_seq = offset
        # True once a terminal state has been observed: the loop makes ONE
        # more pass to collect anything that committed in the window, then
        # stops. Without it a row written microseconds before the terminal
        # transition would be dropped by the reader that was watching for it.
        final = False

        waiter = await self._register_demand(
            self._stream_waiters, (job_id, key), job_id
        )
        try:
            while True:
                rows = await self.pool.fetch(
                    self._READ_STREAM_SQL,
                    job_id,
                    key,
                    next_seq,
                    self._STREAM_READ_BATCH,
                )
                for row in rows:
                    if row["closed"]:
                        return
                    next_seq = row["seq"] + 1
                    yield row["value"]

                if len(rows) == self._STREAM_READ_BATCH:
                    continue  # a full batch means there may be more, now
                if final:
                    return

                state = await self.pool.fetchval(
                    "SELECT state FROM jorb WHERE id = $1", job_id
                )
                if state is None:
                    raise JobError(
                        f"job {job_id} does not exist, so stream {key!r} will "
                        f"never be written",
                        job_id=job_id,
                    )
                if state in _TERMINAL_JOB_STATES:
                    final = True
                    continue

                await self._wait_beat(waiter)
        finally:
            # runs on the caller's `break` (GeneratorExit at the yield) as
            # well as on a normal end, which is what keeps a reader that
            # stopped early from leaving a waiter behind
            self._release_demand(self._stream_waiters, (job_id, key), waiter)

    async def get_stream(self, job_id: int, key: str) -> dict[str, Any]:
        """Snapshot one of a job's streams: `{"values": [...], "closed": bool}`.

        The non-streaming read, for a caller that wants what has been written
        so far and not a live feed — a report page, an assertion, a job that
        has already finished. `values` are in order from position 0, and
        `closed` says whether the job declared the stream finished.

        Values after a closing marker are not reported, so this and
        `read_stream()` agree about where a stream ends. A job that does not
        exist (or never wrote this key) is an empty, unclosed snapshot rather
        than an error: this is a query, not a wait, so there is nothing to
        wait in vain for.
        """
        rows = await self.pool.fetch(
            "SELECT value, closed FROM jorb_stream "
            "WHERE job_id = $1 AND key = $2 ORDER BY seq",
            job_id,
            key,
        )
        values: list[Any] = []
        for row in rows:
            if row["closed"]:
                return {"values": values, "closed": True}
            values.append(row["value"])
        return {"values": values, "closed": False}

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
        for it (there is no mailbox NOTIFY — see sql/schema/90_notify.sql). External
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

    async def queue_depth(self, queue: str | None = None) -> int:
        """
        How many jobs are claimable RIGHT NOW.

        Counts the runnable backlog only: a job parked in the future (retry
        backoff, enqueue-at) is where it was asked to be, not waiting for a
        worker, and counting it as depth makes a healthy install look stuck.
        The same split :data:`pyjobby.db.QUEUE_STATS_SQL` reports as
        'queued' vs 'scheduled'.

        Args:
            queue: Queue name, or None (the default) for every queue

        Returns:
            Number of jobs claimable now

        Example:
            depth = await client.queue_depth('emails')
            print(f"Queue has {depth} jobs waiting")
        """
        async with self.pool.acquire() as conn:
            depth: int = await conn.fetchval(
                """
                SELECT COUNT(*)
                FROM jorb
                WHERE ($1::text IS NULL OR queue = $1)
                  AND state = 'queued'
                  AND run_after <= now()
            """,
                queue,
            )
            return depth

    async def queue_stats(
        self, queue: str | None = None, window: timedelta = timedelta(hours=1)
    ) -> dict[str, int]:
        """
        Per-state counts: LIVE states exactly, terminal states within
        `window` (default: the last hour).

        Live work (queued/scheduled/claimed/running/waiting) is bounded and
        counted in full. Terminal counts are a recent-activity dashboard
        number, not an all-time audit — the all-time count grows with the
        install's whole history and belongs to SQL, not to a call every
        dashboard makes on a timer.

        'queued' means claimable RIGHT NOW; a job parked in the future
        (retry backoff, enqueue-at) is reported as 'scheduled' and is
        deliberately not counted as backlog. :data:`pyjobby.db.QUEUE_STATS_SQL`
        is the one home for these semantics — every surface reads it.

        Args:
            queue: Queue name, or None (the default) to aggregate EVERY
                queue in the install
            window: how far back to count finished/crashed/cancelled

        Returns:
            Every job state plus 'scheduled', zero-filled.

        Example:
            stats = await client.queue_stats('emails')
            print(f"Queued: {stats['queued']}, Running: {stats['running']}")
        """
        rows = await self.pool.fetch(db.QUEUE_STATS_SQL, window, queue)
        stats = dict.fromkeys(db.QUEUE_STATS_STATES, 0)
        for row in rows:
            stats[row["state"]] += row["n"]
        return stats

    async def list_queues(
        self, window: timedelta = timedelta(hours=1)
    ) -> list[dict[str, Any]]:
        """
        Every queue with per-state counts — the same contract as
        queue_stats() and the same SQL: live states exact (with 'queued'
        split from deferred 'scheduled'), terminal states within `window`,
        `total` the sum of what is reported.

        Example:
            queues = await client.list_queues()
            for q in queues:
                print(f"{q['queue']}: {q['queued']} queued, {q['running']} running")
        """
        rows = await self.pool.fetch(db.QUEUE_STATS_SQL, window, None)
        queues: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = queues.setdefault(
                row["queue"],
                {"queue": row["queue"], **dict.fromkeys(db.QUEUE_STATS_STATES, 0)},
            )
            entry[row["state"]] = row["n"]
        for entry in queues.values():
            entry["total"] = sum(entry[state] for state in db.QUEUE_STATS_STATES)
        return [queues[name] for name in sorted(queues)]

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

    async def update_job_app_version(
        self, job_id: int, app_version: str | None
    ) -> bool:
        """Re-pin (or unpin) a job that has not been claimed yet.

        The twin of `update_job_priority`, and it exists for the same reason:
        the version is a claim gate, so a job pinned to code nobody runs is
        stranded, and the fix has to be reachable without raw SQL. `None`
        CLEARS the pin, which makes the job claimable by every worker — the
        remedy for a pin whose deploy has moved on.

        Only queued/waiting jobs, exactly like the priority twin: a claimed or
        running job has already been matched to a worker, so changing the gate
        it passed through decides nothing, and a terminal job's pin is
        history. A RETRY of that job will keep whatever this row says, which is
        why repinning it now is the operator's move rather than the requeue's.

        Returns True if the row was updated, False if the job does not exist
        or has already left the queue.

        Raises:
            ValueError: an empty or over-long version (see
                validate_app_version) — the same refusal an enqueue makes.

        Example:
            # the deploy this job was waiting for is never coming
            if await client.update_job_app_version(12345, None):
                print("unpinned; any worker may run it now")
        """
        app_version = validate_app_version(app_version)

        async with self.pool.acquire() as conn:
            result: str = await conn.execute(
                """
                UPDATE jorb
                SET app_version = $2
                WHERE id = $1
                  AND state IN ('queued', 'waiting')
            """,
                job_id,
                app_version,
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

        # Validate order_by to prevent SQL injection. "priority" is accepted
        # as the API-side name for the prio column (every enqueue-side knob
        # calls it priority). An UNKNOWN field raises: silently falling back
        # to created-order handed back rows in the wrong order with nothing
        # to say so.
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
        if order_by == "priority":
            order_by = "prio"
        if order_by not in valid_fields:
            raise ValueError(
                f"order_by must be one of {valid_fields} (or 'priority'), "
                f"got {order_by!r}"
            )

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
                print(f"Job {job['id']} failed: {job['error_message']}")
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

        # one statement, not one round trip per id (db.cancel_jobs shares
        # CANCEL_SQL's guard and CASE logic verbatim)
        return await db.cancel_jobs(self.pool, job_ids)

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

        # one statement, not one round trip per id (db.retry_jobs is the
        # same guarded requeue with `id = ANY(...)`)
        return await db.retry_jobs(self.pool, job_ids)

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
        return await self._create_chain(
            [(job_class, kwargs, True) for job_class, kwargs in steps],
            queue=queue,
            priority=priority,
            pass_results=False,
            common_options={},
        )

    async def _create_chain(
        self,
        stages: list[tuple[str, dict[str, Any], bool]],
        *,
        queue: str,
        priority: int,
        pass_results: bool,
        common_options: dict[str, Any],
    ) -> list[int]:
        """The one home for the linear-chain builders.

        Each stage waits on the previous (waitfor_job); when ``pass_results``
        is set, a stage whose predecessor SAVED its result also receives it
        (use_result_from -> kwargs['upstream_result'] at execution time).
        create_pipeline chains without passing -- injecting upstream_result
        into jobs that never asked for it would be a TypeError in their
        task() -- while create_pipeline_with_results threads results through.
        Different verbs, one chain construction.

        The whole chain is written in ONE transaction, for the reason
        DAGBuilder.execute gives at length: stage 0 is immediately runnable
        the instant it commits, and the wake-up of a waiting stage is
        performed by the worker that FINISHES its predecessor -- so a stage 0
        that committed, ran and finished while stage 1 was still being
        written would leave stage 1 waiting on a job nobody will report again
        (until `monitor.sweep_stranded_waiters` notices). A failure partway
        down the chain must leave no jobs at all, not a committed head whose
        tail does not exist.
        """
        if not stages:
            return []

        job_ids: list[int] = []
        previous_job: int | None = None
        previous_saved = False

        async with self.pool.acquire() as conn, conn.transaction():
            try:
                for job_class, kwargs, save_result in stages:
                    # The result options are supplied ONLY by the verb that
                    # owns them. create_pipeline never named them, so a stage
                    # whose kwargs carry `save_result` passes it through to
                    # the enqueue path as it always did; naming them
                    # unconditionally here turned that into a "multiple values
                    # for keyword argument" TypeError.
                    result_options: dict[str, Any] = {}
                    if pass_results:
                        result_options["save_result"] = save_result
                        result_options["use_result_from"] = (
                            previous_job if previous_saved else None
                        )

                    # enqueue_in_transaction is static and so has no client to
                    # read the fleet's ceiling or declared code version from;
                    # pass ours, and let an explicit common_options value still
                    # win. A pipeline is ordinary work and carries this
                    # client's pin like any other enqueue would.
                    options: dict[str, Any] = {
                        "queue": queue,
                        "priority": priority,
                        "prio_ceiling": self.prio_ceiling,
                        "app_version": self.app_version,
                        **result_options,
                        **common_options,
                    }

                    job_id = await self.enqueue_in_transaction(
                        conn,
                        job_class,
                        **kwargs,
                        waitfor_job=previous_job,
                        **options,
                    )
                    job_ids.append(job_id)
                    previous_job = job_id
                    previous_saved = save_result
            except asyncpg.UndefinedTableError as e:
                raise self._unmigrated_database_error() from e

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
        # A group is only meaningful whole: a waiter on a half-written group
        # is satisfied by the members that exist and never sees the rest, so
        # the group id and every member are written on ONE connection in ONE
        # transaction. The members are a single ENQUEUE_BATCH_SQL statement
        # (all-or-nothing by itself); the transaction is what also ties the
        # id allocation to them.
        jobs: list[tuple[Any, ...]] = [(job_class, kwargs) for kwargs in items]
        async with self.pool.acquire() as conn, conn.transaction():
            if run_group is None:
                run_group = await conn.fetchval("SELECT nextval('jorb_id_seq')")

            if not jobs:
                return [], run_group

            rows = self._build_batch_rows(
                jobs,
                None,
                queue=queue,
                priority=priority,
                run_group=run_group,
            )
            job_ids = await self._insert_batch_rows(conn, rows)

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
        except Exception as e:
            # the bool is this probe's contract, but the CAUSE must not be
            # swallowed with it — "unhealthy" with no reason is undebuggable
            logger.warning(f"health_check failed: {type(e).__name__}: {e}")
            return False

    # =========================================================================
    # DAG Support
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
    # Pipeline with Result Passing
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
        return await self._create_chain(
            list(stages),
            queue=queue,
            priority=priority,
            pass_results=True,
            common_options=common_options,
        )


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
        app_version: str | None = None,
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
            app_version: code version stamped on every enqueue through this
                client (default: None — unpinned). Named explicitly for the
                same reason. See JobClient.__init__.
            **connect_kwargs: asyncpg.connect kwargs (host, port, database,
                user, password, ...) used when no DSN is given
        """
        self._loop = asyncio.new_event_loop()
        self._closed = False
        try:
            self._client: JobClient = self._loop.run_until_complete(
                self._create(
                    dsn,
                    connect_kwargs,
                    min_size,
                    max_size,
                    prio_ceiling,
                    app_version,
                )
            )
        except BaseException:
            # a bad DSN / unreachable database / bad kwargs raises here, and
            # __init__ leaves no object to call close() on — so the loop
            # (with its epoll fd and self-pipe) must be closed before the
            # exception propagates, or a retry loop leaks a loop per attempt
            self._closed = True
            self._loop.close()
            raise

    @staticmethod
    async def _create(
        dsn: str | None,
        connect_kwargs: dict[str, Any],
        min_size: int,
        max_size: int,
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
        app_version: str | None = None,
    ) -> JobClient:
        if dsn is not None:
            pool = await db.create_pool(dsn, min_size=min_size, max_size=max_size)
            client = JobClient(
                pool,
                db_params=dsn,
                prio_ceiling=prio_ceiling,
                app_version=app_version,
            )
        else:
            pool = await db.create_pool(
                min_size=min_size, max_size=max_size, **connect_kwargs
            )
            client = JobClient(
                pool,
                db_params=dict(connect_kwargs),
                prio_ceiling=prio_ceiling,
                app_version=app_version,
            )
        # the pool is this facade's own creation; nobody else can close it
        client._owns_pool = True
        return client

    @classmethod
    def from_config(
        cls,
        config_path: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        prio_ceiling: int | None = None,
        app_version: str | None = None,
    ) -> SyncJobClient:
        """Build from a pyjobby.toml, like JobClient.from_config() —
        scripts and cron jobs are exactly where a config file lives.

        ``prio_ceiling`` left unset takes the file's ``prio_ceiling`` (else
        1000), ``app_version`` left unset takes the file's ``app_version``
        (else None, unpinned), and a file with no db_params is a ConfigError
        rather than a silent fallback to asyncpg's environment defaults — all
        exactly as in JobClient.from_config(), which documents why.
        """
        from .configloader import ConfigError, load_config_from_file

        config = load_config_from_file(
            config_path, keys=["db_params", "prio_ceiling", "app_version"]
        )
        db_params = config.get("db_params")
        if not db_params:
            raise ConfigError(f"No db_params found in config file: {config_path}")

        if prio_ceiling is None:
            configured = config.get("prio_ceiling")
            prio_ceiling = (
                DEFAULT_PRIO_CEILING if configured is None else int(configured)
            )

        if app_version is None:
            app_version = config.get("app_version")

        return cls(
            min_size=min_size,
            max_size=max_size,
            prio_ceiling=prio_ceiling,
            app_version=app_version,
            **db_params,
        )

    def _run(self, coro: Awaitable[Any]) -> Any:
        if self._closed:
            raise RuntimeError("SyncJobClient is closed")
        return self._loop.run_until_complete(coro)

    def enqueue(self, job_class: str, **options: Any) -> int:
        """Synchronous JobClient.enqueue()."""
        job_id: int = self._run(self._client.enqueue(job_class, **options))
        return job_id

    def enqueue_identified(
        self, job_class: str, *, identity_key: str, **options: Any
    ) -> tuple[int, bool]:
        """Synchronous JobClient.enqueue_identified()."""
        outcome: tuple[int, bool] = self._run(
            self._client.enqueue_identified(
                job_class, identity_key=identity_key, **options
            )
        )
        return outcome

    def debounce(
        self,
        job_class: str,
        *,
        key: str,
        period: float,
        cap: float | None = None,
        **options: Any,
    ) -> tuple[int, bool]:
        """Synchronous JobClient.debounce()."""
        outcome: tuple[int, bool] = self._run(
            self._client.debounce(job_class, key=key, period=period, cap=cap, **options)
        )
        return outcome

    def get_job(self, job_id: int) -> JobInfo | None:
        """Synchronous JobClient.get_job()."""
        info: JobInfo | None = self._run(self._client.get_job(job_id))
        return info

    def get_job_by_identity(self, identity_key: str) -> JobInfo | None:
        """Synchronous JobClient.get_job_by_identity()."""
        info: JobInfo | None = self._run(self._client.get_job_by_identity(identity_key))
        return info

    def wait_for_result(self, job_id: int, timeout: float | None = None) -> Any:
        """Synchronous JobClient.wait_for_result()."""
        return self._run(self._client.wait_for_result(job_id, timeout=timeout))

    def cancel_job(self, job_id: int) -> dict[str, Any]:
        """Synchronous JobClient.cancel_job()."""
        result: dict[str, Any] = self._run(self._client.cancel_job(job_id))
        return result

    def retry_job(self, job_id: int) -> dict[str, Any]:
        """Synchronous JobClient.retry_job()."""
        result: dict[str, Any] = self._run(self._client.retry_job(job_id))
        return result

    def rerun_job(self, job_id: int, *, fresh: bool = True) -> dict[str, Any]:
        """Synchronous JobClient.rerun_job()."""
        result: dict[str, Any] = self._run(self._client.rerun_job(job_id, fresh=fresh))
        return result

    def fork_job(
        self,
        job_id: int,
        *,
        from_step: int = 1,
        queue: str | None = None,
        priority: int | None = None,
        kwargs_override: dict[str, Any] | None = None,
        app_version: str | None = None,
    ) -> dict[str, Any]:
        """Synchronous JobClient.fork_job()."""
        result: dict[str, Any] = self._run(
            self._client.fork_job(
                job_id,
                from_step=from_step,
                queue=queue,
                priority=priority,
                kwargs_override=kwargs_override,
                app_version=app_version,
            )
        )
        return result

    def fork_job_from_failure(
        self,
        job_id: int,
        *,
        queue: str | None = None,
        priority: int | None = None,
        kwargs_override: dict[str, Any] | None = None,
        app_version: str | None = None,
    ) -> dict[str, Any]:
        """Synchronous JobClient.fork_job_from_failure()."""
        result: dict[str, Any] = self._run(
            self._client.fork_job_from_failure(
                job_id,
                queue=queue,
                priority=priority,
                kwargs_override=kwargs_override,
                app_version=app_version,
            )
        )
        return result

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
    # Full parity with JobClient's async surface. Hand-written like
    # SyncMachine, and held complete the same way: the mirror test compares
    # this class against JobClient's public async methods, so a method added
    # there without a wrapper here fails CI. Excluded, with reasons, in that
    # test: enqueue_in_transaction (takes the CALLER's async connection) and
    # enqueue_handle (JobHandle's methods are coroutines bound to the async
    # client — run()/wait_for_result() are the sync shapes of that workflow).
    # ---------------------------------------------------------------------

    def run(self, job_class: str, timeout: float | None = None, **options: Any) -> Any:
        """Synchronous JobClient.run()."""
        return self._run(self._client.run(job_class, timeout=timeout, **options))

    def enqueue_batch(
        self,
        jobs: list[tuple[Any, ...]],
        prio_ceiling: int | None = None,
        **options: Any,
    ) -> list[int]:
        """Synchronous JobClient.enqueue_batch()."""
        ids: list[int] = self._run(
            self._client.enqueue_batch(jobs, prio_ceiling=prio_ceiling, **options)
        )
        return ids

    def wait_for_event(
        self,
        job_id: int,
        key: str,
        accept: Callable[[Any], bool] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Synchronous JobClient.wait_for_event()."""
        return self._run(
            self._client.wait_for_event(job_id, key, accept=accept, timeout=timeout)
        )

    def wait_for_group(self, run_group: int, timeout: float | None = None) -> int:
        """Synchronous JobClient.wait_for_group()."""
        members: int = self._run(
            self._client.wait_for_group(run_group, timeout=timeout)
        )
        return members

    def cancel_and_wait(self, job_id: int, timeout: float | None = None) -> str | None:
        """Synchronous JobClient.cancel_and_wait()."""
        state: str | None = self._run(
            self._client.cancel_and_wait(job_id, timeout=timeout)
        )
        return state

    def get_steps(self, job_id: int) -> list[dict[str, Any]]:
        """Synchronous JobClient.get_steps()."""
        steps: list[dict[str, Any]] = self._run(self._client.get_steps(job_id))
        return steps

    def read_stream(self, job_id: int, key: str, *, offset: int = 0) -> Iterator[Any]:
        """Synchronous JobClient.read_stream(): a plain generator.

        Each row costs one turn of the wrapped loop (`__anext__` is a
        coroutine like any other method here), so the sync reader parks on
        the same notification and the same fallback poll as the async one.
        Closing the generator early — `break`, or an exception in the
        caller's loop — closes the async one, releasing its demand
        registration rather than leaving a waiter behind.
        """
        rows = self._client.read_stream(job_id, key, offset=offset)
        try:
            while True:
                try:
                    yield self._run(rows.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            # A client closed while the caller still held the generator has
            # already torn its loop down; there is nothing left to close on.
            if not self._closed:
                self._run(rows.aclose())

    def get_stream(self, job_id: int, key: str) -> dict[str, Any]:
        """Synchronous JobClient.get_stream()."""
        snapshot: dict[str, Any] = self._run(self._client.get_stream(job_id, key))
        return snapshot

    def queue_depth(self, queue: str | None = None) -> int:
        """Synchronous JobClient.queue_depth()."""
        depth: int = self._run(self._client.queue_depth(queue))
        return depth

    def queue_stats(
        self, queue: str | None = None, window: timedelta = timedelta(hours=1)
    ) -> dict[str, int]:
        """Synchronous JobClient.queue_stats()."""
        stats: dict[str, int] = self._run(
            self._client.queue_stats(queue, window=window)
        )
        return stats

    def list_queues(
        self, window: timedelta = timedelta(hours=1)
    ) -> list[dict[str, Any]]:
        """Synchronous JobClient.list_queues()."""
        queues: list[dict[str, Any]] = self._run(
            self._client.list_queues(window=window)
        )
        return queues

    def purge_queue(self, queue: str, states: list[str] | None = None) -> int:
        """Synchronous JobClient.purge_queue()."""
        purged: int = self._run(self._client.purge_queue(queue, states=states))
        return purged

    def get_job_full(self, job_id: int) -> dict[str, Any] | None:
        """Synchronous JobClient.get_job_full()."""
        row: dict[str, Any] | None = self._run(self._client.get_job_full(job_id))
        return row

    def get_job_result(self, job_id: int) -> Any | None:
        """Synchronous JobClient.get_job_result()."""
        return self._run(self._client.get_job_result(job_id))

    def delete_job(self, job_id: int) -> bool:
        """Synchronous JobClient.delete_job()."""
        deleted: bool = self._run(self._client.delete_job(job_id))
        return deleted

    def update_job_priority(self, job_id: int, new_priority: int) -> bool:
        """Synchronous JobClient.update_job_priority()."""
        updated: bool = self._run(
            self._client.update_job_priority(job_id, new_priority)
        )
        return updated

    def update_job_app_version(self, job_id: int, app_version: str | None) -> bool:
        """Synchronous JobClient.update_job_app_version()."""
        updated: bool = self._run(
            self._client.update_job_app_version(job_id, app_version)
        )
        return updated

    def get_jobs(self, **filters: Any) -> list[dict[str, Any]]:
        """Synchronous JobClient.get_jobs()."""
        rows: list[dict[str, Any]] = self._run(self._client.get_jobs(**filters))
        return rows

    def search_jobs(self, **filters: Any) -> list[dict[str, Any]]:
        """Synchronous JobClient.search_jobs()."""
        rows: list[dict[str, Any]] = self._run(self._client.search_jobs(**filters))
        return rows

    def get_failed_jobs(self, **filters: Any) -> list[dict[str, Any]]:
        """Synchronous JobClient.get_failed_jobs()."""
        rows: list[dict[str, Any]] = self._run(self._client.get_failed_jobs(**filters))
        return rows

    def get_waiting_jobs(self, **filters: Any) -> list[dict[str, Any]]:
        """Synchronous JobClient.get_waiting_jobs()."""
        rows: list[dict[str, Any]] = self._run(self._client.get_waiting_jobs(**filters))
        return rows

    def bulk_cancel(self, job_ids: list[int]) -> int:
        """Synchronous JobClient.bulk_cancel()."""
        cancelled: int = self._run(self._client.bulk_cancel(job_ids))
        return cancelled

    def bulk_retry(self, job_ids: list[int]) -> list[int]:
        """Synchronous JobClient.bulk_retry()."""
        requeued: list[int] = self._run(self._client.bulk_retry(job_ids))
        return requeued

    def bulk_delete(self, job_ids: list[int]) -> int:
        """Synchronous JobClient.bulk_delete()."""
        deleted: int = self._run(self._client.bulk_delete(job_ids))
        return deleted

    def bulk_update_priority(self, job_ids: list[int], new_priority: int) -> int:
        """Synchronous JobClient.bulk_update_priority()."""
        updated: int = self._run(
            self._client.bulk_update_priority(job_ids, new_priority)
        )
        return updated

    def create_pipeline(
        self,
        steps: list[tuple[str, dict[str, Any]]],
        queue: str = "default",
        priority: int = 100,
    ) -> list[int]:
        """Synchronous JobClient.create_pipeline()."""
        ids: list[int] = self._run(
            self._client.create_pipeline(steps, queue=queue, priority=priority)
        )
        return ids

    def create_fan_out(
        self,
        job_class: str,
        items: list[dict[str, Any]],
        queue: str = "default",
        priority: int = 100,
        run_group: int | None = None,
    ) -> tuple[list[int], int]:
        """Synchronous JobClient.create_fan_out()."""
        fanned: tuple[list[int], int] = self._run(
            self._client.create_fan_out(
                job_class, items, queue=queue, priority=priority, run_group=run_group
            )
        )
        return fanned

    def create_pipeline_with_results(
        self,
        stages: list[tuple[str, dict, bool]],
        queue: str = "default",
        priority: int = 100,
        **common_options: Any,
    ) -> list[int]:
        """Synchronous JobClient.create_pipeline_with_results()."""
        ids: list[int] = self._run(
            self._client.create_pipeline_with_results(
                stages, queue=queue, priority=priority, **common_options
            )
        )
        return ids

    def health_check(self) -> bool:
        """Synchronous JobClient.health_check()."""
        healthy: bool = self._run(self._client.health_check())
        return healthy

    def dag(self, name: str | None = None, **common_options: Any) -> DAGBuilder:
        """A DAGBuilder (plain object); execute it with execute_dag()."""
        return self._client.dag(name, **common_options)

    def execute_dag(self, dag: DAGBuilder) -> dict:
        """Synchronous JobClient.execute_dag()."""
        mapping: dict = self._run(self._client.execute_dag(dag))
        return mapping

    def get_dag_status(self, dag_id: int) -> dict[str, Any]:
        """Synchronous JobClient.get_dag_status()."""
        status: dict[str, Any] = self._run(self._client.get_dag_status(dag_id))
        return status

    def wait_for_dag(self, dag_id: int, timeout: float | None = None) -> bool:
        """Synchronous JobClient.wait_for_dag()."""
        outcome: bool = self._run(self._client.wait_for_dag(dag_id, timeout))
        return outcome

    @property
    def listening(self) -> bool:
        """Synchronous JobClient.listening."""
        return self._client.listening

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
        """Close the underlying client (pool + listener) and the loop.

        Idempotent, and the loop is closed even if the pool close raises (a
        dead server): otherwise a `finally: client.close()` retry would
        re-enter run_until_complete on a live loop, and the loop would leak.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.run_until_complete(self._client.close())
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
        finally:
            self._loop.close()

    def __enter__(self) -> SyncJobClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()
