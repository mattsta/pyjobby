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
#: with ``pyjobby/sql/schema.sql`` by ``test_declared_states_match_the_enum``,
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

#: The states that represent work still in the system.
LIVE_STATES: tuple[str, ...] = tuple(
    state for state in JOB_STATES if state not in TERMINAL_STATES
)

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
    # guard, `AND state IN ('claimed', 'running')`, so all three permit the
    # same sources. The worker always records `run` (claimed -> running)
    # before completing, and both statements are fenced on the same epoch --
    # so if `run` no-ops, `finished` no-ops too and the job never moves. The
    # edge is therefore unreachable in practice and permitted by the SQL, and
    # the declaration describes what the statements CAN do. Narrowing the
    # guard instead would be a behaviour change on the hot path whose failure
    # mode is a silently uncompleted job, which is not a trade worth making
    # for a tidier table.
    "claimed": frozenset({"running", "queued", "cancelled", "crashed", "finished"}),
    # running -> terminal, or back to queued: retry backoff, self-reschedule,
    # durable sleep, or a monitor requeue. The running -> running self-edge is
    # the idempotent `run` statement: ex()'s reconnect-replay of a run whose
    # COMMIT ack was lost re-applies it to the already-running row at the same
    # epoch, a no-op transition rather than a spurious "superseded".
    "running": frozenset({"finished", "crashed", "cancelled", "queued", "running"}),
    # Terminal states are final EXCEPT for an explicit operator requeue. That
    # is the one edge that makes them "terminal for the platform" rather than
    # "immutable", and it is why retention deletes rows instead of trusting
    # that nothing will touch them again.
    "finished": frozenset({"queued"}),
    "crashed": frozenset({"queued"}),
    "cancelled": frozenset({"queued"}),
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
