"""`pj-admin jobs why ID` — one command for "why is this job not running?".

The verb's contract is that its answers are exactly the ways `claim_jorb()`
(pyjobby/sql/schema/30_claim.sql) can decline a row, plus the states a row can
be in before it is a candidate at all. So this file is organised by REASON
CODE: one test per entry in `admin_api.EXPLAIN_REASONS`, each constructing the
state directly — a job inserted in that state, a paused queue, a filled
concurrency cap, a worker registered with a low ceiling — and asserting both
the code and that the DETAILS name the right thing. A reason code that comes
out right while its details name the wrong job, the wrong capability or the
wrong cap is worse than no answer: the operator acts on the details.

It is a file of its own rather than another class in tests/test_admin_api*.py
because the reason table, the fixtures that build each blocked state, and the
CLI surface that prints them are one subject; the admin_api files are already
split by API area (jobs, queues, schedules) and this cuts across all three.

The exhaustiveness test at the bottom is the one that keeps the set honest:
every `jorbstate` value must get SOME reason from the table. A state that
falls through to an empty answer is the silence this verb exists to end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from pyjobby.admin_api import EXPLAIN_REASONS, UNCLAIMABLE_REASONS, AdminAPI
from pyjobby.client import DEFAULT_PRIO_CEILING
from pyjobby.lifecycle import JOB_STATES

from .test_cli_errors import dsn_for, run_cli

pytestmark = pytest.mark.asyncio


@pytest.fixture
def admin_api(db_connection) -> AdminAPI:
    return AdminAPI(db_connection)


async def make_job(conn, queue: str, **cols) -> int:
    """Insert one job row with exactly the columns a test names.

    Built from the caller's kwargs rather than from a fixed column list: the
    states this file constructs differ by which columns are set (waitfor_job,
    claimed_by, run_after, timeout_at), and a positional helper covering all
    of them is unreadable at every call site.
    """
    values: dict = {
        "job_class": "tests.dxe_jobs.OkJob",
        "queue": queue,
        "state": "queued",
        "prio": 100,
    }
    values.update(cols)
    names = list(values)
    placeholders = ", ".join(f"${i}" for i in range(1, len(names) + 1))
    return await conn.fetchval(
        f"INSERT INTO jorb ({', '.join(names)}) VALUES ({placeholders}) RETURNING id",
        *values.values(),
    )


async def make_worker(
    conn,
    queue: str,
    *,
    capabilities: tuple[str, ...] = ("test",),
    max_prio: int = DEFAULT_PRIO_CEILING,
    host: str = "worker-host",
    pid: int = 4242,
) -> int:
    """Register a live worker on `queue`, as pj.py's WORKER_REGISTER_SQL does."""
    return await conn.fetchval(
        """INSERT INTO jorb_worker (host, pid, queue, capabilities, max_prio)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        host,
        pid,
        queue,
        list(capabilities),
        max_prio,
    )


async def pause(conn, queue: str) -> None:
    await conn.execute("INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)", queue)


# ============================================================================
# The pre-claim states: a row claim_jorb never looks at
# ============================================================================


class TestTerminal:
    async def test_finished_says_when(self, admin_api, db_connection, unique_queue):
        job = await make_job(
            db_connection,
            unique_queue,
            state="finished",
            finished=datetime.now(UTC),
            run_count=1,
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "finished"
        assert answer["details"]["terminal_at"] is not None
        assert answer["details"]["run_count"] == 1

    async def test_crashed_carries_the_error(
        self, admin_api, db_connection, unique_queue
    ):
        job = await make_job(
            db_connection,
            unique_queue,
            state="crashed",
            finished=datetime.now(UTC),
            error_message="ZeroDivisionError: division by zero",
            error_count=3,
            run_count=3,
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "crashed"
        assert answer["details"]["error_message"] == (
            "ZeroDivisionError: division by zero"
        )
        assert answer["details"]["error_count"] == 3
        assert "division by zero" in answer["summary"]

    async def test_cancelled(self, admin_api, db_connection, unique_queue):
        job = await make_job(
            db_connection,
            unique_queue,
            state="cancelled",
            finished=datetime.now(UTC),
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "cancelled"
        assert answer["details"]["terminal_at"] is not None


class TestInFlight:
    async def test_claimed_names_the_worker_and_its_deadline(
        self, admin_api, db_connection, unique_queue
    ):
        worker = await make_worker(db_connection, unique_queue, host="host-a", pid=99)
        now = datetime.now(UTC)
        job = await make_job(
            db_connection,
            unique_queue,
            state="claimed",
            claimed_by=worker,
            worker_host="host-a",
            worker_pid=99,
            claimed_at=now,
            timeout_at=now + timedelta(seconds=3600),
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "claimed"
        assert answer["details"]["worker_id"] == worker
        assert answer["details"]["worker_host"] == "host-a"
        assert answer["details"]["worker_pid"] == 99
        assert answer["details"]["worker_live"] is True
        assert answer["details"]["timeout_at"] is not None

    async def test_running_since_when(self, admin_api, db_connection, unique_queue):
        worker = await make_worker(db_connection, unique_queue)
        now = datetime.now(UTC)
        job = await make_job(
            db_connection,
            unique_queue,
            state="running",
            claimed_by=worker,
            worker_host="worker-host",
            worker_pid=4242,
            claimed_at=now,
            started=now,
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "running"
        assert answer["details"]["started"] is not None
        assert answer["details"]["worker_live"] is True

    async def test_a_dead_worker_is_named_as_dead(
        self, admin_api, db_connection, unique_queue
    ):
        """The answer to "claimed an hour ago and nothing happened"."""
        worker = await db_connection.fetchval(
            """INSERT INTO jorb_worker (host, pid, queue, last_seen)
               VALUES ('gone', 1, $1, now() - interval '1 hour') RETURNING id""",
            unique_queue,
        )
        job = await make_job(
            db_connection,
            unique_queue,
            state="running",
            claimed_by=worker,
            worker_host="gone",
            worker_pid=1,
            claimed_at=datetime.now(UTC),
            started=datetime.now(UTC),
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "running"
        assert answer["details"]["worker_live"] is False
        assert "NOT heartbeating" in answer["summary"]


class TestWaiting:
    async def test_waiting_on_job_names_the_blocker_and_its_state(
        self, admin_api, db_connection, unique_queue
    ):
        upstream = await make_job(db_connection, unique_queue, state="running")
        job = await make_job(
            db_connection, unique_queue, state="waiting", waitfor_job=upstream
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "waiting_on_job"
        assert answer["details"]["blocking_job_id"] == upstream
        assert answer["details"]["blocking_job_state"] == "running"

    async def test_waiting_on_a_crashed_job_says_it_will_never_wake(
        self, admin_api, db_connection, unique_queue
    ):
        """Both wakes fire only on 'finished', so a crashed upstream is fatal."""
        upstream = await make_job(db_connection, unique_queue, state="crashed")
        job = await make_job(
            db_connection, unique_queue, state="waiting", waitfor_job=upstream
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "waiting_on_job"
        assert answer["details"]["blocking_job_state"] == "crashed"
        assert "never start" in answer["summary"]

    async def test_waiting_on_group_counts_the_unfinished_members(
        self, admin_api, db_connection, unique_queue
    ):
        group = 7717
        await make_job(db_connection, unique_queue, state="finished", run_group=group)
        await make_job(db_connection, unique_queue, state="running", run_group=group)
        await make_job(db_connection, unique_queue, state="queued", run_group=group)
        job = await make_job(
            db_connection, unique_queue, state="waiting", waitfor_group=group
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "waiting_on_group"
        assert answer["details"]["blocking_group"] == group
        assert answer["details"]["unfinished_members"] == 2
        assert answer["details"]["unfinished_members_capped"] is False

    async def test_waiting_on_nothing_is_reported_as_unwakeable(
        self, admin_api, db_connection, unique_queue
    ):
        job = await make_job(db_connection, unique_queue, state="waiting")
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "waiting_unblocked"
        assert "no completion anywhere can wake it" in answer["summary"]


# ============================================================================
# The queued row: every condition claim_jorb applies to it
# ============================================================================


class TestQueuedRowPredicate:
    async def test_deferred_says_how_long(self, admin_api, db_connection, unique_queue):
        job = await make_job(
            db_connection,
            unique_queue,
            run_after=datetime.now(UTC) + timedelta(seconds=900),
        )
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "deferred"
        # Bounded rather than exact: run_after is computed by this process and
        # the remaining span by the database, so the two clocks differ by the
        # round trip. 900 +/- 10s is the assertion that means "it reported the
        # deferral, in seconds" without pinning a clock skew.
        assert 890 < answer["details"]["seconds_until_run_after"] < 910

    async def test_above_every_live_workers_ceiling(
        self, admin_api, db_connection, unique_queue
    ):
        """The silent one: healthy row, live fleet, and nobody can see it."""
        await make_worker(db_connection, unique_queue, max_prio=50)
        await make_worker(db_connection, unique_queue, max_prio=200, pid=4243)
        job = await make_job(db_connection, unique_queue, prio=900)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "above_worker_ceiling"
        assert answer["details"]["prio"] == 900
        assert answer["details"]["max_live_ceiling"] == 200
        assert answer["details"]["live_workers"] == 2
        assert answer["details"]["workers_at_or_above_prio"] == 0

    async def test_capability_nobody_advertises_names_what_is_advertised(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(db_connection, unique_queue, capabilities=("cpu", "test"))
        await make_worker(db_connection, unique_queue, capabilities=("cpu",), pid=4243)
        job = await make_job(db_connection, unique_queue, capability="gpu")
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "capability_unmet"
        assert answer["details"]["capability"] == "gpu"
        assert answer["details"]["workers_with_capability"] == 0
        assert answer["details"]["advertised_capabilities"] == ["cpu", "test"]

    async def test_a_capability_that_IS_advertised_does_not_block(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(db_connection, unique_queue, capabilities=("gpu",))
        job = await make_job(db_connection, unique_queue, capability="gpu")
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "claimable"


class TestFleet:
    async def test_no_live_workers_on_the_queue(
        self, admin_api, db_connection, unique_queue
    ):
        # A live worker on ANOTHER queue must not count as capacity here.
        await make_worker(db_connection, f"{unique_queue}_other")
        job = await make_job(db_connection, unique_queue)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "no_live_workers"
        assert answer["details"]["queue"] == unique_queue
        assert answer["details"]["live_workers"] == 0

    async def test_a_stale_heartbeat_is_not_a_live_worker(
        self, admin_api, db_connection, unique_queue
    ):
        await db_connection.execute(
            """INSERT INTO jorb_worker (host, pid, queue, last_seen)
               VALUES ('stale', 1, $1, now() - interval '1 hour')""",
            unique_queue,
        )
        job = await make_job(db_connection, unique_queue)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "no_live_workers"


class TestQueueControlPlane:
    async def test_paused(self, admin_api, db_connection, unique_queue):
        await pause(db_connection, unique_queue)
        await make_worker(db_connection, unique_queue)
        job = await make_job(db_connection, unique_queue)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "queue_paused"
        assert answer["details"]["paused"] is True
        assert answer["details"]["queue"] == unique_queue

    async def test_at_max_concurrency_reports_the_cap_numbers(
        self, admin_api, db_connection, unique_queue
    ):
        await db_connection.execute(
            "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, 2)",
            unique_queue,
        )
        await make_worker(db_connection, unique_queue)
        await make_job(db_connection, unique_queue, state="running")
        await make_job(db_connection, unique_queue, state="claimed")
        job = await make_job(db_connection, unique_queue)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "queue_at_max_concurrency"
        assert answer["details"]["max_concurrency"] == 2
        assert answer["details"]["inflight"] == 2

    async def test_rate_limited_reports_the_window_and_admissions(
        self, admin_api, db_connection, unique_queue
    ):
        await db_connection.execute(
            """INSERT INTO jorb_queue (name, rate_limit, rate_period_seconds)
               VALUES ($1, 1, 30)""",
            unique_queue,
        )
        await make_worker(db_connection, unique_queue)
        # An admission, counted by claimed_at exactly as claim_jorb counts it
        # -- the job it belonged to has already finished.
        await make_job(
            db_connection,
            unique_queue,
            state="finished",
            claimed_at=datetime.now(UTC),
        )
        job = await make_job(db_connection, unique_queue)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "rate_limited"
        assert answer["details"]["rate_limit"] == 1
        assert answer["details"]["rate_period_seconds"] == 30.0
        assert answer["details"]["recent_admissions"] == 1

    async def test_an_admission_outside_the_window_does_not_limit(
        self, admin_api, db_connection, unique_queue
    ):
        await db_connection.execute(
            """INSERT INTO jorb_queue (name, rate_limit, rate_period_seconds)
               VALUES ($1, 1, 30)""",
            unique_queue,
        )
        await make_worker(db_connection, unique_queue)
        await db_connection.execute(
            """INSERT INTO jorb (job_class, queue, state, claimed_at)
               VALUES ('x.Y', $1, 'finished', now() - interval '10 minutes')""",
            unique_queue,
        )
        job = await make_job(db_connection, unique_queue)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "claimable"


class TestClaimable:
    async def test_claimable_counts_the_jobs_ahead_of_it(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(db_connection, unique_queue)
        await make_job(db_connection, unique_queue, prio=10)
        await make_job(db_connection, unique_queue, prio=50)
        # Same prio but enqueued later, so it sorts BEHIND: claim order is
        # (prio, run_after), which is what "ahead" has to mean.
        await make_job(
            db_connection,
            unique_queue,
            prio=100,
            run_after=datetime.now(UTC) + timedelta(seconds=-1),
        )
        job = await make_job(db_connection, unique_queue, prio=100)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "claimable"
        assert answer["details"]["jobs_ahead"] == 3
        assert answer["details"]["jobs_ahead_capped"] is False
        assert answer["details"]["live_workers"] == 1
        assert answer["details"]["queue_serialised"] is False

    async def test_the_head_of_an_empty_queue_is_next(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(db_connection, unique_queue)
        job = await make_job(db_connection, unique_queue)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "claimable"
        assert answer["details"]["jobs_ahead"] == 0
        assert "next in claim order" in answer["summary"]

    async def test_a_controlled_queue_says_claims_are_serialised(
        self, admin_api, db_connection, unique_queue
    ):
        """The advisory lock and SKIP LOCKED are not reasons; they are here."""
        await db_connection.execute(
            "INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, 10)",
            unique_queue,
        )
        await make_worker(db_connection, unique_queue)
        job = await make_job(db_connection, unique_queue)
        answer = await admin_api.explain_job(job)

        assert answer["reason"] == "claimable"
        assert answer["details"]["queue_serialised"] is True


# ============================================================================
# The fleet-wide sweep: `unclaimable_jobs`
# ============================================================================
# The proactive counterpart to the two reason codes above. `explain_job`
# answers for ONE job somebody already suspects; this one FINDS them, so it
# belongs to the same subject and is tested against the same fixtures and the
# same reason vocabulary. What it must never do is disagree with `explain_job`
# about which cause a job has -- the two send the operator to different
# remedies -- so several of these assert both verbs on the same row.


class TestUnclaimableSweep:
    async def test_a_job_above_every_live_ceiling_is_found(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(db_connection, unique_queue, max_prio=50)
        await make_worker(db_connection, unique_queue, max_prio=200, pid=4243)
        low = await make_job(db_connection, unique_queue, prio=300)
        high = await make_job(db_connection, unique_queue, prio=900)

        report = await admin_api.unclaimable_jobs()

        assert len(report) == 1
        (entry,) = report
        assert entry["queue"] == unique_queue
        assert entry["reason"] == "above_worker_ceiling"
        assert entry["count"] == 2
        assert entry["count_capped"] is False
        assert entry["live_workers"] == 2
        assert entry["sample_job_ids"] == [low, high]  # claim order: prio, id
        assert entry["details"] == {
            "max_live_ceiling": 200,
            "lowest_blocked_prio": 300,
            "highest_blocked_prio": 900,
        }
        # and the per-job verb agrees about the cause
        assert (await admin_api.explain_job(high))["reason"] == entry["reason"]

    async def test_a_capability_nobody_advertises_is_found(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(db_connection, unique_queue, capabilities=("cpu", "test"))
        job = await make_job(db_connection, unique_queue, capability="gpu")

        report = await admin_api.unclaimable_jobs()

        (entry,) = report
        assert entry["queue"] == unique_queue
        assert entry["reason"] == "capability_unmet"
        assert entry["count"] == 1
        assert entry["sample_job_ids"] == [job]
        assert entry["details"] == {
            "missing_capabilities": ["gpu"],
            "advertised_capabilities": ["cpu", "test"],
        }
        assert (await admin_api.explain_job(job))["reason"] == entry["reason"]

    async def test_claimable_work_is_not_reported(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(
            db_connection, unique_queue, capabilities=("gpu",), max_prio=100
        )
        await make_job(db_connection, unique_queue, prio=100, capability="gpu")
        await make_job(db_connection, unique_queue, prio=1)

        assert await admin_api.unclaimable_jobs() == []

    async def test_prio_equal_to_the_ceiling_is_claimable(
        self, admin_api, db_connection, unique_queue
    ):
        """claim_jorb admits prio <= ceiling, so the boundary is not blocked."""
        await make_worker(db_connection, unique_queue, max_prio=100)
        await make_job(db_connection, unique_queue, prio=100)

        assert await admin_api.unclaimable_jobs() == []

    async def test_an_idle_database_is_empty(self, admin_api):
        assert await admin_api.unclaimable_jobs() == []

    async def test_a_queue_with_no_live_workers_is_not_this_checks_business(
        self, admin_api, db_connection, unique_queue
    ):
        """The deliberate exclusion (see AdminAPI.unclaimable_jobs).

        Every job on a workerless queue is trivially unclaimable, so including
        them would restate the worker check for every idle queue in the
        install and bury the condition this verb exists to find. The remedies
        differ too: start a worker vs. change what the running ones accept.
        `explain_job` still answers for the individual job.
        """
        job = await make_job(db_connection, unique_queue, prio=900, capability="gpu")

        assert await admin_api.unclaimable_jobs() == []
        assert (await admin_api.explain_job(job))["reason"] == "no_live_workers"

    async def test_a_stale_heartbeat_is_not_a_live_worker(
        self, admin_api, db_connection, unique_queue
    ):
        """Same liveness grace as the rest of the platform: a queue whose only
        worker stopped heartbeating has no live fleet, so it drops out by the
        rule above rather than reporting every job on it."""
        await db_connection.execute(
            """INSERT INTO jorb_worker (host, pid, queue, max_prio, last_seen)
               VALUES ('stale', 1, $1, 10, now() - interval '1 hour')""",
            unique_queue,
        )
        await make_job(db_connection, unique_queue, prio=900)

        assert await admin_api.unclaimable_jobs() == []

    async def test_deferred_work_is_not_unclaimable_yet(
        self, admin_api, db_connection, unique_queue
    ):
        """CLAIMABLE-NOW only: a job on retry backoff or a scheduled batch is
        invisible on purpose, and its ceiling stops mattering until it is
        due."""
        await make_worker(db_connection, unique_queue, max_prio=10)
        await make_job(
            db_connection,
            unique_queue,
            prio=900,
            run_after=datetime.now(UTC) + timedelta(hours=1),
        )

        assert await admin_api.unclaimable_jobs() == []

    async def test_only_queued_rows_count(self, admin_api, db_connection, unique_queue):
        """A crashed row above the ceiling is in the DLQ, not silent."""
        await make_worker(db_connection, unique_queue, max_prio=10)
        for state in ("crashed", "cancelled", "finished", "running", "waiting"):
            await make_job(db_connection, unique_queue, state=state, prio=900)

        assert await admin_api.unclaimable_jobs() == []

    async def test_the_two_causes_are_disjoint_and_ordered_like_jobs_why(
        self, admin_api, db_connection, unique_queue
    ):
        """A job that is BOTH is counted once, under the cause `explain_job`
        headlines -- otherwise the two verbs disagree and the operator is
        pointed at the wrong fix."""
        await make_worker(
            db_connection, unique_queue, max_prio=100, capabilities=("cpu",)
        )
        both = await make_job(db_connection, unique_queue, prio=900, capability="gpu")

        report = await admin_api.unclaimable_jobs()

        assert [(e["reason"], e["count"]) for e in report] == [
            ("above_worker_ceiling", 1)
        ]
        assert report[0]["sample_job_ids"] == [both]
        assert (await admin_api.explain_job(both))["reason"] == "above_worker_ceiling"

    async def test_each_queue_and_cause_is_its_own_record(
        self, admin_api, db_connection, unique_queue
    ):
        other = f"{unique_queue}_b"
        await make_worker(
            db_connection, unique_queue, max_prio=100, capabilities=("cpu",)
        )
        await make_worker(db_connection, other, max_prio=100, pid=4243)
        await make_job(db_connection, unique_queue, prio=900)
        await make_job(db_connection, unique_queue, capability="gpu")
        await make_job(db_connection, other, prio=900)

        report = await admin_api.unclaimable_jobs()

        assert [(e["queue"], e["reason"], e["count"]) for e in report] == [
            (unique_queue, "above_worker_ceiling", 1),
            (unique_queue, "capability_unmet", 1),
            (other, "above_worker_ceiling", 1),
        ]

    async def test_a_worker_on_another_queue_is_not_capacity_here(
        self, admin_api, db_connection, unique_queue
    ):
        """The fleet is read per queue: a generous ceiling elsewhere must not
        make this queue's work look claimable."""
        await make_worker(db_connection, unique_queue, max_prio=10)
        await make_worker(db_connection, f"{unique_queue}_other", max_prio=9999)
        await make_job(db_connection, unique_queue, prio=900)

        report = await admin_api.unclaimable_jobs()

        assert len(report) == 1
        assert report[0]["details"]["max_live_ceiling"] == 10

    async def test_the_count_and_the_sample_are_both_bounded(
        self, admin_api, db_connection, unique_queue
    ):
        """The operator needs examples and a magnitude, not a dump: past the
        scan limit the count is reported as capped."""
        await make_worker(db_connection, unique_queue, max_prio=10)
        for _ in range(6):
            await make_job(db_connection, unique_queue, prio=900)

        report = await admin_api.unclaimable_jobs(scan_limit=4, sample_limit=2)

        (entry,) = report
        assert entry["count"] == 4
        assert entry["count_capped"] is True
        assert len(entry["sample_job_ids"]) == 2

    async def test_every_reason_it_emits_is_in_the_reason_table(
        self, admin_api, db_connection, unique_queue
    ):
        """The vocabulary is shared with `jobs why`, not parallel to it."""
        await make_worker(
            db_connection, unique_queue, max_prio=100, capabilities=("cpu",)
        )
        await make_job(db_connection, unique_queue, prio=900)
        await make_job(db_connection, unique_queue, capability="gpu")

        report = await admin_api.unclaimable_jobs()

        assert [e["reason"] for e in report] == list(UNCLAIMABLE_REASONS)
        assert all(e["reason"] in EXPLAIN_REASONS for e in report)

    async def test_the_report_is_json_serialisable(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(db_connection, unique_queue, capabilities=("cpu",))
        await make_job(db_connection, unique_queue, capability="gpu")

        report = await admin_api.unclaimable_jobs()

        assert json.loads(json.dumps(report)) == report


# ============================================================================
# The shape of the answer
# ============================================================================


class TestAnswerShape:
    async def test_a_missing_job_is_None(self, admin_api):
        assert await admin_api.explain_job(999_999_999) is None

    async def test_every_state_gets_a_reason_from_the_table(
        self, admin_api, db_connection, unique_queue
    ):
        """No jorbstate falls through to an empty or unknown answer.

        This is the exhaustiveness that matters: the verb may not have a hole
        for a state the platform can actually put a job in, and every code it
        emits must be one the reason table documents against a claim_jorb
        condition.
        """
        for state in JOB_STATES:
            job = await make_job(db_connection, unique_queue, state=state)
            answer = await admin_api.explain_job(job)

            assert answer is not None, state
            assert answer["state"] == state
            assert answer["reason"] in EXPLAIN_REASONS, (
                f"state {state!r} answered with an undocumented reason "
                f"{answer['reason']!r}"
            )
            assert answer["summary"].strip(), state

    async def test_the_answer_carries_the_identifying_facts(
        self, admin_api, db_connection, unique_queue
    ):
        await make_worker(db_connection, unique_queue, capabilities=("gpu",))
        job = await make_job(db_connection, unique_queue, prio=42, capability="gpu")
        answer = await admin_api.explain_job(job)

        assert answer["job_id"] == job
        assert answer["state"] == "queued"
        assert answer["queue"] == unique_queue
        assert answer["job_class"] == "tests.dxe_jobs.OkJob"
        assert answer["prio"] == 42
        assert answer["capability"] == "gpu"
        assert answer["run_after"] and answer["created"] and answer["updated"]

    async def test_the_whole_answer_is_json_serialisable(
        self, admin_api, db_connection, unique_queue
    ):
        """--json is the point of the structured form; no default=str rescue."""
        worker = await make_worker(db_connection, unique_queue)
        job = await make_job(
            db_connection,
            unique_queue,
            state="running",
            claimed_by=worker,
            claimed_at=datetime.now(UTC),
            started=datetime.now(UTC),
            timeout_at=datetime.now(UTC) + timedelta(hours=1),
        )
        answer = await admin_api.explain_job(job)

        assert json.loads(json.dumps(answer))["reason"] == "running"


# ============================================================================
# `pj-admin jobs why` — the CLI surface
# ============================================================================


class TestCLI:
    async def test_text_and_json_agree_on_the_reason(
        self, db_pool, db_params, unique_queue
    ):
        job = await make_job(
            db_pool,
            unique_queue,
            state="crashed",
            error_message="boom",
            finished=datetime.now(UTC),
        )
        dsn = dsn_for(db_params)

        text = await run_cli("--dsn", dsn, "jobs", "why", str(job))
        assert text.exit_code == 0, text.output
        assert "crashed" in text.output
        assert "boom" in text.output

        as_json = await run_cli("--dsn", dsn, "jobs", "why", str(job), "--json")
        assert as_json.exit_code == 0, as_json.output
        answer = json.loads(as_json.output)
        assert answer["reason"] == "crashed"
        assert answer["job_id"] == job
        assert answer["details"]["error_message"] == "boom"

    async def test_the_details_are_printed_as_an_indented_block(
        self, db_pool, db_params, unique_queue
    ):
        await make_worker(db_pool, unique_queue, max_prio=10)
        job = await make_job(db_pool, unique_queue, prio=999)

        result = await run_cli("--dsn", dsn_for(db_params), "jobs", "why", str(job))

        assert result.exit_code == 0, result.output
        assert "above_worker_ceiling" in result.output
        assert "    max_live_ceiling:" in result.output

    async def test_a_missing_job_exits_1(self, db_params):
        result = await run_cli("--dsn", dsn_for(db_params), "jobs", "why", "999999999")

        assert result.exit_code == 1
        assert "not found" in result.output

    async def test_a_missing_job_exits_1_with_json_too(self, db_params):
        """--json must not turn a missing job into a zero exit and no output."""
        result = await run_cli(
            "--dsn", dsn_for(db_params), "jobs", "why", "999999999", "--json"
        )

        assert result.exit_code == 1
        assert "not found" in result.output
