"""Soak: sustained load, then measure what accumulated.

The platform's pitch is durability over time -- retention on by default,
jorb_history at ~4 rows per job, durable machines parked for months --
and every number in SCALE.md is short-run. This run holds a steady
arrival rate against a full fleet (workers, monitor with sharply
compressed retention windows, scheduler firing every minute, durable
machines parked mid-sleep) and then asserts the accumulation claims:

* retention keeps up AT RATE: the job table reaches a bounded steady
  state instead of growing with total throughput, and the monitor logs
  "caught up" rather than only budget exhaustion;
* the NOTIFY queue stays near empty under sustained enqueue/complete;
* a parked durable machine costs nothing while parked: no history, no
  claims, no worker;
* the hot-query plans hold on a database that has real churn in it --
  `pj-bench plans` reports no sequential scan at the end (its discard
  budgets are calibrated for its own seeded distribution and are treated
  as data here, not gates);
* doctor still exits 0 with the fleet up.

Excluded by default; duration and rate come from the environment:

    PYJOBBY_SOAK_SECONDS=1800 PYJOBBY_SOAK_RATE=40 \
        poetry run pytest -m soak -s
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time

import pytest

from pyjobby.client import JobClient
from tests.ops.conftest import REPO_ROOT, TOOL_RUNNER, wait_until
from tests.schema_fixtures import dsn_from

SOAK_SECONDS = float(os.environ.get("PYJOBBY_SOAK_SECONDS", "180"))
SOAK_RATE = float(os.environ.get("PYJOBBY_SOAK_RATE", "40"))

pytestmark = [
    pytest.mark.ops,
    pytest.mark.slow,
    pytest.mark.e2e,
    pytest.mark.soak,
    # The suite-wide 300s timeout is sized for tests, not for a soak whose
    # whole point is duration: budget the drive window plus the drain,
    # plans and doctor tail.
    pytest.mark.timeout(SOAK_SECONDS + 600),
]

#: Retention window, compressed so steady state is reachable within the
#: run: terminal jobs older than ~60s are eligible for the sweep.
RETENTION_DAYS = 60 / 86400
CHECKPOINT_RETENTION_DAYS = 30 / 86400

#: The workload mix per 100 jobs: mostly instant successes, some
#: checkpointed pipelines (jorb_step churn + one same-row retry each),
#: some short durable sleeps (requeue churn), a trickle of dead letters.
MIX = [
    ("tests.dxe_jobs.OkJob", 90, {"x": 2}),
    ("tests.dxe_jobs.StepPipelineJob", 4, {}),
    ("tests.dxe_jobs.SleeperJob", 4, {"seconds": 1}),
    ("tests.dxe_jobs.FailJob", 2, {"max_retries": 1, "initial_retry_delay": 1}),
]


class Sample:
    def __init__(self, at: float, **numbers: float):
        self.at = at
        self.numbers = numbers

    def __repr__(self) -> str:
        cells = ", ".join(f"{k}={v:,.0f}" for k, v in self.numbers.items())
        return f"t+{self.at:5.0f}s  {cells}"


async def take_sample(db_pool, started: float, queue: str) -> Sample:
    sizes = await db_pool.fetchrow(
        "SELECT pg_total_relation_size('jorb') AS jorb_bytes, "
        "pg_total_relation_size('jorb_history') AS history_bytes, "
        "pg_total_relation_size('jorb_step') AS step_bytes"
    )
    tuples = await db_pool.fetchrow(
        "SELECT coalesce(sum(n_live_tup), 0) AS live, "
        "coalesce(sum(n_dead_tup), 0) AS dead "
        "FROM pg_stat_user_tables WHERE relname = 'jorb'"
    )
    counts = await db_pool.fetchrow(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE state IN ('finished', 'crashed', 'cancelled')) "
        "AS terminal FROM jorb WHERE queue = $1",
        queue,
    )
    notify = await db_pool.fetchval("SELECT pg_notification_queue_usage()")
    return Sample(
        time.monotonic() - started,
        jorb_rows=counts["total"],
        terminal_rows=counts["terminal"],
        jorb_mb=sizes["jorb_bytes"] / 1e6,
        history_mb=sizes["history_bytes"] / 1e6,
        step_mb=sizes["step_bytes"] / 1e6,
        live_tup=tuples["live"],
        dead_tup=tuples["dead"],
        notify_pct=float(notify) * 100,
    )


class TestSoak:
    async def test_sustained_load_reaches_steady_state_not_growth(
        self, fleet, admin, db_pool, db_params, unique_queue
    ):
        monitor = fleet.monitor(
            "--retention-days",
            str(RETENTION_DAYS),
            "--checkpoint-retention-days",
            str(CHECKPOINT_RETENTION_DAYS),
            check_interval=2.0,
            liveness_grace=10.0,
        )
        fleet.worker(unique_queue, workers=3)
        fleet.scheduler(poll_interval=5)
        added = admin(
            "schedule",
            "add",
            f"soak_report_{unique_queue}",
            "tests.dxe_jobs.OkJob",
            "* * * * *",
            "--queue",
            unique_queue,
        )
        assert added.returncode == 0, added.stdout + added.stderr

        client = JobClient(pool=db_pool)
        # Durable machines parked for the WHOLE run: asleep, holding no
        # worker, costing nothing. Their accounting is asserted at the end.
        parked = [
            await client.enqueue(
                "tests.dxe_jobs.SleeperJob",
                queue=unique_queue,
                seconds=SOAK_SECONDS * 2,
            )
            for _ in range(3)
        ]
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT count(*) = 3 FROM jorb WHERE id = ANY($1) "
                "AND state = 'queued' AND run_after > now()",
                parked,
            ),
            describe="durable machines parked mid-sleep",
            timeout=60,
        )
        parked_history_at_start = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_history WHERE job_id = ANY($1)", parked
        )

        # ------------------------------------------------------------------
        # Drive: a steady arrival rate for the whole window.
        # ------------------------------------------------------------------
        started = time.monotonic()
        samples: list[Sample] = []
        enqueued = 0
        mix: list[tuple[str, dict]] = []
        for job_class, weight, kwargs in MIX:
            mix += [(job_class, kwargs)] * weight

        next_sample = 15.0
        while (elapsed := time.monotonic() - started) < SOAK_SECONDS:
            batch_deadline = time.monotonic() + 1.0
            for _ in range(int(SOAK_RATE)):
                job_class, kwargs = mix[enqueued % len(mix)]
                await client.enqueue(job_class, queue=unique_queue, **kwargs)
                enqueued += 1
            if elapsed >= next_sample:
                samples.append(await take_sample(db_pool, started, unique_queue))
                print(samples[-1])
                next_sample += 15.0
            await asyncio.sleep(max(0.0, batch_deadline - time.monotonic()))

        # Stop producing; let the tail drain through one retention window.
        await asyncio.sleep(75)
        final = await take_sample(db_pool, started, unique_queue)
        samples.append(final)
        print(final)

        # ------------------------------------------------------------------
        # The accumulation claims.
        # ------------------------------------------------------------------
        # Steady state, not growth: what remains is bounded by rate x window
        # (with slack for sweep cadence), not by total throughput.
        steady_bound = SOAK_RATE * 60 * 3
        assert final.numbers["jorb_rows"] < steady_bound, (
            f"{enqueued:,} enqueued but {final.numbers['jorb_rows']:,.0f} rows "
            f"remain (bound {steady_bound:,.0f}): retention is not keeping up"
        )
        # And the monitor itself says so: caught up, not stuck on budget.
        assert "caught up" in monitor.log_text()

        # The cliff stayed far away under sustained NOTIFY traffic.
        assert final.numbers["notify_pct"] < 1.0

        # Parked durable machines: still asleep, never claimed, and the
        # whole park cost at most the enqueue-time trail it started with.
        parked_rows = await db_pool.fetch(
            "SELECT state, run_count FROM jorb WHERE id = ANY($1)", parked
        )
        assert all(
            r["state"] == "queued" and r["run_count"] == 1 for r in parked_rows
        ), [dict(r) for r in parked_rows]
        parked_history_at_end = await db_pool.fetchval(
            "SELECT count(*) FROM jorb_history WHERE job_id = ANY($1)", parked
        )
        # No growth -- and usually SHRINKAGE: the retention window sweeps
        # the history of LIVE jobs too (the documented forever-leak guard
        # for machines that never terminate), so with a compressed window
        # even the enqueue-time trail ages out while the job sleeps on.
        assert parked_history_at_end <= parked_history_at_start, (
            "a parked machine wrote history while parked"
        )

        # The fleet is still healthy by its own executable definition.
        report = admin("doctor", "--max-age-minutes", "10")
        assert report.returncode == 0, report.stdout + report.stderr

        # Plan drift: every hot query must still hold its index plan against
        # a database with real churn in it. The hard claim is NO SEQUENTIAL
        # SCANS; the discard budgets are calibrated to pj-bench's own seeded
        # workload distribution (so many jobs checkpointed, so many DAGs
        # populated), which this run's residue deliberately does not match --
        # an overrun there is reported as data, not failed on.
        #
        # The fleet comes DOWN first: plans seeds thousands of terminal jobs
        # and retired workers to measure against, and a live monitor with
        # this run's 60-second retention window reaps that seed out from
        # under the measurement.
        fleet.destroy_all()
        plans = subprocess.run(
            [
                *TOOL_RUNNER,
                "pj-bench",
                "--dsn",
                dsn_from(db_params),
                "plans",
                "--force",
                "--json",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert plans.returncode == 0, plans.stderr[-2000:] + plans.stdout[-1000:]
        verdict = json.loads(plans.stdout)
        assert verdict["seq_scan_offenders"] == [], verdict["seq_scan_offenders"]
        if verdict["discard_offenders"]:
            print(
                "discard budgets exceeded on soaked data (calibrated for the "
                f"seeded distribution): {verdict['discard_offenders']}"
            )

        print(
            f"\nsoak: {enqueued:,} jobs over {SOAK_SECONDS:.0f}s "
            f"at {SOAK_RATE:.0f}/s nominal"
        )
