"""Reusable durable state machines for the StateMachineJob tests.

Real classes resolved by a live worker through their dotted path, exactly
like ``tests.dxe_jobs``. Keep them small and deterministic — the point of
each one is a single property of the machine runtime, not a plausible
business process.
"""

from __future__ import annotations

from typing import Any

from pyjobby.statemachine import StateMachineJob


class OrderMachine(StateMachineJob):
    """The canonical shape: two actions, an unguarded edge, two final states.

    Each action publishes its own ``jorb_event`` so a test can prove it ran
    exactly once without reading the checkpoint log — an action that re-ran
    would overwrite its own count and the count would still be 1, so the
    events record a running total rather than a flag.
    """

    initial = "awaiting_payment"
    final = frozenset({"shipped", "refunded"})
    transitions = {
        "awaiting_payment": {
            "paid": ("packing", "charge"),
            "cancel": "refunded",
        },
        "packing": {"packed": ("shipped", "buy_label")},
    }

    # Short so the tests do not spend their lives waiting; a real machine
    # parks far longer.
    wait_seconds = 2.0
    idle_seconds = 1.0

    async def charge(self, event: str, payload: Any) -> dict[str, Any]:
        return await self._count("charge", payload)

    async def buy_label(self, event: str, payload: Any) -> dict[str, Any]:
        return await self._count("buy_label", payload)

    async def _count(self, name: str, payload: Any) -> dict[str, Any]:
        prior = await self.get_event(f"ran.{name}") or {"n": 0}
        record = {"n": int(prior["n"]) + 1, "payload": payload}
        await self.set_event(f"ran.{name}", record)
        return record


class CrashOnceMachine(StateMachineJob):
    """Crashes inside its action on the first attempt only.

    Proves the durability claim end to end: the action's effect is recorded
    once, the retry resumes from the checkpoint rather than from the start,
    and the machine still arrives at its final state.
    """

    initial = "start"
    final = frozenset({"done"})
    transitions = {"start": {"go": ("done", "explode_once")}}
    wait_seconds = 2.0
    idle_seconds = 1.0

    async def explode_once(self, event: str, payload: Any) -> dict[str, Any]:
        prior = await self.get_event("attempts") or {"n": 0}
        attempt = int(prior["n"]) + 1
        await self.set_event("attempts", {"n": attempt})
        if attempt == 1:
            raise RuntimeError("first attempt always fails")
        return {"attempt": attempt}


class QuietMachine(StateMachineJob):
    """Parks for a long time without waking, so a client-side measurement of
    round trips measures the CLIENT.

    Every wake republishes `machine.state`, and every publish is a NOTIFY that
    legitimately wakes any waiter. A machine that cycles once a second
    therefore inflates a waiter's round-trip count with traffic that is not
    the waiter's fault — so a test asking "does this waiter poll?" has to hold
    the machine still to get an answer about the waiter.
    """

    initial = "parked"
    final = frozenset({"released"})
    transitions = {"parked": {"release": "released"}}
    wait_seconds = 30.0
    idle_seconds = 30.0


class IdleMachine(StateMachineJob):
    """Never receives anything: exists to prove idle replay stays bounded.

    Waits briefly, sleeps briefly, repeats. Without compaction this would
    accumulate two checkpoints per turn forever; the test asserts the log
    stays at one turn's worth however many turns elapse.
    """

    initial = "waiting"
    final = frozenset({"never"})
    transitions = {"waiting": {"impossible": "never"}}
    wait_seconds = 0.25
    idle_seconds = 0.25


class ObservedMachine(StateMachineJob):
    """Overrides both hooks, recording each call as a durable event.

    The hooks are the machine's only extension points that are NOT steps, and
    the difference matters: `on_transition` re-runs on replay by design, so a
    subclass that puts an effect in one gets that effect more than once. These
    record through `set_event`, which is an idempotent upsert, so the test can
    observe that they ran without the recording itself being a lie about how
    many times.
    """

    initial = "start"
    final = frozenset({"done"})
    transitions = {"start": {"go": "done"}}
    wait_seconds = 2.0
    idle_seconds = 1.0

    async def on_unhandled(self, state: str, event: str, payload: Any) -> None:
        await self.set_event("saw.unhandled", {"state": state, "event": event})

    async def on_transition(self, source: str, event: str, target: str) -> None:
        await self.set_event(
            "saw.transition", {"source": source, "event": event, "target": target}
        )


class PeerReaderMachine(StateMachineJob):
    """Reads another job's published event, by id, and republishes what it saw.

    `get_event(key, job_id=...)` is the cross-job form, which is how one
    machine observes another's state without a mailbox round trip.
    """

    initial = "reading"
    final = frozenset({"read"})
    transitions = {"reading": {"look": ("read", "peek")}}
    wait_seconds = 2.0
    idle_seconds = 1.0

    async def peek(self, event: str, payload: Any) -> dict[str, Any]:
        seen = await self.get_event("machine.state", job_id=int(payload["peer"]))
        await self.set_event("peer.state", seen)
        return {"peer_state": seen}
