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

from pyjobby.lifecycle import (
    JOB_STATES,
    LEGAL_TRANSITIONS,
    LIVE_STATES,
    TERMINAL_STATES,
    illegal_transitions,
    is_legal,
)
from pyjobby.pj import STMTS

#: `SET state = 'x'` — what a statement moves a job TO.
_WRITES_STATE = re.compile(r"SET\s+state\s*=\s*'(\w+)'", re.IGNORECASE)

#: `AND state = 'x'` or `AND state IN ('x', 'y')` — what it moves a job FROM.
_GUARDS_STATE = re.compile(
    r"AND\s+state\s+(?:=\s*'(\w+)'|IN\s*\(([^)]*)\))", re.IGNORECASE
)


def statement_transitions() -> dict[str, tuple[frozenset[str], str]]:
    """Every state change ``STMTS`` can perform: {name: (sources, target)}.

    Read out of the SQL text rather than maintained by hand, because a list
    maintained by hand is precisely the thing this is checking for. A new
    statement is picked up by existing, not by anyone remembering to add it.
    """
    found: dict[str, tuple[frozenset[str], str]] = {}
    for name, sql in STMTS.items():
        target = _WRITES_STATE.search(sql)
        if target is None:
            continue
        guard = _GUARDS_STATE.search(sql)
        if guard is None:
            sources: frozenset[str] = frozenset()
        elif guard.group(1):
            sources = frozenset({guard.group(1)})
        else:
            sources = frozenset(
                part.strip().strip("'") for part in guard.group(2).split(",")
            )
        found[name] = (sources, target.group(1))
    return found


def test_the_extractor_finds_the_statements_that_change_state():
    """Guard the guard: an extractor that silently matched nothing would make
    every test below vacuously pass."""
    found = statement_transitions()
    assert {"run", "finished", "retry", "crashed", "cancelled"} <= set(found)
    assert found["run"] == (frozenset({"claimed"}), "running")
    assert found["finished"] == (frozenset({"claimed", "running"}), "finished")


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
