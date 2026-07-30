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
from .machine_jobs import OrderMachine, RelayMachine

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


async def test_both_hooks_are_called(live_worker, unique_queue, db_pool):
    """`on_unhandled` and `on_transition` are the two extension points.

    Neither is a checkpointed step, which is deliberate and documented:
    `on_transition` re-runs on replay, so effects belong in actions. This
    asserts only that a subclass's overrides are reached at all — the
    behaviour a subclass depends on.
    """
    await live_worker()
    job_id = await enqueue_machine(
        db_pool, "tests.machine_jobs.ObservedMachine", unique_queue
    )
    await wait_for_machine_state(db_pool, job_id, "start")

    await post(db_pool, job_id, "nonsense")  # no edge for this in 'start'
    await post(db_pool, job_id, "go")

    row = await wait_for_job_state(db_pool, job_id, ("finished",))
    assert row["result"]["final_state"] == "done"

    unhandled = await db_pool.fetchval(
        "SELECT value FROM jorb_event WHERE job_id=$1 AND key='saw.unhandled'", job_id
    )
    assert unhandled == {"state": "start", "event": "nonsense"}

    transition = await db_pool.fetchval(
        "SELECT value FROM jorb_event WHERE job_id=$1 AND key='saw.transition'", job_id
    )
    assert transition == {"source": "start", "event": "go", "target": "done"}


async def test_a_machine_can_read_another_jobs_event_by_id(
    live_worker, unique_queue, db_pool
):
    """`get_event(key, job_id=...)`: observing a peer without a mailbox.

    The cross-job form is the reason `get_event` takes a job id at all, and
    it is what lets one machine watch another's published state directly.
    """
    await live_worker()
    peer = await enqueue_machine(
        db_pool, "tests.machine_jobs.OrderMachine", unique_queue
    )
    await wait_for_machine_state(db_pool, peer, "awaiting_payment")

    reader = await enqueue_machine(
        db_pool, "tests.machine_jobs.PeerReaderMachine", unique_queue
    )
    await wait_for_machine_state(db_pool, reader, "reading")
    await post(db_pool, reader, "look", peer=peer)

    row = await wait_for_job_state(db_pool, reader, ("finished",))
    assert row["result"]["final_state"] == "read"

    seen = await db_pool.fetchval(
        "SELECT value FROM jorb_event WHERE job_id=$1 AND key='peer.state'", reader
    )
    assert seen == {"state": "awaiting_payment"}


# =========================================================================
# The turn is one commit — resume from every point inside it
# =========================================================================
# A machine holds two durable facts: the state event it publishes, and the
# checkpoint log of how it got there. It re-derives its position from the
# first and replays the second forward from that position, so a pair in which
# the two disagree describes no machine that can run.
#
# The pair that used to be reachable was (NEW state, PREVIOUS turn's log): the
# state was published, and the log was wiped at the top of the NEXT iteration,
# so a crash in between committed one without the other. The next attempt then
# re-derived the new state and replayed a log recorded against the old one --
# the recv handed the previous turn's event to a state with no edge for it,
# and the sequence number after that found a checkpoint under a different
# name. NondeterminismError, deterministically, on that attempt and on every
# retry of it, because nothing about the failed attempt changed the log. Only
# a checkpoint-wiping `rerun --fresh` recovered such a job.
#
# These drive the real `StateMachineJob.task()` from a chosen resume point. A
# live worker cannot be stopped between two adjacent commits on demand, and
# what is under test is precisely what the object does when it is started
# again with a given (state, log) pair -- so the pair is constructed, from the
# checkpoints a real turn of the same machine actually recorded.


async def _machine_row(pool, queue: str, klass: str) -> dict[str, Any]:
    """A claimed-looking job row for `klass`, ready to be driven directly."""
    row = await pool.fetchrow(
        """INSERT INTO jorb (job_class, kwargs, queue, state, run_epoch, run_count)
           VALUES ($1, '{}', $2, 'running', 1, 1) RETURNING *""",
        klass,
        queue,
    )
    return dict(row)


async def _publish(pool, job_id: int, state: str) -> None:
    await pool.execute(
        "INSERT INTO jorb_event (job_id, key, value) VALUES ($1, $2, $3) "
        "ON CONFLICT (job_id, key) DO UPDATE SET value = EXCLUDED.value",
        job_id,
        STATE_KEY,
        {"state": state},
    )


async def _reference_turn(db_pool, db_params, queue: str) -> list[dict[str, Any]]:
    """Drive one real turn's primitives and return the log it left MID-TURN.

    Everything below is built from this, so a renamed step or a re-ordered
    primitive changes the cases rather than leaving them asserting about a
    shape the code no longer has. Stopped before the boundary on purpose:
    the boundary is what discards the log, and the log is the thing wanted.
    """
    from pyjobby import db as db_mod

    from .utils.dxe import connection_bound_job

    peer = await _machine_row(db_pool, queue, "tests.machine_jobs.OrderMachine")
    row = await _machine_row(db_pool, queue, "tests.machine_jobs.RelayMachine")
    await _publish(db_pool, row["id"], "start")
    await post(db_pool, row["id"], "go", peer=peer["id"])

    conn = await db_mod.connect(**db_params)
    try:
        machine = await connection_bound_job(conn, row, cls=RelayMachine)
        payload = await machine.recv(topic=EVENT_TOPIC, timeout=5)
        await machine.step("start--go->relayed", machine.relay, "go", payload)
    finally:
        await conn.close()

    log = [
        dict(s)
        for s in await db_pool.fetch(
            "SELECT step_seq, name, output FROM jorb_step "
            "WHERE job_id = $1 ORDER BY step_seq",
            row["id"],
        )
    ]
    assert [s["name"] for s in log] == [
        f"dxe.recv:{EVENT_TOPIC}",
        "start--go->relayed",
        f"dxe.send:{peer['id']}:relay",
    ], f"the turn's shape changed; the crash points are built from it: {log}"
    return log


#: (label, sequences already committed, published state, is the event still
#: pending, how many times the action must run, how many messages it must
#: deliver).
#:
#: Sequence 1 is the recv -- the mailbox row is consumed and checkpointed by
#: ONE statement, so "consumed but unrecorded" is not a reachable point. 2 is
#: the transition's action step, and 3 is the ``send()`` nested inside it:
#: allocated UNDER the action, and therefore stranded ABOVE the action's own
#: checkpoint the moment that checkpoint fast-forwards.
CRASH_POINTS = [
    ("before the recv", [], "start", True, 1, 1),
    ("after the recv", [1], "start", False, 1, 1),
    ("after the nested send", [1, 3], "start", False, 1, 0),
    ("after the action's checkpoint", [1, 2, 3], "start", False, 0, 0),
    ("after the boundary", [], "relayed", False, 0, 0),
]


@pytest.mark.parametrize(
    ("label", "committed", "state", "pending", "action_runs", "delivered"),
    CRASH_POINTS,
    ids=[case[0].replace(" ", "-") for case in CRASH_POINTS],
)
async def test_a_machine_converges_from_every_crash_point_in_a_turn(
    db_pool,
    db_params,
    unique_queue,
    label,
    committed,
    state,
    pending,
    action_runs,
    delivered,
):
    """Resumed at any point inside a turn, the machine reaches the same state.

    ``[1, 2, 3]`` is the case that mattered: the action's checkpoint has
    committed, the boundary has not, and the ``send()`` nested inside the
    action has left sequence 3 above the action's own. No replay re-allocates
    that sequence -- the action fast-forwards instead of running -- so the
    next primitive claimed it, found a checkpoint under a different name, and
    raised. The compaction that would have cleared it refused forever, because
    the log's high-water mark was permanently out of the replaying execution's
    reach; the turn boundary wipes unconditionally for exactly that reason.

    ``action_runs``/``delivered`` keep this a durability test rather than a
    liveness one: a checkpoint that exists must never be re-executed, and one
    that does not must be.
    """
    from pyjobby import db as db_mod

    from .utils.dxe import connection_bound_job

    log = await _reference_turn(db_pool, db_params, unique_queue)

    peer = await _machine_row(db_pool, unique_queue, "tests.machine_jobs.OrderMachine")
    row = await _machine_row(db_pool, unique_queue, "tests.machine_jobs.RelayMachine")
    await _publish(db_pool, row["id"], state)

    if pending:
        await post(db_pool, row["id"], "go", peer=peer["id"])
    # the event that takes the machine out of 'relayed', pending throughout:
    # the turn under test has to END somewhere for the next one to expose a
    # sequence number the stranded checkpoint could collide with
    await post(db_pool, row["id"], "finish")

    outputs = {
        1: {"event": "go", "peer": peer["id"]},
        2: {"sent_to": peer["id"]},
        3: log[2]["output"],
    }
    names = {
        1: log[0]["name"],
        2: log[1]["name"],
        3: f"dxe.send:{peer['id']}:relay",
    }
    for seq in committed:
        await db_pool.execute(
            "INSERT INTO jorb_step (job_id, step_seq, name, output, error, "
            "run_epoch, started, finished) "
            "VALUES ($1, $2, $3, $4, NULL, 1, now(), now())",
            row["id"],
            seq,
            names[seq],
            outputs[seq],
        )

    conn = await db_mod.connect(**db_params)
    try:
        machine = await connection_bound_job(conn, row, cls=RelayMachine)
        result = await machine.task()
    finally:
        await conn.close()

    assert result["final_state"] == "done", (
        f"resumed {label}, the machine did not converge on its next state"
    )
    assert await machine_state(db_pool, row["id"]) == "done"

    ran = await db_pool.fetchval(
        "SELECT value FROM jorb_event WHERE job_id=$1 AND key='ran.relay'", row["id"]
    )
    assert (0 if ran is None else int(ran["n"])) == action_runs, (
        f"resumed {label}, the action ran the wrong number of times"
    )
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id = $1", peer["id"]
        )
        == delivered
    ), f"resumed {label}, the nested send was not exactly-once"


async def test_a_machine_recovers_from_a_state_ahead_of_its_log(
    db_pool, db_params, unique_queue
):
    """The historically-poisoned pair, fed to the machine directly.

    (new state, previous turn's log) is no longer reachable -- the test below
    is what makes that true -- but a database can hold it for other reasons
    (an operator's edit, a restore, a job that ran an older build), and the
    answer to it must not be "raise the same NondeterminismError on every
    retry forever". It is not, because the boundary wipes unconditionally: the
    replayed event is simply unhandled in the new state, the log goes with it,
    and the machine carries on from where it says it is.
    """
    from pyjobby import db as db_mod

    from .utils.dxe import connection_bound_job

    row = await _machine_row(db_pool, unique_queue, "tests.machine_jobs.OrderMachine")
    # published 'packing'...
    await _publish(db_pool, row["id"], "packing")
    # ...over the log of the turn that GOT it to 'packing'
    await db_pool.execute(
        "INSERT INTO jorb_step (job_id, step_seq, name, output, error, run_epoch,"
        " started, finished) VALUES "
        "($1, 1, $2, $3, NULL, 1, now(), now()), "
        "($1, 2, 'awaiting_payment--paid->packing', $4, NULL, 1, now(), now())",
        row["id"],
        f"dxe.recv:{EVENT_TOPIC}",
        {"event": "paid"},
        {"n": 1},
    )
    await post(db_pool, row["id"], "packed")

    conn = await db_mod.connect(**db_params)
    try:
        machine = await connection_bound_job(conn, row, cls=OrderMachine)
        result = await machine.task()
    finally:
        await conn.close()

    assert result["final_state"] == "shipped"
    assert await machine_state(db_pool, row["id"]) == "shipped"


async def test_a_turn_boundary_publishes_and_wipes_in_one_commit(
    db_pool, db_params, unique_queue
):
    """The pair a reader can observe is (new state, empty log), never one
    half of it. Read under ONE repeatable-read snapshot, so "both" is a fact
    about a single instant rather than about two round trips.
    """
    from pyjobby import db as db_mod

    from .utils.dxe import connection_bound_job

    row = await _machine_row(db_pool, unique_queue, "tests.machine_jobs.OrderMachine")
    await _publish(db_pool, row["id"], "awaiting_payment")
    await post(db_pool, row["id"], "paid", amount=1)

    conn = await db_mod.connect(**db_params)
    try:
        machine = await connection_bound_job(conn, row, cls=OrderMachine)
        payload = await machine.recv(topic=EVENT_TOPIC, timeout=5)
        await machine.step(
            "awaiting_payment--paid->packing", machine.charge, "paid", payload
        )
        assert await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id=$1", row["id"]
        ), "the turn recorded nothing, so the wipe below proves nothing"
        await machine._turn_boundary("packing")
    finally:
        await conn.close()

    async with (
        db_pool.acquire() as reader,
        reader.transaction(isolation="repeatable_read"),
    ):
        state = await reader.fetchval(
            "SELECT value FROM jorb_event WHERE job_id=$1 AND key=$2",
            row["id"],
            STATE_KEY,
        )
        steps = await reader.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id=$1", row["id"]
        )
    assert state == {"state": "packing"}
    assert steps == 0, (
        "the new state is published while the turn's log survives -- the pair "
        "that poisons every future replay of this machine"
    )


async def test_a_superseded_turn_boundary_writes_neither_half(
    db_pool, db_params, unique_queue
):
    """The fence covers the whole transaction, not just the wipe.

    A boundary that published the state and only then discovered it had been
    superseded would either wipe the live attempt's log or publish a zombie's
    position over it. Both halves are inside the fenced transaction, so a
    stale epoch writes nothing at all.
    """
    from pyjobby import db as db_mod
    from pyjobby import dxe

    from .utils.dxe import connection_bound_job

    row = await _machine_row(db_pool, unique_queue, "tests.machine_jobs.OrderMachine")
    await _publish(db_pool, row["id"], "awaiting_payment")
    await db_pool.execute(
        "INSERT INTO jorb_step (job_id, step_seq, name, output, error, run_epoch,"
        " started, finished) VALUES ($1, 1, $2, $3, NULL, 1, now(), now())",
        row["id"],
        f"dxe.recv:{EVENT_TOPIC}",
        {"event": "paid"},
    )

    conn = await db_mod.connect(**db_params)
    try:
        machine = await connection_bound_job(conn, row, cls=OrderMachine, epoch=0)
        with pytest.raises(dxe.StaleExecutionError):
            await machine._turn_boundary("packing")
    finally:
        await conn.close()

    assert await machine_state(db_pool, row["id"]) == "awaiting_payment"
    assert (
        await db_pool.fetchval(
            "SELECT count(*) FROM jorb_step WHERE job_id=$1", row["id"]
        )
        == 1
    )
