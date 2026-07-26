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

Parallel sessions (several agents, or `-n auto`) need separate databases —
`jorb_worker`, `jorb_queue`, and the aggregate views are global tables, so
sessions sharing one database will see each other's rows.

## Shared test infrastructure

Reusable pieces live in the suite itself; extend these rather than writing
one-off scaffolding:

| Piece | What it gives you |
|---|---|
| `live_worker` fixture (`conftest.py`) | a REAL `JobSystem` running in-process (registry, heartbeat, LISTEN wakeups, DXE checkpoint binding); call it again for a second worker |
| `wait_for_job_state(conn, id, states)` | poll a job to a target state with a useful failure message |
| `tests/dxe_jobs.py` | shared job classes (`OkJob`, `FailJob`, `SlowJob`, `StepPipelineJob`, `SleeperJob`, `PingJob`, `PongJob`) resolved by dotted path like production jobs |
| `tests/utils/factories.py` | v1-safe row builders (aware UTC, non-NULL jsonb) |
| `unique_queue` / `test_id` fixtures | per-test namespacing so tests never collide on shared tables |

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
