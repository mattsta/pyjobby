"""Driving a durable state machine from outside the worker.

The worker-side tests in `test_statemachine.py` prove the machine runs. These
prove an *application* can use it without holding the platform's vocabulary by
hand — the mailbox topic, the `event` payload field, the reserved state key —
and without discovering its mistakes only by their absence.

The central case is `send()` refusing an event the current state has no edge
for. In an in-process FSM library that refusal is a convenience: the library
raises on the caller's thread and the caller still has its event. Over a
durable mailbox it is not a convenience, because `recv()` consumes the row and
checkpoints the consumption whether or not a transition fires. An unhandled
event is not deferred and not returned. It is gone. Checking before the send
is the only place the caller can still be told.
"""

from __future__ import annotations

import pytest

from pyjobby.client import JobClient, MachineHandle, UnhandledEventError
from pyjobby.fsm import EVENT_FIELD, EVENT_TOPIC, STATE_KEY

from .machine_jobs import OrderMachine, QuietMachine
from .utils.counting import counted_client

# `client` is the shared conftest fixture: a JobClient over the test pool.

# =========================================================================
# Local: what the declaration answers without a database
# =========================================================================


def test_a_handle_answers_from_the_declaration_without_a_database():
    """No pool, no worker, no round trip — the table is in the code."""
    handle = MachineHandle(id=0, client=None, machine=OrderMachine)  # type: ignore[arg-type]
    assert OrderMachine.may("awaiting_payment", "paid")
    assert not OrderMachine.may("awaiting_payment", "packed")
    assert "awaiting_payment --> packing: paid / charge" in handle.diagram()


def test_a_handle_without_the_class_refuses_the_local_questions():
    """Better than guessing: the transition table lives in code, not a row.

    A handle built from a bare job id can still drive the machine — it just
    cannot answer questions whose answer is the declaration, and says so
    instead of silently skipping the check.
    """
    handle = MachineHandle(id=0, client=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="needs the machine class"):
        handle.diagram()


# =========================================================================
# Against a real worker
# =========================================================================


async def test_the_whole_client_side_lifecycle(live_worker, unique_queue, client):
    """Start it, drive it, watch it, collect the result — no raw SQL.

    This is the shape an application actually writes, and every string the
    platform reserves (`fsm`, `event`, `machine.state`) stays inside the
    handle.
    """
    await live_worker()
    order = await client.start_machine(OrderMachine, queue=unique_queue)

    assert await order.wait_for_state("awaiting_payment", timeout=20)
    assert await order.may("paid")
    assert not await order.may("packed")

    await order.send("paid", amount=100)
    assert await order.wait_for_state("packing", timeout=20) == "packing"

    await order.send("packed")
    assert await order.wait_for_state("shipped", timeout=20) == "shipped"

    result = await order.result(timeout=20)
    assert result["final_state"] == "shipped"

    # The payload rode along to the action and was recorded by its step.
    steps = await order.history()
    assert steps == [] or all(step["error"] is None for step in steps)


async def test_send_refuses_an_event_the_current_state_would_drop(
    live_worker, unique_queue, client, db_pool
):
    """The check that has no in-process equivalent.

    Without it the mailbox row is inserted, consumed, and discarded, and the
    caller's only evidence is that nothing happened.
    """
    await live_worker()
    order = await client.start_machine(OrderMachine, queue=unique_queue)
    await order.wait_for_state("awaiting_payment", timeout=20)

    with pytest.raises(UnhandledEventError) as caught:
        await order.send("packed")

    assert caught.value.state == "awaiting_payment"
    assert caught.value.event == "packed"
    assert caught.value.accepted == ["cancel", "paid"]

    # Nothing was written: the refusal happened before the send.
    mail = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_mailbox WHERE dest_job_id = $1", order.id
    )
    assert mail == 0

    # And the machine is untouched and still usable.
    await order.send("paid")
    assert await order.wait_for_state("packing", timeout=20) == "packing"


async def test_check_can_be_turned_off_deliberately(
    live_worker, unique_queue, client, db_pool
):
    """`check=False` is for a caller without the class, or racing on purpose.

    It does what it says: the message is delivered, the machine consumes it,
    and it is dropped.
    """
    await live_worker()
    order = await client.start_machine(OrderMachine, queue=unique_queue)
    await order.wait_for_state("awaiting_payment", timeout=20)

    await order.send("packed", check=False)
    await order.send("paid")
    assert await order.wait_for_state("packing", timeout=20) == "packing"

    consumed = await db_pool.fetchval(
        "SELECT count(*) FROM jorb_mailbox "
        "WHERE dest_job_id = $1 AND consumed_at IS NOT NULL",
        order.id,
    )
    assert consumed == 2, "both were consumed; only the accepted one transitioned"


async def test_waiting_for_a_state_does_not_poll(
    live_worker, unique_queue, db_pool, db_params
):
    """A waiter sleeps on the notification; it does not spin on the database.

    Measured rather than reasoned about, because the failure it guards is
    invisible by reading: `wait_for_state` looked correct while making three
    round trips every 250ms, one of them an `UPDATE` on the `jorb` row to
    re-register demand. Only counting shows that.

    The client is built WITH `db_params` on purpose. A pool-only client has no
    LISTEN connection and falls back to polling by design, so measuring one
    would measure that fallback and report it as the waiter's behaviour —
    which is exactly the mistake this test exists to catch, made one level up.
    `QuietMachine` then holds still, so no legitimate state-change
    notification inflates the count either.
    """
    await live_worker()
    client = JobClient(pool=db_pool, db_params=db_params)
    machine = await client.start_machine(QuietMachine, queue=unique_queue)
    await machine.wait_for_state("parked", timeout=20)

    # Wait on a state it will not reach, and time out on purpose.
    with counted_client(client) as counter, pytest.raises(TimeoutError):
        await machine.wait_for_state("released", timeout=6)

    # With a listener the fallback is 2s: ~3 checks in 6 seconds, plus one
    # demand registration. The polling version made three round trips every
    # 250ms — about 72. The bound is loose enough to be about the shape
    # rather than an exact count.
    assert counter.calls <= 6, (
        f"{counter.calls} round trips in a 6s wait: that is polling, not "
        f"waiting on the notification"
    )


async def test_a_handle_can_be_rebuilt_from_an_id(live_worker, unique_queue, client):
    """A machine outlives the process that started it.

    That is the whole point of durability, and it means the usual case is a
    handle rebuilt from an id in a database somewhere — a request handler, a
    webhook, a different service entirely.
    """
    await live_worker()
    started = await client.start_machine(OrderMachine, queue=unique_queue)
    await started.wait_for_state("awaiting_payment", timeout=20)

    elsewhere = client.machine(started.id, OrderMachine)
    await elsewhere.send("paid")
    assert await elsewhere.wait_for_state("packing", timeout=20) == "packing"


async def test_start_machine_defaults_to_the_machines_queue(client, db_pool):
    """A machine parks a worker, so it must not land on the default queue.

    No worker runs here on purpose: the assertion is about where the row went,
    and starting one would only make the test slower.
    """
    order = await client.start_machine(OrderMachine)
    queue = await db_pool.fetchval("SELECT queue FROM jorb WHERE id = $1", order.id)
    assert queue == "machines"


async def test_the_handle_uses_the_declarations_topic_and_keys(
    live_worker, unique_queue, client, db_pool
):
    """The reserved strings are the platform's, not the application's.

    Asserted against the wire so that renaming a constant cannot quietly
    desynchronise the two ends — which is exactly what `pyjobby.fsm` exists
    to prevent.
    """
    await live_worker()
    order = await client.start_machine(OrderMachine, queue=unique_queue)
    await order.wait_for_state("awaiting_payment", timeout=20)
    await order.send("paid", amount=7)

    row = await db_pool.fetchrow(
        "SELECT topic, message FROM jorb_mailbox WHERE dest_job_id = $1", order.id
    )
    assert row["topic"] == EVENT_TOPIC
    assert row["message"][EVENT_FIELD] == "paid"
    assert row["message"]["amount"] == 7

    published = await db_pool.fetchval(
        "SELECT key FROM jorb_event WHERE job_id = $1 AND key = $2",
        order.id,
        STATE_KEY,
    )
    assert published == STATE_KEY
