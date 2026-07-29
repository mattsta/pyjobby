# Troubleshooting pyjobby

A symptom index. Every entry names the check that confirms it and the
document that explains it — the playbooks live in
[OPERATIONS.md](OPERATIONS.md), the scaling failure modes in
[SCALE.md](SCALE.md), and this file does not restate either.

## Start with `doctor`

```bash
pj-admin --dsn "$PYJOBBY_DSN" doctor
```

It is the executable version of "is the platform healthy", it runs in about
a second, and it exits 1 on any FAIL — so it works as a cron probe, a CI
gate, and a deploy smoke test. Run it before reading logs.

```console
$ pj-admin --dsn "$PYJOBBY_DSN" doctor
PASS database: connected
PASS schema: installed, migrations current (baseline)
PASS triggers: all schema triggers present (7)
PASS notify-queue: 0.0% full
WARN workers: no live workers seen in last 60s
PASS job-threads: 0 live worker(s) claiming
PASS queue q_reports: depth 1, oldest runnable 51m
PASS unclaimable: no queued job is invisible to its queue's live workers
PASS blocked-waiters: no waiting jobs blocked on failed upstreams
PASS mailbox: no unread mail older than a day
PASS dlq: empty
PASS schedules: no overdue schedules
$ echo $?
0
```

FAIL means the platform cannot function and is the only thing that changes
the exit code. Lost capacity is a WARN, deliberately: "no live workers at
all" is a WARN, so one worker of ten refusing to claim cannot be graver.

| Check             | FAIL / WARN means                                                                                                                          | Go to                                                                                     |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `database`        | cannot connect with the DSN or config given                                                                                                | [Config and connection](#the-database-is-unreachable-or-the-config-is-wrong)              |
| `schema`          | no schema at all, or a schema **missing objects this release needs** (each one named)                                                      | [Schema is missing or stale](#the-schema-is-missing-or-stale)                             |
| `triggers`        | one of the schema's triggers is missing — NOTIFY waiters degrade to polling, or history stops being recorded                               | [Schema is missing or stale](#the-schema-is-missing-or-stale)                             |
| `notify-queue`    | WARN at 25% full, FAIL past 50%                                                                                                            | [NOTIFY queue saturation](#notify-queue-saturation)                                       |
| `workers`         | no heartbeat in the last 60s                                                                                                               | [Nothing is being claimed](#nothing-is-being-claimed)                                     |
| `job-threads`     | live workers that claim nothing                                                                                                            | [A worker is alive and doing nothing](#a-worker-is-alive-heartbeating-and-doing-nothing)  |
| `queue <name>`    | backlog past `--max-depth` (10000) or `--max-age-minutes` (60)                                                                             | [The backlog is growing](#the-backlog-is-growing)                                         |
| `unclaimable`     | queued, runnable jobs that no live worker on their queue could ever claim — above every ceiling, or wanting a capability nobody advertises | [Jobs sit queued forever](#jobs-sit-queued-forever-and-nothing-is-wrong-with-the-workers) |
| `blocked-waiters` | jobs in `waiting` whose upstream crashed or was cancelled — the monitor leaves them alone, so this is the only place they show up          | [Jobs are landing in the DLQ](#jobs-are-landing-in-the-dlq)                               |
| `mailbox`         | unread durable mail older than a day — usually a sender using a topic nothing `recv()`s                                                    | [STATECHARTS.md § Waiting](STATECHARTS.md#waiting)                                        |
| `dlq`             | jobs have exhausted their retries                                                                                                          | [Jobs are landing in the DLQ](#jobs-are-landing-in-the-dlq)                               |
| `schedules`       | an enabled schedule is overdue by >5m                                                                                                      | [A schedule is not firing](#a-schedule-is-not-firing)                                     |

Age is the more honest queue alarm than depth: a deep queue that is
draining is fine; an old queue is not. Tune the thresholds per install with
`--max-depth` and `--max-age-minutes`.

## Symptom index

| Symptom                                                       | Section                                                                                   |
| ------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| One named job is not running                                  | `pj-admin jobs why ID`, then [Nothing is being claimed](#nothing-is-being-claimed)        |
| Jobs sit in `queued`, workers look idle                       | [Nothing is being claimed](#nothing-is-being-claimed)                                     |
| Jobs sit queued forever and the workers are fine              | [Jobs sit queued forever](#jobs-sit-queued-forever-and-nothing-is-wrong-with-the-workers) |
| A worker heartbeats but never claims                          | [A worker is alive and doing nothing](#a-worker-is-alive-heartbeating-and-doing-nothing)  |
| Queue depth or age climbing                                   | [The backlog is growing](#the-backlog-is-growing)                                         |
| One tenant's jobs pile up while the rest of the queue drains  | [One partition is backed up](#one-partition-is-backed-up-and-the-rest-of-the-queue-is-fine) |
| The table grows even though retention is on                   | [Retention is falling behind](#retention-is-falling-behind)                               |
| Enqueues start failing platform-wide                          | [NOTIFY queue saturation](#notify-queue-saturation)                                       |
| A cron schedule stopped running                               | [A schedule is not firing](#a-schedule-is-not-firing)                                     |
| Jobs stuck in `claimed` or `running`                          | [A job is stuck](#a-job-is-stuck-in-claimed-or-running)                                   |
| `crashed` count rising                                        | [Jobs are landing in the DLQ](#jobs-are-landing-in-the-dlq)                               |
| `column ... does not exist`, `relation "jorb" does not exist` | [Schema is missing or stale](#the-schema-is-missing-or-stale)                             |
| `Job class not found`                                         | [A job class cannot be imported](#a-job-class-cannot-be-imported)                         |
| A job ran twice                                               | [A job ran more than once](#a-job-ran-more-than-once)                                     |
| A process exits immediately at startup                        | [Config and connection](#the-database-is-unreachable-or-the-config-is-wrong)              |

---

## The database is unreachable, or the config is wrong

Every entry point exits non-zero when it cannot resolve or load its
configuration, and says which of the two failed:

```console
$ pj-admin -c /nonexistent.toml doctor
Error: Could not load config file: /nonexistent.toml
Error: '/nonexistent.toml' doesn't exist
Error: Use --config to point at a pyjobby conf file, or --dsn to connect directly.
FAIL config: unusable
```

`pj`, `pj-scheduler`, `pj-web` and `pj-ws` accept **only** a config file —
they do not read `PYJOBBY_DSN`. A container that exports the DSN and
mounts no config will start `pj-monitor` and `pj-admin` fine and fail every
worker. The full matrix is in
[deployment-guide.md § Configuration](deployment-guide.md#configuration).

A database that goes away _after_ startup is not an incident: workers, the
monitor and the scheduler reconnect with backoff and re-prepare their
statements. Nothing needs restarting, and restarting a worker only
abandons its in-flight jobs to the monitor.

## The schema is missing or stale

Both cases are a `FAIL schema`, and both are fixed by the same command.

**No schema at all:**

```console
$ pj-admin doctor
PASS database: connected
FAIL schema: base schema not installed (run: pj-admin db migrate)
$ echo $?
1
```

**A schema of the wrong shape** — installed from a different revision of
the base schema, so it has `jorb` and records nothing pending, and a
presence-only check would pass it. `doctor` checks the schema's _shape_
against the manifest of objects this release actually addresses
(`pyjobby/migrations.py`), and names what is absent:

```console
$ pj-admin doctor
PASS database: connected
FAIL schema: installed, but 3 object(s) this release needs are missing: index jorb_dag_retention_idx, index jorb_schedule_log_retention_idx, index jorb_worker_retention_idx (run: pj-admin db migrate)
$ echo $?
1
```

It names up to five and then counts the rest; `pj-admin db status` lists
every one:

```console
$ pj-admin db status
Base schema installed: yes
Applied migrations:    none
Pending migrations:    none
Missing objects:       3
  index jorb_dag_retention_idx
  index jorb_schedule_log_retention_idx
  index jorb_worker_retention_idx
```

`Missing objects` is the line that matters. The version lines can only say
what this database _recorded_, and a drifted database records exactly what
a current one does — only the object list tells them apart.

Both checks stop the report: every line below `schema` queries something
the check just reported missing, and a health report that crashes halfway
through is worse than one that stops.

**Fix: `pj-admin db migrate`.** On an empty database it installs the whole
base schema; on a database with pending migration files (a live-deployment
concern — none ship today) it applies them, one transaction per file,
serialised fleet-wide by an advisory lock, so running it from every host's
deploy step is safe. It also recreates a missing **trigger or index** from
the base schema — the two kinds of drift whose canonical DDL is a
standalone CREATE statement, so recreating one risks no data. Anything
deeper — a missing table, column, view or function — means the database
was installed from a different schema revision; `db migrate` refuses
rather than repairing on top of it, the FAIL line says so instead of
prescribing it, and the remedies are recreating the database or
reconciling by hand from the object list. See
[deployment-guide.md § The database](deployment-guide.md#the-database).

If a stale schema is reached by a command rather than by `doctor`, you get
a message instead of a traceback — `pj-admin` turns every
undefined-table/column/function error into the same answer:

```console
$ pj-admin workers list
Error: The database schema is missing or out of date: relation "jorb_worker" does not exist
Error: Install or upgrade it with `pj-admin db migrate`, then confirm with `pj-admin doctor`.
$ echo $?
1
```

The same handler covers a missing _column_ (`column "job_threads" does not
exist`), a missing function and a missing schema, and it sits on the root
command group — so every `pj-admin` subcommand, including ones added later,
answers a stale database the same way.

A worker or the monitor hitting the same condition still raises
`asyncpg.exceptions.UndefinedColumnError` / `UndefinedTableError` in its
log. Any of those naming a `jorb*` table means this database does not match
this version of pyjobby; run `db migrate` and confirm with `doctor`.

## Nothing is being claimed

**If you have a job id, start with `pj-admin jobs why ID`.** It is the
executable version of this whole section: it asks the claim path itself
which of these conditions is refusing the job and answers with the numbers
behind it — the paused queue, the cap and what is filling it, the missing
capability and what _is_ advertised, the priority ceiling, the `run_after`,
the blocking job and its state. The priority-ceiling case in particular
cannot be reached any other way at runtime.

```console
$ pj-admin jobs why 48822

Job 48822: capability_unmet
queued  reports  myapp.jobs.RenderVideo  prio 100  cap gpu
--------------------------------------------------
This job requires capability 'gpu' and none of the 1 live worker(s) on 'reports' advertises it (they advertise: cpu). Start a worker with `pj --queue reports --cap gpu`.

    capability:                      gpu
    live_workers:                    1
    workers_with_capability:         0
    advertised_capabilities:         cpu
```

`--json` gives a monitoring script the same answer with a stable `reason`
code; the reason table is in
[ADMIN_TOOLS.md § Why is this job not running?](ADMIN_TOOLS.md#why-is-this-job-not-running).

The rest of this section is the same walk by hand — for when the symptom is
"the queue is not moving" and there is no one job to point at:

1. **`pj-admin doctor`.** A `WARN job-threads` line names any worker that
   is alive and claiming nothing — that is a different problem, below. A
   `WARN unclaimable` line names work that no live worker on that queue
   could ever claim, and is the next section.
2. **`pj-admin queues show NAME`.** Is it `Paused: yes`? Is
   `Max concurrency` or `Rate limit` set and binding? Both are enforced in
   the database, so they bind every claimer, and a paused queue stops
   claims within a fraction of a second.
3. **`pj-admin workers list`.** Are there live workers _on that queue_?
   Read the Queue column, not the worker count. `--workers` is **per
   queue**, and no worker is ever started on a queue you did not name:
   `pj --queue reports --workers 4` is four workers, all on `reports`, and
   `--queue reports --queue billing --workers 4` is eight processes, four
   on each. See
   [deployment-guide.md § Worker settings](deployment-guide.md#worker-settings).
4. **Capability.** A job with a `capability` no worker advertises is
   invisible to those workers. Workers advertise with `--cap`.
5. **Priority — and remember the direction.** A **lower** `prio` is
   claimed **sooner**; the big numbers are the ones nothing runs. Each
   worker has a ceiling (`pj --max-prio`, default 1000) and claims only
   `prio <= ceiling`, so a job above every live worker's ceiling is
   invisible to all of them. `pj-admin jobs inspect ID` shows the job's
   `Priority`; compare it against the `--max-prio` the fleet was started
   with. Rows enqueued through `JobClient` cannot get into this state (the
   client refuses them), so when it happens the row arrived another way —
   raw SQL, a schedule, a tool. An idle worker also logs it once a minute:
   grep its log for `ABOVE this worker's priority ceiling`. Fix by lowering
   the job's `prio` or by running a worker with a `--max-prio` that covers
   it — see
   [OPERATIONS.md § Priority](OPERATIONS.md#priority-and-the-ceiling-a-worker-claims-under).
6. **`run_after`.** A job with a future `run_after` is _supposed_ to be
   invisible — that is how retry backoff and durable sleep are
   implemented. `pj-admin jobs inspect ID` shows it.

   **A debounced job is this case, working as designed**, and it is the one
   where `run_after` keeps moving while you watch. `client.debounce()` parks
   one job per key and every duplicate enqueue of that key pushes the row
   further out, so a busy key can sit `queued` for as long as its producers
   keep going. `pj-admin jobs why ID` says so in as many words and gives the
   two numbers that settle it — when it fires, and the cap past which no
   bounce may defer it (`none` if the caller asked for no cap, in which case
   the answer is to look at the producers, not the fleet):

   ```console
   $ pj-admin jobs why 51203

   Job 51203: deferred
   queued  search  myapp.jobs.ReindexDocument  prio 100
   --------------------------------------------------
   Deferred: run_after is 4s in the future (2026-07-29T11:02:19+00:00). This is how retry backoff, `enqueue_at` and durable sleep are implemented — nothing is wrong. This job is DEBOUNCED on key 'reindex:8814': every duplicate enqueue of that key moves run_after further out and replaces this row's arguments, so it fires once the burst stops — and no later than 2026-07-29T11:02:45+00:00, its cap.

       run_after:                       2026-07-29T11:02:19+00:00
       seconds_until_run_after:         4.0
       debounce_key:                    reindex:8814
       debounce_deadline:               2026-07-29T11:02:45+00:00
   ```

   See
   [CLIENT_LIBRARY.md § Debouncing a burst](CLIENT_LIBRARY.md#4c-debouncing-a-burst-debounce).
7. **The worker's own log**, for `NOT CLAIMING` (next section).

`pj-admin queues pause` / `resume` and `queues limits` change all of this
live, with no restart — see
[OPERATIONS.md § Queue controls](OPERATIONS.md#queue-controls-live-no-restarts).

## Jobs sit queued forever and nothing is wrong with the workers

Every other signal reads healthy: the workers heartbeat, the queue is not
paused, no cap is binding, the DLQ is empty, and the jobs never fail —
because they are never _tried_. They are runnable and **invisible to every
live worker on their queue**, so nothing claims them, nothing times them
out, and no age-based check looks at `queued`. Left alone they sit there
forever.

`doctor` sweeps for exactly this:

```console
$ pj-admin doctor
...
PASS workers: 1 live worker(s) seen in last 60s
PASS job-threads: 1 live worker(s) claiming
PASS queue reports: depth 4, oldest runnable 0m
WARN unclaimable: 4 claimable job(s) that no live worker can claim: 3 on 'reports' above every live worker's ceiling (prio 500; the highest --max-prio among 1 live worker(s) is 100; e.g. jobs 1, 2, 3); 1 on 'reports' needing capability 'gpu', which none of the 1 live worker(s) advertises (they advertise: cpu; e.g. jobs 4). they are runnable and INVISIBLE to every live worker on their queue, so nothing ever claims them: they stay queued forever, never fail, and never reach the DLQ. Raise the fleet's ceiling (pj --max-prio N), start a worker advertising the capability (pj --queue Q --cap C), or lower the job (pj-admin jobs set-priority ID N). Remember lower prio = more urgent. `pj-admin jobs why ID` explains any one of them in full
```

Two causes, and the line says which, per queue:

- **Above every live worker's ceiling.** A worker claims only
  `prio <= --max-prio` (default 1000) and **lower prio is more urgent**, so
  a big number is not "run it last", it is "run it never". Fix by raising
  the fleet's ceiling (`pj --queue reports --max-prio 5000`, which must be
  declared on the client too — see
  [OPERATIONS.md § Priority](OPERATIONS.md#priority-and-the-ceiling-a-worker-claims-under))
  or by lowering the jobs: `pj-admin jobs set-priority ID 900`.
- **A capability nobody advertises.** The line names what the fleet _does_
  advertise. Fix by starting a worker with it: `pj --queue reports --cap gpu`.

Take an id from the line and `pj-admin jobs why ID` for the full per-job
answer with the numbers behind it. Rows enqueued through `JobClient` cannot
reach the priority case (the client refuses them), so when it happens the
row arrived another way — raw SQL, a schedule, a tool.

The check is a **WARN**, never a FAIL: the platform is healthy, the workload
is not. A queue with **no live workers at all** is not reported here — that
is the `workers` check above it, and a different remedy (start a worker,
rather than change what the running ones accept). Alert on it the same way
you alert on the DLQ: it is a condition nothing else in the system will ever
tell you about.

## One partition is backed up and the rest of the queue is fine

The queue drains, throughput looks normal, `doctor` is clean — and one
tenant's jobs are hours old while everyone else's run in seconds. On a queue
with `partition_limits` that is usually the feature working, not failing:
that tenant is at **its own share** of the limit, and its work waits while
the other lanes keep going.

Ask the job:

```console
$ pj-admin jobs why 84213
reason: queue_at_max_concurrency
partition 'acme' of queue 'ingest' is at its concurrency cap: 4 job(s) claimed
or running against a cap of 4 — this queue counts its limits PER
partition_key, so other partitions keep draining and only this one is held
back. ...
```

The `details` carry `partition_limits` and `partition_key`, and the count is
that **lane's** in-flight, not the queue's — so the number you would raise is
the one that is actually binding. Three real fixes, in order of how often
they are right:

1. **Nothing.** The lane is being throttled on purpose. Look at how far
   behind it is (`pj-admin queues stats`, `pj-admin metrics`) and decide
   whether the share is wrong or the tenant is simply sending more than its
   share allows.
2. **Raise the share** — `pj-admin queues limits ingest --max-concurrency 8`.
   It is per lane, so this raises it for every tenant.
3. **Give that tenant its own queue** if its backlog is enormous. A lane
   parked at its cap with a huge backlog in front of the queue's claim order
   is the one shape that costs the claim path real work
   ([SCALE.md](SCALE.md#partitioned-claims-what-fairness-costs)).

Two answers that mean something else:

- `reason: queue_at_max_concurrency` with `partition_limits: false` — the
  limit is queue-wide and one tenant _is_ taking all of it. That is the
  condition `--partition-limits` exists to fix.
- Jobs with **no** `partition_key` piling up — they are their own lane, and
  they are at that lane's cap. They are never hidden: if they were, `jobs
  why` would say so and `doctor`'s unclaimable sweep would find them.

A lane at its limit is **backlog, not unclaimable work**. `doctor` stays
silent about it deliberately: it is claimed the instant the lane lets go, so
reporting it would drown the sweep that finds work no worker can _ever_
claim.

## A worker is alive, heartbeating, and doing nothing

The worst shape of outage there is: on liveness alone this worker is
indistinguishable from a healthy idle one. It has abandoned job threads
filling its pool — synchronous jobs that blew their deadline, which nothing
can interrupt — so it refuses to claim work it cannot start.

`doctor` names up to three of them and then summarises, and
`pj-admin workers list` shows the same state per worker:

```
WARN job-threads: 1 of 4 live worker(s) not claiming -- worker 42 (host-b:9910, queue heavy) 8/8 job threads abandoned. ...

ID  Host      PID    Queue  Status        Threads  Last Seen  Current Job
2   host-b    9910   heavy  not claiming  8/8      3s ago     -
```

Alert on `pyjobby_workers_not_claiming > 0`, and watch
`pyjobby_worker_job_threads_abandoned_max` for the approach: 7 of 8 is one
timed-out job away from a worker doing nothing and reads identically to 0
of 8.

It is a WARN, not a FAIL: the condition is self-healing — the threads
finish and the worker resumes. If it persists, that queue's job class
blocks far past its timeout, and the fix is a shorter timeout, an
interruptible implementation, or a dedicated queue and worker for it.
Raising `--job-threads` buys tolerance, not stoppability.

Full behaviour, the registry columns, and why both counts are published:
[OPERATIONS.md § Abandoned job threads](OPERATIONS.md#abandoned-job-threads-when-a-worker-stops-claiming-on-purpose).

## The backlog is growing

`doctor` warns per queue; `pj-admin metrics` says whether it is capacity or
code:

```
Throughput:        0.35 jobs/s (completed)
Arrivals:          0.35 jobs/s (created)
Balance:           +0.00 jobs/s (keeping up)
...
Avg Duration:      0.00s
Avg Queue Wait:    7.00s
```

- **Arrivals sustained above throughput** is the definition of falling
  behind. Add workers, or raise `--max-concurrency` if a cap is binding.
- **Rising queue wait with flat duration** is capacity.
- **Rising duration** is a code or dependency problem — profile the job,
  not the platform.

If the backlog is a runaway producer rather than missing capacity,
`pj-admin queues pause NAME`, triage, then resume; for chronic pressure set
`--max-concurrency` / `--rate-limit` instead of pausing.

## Retention is falling behind

Retention is on by default, and a sweep that cannot keep up is worse than
none — the dashboard says retention is enabled while the table grows
forever. So it says so, at WARNING:

```
Retention expired jobs: deleted 5000 and stopped on its 5.0s budget with a backlog still pending — retention is falling behind
```

Caught up looks like this, at INFO:

```
Retention expired jobs: deleted 1200, caught up
```

Raise `--retention-batch-size`, or `--retention-max-seconds` if the monitor
has headroom — but understand the trade: the budget is what stops a
retention backlog from delaying timeout enforcement and dead-worker
recovery, which decide how long a stuck job stays stuck. If the arrival
rate genuinely exceeds what one monitor can delete, shorten
`--retention-days` instead.

Confirm from the storage side with `pj-admin metrics`, which reports table
bytes and dead-tuple ratio per table. Background:
[SCALE.md § Retention falling behind ingest](SCALE.md#2-retention-falling-behind-ingest).

Do not write a cleanup cron alongside it. A hand-rolled `DELETE` competes
with the sweeps, misses the tables that hang off `jorb`, and can delete a
terminal job that a `waiting` job still depends on — retention refuses
those on purpose.

## NOTIFY queue saturation

The highest-severity failure mode in the system, because it is a cliff
rather than a gradient. PostgreSQL's async notification queue is
server-wide and bounded, and **at 100% every transaction that issues a
NOTIFY fails** — which means no job can be enqueued or completed anywhere.

```
WARN notify-queue: 31.0% full and it should be near empty -- ...
FAIL notify-queue: 62.0% full -- at 100% every enqueue and completion fails platform-wide; ...
```

`doctor` WARNs at 25% and FAILs past 50%, well before the edge;
`pyjobby_notify_queue_usage_ratio` is the metric to alert on.

The queue drains only as fast as the **slowest connected listener**, so the
cause is almost always a consumer that stopped reading — a wedged
dashboard, a stuck `pj-ws`, an abandoned `LISTEN` session. Find it in
`pg_stat_activity` and end it. There is no channel to turn off for relief:
all five remaining channels are load-bearing, and four of them are already
demand-gated (an unobserved job emits none). The fifth,
`schedule_executed`, is **ungated on purpose** — its consumer, the
websocket dashboard, has no polling fallback, so a gate would drop events
rather than delay them — and it costs nothing here, because it fires once
per schedule _execution_ on `jorb_schedule_log`, at cron rate, never on a
job hot path. Full argument:
[SCALE.md § NOTIFY queue saturation](SCALE.md#3-notify-queue-saturation--the-cliff).

A missing trigger is the opposite failure and `doctor` checks it
separately, by name, over all seven the schema installs — the five NOTIFY
triggers plus `jorb_history_record` and `jorb_dag_complete`. Nothing raises
when one goes missing: waiters silently degrade to their polling fallback,
or the audit trail silently stops being written. `FAIL triggers` names the
ones that are gone. That is a stale-schema symptom; see above.

## A schedule is not firing

```
WARN schedules: 1 enabled schedule(s) overdue by >5m (is pj-scheduler running?)
```

In order:

1. **Is `pj-scheduler` running?** Nothing fires without it, and it is a
   separate process from the workers.
2. **Is the schedule enabled?** `pj-admin schedule show NAME`. Two things
   disable a schedule on their own, and both log at ERROR:
   - **The circuit breaker** — `Schedule 'X' disabled: Circuit breaker
triggered: 5 consecutive failures (threshold: 5)`. Fix the job, then
     `pj-admin schedule enable NAME`.
   - **Unevaluatable** — `Schedule N disabled: ...`. A cron expression or
     timezone that cannot be evaluated can never get a new `next_run`, so
     leaving it enabled would make it due forever and fail on every poll.
     Disabling it is the only outcome that both stops the spin and is
     visible. Delete and re-add it with a valid expression; `schedule add`
     validates both up front.
3. **Was it skipped rather than failed?** `pj-admin schedule history NAME
--result skipped` — `max_concurrent` and the backpressure threshold both
   skip a fire deliberately.
4. **Missed fires are not backfilled.** A scheduler that was down at fire
   time skips those ticks; `next_run` advances from now.

Everything a schedule means is in
[RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md).

## A job is stuck in `claimed` or `running`

Usually it is not stuck — it is being handled.

```bash
pj-admin jobs why ID       # who holds it, since when, and is that worker alive
pj-admin jobs inspect ID   # the whole row
```

`jobs why` answers the question that decides what to do here: it names the
worker (host, pid), when the job was claimed, its deadline, and **whether
that worker is still heartbeating** — a registered worker the registry no
longer counts as live is the difference between "busy" and "the monitor is
about to requeue this".

If it is past its `timeout_at`, `pj-monitor` will retry or dead-letter it
per its `on_timeout` policy on its next sweep (10s by default). If its
worker's host died, the monitor requeues its in-flight jobs within
`--liveness-grace` (60s) and the job resumes from its last completed DXE
step. Both are automatic, and both require `pj-monitor` to actually be
running — if it is not, nothing recovers, ever. That is the first thing to
check.

To intervene now: `pj-admin jobs cancel ID`. Running jobs receive the
cancellation within about a second and stop at their next await point.

**Do not requeue jobs with hand-written SQL.** An `UPDATE jorb SET state =
'queued'` writes no history row, skips the `on_timeout` policy, and hands
the row back to a claimer while the original execution may still be alive.
The platform's own recovery advances `run_epoch`, which is what fences a
superseded execution out of writing results or checkpoints; a manual update
does not, and the two executions race. Use `pj-admin jobs rerun ID`.

A note on what a timeout can interrupt: async code is genuinely stopped at
its await point. A _synchronous_ `task()` runs in a worker thread — the
deadline fires on time and the job is recorded as timed out, but the thread
runs to completion and its result is discarded. That thread is the one that
later shows up as abandoned. Details in
[OPERATIONS.md § Timeouts](OPERATIONS.md#timeouts).

## Jobs are landing in the DLQ

The DLQ is exactly `state = 'crashed'` — the terminal state after retries
are exhausted. There is no separate table and no error-count heuristic.

```bash
pj-admin dlq list
pj-admin jobs history ID     # every attempt, with its error
pj-admin jobs steps ID       # where a durable pipeline stopped
```

`jobs history` is the useful one: the job has kept a single row since it
was enqueued, so the per-attempt trail is the only place the earlier
failures exist.

After a code fix, `pj-admin dlq retry ID` requeues the same row with a
fresh attempt budget. A DXE job resumes from its last completed
checkpoint; `pj-admin jobs rerun ID` wipes the checkpoints and
restarts from step 1 (`--resume` keeps them).

`pj-admin jobs fork ID --from-failure` is the same recovery under a **new
id**: it creates a second job that starts at the step that failed, with the
completed prefix copied in as checkpoints, and leaves the crashed original
untouched as the record of the incident. Take it instead of `dlq retry`
when the evidence must survive the recovery, when the fork needs a
different queue or priority (`--queue`, `--priority`), or when the job has
already been retried as far as it can be. See
[OPERATIONS.md § Retry, re-run, fork](OPERATIONS.md#retry-re-run-fork).

Watch `DLQ Growth` in `pj-admin metrics` and `Retry Pressure` next to it —
retries that eventually succeed cost throughput without ever reaching the
DLQ, so a flat DLQ with rising retry pressure is still a problem.

## A job class cannot be imported

```
FileNotFoundError: Job class not found: job.email.SendEmail; search path: [...]
TypeError: job.email.SendEmail is not a pyjobby Job subclass (got ...)
```

The worker resolves `job_class` as a dotted path at execution time, so the
class must be importable _by the worker process_, not by whatever enqueued
it — and the message prints the search path it used. Check, in order: the
dotted path recorded on the job (`pj-admin jobs inspect ID` prints it,
case-sensitively), the worker's `--path` flags, and `PYTHONPATH` in the
unit or container that starts it. The `TypeError` variant means the path
resolved to something that is not a `pyjobby.pj.Job` subclass — usually a
module, or the wrong name in the right module.

This crashes the job, so it retries and eventually dead-letters — the
signature is a whole job class arriving in the DLQ at once after a deploy
that moved a module.

## A job ran more than once

Retries re-run `task()` from the beginning unless the job checkpoints its
work. That is the design: at-least-once execution, with the tools to make
it exactly-once where it matters.

First rule out the one _configuration_ that repeats every long job: a
monitor `--liveness-grace` at or below the worker heartbeat interval (10s
default, `pj --heartbeat-interval`). Live workers then look dead between
beats and their in-flight jobs are requeued mid-run, over and over — the
signature is the monitor logging `Requeued job ... from dead worker` for
workers that are alive, `run_count` climbing on jobs that never finish,
and a monitor startup WARNING naming the two flags. Raise the grace
(default 60s, 6× the heartbeat).

- **Durable steps** — `self.step(...)` records each completed step, and a
  completed step never runs twice, fenced by `run_epoch` against a zombie
  execution. This is the real answer for side effects. See
  [DXE.md](DXE.md).
- **`deadline_key`** — a partial unique index makes one _queued_ row per
  `(deadline_key, queue)`, so duplicate submissions collapse into one job.
  This is the enqueue-side guard, not the execution-side one. Note that it
  **re-arms**: the key is released the moment a worker claims the job, so it
  stops a duplicate that has not started and nothing after that.
- **`identity_key`** — the enqueue-side guard for work that must happen once
  and only once. Its unique index has no state predicate, so the row holds
  the key while it runs and after it terminates, and a second enqueue
  returns the existing job's id instead of raising. **Bounded by retention:**
  when the sweep reaps the terminal row the key is free again and the same
  identity creates a new job — so if you are seeing a second run days or
  weeks later, check `--retention-days` before suspecting the index, and
  scope keys to a time you can name (`nightly-rebuild:2026-07-29`, not
  `nightly-rebuild`). See
  [CLIENT_LIBRARY.md](CLIENT_LIBRARY.md#4b-at-most-once-work-identity-keys).
- **`client.debounce()`** — the enqueue-side guard for a _burst_: the
  duplicates are not refused and not answered with the existing job, they
  **move** it, so a hundred enqueues become one run carrying the last call's
  arguments. Use it when the duplicates are genuine (nine edits really did
  happen) and only the final state matters. See
  [CLIENT_LIBRARY.md](CLIENT_LIBRARY.md#4c-debouncing-a-burst-debounce).
- **Idempotent side effects** — the fallback when none of them applies.

Note that a re-run you asked for is a separate verb precisely because it
repeats side effects: `jobs retry` refuses a finished job, `jobs rerun`
accepts one. See
[OPERATIONS.md § Retry, re-run, fork](OPERATIONS.md#retry-re-run-fork).

---

## When you still need SQL

Everything above has a command, and the commands write history where hand
SQL does not. When you genuinely need to look at the raw tables, look —
but treat writes as out of bounds:

```sql
-- what the platform thinks it is doing, right now
SELECT state, count(*) FROM jorb GROUP BY state;

-- is autovacuum keeping up on the hot table
SELECT n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = 'jorb';

-- who is not draining the notification queue
SELECT pg_notification_queue_usage();
```

`pj-bench plans` is the tool for "is a query still using its index" — it
EXPLAINs every hot query against seeded data and exits non-zero on a
sequential scan. Run it in CI; it is the only check that catches this class
of problem before production.

## Reporting a problem

Include the version (`pj -v`), the PostgreSQL version, the full `pj-admin
doctor` output, and — if it concerns one job — `pj-admin jobs history ID`
and `pj-admin jobs steps ID`. Those four answer most questions without a
round trip.
