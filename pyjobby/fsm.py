"""What a durable state machine *is*, independent of how it runs.

The declaration format and every check over it live here, and nothing in this
module imports anything from pyjobby. That is deliberate rather than tidy:
both ends of a machine need to agree on it, and the two ends sit on opposite
sides of an import cycle.

* ``pyjobby.statemachine`` is the worker end — a ``Job`` subclass that runs
  the loop. It imports ``pyjobby.pj``.
* ``pyjobby.client`` is the application end — it enqueues machines, sends them
  events and reads their state. ``pyjobby.pj`` imports *it*.

So the shared vocabulary — the mailbox topic, the event key, the state key,
what an edge looks like, what makes a declaration valid — cannot live in
either one without duplicating it into the other, and a duplicated constant is
a constant that drifts. It lives here, and both import it.

The practical payoff is that a client can hold the same declaration the worker
runs and answer questions about it locally: what states exist, whether an
event would be accepted right now, what the diagram looks like. None of that
needs a database, and asking the database would be the wrong shape anyway --
the declaration is in the code, not in a row.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

#: The ``jorb_event`` key a machine keeps its current state under. Reserved: a
#: machine that also publishes application events should use other keys.
STATE_KEY = "machine.state"

#: The ``jorb_mailbox`` topic machine events arrive on. Scoped so a machine can
#: still use ``send()``/``recv()`` on other topics for its own purposes without
#: those messages being read as transitions.
EVENT_TOPIC = "fsm"

#: The key inside a message payload naming the event. The rest of the payload
#: is the application's, and is handed to the action untouched.
EVENT_FIELD = "event"


class MachineDefinitionError(TypeError):
    """A machine's declaration is inconsistent — raised at class creation.

    Every check this covers is decidable from the declaration alone, so it is
    made once at import rather than on the transition that would have hit it.
    A typo in a target state name is otherwise a crash weeks later, in
    production, on the one edge nobody exercised.
    """


class Transition(NamedTuple):
    """One edge: where it goes, and what runs on the way."""

    target: str
    action: str | None = None


#: The three shapes a declaration may use for an edge. A bare string is
#: shorthand for "this target, no action"; a 2-tuple is (target, action).
Edge = Transition | tuple[str, str | None] | str

#: What a subclass writes: {source_state: {event_name: Edge}}.
TransitionSpec = Mapping[str, Mapping[str, Edge]]

#: The resolved form everything else works with.
EdgeTable = dict[str, dict[str, Transition]]


def normalize(spec: TransitionSpec) -> EdgeTable:
    """Accept the three edge shapes and return exactly one of them."""
    table: EdgeTable = {}
    for source, edges in spec.items():
        row: dict[str, Transition] = {}
        for event, edge in edges.items():
            if isinstance(edge, Transition):
                row[event] = edge
            elif isinstance(edge, str):
                row[event] = Transition(edge, None)
            else:
                target, action = edge
                row[event] = Transition(target, action)
        table[source] = row
    return table


def states(edges: EdgeTable) -> frozenset[str]:
    """Every state named anywhere in the table.

    Includes targets that are never sources, which is what final states are.
    """
    return frozenset(edges) | frozenset(
        edge.target for row in edges.values() for edge in row.values()
    )


def may(edges: EdgeTable, state: str, event: str) -> bool:
    """Would `event` be accepted in `state`?

    One function rather than a generated method per event, and worth having:
    an event sent to a state with no edge for it is CONSUMED and dropped,
    because ``recv()`` has already taken the mailbox row and checkpointed
    taking it. Asking first is how a caller avoids losing one.
    """
    return event in edges.get(state, {})


def validate(
    name: str,
    edges: EdgeTable,
    initial: str,
    final: frozenset[str],
    has_action: object = None,
) -> None:
    """Raise ``MachineDefinitionError`` if the declaration is inconsistent.

    ``has_action`` is an optional predicate taking an action name and
    returning whether it resolves to something callable. The worker end passes
    one that looks the method up on the class; a client holding only a
    declaration passes none, because it has no class to look on and the worker
    will have checked already.
    """
    if not edges:
        return
    if not initial:
        raise MachineDefinitionError(f"{name} declares no initial state")

    known = states(edges)
    if initial not in known:
        raise MachineDefinitionError(
            f"{name}.initial = {initial!r} is not a state of the machine; "
            f"known states are {sorted(known)}"
        )
    for bad in sorted(final - known):
        raise MachineDefinitionError(
            f"{name}.final contains {bad!r}, which no transition reaches or leaves"
        )
    for source, row in edges.items():
        if source in final:
            raise MachineDefinitionError(
                f"{name}: {source!r} is final but has outgoing transitions "
                f"{sorted(row)} — a final state is where the job returns, so "
                f"those edges can never fire"
            )
        for event, edge in row.items():
            if edge.target not in known:
                raise MachineDefinitionError(
                    f"{name}: {source!r} --{event}--> {edge.target!r} names a "
                    f"state that appears nowhere else; known states are "
                    f"{sorted(known)}"
                )
            if (
                edge.action is not None
                and callable(has_action)
                and not has_action(edge.action)
            ):
                raise MachineDefinitionError(
                    f"{name}: {source!r} --{event}--> {edge.target!r} names "
                    f"action {edge.action!r}, which is not a method of {name}"
                )
    reached = {edge.target for row in edges.values() for edge in row.values()}
    unreachable = known - {initial} - reached
    if unreachable:
        raise MachineDefinitionError(
            f"{name}: states {sorted(unreachable)} are not the initial state "
            f"and no transition reaches them"
        )


def to_mermaid(edges: EdgeTable, initial: str, final: frozenset[str]) -> str:
    """The declaration as a Mermaid ``stateDiagram-v2``.

    Renders the *declaration*, never the database, so it costs nothing at
    runtime and is safe to call from a docs build, a CLI, or a client that has
    never talked to a worker.
    """
    lines = ["stateDiagram-v2", f"    [*] --> {initial}"]
    for source, row in sorted(edges.items()):
        for event, edge in sorted(row.items()):
            label = f"{event} / {edge.action}" if edge.action else event
            lines.append(f"    {source} --> {edge.target}: {label}")
    lines.extend(f"    {state} --> [*]" for state in sorted(final))
    return "\n".join(lines)
