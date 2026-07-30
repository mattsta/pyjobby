# Operating pyjobby

The runbook: what runs, how to check it, and what to do when something is
wrong. The executable version of the health section is `pj-admin doctor`.

## The processes

| Process   | Command                                                       | Count                                     | Purpose                                                            |
| --------- | ------------------------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------ |
| Workers   | `pj --config ./pyjobby.toml --queue Q --workers N`            | N processes **per named queue**, per host | claim + execute jobs                                               |
| Monitor   | `pj-monitor --config ./pyjobby.toml`                          | 1 (more are safe)                         | timeout enforcement, dead-worker reclaim, stranded-waiter recovery |
| Scheduler | `pj-scheduler --config ./pyjobby.toml`                        | 1 (more are safe)                         | fires cron schedules                                               |
| Web admin | `pj-web --config ./pyjobby.toml --host 127.0.0.1 --port 8081` | optional                                  | HTML admin + `/metrics`                                            |
| Websocket | `pj-ws --config ./pyjobby.toml --port 8082`                   | optional                                  | realtime dashboard feed                                            |

`--workers N` is **per queue**, and a worker is never started on a queue you
did not name:

```bash
pj --queue emails --workers 4                  # 4 workers, all on `emails`
pj --queue emails --queue billing --workers 4  # 4 on each: 8 processes
pj --queue emails --queue emails --workers 4   # 4 — a repeat asks for nothing extra
```

Naming another queue therefore never changes the capacity of the queues you
already named. Scale a single queue by raising `--workers`, or by starting
another `pj` — nothing coordinates between launchers.

Start order does not matter — every process connects independently and
tolerates the database being briefly unavailable. One command installs or
upgrades the schema idempotently:

```bash
pj-admin db migrate        # base schema + any pending migrations
pj-admin db status
```

Neither web surface has authentication: keep them on localhost or behind an
authenticating proxy.

## Shutdown (SIGTERM / SIGINT)

Every process handles `SIGTERM` and `SIGINT` (Ctrl-C) as a graceful stop, so
`systemctl stop`, `docker stop` and Kubernetes rollout all end cleanly rather
than killing work mid-flight. No process needs `SIGKILL` under normal
operation.

| Process                    | On SIGTERM                                                                                                                                                                                             |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Worker (`pj`)              | Stops claiming new jobs and finishes the one it is running, then exits. In-flight work is not abandoned; give the container a stop grace period at least as long as your longest job (or its timeout). |
| Monitor (`pj-monitor`)     | Ends at the next clean point: it stops between sweeps, and a sweep that is mid-drain yields after the current batch rather than mid-statement.                                                         |
| Scheduler (`pj-scheduler`) | Cuts its poll sleep short and exits before the next firing; a schedule already firing completes.                                                                                                       |
| Websocket (`pj-ws`)        | Stops accepting connections, drains the aiohttp runner, then closes its pool.                                                                                                                          |
| Web admin (`pj-web`)       | Stops serving and exits.                                                                                                                                                                               |

A worker that is killed with `SIGKILL` (or whose host dies) does not corrupt
anything: its in-flight jobs are reclaimed by the monitor's dead-worker sweep
once the worker's registry row goes stale, and run-epoch fencing prevents the
killed attempt from writing anything after it is reclaimed.

## Health: `pj-admin doctor`

```bash
pj-admin --dsn "$PYJOBBY_DSN" doctor [--max-depth 10000] [--max-age-minutes 60]
```

Checks (FAIL exits nonzero; WARN does not): database reachable, the schema's
**shape** against the manifest of objects this release addresses, all seven
schema triggers present by name, NOTIFY queue saturation, live workers seen
in the last 60s, workers that are alive but claiming nothing, per-queue
depth and oldest-runnable age, jobs no live worker on their queue could ever
claim, waiters blocked on a crashed or cancelled
upstream, unread durable mail older than a day, DLQ size, overdue
schedules. Run it from cron/CI
as a platform health probe; scrape `GET /metrics` on the web admin for
Prometheus.

```console
$ pj-admin --dsn "$PYJOBBY_DSN" doctor
PASS database: connected
PASS schema: installed, migrations current (baseline)
PASS triggers: all schema triggers present (7)
PASS notify-queue: 0.0% full
WARN workers: no live workers seen in last 60s
PASS job-threads: 0 live worker(s) claiming
PASS queues: no queued jobs
PASS unclaimable: no queued job is invisible to its queue's live workers
PASS blocked-waiters: no waiting jobs blocked on failed upstreams
PASS mailbox: no unread mail older than a day
PASS dlq: empty
PASS schedules: no overdue schedules
```

A schema check that FAILs names what is absent and ends the report:

```console
FAIL schema: installed, but 3 object(s) this release needs are missing: index jorb_dag_retention_idx, index jorb_schedule_log_retention_idx, index jorb_worker_retention_idx (run: pj-admin db migrate)
```

`pj-admin db migrate` fixes both that and a database with no schema at all;
`pj-admin db status` lists every missing object. Full reference:
[ADMIN_TOOLS.md § doctor](ADMIN_TOOLS.md#doctor--the-health-entry-point).

## How execution works (what the states mean)

```
queued -> claimed -> running -> finished          (success)
                          \-> queued (retry, same row, backoff)
                          \-> crashed              (terminal: THE DLQ)
                          \-> cancelled            (terminal)
waiting -> queued                                  (dependency satisfied)
```

- A job keeps **one row for life**. Retries requeue the same row;
  `run_epoch` increments on every claim and fences superseded executions
  out of writing anything. Per-attempt details are in `jorb_history`
  (`pj-admin jobs history ID`).
- **`crashed` is terminal**: the dead letter queue is exactly
  `state = 'crashed'`. `pj-admin dlq list` / `pj-admin dlq retry ID`
  (errors reset) or `pj-admin jobs rerun ID`.
- **DXE jobs** (using `self.step(...)`) resume from their last completed
  checkpoint on any retry — `pj-admin jobs steps ID` shows what completed.
  `pj-admin jobs rerun ID` wipes checkpoints for a from-scratch rerun;
  `--resume` keeps them.
- **Durable sleeps** hold no worker: a sleeping job is simply `queued` with
  a future `run_after`.

## Timeouts

A job's timeout is `admin_data.timeout_seconds`, else the job class's
`timeout`, else the worker's `--default-timeout` (3600s). `0` disables it.
It is **one** number, enforced in two places:

- **In-process, by the worker.** A single deadline wraps the whole
  execution, so a job configured for N seconds stops being run at N seconds
  — not at 2N because it spent time producing its coroutine, and not
  indefinitely because it streamed its results from an async generator.
  Reaching it applies `on_timeout` (`retry`, the default, or `fail`).
- **Out-of-process, by the monitor.** `jorb.timeout_at` is written when the
  job starts running and cleared by every terminal transition; the monitor
  sweeps rows past it and applies the same `on_timeout` policy. This is the
  backstop for everything the worker cannot enforce itself — a killed
  worker, a lost database connection, or a job that blocks so hard it
  cannot be interrupted.

**What the in-process deadline can actually interrupt.** It is delivered as
a cancellation at an await point, so async code is genuinely stopped where
it is suspended, `finally` blocks and all. A _synchronous_ `task()` runs in
a worker thread: the deadline still fires on time and the job is recorded as
timed out, but nothing stops the thread — it runs to completion in the
background and its result is discarded. Synchronous code called **inline**
from an async `task()` is worse: it blocks the event loop and starves the
timer, so its deadline is advisory until it returns. For both, the monitor
(or ending the process) is what bounds the rest. Note that `self.cancelled`
is the **operator** cancel signal (`pj-admin jobs cancel`) and is _not_ set
by a timeout: a long synchronous loop that wants to stop itself early has to
watch its own clock.

**A job cannot report success past its own deadline.** Catching the
cancellation to clean up and re-raising is correct and unchanged. Catching it
and _returning a value_ used to store that value as a success — for an
attempt the worker had already given up on, and terminally, so the monitor
could never correct it. The worker now refuses that result and records the
timeout, applying `on_timeout` exactly as if the cancellation had propagated.
This is keyed on the deadline's timer having fired while the job was still
running, not on a clock read taken afterwards, so a job that finishes just
inside its deadline is still a success no matter how long the worker then
takes to store it. An _exception_ raised after the deadline is still recorded
as that exception (message and traceback intact), which means it follows
`max_retries` rather than `on_timeout`.

## Abandoned job threads: when a worker stops claiming on purpose

Every job's `run()` is called in a thread from the worker's **own** pool,
sized by `--job-threads` (default 8). A timed-out synchronous job leaves its
thread running — nothing can stop it — so those threads accumulate on a
worker whose jobs keep blocking past their deadlines.

A worker runs one job at a time, so between jobs _every live job thread is an
abandoned one_. When they fill the pool the worker **stops claiming and says
so**, at ERROR, immediately and then every 30s until it recovers:

```
[q:1000] NOT CLAIMING: 8 abandoned job thread(s) fill this worker's pool of 8.
Timed-out synchronous jobs cannot be stopped; this worker resumes when they
finish. Refusing for 0s so far — if this persists, that job class blocks far
past its timeout and needs a shorter one, an interruptible implementation, or
its own worker.
```

It resumes (with a `Claiming again after Ns` warning) as soon as one thread
finishes. Nothing is claimed and abandoned to do this, so no job's retry
budget is spent on the worker's condition: the queue simply backs up where
other workers can drain it.

The dedicated pool matters on its own — job threads used to share asyncio's
default executor with the worker's own `getaddrinfo`, so a runaway job class
could break the worker's reconnects. Now it can only exhaust the budget that
exists for running jobs.

### How you find out (without reading that worker's log)

A worker in this state keeps heartbeating, so on liveness alone it is
indistinguishable from a healthy idle one — which is the worst shape of
outage there is. The worker therefore publishes the condition on its own
registry row, on the heartbeat statement that already ran every cycle:

| Column                              | Meaning                                        |
| ----------------------------------- | ---------------------------------------------- |
| `jorb_worker.job_threads`           | this worker's pool size (`--job-threads`)      |
| `jorb_worker.job_threads_abandoned` | live threads belonging to no job it is running |

`job_threads_abandoned >= job_threads` **is** the refusing state. Both counts
are published rather than that one boolean because the boolean hides the
approach: 7 of 8 is one timed-out job away from a worker doing nothing, and
reads identically to 0 of 8. A thread belonging to a job that is _currently
running_ is never counted, so a healthy worker reads 0 even mid-job.

Everything else reads that row:

```bash
pj-admin doctor          # WARN job-threads: N of M live worker(s) not claiming -- worker 42 (host:pid, queue q) 8/8 job threads abandoned. ...
pj-admin workers list    # Status "not claiming", Threads "8/8"
```

```
pyjobby_workers_not_claiming               1   # live workers claiming nothing
pyjobby_worker_job_threads_abandoned_max   8   # the worst one: the approach
```

Alert on `pyjobby_workers_not_claiming > 0`. It is a **WARN** in `doctor`, not
a FAIL (exit code stays 0): the condition is self-healing — the abandoned
threads finish and the worker resumes — and lost capacity is graded the same
way as `no live workers seen in last 60s`. If it is actually costing
throughput, the backlog check says so from the queue's side. The web
dashboard's worker table shows the same status, and `/api/metrics` carries it
under `job_threads`.

`pyjobby_workers_live` still counts these workers, deliberately: they _are_
alive. That is why the second gauge sits next to it.

**If you see it:** that queue's job class blocks far past its timeout. Fix it
with a shorter timeout, an interruptible (async, or self-clock-watching)
implementation, or a dedicated queue and worker for it. Raising
`--job-threads` buys tolerance for more simultaneously-abandoned threads; it
does not make them stoppable. Ending the process is still the only thing that
does — and note that those threads also delay the process's own exit.

## Queue controls (live; no restarts)

```bash
pj-admin queues pause NAME          # workers stop claiming immediately
pj-admin queues resume NAME
pj-admin queues limits NAME --max-concurrency 8 --rate-limit 100 --rate-period 60
pj-admin queues limits NAME --max-concurrency none      # clear a limit
pj-admin queues limits NAME --partition-limits          # count them PER tenant
pj-admin queues limits NAME --no-partition-limits       # back to queue-wide
pj-admin queues show NAME
```

Controls live in `jorb_queue` and are enforced inside the worker's claim
statement — changes take effect on the next claim attempt (sub-second).

## Priority, and the ceiling a worker claims under

`jorb.prio` is **inverted**: LOWER is MORE urgent. 100 is the default, 10
jumps the queue, 900 is background work. Every worker also has a **ceiling**
— `pj --max-prio`, default 1000 — and claims only `prio <= ceiling`.

A priority above every live worker's ceiling is therefore not "very low
priority", it is **unclaimable**: the job is never claimed, never runs, never
fails, never retries, never reaches the DLQ, and no age-based check sees it,
because none of them look at `queued`. It simply sits there. Three things
stop that happening quietly:

- **`doctor` sweeps for it.** `WARN unclaimable` counts, per queue, the
  jobs that are runnable now and above the ceiling of every live worker on
  that queue — naming the highest `--max-prio` in the fleet, the priorities
  blocked behind it, and example ids for `pj-admin jobs why ID`. It is the
  only check that finds these without being handed a job id.

- **The client refuses the enqueue.** `client.enqueue(..., priority=5000)`
  raises `ValueError` naming the ceiling — at the caller, where it can still
  be fixed. The ceiling is a _worker_ setting the client cannot observe, so a
  deployment that really runs less-urgent work declares it once, and in both
  places:

  ```bash
  pj --config ./pyjobby.toml --queue backfill --max-prio 5000
  ```

  ```python
  client = JobClient(
      pool, prio_ceiling=5000
  )  # or JobClient.create(..., prio_ceiling=5000)
  ```

  Both halves are required. Declaring it on the client alone gets the job
  written and still never claimed; the flag alone is enough for a worker but
  the client will keep refusing to feed it.

- **An idle worker reports what is hiding above it.** For rows that arrived
  another way — raw SQL, a schedule, a tool — a worker with nothing to claim
  logs this at most once a minute, and never while it has work to do:

  ```
  [backfill:1000] 3 runnable job(s) on this queue are ABOVE this worker's priority
  ceiling of 1000 ... the lowest blocked one is 4200 ... unless another worker on
  this queue runs with a higher --max-prio, those jobs stay queued forever.
  ```

To fix jobs already in that state: lower their priority
(`pj-admin jobs set-priority ID 900`, or `client.update_job_priority(id,
900)`, both refused above the ceiling for the same reason), or start a
worker whose `--max-prio` covers them.

## Pinning in-flight work to a code version

A rolling deploy replaces the code under jobs that are already queued. A job
enqueued with an `app_version` is claimed **only** by a worker advertising the
same one, so its remaining work runs on the build it was written for:

```bash
pj --config ./pyjobby.toml --queue default --app-version 2026.07.28+a1b2c3d
```

```python
await client.enqueue("myapp.jobs.MigrateTenant", app_version="2026.07.28+a1b2c3d")
# or declare it once: JobClient(pool, app_version=...), or `app_version` in
# pyjobby.toml, which both `pj` and JobClient.from_config read
```

**Only the job opts in.** A job with no `app_version` — the default — is
claimed by every worker, versioned ones included, so a deploy never stops the
fleet draining its ordinary backlog. There is no worker-side "matching work
only" flag.

Retry, re-run and DLQ retry keep the pin (same row, same code). A fork does
not, unless asked: forking is how work is re-run under _new_ code. Schedule
firings are never pinned.

A job pinned to a build nobody runs is unclaimable in exactly the way a job
above every ceiling is, and the same three things stop it happening quietly:
`doctor`'s `WARN unclaimable` counts them per queue and names the versions the
fleet _does_ advertise, `pj-admin jobs why ID` answers `app_version_unmet` with
those numbers, and an idle worker logs it at most once a minute:

```
[default:1000] 3 runnable job(s) on this queue are PINNED to an app version this
worker does not advertise (e.g. '2026.07.01'; this worker advertises
'2026.07.28') ... those jobs stay queued forever
```

Nothing can refuse the pin at enqueue time — the fleet it must match is
whatever is running when the job is finally claimed — so the reporting is the
whole safety net. Two remedies: run the version the job asked for, or change
the job (`pj-admin jobs set-app-version ID VERSION`, or `--clear` to unpin it).

## Retention: what it deletes, and what it refuses to

Retention is **on by default** (`--retention-days 30`) and runs in the
monitor. Two windows, and the second one exists because checkpoints are the
bulkiest rows in the system with the shortest useful life:

| Window                            | Deletes                                                                                                                                                                                                                                                                                                                                        |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--retention-days` (30)           | terminal jobs — and with them, by cascade, their history, events, streams, mail and checkpoints — plus **the five tables no cascade reaches**: consumed mailbox rows of _live_ jobs, history rows of _live_ jobs (a durable machine that never terminates writes ~3 per wake, forever), emptied DAGs, schedule executions, retired worker registry rows |
| `--checkpoint-retention-days` (1) | the `jorb_step` checkpoints **and `jorb_stream` rows** of terminal jobs, keeping the job row itself. Both are `finished`-only: a `crashed`/`cancelled` job is retryable, its retry fast-forwards completed checkpoints, and reaping either early would make the resumed job re-run steps or leave its stream missing what the first attempt wrote |

`0` on either means keep forever; that sweep does not run at all.

**One window covers all five job-scoped tables** rather than a knob per
table, because none of them has a lifetime of its own to argue for — they all
mean "as long as the work they describe". Checkpoints get the second knob
because they genuinely do, and streams share it rather than earning a third:
a stream is read while the job runs, and every reader stops at the terminal
state.

The three tables added last are the ones a cascade can never reach, and two
of them were leaking an _answer_, not just rows:

- **`jorb_dag`** — jobs point **at** a DAG (`ON DELETE SET NULL`), so
  deleting jobs never touches it. Left alone, `pj-admin dag list` fills up
  with DAGs reporting `total_jobs = 0` — DAGs that appear to have run
  nothing, forever, when in fact they completed and their jobs aged out. A
  DAG is reaped once it is past the window **and has no jobs left**; one with
  even a single job, in any state, is kept at any age.
- **`jorb_schedule_log`** — cascades only from `jorb_schedule`, which you
  disable rather than delete, so it had no upper bound at all. Each
  schedule's **most recent execution is never deleted**, however old: a
  quarterly schedule must not read as "never ran" in
  `pj-admin schedule history` while `last_run` says otherwise.
- **`jorb_worker`** — one row per worker _process start_, previously only
  stamped `shutdown_at`, never removed; a fleet that redeploys daily
  accumulates rows indefinitely. Only rows that are both retired **and**
  silent for the whole window are candidates, so a live worker — or one the
  monitor retired during a blip that then came back — is never touched. A
  worker that still owns `claimed`/`running` jobs is refused whatever its
  age, because `claimed_by` carries no foreign key and deleting the row
  would strand that work where no sweep can find it.

That last point is the general rule: **every retention sweep refuses to
delete a row something live still needs**, and the refusal is the half worth
knowing. Job retention already keeps a terminal job that a `waiting` job
depends on; these keep a populated DAG, a schedule's last execution, and a
worker with jobs in flight.

One thing the job window deletes is not a row but a **promise**: reaping a
terminal job frees its `identity_key`, so the retention horizon _is_ the
bound on the platform's at-most-once guarantee — the same identity enqueued
after the window creates a new job
([CLIENT_LIBRARY.md](CLIENT_LIBRARY.md#4b-at-most-once-work-identity-keys)).
Lengthening `--retention-days` lengthens that guarantee; shortening it
shortens it.

Nothing here is an operator action. Watch that the monitor logs
`caught up` rather than `stopped on its ... budget`
([TROUBLESHOOTING](TROUBLESHOOTING.md#retention-is-falling-behind)), and set
`--retention-days` to match your storage budget.

## Failure playbooks

**A worker host died.** Nothing to do. Its registry heartbeat
(`jorb_worker.last_seen`) goes stale; within the monitor's
`--liveness-grace` (60s default) the monitor requeues its in-flight jobs
and retires the worker rows. Jobs resume from their last completed step.
A job claimed by a worker that never managed to register at all has no
heartbeat to judge by; the monitor requeues those by claim AGE instead,
after `--claimed-grace` (300s default).

Tuning either half of that: workers heartbeat every 10s
(`pj --heartbeat-interval`), and `--liveness-grace` must stay comfortably
above it — keep the default 6× ratio when lowering both for faster
failover. A grace the heartbeat cannot reliably beat declares **live**
workers dead between beats and requeues their in-flight jobs out from
under them, over and over; no job longer than the grace ever finishes,
while the monitor logs `Requeued job ... from dead worker` for workers
that are fine. The monitor warns at startup when configured that way.

**A job is stuck running / hung.** `pj-admin jobs inspect ID` — if past its
`timeout_at`, the monitor will retry/dead-letter it per its `on_timeout`
policy. To intervene now: `pj-admin jobs cancel ID` (running jobs receive
the cancellation within ~1s and stop at their next await point).

**A queue is flooding the system.** `pj-admin queues pause NAME`, then
inspect (`pj-admin jobs list -q NAME`), bulk-cancel or fix, then resume.
For chronic pressure set `--max-concurrency` / `--rate-limit` instead.

**Jobs are landing in the DLQ.** `pj-admin dlq list`, then
`pj-admin jobs history ID` for the per-attempt errors and
`pj-admin jobs steps ID` to see where a durable pipeline stopped. After a
code fix, `pj-admin dlq retry ID` (fresh attempt budget).

**Nothing is being claimed.** In order: `pj-admin doctor` (a `WARN
job-threads` names any worker that is alive and claiming nothing; a `WARN
unclaimable` names work no live worker on that queue could ever claim);
`pj-admin queues show NAME` (paused? limits hit?); `pj-admin workers list`
(any live workers on that queue, and is any of them `not claiming`?); the
workers' own logs for `NOT CLAIMING` (abandoned job threads — see above) and
for `ABOVE this worker's priority ceiling` (jobs whose `prio` exceeds
`--max-prio` — see [Priority, and the ceiling a worker claims
under](#priority-and-the-ceiling-a-worker-claims-under)) and for `PINNED to an
app version` (jobs pinned to a build this worker does not run — see [Pinning
in-flight work to a code
version](#pinning-in-flight-work-to-a-code-version)); and remember that a
`capability` no worker advertises is invisible in the same way.

**The scheduler missed fires** (was down at fire time). By default missed
ticks are skipped, not backfilled, and `next_run` advances from now. A
schedule created with `pj-admin schedule add ... --backfill-limit N` instead
catches up on the N most recent missed ticks and records the rest as one
`backfill_limit` skip — never more than N + 1 fires per recovery, so no outage
can flood the queue on restart. Check `pj-admin schedule history NAME`.

**Database was down.** Workers/monitor/scheduler reconnect with backoff
automatically and re-prepare their statements; nothing needs a restart.

## Observability quick reference

| Question                            | Answer                                                            |
| ----------------------------------- | ----------------------------------------------------------------- |
| Fleet health                        | `pj-admin doctor`, `pj-admin workers list`                        |
| A worker is alive but doing nothing | `pyjobby_workers_not_claiming`, `doctor`'s `job-threads` check    |
| Queued work nothing can ever claim  | `doctor`'s `unclaimable` check, then `pj-admin jobs why ID`       |
| Queue depths/ages                   | `pj-admin queues list`, `/metrics` gauges                         |
| What happened to job N              | `pj-admin jobs history N`, `jobs steps N`                         |
| Throughput/error rates              | `/metrics` counters + duration quantiles                          |
| Live event stream                   | `pj-ws`, then the dashboard it serves at `http://127.0.0.1:8082/` |
| Progress of a running job           | `client.get_event(job_id, "progress")` (if the job publishes)     |

## Queue controls: what the limits actually promise

`max_concurrency` and `rate_limit` are enforced by `claim_jorb()` in the
database, not by the workers, so they bind every claimer — a worker, a
script, anything that admits a job. Two consequences worth knowing:

- **The limits are exact, not approximate.** Claims for a queue that has
  either limit set are serialized against each other, so simultaneous
  claims cannot each read a stale count and admit past the cap.
- **They cost nothing when unset.** A queue with no limits never takes the
  lock and claims exactly as fast as it did before.

`rate_limit` counts **admissions** in the trailing `rate_period_seconds`
window — jobs picked up by a worker, not jobs that reached `running`. The two
differ by one statement, and counting the latter let a burst slip through.

### `partition_limits`: the same limits, per tenant

A queue-wide `max_concurrency` is a fair-share scheme with one participant.
Put two tenants on the queue and the busier one takes the whole cap: the
other's work sits there queued, runnable, and never claimed, because the cap
is already full of somebody else's jobs.

`partition_limits` **re-scopes the limits that queue already has** to each
distinct `jorb.partition_key`:

```bash
pj-admin queues limits ingest --max-concurrency 4 --partition-limits
```

- `max_concurrency 4` now means **4 in flight per key**, not 4 on the queue.
- `rate_limit R` per period now means **R admissions per key per window** —
  every key gets its own window.
- Jobs get their key at enqueue: `client.enqueue(..., partition_key="acme")`.
  It flows through every enqueue path (single, batch, `debounce`,
  `enqueue_identified`) and a **fork inherits it**, because it says whose
  work this is rather than which piece of work it is.

Three rules worth reading twice:

- **IT ADDS NO LIMIT OF ITS OWN.** On a queue with neither `max_concurrency`
  nor `rate_limit` set, turning it on changes nothing at all — there is
  nothing to re-scope. `queues limits` warns when you do that.
- **THE NULL LANE IS A LANE.** Jobs enqueued without a `partition_key` form
  **one lane among the others**: counted, capped and admitted exactly like a
  named one. They are never hidden from the claim and never refused for being
  unlabelled. A fair-share scheme that quietly blackholes the work nobody
  remembered to label is a worse failure than the starvation it replaced, so
  the platform will not do it — you can adopt `partition_key` on a live queue
  one producer at a time.
- **Exactness is unchanged, and so is the cost tier.** A queue with a limit
  serialises its claims whether the limit is per queue or per lane, so the
  per-lane counts cannot be fooled by an uncommitted claim any more than the
  queue-wide ones could. A queue with **no** limit still never takes the lock,
  `partition_limits` set or not.

`pj-admin queues show NAME` reports the scope on the limits themselves
(`Max concurrency: 4 PER partition_key`), and `pj-admin jobs why ID` names
the lane a job is waiting behind and how many of that lane's jobs are in
flight. Work waiting on its lane's limit is **backlog, not unclaimable**:
`doctor`'s unclaimable sweep stays silent about it, because it is claimed the
moment the lane lets go.

What it costs is in [SCALE.md](SCALE.md#partitioned-claims-what-fairness-costs).

## Retry, re-run, fork

Three verbs for "run this again", because they carry different risk. The
first two reuse the job's row; the third does not.

| Verb | Row | Starts from | For |
| --- | --- | --- | --- |
| `jobs retry ID` | **same** id | its checkpoints (resume) | a job that did _not_ succeed (`crashed`, `cancelled`) |
| `jobs rerun ID` | **same** id | step 1 (or `--resume`) | any terminal job, **including a finished one** |
| `jobs fork ID` | **NEW** id | `--from-step N` | any job in any state, when the re-run must not be the same job |

- **Retry** — `pj-admin jobs retry ID`, `pj-admin dlq retry ID`. Refuses a
  finished job: re-running successful work repeats its side effects, and
  that has to be asked for by name.
- **Re-run** — `pj-admin jobs rerun ID`. Accepts a finished job. FRESH by
  default: the DXE step checkpoints are dropped and the job re-executes
  from step 1 — that is what "run it again" means. Add `--resume` to keep
  the checkpoints instead; completed steps fast-forward and the job
  continues where it stopped, which is how an interrupted durable job is
  resumed.
- **Fork** — `pj-admin jobs fork ID --from-step N` (or `--from-failure`).
  Creates a **second job** that re-executes this one's work from step N,
  with steps 1..N-1 copied in as checkpoints so they fast-forward. The
  source is not touched at all — not its state, not its result, not its
  checkpoints — so it can be in _any_ state, running included.

Reach for **fork** when the re-run must not be the same job:

- **replay an incident from just before the failing step.** Deploy the fix,
  `jobs fork ID --from-failure`, and the expensive prefix is not paid for a
  second time. The crashed original stays crashed as the record of what
  happened.
- **re-run an expensive pipeline's tail** under different behaviour, without
  destroying the run you already have.
- **change a row-level knob a retry cannot touch**: `--queue`, `--priority`,
  or different arguments.

What a fork inherits: job class, arguments, queue, priority, capability,
`uid`, tags, `partition_key`, and the retry/timeout policy — everything that
describes or labels the WORK (`uid` is a tenant tag and `partition_key` is a
tenant's fair-share lane, so a tenant's fork stays theirs and keeps counting
against their share).
What it does not: `deadline_key`, `identity_key`, `debounce_key`,
`schedule_id`, DAG
membership, dependency edges, and every execution counter. A fork is a new
identity, so it cannot inherit one: two live rows sharing an idempotency key
would make that key mean nothing, and an `identity_key` promises there is
only one row holding it.

Streams, events and mailbox messages are **not** copied either — they are
the source's output, and the fork produces its own
([DXE.md](DXE.md#forking-a-job-a-new-row-from-a-checkpoint-prefix)).

Lineage is best-effort audit, not a dependency: `jobs inspect` shows
`Forked From` on the fork and `Forked Into` on the source, and retention may
reap the source at any time (the fork survives, and its own history row
keeps the id).

## Reading the latency numbers

`pj-admin metrics` reports queue wait and execution duration separately:

- **Avg/Max Queue Wait** — how long jobs sat before a worker picked them up
  (`claimed_at - run_after`). Rising wait with flat duration is a **capacity**
  problem: add workers, or raise `max_concurrency`.
- **Avg Duration** — how long jobs ran once picked up (`finished - started`).
  Rising duration is a **code or dependency** problem.

A single blended "how long did the job take" number cannot tell these apart,
which is why it is not reported.
