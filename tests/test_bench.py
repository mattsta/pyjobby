"""Tests for the pj-bench harness itself.

A benchmark nobody trusts is worse than no benchmark: it produces numbers
that get written into documents and then defended. These tests do not
measure performance — they prove the harness is HONEST at tiny N:

* every subcommand runs and emits JSON with the keys its consumers read
* ``plans`` exits non-zero on a sequential scan of jorb and zero otherwise,
  which is the only reason it is safe to run in CI as a gate
* the triggers ``enqueue`` disables come back even when the run explodes
  while they are off — the one failure here that would silently break a
  production install rather than just report a wrong number
* cleanup removes exactly what the run created, asserted as row counts
* the busy-database guard fires, so a benchmark cannot quietly compete with
  real work and report the contention as the platform's speed

Everything uses tiny N deliberately. Correctness at N=5 and correctness at
N=20000 are the same property here; only the runtime differs.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import traceback

import asyncpg
import pytest
from click.testing import CliRunner

from pyjobby import bench
from pyjobby.procs import dsn_from, spawn, terminate, wait_until

#: Enough rows that the planner has a real choice. Below this a sequential
#: scan is the genuinely correct plan and the gate would pass for the wrong
#: reason (which is exactly why `plans` has a --seed at all).
PLAN_SEED = 20_000

TRIGGERS = (
    bench.TRIGGER_ENQUEUED,
    bench.TRIGGER_DONE,
    bench.TRIGGER_CANCEL,
    bench.TRIGGER_HISTORY,
)


def _invoke(dsn: str, args: tuple[str, ...]) -> tuple[int, dict, str]:
    result = CliRunner().invoke(bench.cli, ["--dsn", dsn, *args, "--json"], obj={})
    output = result.output
    payload: dict = {}
    if "{" in output:
        # raw_decode, not loads: a failing gate prints its explanation AFTER
        # the JSON document, and the document must still parse.
        payload, _ = json.JSONDecoder().raw_decode(output[output.index("{") :])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        # CliRunner swallows tracebacks; a test that fails on the exit code
        # alone is undebuggable, so carry the traceback in the output.
        output += "\n" + "".join(
            traceback.format_exception(result.exception)  # type: ignore[arg-type]
        )
    return result.exit_code, payload, output


async def run_bench(dsn: str, *args: str) -> tuple[int, dict, str]:
    """Invoke pj-bench through its real click entry point.

    Runs in a worker thread because the command drives ``asyncio.run``, and
    these tests already own an event loop. Exercising the real entry point
    matters more than convenience here: a console script that only works
    when someone else already built the loop is not the thing an operator
    runs.

    Returns the exit code, the parsed JSON, and the combined output, so a
    test can assert on the machine-readable contract and the exit code
    together — a command that reports a failure while exiting 0 is exactly
    the bug a CI gate cannot have.
    """
    return await asyncio.to_thread(_invoke, dsn, args)


async def counts(conn: asyncpg.Connection) -> dict[str, int]:
    """Row counts for every table pj-bench can write to."""
    tables = (
        "jorb",
        "jorb_history",
        "jorb_step",
        "jorb_mailbox",
        "jorb_queue",
        "jorb_worker",
    )
    return {
        table: int(await conn.fetchval(f"SELECT count(*) FROM {table}"))  # noqa: S608
        for table in tables
    }


async def trigger_states(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch(
        # ::text because pg_trigger.tgenabled is the internal "char" type,
        # which comes back as bytes and silently fails a == "O" comparison
        "SELECT tgname, tgenabled::text AS tgenabled FROM pg_trigger "
        "WHERE tgrelid = 'jorb'::regclass AND NOT tgisinternal"
    )
    return {r["tgname"]: r["tgenabled"] for r in rows}


class TestSubcommandsEmitDocumentedJson:
    """--json is the CI contract; these are the keys it promises."""

    async def test_enqueue(self, db_params):
        code, payload, _ = await run_bench(
            dsn_from(db_params),
            "enqueue",
            "--concurrency",
            "2",
            "--jobs-per-connection",
            "5",
            "--rows",
            "50",
            "--repeat",
            "1",
            "--no-warmup",
            "--allow-trigger-toggle",
        )

        assert code == 0
        assert payload["benchmark"] == "enqueue"
        assert payload["jobs"] == 10
        assert payload["jobs_per_second"] > 0
        assert set(payload["modes"]) == {
            "production",
            "serial_contrast",
            "bulk_contrast",
        }
        # the contrast modes must SAY they are contrast, in the JSON, so a
        # consumer cannot quote the bulk number as production enqueue
        assert "NOT production enqueue" in payload["modes"]["bulk_contrast"]["meaning"]
        assert "CONTRAST ONLY" in payload["modes"]["serial_contrast"]["meaning"]
        production = payload["modes"]["production"]["variants"]
        assert production["all_triggers_on"]["jobs_per_second"] > 0
        assert production["all_notify_off"]["jobs_per_second"] > 0
        lock = payload["notify_commit_lock"]
        assert lock["ratio"] is not None
        assert lock["wakeup_only_recovery_pct"] is not None
        # Every variant must name only triggers that exist. A variant naming
        # a deleted trigger fails the whole run inside ALTER TABLE, which is
        # how `pj-bench enqueue --allow-trigger-toggle` broke when
        # job_state_change_notify was removed from the schema.
        assert "firehose_off" not in production
        assert "firehose_and_history_off" not in production
        assert payload["cleanup"]["jobs_deleted"] >= 0

    async def test_every_toggled_trigger_still_exists_in_the_schema(self, db_pool):
        """The variants are only meaningful if their triggers are real.

        `ALTER TABLE ... DISABLE TRIGGER` on a name that is not there is an
        error, so a stale entry in ENQUEUE_VARIANTS does not degrade the
        report — it takes the subcommand down with it.
        """
        live = set(await trigger_states(db_pool))

        named = {t for _, triggers in bench.ENQUEUE_VARIANTS for t in triggers}

        assert named <= live, named - live
        assert set(bench.NOTIFY_TRIGGERS) <= live

    async def test_enqueue_refuses_trigger_toggling_by_default(self, db_params):
        """The dangerous variants must be opt-in, and say so rather than
        silently vanishing from the report."""
        code, payload, _ = await run_bench(
            dsn_from(db_params),
            "enqueue",
            "--concurrency",
            "2",
            "--jobs-per-connection",
            "5",
            "--rows",
            "50",
            "--repeat",
            "1",
            "--no-warmup",
        )

        assert code == 0
        variants = payload["modes"]["production"]["variants"]
        assert variants["all_triggers_on"]["jobs_per_second"] > 0
        assert variants["all_notify_off"] == {
            "skipped": "requires --allow-trigger-toggle"
        }
        assert payload["notify_commit_lock"]["ratio"] is None

    async def test_claim(self, db_params):
        code, payload, _ = await run_bench(
            dsn_from(db_params),
            "claim",
            "--workers",
            "2",
            "--jobs",
            "20",
            "--repeat",
            "1",
            "--no-warmup",
        )

        assert code == 0
        assert payload["benchmark"] == "claim"
        assert payload["claims_per_second"] > 0
        for mode in ("uncapped", "capped"):
            data = payload["modes"][mode]
            assert data["claims"] == 20
            assert data["claims_per_second"]["median"] > 0
            assert 0.0 <= data["lock_miss_rate"] <= 1.0
        # an uncapped queue never takes the advisory lock, so it cannot miss it
        assert payload["modes"]["uncapped"]["empty_claims_with_work_available"] == 0

    async def test_notify(self, db_params):
        code, payload, _ = await run_bench(
            dsn_from(db_params), "notify", "--lifecycles", "5"
        )

        assert code == 0
        assert payload["benchmark"] == "notify"

        # The arithmetic is asserted as the harness being RIGHT, not as a
        # fixed count: how many notifications a lifecycle emits is a property
        # of the schema, and a test that pinned it would fail on an
        # improvement and be silently "fixed" by editing the number, which is
        # how a benchmark stops meaning anything. What must never move is
        # that the reported figure is the count that arrived.
        lifecycles = payload["lifecycles"]
        assert lifecycles == 5
        assert set(payload["phases"]) == {"unobserved", "observed"}
        for phase in payload["phases"].values():
            assert phase["total"] == sum(phase["per_channel"].values())
            assert phase["per_lifecycle"] == phase["total"] / lifecycles
            assert phase["projected_notifications_per_second"] == pytest.approx(
                phase["per_lifecycle"] * bench.SCALE_TARGET_RATE
            )
            # every channel it counted is one the schema can actually emit on
            assert set(phase["per_channel"]) == set(bench.NOTIFY_CHANNELS)

        # There is no unqualified "per_lifecycle": the number's meaning
        # depends on who was watching, so the key has to say which.
        assert "per_lifecycle" not in payload
        assert (
            payload["per_lifecycle_unobserved"]
            == (payload["phases"]["unobserved"]["per_lifecycle"])
        )
        assert (
            payload["per_lifecycle_observed"]
            == (payload["phases"]["observed"]["per_lifecycle"])
        )

        # The demand gate, measured rather than asserted from the schema: an
        # unobserved lifecycle costs strictly less than a watched one, and
        # the difference is what the gate saves.
        assert payload["per_lifecycle_unobserved"] < payload["per_lifecycle_observed"]
        assert payload["demand_gate_saves_per_lifecycle"] == (
            payload["per_lifecycle_observed"] - payload["per_lifecycle_unobserved"]
        )

        # The claims, re-derived after job_state_change was deleted: zero for
        # a job nobody is watching (every remaining channel is gated), two
        # for a watched one (the jorb_enqueued wakeup and the jorb_done
        # completion signal, and nothing else).
        claims = payload["scale_md"]
        assert claims["claimed_unobserved"] == 0
        assert claims["claimed_observed"] == 2
        assert claims["unobserved_agrees"] == (payload["per_lifecycle_unobserved"] == 0)
        assert claims["observed_agrees"] == (payload["per_lifecycle_observed"] == 2)
        assert claims["agrees"] == (
            claims["unobserved_agrees"] and claims["observed_agrees"]
        )

        # Structural rather than tunable: history records all four row
        # writes, and the queue-usage reading is a fraction.
        assert payload["history_rows_per_lifecycle"] == 4.0
        assert claims["history_agrees"] is True
        assert 0.0 <= payload["notify_queue_usage"]["after"] <= 1.0

    async def test_notify_counts_only_channels_the_schema_emits_on(self, db_pool):
        """LISTENing on a dead channel is not harmless.

        PostgreSQL accepts LISTEN on any name, so a stale entry in
        NOTIFY_CHANNELS does not raise -- it reports a confident 0 for a
        channel that cannot exist, which is exactly how `pj-bench notify`
        kept counting job_state_change after it was deleted.
        """
        emitted = {
            r["channel"]
            for r in await db_pool.fetch(
                r"""
                SELECT DISTINCT
                       (regexp_match(pg_get_triggerdef(t.oid),
                                     $re$jorb_notify\('([a-z_]+)'$re$))[1]
                           AS channel
                  FROM pg_trigger t
                  JOIN pg_proc p ON p.oid = t.tgfoid
                 WHERE p.proname = 'jorb_notify' AND NOT t.tgisinternal
                """
            )
        }

        assert emitted, "no jorb_notify triggers found; the query is wrong"
        assert set(bench.NOTIFY_CHANNELS) == emitted

    async def test_e2e(self, db_params):
        code, payload, _ = await run_bench(
            dsn_from(db_params),
            "e2e",
            "--jobs",
            "5",
            "--workers",
            "1",
            "--repeat",
            "1",
            "--no-warmup",
            "--timeout",
            "60",
        )

        assert code == 0
        assert payload["benchmark"] == "e2e"
        assert payload["timed_out"] is False
        assert payload["completed"] == 5
        assert payload["jobs_per_second"]["median"] > 0
        for block in ("enqueue_to_finished", "claim_to_finished"):
            latency = payload[block]
            assert latency["count"] == 5
            assert latency["p50"] <= latency["p95"] <= latency["p99"] <= latency["max"]

    async def test_plans(self, db_params, db_pool):
        code, payload, _ = await run_bench(
            dsn_from(db_params), "plans", "--seed", str(PLAN_SEED)
        )

        assert code == 0, payload
        assert payload["benchmark"] == "plans"
        assert payload["healthy"] is True
        assert payload["seq_scan_offenders"] == []
        assert set(payload["queries"]) == {
            "claim",
            "concurrency_cap",
            "retention_probe",
            "checkpoint_sweep",
            "mailbox_sweep",
            "metrics_completions",
            "metrics_arrivals",
        }
        for key, data in payload["queries"].items():
            assert data["access_methods"], key
            assert data["buffers"] > 0, key
            assert data["seq_scan_on_jorb"] == [], key
        # the two paths docs/SCALE.md names must reach their own index
        assert "jorb_claim_idx" in payload["queries"]["claim"]["indexes"]
        assert "jorb_retention_idx" in payload["queries"]["retention_probe"]["indexes"]
        # The concurrency-cap count runs inside the per-queue advisory lock,
        # so it is the one query here whose cost is subtracted from a capped
        # queue's entire throughput rather than from one timer. It must reach
        # an index: a scan there sets the capped ceiling by TABLE size, which
        # a capped queue's own workload does not control.
        assert payload["queries"]["concurrency_cap"]["indexes"], payload["queries"][
            "concurrency_cap"
        ]
        # the gate seeds 20k rows; leaving them behind would poison every
        # later measurement taken on the same database
        assert payload["cleanup"]["jobs_deleted"] == PLAN_SEED
        assert await db_pool.fetchval("SELECT count(*) FROM jorb") == 0

    async def test_all(self, db_params):
        code, payload, _ = await run_bench(
            dsn_from(db_params),
            "all",
            "--concurrency",
            "2",
            "--jobs-per-connection",
            "5",
            "--rows",
            "50",
            "--claim-workers",
            "2",
            "--claim-jobs",
            "20",
            "--e2e-jobs",
            "5",
            "--e2e-workers",
            "1",
            "--lifecycles",
            "5",
            "--seed",
            str(PLAN_SEED),
            "--repeat",
            "1",
            "--no-warmup",
        )

        assert code == 0, payload
        assert set(payload) >= {"enqueue", "claim", "e2e", "notify", "plans", "healthy"}
        assert payload["healthy"] is True
        assert payload["notify"]["per_lifecycle_observed"] > 0
        assert payload["notify"]["per_lifecycle_unobserved"] == 0


class TestPlansGate:
    """`plans` is only usable in CI if its exit code is trustworthy."""

    async def test_exits_non_zero_when_a_seq_scan_happens(self, db_params):
        """Forced through the planner, not through a too-small table, so the
        failure is deterministic rather than a statistics accident."""
        code, payload, output = await run_bench(
            dsn_from(db_params),
            "plans",
            "--seed",
            "500",
            "--planner-setting",
            "enable_indexscan=off",
            "--planner-setting",
            "enable_bitmapscan=off",
        )

        assert code == 1
        assert payload["healthy"] is False
        assert "retention_probe" in payload["seq_scan_offenders"]
        assert payload["queries"]["retention_probe"]["seq_scan_on_jorb"]
        assert "sequential scan of jorb" in output

    async def test_rejects_a_malformed_planner_setting(self, db_params):
        code, _, output = await run_bench(
            dsn_from(db_params),
            "plans",
            "--seed",
            "10",
            "--planner-setting",
            "enable_indexscan = off; DROP TABLE jorb",
        )

        assert code == 1
        assert "Invalid --planner-setting" in output


class TestTriggerRestoration:
    """Leaving a jorb trigger disabled is the one failure here that breaks
    the install rather than the measurement."""

    async def test_triggers_are_restored_after_a_normal_run(self, db_params, db_pool):
        before = await trigger_states(db_pool)
        assert set(TRIGGERS) <= set(before)

        code, _, _ = await run_bench(
            dsn_from(db_params),
            "enqueue",
            "--concurrency",
            "2",
            "--jobs-per-connection",
            "5",
            "--rows",
            "50",
            "--repeat",
            "1",
            "--no-warmup",
            "--allow-trigger-toggle",
        )

        assert code == 0
        assert await trigger_states(db_pool) == before

    async def test_triggers_are_restored_when_the_run_raises_mid_flight(
        self, db_params, db_pool, monkeypatch
    ):
        """Simulate a failure while triggers are DISABLED.

        The failure is injected exactly when something is disabled rather
        than on a fixed call number, so reordering the variant list cannot
        turn this into a test of the untoggled path.
        """
        before = await trigger_states(db_pool)
        real_repeat_timed = bench.repeat_timed

        async def explode(*args, **kwargs):
            if bench._PENDING_RESTORE:
                raise RuntimeError("simulated mid-run failure")
            return await real_repeat_timed(*args, **kwargs)

        monkeypatch.setattr(bench, "repeat_timed", explode)

        code, _, _ = await run_bench(
            dsn_from(db_params),
            "enqueue",
            "--concurrency",
            "2",
            "--jobs-per-connection",
            "5",
            "--rows",
            "50",
            "--repeat",
            "1",
            "--no-warmup",
            "--allow-trigger-toggle",
        )

        assert code != 0
        assert await trigger_states(db_pool) == before
        assert all(state == "O" for state in (await trigger_states(db_pool)).values())
        # nothing left for the atexit hook to rescue
        assert bench._PENDING_RESTORE == []

    async def test_triggers_are_restored_after_ctrl_c(self, db_params, db_pool):
        """SIGINT a REAL pj-bench process while a trigger is disabled.

        The in-process failure test above cannot reach this path: Ctrl-C
        unwinds through a cancelled event loop, where the ``finally``'s own
        awaits raise immediately, so the restore has to come from the atexit
        hook opening a fresh connection. That is the difference between "the
        benchmark crashed" and "the install stopped recording history until
        someone noticed", so it is worth a real process to prove.
        """
        before = await trigger_states(db_pool)

        proc = spawn(
            "pj-bench",
            "--dsn",
            dsn_from(db_params),
            "enqueue",
            "--concurrency",
            "4",
            "--jobs-per-connection",
            "4000",
            "--rows",
            "2000",
            "--repeat",
            "1",
            "--no-warmup",
            "--allow-trigger-toggle",
        )
        try:
            # interrupt only once something is actually disabled, rather
            # than after a sleep that could land anywhere
            disabled = await wait_until(
                lambda: _disabled_triggers(db_pool),
                timeout=60,
                interval=0.02,
                what="a jorb trigger being disabled",
            )
            assert set(disabled) <= set(TRIGGERS), disabled
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            proc.wait(timeout=30)
        finally:
            terminate(proc)

        assert proc.returncode != 0
        assert await trigger_states(db_pool) == before

    async def test_the_rescue_path_re_enables_on_a_fresh_connection(
        self, db_params, db_pool
    ):
        """The last-resort restore, exercised on its own.

        It has to work from a connection the run no longer owns, which is
        the case the in-band ``finally`` cannot cover — a benchmark whose
        connection died with a trigger disabled. It also must NOT be left to
        an atexit hook: by then ``concurrent.futures`` has shut down and the
        ``asyncio.run`` this needs raises "cannot schedule new futures after
        interpreter shutdown", which is exactly how a disabled trigger
        survives a benchmark.
        """
        dsn = dsn_from(db_params)
        before = await trigger_states(db_pool)
        await db_pool.execute(
            f"ALTER TABLE jorb DISABLE TRIGGER {bench.TRIGGER_HISTORY}"
        )
        assert await _disabled_triggers(db_pool) == [bench.TRIGGER_HISTORY]

        bench._PENDING_RESTORE.append((dsn, (bench.TRIGGER_HISTORY,)))
        try:
            await asyncio.to_thread(bench._restore_pending_triggers)
        finally:
            bench._PENDING_RESTORE.clear()
            await db_pool.execute(
                f"ALTER TABLE jorb ENABLE TRIGGER {bench.TRIGGER_HISTORY}"
            )

        assert await trigger_states(db_pool) == before
        assert bench._PENDING_RESTORE == []


async def _disabled_triggers(pool: asyncpg.Pool) -> list[str]:
    states = await trigger_states(pool)
    return [name for name, state in states.items() if state != "O"]


class TestCleanup:
    """Every subcommand must leave the database exactly as it found it."""

    @pytest.mark.parametrize(
        "args",
        [
            pytest.param(
                [
                    "enqueue",
                    "--concurrency",
                    "2",
                    "--jobs-per-connection",
                    "5",
                    "--rows",
                    "50",
                    "--repeat",
                    "1",
                    "--no-warmup",
                    "--allow-trigger-toggle",
                ],
                id="enqueue",
            ),
            pytest.param(
                [
                    "claim",
                    "--workers",
                    "2",
                    "--jobs",
                    "20",
                    "--repeat",
                    "1",
                    "--no-warmup",
                ],
                id="claim",
            ),
            pytest.param(["notify", "--lifecycles", "5"], id="notify"),
            pytest.param(["plans", "--seed", "500"], id="plans"),
        ],
    )
    async def test_leaves_no_rows_behind(self, db_params, db_pool, args):
        before = await counts(db_pool)
        assert before == dict.fromkeys(before, 0)

        await run_bench(dsn_from(db_params), *args)

        assert await counts(db_pool) == before

    async def test_e2e_also_removes_the_worker_registry_rows_it_created(
        self, db_params, db_pool
    ):
        """The worker processes register themselves; the queue name they
        register under is this run's, so cleanup can reach them exactly."""
        before = await counts(db_pool)

        code, payload, _ = await run_bench(
            dsn_from(db_params),
            "e2e",
            "--jobs",
            "5",
            "--workers",
            "1",
            "--repeat",
            "1",
            "--no-warmup",
            "--timeout",
            "60",
        )

        assert code == 0
        assert payload["cleanup"]["workers_deleted"] >= 1
        assert await counts(db_pool) == before

    async def test_a_run_never_touches_rows_it_did_not_create(self, db_params, db_pool):
        """Someone else's jobs survive a benchmark untouched."""
        await db_pool.execute(
            "INSERT INTO jorb (job_class, kwargs, queue) "
            "SELECT 'other.Job', '{}'::jsonb, 'someone_elses_queue' "
            "FROM generate_series(1, 7)"
        )

        code, _, _ = await run_bench(
            dsn_from(db_params),
            "claim",
            "--workers",
            "2",
            "--jobs",
            "20",
            "--repeat",
            "1",
            "--no-warmup",
            "--max-existing-jobs",
            "10",
        )

        assert code == 0
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = 'someone_elses_queue'"
            )
            == 7
        )
        assert await db_pool.fetchval("SELECT count(*) FROM jorb") == 7


class TestBusyDatabaseGuard:
    """A benchmark that silently competes with real work reports garbage."""

    async def test_refuses_a_populated_database_and_names_the_count(
        self, db_params, db_pool
    ):
        await db_pool.execute(
            "INSERT INTO jorb (job_class, kwargs, queue) "
            "SELECT 'other.Job', '{}'::jsonb, 'busy' FROM generate_series(1, 5)"
        )

        code, _, output = await run_bench(
            dsn_from(db_params),
            "notify",
            "--lifecycles",
            "3",
            "--max-existing-jobs",
            "2",
        )

        assert code == 1
        assert "already holds 5 jobs" in output
        assert "--force" in output

    async def test_force_overrides_and_still_cleans_up(self, db_params, db_pool):
        await db_pool.execute(
            "INSERT INTO jorb (job_class, kwargs, queue) "
            "SELECT 'other.Job', '{}'::jsonb, 'busy' FROM generate_series(1, 5)"
        )

        code, payload, _ = await run_bench(
            dsn_from(db_params),
            "notify",
            "--lifecycles",
            "3",
            "--max-existing-jobs",
            "2",
            "--force",
        )

        assert code == 0
        assert payload["guard"]["existing_jobs"] == 5
        # The observed phase is the one that must have produced traffic: an
        # unobserved lifecycle legitimately emits nothing, so asserting on it
        # would only prove the run happened, not that it measured anything.
        assert payload["per_lifecycle_observed"] > 0
        assert await db_pool.fetchval("SELECT count(*) FROM jorb") == 5


class TestStatisticsHelpers:
    """The numbers the harness reports about its own numbers."""

    def test_summarize_reports_the_median_and_the_spread(self):
        summary = bench.summarize([1.0, 2.0, 9.0])

        assert summary["median"] == 2.0
        assert summary["min"] == 1.0
        assert summary["max"] == 9.0
        assert summary["spread"] == 8.0
        assert summary["runs"] == 3
        # a 400% spread is the signal that the median means nothing
        assert summary["spread_pct"] == pytest.approx(400.0)

    def test_summarize_of_nothing_is_zero_not_an_error(self):
        assert bench.summarize([])["runs"] == 0

    def test_percentiles_interpolate(self):
        values = [float(i) for i in range(1, 101)]

        assert bench.percentile(values, 0.5) == pytest.approx(50.5)
        assert bench.percentile(values, 0.99) == pytest.approx(100.0, abs=1.0)
        assert bench.percentile([], 0.5) == 0.0

    async def test_warmup_is_reported_but_not_measured(self):
        calls: list[int] = []

        async def work() -> None:
            calls.append(1)

        summary = await bench.repeat_timed(work, repeat=3, warmup=True)

        assert len(calls) == 4  # 1 warm-up + 3 measured
        assert summary["runs"] == 3
        assert summary["warmup_seconds"] is not None

    async def test_setup_runs_before_each_measured_run_and_is_not_timed(self):
        order: list[str] = []

        async def setup() -> None:
            order.append("setup")

        async def work() -> None:
            order.append("work")

        await bench.repeat_timed(work, repeat=2, warmup=False, setup=setup)

        assert order == ["setup", "work", "setup", "work"]


class TestTargetResolution:
    """--dsn/--config resolve the way the rest of the platform does."""

    def test_dsn_round_trips_to_connect_parameters(self):
        target = bench.Target.from_dsn("postgresql://user:p%40ss@db.example:6543/jobs")

        assert target.params == {
            "database": "jobs",
            "user": "user",
            "password": "p@ss",
            "host": "db.example",
            "port": 6543,
        }
        assert target.label == "db.example:6543/jobs"

    def test_config_params_round_trip_to_a_dsn(self):
        target = bench.Target.from_params(
            {
                "database": "jobs",
                "user": "user",
                "password": "p@ss",
                "host": "db.example",
                "port": 6543,
            }
        )

        assert target.dsn == "postgresql://user:p%40ss@db.example:6543/jobs"

    def test_a_missing_config_is_a_config_problem_not_a_database_problem(self):
        with pytest.raises(bench.ConfigProblem):
            bench.resolve_target("/nonexistent/pyjobby.conf.py", None)
