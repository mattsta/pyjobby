"""Durable finite state machines, as jobs.

A machine here is an ordinary job whose ``task()`` is an event loop. Nothing
in the schema knows about it: the current state is a ``jorb_event`` row, the
events are ``jorb_mailbox`` rows, the actions are DXE steps, and the waiting
is a durable ``sleep()``. Adding a machine costs a job that has none exactly
nothing, which is why this is a module rather than a subsystem. See
``docs/STATECHARTS.md``.

What you get that an in-process FSM library cannot give you:

* the machine survives ``SIGKILL`` at any instant and resumes where it was,
  with completed actions skipped rather than re-run;
* a worker presumed dead but still alive cannot drive the machine forward
  after another worker takes over — ``run_epoch`` fences every write it makes;
* an action that writes to this database is exactly-once, via
  ``transaction()``;
* the transition log is written by a database trigger, so no code path can
  forget to append to it;
* a machine can wait six months for an event without holding a process, a
  connection, or a thread.

What you do not get, and should not ask for: parallel regions. DXE replays a
single ordered step sequence, and two concurrently-active regions do not have
one — the first resume of a genuinely parallel machine would fail with
``NondeterminismError`` by construction. Regions that are genuinely
independent are separate jobs, and ``run_group``/``waitfor_group`` already
does that durably.

Nesting, history states and entry/exit actions are *not* provided and are not
missing: they are properties of the transition function and of the shape of
the state value, both of which are yours. A state of
``{"region": "shipping", "sub": "awaiting_pickup"}`` is a nested state, and
because it is a database row it survives a restart.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from loguru import logger

from . import fsm
from .fsm import EVENT_TOPIC, STATE_KEY, EdgeTable, Transition, TransitionSpec
from .pj import Job

# The declaration format itself lives in `pyjobby.fsm`, which imports nothing,
# because `pyjobby.client` needs it too and `pyjobby.pj` imports the client --
# so a machine's vocabulary cannot live in either end without being duplicated
# into the other. See that module's docstring.
__all__ = [
    "EVENT_TOPIC",
    "STATE_KEY",
    "StateMachineJob",
    "Transition",
    "TransitionSpec",
]


class StateMachineJob(Job):
    """Base class for a durable state machine. Subclasses declare the table.

    ::

        @job
        class Order(StateMachineJob):
            initial = "awaiting_payment"
            final = frozenset({"shipped", "refunded"})
            transitions = {
                "awaiting_payment": {"paid": ("packing", "charge"),
                                     "cancel": "refunded"},
                "packing": {"packed": ("shipped", "buy_label")},
            }

            async def charge(self, event, payload): ...
            async def buy_label(self, event, payload): ...

    and drive it with the ordinary client API — there is no second surface::

        job_id = await client.enqueue("myapp.Order", queue="machines")
        await client.send_message(job_id, {"event": "paid"}, topic="fsm")
        state = await client.get_event(job_id, "machine.state", timeout=10)

    **Give machines their own queue.** They park on ``recv()`` waiting for
    events, and a worker parked on a machine is a worker not running ordinary
    jobs. This is the warning in ``docs/EXAMPLES.md`` §6, with more force.
    """

    #: {source: {event: Transition | (target, action) | target}}
    transitions: ClassVar[TransitionSpec] = {}

    #: Resolved, validated form of `transitions`, built at class creation.
    edges: ClassVar[EdgeTable] = {}

    #: Where a fresh machine starts, and where it is allowed to stop.
    initial: ClassVar[str] = ""
    final: ClassVar[frozenset[str]] = frozenset()

    #: A machine is not a job with a deadline: it is *supposed* to sit for a
    #: long time doing nothing. 0 disables the job timeout.
    timeout: ClassVar[int | None] = 0

    #: How long one recv() parks a worker before the machine gives the worker
    #: back and waits in the database instead. The trade is latency against
    #: worker occupancy: a shorter park frees the worker sooner and adds up
    #: to `idle_seconds` of delay to an event that arrives just after it.
    wait_seconds: ClassVar[float] = 30.0
    idle_seconds: ClassVar[float] = 300.0

    #: Mailbox topic carrying transition events.
    topic: ClassVar[str] = EVENT_TOPIC

    #: jorb_event key carrying the current state.
    state_key: ClassVar[str] = STATE_KEY

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Resolve and check the declaration once, at import.

        Deliberate metaprogramming: action names are resolved to bound methods
        through the class, which is the only way a declarative table can name
        behaviour. Doing it here rather than at transition time is the point —
        an unknown target state or a misspelled action is a startup failure
        with a precise message instead of a crash on a rare edge in
        production.
        """
        super().__init_subclass__(**kwargs)
        cls.edges = fsm.normalize(cls.transitions)
        fsm.validate(
            cls.__name__,
            cls.edges,
            cls.initial,
            cls.final,
            # Deliberate metaprogramming: an action is declared by NAME, so
            # resolving it against the class is the only way a declarative
            # table can name behaviour. Doing it at class creation is the
            # point -- a misspelled action is a startup failure rather than a
            # crash on a rare edge in production.
            has_action=lambda name: callable(getattr(cls, name, None)),
        )
        if cls.edges and not cls.final:
            logger.warning(
                f"{cls.__name__} declares no final states: it will run until "
                f"cancelled. That is legal — see StateMachineJob.compact() for "
                f"why its replay cost stays bounded — but it is usually a typo."
            )

    # ------------------------------------------------------------------
    # Declaration queries — pure, and answerable without a database
    # ------------------------------------------------------------------

    @classmethod
    def states(cls) -> frozenset[str]:
        """Every state named anywhere in the declaration."""
        return fsm.states(cls.edges)

    @classmethod
    def may(cls, state: str, event: str) -> bool:
        """Would `event` be accepted in `state`? See `fsm.may`."""
        return fsm.may(cls.edges, state, event)

    @classmethod
    def to_mermaid(cls) -> str:
        """The declaration as a Mermaid ``stateDiagram-v2``."""
        return fsm.to_mermaid(cls.edges, cls.initial, cls.final)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def current_state(self) -> str:
        """This machine's state as committed, or `initial` if it has none."""
        published = await self.get_event(self.state_key)
        if isinstance(published, dict) and "state" in published:
            return str(published["state"])
        return self.initial

    async def on_unhandled(self, state: str, event: str, payload: Any) -> None:
        """An event arrived that this state has no edge for.

        Default is to drop it with a warning, which is what the mailbox has
        already done — ``recv()`` consumed the row and checkpointed the
        consumption, so there is nothing to put back. Override to record it,
        to raise, or to forward it somewhere that cares. Re-sending it to
        yourself is available but rarely right: it returns at the back of the
        queue, with the ordering lost.
        """
        logger.warning(
            f"[job {self.job['id']}] {type(self).__name__} in {state!r} has no "
            f"transition for {event!r}; the event is consumed and dropped"
        )

    async def on_transition(self, source: str, event: str, target: str) -> None:
        """Called after each transition commits. Default does nothing.

        Not a checkpointed step, so it re-runs on replay — keep it to logging
        and metrics. Anything with an effect belongs in an action, which is a
        step and therefore runs once.
        """

    async def task(self, **kwargs: Any) -> dict[str, Any]:
        """Run the machine until it reaches a final state.

        The loop is ordinary Python; every durable thing in it is a DXE
        primitive doing what it already did for every other job.
        """
        state = await self.current_state()
        await self._publish(state)
        turns = 0

        while state not in self.final:
            # Bounds replay. Once this attempt owes nothing to a previous
            # one, the log so far is dead weight -- the machine re-derives
            # its position from the state event above, not by replaying --
            # so it goes, and the step sequence starts at 1 again. Without
            # this an idle machine accumulates two checkpoints per wake
            # forever, at ~0.9us and 260 bytes each on EVERY subsequent
            # wake (pj-bench replay). With it, the log never exceeds one
            # turn.
            await self.compact()

            payload = await self.recv(topic=self.topic, timeout=self.wait_seconds)
            if payload is None:
                # No worker is held across this. The job checkpoints a wake
                # time, requeues itself, and unwinds; the next claim resumes
                # here with the sleep already satisfied.
                await self.sleep(self.idle_seconds)
                continue

            turns += 1
            event = (
                str(payload.get(fsm.EVENT_FIELD, ""))
                if isinstance(payload, dict)
                else ""
            )
            edge = self.edges.get(state, {}).get(event)
            if edge is None:
                await self.on_unhandled(state, event, payload)
                continue

            if edge.action is not None:
                # A step: on any later attempt this returns the recorded
                # result instead of running again. The source and event are
                # in the name so `pj-admin jobs steps <id>` reads as the
                # machine's history rather than as a list of opaque calls.
                await self.step(
                    f"{state}--{event}->{edge.target}",
                    self._action(edge.action),
                    event,
                    payload,
                )

            source, state = state, edge.target
            await self._publish(state)
            await self.on_transition(source, event, state)

        return {"final_state": state, "turns": turns}

    def _action(self, name: str) -> Callable[..., Awaitable[Any]]:
        """The bound method for a declared action name.

        Explicit metaprogramming, and validated at class creation, so by the
        time this runs the attribute is known to exist and to be callable.
        """
        return getattr(self, name)  # type: ignore[no-any-return]

    async def _publish(self, state: str) -> None:
        """Commit the current state where clients and other jobs can read it.

        Idempotent (an upsert on ``(job_id, key)``), which is what lets it sit
        outside the checkpoint log: re-running it on replay writes the value
        that is already there.
        """
        await self.set_event(self.state_key, {"state": state})
