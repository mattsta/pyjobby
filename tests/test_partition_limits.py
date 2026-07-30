"""Per-lane queue limits: `jorb_queue.partition_limits` and `jorb.partition_key`.

A queue-wide `max_concurrency` is a fair-share scheme with one participant.
Give the queue to more than one tenant and the biggest of them takes the whole
cap: everybody else's work sits behind it, healthy, claimable, and never
claimed. `partition_limits` re-scopes THAT SAME LIMIT to each distinct
`jorb.partition_key`, so the cap means "N per tenant" and a tenant filling its
own share cannot touch anyone else's.

What this file pins, in the order the claim path meets it:

* **The two-tier lock property survives.** A queue with a limit serialises its
  claims whether the limit is per queue or per lane; a queue with NO limit
  never takes the lock, and turning `partition_limits` on does not change that
  — the flag re-scopes limits and adds none of its own.
* **Exactness per lane**, under contention, in the default suite: at no
  observation does any lane exceed its cap, with four claimers racing.
* **A saturated lane never blocks another**, including when the saturated
  lane's backlog sorts AHEAD of everyone else's work. This is the starvation
  the feature exists to end, so it is asserted in the shape that produces it.
* **The NULL lane is a lane.** Jobs with no `partition_key` are counted,
  capped and admitted exactly like a named lane's — never invisible, never
  refused for being unlabelled. A fair-share scheme that blackholes the work
  nobody remembered to label is a worse failure than the one it replaced.
* **Off is inert.** With `partition_limits` FALSE the key is labelling and
  nothing else, and the queue-wide limits bind exactly as they always did.
* **The key reaches the row from every enqueue path**, and a fork inherits it
  (whose work it is, not which piece of work it is).

The lock machinery itself — that the wait is bounded, that the lock survives
the EXCEPTION subtransaction — belongs to tests/test_claim_contention.py, and
this file imports its claim helpers rather than growing a second copy.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from pyjobby.admin_api import AdminAPI
from pyjobby.db import fork_job
from pyjobby.enqueue_rules import MAX_KEY_LENGTH

from .test_claim_contention import CLAIM_LOCK_KEY, claim_once
from .test_cli_errors import dsn_for, run_cli

pytestmark = pytest.mark.asyncio


async def enqueue_lane(
    conn, queue: str, lane: str | None, count: int, prio: int = 100
) -> list[int]:
    """`count` claimable jobs on `queue`, all in one partition lane.

    Returns the ids in enqueue order, because the tests about starvation are
    about which of two lanes' rows a claim picks and that is decided by
    (prio, run_after) — i.e. by the order they were written.
    """
    rows = await conn.fetch(
        """INSERT INTO jorb (job_class, kwargs, queue, prio, capability, state,
                             partition_key)
           SELECT 'tests.dxe_jobs.OkJob', '{}'::jsonb, $1, $2, 'test', 'queued', $3
             FROM generate_series(1, $4)
           RETURNING id""",
        queue,
        prio,
        lane,
        count,
    )
    return [r["id"] for r in rows]


async def control(conn, queue: str, **fields) -> None:
    """Set this queue's control row through the admin API, live."""
    await AdminAPI(conn).set_queue_control(queue, **fields)


async def in_flight_by_lane(conn, queue: str) -> dict[str | None, int]:
    """Claimed+running per lane, as the cap counts it."""
    rows = await conn.fetch(
        """SELECT partition_key, count(*) AS n FROM jorb
            WHERE queue = $1 AND state IN ('claimed', 'running')
            GROUP BY partition_key""",
        queue,
    )
    return {r["partition_key"]: r["n"] for r in rows}


async def lane_of(conn, job_id: int) -> str | None:
    return await conn.fetchval("SELECT partition_key FROM jorb WHERE id = $1", job_id)


@pytest.fixture
async def admin_api(db_pool):
    """AdminAPI on a POOL connection, deliberately not on ``db_connection``.

    That fixture holds a transaction open for the whole test, so its ``now()``
    is frozen at fixture setup -- and every reason this file asks about
    (deferred vs claimable, a rate window, a queue's backlog) is a comparison
    against ``now()``. Rows written afterwards would read as scheduled for the
    future and the verb would answer about a clock the claim path never uses.
    """
    async with db_pool.acquire() as conn:
        yield AdminAPI(conn)


# ============================================================================
# 1. the concurrency cap, per lane
# ============================================================================


class TestPartitionedConcurrency:
    async def test_the_cap_is_per_lane_and_every_lane_gets_its_share(
        self, db_pool, unique_queue
    ):
        """Cap of 1, three lanes: three jobs go out, one per lane, then none.

        Queue-wide, this queue admits ONE job and the other two lanes wait for
        it. Per lane it admits three — which is the whole feature stated as a
        number.
        """
        await control(db_pool, unique_queue, max_concurrency=1, partition_limits=True)
        for lane in ("alpha", "beta", None):
            await enqueue_lane(db_pool, unique_queue, lane, 2)

        claimed = []
        for _ in range(6):
            row = await claim_once(db_pool, unique_queue)
            if row is None:
                break
            claimed.append(await lane_of(db_pool, row["id"]))

        assert sorted(claimed, key=lambda lane: (lane is None, lane or "")) == [
            "alpha",
            "beta",
            None,
        ], f"expected exactly one job admitted per lane, got {claimed}"
        assert await claim_once(db_pool, unique_queue) is None, (
            "every lane is at its cap of 1, so nothing more may be admitted"
        )

    async def test_a_saturated_lane_never_blocks_another_even_from_the_front(
        self, db_pool, unique_queue
    ):
        """The starvation case, in the shape that actually produces it.

        The hog's backlog is enqueued FIRST, so every one of its rows sorts
        ahead of the small tenant's in claim order. A claim that stopped at
        the head of the queue would find a hog row, see the lane full, and
        report the queue empty — which is precisely the behaviour a queue-wide
        cap has and the reason this feature exists.
        """
        await control(db_pool, unique_queue, max_concurrency=1, partition_limits=True)
        await enqueue_lane(db_pool, unique_queue, "hog", 40)
        small = await enqueue_lane(db_pool, unique_queue, "small", 1)

        first = await claim_once(db_pool, unique_queue)
        assert await lane_of(db_pool, first["id"]) == "hog", (
            "claim order is unchanged: the oldest row still wins when its lane"
            " has headroom"
        )

        second = await claim_once(db_pool, unique_queue)
        assert second is not None, (
            "the hog's lane is full and its 39 remaining rows sort first, but "
            "the small tenant's job is admissible — the claim walked past them"
        )
        assert second["id"] == small[0]

        assert await claim_once(db_pool, unique_queue) is None
        assert await in_flight_by_lane(db_pool, unique_queue) == {"hog": 1, "small": 1}

    async def test_the_null_lane_is_a_lane_and_not_a_blackhole(
        self, db_pool, unique_queue
    ):
        """Unlabelled work is admitted, capped, and released like anyone's.

        The failure this guards is the quiet one: a lane test written as
        `partition_key = ANY(blocked)` is NULL for an unlabelled row, so it is
        neither true nor false, the row never satisfies the predicate, and
        every job nobody remembered to label becomes permanently unclaimable
        while the queue reports itself healthy.
        """
        await control(db_pool, unique_queue, max_concurrency=1, partition_limits=True)
        await enqueue_lane(db_pool, unique_queue, "named", 5)
        unlabelled = await enqueue_lane(db_pool, unique_queue, None, 2)

        # The named lane fills first (it sorts first), and the NULL lane's job
        # is still admitted behind it.
        assert (
            await lane_of(db_pool, (await claim_once(db_pool, unique_queue))["id"])
            == "named"
        )
        second = await claim_once(db_pool, unique_queue)
        assert second is not None and second["id"] == unlabelled[0], (
            "the NULL lane was refused: unlabelled work is invisible to the claim"
        )

        # ...and it is CAPPED like a named one, not unlimited.
        assert await claim_once(db_pool, unique_queue) is None
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1",
            unlabelled[0],
        )
        third = await claim_once(db_pool, unique_queue)
        assert third is not None and third["id"] == unlabelled[1], (
            "the NULL lane's slot was freed and its next job should have taken it"
        )

    async def test_a_lane_frees_its_own_slot_and_nobody_else_s(
        self, db_pool, unique_queue
    ):
        """Finishing a job admits the next job IN THAT LANE, not a queue-wide one."""
        await control(db_pool, unique_queue, max_concurrency=1, partition_limits=True)
        a = await enqueue_lane(db_pool, unique_queue, "a", 2)
        b = await enqueue_lane(db_pool, unique_queue, "b", 2)

        assert (await claim_once(db_pool, unique_queue))["id"] == a[0]
        assert (await claim_once(db_pool, unique_queue))["id"] == b[0]
        assert await claim_once(db_pool, unique_queue) is None

        await db_pool.execute(
            "UPDATE jorb SET state = 'finished', finished = now() WHERE id = $1", b[0]
        )
        freed = await claim_once(db_pool, unique_queue)
        assert freed is not None and freed["id"] == b[1], (
            "b's slot came free, so b's next job is the only admissible row — "
            "a is still at its own cap"
        )


# ============================================================================
# 2. exactness under contention -- in the default suite
# ============================================================================

#: Racing claimers, lanes, and jobs per lane. Contention is what exposes a
#: broken count, not volume: four claimers with no think time on three lanes
#: capped at one apiece overlap continuously, and the invariant ("no lane ever
#: over its cap") does not become true at a larger scale.
PARTITION_CLAIMERS = 4
PARTITION_LANES: tuple[str | None, ...] = ("tenant-a", "tenant-b", None)
PARTITION_JOBS_PER_LANE = 25


class TestPartitionedExactnessUnderContention:
    async def test_no_lane_ever_exceeds_its_cap_while_four_claimers_race(
        self, db_pool, db_params, unique_queue
    ):
        """The crown jewel: exact per lane, at every observation, under a race.

        Four claimers churn the queue — claim, finish, claim again — while a
        fifth connection samples the in-flight count per lane the whole way
        down. "Exact, not approximate" means the cap holds at EVERY committed
        snapshot, not on average, so a single sample above the cap fails the
        run and the lane that broke is named.

        It also asserts the lanes made progress INDEPENDENTLY: every lane
        drains, so no lane was starved by another's saturation for the whole
        run.
        """
        await control(db_pool, unique_queue, max_concurrency=1, partition_limits=True)
        for lane in PARTITION_LANES:
            await enqueue_lane(db_pool, unique_queue, lane, PARTITION_JOBS_PER_LANE)
        total = PARTITION_JOBS_PER_LANE * len(PARTITION_LANES)

        drained: list[int] = []
        over_cap: list[dict[str | None, int]] = []
        done = asyncio.Event()

        async def churn(worker_id: int) -> None:
            async with pool.acquire() as conn:
                while not done.is_set():
                    row = await claim_once(conn, unique_queue, worker_id=worker_id)
                    if row is None:
                        left = await conn.fetchval(
                            "SELECT count(*) FROM jorb WHERE queue = $1 "
                            "AND state = 'queued'",
                            unique_queue,
                        )
                        if not left:
                            return
                        # Every lane is at its cap this instant; another
                        # claimer is about to free one.
                        await asyncio.sleep(0.002)
                        continue
                    drained.append(row["id"])
                    await conn.execute(
                        "UPDATE jorb SET state = 'finished', finished = now() "
                        "WHERE id = $1",
                        row["id"],
                    )

        async def sample() -> None:
            async with pool.acquire() as conn:
                while not done.is_set():
                    counts = await in_flight_by_lane(conn, unique_queue)
                    if any(n > 1 for n in counts.values()):
                        over_cap.append(counts)
                        return
                    await asyncio.sleep(0)

        pool = await asyncpg.create_pool(
            **db_params,
            min_size=PARTITION_CLAIMERS + 1,
            max_size=PARTITION_CLAIMERS + 1,
        )
        try:
            sampler = asyncio.create_task(sample())
            await asyncio.wait_for(
                asyncio.gather(*(churn(i) for i in range(PARTITION_CLAIMERS))),
                timeout=120,
            )
            done.set()
            await sampler
        finally:
            await pool.close()

        assert not over_cap, (
            f"a lane held more than its cap of 1 in flight: {over_cap[0]} — the "
            f"per-lane count is not serialised"
        )
        assert len(drained) == total, f"{len(drained)} of {total} jobs were claimed"
        assert len(set(drained)) == total, "a job was claimed by two claimers at once"

        finished_by_lane = {
            r["partition_key"]: r["n"]
            for r in await db_pool.fetch(
                """SELECT partition_key, count(*) AS n FROM jorb
                    WHERE queue = $1 AND state = 'finished'
                    GROUP BY partition_key""",
                unique_queue,
            )
        }
        assert finished_by_lane == dict.fromkeys(
            PARTITION_LANES, PARTITION_JOBS_PER_LANE
        ), (
            f"lanes did not progress independently: {finished_by_lane} — one "
            f"lane's saturation held another back for the whole run"
        )


# ============================================================================
# 3. the rate limit, per lane
# ============================================================================


class TestPartitionedRateLimit:
    async def test_each_lane_gets_its_own_admission_window(self, db_pool, unique_queue):
        """rate_limit R per period means R per lane per period, counted by
        admissions (claimed_at) exactly as the queue-wide limit is."""
        await control(
            db_pool,
            unique_queue,
            rate_limit=1,
            rate_period_seconds=60.0,
            partition_limits=True,
        )
        a = await enqueue_lane(db_pool, unique_queue, "a", 2)
        b = await enqueue_lane(db_pool, unique_queue, "b", 2)

        assert (await claim_once(db_pool, unique_queue))["id"] == a[0]
        # a has spent its window; b has not.
        assert (await claim_once(db_pool, unique_queue))["id"] == b[0]
        assert await claim_once(db_pool, unique_queue) is None, (
            "both lanes have admitted their one job for this window"
        )

        # Roll a's window forward only. Its next job is admissible again and
        # b's still is not, which is what "its own window" means.
        await db_pool.execute(
            "UPDATE jorb SET claimed_at = now() - interval '2 minutes' WHERE id = $1",
            a[0],
        )
        rolled = await claim_once(db_pool, unique_queue)
        assert rolled is not None and rolled["id"] == a[1]
        assert await claim_once(db_pool, unique_queue) is None

    async def test_the_two_limits_compose_per_lane(self, db_pool, unique_queue):
        """A lane blocked by EITHER limit is skipped; a lane blocked by
        neither is admitted. The blocked set is the union, not a choice."""
        await control(
            db_pool,
            unique_queue,
            max_concurrency=1,
            rate_limit=5,
            rate_period_seconds=60.0,
            partition_limits=True,
        )
        await enqueue_lane(db_pool, unique_queue, "conc", 2)
        rated = await enqueue_lane(db_pool, unique_queue, "rate", 2)
        free = await enqueue_lane(db_pool, unique_queue, "free", 1)

        # 'conc' takes its one concurrency slot. Asserted as the LANE: an `id
        # is not None` on a claimed row is true of every claim ever made, so
        # it certified nothing about which lane the claim came from -- which
        # is the entire subject of this test.
        took_the_slot = await claim_once(db_pool, unique_queue)
        assert took_the_slot is not None
        assert took_the_slot["partition_key"] == "conc"
        # 'rate' has already spent its window (5 admissions), in flight or not.
        await db_pool.execute(
            """UPDATE jorb SET claimed_at = now(), state = 'finished',
                               finished = now()
                WHERE id = ANY($1)""",
            rated,
        )
        await enqueue_lane(db_pool, unique_queue, "rate", 5)
        await db_pool.execute(
            """UPDATE jorb SET claimed_at = now(), state = 'finished',
                               finished = now()
                WHERE queue = $1 AND partition_key = 'rate' AND state = 'queued'""",
            unique_queue,
        )
        rate_again = await enqueue_lane(db_pool, unique_queue, "rate", 1)

        admitted = await claim_once(db_pool, unique_queue)
        assert admitted is not None and admitted["id"] == free[0], (
            "the only lane blocked by neither limit is 'free'"
        )
        assert admitted["partition_key"] == "free"
        assert await claim_once(db_pool, unique_queue) is None
        # the rate-limited row is HELD BACK, not lost -- named by its lane,
        # because "a non-empty list is truthy" was the whole of what the
        # previous assertion here established
        held_back = await db_pool.fetchrow(
            "SELECT state::text AS state, partition_key FROM jorb WHERE id = $1",
            rate_again[0],
        )
        assert (held_back["state"], held_back["partition_key"]) == ("queued", "rate")


# ============================================================================
# 4. off is inert, and the two-tier lock property survives
# ============================================================================


class TestPartitionLimitsOff:
    async def test_the_key_is_inert_labelling_without_the_flag(
        self, db_pool, unique_queue
    ):
        """Same rows, same cap, flag off: the limit is queue-wide again."""
        await control(db_pool, unique_queue, max_concurrency=1)
        for lane in ("a", "b", None):
            await enqueue_lane(db_pool, unique_queue, lane, 2)

        assert await claim_once(db_pool, unique_queue) is not None
        assert await claim_once(db_pool, unique_queue) is None, (
            "partition_limits is off, so ONE job in flight fills the whole queue"
        )

    async def test_an_uncontrolled_queue_ignores_the_key_entirely(
        self, db_pool, unique_queue
    ):
        """No control row at all: every lane's work is claimable, in order."""
        ids = []
        for lane in ("a", None, "b"):
            ids += await enqueue_lane(db_pool, unique_queue, lane, 2)

        claimed = []
        while (row := await claim_once(db_pool, unique_queue)) is not None:
            claimed.append(row["id"])
        assert claimed == ids

    async def test_a_partitioned_queue_with_no_limit_never_takes_the_claim_lock(
        self, db_pool, db_params, unique_queue
    ):
        """THE TWO-TIER PROPERTY, on the tier that must stay free.

        `partition_limits` re-scopes limits and adds none, so a queue that has
        no limit to re-scope must keep the lock-free fast path that carries
        almost all of the platform's traffic. Probed directly, because nothing
        else would fail if the flag started serialising every queue that set
        it: the claims would all still be correct, just slower forever.
        """
        await control(db_pool, unique_queue, partition_limits=True)
        await enqueue_lane(db_pool, unique_queue, "a", 2)
        key = await db_pool.fetchval(CLAIM_LOCK_KEY, unique_queue)

        claimer = await asyncpg.connect(**db_params)
        prober = await asyncpg.connect(**db_params)
        try:
            tx = claimer.transaction()
            await tx.start()
            assert await claim_once(claimer, unique_queue) is not None
            free = await prober.fetchval("SELECT pg_try_advisory_xact_lock($1)", key)
            await tx.rollback()
        finally:
            await claimer.close()
            await prober.close()

        assert free, (
            "a partitioned queue with NO limit took the per-queue claim lock: "
            "the flag has become a third tier instead of a re-scoping of the "
            "limits that already existed"
        )

    async def test_a_partitioned_queue_with_a_limit_does_take_the_claim_lock(
        self, db_pool, db_params, unique_queue
    ):
        """...and the other tier must stay serialised, for the same reason it
        always was: a per-lane count is no less blind to an uncommitted claim
        than a per-queue count is."""
        await control(db_pool, unique_queue, max_concurrency=5, partition_limits=True)
        await enqueue_lane(db_pool, unique_queue, "a", 2)
        key = await db_pool.fetchval(CLAIM_LOCK_KEY, unique_queue)

        claimer = await asyncpg.connect(**db_params)
        prober = await asyncpg.connect(**db_params)
        try:
            tx = claimer.transaction()
            await tx.start()
            assert await claim_once(claimer, unique_queue) is not None
            free = await prober.fetchval("SELECT pg_try_advisory_xact_lock($1)", key)
            await tx.rollback()
        finally:
            await claimer.close()
            await prober.close()

        assert not free, (
            "a partitioned queue with a cap claimed WITHOUT holding the queue "
            "lock: its per-lane counts cannot see an uncommitted claim, so the "
            "cap is approximate again"
        )


# ============================================================================
# 5. the key reaches the row from every enqueue path
# ============================================================================


class TestPartitionKeyOnTheEnqueuePaths:
    async def test_enqueue_carries_it(self, job_client, db_pool, unique_queue):
        job_id = await job_client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, partition_key="tenant-7", x=1
        )
        assert await lane_of(db_pool, job_id) == "tenant-7"

    async def test_batch_carries_it_shared_and_per_job(
        self, job_client, db_pool, unique_queue
    ):
        ids = await job_client.enqueue_batch(
            [
                ("tests.dxe_jobs.OkJob", {"x": 1}),
                ("tests.dxe_jobs.OkJob", {"x": 2}, {"partition_key": "override"}),
            ],
            queue=unique_queue,
            partition_key="shared",
        )
        assert [await lane_of(db_pool, i) for i in ids] == ["shared", "override"]

    async def test_debounce_carries_it(self, job_client, db_pool, unique_queue):
        job_id, created = await job_client.debounce(
            "tests.dxe_jobs.OkJob",
            key=f"{unique_queue}:doc",
            period=30.0,
            queue=unique_queue,
            partition_key="tenant-d",
        )
        assert created
        assert await lane_of(db_pool, job_id) == "tenant-d"

    async def test_identified_enqueue_carries_it(
        self, job_client, db_pool, unique_queue
    ):
        job_id, created = await job_client.enqueue_identified(
            "tests.dxe_jobs.OkJob",
            identity_key=f"{unique_queue}:once",
            queue=unique_queue,
            partition_key="tenant-i",
        )
        assert created
        assert await lane_of(db_pool, job_id) == "tenant-i"

    async def test_a_fork_inherits_the_lane(self, job_client, db_pool, unique_queue):
        """Unlike the three dedupe keys: a partition_key says WHOSE work this
        is, not WHICH piece of work it is, so a tenant's fork is still theirs
        and still counts against their share."""
        source = await job_client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, partition_key="tenant-f", x=1
        )
        forked = await fork_job(db_pool, source)
        assert await lane_of(db_pool, forked["job_id"]) == "tenant-f"

    async def test_an_oversized_key_is_refused_at_the_caller(
        self, job_client, unique_queue
    ):
        with pytest.raises(ValueError, match="partition_key"):
            await job_client.enqueue(
                "tests.dxe_jobs.OkJob",
                queue=unique_queue,
                partition_key="x" * (MAX_KEY_LENGTH + 1),
            )

    async def test_a_key_at_the_bound_is_accepted(
        self, job_client, db_pool, unique_queue
    ):
        key = "x" * MAX_KEY_LENGTH
        job_id = await job_client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, partition_key=key
        )
        assert await lane_of(db_pool, job_id) == key


# ============================================================================
# 6. the toggle, live
# ============================================================================


class TestPartitionLimitsToggle:
    async def test_the_toggle_changes_the_next_claim_with_no_restart(
        self, db_pool, unique_queue
    ):
        """The control plane's promise, applied to this control: flip it and
        the very next claim enforces the other scope."""
        await control(db_pool, unique_queue, max_concurrency=1)
        await enqueue_lane(db_pool, unique_queue, "a", 2)
        await enqueue_lane(db_pool, unique_queue, "b", 2)

        assert await claim_once(db_pool, unique_queue) is not None
        assert await claim_once(db_pool, unique_queue) is None

        await control(db_pool, unique_queue, partition_limits=True)
        assert await claim_once(db_pool, unique_queue) is not None, (
            "the toggle did not take effect on the next claim"
        )

        await control(db_pool, unique_queue, partition_limits=False)
        assert await claim_once(db_pool, unique_queue) is None, (
            "turning it back off did not restore the queue-wide cap"
        )

    async def test_the_cli_sets_it_and_queues_show_reports_it(
        self, db_pool, db_params, unique_queue
    ):
        dsn = dsn_for(db_params)
        set_it = await run_cli(
            "--dsn",
            dsn,
            "queues",
            "limits",
            unique_queue,
            "--max-concurrency",
            "2",
            "--partition-limits",
        )
        assert set_it.exit_code == 0, set_it.output
        assert "PER partition_key" in set_it.output

        shown = await run_cli("--dsn", dsn, "queues", "show", unique_queue)
        assert shown.exit_code == 0, shown.output
        assert "Partition limits:    yes" in shown.output

        assert await db_pool.fetchval(
            "SELECT partition_limits FROM jorb_queue WHERE name = $1", unique_queue
        )

        off = await run_cli(
            "--dsn", dsn, "queues", "limits", unique_queue, "--no-partition-limits"
        )
        assert off.exit_code == 0, off.output
        assert not await db_pool.fetchval(
            "SELECT partition_limits FROM jorb_queue WHERE name = $1", unique_queue
        )

    async def test_the_cli_warns_when_there_is_no_limit_to_re_scope(
        self, db_params, unique_queue
    ):
        """The one way to misread the flag. It re-scopes limits and adds none,
        so on its own it changes nothing — said out loud rather than left for
        the operator to discover by watching nothing happen."""
        result = await run_cli(
            "--dsn",
            dsn_for(db_params),
            "queues",
            "limits",
            unique_queue,
            "--partition-limits",
        )
        assert result.exit_code == 0, result.output
        assert "no limit to re-scope" in result.output


# ============================================================================
# 7. what the operator is told about a lane that is waiting
# ============================================================================


class TestSaturatedLaneIsExplainedNotFlagged:
    async def test_jobs_why_names_the_lane_and_its_in_flight_count(
        self, db_pool, admin_api, unique_queue
    ):
        """A job held back by its own lane gets the concurrency reason with
        the LANE's numbers — the queue-wide total would send the operator to
        raise a cap that is not the one binding."""
        await control(db_pool, unique_queue, max_concurrency=1, partition_limits=True)
        await db_pool.execute(
            """INSERT INTO jorb_worker (host, pid, queue, capabilities, max_prio)
               VALUES ('why-host', 991, $1, ARRAY['test'], 1000)""",
            unique_queue,
        )
        held = await enqueue_lane(db_pool, unique_queue, "tenant-x", 2)
        await enqueue_lane(db_pool, unique_queue, "tenant-y", 5)
        assert await claim_once(db_pool, unique_queue) is not None

        answer = await admin_api.explain_job(held[1])
        assert answer["reason"] == "queue_at_max_concurrency"
        assert answer["details"]["partition_limits"] is True
        assert answer["details"]["partition_key"] == "tenant-x"
        assert answer["details"]["inflight"] == 1, (
            "the count must be the LANE's in-flight, not the queue's"
        )
        assert "tenant-x" in answer["summary"]

    async def test_jobs_why_names_the_null_lane_in_words(
        self, db_pool, admin_api, unique_queue
    ):
        await control(db_pool, unique_queue, max_concurrency=1, partition_limits=True)
        await db_pool.execute(
            """INSERT INTO jorb_worker (host, pid, queue, capabilities, max_prio)
               VALUES ('why-host', 992, $1, ARRAY['test'], 1000)""",
            unique_queue,
        )
        held = await enqueue_lane(db_pool, unique_queue, None, 2)
        assert await claim_once(db_pool, unique_queue) is not None

        answer = await admin_api.explain_job(held[1])
        assert answer["reason"] == "queue_at_max_concurrency"
        assert answer["details"]["partition_key"] is None
        assert "NULL partition" in answer["summary"]

    async def test_doctor_does_not_call_a_saturated_lane_unclaimable(
        self, db_pool, admin_api, unique_queue
    ):
        """A lane at its cap is BACKLOGGED, not unclaimable.

        `unclaimable_jobs` finds work the live fleet could never claim — a
        priority above every ceiling, a capability nobody advertises. Work
        waiting on a limit is claimed the moment the limit lets go, so
        reporting it here would turn the platform's quietest-failure sweep
        into a noise generator for every queue that uses fair-share limits.
        """
        await control(db_pool, unique_queue, max_concurrency=1, partition_limits=True)
        await db_pool.execute(
            """INSERT INTO jorb_worker (host, pid, queue, capabilities, max_prio)
               VALUES ('doctor-host', 993, $1, ARRAY['test'], 1000)""",
            unique_queue,
        )
        await enqueue_lane(db_pool, unique_queue, "tenant-x", 50)
        await enqueue_lane(db_pool, unique_queue, None, 50)
        assert await claim_once(db_pool, unique_queue) is not None
        assert await claim_once(db_pool, unique_queue) is not None

        api = admin_api
        assert [
            r for r in await api.unclaimable_jobs() if r["queue"] == unique_queue
        ] == [], "a lane at its concurrency cap was reported as unclaimable work"

        stats = await api.queue_stats(queue=unique_queue)
        assert stats[0]["queued"] == 98, "the held-back work is backlog, and visible"
        assert stats[0]["partition_limits"] is True
