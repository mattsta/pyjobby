# Deploying pyjobby

What to install, what to run, and what each process needs. Once it is
running, [OPERATIONS.md](OPERATIONS.md) is the runbook and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) is the symptom index. Capacity and
tuning evidence is in [SCALE.md](SCALE.md); this document does not repeat
its numbers.

## Install

```bash
pip install git+https://github.com/mattsta/pyjobby.git#main
```

Python 3.14 or newer, PostgreSQL, and nothing else — no broker, no cache, no
sidecar. The wheel carries the base schema (`pyjobby/sql/schema/`), so the database is
installed by the package rather than by a file you copy around.

Installing puts seven commands on `PATH`:

| Command | Role |
|---|---|
| `pj` | worker processes: claim and execute jobs |
| `pj-monitor` | the reaper: timeouts, dead-worker reclaim, retention |
| `pj-scheduler` | fires cron schedules |
| `pj-web` | HTML admin + `/metrics` |
| `pj-ws` | realtime dashboard feed |
| `pj-admin` | operator CLI (see [ADMIN_TOOLS.md](ADMIN_TOOLS.md)) |
| `pj-bench` | benchmark and plan-regression harness |

`pj -v` prints the version.

## The database

One command installs the schema, and the same command will apply migrations
once those exist. It is the only supported way to do either:

```console
$ pj-admin --dsn "$PYJOBBY_DSN" db migrate
Installed base schema
```

```console
$ pj-admin --dsn "$PYJOBBY_DSN" db status
Base schema installed: yes
Applied migrations:    none
Pending migrations:    none
Missing objects:       none
```

**A fresh database** gets the base schema — the ordered files in
`pyjobby/sql/schema/`, concatenated and executed in one transaction. No
migration files ship: the base schema **is** the whole current schema, so a
fresh install is always complete and there is nothing to apply or record.

**Migrations arrive with live deployments.** The runner already handles them
— an existing database gets any numbered files from
`pyjobby/sql/migrations/` it has not recorded in `schema_migrations`, oldest
first, one transaction each, and a fresh install records them without
running them — but the first such file is minted only when there is a live
database to upgrade. Until then every schema change lands in the base schema
directly.

It is idempotent, so running it on every deploy is the intended usage, and it
takes a PostgreSQL advisory lock for the duration — two hosts running their
deploy step at the same instant serialise, one does the work and the other
waits and then finds nothing to do. Prefer running it from **one** place, as
a deploy step or an init container, rather than from every worker's startup:
concurrency is safe, but a hundred workers queued behind one lock is a slow
rollout.

**Verify before you cut traffic over.** `pj-admin doctor`'s schema check reads
the catalog and compares it against every object this release needs, so it
FAILs by name on a database that is present but stale, and `db status` lists
the same objects:

```console
$ pj-admin --dsn "$PYJOBBY_DSN" doctor
PASS database: connected
FAIL schema: installed, but 19 object(s) this release needs are missing: column jorb.awaited, column jorb.claimed_at, column jorb.tags, column jorb_worker.idle, column jorb_worker.job_threads, and 14 more (run: pj-admin db migrate)
```

Doctor stops there rather than continuing into checks that would fail on the
columns it has just reported missing. Every other `pj-admin` command reports
the same condition in one line, naming the same remedy, instead of a
traceback:

```console
$ pj-admin --dsn "$PYJOBBY_DSN" workers list
Error: The database schema is missing or out of date: column w.job_threads does not exist
Error: Install or upgrade it with `pj-admin db migrate`, then confirm with `pj-admin doctor`.
```

One case reports PASS while still asking for `db migrate` (once migration
files exist): a database whose objects are all present but whose
`schema_migrations` rows are not. It runs the current code correctly — hence
PASS — but the record is what the *next* upgrade reads, so run `db migrate`
once to write it.

Do not hand-write DDL, and do not load the schema from a copy checked into
your own repository. The schema is one file, it is versioned with the code
that queries it, and the test suite reinstalls it whenever its content hash
changes — a hand-maintained duplicate is a database the platform has never
been tested against.

The schema also sets its own autovacuum thresholds and fillfactor on `jorb`
and `jorb_history`. That is part of the install, not a step you perform: see
[SCALE.md § Vacuum pressure](SCALE.md#4-vacuum-pressure). If you customise
those tables, verify the settings survived.

## Configuration

Every process reaches the database one of two ways.

**A config file** — a TOML file defining `db_params`:

```toml
# /etc/pyjobby/pyjobby.toml
prio_ceiling = 1000  # must match what your `pj` workers claim under

[db_params]
database = "pyjobby"
user = "pyjobby"
password = "${PYJOBBY_DB_PASSWORD}"
host = "postgres.internal"   # or a directory for a unix socket
port = 5432
command_timeout = 60         # optional
```

The file is **data, never code**: it is parsed with the standard library's
`tomllib` and nothing in it can execute. Secrets come from the environment
explicitly — any string value of the exact form `"${VAR_NAME}"` is replaced
with that environment variable at load time, and a reference to an unset
variable is a loud startup error naming the variable. (A `.py` config is
refused by name: an executable config format means every daemon runs
arbitrary code from whatever path it is pointed at.)

`prio_ceiling` (an int) is the worker fleet's priority ceiling, read by
`pj`, `pj-scheduler`, `pj-web` and `pj-ws` when their `--max-prio` flag is
not given — one declaration instead of the same number repeated on four
command lines. An explicit flag always wins.

`db_params` is **`asyncpg.connect()` keyword arguments**, and only those.
Do not put `min_size` or `max_size` in it: workers, the scheduler,
`pj-admin` and `pj-bench` open a plain connection and will reject the
unknown argument, and `pj-web` and `pj-ws` supply their own pool sizes
alongside your dict and will reject the duplicate. Pool sizing is not
configurable from here.

**A DSN** — `--dsn`, or the `PYJOBBY_DSN` environment variable:

```bash
export PYJOBBY_DSN="postgresql://user:password@host:5432/pyjobby"
```

Which process accepts which is not uniform, and it decides how you package
your deployment:

| Process | Config file | `--dsn` / `PYJOBBY_DSN` |
|---|---|---|
| `pj` | `-c`, default `./pyjobby.toml` | no |
| `pj-scheduler` | `-c`, default `./pyjobby.toml` | no |
| `pj-web` | `-c/--config`, default `./pyjobby.toml` | no |
| `pj-ws` | `-c/--config`, default `./pyjobby.toml` | no |
| `pj-monitor` | `--config` | yes |
| `pj-admin` | `-c/--config` | yes (wins over `--config`) |
| `pj-bench` | `-c/--config` | yes (wins over `--config`) |

So workers, the scheduler and the two web surfaces need a config file on
disk. A container image that ships no config must mount one.

Every entry point exits non-zero when it cannot resolve or load its
configuration, so a supervisor's restart-on-failure and a deploy script's
`set -e` both work:

```console
$ pj-admin -c /nonexistent.toml doctor
Error: Could not load config file: /nonexistent.toml
Error: '/nonexistent.toml' doesn't exist
Error: Use --config to point at a pyjobby conf file, or --dsn to connect directly.
FAIL config: unusable
$ echo $?
1
```

## The processes to run

| Process | Count | Required? |
|---|---|---|
| `pj` | per queue and host as needed | yes — nothing executes without it |
| `pj-monitor` | 1 (more are safe) | **yes** |
| `pj-scheduler` | 1 (more are safe) | only if you use cron schedules |
| `pj-web` | 0 or 1 | optional |
| `pj-ws` | 0 or 1 | optional |

Start order does not matter: every process connects independently and
reconnects with backoff if the database goes away.

### `pj-monitor` is not optional

It is the only thing that recovers work. Without it:

* a job whose worker host died stays `claimed` forever — nothing else
  requeues it;
* a job that blew its timeout in a way the worker could not interrupt (a
  synchronous task, a killed process) stays `running` forever;
* every terminal job, its history, its events, its mailbox and its
  checkpoints accumulate without limit, because retention lives here.

The worker enforces timeouts in-process as well, but only for the failures
it survives. `pj-monitor` is the backstop for the rest, and it is a single
process for the whole install — not one per host.

```console
$ pj-monitor --config /etc/pyjobby/pyjobby.toml
Starting monitor (check every 10.0s)...
DSN: localhost:5432/pyjobby
Retention: jobs older than 30.0d, checkpoints 1.0d after the job terminates
Monitor started (interval 10.0s, liveness grace 60.0s, job retention 30.0d, checkpoint retention 1.0d)
```

It states its whole policy on startup, so a misconfigured retention window
is visible in the first four lines of the log rather than in a table six
months later.

Several instances are safe — every sweep is a single atomic statement or a
transaction holding its own row locks.

## Production settings that matter

### Retention — on by default

`pj-monitor` deletes terminal jobs older than `--retention-days` (**30**)
and the DXE checkpoints of terminal jobs older than
`--checkpoint-retention-days` (**1**). `0` on either means keep forever.
Deleting a job takes its history, events, mailbox, checkpoints and DAG
edges with it by `ON DELETE CASCADE`; the same window also reaps orphaned
DAGs, the schedule log, retired worker registry rows and consumed mailbox
messages.

The defaults are on so that an install nobody revisits does not grow
forever — they are not a guess at your storage budget. Set them
deliberately. The design argument for the two independent windows is in
[ARCHITECTURE.md § Retention](ARCHITECTURE.md#retention).

Two knobs control how hard it works:

* `--retention-batch-size` (1000) — rows per delete batch.
* `--retention-max-seconds` (5.0) — time budget per sweep per cycle. A
  sweep keeps taking batches until it is caught up or the budget runs out,
  so it can catch up on a busy install without ever delaying timeout
  enforcement or dead-worker recovery.

Falling behind is reported at WARNING; see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#retention-is-falling-behind).

### Worker settings

```bash
pj --config /etc/pyjobby/pyjobby.toml \
   --queue reports --workers 4 \
   --max-retries 10 --default-timeout 3600 --job-threads 8
```

**`--workers` is per queue.** That command starts four workers on `reports`
and nothing anywhere else. Naming a second queue adds four more on it —
`--queue reports --queue exports --workers 4` is eight processes, four on
each — so adding a queue never changes the capacity of the queues you already
named.

| Flag | Default | What it decides |
|---|---|---|
| `--queue` | `default` | a queue to staff; repeatable, duplicates collapse |
| `--cap` | none | capabilities this host advertises; repeatable |
| `--workers` | CPU count / 2 | worker processes **per queue** |
| `--max-prio` | 1000 | priority ceiling; jobs above it are not claimed |
| `--max-retries` | 10 | attempts before a job is dead-lettered (`crashed`) |
| `--default-timeout` | 3600 | fallback job timeout in seconds; `0` disables |
| `--check-interval` | 5.0 | idle poll interval; LISTEN/NOTIFY wakes workers sooner |
| `--job-threads` | 8 | this worker's own job-thread pool |
| `--path` | `.` | extra import paths for job classes; repeatable |
| `--reload` | off | re-import a job module when its source changes |

Leave `--reload` off in production: on it re-executes module code on every
job.

`--job-threads` is a production setting, not a performance knob. Every
job's `run()` executes in a thread from this pool, and a synchronous job
that blows its deadline leaves its thread running forever — nothing can
stop it. When abandoned threads fill the pool the worker stops claiming and
says so, while still heartbeating and still counting as live capacity.
Raising the number buys tolerance for more simultaneously-abandoned
threads; it does not make them stoppable. The full behaviour, the registry
columns that expose it, and the metric to alert on are in
[OPERATIONS.md § Abandoned job threads](OPERATIONS.md#abandoned-job-threads-when-a-worker-stops-claiming-on-purpose).

### Queue caps

Concurrency and rate limits live in the `jorb_queue` table, are enforced
inside the claim statement, and change live with no restart:

```bash
pj-admin queues limits reports --max-concurrency 8 --rate-limit 100 --rate-period 60
pj-admin queues limits reports --max-concurrency none      # clear one
pj-admin queues show reports
```

A worked session with real output is in
[ADMIN_TOOLS.md § queues](ADMIN_TOOLS.md#queues).

Because they are enforced in the database they bind every claimer, not just
`pj` processes. They cost nothing when unset. See
[OPERATIONS.md § Queue controls](OPERATIONS.md#queue-controls-what-the-limits-actually-promise).

### Connection budget

Connection counts are fixed by the code, not by configuration, so the
budget is arithmetic:

| Process | Connections |
|---|---|
| each `pj` worker process | 2 (one for work, one dedicated to heartbeats) |
| `pj-scheduler` | 1 |
| `pj-monitor` | 1–2 (pool) |
| `pj-web` | 1–10 (pool, lazy) |
| `pj-ws` | 2–10 (pool) + 1 dedicated LISTEN connection |
| each `pj-admin` invocation | 1, for its lifetime |

`pj --workers N` forks N worker processes, so one such command is 2N
connections. Add whatever your application's `JobClient` pool uses to
enqueue, and check the total against PostgreSQL's `max_connections`.

A worker's heartbeat lives on its own connection deliberately: a stale
heartbeat is what tells the monitor that process is gone, so it must not be
able to go stale merely because the job connection is busy.

## Running under systemd

A worker template, one unit per queue:

```ini
# /etc/systemd/system/pyjobby@.service
[Unit]
Description=pyjobby worker (%i)
After=network.target

[Service]
Type=simple
User=pyjobby
WorkingDirectory=/opt/pyjobby
Environment="PYTHONPATH=/opt/pyjobby"
# %i is the queue name: `systemctl start pyjobby-worker@reports`
ExecStart=/opt/pyjobby/.venv/bin/pj \
    --config /etc/pyjobby/pyjobby.toml \
    --queue %i --workers 4
Restart=always
RestartSec=10s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/pyjobby-monitor.service
[Unit]
Description=pyjobby monitor (timeouts, dead-worker reclaim, retention)
After=network.target

[Service]
Type=simple
User=pyjobby
ExecStart=/opt/pyjobby/.venv/bin/pj-monitor \
    --config /etc/pyjobby/pyjobby.toml \
    --retention-days 30 --checkpoint-retention-days 1
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

`pj-scheduler`, `pj-web` and `pj-ws` follow the same shape. Workers exit
non-zero on a bad config, so `Restart=always` will not mask a broken
deploy — the unit will flap and `systemctl status` will show it.

```bash
systemctl enable --now pyjobby-monitor.service
systemctl enable --now pyjobby@default.service pyjobby@reports.service
journalctl -u 'pyjobby@*' -f
```

Note that abandoned job threads also delay a worker process's own exit, so
give workers a generous `TimeoutStopSec` if your jobs are synchronous and
long.

## Containers

```dockerfile
FROM python:3.14-slim

RUN useradd -m -u 1000 pyjobby
WORKDIR /app

RUN pip install --no-cache-dir git+https://github.com/mattsta/pyjobby.git#main

COPY job/ /app/job/
USER pyjobby

CMD ["pj", "--config", "/etc/pyjobby/pyjobby.toml"]
```

The image needs your job classes on the import path and a config file
mounted at runtime; it does not need the schema, which travels inside the
package.

```yaml
services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_DB: pyjobby
      POSTGRES_USER: pyjobby
      POSTGRES_PASSWORD: secret
    volumes: [postgres_data:/var/lib/postgresql/data]

  migrate:
    build: .
    depends_on: [postgres]
    environment:
      PYJOBBY_DSN: postgresql://pyjobby:secret@postgres:5432/pyjobby
    command: ["pj-admin", "db", "migrate"]
    restart: "no"

  monitor:
    build: .
    depends_on: [migrate]
    environment:
      PYJOBBY_DSN: postgresql://pyjobby:secret@postgres:5432/pyjobby
    command: ["pj-monitor"]
    restart: always

  worker-default:
    build: .
    depends_on: [migrate]
    volumes:
      - ./pyjobby.toml:/etc/pyjobby/pyjobby.toml:ro
    command: ["pj", "--config", "/etc/pyjobby/pyjobby.toml",
              "--queue", "default", "--queue", "default",
              "--queue", "default", "--queue", "default", "--workers", "4"]
    restart: always

volumes:
  postgres_data:
```

`pj-monitor` takes `PYJOBBY_DSN`, so it needs no mounted config; `pj` does.

## Kubernetes

Run `pj-admin db migrate` as a Job (or an init container) before the
Deployments, one Deployment per queue, and exactly one replica of the
monitor.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: pyjobby-migrate
spec:
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: migrate
          image: myregistry/pyjobby:latest
          command: ["pj-admin", "db", "migrate"]
          env:
            - name: PYJOBBY_DSN
              valueFrom:
                secretKeyRef: {name: pyjobby-db, key: dsn}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pyjobby-monitor
spec:
  replicas: 1
  selector:
    matchLabels: {app: pyjobby-monitor}
  template:
    metadata:
      labels: {app: pyjobby-monitor}
    spec:
      containers:
        - name: monitor
          image: myregistry/pyjobby:latest
          command: ["pj-monitor"]
          env:
            - name: PYJOBBY_DSN
              valueFrom:
                secretKeyRef: {name: pyjobby-db, key: dsn}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pyjobby-worker-default
spec:
  replicas: 3
  selector:
    matchLabels: {app: pyjobby-worker, queue: default}
  template:
    metadata:
      labels: {app: pyjobby-worker, queue: default}
    spec:
      containers:
        - name: worker
          image: myregistry/pyjobby:latest
          command: ["pj", "--config", "/etc/pyjobby/pyjobby.toml",
                    "--queue", "default", "--queue", "default",
                    "--queue", "default", "--queue", "default",
                    "--workers", "4"]
          volumeMounts:
            - {name: config, mountPath: /etc/pyjobby, readOnly: true}
          resources:
            requests: {memory: "512Mi", cpu: "500m"}
            limits:   {memory: "2Gi",   cpu: "2000m"}
      volumes:
        - name: config
          configMap: {name: pyjobby-config}
```

A worker that has lost its database is still a healthy process — it
reconnects with backoff. Do not attach a liveness probe that restarts it
for that; restarting only abandons its in-flight jobs to the monitor. If
you want a fleet-level probe, run `pj-admin doctor` from a CronJob and
alert on its exit code.

## Exposure and access

Neither web surface authenticates anything. `pj-web` binds `127.0.0.1:8081`
and `pj-ws` binds `127.0.0.1:8082` by default, and both say so in
`--help`. `pj-web` can cancel, retry and delete jobs; `pj-ws` can cancel,
retry and re-prioritise them. Keep them on localhost, behind an
authenticating reverse proxy, or on a private network. Do not pass
`--host 0.0.0.0` without one of those.

For the database, give the platform a role that can write every `jorb*`
table. It needs `DELETE` — retention is a delete — and it needs to create
the schema on first deploy. Do not narrow it to `SELECT, INSERT, UPDATE`
on `jorb` alone; the platform owns eleven tables (`jorb`, `jorb_queue`,
`jorb_worker`, `jorb_step`, `jorb_event`, `jorb_mailbox`, `jorb_history`,
`jorb_schedule`, `jorb_schedule_log`, `jorb_dag`, `jorb_dependencies`) plus
`schema_migrations`, and the monitor deletes from most of them.

## Backup and restore

`pg_dump` the whole database. There is nothing outside PostgreSQL to back
up, and nothing that has to be quiesced first: every state transition is a
single committed transaction, and a restored snapshot is a consistent
platform state.

```bash
pg_dump -Fc -d "$PYJOBBY_DSN" -f pyjobby-$(date +%Y%m%d-%H%M%S).dump
```

Do not write your own archive-and-delete job. Retention already deletes on
a schedule, in retention order, through the index built for it, in bounded
batches that cannot starve recovery. A hand-rolled `DELETE ... WHERE
updated < ...` competes with it, misses the tables that hang off `jorb`,
and can delete a terminal job that a `waiting` job still depends on.

To restore: stop the workers, `pg_restore`, run `pj-admin db migrate` (it
is a no-op if the dump was current), then `pj-admin doctor` before starting
anything.

## Verifying a deployment

In order, on the target database:

```bash
pj-admin db status         # base schema installed, no pending migrations
pj-admin doctor            # exits 1 on any FAIL
pj-bench plans             # exits non-zero if any hot query lost its index
```

Then start one worker and run the real end-to-end path — `pj-bench e2e`
launches actual `pj` processes against actual jobs, in its own uniquely
named queue that it deletes afterwards:

```console
$ pj-bench e2e --jobs 30000 -w 4 --repeat 1 --no-warmup --timeout 300
metric                             value
---------------------------------------------------------------------
worker processes                   4
jobs per run                       30,000
completed jobs/s                   2,462.65
spread                             0%
headroom vs 278/s                  8.86x
enqueue->finished p50/p95/p99/max  6.922 / 12.496 / 12.960 / 13.077 s
claim->finished p50/p95/p99/max    0.001 / 0.001 / 0.002 / 0.022 s
drained within timeout             yes
```

Those two latency rows are the ones to read: `claim->finished` is what the
platform costs, `enqueue->finished` is queue wait on top of it, and the gap
between them is capacity. Run this against a database that is otherwise
idle — the command refuses by default if more than 1000 jobs are already
there, because their contention would be measured as yours.

`pj-bench plans` belongs in CI. It is the only check here that catches a
problem before it reaches production.

## Capacity

Sizing, throughput ceilings, what breaks first, and the pre-flight
checklist for running at rate are all in [SCALE.md](SCALE.md). The short
version: the wall is plans, not volume, and the three things worth alerting
on are `notify_queue_usage`, backlog **age** (not depth), and completions
per second against arrivals per second.
