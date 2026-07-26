# Testing pyjobby

The suite is the platform's correctness proof: **1,125 tests** against a
real PostgreSQL, no mocked database anywhere in the core paths. Live
workers, real NOTIFY delivery, real transactions.

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

Under `pytest-xdist` each worker automatically gets its own database
(`pyjobby_test_gw0`, `_gw1`, …), created and migrated on first use. That
isolation is required, not an optimization: `jorb_worker`, `jorb_queue`,
and the aggregate views are global tables, so workers sharing one database
would see each other's rows and truncate each other's data mid-test. It
also lets tests assert exact global counts.

Separate *sessions* (e.g. several agents running suites at once) still need
distinct `PYJOBBY_TEST_DSN` values for the same reason.

## Shared test infrastructure

Reusable pieces live in the suite itself; extend these rather than writing
one-off scaffolding:

| Piece | What it gives you |
|---|---|
| `live_worker` fixture (`conftest.py`) | a REAL `JobSystem` running in-process (registry, heartbeat, LISTEN wakeups, DXE checkpoint binding); call it again for a second worker |
| `wait_for_job_state(conn, id, states)` | poll a job to a target state with a useful failure message |
| `tests/dxe_jobs.py` | shared job classes (`OkJob`, `FailJob`, `SlowJob`, `StepPipelineJob`, `SleeperJob`, `PingJob`, `PongJob`) resolved by dotted path like production jobs |
| `tests/utils/factories.py` | v1-safe row builders (aware UTC, non-NULL jsonb) |
| `tests/utils/processes.py` | launch real console scripts (`daemon`, `wait_until`, `free_port`, `port_is_open`) and reap their process groups |
| `unique_queue` / `test_id` fixtures | per-test namespacing so tests never collide on shared tables |

## CI

`.github/workflows/ci.yml` runs on every push and pull request against a
PostgreSQL 18 service container: `ruff check` + `ruff format --check`,
`mypy`, `pj-admin db migrate` followed by `pj-admin doctor` (proving a
fresh install is actually usable), then the suite with `-n auto` and the
coverage floor. A second job builds the wheel and asserts the packaged SQL
is present — the schema shipping inside the wheel is what makes
`pj-admin db migrate` work for an installed package.

## Coverage is a diagnostic, not a target

Current baseline: **81%** overall (`fail_under` in `pyproject.toml` holds
this floor so it can only ratchet upward).

**Read that number as a map of unexercised behavior, never as a score to
maximize.** This project has direct evidence of why:

| Module | Coverage during the earlier "coverage march" | Actual state at that coverage |
|---|---|---|
| `scheduler.py` | 97% | had **no entry point** — nothing ever ran it; cron never fired |
| `timeout_monitor.py` | 99% | complete **no-op** — queried `state='running'`, which the worker never wrote |
| `dag.py` | 100% | `wait_for_dag()` read columns that don't exist — could never detect completion |
| `client.py` | 90% | `enqueue()` **failed in production** (no JSON codec on the pool) |

Every line in those modules was executed by a test. Line coverage cannot
see whether a subsystem is *wired up*, whether its state machine is
*reachable*, or whether the assertion that passed was *meaningful* — some
tests in that era passed because the worker process they spawned died on a
`TypeError` before asserting anything.

### What to do instead

1. Run coverage to **find** untested regions, then ask: *what contract is
   untested here?* Write a test for that contract. Never write a test whose
   purpose is to touch a line.
2. Prefer tests that would fail if the feature were **unwired** — start the
   real process, drive the real command, assert the observable outcome.
3. Assert exact values, not `is not None` / `isinstance` / `in (...)`. A
   type check passes against a broken implementation.
4. Suspect any test that cannot fail: no assertion on the subject, an
   assertion satisfied by the setup alone, or a swallowed exception.
5. When a bug is found, the fix is a **behavioral** test at the contract
   level plus the source change — not a line-coverage bump.
