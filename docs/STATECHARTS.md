# Durable state machines

A long-running workflow that waits on the outside world between steps — an
order, an onboarding, an approval, a device provisioning — is a state machine.
`StateMachineJob` lets you declare one and run it as a pyjobby job, so it
survives crashes, cannot be driven forward by a worker that has been
superseded, runs each action at most once, and can wait months between events
without holding a process.

Nothing in the schema knows about it. The current state is a `jorb_event` row,
events are `jorb_mailbox` rows, actions are DXE steps, and waiting is a durable
`sleep()`. A job that has no machine pays nothing for this feature.

---

## Declaring a machine

```python
from pyjobby.registry import job
from pyjobby.statemachine import StateMachineJob


@job
class Order(StateMachineJob):
    initial = "awaiting_payment"
    final = frozenset({"shipped", "refunded"})
    transitions = {
        "awaiting_payment": {"paid": ("packing", "charge"), "cancel": "refunded"},
        "packing": {"packed": ("shipped", "buy_label")},
    }

    async def charge(self, event, payload): ...
    async def buy_label(self, event, payload): ...
```

An edge takes any of three shapes:

| Written as                        | Means                                             |
| --------------------------------- | ------------------------------------------------- |
| `("packing", "charge")`           | go to `packing`, running `self.charge` on the way |
| `"refunded"`                      | go to `refunded`, no action                       |
| `Transition("packing", "charge")` | the same as the tuple, named                      |

**The declaration is checked when the class is created, not when a transition
fires.** A target state that appears nowhere else, an `initial` that is not a
state, a `final` state with outgoing edges, an action naming a method that does
not exist, or a state nothing reaches raises `MachineDefinitionError` at import,
with a message naming the problem. All of those are decidable from the class
body, so paying for them once at startup beats finding one months later on the
edge nobody exercised.

### Actions

An action runs inside `step()`, so it is checkpointed: once it has completed, no
later attempt of the job runs it again. If the action's work is a write to this
same database, use `transaction()` inside it to get exactly-once instead of
at-least-once — see [DXE.md](DXE.md#transactional-steps).

Actions receive `(event, payload)`: the event name, and the message the client
sent.

### Optional hooks

```python
async def on_unhandled(self, state, event, payload): ...
async def on_transition(self, source, event, target): ...
```

`on_unhandled` is called when an event arrives that the current state has no
edge for; the default logs a warning. `on_transition` is called after each
transition commits and is **not** a checkpointed step, so it re-runs on replay —
keep it to logging and metrics, and put anything with an effect in an action.

---

## Driving a machine

The client API is the same one you use for every other job.

```python
from myapp.orders import Order

order = await client.start_machine(Order, kwargs={"customer": 42})

await order.send("paid", amount=100)
await order.wait_for_state("shipped", timeout=600)
result = await order.result()  # {"final_state": "shipped", "turns": 2}
```

| Call                                             | Does                                            |
| ------------------------------------------------ | ----------------------------------------------- |
| `client.start_machine(Order, **enqueue_options)` | enqueue a machine, return a handle              |
| `client.machine(job_id, Order)`                  | a handle for one already running — no I/O       |
| `await handle.send(event, **payload)`            | deliver a transition event                      |
| `await handle.may(event)`                        | would this event be accepted right now?         |
| `await handle.state()`                           | current state                                   |
| `await handle.wait_for_state(*states, timeout=)` | block until it is in one of them                |
| `await handle.result(timeout=)`                  | wait for a final state and return the result    |
| `await handle.history()`                         | the current stretch of checkpointed transitions |
| `await handle.cancel()`                          | stop it wherever it is                          |
| `handle.diagram()`                               | Mermaid, rendered locally from the declaration  |

`SyncJobClient.start_machine()` and `.machine()` return a `SyncMachine` with the
same methods, blocking, for scripts and cron jobs.

Pass the **class** rather than a dotted string when you can import it: the
handle then holds the declaration and can answer `may()`, `diagram()` and the
`send()` check without a round trip.

### Why `send()` refuses unhandled events

```python
await order.send("packed")
# UnhandledEventError: machine 41 is in 'awaiting_payment', which has no
# transition for 'packed'; it accepts ['cancel', 'paid']
```

This is not the same convenience an in-process FSM library offers. There, an
unhandled event raises on the machine's own thread and your event is still in
your hand. Here the event travels through a durable mailbox, and the machine's
`recv()` **consumes the row and checkpoints having consumed it** whether or not
any transition fires. An event sent to a state that does not handle it is not
deferred, not re-queued and not returned — it is gone, and the only symptom is
that nothing happened.

The check costs one read of the machine's state. Ask directly with
`await order.may("paid")`, or skip it with `send(..., check=False)` when you are
deliberately racing the machine or do not have the class to hand.

### Waiting

`wait_for_state()` waits for a **state**, not a transition, so it returns
immediately if the machine is already there rather than waiting forever for an
edge that has already been crossed. It raises rather than hanging if the machine
reaches a terminal state without ever satisfying the wait.

It sleeps on the notification rather than polling — provided the client was
built with connection parameters. A pool-only client (`JobClient(pool)` with no
`db_params`) has no LISTEN connection and falls back to polling by design, so
pass `db_params` for anything that waits.

---

## Running machines

**Give machines their own queue.** A machine parks on `recv()` waiting for
events, so a worker running a machine is a worker not running anything else.
`start_machine()` defaults to a `machines` queue for this reason — the safe
arrangement is the one you get without reading anything. Pass `queue=` to choose
another, and run workers for it:

```
pj --queue machines --workers 4
```

Two class attributes set the trade between latency and worker occupancy:

| Attribute      | Default | Meaning                                                               |
| -------------- | ------- | --------------------------------------------------------------------- |
| `wait_seconds` | 30      | how long one `recv()` holds a worker before the machine gives it back |
| `idle_seconds` | 300     | how long it then waits in the database, holding nothing               |

A machine occupies a worker about `wait / (wait + idle)` of the time — 9% at the
defaults, so roughly one worker per eleven idle machines. Raising `idle_seconds`
lowers that and adds up to that much delay to an event arriving just after a
park ends. Lowering `wait_seconds` frees workers sooner at the cost of more wake
cycles.

`timeout = 0` on the class: a machine is not a job with a deadline, and this is
the default. Leave it unless you genuinely want the machine killed after a fixed
time.

### What bounds the cost of living a long time

Resuming a job replays its checkpoint log, and a machine records a `recv` and a
`sleep` on every idle wake — so a naive machine's replay cost would grow with
how long it has existed rather than with how much it has done.
`StateMachineJob` calls [`compact()`](DXE.md#bounding-replay-compact) at each
turn boundary, which discards the log and restarts the step sequence, so the log
never exceeds one turn and a machine may live indefinitely.

The consequence for you is that `history()` shows the **current** stretch of
work, not everything the machine has ever done. If you need a permanent audit
trail, publish one: as machine events, or into your own table from inside a
`transaction()`.

---

## What a machine gets that an in-process FSM cannot

- It survives `SIGKILL` at any instant and resumes where it was, with completed
  actions skipped rather than re-run.
- A worker presumed dead but still alive **cannot drive it forward** once
  another worker takes over — `run_epoch` fences every durable write, including
  the state publish and outbound messages.
- An action that writes to this database can be exactly-once.
- Timers are database rows, so a machine can wait six months without a process,
  a connection or a thread.
- `pj-admin jobs steps <id>` shows which transition ran, when, how long it took
  and what it failed with.
- A client cannot silently lose an event.

## What a machine is not

**Parallel regions are not supported and will not be.** DXE replays a single
ordered sequence of steps, and two concurrently-active regions of one machine
would share one counter — the first resume would raise `NondeterminismError` by
construction. This is a contradiction rather than a missing feature, and the
answer already exists: regions that are genuinely independent are separate jobs,
which `run_group` and `waitfor_group` already coordinate durably. See
[CLIENT_LIBRARY.md](CLIENT_LIBRARY.md) for fan-out/fan-in.

**Internal events should be plain Python, not messages to yourself.** A `send()`
to your own mailbox is a database round trip followed by a poll where a function
call would do. Do the work inside one transition and publish the resulting state
once.

**Nesting and history states are yours to write, and cost nothing extra.** The
state value is JSONB, so `{"region": "shipping", "sub": "awaiting_pickup"}` is a
nested state and a `{"history": {...}}` key is a history state — and because
both are database rows, they survive a restart. `StateMachineJob` does not
interpret them; your transition function does.

---

## See also

- [DXE.md](DXE.md) — the durable primitives underneath: `step()`,
  `transaction()`, `sleep()`, events, mailboxes, fencing, and `compact()`.
- [CLIENT_LIBRARY.md](CLIENT_LIBRARY.md#state-machines) — the client API in
  context with the rest of the enqueue surface.
- [OPERATIONS.md](OPERATIONS.md) — running and watching the fleet the machines
  run on.
