# Testing pyjobby

The suite is the platform's correctness proof: **over 1,700 tests** against a
real PostgreSQL, no mocked database anywhere in the core paths. Live workers,
real NOTIFY delivery, real transactions, real SIGKILLs.

Read the second half of this document *before* you measure anything. Every
rule in it was gotten wrong here first, and each mistake produced a
confidently wrong number that someone then wrote down and defended.

## Running it

```bash
make setup-db          # create role/db, install schema + migrations
make test              # everything
make test-fast         # skip slow/concurrency markers
make test-parallel     # -n auto
make coverage          # adds the coverage report
```

Point the suite at any database with `PYJOBBY_TEST_DSN`:

```bash
PYJOBBY_TEST_DSN="postgresql://pyjobby_test:pyjobby_test_password@localhost:5432/pyjobby_test" \
  poetry run pytest tests/test_dxe_core.py -q --no-cov
```

### The `performance` marker

`addopts` in `pyproject.toml` carries `-m "not performance"`, so the 18
throughput tests never run in the default suite or in CI. This is not
squeamishness — see [rule 7](#7-a-throughput-assertion-cannot-share-a-machine).
Run them deliberately, alone, and watch the printed numbers:

```bash
poetry run pytest -m performance -p no:xdist -s
```

`-m` is single-valued, so **any `-m` you pass replaces the exclusion** rather
than narrowing it. `pytest -m "not slow"` silently re-admits every performance
test that is not also marked slow.

They are permanent, not disposable: `TestNotifyGateThroughput` rebuilds the
deleted `job_state_change` firehose to measure against, so the reason it was
deleted stays a measurement instead of decaying into a claim in a comment.

## The database each test gets

Under `pytest-xdist` each worker automatically gets its own database
(`pyjobby_test_gw0`, `_gw1`, …), created and migrated on first use. That
isolation is required, not an optimization: `jorb_worker`, `jorb_queue`, and
the aggregate views are global tables, so workers sharing one database would
see each other's rows and truncate each other's data mid-test. It also lets
tests assert exact global counts.

Separate *sessions* (e.g. several agents running suites at once) still need
distinct `PYJOBBY_TEST_DSN` values, for the same reason.

**The schema fingerprint.** `conftest._install_schema` stores a SHA-256 of
`pyjobby/sql/schema.sql` in a `test_schema_fingerprint` table. If the file has
changed, the database is dropped (`DROP SCHEMA public CASCADE`) and rebuilt
from `migrations.migrate()`. pyjobby is forward-only with one canonical schema
file, so a database installed from an older revision of it is simply wrong —
and it fails as `function does not exist` / `column does not exist`, which
reads like a product bug rather than a stale fixture. Editing `schema.sql` is
therefore the whole of a schema change; nothing else needs touching.

### The trap: writing to the worker database and reading the base DSN

**A test that writes through `db_pool`/`db_params` but reads through the base
`PYJOBBY_TEST_DSN` is testing nothing.** Under xdist those are two different
databases: the write lands in `pyjobby_test_gw3`, the read inspects
`pyjobby_test`, and the assertion passes or fails on unrelated leftovers. The
base database is also the one conftest never re-fingerprints, so it silently
lags behind `schema.sql`.

This has happened twice. Both times the failure was intermittent, both times
it appeared only under `-n auto`, and both times the symptom was blamed on the
newest change in the diff rather than on the fixture. Once the tests were
passing *only* because the two databases happened to agree.

The rule: anything that needs a DSN string builds it from `db_params`. Use
`pyjobby.procs.dsn_from(db_params)` — never `conftest.TEST_DSN`, never
the environment variable. `tests/test_dxe_admin.py`'s `dsn` fixture is the
worked example.

### Cleanup is `DELETE`, and that matters

`ensure_clean_database` is autouse and runs `DELETE` (not `TRUNCATE`) before
every test. That is correct for correctness tests and quietly fatal for plan
tests — a deleted row is not a gone row. See
[rule 6](#6-truncate-before-seeding-a-ratio-threshold-does-not-rescue-you).

## Shared test infrastructure

Reusable pieces live in the suite itself; extend these rather than writing
one-off scaffolding.

| Piece | What it gives you |
|---|---|
| `live_worker` fixture (`conftest.py`) | a REAL `JobSystem` running in-process (registry, heartbeat, LISTEN wakeups, DXE checkpoint binding); call it again for a second worker |
| `wait_for_job_state(conn, id, states)` | poll a job to a target state with a useful failure message |
| `unique_queue` / `test_id` fixtures | per-test namespacing so tests never collide on shared tables |
| `tests/dxe_jobs.py` | shared job classes (`OkJob`, `FailJob`, `SlowJob`, `StepPipelineJob`, `SleeperJob`, `PingJob`, `PongJob`) resolved by dotted path like production jobs |
| `tests/utils/factories.py` | v1-safe row builders (aware UTC, non-NULL jsonb) |
| `pyjobby/procs.py` | launch real console scripts and reap their process groups |
| `tests/utils/faults.py` | fault injection and the side-effect ledger |
| `tests/utils/plans.py` | seeding and assertions for query-plan tests |

### `pyjobby/procs.py` — real processes

A console script that parses `--help` proves nothing. These helpers start the
actual entry point from `.venv/bin`, wait for an observable effect, and reap
it:

- `spawn(*args)` — `start_new_session=True`, so the child gets its **own
  process group**. Every kill here is a group kill: the `pj` launcher forks
  its workers, and killing only the direct child leaves pollers behind that
  claim other tests' jobs.
- `daemon(...)` — a context manager that **fails the test if the process died
  during startup**. A daemon that exits immediately (bad flag, unreadable
  config, import error) is otherwise indistinguishable from one running
  quietly.
- `wait_until(predicate)` — polls an async predicate and returns the truthy
  value so the caller can assert on it.
- `free_port()` / `port_is_open()` — for tests that bind a server.
- `dsn_from(db_params)` — the only correct way to get a DSN string (above).

### `tests/utils/faults.py` — break it for real

Durability claims are only worth the failures they have actually survived, so
nothing here is simulated:

- `kill_backends(pool, pids)` — `pg_terminate_backend` on a worker's
  server-side backends, indistinguishable from a failover to the victim. It
  waits until they are gone from `pg_stat_activity`, so the fault has really
  landed when the call returns. `new_backends(pool)` diffs `pg_stat_activity`
  around a worker's startup to identify exactly that worker's connections.
- `sigkill_group(proc)` — `SIGKILL` to a real worker's process group. **No
  SIGTERM first**, deliberately: the graceful-shutdown handler must never run,
  so nothing deregisters from `jorb_worker` and no terminal state is written.
  That is the failure the monitor's dead-worker sweep exists for. Returns -9.
- `age_worker_heartbeats` / `age_claim` — backdate the registry or a claim so
  a test reaches the monitor's grace period without sleeping through it.
- `write_worker_config(tmp_path, db_params)` — a `pyjobby.conf.py` the real
  `pj` launcher will read.

**The side-effect ledger** is the other half. Proving that a step "did not
re-execute" needs an observable effect *outside* the checkpoint table —
otherwise the test asks the checkpoint table whether the checkpoint table is
right. `jorb_test_effect` is a test-only table (not in `schema.sql`), created
on demand and scoped by `tag` (tests pass their unique queue name), and jobs
append one row per real execution. `record_effect` writes on the worker's own
connection; `record_effect_out_of_band` writes on a separate, immediately
committed connection, which is what lets a test see that a transactional write
is *staged but not committed* and kill at exactly that instant.
`effect_counts` / `effect_counts_per_job` return dicts, for exact-value
assertions rather than "at least one".

### `tests/utils/plans.py` — plan assertions

- `seed_for_plans(pool)` — 20,000 rows in the shape a real steady state has: a
  large terminal history, a small live set, timestamps spread over 60 days. It
  **`TRUNCATE`s first** and leaves the table `VACUUM (ANALYZE)`d, so the
  measurement does not depend on what ran before it.
- `reset_job_tables(pool)` — `TRUNCATE … RESTART IDENTITY CASCADE`.
- `settle(pool)` — `VACUUM (ANALYZE)`; never `VACUUM FULL` (exclusive lock,
  and not what production looks like). `ANALYZE` alone is not enough: only
  `VACUUM` sets the visibility map, and until it is set an index-only plan is
  costed as though every tuple needed a heap fetch — which is enough to make
  the planner pick a sequential scan and the gate report a design flaw that
  its own seeding created.
- `plan_for(pool, sql, *args)` — `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF)`.
- `rows_removed_by_filter(plan)` — summed across nodes; the discard can happen
  at any of them. See [rule 5](#5-no-seq-scan-is-necessary-and-not-sufficient).
- `buffers_in(plan)` — from the **root node only**. EXPLAIN reports buffers
  cumulatively up the tree, so summing every node counts each child once per
  ancestor and reports a query as several times more expensive than it is.
- `assert_no_seq_scan(plan)`, `assert_reads_far_less_than_a_scan(pool, plan)`
  (the probe touched under a tenth of what reading the table costs, calibrated
  against the table's *current* page count).

### `pj-bench` — the permanent benchmark harness

Every number in `docs/SCALE.md` was once measured by a script that was then
thrown away. `pj-bench` is the replacement: each subcommand *reproduces* one
of those measurements.

| Subcommand | What it measures |
|---|---|
| `pj-bench enqueue` | insert throughput and what NOTIFY costs at the commit lock; three modes (`production`, `serial_contrast`, `bulk_contrast`) and five trigger variants |
| `pj-bench claim` | claim throughput through the real `claim_jorb()`, lock contention, and what a capped queue *sustains* — five interleaved arms |
| `pj-bench e2e` | real `pj` worker processes: completed jobs/s, plus `enqueue_to_finished` and `claim_to_finished` latency separately |
| `pj-bench notify` | notifications per job lifecycle, **unobserved and observed**, because on a demand-gated schema that question has two correct answers |
| `pj-bench plans` | `EXPLAIN (ANALYZE, BUFFERS)` every hot query in **two states** (caught up and backlogged); **exits non-zero on a sequential scan of any gated table, or on a discard budget overrun**. Its sweep cases are derived from monitor.py's `SWEEP_*_SQL` constants, so a new sweep with no gate entry is an error rather than a gap |
| `pj-bench all` | everything, with one summary table |

`pj-bench plans --force` **is the CI gate** (`.github/workflows/ci.yml`), and
it is the only subcommand safe to gate on: it asserts plans, not durations.
Its `--planner-setting enable_indexscan=off` exists to prove the gate actually
fires.

Cross-cutting flags: `--json` (stable keys, for diffing two runs), `--repeat N`
and `--warmup/--no-warmup`, and the busy-database guard (`--max-existing-jobs`,
`--force`) which refuses to run alongside real work — that measures the
contention, not the platform. `pj-bench enqueue` will not disable triggers
without `--allow-trigger-toggle`, and restores them from a `finally`, a SIGTERM
handler, *and* an `atexit` hook on a fresh connection: a
`jorb_enqueued_notify` left disabled is a silent install-wide outage, not a
slow query.

`tests/test_bench.py` tests the harness itself at tiny N — the JSON keys, the
non-zero exit, the trigger restoration under an exception, and that cleanup
removes exactly what the run created. A benchmark nobody trusts is worse than
no benchmark, because its numbers get written into documents and then
defended.

## How to measure

Eight rules. Each one is here because breaking it produced a published number
that was wrong.

### 1. One transaction per job

A bulk insert amortises a single commit — and a single NOTIFY commit-lock
acquisition — over the whole batch. `docs/SCALE.md` once quoted **67k rows/s**
for enqueue from a 20,000-row single-statement insert. Real enqueue is roughly
**6x** slower than that; the bulk figure described a path nobody runs.

`pj-bench enqueue` still measures it, labelled `bulk_contrast`, precisely so
it cannot be quoted as the answer. `production` is concurrent, one transaction
per job. Measure the shape production writes in.

### 2. Some costs only exist under concurrency

Committing a transaction that issued a `NOTIFY` takes a **global exclusive
lock** held to the end of that commit, because notifications must be delivered
in commit order and commit order is not settled until commits finish. Every
notifying commit therefore serialises against every other one.

A single client has nothing to serialise against. Measured serially, the
NOTIFY cost is **3%** — noise, "not worth touching". Measured concurrently on
the same schema it is **62%** (~2.6x). The serial number was not merely
imprecise; it pointed the wrong way, and it is why the firehose channel
survived as long as it did.

Corollary, learned the same way: the lock is per **commit**, not per
notification. Silencing one of several ungated channels recovered *nothing*
(the three-per-lifecycle `job_state_change` firehose: ceiling unmoved; gating
`jorb_done` while the firehose survived: 1.01x). Deleting the last ungated
channel recovered everything (**2.63–2.95x** on the completion path). Partial
trimming is worth exactly zero until a commit path reaches zero notifications.

### 3. Interleave the arms; report a median and its spread

Running all of arm A and then all of arm B lands every bit of machine drift —
accumulating dead tuples, an autovacuum waking up, a checkpoint, another
process starting — on the ratio you are about to publish.

`pj-bench claim` once reported the uncapped rate moving **15x** on an entirely
unchanged schema, purely because the box got busy between the two halves.

So: alternate rounds (`for _ in range(repeat): for arm in arms:`), discard a
warm-up round, and reduce by **median plus spread**. A median with a 3x spread
is also noise, and a reader who cannot see the spread will believe the median.
`bench.summarize()` reports `spread_pct` for exactly this reason, and the
in-suite benchmarks use the same round-robin shape (`_measure()` in
`tests/test_notify_gating.py`, the timeout sweep in
`tests/test_claim_contention.py`).

### 4. Assert the plan, not a duration

Timings flake on a loaded CI box and pass on a fast one **with the index
dropped**. A plan is a fact; a duration is a statement about hardware.

"Did this query stop using its index" is also the regression that stays
correct while getting slower forever — the one you otherwise discover in
production, months in, when the table is finally big enough to matter.

Where a number is unavoidable, gate on the *waste* rather than the rate. The
claim-lock timeout sweep measured 627 / 839 / 766 / 830 / 862 claims/s on a
quiet box and 219 / 616 / 691 / 618 / 239 on a loaded one — unusable — while
the empty-return counts stayed a clean step function in both. The assertions
are on the empty returns.

### 5. No-seq-scan is necessary and NOT sufficient

An index scan that reads every row and discards every row is **not a
sequential scan**, passes a seq-scan assertion, and costs the same.

This hid a real defect in the checkpoint sweep for a long time. The original
`jorb_step JOIN jorb` form planned as a merge join driven by `jorb_pkey`:
**20,000 rows removed by filter and 534–1,194 buffers to delete nothing**,
every cycle, growing with the table forever. It passed every seq-scan check it
was given.

Always pair the access-method assertion with `rows_removed_by_filter(plan)`,
and prefer an exact expectation (`== 0` in the steady state) over a bound.
Ordering counts too: `ORDER BY id` on the retention probe makes the planner
take the pkey and filter everything — 465 buffers, 20,000 rows discarded —
where ordering by the indexed expression is a **2-buffer** index scan.

### 6. Truncate before seeding; a ratio threshold does not rescue you

Plan tests are bloat-sensitive. The autouse cleanup uses `DELETE`, so each
20k-row seed lays its rows across the pages the last one left behind. After a
handful of plan tests the heap holds several times the live rows, the same
query touches proportionally more buffers, and assertions start failing purely
because of what ran earlier — the classic file that passes alone and fails in
a suite.

Plain `VACUUM` does not rescue it either: it marks pages reusable rather than
returning them, so the table stays large and the rows stay spread.

**And a ratio threshold does not rescue it.** Dead tuples inflate the table's
page count *and* the buffers the probe touches, so both sides of
`touched * 10 < pages` move together and the assertion drifts out of meaning
in either direction. Calibrating against the current size (as
`assert_reads_far_less_than_a_scan` does) is still right — an absolute
threshold looks precise and is not, and a gate that only passes after
`VACUUM FULL` is a gate everyone learns to ignore — but it is a second line of
defence, not a substitute for starting from a truncated table.

Use `seed_for_plans()` / `reset_job_tables()`. `pj-bench`'s own
`cleanup_queue()` VACUUMs after every run for the same reason: a tool that
silently degrades every other query on the database it shares is not usable
infrastructure.

### 7. A throughput assertion cannot share a machine

Under `-n 4`, a throughput test measures the contention between xdist workers,
not the code. The notify-gate benchmark reported **1.24x** for a change that
measures **2.63x** on a quiet box — a failing assertion for a real, large win.

A performance test that fails because the machine is busy trains everyone to
ignore performance tests. That is why they carry the `performance` marker and
are excluded from `addopts`, and why they are run `-p no:xdist`.

### 8. The test for a new index or rollup is *who pays when it is unused*

Not "is it cheap". Both of the following are "add an index or a table to make
a read cheaper", and only one of them was accepted:

- **Rejected — the cumulative per-queue counter rollup.** An O(1) source for
  `pyjobby_jobs_finished_total{queue}` means every state transition in a queue
  updates one row: ~1,100 updates/s funnelled onto a handful of tuples, on the
  write path, forever, to make a read cheaper that happens once per scrape.
  Every job pays whether or not anybody reads the counter.
- **Accepted — the partial GIN index on `jorb.tags`.** GIN is the most
  expensive index type to maintain, so it was measured with the arms
  interleaved (rule 3) before it was kept. Untagged enqueue was unchanged; the
  predicate is `WHERE tags <> '{}'`, so a job that sets no tags never matches
  it and never touches the index. It charges nothing to the jobs that do not
  use it.

The full numbers and the rest of the write-path decisions are in
[SCALE.md](SCALE.md) — read them there rather than restating them here, so
there is one place to update when they are re-measured.

## What makes a test worth having

### Coverage is a diagnostic, not a target

The floor lives in `pyproject.toml` (`fail_under`, currently **89%**) so it can
only ratchet upward; the suite measures **91%** against it today.

**Read that number as a map of unexercised behavior, never as a score to
maximize.** This project has direct evidence of why:

| Module | Coverage during the earlier "coverage march" | Actual state at that coverage |
|---|---|---|
| `scheduler.py` | 97% | had **no entry point** — nothing ever ran it; cron never fired |
| `timeout_monitor.py` | 99% | complete **no-op** — queried `state='running'`, which the worker never wrote |
| `dag.py` | 100% | `wait_for_dag()` read columns that don't exist — could never detect completion |
| `client.py` | 90% | `enqueue()` **failed in production** (no JSON codec on the pool) |

Every line in those modules was executed by a test. Line coverage cannot see
whether a subsystem is *wired up*, whether its state machine is *reachable*,
or whether the assertion that passed was *meaningful* — some tests in that era
passed because the worker process they spawned died on a `TypeError` before
asserting anything.

### What to do instead

1. Run coverage to **find** untested regions, then ask: *what contract is
   untested here?* Write a test for that contract. Never write a test whose
   purpose is to touch a line.
2. **Mutation-test your own assertion.** Break the implementation on purpose —
   drop the index, delete the trigger, remove the fence, return the wrong
   epoch — and confirm the test fails. An assertion that has never been seen
   to fail is a hypothesis, not a test. Build the control into the test where
   you can: the epoch-fencing suite fires every fenced statement at a
   superseded epoch *and* keeps a positive control at the current epoch, so a
   statement that simply stopped working could never pass.
3. **Prefer real processes and real databases to mocks.** Start the real
   console script, drive the real command, kill the real process group, assert
   the observable outcome. A test that would still pass if the feature were
   unwired is not testing the feature. (This is the direct lesson of the 97%
   scheduler with no entry point.)
4. **Assert exact values**, not `is not None` / `isinstance` / `in (...)`. A
   type check passes against a broken implementation. Prefer
   `effect_counts(...) == {"first": 1, "second": 1}` over "at least one".
5. **Pin a defect's contrast, not just its fix.** The clearest example is
   `KILL_WINDOW_CASES` in `tests/test_dxe_faults.py`: the same fault (SIGKILL
   in the window between a job's database write and its checkpoint) injected
   into the same code shape, once with `step()` and once with `transaction()`.

   The ledger counts what really executed; the checkpoint table is never
   consulted for the claim.

   - `transaction()` — after the kill `{attempt: 1}` (the write is staged,
     invisible, and never commits); at the end `{attempt: 2, write: 1}` —
     **exactly once**.
   - `step()` — after the kill `{attempt: 1, write: 1}` (the write committed
     on its own); at the end `{attempt: 2, write: 2}` — **at least once**.

   That duplicate is not a bug in `step()`; it is what at-least-once means,
   and it is why `transaction()` exists. Pinning both rows is what stops the
   two primitives being "simplified" back into one.
6. Suspect any test that cannot fail: no assertion on the subject, an
   assertion satisfied by the setup alone, or a swallowed exception.
7. When a bug is found, the fix is a **behavioral** test at the contract level
   plus the source change — not a line-coverage bump.

## CI

`.github/workflows/ci.yml` runs on every push and pull request against a
PostgreSQL 18 service container:

1. `ruff check` + `ruff format --check`, then `mypy`.
2. `pj-admin db migrate` followed by `pj-admin doctor` — proving a fresh
   install is actually usable, not just that migration returned zero.
3. The suite with `-n auto` and the coverage floor (`performance` tests are
   excluded by `addopts`; see rule 7).
4. `pj-bench plans --force` — the gate that catches a lost index. It is the
   only performance-adjacent thing CI runs, because it asserts plans rather
   than durations (rule 4).

A second job builds the wheel and asserts the packaged SQL is present: the
schema shipping inside the wheel is what makes `pj-admin db migrate` work for
an installed package.
