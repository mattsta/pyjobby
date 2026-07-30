"""The job lifecycle, declared in one place.

pyjobby's own state machine is enforced *by construction*: every statement
that changes ``jorb.state`` carries an ``AND state IN (...)`` predicate, so an
illegal transition matches zero rows rather than raising. That is the right
posture for a system where the loser of a race must lose quietly, and it is
strictly stronger than a validating trigger — the guard is part of the same
atomic statement as the write.

Enforcement spread across statements is not, on its own, a *description*.
This module is the description: one place a reader can look to learn what the
machine is, and one thing a new statement can be checked against.

``tests/test_lifecycle.py`` checks it against the code in both directions --
no statement in ``STMTS`` may realise a transition this table does not permit,
and the states named here must be exactly the ``jorbstate`` enum --
while ``tests/test_invariants.py`` walks recorded ``jorb_history`` over
generated workloads against the same table. The predicates remain the
enforcement; this is what makes them reviewable.

Nothing here is on the write path — it is module-level data and pure
functions, imported for its constants and its checks.
"""

from __future__ import annotations

#: Every label of the ``jorbstate`` enum, in lifecycle order. Kept in step
#: with ``pyjobby/sql/schema/00_core.sql`` by ``test_declared_states_match_the_enum``,
#: which reads the enum out of the database rather than trusting this tuple.
JOB_STATES: tuple[str, ...] = (
    "queued",
    "claimed",
    "running",
    "waiting",
    "finished",
    "crashed",
    "cancelled",
)

#: The states a job never leaves on its own. Only these are ever eligible for
#: retention deletion: a queued/claimed/running/waiting job is live work
#: however old it looks, because a job parked on a dependency can legitimately
#: outlive any window.
#:
#: ``crashed`` is in here because ``crashed`` IS the dead letter queue. There
#: is no separate DLQ table to move a exhausted job into.
TERMINAL_STATES: tuple[str, ...] = ("finished", "crashed", "cancelled")

#: :data:`TERMINAL_STATES` as the inside of a SQL ``IN (...)`` list.
#:
#: Interpolated rather than bound, deliberately and everywhere it is used:
#: ``jorb_retention_idx`` is PARTIAL on this predicate, and a bound parameter
#: falls off the index the moment PostgreSQL switches to a generic plan. The
#: values are these three literals, so there is nothing to inject.
TERMINAL_STATES_SQL: str = ", ".join(f"'{state}'" for state in TERMINAL_STATES)

#: The states that represent work still in the system.
LIVE_STATES: tuple[str, ...] = tuple(
    state for state in JOB_STATES if state not in TERMINAL_STATES
)

#: :data:`LIVE_STATES` as the inside of a SQL ``IN (...)`` list, interpolated
#: rather than bound for the reason :data:`TERMINAL_STATES_SQL` is: the
#: partial indexes the retention sweep's dependency refusal rides
#: (``jorb_waitfor_job_idx``, ``jorb_waitfor_group_idx``,
#: ``jorb_use_result_from_idx``) are declared on exactly this predicate, and a
#: bound parameter falls off a partial index the moment PostgreSQL switches to
#: a generic plan. The values are these four literals, so there is nothing to
#: inject.
LIVE_STATES_SQL: str = ", ".join(f"'{state}'" for state in LIVE_STATES)

#: The states in which a job IS MATCHED TO A WORKER: claimed but not yet
#: started, or executing. "In flight" in every count, sweep and dashboard the
#: platform has -- fleet saturation, per-queue concurrency, the monitor's
#: dead-worker and stuck-claim sweeps, the cancel path's "ask it to stop
#: rather than cancel it outright" arm.
#:
#: It is the busiest predicate in the system and it was written out by hand in
#: twenty places, which is twenty chances to type ``('claimed')`` and silently
#: under-count running work forever -- no error, no failing row, just a
#: concurrency cap that admits twice what it was set to.
IN_FLIGHT_STATES: tuple[str, ...] = ("claimed", "running")

#: :data:`IN_FLIGHT_STATES` as the inside of a SQL ``IN (...)`` list.
#:
#: Interpolated rather than bound, for the reason :data:`TERMINAL_STATES_SQL`
#: is and with more riding on it: ``jorb_inflight_idx`` and
#: ``jorb_partition_inflight_idx`` are PARTIAL indexes whose predicate is
#: exactly this literal list. PostgreSQL proves a partial index usable only by
#: matching the query's own clauses against that predicate -- it cannot derive
#: the list from a bound array parameter -- so ``state = ANY($1)`` here is
#: correct, index-less, and a sequential scan of the largest table in the
#: system. THE PARTIAL INDEXES PIN THIS LITERAL: the order and spelling below
#: are the schema's, and changing either is a schema change.
#:
#: The values are two literals chosen here, so there is nothing to inject.
IN_FLIGHT_STATES_SQL: str = ", ".join(f"'{state}'" for state in IN_FLIGHT_STATES)

#: The states in which a job has NOT YET BEEN MATCHED TO A WORKER, and the
#: only ones whose CLAIM GATES an operator may still change.
#:
#: ``prio`` and ``app_version`` are both read by ``claim_jorb`` to decide who
#: may take the row (``prio <= the worker's ceiling``, ``app_version`` equal to
#: what the worker advertises or NULL). Editing either one after the claim
#: decides nothing -- the gate has already been passed -- and editing a
#: terminal job's is rewriting history. So the FIVE surfaces that offer those
#: edits -- ``JobClient`` and ``AdminAPI`` for both priority and version, plus
#: the WebSocket dashboard's ``adjust_priority`` -- all guard on exactly this
#: pair, and they get it from ``db.UPDATE_PRIORITY_SQL`` /
#: ``db.UPDATE_PRIORITY_MANY_SQL`` / ``db.UPDATE_APP_VERSION_SQL`` rather than
#: each spelling ``state IN ('queued', 'waiting')`` into its own SQL.
#:
#: The dashboard and the client's BULK re-prioritise are both in that count
#: because both had written their own copy of the guard, which is the shape of
#: the defect: every hand-written editable gate is another literal, and the
#: first one to be written wrong fails silently -- an edit that matches zero
#: rows, or one that rewrites a job somebody is already running.
#:
#: ``waiting`` is in it: a blocked job is not claimable YET, but it will be,
#: and it will be claimed under whatever gates it carries at that moment.
#:
#: Interpolated into SQL through ``PRE_CLAIM_STATES_SQL`` for the reason
#: ``TERMINAL_STATES_SQL`` is: the values are literals chosen here, and there
#: is nothing to inject.
PRE_CLAIM_STATES: tuple[str, ...] = ("queued", "waiting")

#: ``PRE_CLAIM_STATES`` as a SQL ``IN`` list.
PRE_CLAIM_STATES_SQL: str = ", ".join(f"'{state}'" for state in PRE_CLAIM_STATES)

#: The pseudo-state a job's history starts from. Not a ``jorbstate`` value:
#: ``jorb_history`` records the INSERT as an ``enqueued`` event so that every
#: job's audit trail has an origin, and a walk over that trail needs somewhere
#: to start.
HISTORY_ORIGIN = "enqueued"

#: source -> every state it may move to. The single declaration of the
#: platform's own FSM.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    # A fresh row is enqueued as queued (claimable) or waiting (blocked on a
    # dependency). The claimed/running entries cover a job claimed so fast
    # that the insert and the claim land in the same history read.
    HISTORY_ORIGIN: frozenset({"queued", "waiting", "claimed", "running"}),
    "waiting": frozenset({"queued", "cancelled"}),
    "queued": frozenset({"claimed", "cancelled"}),
    # claimed -> running normally; the monitor may requeue it, and any of the
    # three terminal writes can land before execution starts.
    #
    # `finished` is in here for symmetry with the code rather than because a
    # worker produces it: `finished`, `crashed` and `cancelled` share ONE
    # guard, `AND state IN (IN_FLIGHT_STATES_SQL)`, so all three permit the
    # same sources. The worker always records `run` (claimed -> running)
    # before completing, and both statements are fenced on the same epoch --
    # so if `run` no-ops, `finished` no-ops too and the job never moves. The
    # edge is therefore unreachable in practice and permitted by the SQL, and
    # the declaration describes what the statements CAN do. Narrowing the
    # guard instead would be a behaviour change on the hot path whose failure
    # mode is a silently uncompleted job, which is not a trade worth making
    # for a tidier table.
    # `waiting` is in here for the same reason it is on the terminal states
    # below: db.build_requeue_sql's target is a CASE, and a caller that widens
    # `allowed_states` to an in-flight state (the monitor's basis, and the DXE
    # fault tests') requeues a row that may still carry waitfor columns.
    "claimed": frozenset(
        {"running", "queued", "waiting", "cancelled", "crashed", "finished"}
    ),
    # running -> terminal, or back to queued: retry backoff, self-reschedule,
    # durable sleep, or a monitor requeue. The running -> running self-edge is
    # the idempotent `run` statement: ex()'s reconnect-replay of a run whose
    # COMMIT ack was lost re-applies it to the already-running row at the same
    # epoch, a no-op transition rather than a spurious "superseded".
    "running": frozenset(
        {"finished", "crashed", "cancelled", "queued", "waiting", "running"}
    ),
    # Terminal states are final EXCEPT for an explicit operator requeue. That
    # is the one edge that makes them "terminal for the platform" rather than
    # "immutable", and it is why retention deletes rows instead of trusting
    # that nothing will touch them again.
    #
    # THE REQUEUE HAS TWO TARGETS, not one. db.build_requeue_sql returns a row
    # that still carries a waitfor_job/waitfor_group to 'waiting', never to
    # 'queued': `cancelled` is retryable and a parked waiter is exactly what
    # the monitor's unsatisfiable-waiter sweep cancels, so requeueing straight
    # into 'queued' would hand claim_jorb a job whose dependency is unmet
    # (claim_jorb does not read the waitfor columns). The wake machinery then
    # releases it, or the unsatisfiable sweep cancels it again.
    "finished": frozenset({"queued", "waiting"}),
    "crashed": frozenset({"queued", "waiting"}),
    "cancelled": frozenset({"queued", "waiting"}),
}


def is_legal(source: str, target: str) -> bool:
    """May a job move from `source` to `target`?"""
    return target in LEGAL_TRANSITIONS.get(source, frozenset())


def illegal_transitions() -> frozenset[tuple[str, str]]:
    """Every (source, target) pair the machine forbids.

    The complement of the declaration, which is what a test asserting "this
    cannot happen" needs — enumerating what is allowed proves nothing about
    what is not.
    """
    return frozenset(
        (source, target)
        for source in JOB_STATES
        for target in JOB_STATES
        if source != target and not is_legal(source, target)
    )
