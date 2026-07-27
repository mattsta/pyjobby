"""Durable state machines: the declaration, and the runtime.

Two halves, deliberately separated. The declaration tests need no database at
all — every check `StateMachineJob.__init_subclass__` makes is decidable from
the class body, which is the reason it happens at import rather than on the
transition that would have hit the mistake. The runtime tests drive a real
worker and assert the three properties that are the whole point of putting a
state machine in a job system: state survives a crash, an action that
completed never runs a second time, and an idle machine's replay cost does
not grow with how long it has been idle.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyjobby.fsm import EVENT_TOPIC, STATE_KEY, MachineDefinitionError, Transition
from pyjobby.statemachine import StateMachineJob

from .conftest import wait_for_job_state

# No module-level asyncio mark: half these tests are synchronous by design,
# because half of what a machine declaration promises is checkable without a
# database. `asyncio_mode = "auto"` picks up the async half on its own.


# =========================================================================
# The declaration — no database
# =========================================================================


class Sample(StateMachineJob):
    """Exercises all three edge shapes: Transition, tuple, and bare string."""

    initial = "a"
    final = frozenset({"c"})
    transitions = {
        "a": {"go": ("b", "act"), "skip": "c"},
        "b": {"finish": Transition("c")},
    }

    async def act(self, event: str, payload: Any) -> str:
        return "acted"


def test_the_three_edge_shapes_normalize_to_one():
    """A tuple, a bare string and a Transition mean the same thing."""
    assert Sample.edges["a"]["go"] == Transition("b", "act")
    assert Sample.edges["a"]["skip"] == Transition("c", None)
    assert Sample.edges["b"]["finish"] == Transition("c", None)


def test_states_includes_targets_that_are_never_sources():
    """A final state has no outgoing edges, so it appears only as a target."""
    assert Sample.states() == {"a", "b", "c"}


def test_may_answers_without_touching_the_database():
    """`may_<trigger>`, as one function rather than a generated method each.

    Worth having here in a way it is not in-process: an event sent to a state
    with no edge for it is CONSUMED and dropped, so asking first is the only
    way a client avoids losing it.
    """
    assert Sample.may("a", "go")
    assert not Sample.may("a", "finish")
    assert not Sample.may("c", "go")  # final: no edges at all


def test_mermaid_renders_the_declaration():
    diagram = Sample.to_mermaid()
    assert diagram.startswith("stateDiagram-v2")
    assert "[*] --> a" in diagram
    assert "a --> b: go / act" in diagram  # the action is named on the edge
    assert "a --> c: skip" in diagram  # no action, no slash
    assert "c --> [*]" in diagram


def test_an_abstract_base_with_no_transitions_is_allowed():
    """Intermediate bases are legitimate; they simply cannot run."""

    class Abstract(StateMachineJob):
        wait_seconds = 1.0

    assert Abstract.edges == {}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(
            {"initial": "", "transitions": {"a": {"go": "b"}}},
            "declares no initial state",
            id="no-initial",
        ),
        pytest.param(
            {"initial": "nowhere", "transitions": {"a": {"go": "b"}}},
            "is not a state of the machine",
            id="initial-not-a-state",
        ),
        pytest.param(
            {
                "initial": "a",
                "transitions": {"a": {"go": "typo_state"}},
                "final": frozenset({"b"}),
            },
            "which no transition reaches or leaves",
            id="final-unreachable",
        ),
        pytest.param(
            {
                "initial": "a",
                "final": frozenset({"b"}),
                "transitions": {"a": {"go": "b"}, "b": {"back": "a"}},
            },
            "is final but has outgoing transitions",
            id="final-with-edges",
        ),
        pytest.param(
            {"initial": "a", "transitions": {"a": {"go": ("b", "missing_method")}}},
            "is not a method of",
            id="unknown-action",
        ),
    ],
)
def test_a_broken_declaration_fails_at_import(body, message):
    """Every one of these is decidable from the class body alone.

    So it is a startup failure with a precise message, not a crash weeks
    later on the one edge nobody exercised.
    """
    with pytest.raises(MachineDefinitionError, match=message):
        type("Broken", (StateMachineJob,), body)


def test_an_unreachable_state_fails_at_import():
    """A state nothing reaches is a typo in a target name almost every time."""
    with pytest.raises(MachineDefinitionError, match="no transition reaches them"):
        type(
            "Orphan",
            (StateMachineJob,),
            {
                "initial": "a",
                "final": frozenset({"b"}),
                "transitions": {"a": {"go": "b"}, "orphan": {"x": "b"}},
            },
        )


# =========================================================================
# The runtime — a real worker, a real database
# =========================================================================


async def machine_state(pool, job_id: int) -> str | None:
    """The machine's committed state, read the way a client reads it."""
    value = await pool.fetchval(
        "SELECT value FROM jorb_event WHERE job_id = $1 AND key = $2",
        job_id,
        STATE_KEY,
    )
    return None if value is None else str(value["state"])


async def enqueue_machine(pool, klass: str, queue: str) -> int:
    job_id: int = await pool.fetchval(
        """INSERT INTO jorb (job_class, kwargs, queue, admin_data)
           VALUES ($1, '{}', $2, $3) RETURNING id""",
        klass,
        queue,
        {"max_retries": 3, "initial_retry_delay": 0},
    )
    return job_id


async def post(pool, job_id: int, event: str, **payload: Any) -> None:
    """Deliver a transition event the way `client.send_message` does."""
    await pool.execute(
        "INSERT INTO jorb_mailbox (dest_job_id, topic, message) VALUES ($1,$2,$3)",
        job_id,
        EVENT_TOPIC,
        {"event": event, **payload},
    )


async def wait_for_machine_state(
    pool, job_id: int, want: str, tries: int = 100
) -> None:
    import asyncio

    for _ in range(tries):
        if await machine_state(pool, job_id) == want:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"machine {job_id} never reached {want!r} "
        f"(stuck at {await machine_state(pool, job_id)!r})"
    )


async def test_a_machine_runs_its_transitions_and_finishes(
    live_worker, unique_queue, db_pool
):
    """The whole loop: two events, two actions, a final state."""
    await live_worker()
    job_id = await enqueue_machine(
        db_pool, "tests.machine_jobs.OrderMachine", unique_queue
    )

    await wait_for_machine_state(db_pool, job_id, "awaiting_payment")
    await post(db_pool, job_id, "paid", amount=100)
    await wait_for_machine_state(db_pool, job_id, "packing")
    await post(db_pool, job_id, "packed")

    row = await wait_for_job_state(db_pool, job_id, ("finished",))
    assert row["result"]["final_state"] == "shipped"
    assert await machine_state(db_pool, job_id) == "shipped"


async def test_an_unhandled_event_is_consumed_and_the_machine_stays_put(
    live_worker, unique_queue, db_pool
):
    """The mailbox row is gone either way — that is why `may()` exists."""
    await live_worker()
    job_id = await enqueue_machine(
        db_pool, "tests.machine_jobs.OrderMachine", unique_queue
    )
    await wait_for_machine_state(db_pool, job_id, "awaiting_payment")

    await post(db_pool, job_id, "packed")  # legal in `packing`, not here
    await post(db_pool, job_id, "paid")

    await wait_for_machine_state(db_pool, job_id, "packing")
    consumed = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id=$1 AND consumed_at IS NOT NULL",
        job_id,
    )
    assert consumed == 2, "both messages were consumed; only one caused a transition"


async def test_an_action_that_completed_never_runs_twice(
    live_worker, unique_queue, db_pool
):
    """The durability claim, proved by killing the machine inside its action.

    `CrashOnceMachine` records its attempt count in a jorb_event before
    raising, so the count distinguishes "the action re-ran" from "the action
    was replayed": the retry re-executes the FAILED step (a failed step is
    not a result), reaching attempt 2, and then never runs it again.
    """
    await live_worker()
    job_id = await enqueue_machine(
        db_pool, "tests.machine_jobs.CrashOnceMachine", unique_queue
    )
    await wait_for_machine_state(db_pool, job_id, "start")
    await post(db_pool, job_id, "go")

    row = await wait_for_job_state(db_pool, job_id, ("finished",))
    assert row["result"]["final_state"] == "done"
    assert row["error_count"] == 1, "exactly one failed attempt"

    attempts = await db_pool.fetchval(
        "SELECT value FROM jorb_event WHERE job_id=$1 AND key='attempts'", job_id
    )
    assert attempts["n"] == 2, "the action ran twice: once crashing, once succeeding"

    # And the recorded step is the successful one, keyed by the transition.
    steps = await db_pool.fetch(
        "SELECT name, output, error FROM jorb_step WHERE job_id=$1 ORDER BY step_seq",
        job_id,
    )
    succeeded = [s for s in steps if s["name"] == "start--go->done"]
    assert len(succeeded) == 1
    assert succeeded[0]["error"] is None
    assert succeeded[0]["output"]["attempt"] == 2


async def test_an_idle_machine_does_not_accumulate_checkpoints(
    live_worker, unique_queue, db_pool
):
    """The bound that makes an indefinitely-living machine viable.

    `IdleMachine` waits 0.25s and sleeps 0.25s, so it turns over several
    times a second and receives nothing at all. Every turn records a recv
    and a sleep; without `compact()` the log would grow by two rows per turn
    forever, and each subsequent wake would pay to load all of them
    (measured at ~0.9us and 260 bytes per checkpoint by `pj-bench replay`).

    The assertion is on the SHAPE of the growth, not on an exact count: the
    log must stay at one turn's worth however many turns have elapsed.
    """
    import asyncio

    await live_worker()
    job_id = await enqueue_machine(
        db_pool, "tests.machine_jobs.IdleMachine", unique_queue
    )
    await wait_for_machine_state(db_pool, job_id, "waiting")

    async def checkpoints() -> int:
        return int(
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step WHERE job_id = $1", job_id
            )
        )

    # Let it turn over many times, sampling as it goes.
    samples = []
    for _ in range(12):
        await asyncio.sleep(0.4)
        samples.append(await checkpoints())

    runs = await db_pool.fetchval("SELECT run_count FROM jorb WHERE id=$1", job_id)
    assert runs > 2, f"the machine should have woken repeatedly, ran {runs} times"
    assert max(samples) <= 2, (
        f"an idle machine's checkpoint log grew to {max(samples)} rows across "
        f"{runs} wakes (samples: {samples}); compaction is not bounding it"
    )
