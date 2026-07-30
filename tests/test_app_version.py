"""App-version pinning: `jorb.app_version` and `jorb_worker.app_version`.

A rolling deploy replaces the code under jobs that are already in flight. Most
of the time that is fine -- DXE's NondeterminismError catches a resumed job
whose step sequence really did change -- but a deployment that cannot accept
the risk needs to say "this job's remaining work belongs to THIS build". That
is a pin: the job carries a version, and only a worker advertising the same one
may claim it.

What this file pins, in the order the claim path meets it:

* **The gate is OPT-IN PER JOB, and one rule.** An unpinned job (the default)
  is claimable by EVERY worker, versioned ones included; a pinned job is
  claimable only by a worker advertising its version. There is no worker-side
  "require a match" flag, so there is no matrix -- and the cell such a flag
  would add (a versioned worker refusing unpinned work) is asserted NOT to
  exist, because it would stop a fleet draining its own backlog mid-deploy.
* **Both claim branches enforce it**, plain and partitioned: the partitioned
  claim is a separate statement (see sql/schema/30_claim.sql) and a gate added
  to only one of them would be off for every queue with per-lane limits.
* **Stranding is loud**, which is the whole difference between this and a pin
  nobody can find: doctor's unclaimable sweep and `jobs why` are covered in
  tests/test_jobs_why.py against the same fixtures as the other two causes, and
  the live fleet's version of all three is in
  tests/ops/test_workload_visibility.py.
* **Both remedies actually free the job**: repin it to a version that IS
  running, or clear the pin.
* **The stamp reaches the row from every enqueue path**, survives what reuses
  the row (retry, rerun) and is NOT inherited by a fork -- a fork is how work
  is re-run under new code, so inheriting the old pin would strand it.
"""

from __future__ import annotations

import pytest

from pyjobby.admin_api import AdminAPI
from pyjobby.client import (
    MAX_APP_VERSION_LENGTH,
    JobClient,
    validate_app_version,
)
from pyjobby.db import fork_job, retry_job
from pyjobby.pj import STMTS, resolve_app_version

from .test_cli_errors import dsn_for, run_cli

pytestmark = pytest.mark.asyncio


async def claim_as(
    conn,
    queue: str,
    *,
    app_version: str | None = None,
    caps: tuple[str, ...] = ("test",),
    prio: int = 1000,
    worker_pid: int = 4242,
):
    """Claim through the REAL claim statement a worker uses.

    ``app_version`` is what the claiming worker advertises, which is the only
    thing these tests vary -- everything else is the ordinary claim, so a job
    refused here is refused for the version and nothing else.
    """
    return await conn.fetchrow(
        STMTS["claim"],
        worker_pid,
        "app-version-test",
        queue,
        list(caps),
        prio,
        None,
        app_version,
    )


async def pinned_job(conn, queue: str, version: str | None, **cols) -> int:
    """One claimable job on `queue`, pinned to `version` (None = unpinned)."""
    values: dict = {
        "job_class": "tests.dxe_jobs.OkJob",
        "kwargs": {},
        "queue": queue,
        "capability": "test",
        "state": "queued",
        "app_version": version,
    }
    values.update(cols)
    names = list(values)
    placeholders = ", ".join(f"${i}" for i in range(1, len(names) + 1))
    return await conn.fetchval(
        f"INSERT INTO jorb ({', '.join(names)}) VALUES ({placeholders}) RETURNING id",
        *values.values(),
    )


async def version_of(conn, job_id: int) -> str | None:
    return await conn.fetchval("SELECT app_version FROM jorb WHERE id = $1", job_id)


# ============================================================================
# The rule: the JOB decides whether it is pinned
# ============================================================================


class TestTheClaimGate:
    async def test_unpinned_work_is_claimable_by_an_unversioned_worker(
        self, db_connection, unique_queue
    ):
        job = await pinned_job(db_connection, unique_queue, None)

        claimed = await claim_as(db_connection, unique_queue)

        assert claimed is not None and claimed["id"] == job

    async def test_unpinned_work_is_claimable_by_a_VERSIONED_worker(
        self, db_connection, unique_queue
    ):
        """The cell a worker-side "require a match" flag would break.

        A fleet mid-deploy runs versioned workers, and the queue is full of
        ordinary unpinned work. If advertising a version narrowed what a worker
        accepts, the deploy would stop the fleet draining its own backlog --
        so the pin is the JOB's to declare and this must claim.
        """
        job = await pinned_job(db_connection, unique_queue, None)

        claimed = await claim_as(db_connection, unique_queue, app_version="v2")

        assert claimed is not None and claimed["id"] == job

    async def test_pinned_work_is_claimable_by_the_matching_worker(
        self, db_connection, unique_queue
    ):
        job = await pinned_job(db_connection, unique_queue, "v2")

        claimed = await claim_as(db_connection, unique_queue, app_version="v2")

        assert claimed is not None and claimed["id"] == job

    async def test_pinned_work_is_refused_to_a_MISMATCHED_worker(
        self, db_connection, unique_queue
    ):
        await pinned_job(db_connection, unique_queue, "v2")

        assert await claim_as(db_connection, unique_queue, app_version="v1") is None

    async def test_pinned_work_is_refused_to_an_UNVERSIONED_worker(
        self, db_connection, unique_queue
    ):
        """The state a fleet is in BEFORE anybody sets --app-version, and the
        one that strands work: an unversioned worker advertises no version, so
        it matches no pin."""
        await pinned_job(db_connection, unique_queue, "v2")

        assert await claim_as(db_connection, unique_queue) is None

    async def test_a_mismatched_worker_takes_the_unpinned_work_beside_it(
        self, db_connection, unique_queue
    ):
        """The pin holds back the pinned row and NOTHING else. A gate that
        stopped the claim at the first refused row would hide the whole queue
        behind one job -- the failure mode that made partition_limits a
        separate statement."""
        pinned = await pinned_job(db_connection, unique_queue, "v2", prio=1)
        free = await pinned_job(db_connection, unique_queue, None, prio=2)

        claimed = await claim_as(db_connection, unique_queue, app_version="v1")

        assert claimed is not None and claimed["id"] == free
        assert await version_of(db_connection, pinned) == "v2"

    async def test_the_partitioned_claim_enforces_it_too(
        self, db_connection, unique_queue
    ):
        """The partitioned claim is a SECOND statement (30_claim.sql), so the
        gate has to be in both -- otherwise it is silently off for every queue
        with per-lane limits."""
        await AdminAPI(db_connection).set_queue_control(
            unique_queue, max_concurrency=4, partition_limits=True
        )
        pinned = await pinned_job(
            db_connection, unique_queue, "v2", partition_key="tenant-a"
        )

        assert await claim_as(db_connection, unique_queue, app_version="v1") is None

        claimed = await claim_as(db_connection, unique_queue, app_version="v2")
        assert claimed is not None and claimed["id"] == pinned

    async def test_an_empty_string_pin_is_claimable_by_nobody(
        self, db_connection, unique_queue
    ):
        """Why validate_app_version refuses '': it is a value, not the absence
        of one, and no worker can advertise it (`--app-version ""` is the same
        as passing nothing). Written here through raw SQL, because the client
        will not let a caller create this row."""
        await pinned_job(db_connection, unique_queue, "")

        assert await claim_as(db_connection, unique_queue) is None
        assert await claim_as(db_connection, unique_queue, app_version="") is not None


# ============================================================================
# Live workers: a mismatched fleet idles, a matching one drains
# ============================================================================


class TestLiveWorkers:
    async def test_a_worker_publishes_the_version_it_advertises(
        self, live_worker, db_connection, unique_queue
    ):
        """Registered like capabilities and max_prio, and for the same reason:
        nothing else in the platform could answer "does any live worker here
        run v2?"."""
        worker = await live_worker(app_version="v2")

        row = await db_connection.fetchrow(
            "SELECT app_version, version FROM jorb_worker WHERE id = $1",
            worker.worker_id,
        )
        assert row["app_version"] == "v2"
        # NOT the same column as the pyjobby library version beside it
        assert row["version"] != "v2"

    async def test_the_wrong_version_idles_while_the_right_one_drains(
        self, live_worker, db_pool, unique_queue
    ):
        """The whole feature, with real workers: a v1 worker sits on a v2 job
        for as long as it is the only worker, and a v2 worker started beside it
        runs the job -- so the refusal is the claim path's, not a scheduling
        coincidence."""
        from tests.conftest import wait_for_job_state

        await live_worker(app_version="v1")
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, app_version="v2", x=1
        )

        # Long enough for many poll intervals (checkInterval is 0.2s here).
        await _still_queued(db_pool, job_id, seconds=2.0)

        await live_worker(app_version="v2")
        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)
        assert row["app_version"] == "v2", "the pin is not consumed by the claim"

    async def test_clearing_the_pin_frees_it_for_the_worker_already_running(
        self, live_worker, db_pool, unique_queue
    ):
        """Remedy B, live: nothing about the fleet changes and the job runs."""
        from tests.conftest import wait_for_job_state

        await live_worker(app_version="v1")
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, app_version="v2", x=1
        )
        await _still_queued(db_pool, job_id, seconds=1.0)

        assert await client.update_job_app_version(job_id, None) is True

        await wait_for_job_state(db_pool, job_id, ("finished",), timeout=30)


async def _still_queued(pool, job_id: int, seconds: float) -> None:
    """Assert the job stays 'queued' for `seconds`, sampling throughout.

    A single check after a sleep proves only that it was queued at one instant;
    the claim being refused is a claim that keeps being refused.
    """
    import asyncio
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = await pool.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
        assert state == "queued", (
            f"a pinned job was claimed by a worker on another version (state {state!r})"
        )
        await asyncio.sleep(0.05)


# ============================================================================
# The remedies
# ============================================================================


class TestRemedies:
    async def test_repinning_makes_it_claimable_by_the_new_version(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        job = await pinned_job(db_connection, unique_queue, "v2")

        assert await api.update_job_app_version(job, "v3") is True

        assert await claim_as(db_connection, unique_queue, app_version="v2") is None
        claimed = await claim_as(db_connection, unique_queue, app_version="v3")
        assert claimed is not None and claimed["id"] == job

    async def test_clearing_makes_it_claimable_by_everyone(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)
        job = await pinned_job(db_connection, unique_queue, "v2")

        assert await api.update_job_app_version(job, None) is True

        claimed = await claim_as(db_connection, unique_queue)
        assert claimed is not None and claimed["id"] == job

    async def test_only_queued_and_waiting_rows_can_be_repinned(
        self, db_connection, unique_queue
    ):
        """The same states `set-priority` refuses, for the same reason: a
        claimed job has already been matched to a worker, so the gate it passed
        through decides nothing now."""
        api = AdminAPI(db_connection)
        for state in ("claimed", "running", "finished", "crashed", "cancelled"):
            job = await pinned_job(db_connection, unique_queue, "v2", state=state)
            assert await api.update_job_app_version(job, None) is False, state
            assert await version_of(db_connection, job) == "v2", state

        waiting = await pinned_job(
            db_connection, unique_queue, "v2", state="waiting", waitfor_job=1
        )
        assert await api.update_job_app_version(waiting, None) is True

    async def test_a_missing_job_is_False_not_an_error(self, db_connection):
        assert (
            await AdminAPI(db_connection).update_job_app_version(999_999_999, "v2")
            is False
        )

    async def test_the_cli_verb_pins_clears_and_refuses_both_at_once(
        self, db_pool, db_params, unique_queue
    ):
        # The pool, not db_connection: the CLI opens its own connection, and a
        # row written inside this test's rolled-back transaction is invisible
        # to it.
        job = await pinned_job(db_pool, unique_queue, "v2")
        dsn = dsn_for(db_params)

        pinned = await run_cli("--dsn", dsn, "jobs", "set-app-version", str(job), "v3")
        assert pinned.exit_code == 0, pinned.output
        assert await version_of(db_pool, job) == "v3"

        cleared = await run_cli(
            "--dsn", dsn, "jobs", "set-app-version", str(job), "--clear"
        )
        assert cleared.exit_code == 0, cleared.output
        assert await version_of(db_pool, job) is None

        both = await run_cli(
            "--dsn", dsn, "jobs", "set-app-version", str(job), "v4", "--clear"
        )
        assert both.exit_code == 2
        assert "not both" in both.output

        neither = await run_cli("--dsn", dsn, "jobs", "set-app-version", str(job))
        assert neither.exit_code == 2
        assert "--clear" in neither.output

    async def test_the_cli_verb_refuses_a_job_that_has_left_the_queue(
        self, db_pool, db_params, unique_queue
    ):
        job = await pinned_job(db_pool, unique_queue, "v2", state="running")

        result = await run_cli(
            "--dsn", dsn_for(db_params), "jobs", "set-app-version", str(job), "v3"
        )

        assert result.exit_code != 0
        assert "queued/waiting" in result.output

    async def test_workers_list_shows_the_version_each_worker_advertises(
        self, live_worker, db_params, unique_queue
    ):
        await live_worker(app_version="v2")

        result = await run_cli("--dsn", dsn_for(db_params), "workers", "list")

        assert result.exit_code == 0, result.output
        assert "App Version" in result.output
        assert "v2" in result.output


# ============================================================================
# Where the stamp comes from, and what keeps it
# ============================================================================


class TestEnqueuePaths:
    async def test_a_per_call_pin(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, app_version="v2", x=1
        )

        assert await version_of(db_pool, job_id) == "v2"

    async def test_a_client_wide_pin_reaches_every_path(self, db_pool, unique_queue):
        """A deployment that pins EVERYTHING declares it once on the client, so
        every path has to apply it -- a path that forgot would write unpinned
        work indistinguishable from a deliberate choice."""
        client = JobClient(pool=db_pool, app_version="v2")

        plain = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=1)
        identified, created = await client.enqueue_identified(
            "tests.dxe_jobs.OkJob",
            identity_key=f"{unique_queue}:ident",
            queue=unique_queue,
            x=1,
        )
        debounced, _ = await client.debounce(
            "tests.dxe_jobs.OkJob",
            key=f"{unique_queue}:debounce",
            period=60.0,
            queue=unique_queue,
            x=1,
        )
        batched = await client.enqueue_batch(
            [("tests.dxe_jobs.OkJob", {"x": n}) for n in range(2)],
            queue=unique_queue,
        )

        assert created is True
        for job_id in [plain, identified, debounced, *batched]:
            assert await version_of(db_pool, job_id) == "v2", job_id

    async def test_a_per_call_pin_overrides_the_clients(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool, app_version="v2")

        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, app_version="v3", x=1
        )
        batched = await client.enqueue_batch(
            [("tests.dxe_jobs.OkJob", {"x": 1}, {"app_version": "v4"})],
            queue=unique_queue,
            app_version="v3",
        )

        assert await version_of(db_pool, job_id) == "v3"
        assert await version_of(db_pool, batched[0]) == "v4"

    async def test_an_unversioned_client_pins_nothing(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool)

        job_id = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, x=1)

        assert await version_of(db_pool, job_id) is None

    async def test_the_callers_own_transaction_carries_a_named_pin(
        self, db_pool, unique_queue
    ):
        """enqueue_in_transaction is static, so it inherits no client -- it
        pins only what the call names, exactly as it validates priority against
        the platform default rather than the client's ceiling."""
        async with db_pool.acquire() as conn, conn.transaction():
            job_id = await JobClient.enqueue_in_transaction(
                conn,
                "tests.dxe_jobs.OkJob",
                queue=unique_queue,
                app_version="v2",
                x=1,
            )

        assert await version_of(db_pool, job_id) == "v2"

    async def test_a_pipeline_carries_the_clients_pin(self, db_pool, unique_queue):
        client = JobClient(pool=db_pool, app_version="v2")

        job_ids = await client.create_pipeline(
            [("tests.dxe_jobs.OkJob", {"x": 1}), ("tests.dxe_jobs.OkJob", {"x": 2})],
            queue=unique_queue,
        )

        for job_id in job_ids:
            assert await version_of(db_pool, job_id) == "v2"

    async def test_a_schedule_mints_unpinned_jobs(self, db_connection, unique_queue):
        """Out of scope on purpose: a schedule describes recurring work, not a
        deployment, so a firing long after that version stopped running must
        not be stranded by it. The scheduler goes through the same
        build_enqueue_row as every client, which is why this is a real risk
        rather than a hypothetical -- it names no app_version, so it gets
        none."""
        from datetime import UTC, datetime

        from pyjobby.scheduler import SchedulerWorker

        schedule_id = await db_connection.fetchval(
            """INSERT INTO jorb_schedule (name, job_class, cron_expr, queue,
                                          next_run)
               VALUES ($1, 'tests.dxe_jobs.OkJob', '* * * * *', $2, now())
               RETURNING id""",
            f"{unique_queue}-sched",
            unique_queue,
        )
        schedule = dict(
            await db_connection.fetchrow(
                "SELECT * FROM jorb_schedule WHERE id = $1", schedule_id
            )
        )

        job_id = await SchedulerWorker(db_connection).create_scheduled_job(
            schedule, datetime.now(UTC), jitter_seconds=0
        )

        assert job_id is not None
        assert await version_of(db_connection, job_id) is None


class TestWhatKeepsTheStamp:
    async def test_a_retry_keeps_it(self, db_connection, unique_queue):
        """Retry requeues the SAME row to re-execute the same code, so the pin
        stands; clearing it is the operator's move, never a side effect."""
        job = await pinned_job(db_connection, unique_queue, "v2", state="crashed")

        assert await retry_job(db_connection, job) == job

        assert await version_of(db_connection, job) == "v2"

    async def test_a_rerun_keeps_it(self, db_connection, unique_queue):
        api = AdminAPI(db_connection)
        job = await pinned_job(db_connection, unique_queue, "v2", state="finished")

        await api.rerun_job(job)

        assert await version_of(db_connection, job) == "v2"

    async def test_a_fork_is_unpinned_by_default(self, db_connection, unique_queue):
        """Not inherited, unlike partition_key: a fork's main use is re-running
        the work under NEW code, so inheriting the old pin would strand the
        fork on the build the operator just replaced."""
        source = await pinned_job(db_connection, unique_queue, "v2")

        forked = await fork_job(db_connection, source)

        assert await version_of(db_connection, forked["job_id"]) is None
        assert await version_of(db_connection, source) == "v2"

    async def test_a_fork_pins_what_the_caller_asks_for(
        self, db_connection, unique_queue
    ):
        source = await pinned_job(db_connection, unique_queue, "v2")

        forked = await fork_job(db_connection, source, app_version="v3")

        assert await version_of(db_connection, forked["job_id"]) == "v3"

    async def test_the_cli_fork_pins_it(self, db_pool, db_params, unique_queue):
        source = await pinned_job(db_pool, unique_queue, "v2")

        result = await run_cli(
            "--dsn",
            dsn_for(db_params),
            "jobs",
            "fork",
            str(source),
            "--app-version",
            "v3",
        )

        assert result.exit_code == 0, result.output
        forked = await db_pool.fetchval(
            "SELECT id FROM jorb WHERE forked_from = $1", source
        )
        assert await version_of(db_pool, forked) == "v3"


# ============================================================================
# What the platform refuses to write
# ============================================================================


class TestValidation:
    # `async def` throughout, including the tests that touch no database: this
    # file's pytestmark makes every test an asyncio one, and a sync test under
    # it is a pytest warning rather than a passing test.
    async def test_none_is_the_absence_of_a_pin(self):
        assert validate_app_version(None) is None

    @pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
    async def test_an_empty_version_is_refused(self, empty):
        with pytest.raises(ValueError, match="app_version is empty"):
            validate_app_version(empty)

    async def test_the_length_is_bounded(self):
        assert validate_app_version("v" * MAX_APP_VERSION_LENGTH)
        with pytest.raises(ValueError, match="above the"):
            validate_app_version("v" * (MAX_APP_VERSION_LENGTH + 1))

    async def test_the_enqueue_paths_refuse_it_too(self, db_pool, unique_queue):
        """One validator, called from the ONE construction path every writer
        goes through -- so a build variable that came back empty is refused at
        the call site instead of becoming a job nothing can claim."""
        client = JobClient(pool=db_pool)

        with pytest.raises(ValueError, match="app_version is empty"):
            await client.enqueue(
                "tests.dxe_jobs.OkJob", queue=unique_queue, app_version="", x=1
            )
        with pytest.raises(ValueError, match="app_version is empty"):
            await client.enqueue_batch(
                [("tests.dxe_jobs.OkJob", {"x": 1})],
                queue=unique_queue,
                app_version="",
            )

    async def test_repinning_refuses_it_too(self, db_connection, unique_queue):
        job = await pinned_job(db_connection, unique_queue, "v2")

        with pytest.raises(ValueError, match="app_version is empty"):
            await AdminAPI(db_connection).update_job_app_version(job, "")
        assert await version_of(db_connection, job) == "v2"


# ============================================================================
# The config file declares it once, for both halves
# ============================================================================


class TestConfigDeclaration:
    async def test_the_client_takes_the_files_version(self, tmp_path, db_params):
        config = tmp_path / "pyjobby.toml"
        config.write_text(
            'app_version = "v9"\n'
            "[db_params]\n"
            f'database = "{db_params["database"]}"\n'
            f'user = "{db_params["user"]}"\n'
            f'password = "{db_params["password"]}"\n'
            f'host = "{db_params["host"]}"\n'
            f"port = {db_params['port']}\n"
        )

        client = await JobClient.from_config(str(config))
        try:
            assert client.app_version == "v9"
        finally:
            await client.close()

    async def test_the_flag_wins_over_the_file_and_the_file_over_nothing(self):
        assert resolve_app_version("v2", "v1") == "v2"
        assert resolve_app_version(None, "v1") == "v1"
        assert resolve_app_version(None, None) is None

    @pytest.mark.parametrize("empty", ["", "   "])
    async def test_an_empty_version_advertises_none_rather_than_the_empty_string(
        self, empty
    ):
        """`pj --app-version "$GIT_SHA"` with the variable unset. A worker
        advertising '' would claim only jobs pinned to '' -- which nothing can
        enqueue -- so the whole fleet would come up healthy and claim nothing.
        The launcher has no caller to raise at, so it warns and runs unpinned
        instead of refusing to boot over a blank template variable."""
        assert resolve_app_version(empty, None) is None
        assert resolve_app_version(None, empty) is None

    async def test_app_version_is_a_known_config_key(self):
        """A key outside KNOWN_TOP_LEVEL_KEYS is refused rather than skipped,
        so this is what makes the file's declaration reachable at all."""
        from pyjobby.configloader import KNOWN_TOP_LEVEL_KEYS

        assert "app_version" in KNOWN_TOP_LEVEL_KEYS
