# The admin surface

Three ways to drive a running platform, over one backend:

|                              | What it is                            | Best for                    |
| ---------------------------- | ------------------------------------- | --------------------------- |
| `pj-admin`                   | the operator CLI                      | everything below; scripting |
| `pj-web`                     | HTML admin + `/metrics`, no auth      | dashboards, browsing        |
| `pyjobby.admin_api.AdminAPI` | the Python API both of the above call | custom automation           |

This is the reference for _what exists_. What to do with it when something
is wrong is [OPERATIONS.md](OPERATIONS.md) (playbooks) and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) (symptoms). Every command here was
run against a live database; the outputs are real.

## Connecting

`pj-admin` takes a DSN or a config file, and a DSN wins:

```bash
pj-admin --dsn postgresql://user:pass@host:5432/pyjobby doctor
export PYJOBBY_DSN=postgresql://user:pass@host:5432/pyjobby   # same thing
pj-admin -c /etc/pyjobby/pyjobby.toml doctor               # config file
```

```console
$ pj-admin --help
Usage: pj-admin [OPTIONS] COMMAND [ARGS]...

  Pyjobby job queue management CLI

Options:
  -c, --config TEXT  Config file path
  --dsn TEXT         PostgreSQL DSN (overrides --config; also read from
                     PYJOBBY_DSN)
  --help             Show this message and exit.

Commands:
  dag       Manage DAGs (Directed Acyclic Graphs)
  db        Manage the database schema (install / migrate / status)
  dlq       Manage Dead Letter Queue
  doctor    Run health checks against the job platform (exit 1 on any FAIL)
  jobs      Manage jobs
  metrics   Show system metrics
  queues    Manage queues
  schedule  Manage recurring schedules
  workers   Manage workers
```

### Exit codes: scripts can rely on them

Operator-facing failures exit non-zero. A job that does not exist, a job in
the wrong state for the verb, an unusable config, an unreachable database,
or any `doctor` FAIL all exit 1:

```console
$ pj-admin jobs inspect 999999999
Error: Job 999999999 not found
$ echo $?
1
```

Arguments that were wrong before anything was attempted exit **2**, the
status click itself uses for a usage error — an unknown `--state`, a
malformed `--tag`, a limit that is not a number, a priority above the
worker ceiling:

```console
$ pj-admin jobs list --state bogus
Error: Unknown job state: 'bogus'
Error: Valid states: queued, claimed, running, waiting, finished, crashed, cancelled
$ echo $?
2
```

So `1` means "I tried and could not", `2` means "I did not try". An empty
_answer_ is not a failure either — `dlq list` on an empty DLQ, or `queues
show` for a queue with no jobs and no control row, exit 0.

Machine-readable output is `--json`, available on `jobs list`, `jobs
inspect`, `jobs why`, `jobs history`, `jobs steps`, `jobs retry-stats`,
`jobs timeout-stats`, `queues list`, `queues show`, `queues stats`, `workers
list`, `workers stats`, `dlq list`, `metrics`, `schedule list`, `schedule
show`, `schedule history`, `schedule stats`, `dag list`, `dag show`, `dag
visualize`, `doctor` and `db status`.

```bash
pj-admin jobs retry $(pj-admin jobs list --state crashed --json | jq -r '.[].id')
```

## `db` — schema install and upgrade

```console
$ pj-admin db --help
Commands:
  migrate  Install the base schema if missing, then apply pending migrations
  status   Show applied vs pending schema migrations
```

`db migrate` handles **both** histories, though only one exists today:

- **A fresh database** gets the base schema — the ordered files in
  `pyjobby/sql/schema/`, concatenated and executed in one transaction. No
  migration files ship (the base schema is the whole current schema), so
  nothing is applied or recorded and the install is complete by
  construction.
- **An existing database** gets any numbered files in
  `pyjobby/sql/migrations/` it has not recorded, oldest first, one
  transaction per file — the upgrade path that starts mattering once there
  are live deployments to upgrade. The first such file is minted at that
  point; until then every schema change lands in the base schema directly.

Same command either way, and it is idempotent. It is **safe to run from
every host's deploy step simultaneously**: `migrate()` takes a session-level
advisory lock, so one process installs or upgrades and the others wait and
then find nothing to do. (A first install is not idempotent statement by
statement — `CREATE TYPE jorbstate` has no `IF NOT EXISTS` — which is what
the lock is there for.)

`db status` on a database whose shape has drifted:

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

`Missing objects` is the load-bearing line. The version lines can only say
what this database _recorded_, and a drifted database records exactly what
a current one does; the missing list is read out of the catalog and
compared against the required-shape manifest in `pyjobby/migrations.py`. A
healthy database prints `Missing objects:       none`.

`db migrate` reports "applied" and "recorded" distinctly, because they are
different events: a fresh install prints `Installed base schema` (followed
by `Recorded migrations [...]` once any ship), an upgrade prints
`Applied migrations: [...]`, and a database already current prints
`Database schema is up to date`.

See
[deployment-guide.md § The database](deployment-guide.md#the-database) for
what it does and does not bring forward.

## `doctor` — the health entry point

```console
$ pj-admin doctor --help
Usage: pj-admin doctor [OPTIONS]

  Run health checks against the job platform (exit 1 on any FAIL)

  Checks: database reachability, schema/migrations, NOTIFY triggers, NOTIFY
  queue saturation, live workers, workers that are alive but claiming nothing,
  queue backlogs, jobs no live worker can claim, blocked waiters, unread mail,
  the DLQ, and overdue schedules.

  With --json the same checks come out as [{check, status, message}] and the
  exit code is unchanged, so a CI job can scrape them.

Options:
  --max-depth INTEGER        WARN when a queue's backlog exceeds this many
                             queued jobs
  --max-age-minutes INTEGER  WARN when a queue's oldest queued job is older
                             than this
  --json                     Output as JSON
  --help                     Show this message and exit.
```

A live platform, idle:

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
$ echo $?
0
```

### What the `schema` check actually checks

Presence-and-pending is not enough to certify a schema: a database
installed from an _older_ base schema has `jorb` and records nothing
pending, so both answers look healthy while the very next query dies on a
missing column.

So the check is the **shape**: every table, column, view, function, index
and enum label this release addresses, by name, read out of the catalog and
compared against the manifest in `pyjobby/migrations.py` (which the test
suite asserts equals a fresh install's catalog in both directions, so it
cannot rot). Three verdicts:

| Verdict | Line                                                                                                                                |
| ------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| FAIL    | `base schema not installed (run: pj-admin db migrate)`                                                                              |
| FAIL    | `installed, but N object(s) this release needs are missing: …` — up to five named, then a count                                     |
| PASS    | `installed, migrations current (…)`, or `installed and complete; migrations […] are not recorded yet, which the next upgrade reads` |

A stale schema, named:

```console
$ pj-admin doctor
PASS database: connected
FAIL schema: installed, but 3 object(s) this release needs are missing: index jorb_dag_retention_idx, index jorb_schedule_log_retention_idx, index jorb_worker_retention_idx (run: pj-admin db migrate)
$ echo $?
1
```

Either FAIL ends the report there — every check below `schema` queries
something it just reported missing, and a health report that crashes halfway
through is worse than one that stops. `pj-admin db status` prints the full
missing list.

A schema that is complete but has _unrecorded_ migrations — the third line
in that table — is a **PASS**, not a FAIL: the shape check just proved every
object those files install is already present, so the running code can
address this database and only the bookkeeping is behind. Waking someone at
3am over a missing row in `schema_migrations` is how a health probe teaches
people to ignore it. It is still said out loud, because that record is what
the _next_ upgrade reads: until `db migrate` runs, a later release cannot
tell this database from one that never applied the migration at all.

Triggers get their own check for the same reason the shape does: nothing
raises when one is missing, the platform just quietly stops waking waiters
or recording history. All seven the schema installs are checked by name.

### The `unclaimable` check — work the fleet cannot see

The fleet-wide sweep for the condition
[`jobs why`](#why-is-this-job-not-running) answers one job at a time: a job
that is `queued`, due, and that **no live worker on its queue could ever
claim**. Two ways in, and the check names which one, per queue:

| Cause                  | The line says                                                | Fix                                                                     |
| ---------------------- | ------------------------------------------------------------ | ----------------------------------------------------------------------- |
| `above_worker_ceiling` | `N on 'queue' above every live worker's ceiling (prio …)`     | `pj --max-prio` higher, or `pj-admin jobs set-priority ID N` lower       |
| `capability_unmet`     | `N on 'queue' needing capability 'x', which none … advertises` | start a worker with `pj --queue Q --cap x`                              |

```console
$ pj-admin doctor
...
WARN unclaimable: 4 claimable job(s) that no live worker can claim: 3 on 'reports' above every live worker's ceiling (prio 500; the highest --max-prio among 1 live worker(s) is 100; e.g. jobs 1, 2, 3); 1 on 'reports' needing capability 'gpu', which none of the 1 live worker(s) advertises (they advertise: cpu; e.g. jobs 4). they are runnable and INVISIBLE to every live worker on their queue, so nothing ever claims them: they stay queued forever, never fail, and never reach the DLQ. Raise the fleet's ceiling (pj --max-prio N), start a worker advertising the capability (pj --queue Q --cap C), or lower the job (pj-admin jobs set-priority ID N). Remember lower prio = more urgent. `pj-admin jobs why ID` explains any one of them in full
```

It is a **WARN**: the platform is healthy, the workload is not. Up to three
(queue, cause) groups are named and then summarised; counts past 1000 per
group are reported as `N+`, and a handful of example ids come with each so
`pj-admin jobs why ID` can be run on one of them.

A queue with **no live workers at all** is deliberately not reported here —
that is the `workers` check (and `no_live_workers` from `jobs why`), and it
is a different remedy: start a worker, rather than change what the running
ones accept. This check answers what neither of those can — the fleet is up
and the work is still invisible to it.

FAIL is otherwise reserved for "the platform cannot function" — a missing
trigger, a NOTIFY queue past half full — and is the only thing that changes
the exit code. Lost capacity is a WARN. Which check means what, and what to
do about each, is in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#start-with-doctor).

Defaults are `--max-depth 10000` and `--max-age-minutes 60`. Age is the
more honest alarm: a deep queue that is draining is fine, an old queue is
not.

## `jobs`

```console
$ pj-admin jobs --help
Commands:
  cancel         Cancel one or more jobs.
  delete         Delete one or more jobs (permanent!)
  history        Show a job's full transition trail (including per-attempt errors)
  inspect        Show detailed information about a job
  list           List jobs with optional filtering
  rerun          RE-RUN a terminal job — including a FINISHED one (repeats side effects)
  retry          Retry one or more crashed jobs
  retry-stats    Show retry statistics from the jorb_history audit trail
  set-priority   Change a queued or waiting job's priority.
  steps          Show a job's DXE step checkpoints
  timeout-stats  Show timeout statistics (from jorb.timeout_at/state)
  why            Explain, in one command, why a job is not running
```

### Finding jobs

```console
$ pj-admin jobs list --limit 5
ID     State     Queue          Job Class      Priority  Created
----------------------------------------------------------------------
19615  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01
19616  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01
19612  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01
19609  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01
19617  finished  pjbench_e2e_6  pyjobby.bench  100       2026-07-27T01

Showing 5 job(s). Use --limit and --offset for pagination.
```

Filters: `-q/--queue`, `-s/--state`, `--job-class` (patterns), `--uid`,
`--tag KEY=VALUE` (repeat for AND), `-l/--limit`, `-o/--offset`, `--json`.
The table truncates long values for width — use `--json` when you need the
full queue name or class path.

`--tag` matches jobs _containing_ the pair, so extra tags on the job are
fine, and values are read as JSON when they look like it: `batch=7` matches
the number 7, `batch='"7"'` the string.

### One job, end to end

```console
$ pj-admin jobs inspect 48148

Job 48148 Details
--------------------------------------------------
State:           finished
Queue:           pjbench_e2e_3057f4d4
Job Class:       pyjobby.bench.BenchJob
Priority:        100
Created:         2026-07-27T01:34:31.958272+00:00
Updated:         2026-07-27T01:34:40.001308+00:00
Run After:       2026-07-27T01:34:31.958272+00:00
Run Count:       1
Error Count:     0
Worker:          optionality.local:79115

Arguments:
{
  "n": 17748
}

Result:
{
  "n": 17748
}
```

```console
$ pj-admin jobs history 48148

Job 48148 History
At                   Event     From     Epoch  Errors  Worker                Error
----------------------------------------------------------------------------------
2026-07-27T01:34:31  enqueued  -        -      -       -
2026-07-27T01:34:39  claimed   queued   1      0       optionality.local:79
2026-07-27T01:34:40  running   claimed  1      0       optionality.local:79
2026-07-27T01:34:40  finished  running  1      0       optionality.local:79

Total: 4 transition(s)
```

A job keeps **one row for life**: retries requeue the same id, and `history`
is where the per-attempt trail (and each attempt's error) lives. `Epoch` is
the fencing token, not an attempt counter.

```console
$ pj-admin jobs steps 48148
No step checkpoints for job 48148
```

`steps` shows the DXE checkpoints of a durable job — what completed, so
what a resume will skip. See [DXE.md](DXE.md).

### Why is this job not running?

`jobs why ID` answers it in one command. The answers come from the claim
path itself — `claim_jorb()`, the function every worker calls — so they
are exactly the ways a job can fail to be claimed, with the numbers behind
each one.

```console
$ pj-admin jobs why 48821

Job 48821: above_worker_ceiling
queued  reports  myapp.jobs.NightlyRollup  prio 5000
--------------------------------------------------
This job's prio is 5000, above the ceiling of EVERY live worker on 'reports' (highest is 1000) — claim_jorb admits only prio <= the claiming worker's --max-prio, so every one of the 1 live worker(s) is blind to it. Lower the job (`pj-admin jobs set-priority`) or run a worker with a higher --max-prio. Remember lower prio = more urgent.

    prio:                            5000
    max_live_ceiling:                1000
    live_workers:                    1
    workers_at_or_above_prio:        0
```

That one is the reason the verb exists: the row is healthy, the fleet is
healthy, and every live worker is blind to it. Nothing fails, so nothing
reaches the DLQ. It is answerable at all because a worker publishes its
ceiling (`jorb_worker.max_prio`) when it registers, next to its
capabilities.

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

$ pj-admin jobs why 48831

Job 48831: waiting_on_job
waiting  reports  myapp.jobs.Load  prio 100
--------------------------------------------------
Waiting on job 48830 — that job is crashed, and the wake fires only on 'finished', so this one will never start on its own.

    blocking_job_id:                 48830
    blocking_job_state:              crashed
    blocking_job_class:              myapp.jobs.Extract
    waiting_seconds:                 46.892624

$ pj-admin jobs why 48842

Job 48842: claimable
queued  reports  myapp.jobs.Email  prio 100
--------------------------------------------------
Nothing is blocking it: this job is claimable RIGHT NOW on 'reports' (1 live worker(s)), and 3 claimable job(s) sort ahead of it. If it is still sitting here, the fleet is behind — check `pj-admin metrics` and `pj-admin doctor`.

    queue:                           reports
    live_workers:                    1
    jobs_ahead:                      3
    jobs_ahead_capped:               False
    queue_serialised:                False
```

The `reason` is a stable machine-readable code; the summary and the
indented block below it are the same answer for a human. Every reason:

| Reason                               | What it means                                                                                                                                 |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `finished` / `crashed` / `cancelled` | Terminal. `crashed` carries the error; both it and `cancelled` name `jobs retry`.                                                             |
| `claimed` / `running`                | A worker has it — which one (host, pid), since when, its deadline, and **whether that worker is still heartbeating**.                         |
| `waiting_on_job`                     | Parked on job N, with N's state. Both dependency wakes fire only on `finished`, so a crashed or missing upstream is reported as never-waking. |
| `waiting_on_group`                   | Parked on a group, with the count of members not yet finished.                                                                                |
| `waiting_unblocked`                  | `waiting` with no dependency recorded: nothing anywhere can wake it.                                                                          |
| `deferred`                           | `run_after` is in the future, and by how long. Retry backoff, `enqueue_at` and durable sleep all look like this, and none of them is a fault. |
| `queue_paused`                       | The queue is paused; claims stop before any row is looked at.                                                                                 |
| `no_live_workers`                    | Nothing is on that queue — read the queue, not the fleet total.                                                                               |
| `above_worker_ceiling`               | The job's `prio` is above every live worker's `--max-prio`.                                                                                   |
| `capability_unmet`                   | No live worker on the queue advertises the capability, and the answer names what they _do_ advertise.                                         |
| `queue_at_max_concurrency`           | In-flight count against the cap.                                                                                                              |
| `rate_limited`                       | Admissions in the window against the limit.                                                                                                   |
| `claimable`                          | Nothing declines it: how many claimable jobs sort ahead of it in claim order (`prio`, then `run_after`).                                      |

`--json` emits the whole structure, which is what a monitoring script
wants — `reason` is the field to branch on:

```console
$ pj-admin jobs why 48821 --json
{
  "job_id": 48821,
  "state": "queued",
  "queue": "reports",
  "job_class": "myapp.jobs.NightlyRollup",
  "prio": 5000,
  "capability": null,
  "run_after": "2026-07-28T23:25:02.825604+00:00",
  "created": "2026-07-28T23:25:02.825604+00:00",
  "updated": "2026-07-28T23:25:02.825604+00:00",
  "reason": "above_worker_ceiling",
  "summary": "This job's prio is 5000, above the ceiling of EVERY live worker on 'reports' ...",
  "details": {
    "prio": 5000,
    "max_live_ceiling": 1000,
    "live_workers": 1,
    "workers_at_or_above_prio": 0
  }
}

$ pj-admin jobs why 99999
Error: Job 99999 not found
$ echo $?
1
```

Two ways `claim_jorb` declines that are deliberately **not** reasons: a
claimer losing the 50 ms queue lock, and `SKIP LOCKED` passing over a row
another claimer holds. Both mean the queue is being drained by somebody
else this millisecond, not that this job is blocked — so a claimable job on
a queue with a concurrency cap or rate limit reports `queue_serialised:
True` instead, which is why it can take a poll longer than one on an
uncontrolled queue.

Every query behind the verb is bounded (a primary-key lookup, an aggregate,
or a `LIMIT`), and all of them use indexes the schema already keeps — it is
safe to run against a table with hundreds of millions of rows, and safe to
run from a monitoring loop.

This verb needs a job id, which means somebody already suspects that job.
The same two fleet-shaped reasons — `above_worker_ceiling` and
`capability_unmet` — are swept for across every queue by
[`doctor`'s `unclaimable` check](#the-unclaimable-check--work-the-fleet-cannot-see),
which is how those jobs get found in the first place.

### Acting on jobs

```console
$ pj-admin jobs rerun --help
Usage: pj-admin jobs rerun [OPTIONS] JOB_ID

  RE-RUN a terminal job — including a FINISHED one (repeats side effects)

  By default the run is fresh: DXE checkpoints are wiped and the job re-
  executes from step 1, which is what "run it again" means. Pass --resume to
  keep the checkpoints instead — completed steps fast-forward and execution
  continues where it left off, which is how an interrupted durable job is
  resumed.

  `jobs retry` is the verb for jobs that did NOT succeed; it refuses finished
  jobs precisely because re-running them repeats their effects.

Options:
  --resume  Keep DXE checkpoints: completed steps fast-forward instead of
            re-executing
```

- `jobs retry ID...` — for jobs that did **not** succeed (`crashed` or
  `cancelled`). Refuses anything else.
- `jobs rerun ID` — also accepts a **finished** job. Running successful
  work again repeats its side effects, which is why it is a separate verb.
- `jobs cancel ID...` — queued and waiting jobs are cancelled immediately;
  a claimed or running job gets a cancellation _request_ delivered to its
  worker, reported distinctly, because a job whose worker has died stays
  running with only the request recorded.
- `jobs set-priority ID PRIORITY` — re-prioritise a **queued or waiting**
  job (lower numbers are claimed first). Once a job is claimed its priority
  no longer decides anything, so those are refused; a priority above the
  deployment's worker ceiling is refused too, since no worker would claim
  it. The ceiling comes from the config file's `prio_ceiling`, and
  `--max-prio N` overrides it for one command.
- `jobs delete ID...` — permanent, one line per id, prompts once for the
  whole list unless `-f/--force`.

The retry-versus-rerun distinction is expanded in
[OPERATIONS.md § Retry vs. re-run](OPERATIONS.md#retry-vs-re-run).

### Aggregates

```console
$ pj-admin jobs retry-stats

Retry Statistics (last 24h)
------------------------------------------------------------
No retried jobs found

$ pj-admin jobs timeout-stats

Timeout Statistics (last 24h)
------------------------------------------------------------
No timeout data found
```

Both take `-q/--queue`, `--since-hours` and `--json`. `retry-stats` reads
`jorb_history` — an attempt is a `running` event, so a job with more than
one was retried.

## `queues`

```console
$ pj-admin queues --help
Commands:
  clear   Clear (delete) jobs from a queue
  limits  Set (or show, with no options) a queue's concurrency/rate limits
  list    List all queues with their pause/limit controls
  pause   Pause a queue (workers stop claiming from it immediately)
  resume  Resume a paused queue
  show    Show one queue's controls and statistics
  stats   Show queue statistics
```

Controls are live — they are rows in `jorb_queue`, read by the claim
statement, so a change takes effect on the next claim attempt:

```console
$ pj-admin queues pause maintenance
Queue 'maintenance' paused

$ pj-admin queues limits maintenance --max-concurrency 8 --rate-limit 100 --rate-period 60
Queue 'maintenance' limits updated
Paused:              yes
Max concurrency:     8 (claimed+running cap; '-' = unlimited)
Rate limit:          100 start(s) per 60s ('-' = unlimited)

$ pj-admin queues show maintenance

Queue 'maintenance'
--------------------------------------------------
Paused:              yes
Max concurrency:     8 (claimed+running cap; '-' = unlimited)
Rate limit:          100 start(s) per 60s ('-' = unlimited)

Depths:
  queued       0
  scheduled    0
  claimed      0
  running      0
  waiting      0
  finished     0
  crashed      0
  cancelled    0
  total        0

$ pj-admin queues resume maintenance
Queue 'maintenance' resumed
```

`queues limits NAME --max-concurrency none` clears a limit. `queues list`
and `queues stats` are the fleet-wide views:

```console
$ pj-admin queues stats
Queue        Paused  Queued  Scheduled  Claimed  Running  Waiting  Finished  Crashed  Cancelled  Total  Limits
-----------------------------------------------------------------------------------------------------------------------
maintenance  no      0       0          0        0        0        0         0        0          0      conc=8, rate=100/60s
```

Every state the `Total` sums has a column, so the row adds up: `Queued` is
work claimable **now** and `Scheduled` is queued-but-deferred, the same split
`/metrics` and the websocket dashboard report.

`queues clear QUEUE` deletes **queued and waiting** jobs — work that has not
started — and prompts unless `-f/--force`, naming the states it is about to
hit. `-s/--state` narrows it to one state instead, and is the only way to
reach a claimed or running job: deleting one of those does not stop its
worker, it strands the run. `--not-updated-for-days N` restricts the sweep
to rows quiesced that long. It is a blunt instrument for a test queue —
routine cleanup is retention's job
(see [deployment-guide.md § Retention](deployment-guide.md#retention--on-by-default)).

## `workers`

```console
$ pj-admin workers list
ID  Host             PID    Queue            Status  Threads  Last Seen  Current Job
------------------------------------------------------------------------------------
1   optionality.loc  75674  pjbench_e2e_9c5  live    0/8      5s ago     -
2   optionality.loc  75675  pjbench_e2e_9c5  live    0/8      5s ago     -
3   optionality.loc  75673  pjbench_e2e_9c5  live    0/8      5s ago     -
4   optionality.loc  75676  pjbench_e2e_9c5  live    0/8      5s ago     -
```

This reads the `jorb_worker` registry: live workers plus recently
shut-down ones, each with the job it currently holds. Two columns carry the
important signal:

- **Status** — `live`, or **`not claiming`** for a worker that heartbeats
  perfectly and does no work, because abandoned job threads fill its pool:

  ```
  2   host-b    9910   heavy  not claiming  8/8      3s ago     -
  ```

- **Threads** — `abandoned/pool`. `8/8` _is_ the refusing state; `7/8` is
  one timed-out job away from it and reads nothing like `0/8`.

Both are explained in
[OPERATIONS.md § Abandoned job threads](OPERATIONS.md#abandoned-job-threads-when-a-worker-stops-claiming-on-purpose).

```console
$ pj-admin workers stats

Worker Statistics
--------------------------------------------------
Live workers:      4
Stale workers:     0
Shut down:         0
Total registered:  4

Live Workers by Queue:
Queue                 Live Workers
----------------------------------
pjbench_e2e_67f5e7c6  4
```

## `dlq` — the dead letter queue

The DLQ is not a separate table or an error-count heuristic: it is exactly
`state = 'crashed'`, the terminal state a job reaches when its retries are
exhausted.

```console
$ pj-admin dlq list
Dead Letter Queue is empty!
```

`dlq list` takes `--limit` and `--json`. `dlq retry ID` requeues the same
row with a fresh error budget — the operator-driven re-run, as against
`jobs retry`, which is the ordinary one. Triage a dead-lettered job with
`jobs history ID` (the per-attempt errors) and `jobs steps ID` (where a
durable pipeline stopped).

<a id="schedule-management"></a>

## `schedule` — schedule management

```console
$ pj-admin schedule --help
Commands:
  add      Create new recurring schedule
  delete   Delete a recurring schedule
  disable  Disable an enabled schedule
  enable   Enable a disabled schedule
  history  Show schedule execution history
  list     List recurring schedules
  show     Show schedule details
  stats    Show execution statistics for all schedules
```

```console
$ pj-admin schedule add nightly-cleanup examples.jobs.example_jobs.BasicJob "0 2 * * *" \
    --queue maintenance --kwargs '{"message":"cleanup"}'
✓ Schedule created: nightly-cleanup (ID: 1)
  Next run: 2026-07-27 02:00:00+00:00
  Cron:     0 2 * * *
  Queue:    maintenance
```

`add` also takes `-p/--priority`, `--max-prio`, `--capability`,
`--timezone`, `--jitter`, `--max-concurrent`, `--backpressure`,
`--circuit-breaker`, `--description` and `--disabled`. `--max-prio N`
overrides the deployment's worker priority ceiling for this one command,
exactly as it does for `jobs set-priority`: a schedule's priority is checked
against the ceiling when the schedule is created, because a schedule that
fires into an unclaimable priority is worse than one that never existed. The
cron expression and timezone are validated at this point too, not at fire
time.

```console
$ pj-admin schedule show nightly-cleanup

Schedule: nightly-cleanup
------------------------------------------------------------
ID:                    1
Enabled:               ✓ Yes
Description:           -

Schedule:
Cron Expression:       0 2 * * *
Timezone:              UTC
Next Run:              2026-07-27 02:00:00+00:00

Job Configuration:
Job Class:             examples.jobs.example_jobs.BasicJob
Queue:                 maintenance
Priority:              100
Capability:            -
Arguments:             {"message": "cleanup"}

Safety Features:
Max Concurrent Jobs:   1
Jitter (seconds):      0
Backpressure Threshold:1000
Circuit Breaker:       5 failures

Statistics:
Total Runs:            0
Successes:             0
Failures:              0
Skips:                 0
Consecutive Failures:  0
Last Run:              Never
Last Success:          Never
Last Failure:          Never
```

`schedule history NAME_OR_ID` takes `--result success|failure|skipped`,
`-l/--limit` and `--json`; `schedule stats` is the fleet view. `schedule
delete NAME_OR_ID` prompts unless `-f/--force`, like every other destructive
verb here. Prefer `disable` to `delete` either way: it keeps the execution
log.

Everything a schedule _means_ — cron syntax, jitter, backpressure, the
circuit breaker, timezone handling — is in
[RECURRING_SCHEDULER.md](RECURRING_SCHEDULER.md). Nothing fires unless
`pj-scheduler` is running; `doctor` warns about schedules overdue by more
than five minutes for exactly that reason.

## `dag`

```console
$ pj-admin dag --help
Commands:
  list       List DAGs
  show       Show DAG details and job status
  visualize  Visualize DAG structure (ASCII art)
```

`list` takes `-l/--limit` and `--json`.

## `metrics`

```console
$ pj-admin metrics

System Metrics (last 24h)
--------------------------------------------------
Throughput:        0.35 jobs/s (completed)
Arrivals:          0.35 jobs/s (created)
Balance:           +0.00 jobs/s (keeping up)
Retry Pressure:    0.00 attempts/s
DLQ Growth:        0.0000 jobs/s
Finished:          30000
Crashed:           0
Cancelled:         0
Avg Duration:      0.00s
Avg Queue Wait:    7.00s
Max Queue Wait:    13.09s
Backlog:           0 claimable, oldest ready 0s
In Flight:         0 (0 stuck > 5.0m, oldest 0s)
NOTIFY Queue:      0.0% used
Dead Tuples:       79.8% of jorb

Storage:
  jorb              17.2MB total (10.6MB table + 6.5MB index), dead 79.8%
  jorb_history      35.3MB total (26.6MB table + 8.8MB index), dead 1.3%
  jorb_step         16.0KB total (8.0KB table + 8.0KB index), dead 0.0%

Jobs Created in Window, by State:
  finished     30000
```

Takes `-q/--queue`, `--since-hours` (default 24) and `--json`. Two kinds of
number live here and must not be confused: **rates** (`jobs/s`) are
measured over the window and comparable across window sizes; **levels**
(backlog, in flight, storage, NOTIFY usage) are instants.

- **Throughput vs Arrivals** is the only pair that can say "falling
  behind". Sustained arrivals above completions is the definition, and no
  single number expresses it.
- **Queue Wait vs Duration** are reported separately on purpose. Rising
  wait with flat duration is a capacity problem; rising duration is a code
  or dependency problem. A blended "how long did the job take" cannot tell
  those apart, so it is not reported. See
  [OPERATIONS.md § Reading the latency numbers](OPERATIONS.md#reading-the-latency-numbers).
- **Dead Tuples** is a survival question at rate — it answers "is
  autovacuum keeping up" and nothing else does. See
  [SCALE.md § Vacuum pressure](SCALE.md#4-vacuum-pressure).

## `pj-web` — the HTML admin

```bash
pj-web --config /etc/pyjobby/pyjobby.toml --host 127.0.0.1 --port 8081
```

It takes a config file with `-c`/`--config` (the same flag every pyjobby
daemon takes); it does not read `PYJOBBY_DSN`.
Defaults are `127.0.0.1:8081`. **There is no authentication**, and the API
can cancel, retry and delete jobs — bind it to localhost, or put an
authenticating proxy in front. `--host 0.0.0.0` exposes an unauthenticated
control plane. This surface must never be reachable from an untrusted
network: creating a schedule names a `job_class` that every worker then
imports, so write access here is as privileged as the worker processes
themselves (the API checks that the value is shaped like a dotted import
path, which bounds typos and probes, not intent).

Mutating requests (everything except `GET`/`HEAD`) are refused with `403`
when a browser says they came from another site — `Sec-Fetch-Site` other
than `same-origin`/`none`, or an `Origin` naming a different host. Requests
with neither header (curl, deploy scripts) are allowed, so scripting the API
is unchanged; the check exists because a form-encoded `POST` is a CORS
simple request, which means any page an operator visits could otherwise
submit to `127.0.0.1:8081` on their behalf.

Pages: `/`, `/jobs`, `/queues`, `/workers`, `/dlq`, `/schedules`.

Listing endpoints take `limit` (max 1000; over that is a `400`, never a
silent clamp) and `/api/metrics` takes `since_hours` (max 90 days).

JSON API:

| Method | Path                                                                            |
| ------ | ------------------------------------------------------------------------------- |
| GET    | `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/history`, `/api/jobs/{id}/steps` |
| POST   | `/api/jobs/{id}/retry`, `/api/jobs/{id}/cancel`                                 |
| DELETE | `/api/jobs/{id}`                                                                |
| GET    | `/api/queues`, `/api/queues/{queue}/stats`                                      |
| POST   | `/api/queues/{queue}/pause`, `/api/queues/{queue}/resume`                       |
| GET    | `/api/workers`, `/api/workers/stats`                                            |
| GET    | `/api/dlq`                                                                      |
| POST   | `/api/dlq/{id}/retry`                                                           |
| GET    | `/api/metrics`                                                                  |
| GET    | `/api/schedules`, `/api/schedules/{id}`, `/api/schedules/{id}/history`          |
| POST   | `/api/schedules`, `/api/schedules/{id}/enable`, `/api/schedules/{id}/disable`   |
| DELETE | `/api/schedules/{id}`                                                           |

`GET /metrics` (not `/api/metrics`) is the Prometheus scrape endpoint. The
gauges and counters it exposes:

```
pyjobby_jobs_by_state              pyjobby_jobs_enqueued_total
pyjobby_backlog_depth              pyjobby_queue_oldest_queued_seconds
pyjobby_queue_paused               pyjobby_jobs_inflight
pyjobby_jobs_stuck                 pyjobby_inflight_oldest_age_seconds
pyjobby_jobs_started_recent        pyjobby_jobs_terminal_recent
pyjobby_job_duration_seconds       pyjobby_throughput_jobs_per_second
pyjobby_arrival_jobs_per_second    pyjobby_retry_attempts_per_second
pyjobby_dlq_jobs_per_second        pyjobby_notify_queue_usage_ratio
pyjobby_workers_live               pyjobby_workers_not_claiming
pyjobby_worker_job_threads_abandoned_max
pyjobby_table_bytes                pyjobby_table_index_bytes
pyjobby_table_total_bytes          pyjobby_table_live_tuples
pyjobby_table_dead_tuples          pyjobby_table_dead_tuple_ratio
```

The three worth alerting on first are `pyjobby_notify_queue_usage_ratio`,
`pyjobby_queue_oldest_queued_seconds` (age, not depth) and
`pyjobby_workers_not_claiming`.

For the realtime feed — `pj-ws`, its protocol and its dashboard — see
[WEBSOCKET_DASHBOARD.md](WEBSOCKET_DASHBOARD.md). It is unauthenticated
too, and its actions include cancelling and re-prioritising jobs.

## `AdminAPI` — the Python API

Everything above is a thin shell over `pyjobby.admin_api.AdminAPI`, which
takes an asyncpg connection:

```python
import asyncpg
from pyjobby.admin_api import AdminAPI

conn = await asyncpg.connect(dsn)
api = AdminAPI(conn)

crashed = await api.list_jobs(state="crashed", limit=10)
for job in crashed:
    await api.retry_job(job["id"])  # {"job_id": ..., "status": "requeued"}
```

Method groups, all `async`:

- **Jobs** — `list_jobs`, `get_job`, `get_job_history`, `get_job_steps`,
  `retry_job`, `retry_jobs`, `rerun_job`, `cancel_job`, `cancel_jobs`,
  `update_job_priority`, `delete_job`, `delete_jobs`.
- **Queues** — `list_queues`, `queue_stats`, `clear_queue`,
  `list_queue_controls`, `get_queue_control`, `set_queue_control`,
  `pause_queue`, `resume_queue`.
- **Workers** — `list_workers`, `worker_stats`, `job_thread_stats`.
- **Health and metrics** — `get_metrics`, `backlog_stats`,
  `inflight_stats`, `storage_stats`, `notify_queue_usage`.
- **DLQ** — `list_dlq`, `retry_from_dlq`.
- **Schedules** — `list_schedules`, `get_schedule`, `create_schedule`,
  `update_schedule`, `delete_schedule`, `enable_schedule`,
  `disable_schedule`, `get_schedule_history`, `get_schedule_stats`.

Three shapes are worth stating because they are easy to assume wrong:

- `retry_job` and `retry_from_dlq` return the **same** `job_id` they were
  given. A retry requeues the row a job has had since it was enqueued;
  there is no new id to follow.
- `cancel_job`, `retry_job`, `rerun_job` and `retry_from_dlq` always return
  at least `{"job_id", "status"}` and never raise for a job they will not
  touch: a missing job and a job in a refusing state are the same
  `'not_cancellable'` / `'not_retriable'` / `'not_rerunnable'` answer.
  `rerun_job` adds a third key, `fresh`, echoing which mode was asked for —
  "restart from step 1" (the default) and "resume from checkpoints" are
  opposite answers to the same verb, so the reply says which one happened.
  The bulk forms (`cancel_jobs`, `retry_jobs`) return one such dict per id,
  in order.
- `list_dlq()` is `state = 'crashed'`, ordered by `updated`. It is not
  filtered on an error count.

The docstrings in `pyjobby/admin_api.py` are the full reference, including
the exact keys each method returns.
