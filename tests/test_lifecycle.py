"""The platform's own state machine, checked from both ends.

pyjobby enforces its lifecycle *by construction*: every statement that changes
``jorb.state`` carries an ``AND state IN (...)`` predicate, so an illegal
transition matches zero rows instead of raising. That is the right posture for
a system where the loser of a race must lose quietly, and it is stronger than
a validating trigger — the guard is in the same atomic statement as the write.

What it lacked was a declaration. The rules were spread across eight
statements in ``pj.py``, and the only written form of them lived in a test
file the package itself could not see. ``pyjobby.lifecycle`` now declares the
machine; this module checks that the declaration and the code agree, in both
directions:

* **statements → declaration** — no statement in ``STMTS`` can realise a
  transition the table does not permit, and none writes ``state`` without a
  guard at all;
* **declaration → database** — the states it names are exactly the labels of
  the ``jorbstate`` enum.

The third direction — *executed* transitions recorded in ``jorb_history`` —
is asserted over generated workloads in ``test_invariants.py``, which reads
the same declaration.

These are static checks over module data, so they are fast and need no
worker; only the enum comparison touches a database.
"""

from __future__ import annotations

import re

import pytest

from pyjobby.db import (
    REQUEUE_CLEARS_KEYS,
    WAKE_CLEARS_KEYS,
    build_requeue_sql,
)
from pyjobby.lifecycle import (
    JOB_STATES,
    LEGAL_TRANSITIONS,
    LIVE_STATES,
    TERMINAL_STATES,
    illegal_transitions,
    is_legal,
)
from pyjobby.monitor import (
    DEADLETTER_TIMED_OUT_SQL,
    RETRY_TIMED_OUT_SQL,
    SWEEP_DEAD_WORKER_JOBS_SQL,
    SWEEP_STUCK_CLAIMS_SQL,
    WAKE_WAITERS_SQL,
)
from pyjobby.pj import STMTS

#: An UPDATE's SET list: everything from `SET` to the clause that ends it.
#:
#: Extracted first, and the state assignment looked for INSIDE it, because
#: `state` is not always the first thing an UPDATE sets. The pattern used to be
#: `SET\s+state\s*=\s*'(\w+)'`, anchored to the word after SET -- so a
#: statement written `SET updated = now(), state = 'queued'` was read as
#: writing NO state at all, dropped silently out of the inventory below, and
#: every transition-legality assertion in this file simply never saw it. An
#: extractor whose failure mode is a smaller list is the worst kind of guard.
_SET_LIST = re.compile(
    r"\bSET\b(.*?)(?=\bWHERE\b|\bRETURNING\b|\bFROM\b|$)",
    re.IGNORECASE | re.DOTALL,
)

#: `state = 'x'` anywhere in a SET list — what a statement moves a job TO.
_WRITES_STATE = re.compile(r"\bstate\s*=\s*'(\w+)'", re.IGNORECASE)

#: `AND state = 'x'` or `AND state IN ('x', 'y')` — what it moves a job FROM.
_GUARDS_STATE = re.compile(
    r"AND\s+state\s+(?:=\s*'(\w+)'|IN\s*\(([^)]*)\))", re.IGNORECASE
)


def transition_in(sql: str) -> tuple[frozenset[str], str] | None:
    """The (sources, target) one statement implies, or None if it writes no
    state. A function so the extractor itself can be fed a synthetic
    statement and checked, rather than only ever being pointed at STMTS."""
    target = None
    for set_list in _SET_LIST.finditer(sql):
        target = _WRITES_STATE.search(set_list.group(1))
        if target is not None:
            break
    if target is None:
        return None
    guard = _GUARDS_STATE.search(sql)
    if guard is None:
        sources: frozenset[str] = frozenset()
    elif guard.group(1):
        sources = frozenset({guard.group(1)})
    else:
        sources = frozenset(
            part.strip().strip("'") for part in guard.group(2).split(",")
        )
    return sources, target.group(1)


def statement_transitions() -> dict[str, tuple[frozenset[str], str]]:
    """Every state change ``STMTS`` can perform: {name: (sources, target)}.

    Read out of the SQL text rather than maintained by hand, because a list
    maintained by hand is precisely the thing this is checking for. A new
    statement is picked up by existing, not by anyone remembering to add it.
    """
    found: dict[str, tuple[frozenset[str], str]] = {}
    for name, sql in STMTS.items():
        transition = transition_in(sql)
        if transition is not None:
            found[name] = transition
    return found


def test_the_extractor_finds_the_statements_that_change_state():
    """Guard the guard: an extractor that silently matched nothing would make
    every test below vacuously pass."""
    found = statement_transitions()
    assert {"run", "finished", "retry", "crashed", "cancelled"} <= set(found)
    assert found["run"] == (frozenset({"claimed", "running"}), "running")
    assert found["finished"] == (frozenset({"claimed", "running"}), "finished")


def test_the_extractor_sees_a_state_written_anywhere_in_the_set_list():
    """The failure that made the guard smaller than the thing it guards.

    Every statement in STMTS today happens to write `state` first, so the
    old anchored pattern found all of them and looked correct. The first one
    written the other way round -- an ordinary, reviewable way to write an
    UPDATE -- would have vanished from the inventory instead of failing, and
    nothing would have said so. Fed synthetically because the point is
    precisely that no real statement has this shape yet.
    """
    assert transition_in(
        "UPDATE jorb SET updated = now(), state = 'queued' "
        "WHERE id = $1 AND state IN ('crashed', 'cancelled') RETURNING id"
    ) == (frozenset({"crashed", "cancelled"}), "queued")

    # ...and the guard against the opposite failure: a WHERE-clause state test
    # is not a state WRITE, so a pure read must still extract nothing.
    assert transition_in("SELECT id FROM jorb WHERE state = 'queued'") is None
    assert (
        transition_in("UPDATE jorb SET awaited = FALSE WHERE state = 'running'") is None
    )


@pytest.mark.parametrize("statement", sorted(statement_transitions()))
def test_no_statement_realises_an_undeclared_transition(statement):
    """The predicates are the enforcement; this is what makes them reviewable.

    Each state-changing statement names the states it accepts and the state it
    writes. Every (source -> target) that implies must be one the declaration
    permits, so a new statement with a wrong or missing guard fails here rather
    than months later as a surprising row in ``jorb_history``.
    """
    sources, target = statement_transitions()[statement]
    assert sources, (
        f"STMTS[{statement!r}] writes state = {target!r} with no "
        f"`AND state IN (...)` guard: it can move a job there from ANY state, "
        f"including a terminal one"
    )
    illegal = sorted(source for source in sources if not is_legal(source, target))
    assert not illegal, (
        f"STMTS[{statement!r}] moves {illegal} -> {target!r}, which "
        f"pyjobby.lifecycle.LEGAL_TRANSITIONS does not permit"
    )


def test_every_statement_leaving_an_attempt_bumps_the_fence():
    """Any transition OUT of claimed/running advances ``run_epoch``.

    Leaving those states ends or abandons the current execution — and that
    execution may still be alive (an unstoppable synchronous thread, a
    worker mid-crash). Statements guarded ONLY by the epoch — checkpoints,
    events, mailbox sends, set-timeout — must stop applying the moment the
    row leaves the attempt, and a state guard alone does not stop them.
    The rule is stated in ``db.build_requeue_sql``'s docstring and in the
    schema's ``run_epoch`` comment; this makes it structural: a new
    statement that forgets the bump fails here.
    """
    attempt_states = {"claimed", "running"}
    leaving = {
        name
        for name, (sources, target) in statement_transitions().items()
        if sources <= attempt_states and target not in attempt_states
    }
    assert {"finished", "retry", "crashed", "cancelled", "reschedule"} <= leaving
    for name in leaving:
        assert "run_epoch = run_epoch + 1" in STMTS[name], (
            f"STMTS[{name!r}] moves a job out of its attempt without "
            f"advancing run_epoch; the abandoned execution's epoch-guarded "
            f"writes would keep applying"
        )
    # The monitor's two timeout outcomes and the shared operator requeue
    # leave an attempt the same way and follow the same rule.
    for sql in (
        RETRY_TIMED_OUT_SQL,
        DEADLETTER_TIMED_OUT_SQL,
        build_requeue_sql(("crashed",)),
    ):
        assert "run_epoch = run_epoch + 1" in sql


def _statements_that_return_a_job_to_queued() -> tuple[dict[str, str], dict[str, str]]:
    """(requeues, wakes) -- every statement in the platform that writes
    ``state = 'queued'``, split by which key-release fragment it owes.

    The ``STMTS`` half is DERIVED from the same parse the transition checks
    use, so the statement nobody remembers to add is added by existing. A
    statement whose only source state is 'waiting' is a waiter's WAKE and owes
    the smaller ``WAKE_CLEARS_KEYS`` (a waiting row can hold a deadline_key --
    'waiting' is outside jorb_deadline_idx, which is exactly why two waiters
    may share one -- but never a debounce_key, which is refused at the door).
    Everything else is a REQUEUE out of an attempt or a terminal state and
    owes all three columns.

    The statements that do not live in ``STMTS`` cannot be derived and are
    named here instead; the guard-the-guard assertions below make a rename
    that silently empties either half fail.
    """
    requeues: dict[str, str] = {}
    wakes: dict[str, str] = {}
    for name, (sources, target) in statement_transitions().items():
        if target != "queued":
            continue
        bucket = wakes if sources <= {"waiting"} else requeues
        bucket[f"pj.STMTS[{name}]"] = STMTS[name]

    requeues.update(
        {
            "db.build_requeue_sql": build_requeue_sql(("crashed",)),
            "db.build_requeue_sql/bulk": build_requeue_sql(("crashed",), many=True),
            "monitor.RETRY_TIMED_OUT_SQL": RETRY_TIMED_OUT_SQL,
            "monitor.SWEEP_DEAD_WORKER_JOBS_SQL": SWEEP_DEAD_WORKER_JOBS_SQL,
            "monitor.SWEEP_STUCK_CLAIMS_SQL": SWEEP_STUCK_CLAIMS_SQL,
        }
    )
    wakes["monitor.WAKE_WAITERS_SQL"] = WAKE_WAITERS_SQL
    return requeues, wakes


def test_the_queued_target_split_is_not_vacuous():
    """Guard the guard: a parse that found nothing, or that mis-bucketed the
    wakes, would make the binding test below pass over an empty set."""
    requeues, wakes = _statements_that_return_a_job_to_queued()
    derived = {name for name in requeues | wakes if name.startswith("pj.STMTS[")}
    assert derived, "nothing in STMTS was parsed as writing state = 'queued'"
    assert "pj.STMTS[retry]" in requeues
    assert "pj.STMTS[reschedule]" in requeues, (
        "a durable sleep parks the row back in 'queued' and owes the release "
        "like every other requeue; it was the statement that did not"
    )
    assert "pj.STMTS[enqueue-next-self-finished]" in wakes
    assert "pj.STMTS[enqueue-next-if-peer-group-is-finished]" in wakes


def test_every_statement_returning_a_job_to_queued_releases_its_dedupe_keys():
    """A dedupe key's collapse duty ends the first time its row leaves
    'queued', so the statement that puts it BACK must not carry the key in.

    ``jorb_deadline_idx`` and ``jorb_debounce_idx`` are partial UNIQUE indexes
    on ``state = 'queued'``. A row that re-enters them while a later burst
    holds the same key makes the requeue itself raise -- inside a worker's
    failure handler, inside a durable ``sleep()``, or in the middle of a batch
    sweep whose every OTHER row then requeues nothing either.

    Bound to the fragments rather than to the column names so the rule stays
    ONE rule: a statement that spelled the release out by hand would pass a
    substring check and then drift from ``db.REQUEUE_CLEARS_KEYS`` the day it
    changes.
    """
    requeues, wakes = _statements_that_return_a_job_to_queued()
    for name, sql in requeues.items():
        assert REQUEUE_CLEARS_KEYS in sql, (
            f"{name} returns a job to 'queued' without db.REQUEUE_CLEARS_KEYS: "
            f"the row re-enters partial unique indexes its key was already "
            f"released from, and the statement raises when a later burst "
            f"holds that key"
        )
    for name, sql in wakes.items():
        assert WAKE_CLEARS_KEYS in sql, (
            f"{name} wakes a waiter into 'queued' without "
            f"db.WAKE_CLEARS_KEYS: two waiters of one upstream may legally "
            f"hold the same deadline_key, and this is ONE update over all of "
            f"them, so the collision rolls back the wake of every other waiter"
        )
        assert "debounce_key" not in sql, (
            f"{name} clears debounce columns a 'waiting' row can never hold "
            f"(a debounced enqueue with waitfor_* is refused at the door); "
            f"the wake's release is deliberately the smaller one"
        )
    for name, sql in (requeues | wakes).items():
        assert "identity_key" not in sql, (
            f"{name} must NOT clear identity_key: jorb_identity_idx has no "
            f"state predicate and the row holds that key for life"
        )


def test_every_declared_state_is_reachable_and_leaves_somewhere():
    """No state in the declaration is stranded.

    A state nothing reaches, or one with no way out, means the table and the
    statements have drifted — which is what this module exists to catch.
    """
    reachable = {target for targets in LEGAL_TRANSITIONS.values() for target in targets}
    assert set(JOB_STATES) <= reachable, (
        f"states {sorted(set(JOB_STATES) - reachable)} are declared but "
        f"nothing transitions into them"
    )
    for state in JOB_STATES:
        assert LEGAL_TRANSITIONS.get(state), (
            f"{state!r} is a declared state with no outgoing transitions; "
            f"even terminal states have the operator requeue edge"
        )


def test_terminal_states_lead_only_back_to_queued():
    """What "terminal" means here, stated as a property.

    A terminal job is done for the platform, not immutable: an operator may
    requeue it, and that single edge is why retention DELETES rows rather than
    trusting that nothing will touch them again.
    """
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset({"queued"}), (
            f"{state!r} leads to {sorted(LEGAL_TRANSITIONS[state])}; a terminal "
            f"state's only edge is the operator requeue"
        )


def test_live_and_terminal_partition_the_state_space():
    assert set(LIVE_STATES) | set(TERMINAL_STATES) == set(JOB_STATES)
    assert not set(LIVE_STATES) & set(TERMINAL_STATES)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("finished", "running"),  # a terminal job cannot resume in place
        ("queued", "running"),  # claiming is not optional
        ("waiting", "claimed"),  # a blocked job is not claimable
        ("cancelled", "finished"),  # cancellation is not overturned by success
    ],
)
def test_the_transitions_that_must_never_happen(source, target):
    """Named cases from the complement, so the intent is in the test file.

    Enumerating what is allowed proves nothing about what is not, and these
    four are the ones whose absence a reader should be able to confirm without
    re-deriving the whole table.
    """
    assert not is_legal(source, target)
    assert (source, target) in illegal_transitions()


async def test_declared_states_match_the_enum(db_pool):
    """``JOB_STATES`` is the ``jorbstate`` enum, checked against the database.

    A tuple in Python claiming to mirror a type in PostgreSQL is worth exactly
    as much as the check that they agree.
    """
    labels = await db_pool.fetch(
        "SELECT enumlabel FROM pg_enum "
        "WHERE enumtypid = 'jorbstate'::regtype ORDER BY enumsortorder"
    )
    assert {row["enumlabel"] for row in labels} == set(JOB_STATES)


async def test_every_python_state_list_is_the_same_list():
    """Three modules declare the jorbstate labels: lifecycle.JOB_STATES (the
    checked-against-the-database home, above), db.JobState (the EXPORTED enum
    that validates user input in web_admin), and the migrations manifest's
    REQUIRED_ENUM_LABELS. Binding the other two to the first means adding a
    state cannot silently break the one that faces users."""
    from pyjobby import migrations
    from pyjobby.db import JobState

    assert tuple(JobState) == JOB_STATES
    assert migrations.REQUIRED_ENUM_LABELS["jorbstate"] == JOB_STATES
