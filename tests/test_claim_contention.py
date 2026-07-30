"""The bounded wait on the per-queue claim lock: is it bounded, and is it worth it?

A queue with ``max_concurrency`` or ``rate_limit`` set has to serialise its
claims, because under READ COMMITTED two simultaneous claims cannot see each
other's uncommitted rows and would both pass a cap of 1. The lock that does
that serialising has one hard requirement -- a claim held open by a slow or
stuck transaction must never freeze the whole queue -- and the schema used to
meet it with ``pg_try_advisory_xact_lock``: give up instantly, report nothing
claimable, poll again.

Giving up instantly turned out to be the expensive way to be safe. Eight
claimers on one queue lost that try-lock on ~87% of attempts, so draining
2,000 jobs cost ~40,000 wasted round trips. ``claim_queue_lock`` replaces it
with a bounded wait -- blocking acquisition under ``lock_timeout``, the
timeout caught as 55P03 and reported as "nothing claimable" -- so claimers
queue in arrival order instead of stampeding, and the stall stays capped.

The thing that turned out to matter is WHO pays for a lost lock. ``pj-bench
claim`` retries in a tight loop, so it prices the loss at one round trip --
and duly reports that removing 98% of them moves claim rate by 0.96x on an
idle box (2.1x on a saturated one, where the retries were at least competing
for CPU). It could not have reported more: a claimer that lost the try-lock
held no lock, so its retries never delayed the winner, and capped throughput
is 1/(critical section) under either lock.

A real worker pays enormously more than a round trip. An empty claim means
"this queue is empty", so it publishes idle demand -- switching this queue's
enqueue notifications back on for every producer -- and then PARKS for
``checkInterval``, five seconds by default, waiting for a wakeup that is not
coming because the work it wanted was enqueued before it went to sleep. Four
workers on a queue full of claimable work, cap nowhere near binding: the
try-lock let exactly one of them ever claim anything.

The tests here pin what has to be true:

* the wait really is bounded (a held-open claim does not hang a competitor,
  it returns empty-handed within the timeout) -- this is the property the
  try-lock existed for and the one a blocking lock could quietly remove;
* the lock is really held afterwards, despite being acquired inside the
  ``EXCEPTION`` block's implicit subtransaction, and despite the claiming
  ``UPDATE ... FOR UPDATE SKIP LOCKED`` running outside it;
* every job is claimed EXACTLY ONCE under concurrent claimers, capped and
  uncapped -- the invariant the lock exists to keep. It is an invariant, so
  it holds at any scale and is asserted at a scale the default suite can
  afford: a correctness proof that only runs behind ``-m performance`` is a
  correctness proof that never runs;
* lock contention and a full cap are DIFFERENT empty returns, counted
  separately -- a cap that is doing its job also returns nothing, and
  conflating the two would credit this change for work the cap was correctly
  refusing;
* every worker on a capped queue gets to work, which is the win;
* and the round trips and claim rate, measured against the try-lock itself
  rather than against a remembered number (``-m performance -s``).
"""

from __future__ import annotations

import asyncio
import contextlib
import statistics
import time
from collections.abc import AsyncIterator

import asyncpg
import pytest

from pyjobby.pj import STMTS

pytestmark = pytest.mark.asyncio

#: The advisory key claim_jorb() serialises a queue on. Duplicated from
#: schema.sql on purpose: a test that computed it by calling the schema's own
#: code could not notice the schema locking the wrong thing.
CLAIM_LOCK_KEY = "SELECT hashtext('pyjobby.claim:' || $1::text)"

#: Round trips are not free, so the deterministic tests below wait out the
#: real timeout several times. Read it from the installed function rather than
#: hardcoding it, so tuning schema.sql retunes the tests with it.
LOCK_TIMEOUT_SQL = """
    SELECT unnest(proconfig) FROM pg_proc
     WHERE proname = 'claim_queue_lock'
"""


async def lock_timeout_seconds(conn: asyncpg.Connection) -> float:
    """The lock_timeout claim_queue_lock() is declared with, in seconds."""
    settings = [row[0] for row in await conn.fetch(LOCK_TIMEOUT_SQL)]
    values = [s.split("=", 1)[1] for s in settings if s.startswith("lock_timeout=")]
    assert values, f"claim_queue_lock has no lock_timeout: {settings}"
    raw = values[0].strip().strip("'\"")
    assert raw.endswith("ms"), f"expected a millisecond timeout, got {raw!r}"
    return float(raw.removesuffix("ms")) / 1000.0


async def claim_once(
    conn: asyncpg.Connection,
    queue: str,
    worker_id: int = 1,
    app_version: str | None = None,
):
    """Claim through the REAL claim statement the worker uses."""
    return await conn.fetchrow(
        STMTS["claim"],
        worker_id,
        "claim-contention-test",
        queue,
        ["test"],
        1000,
        None,
        app_version,
    )


async def enqueue_many(conn: asyncpg.Connection, queue: str, count: int) -> None:
    await conn.execute(
        """INSERT INTO jorb (job_class, kwargs, queue, prio, capability, state)
           SELECT 'tests.dxe_jobs.OkJob', '{}'::jsonb, $1, 100, 'test', 'queued'
             FROM generate_series(1, $2)""",
        queue,
        count,
    )


#: The lock this change replaced, reinstated as a MEASUREMENT CONDITION so the
#: improvement is compared rather than remembered. A number from an earlier run
#: is not a baseline: this box moved 15x between two runs of ``pj-bench claim``
#: without the schema changing at all, so old and new have to be measured
#: against each other, in one run, interleaved.
TRYLOCK_CONDITION = """
CREATE OR REPLACE FUNCTION claim_queue_lock(p_queue TEXT) RETURNS BOOLEAN
LANGUAGE plpgsql AS $$
BEGIN
    RETURN pg_try_advisory_xact_lock(hashtext('pyjobby.claim:' || p_queue));
END;
$$;
"""


@contextlib.asynccontextmanager
async def claim_lock_timeout(
    conn: asyncpg.Connection, timeout: str
) -> AsyncIterator[None]:
    """Run the body with ``claim_queue_lock`` waiting `timeout` for the lock.

    ``ALTER FUNCTION ... SET lock_timeout`` is a CATALOG change, not a session
    one: it is permanent, it applies to every backend on this database, and no
    later connection can tell it was made by a test. Left behind, it silently
    retunes the claim path for the rest of the suite and for every suite after
    it -- and the schema fingerprint cannot see it, because schema.sql has not
    changed.

    So the restore is a context manager rather than a step at the end of a
    loop: it is entered and left once per swept value, and its ``finally``
    runs for ``BaseException`` too -- ``KeyboardInterrupt`` at any await inside
    the body, and the ``CancelledError`` pytest-timeout and xdist teardown
    deliver. What no ``finally`` can survive is a SIGKILL or an OOM kill, and
    that residue is caught on the other side, by ``_reset_claim_lock`` in
    tests/conftest.py, which reasserts the shipped definition before every
    test in the suite.

    The value to restore is read from the catalog on entry rather than passed
    in, so the sweep cannot restore a timeout the schema does not ship.
    """
    restore = f"{await lock_timeout_seconds(conn) * 1000:.0f}ms"
    await conn.execute(
        f"ALTER FUNCTION claim_queue_lock(TEXT) SET lock_timeout = '{timeout}'"
    )
    try:
        yield
    finally:
        await conn.execute(
            f"ALTER FUNCTION claim_queue_lock(TEXT) SET lock_timeout = '{restore}'"
        )


async def reseed(conn: asyncpg.Connection, queue: str, count: int) -> None:
    """A fresh `count` claimable jobs on a queue with no history behind them.

    The VACUUM is load-bearing for the measurements below, not hygiene: each
    round writes and then deletes thousands of jorb rows and their cascaded
    history, and left to accumulate the dead tuples slow the claim path's
    ``count(*)`` down by several times over a run. Un-vacuumed, the benchmark
    mostly reports how long it has been running.
    """
    await conn.execute("DELETE FROM jorb WHERE queue = $1", queue)
    await conn.execute("VACUUM jorb, jorb_history")
    await enqueue_many(conn, queue, count)


async def cap_queue(conn: asyncpg.Connection, queue: str, max_concurrency: int) -> None:
    await conn.execute(
        """INSERT INTO jorb_queue (name, max_concurrency) VALUES ($1, $2)
           ON CONFLICT (name) DO UPDATE SET max_concurrency = EXCLUDED.max_concurrency""",
        queue,
        max_concurrency,
    )


# ============================================================================
# 1. the wait is bounded -- the property the try-lock existed for
# ============================================================================


async def test_a_held_open_claim_does_not_freeze_the_queue(
    db_pool, db_params, unique_queue
):
    """A competing claimer returns empty-handed within the timeout, not never.

    This is the non-freezing guarantee, stated as a measurement rather than a
    hope: connection A claims inside a transaction it never commits, so it
    holds the queue's advisory lock indefinitely. B must come back -- and the
    upper bound is what makes a blocking lock admissible at all.
    """
    await cap_queue(db_pool, unique_queue, 10)
    await enqueue_many(db_pool, unique_queue, 4)
    timeout = await lock_timeout_seconds(db_pool)

    holder = await asyncpg.connect(**db_params)
    waiter = await asyncpg.connect(**db_params)
    try:
        tx = holder.transaction()
        await tx.start()
        assert await claim_once(holder, unique_queue) is not None

        started = time.perf_counter()
        blocked = await claim_once(waiter, unique_queue)
        elapsed = time.perf_counter() - started

        assert blocked is None, "a claim slipped past the lock the holder owns"
        assert elapsed >= timeout * 0.8, (
            f"the claimer came back in {elapsed * 1000:.1f} ms against a "
            f"{timeout * 1000:.0f} ms timeout -- it did not wait for the lock, "
            f"so this is still an instant give-up"
        )
        assert elapsed < timeout + 2.0, (
            f"the claimer took {elapsed:.2f} s to give up on a "
            f"{timeout * 1000:.0f} ms timeout -- the wait is not bounded"
        )
        await tx.rollback()
    finally:
        await holder.close()
        await waiter.close()


async def test_the_queue_recovers_the_instant_the_holder_commits(
    db_pool, db_params, unique_queue
):
    """The bounded wait costs nothing once the lock is free again.

    A timeout that left the queue poisoned -- claimers stuck behind a lock
    queue that never drains -- would be worse than the stampede it replaced.
    """
    await cap_queue(db_pool, unique_queue, 10)
    await enqueue_many(db_pool, unique_queue, 4)

    holder = await asyncpg.connect(**db_params)
    waiter = await asyncpg.connect(**db_params)
    try:
        tx = holder.transaction()
        await tx.start()
        await claim_once(holder, unique_queue)
        assert await claim_once(waiter, unique_queue) is None
        await tx.commit()

        assert await claim_once(waiter, unique_queue) is not None
    finally:
        await holder.close()
        await waiter.close()


async def test_waiting_claimers_are_served_in_order_not_at_random(
    db_pool, db_params, unique_queue
):
    """Waiters get served instead of being turned away.

    The point of waiting rather than retrying: fire N claims at a capped
    queue holding N jobs, all at once, and count how many come back with
    something on the FIRST attempt. Waiting puts them in the lock manager's
    FIFO queue; the try-lock hands one of them a job and tells the rest the
    queue is empty.

    Asserted against the try-lock measured in the same test rather than
    against N, because "all N" is not guaranteed on a machine busy enough to
    push the serialised critical section past the 50 ms bound -- and a
    claimer timing out there is the timeout doing its job, not a regression.
    """
    claimers = 8
    shipped = await db_pool.fetchval(
        "SELECT pg_get_functiondef('claim_queue_lock(text)'::regprocedure)"
    )
    await cap_queue(db_pool, unique_queue, claimers * 2)

    async def race(lock_sql: str) -> list[int]:
        await db_pool.execute(lock_sql)
        await db_pool.execute("DELETE FROM jorb WHERE queue = $1", unique_queue)
        await enqueue_many(db_pool, unique_queue, claimers)
        conns = [await asyncpg.connect(**db_params) for _ in range(claimers)]
        try:
            rows = await asyncio.gather(
                *(claim_once(c, unique_queue, worker_id=i) for i, c in enumerate(conns))
            )
        finally:
            await asyncio.gather(*(c.close() for c in conns))
        claimed = [r["id"] for r in rows if r is not None]
        assert len(set(claimed)) == len(claimed), "the same job was claimed twice"
        return claimed

    try:
        turned_away = len(await race(TRYLOCK_CONDITION))
        served = len(await race(shipped))
    finally:
        await db_pool.execute(shipped)

    assert served > turned_away, (
        f"waiting for the lock served {served} of {claimers} simultaneous "
        f"claims against the try-lock's {turned_away}: claimers are still "
        f"being turned away rather than queued"
    )
    assert served >= claimers // 2, (
        f"only {served} of {claimers} simultaneous claims were served; even "
        f"waiting, most claimers are coming back empty"
    )


# ============================================================================
# 2. the subtransaction does not break the lock
# ============================================================================
# (that it does not break the claiming ``SKIP LOCKED`` either is section 3,
# where the exactly-once invariant lives.)


async def test_the_lock_survives_the_exception_block_subtransaction(
    db_pool, db_params, unique_queue
):
    """Acquired inside a PL/pgSQL EXCEPTION block, still held by the caller.

    ``BEGIN ... EXCEPTION`` runs its body in an implicit subtransaction. If
    the advisory lock did not survive that subtransaction's commit, claim_jorb
    would run its cap counts and its claiming UPDATE completely unserialised
    and the caps would go back to being fooled by an invisible claim -- and
    nothing else in the suite would fail. So probe the lock directly.
    """
    await cap_queue(db_pool, unique_queue, 10)
    await enqueue_many(db_pool, unique_queue, 2)
    key = await db_pool.fetchval(CLAIM_LOCK_KEY, unique_queue)

    claimer = await asyncpg.connect(**db_params)
    prober = await asyncpg.connect(**db_params)
    try:
        assert await prober.fetchval("SELECT pg_try_advisory_xact_lock($1)", key), (
            "the queue's claim lock was already held before the test started"
        )

        tx = claimer.transaction()
        await tx.start()
        assert await claim_once(claimer, unique_queue) is not None

        held_by_someone_else = not await prober.fetchval(
            "SELECT pg_try_advisory_xact_lock($1)", key
        )
        await tx.rollback()
    finally:
        await claimer.close()
        await prober.close()

    assert held_by_someone_else, (
        "claim_jorb returned without holding the queue's advisory lock: the "
        "EXCEPTION block's subtransaction dropped it on commit"
    )


async def test_the_lock_is_released_when_the_claiming_transaction_ends(
    db_pool, db_params, unique_queue
):
    """xact-scoped means xact-scoped: rollback must free the lock too.

    A subtransaction-committed lock that leaked past its transaction would
    wedge the queue permanently for every later claimer.
    """
    await cap_queue(db_pool, unique_queue, 10)
    await enqueue_many(db_pool, unique_queue, 2)
    key = await db_pool.fetchval(CLAIM_LOCK_KEY, unique_queue)

    claimer = await asyncpg.connect(**db_params)
    prober = await asyncpg.connect(**db_params)
    try:
        tx = claimer.transaction()
        await tx.start()
        await claim_once(claimer, unique_queue)
        await tx.rollback()

        assert await prober.fetchval("SELECT pg_try_advisory_xact_lock($1)", key), (
            "the claim lock outlived the transaction that took it"
        )
    finally:
        await claimer.close()
        await prober.close()


# ============================================================================
# 3. every job is claimed exactly once under contention -- IN THE DEFAULT SUITE
# ============================================================================

#: Contention, not volume, is what exposes a broken claim. These run in the
#: default suite (and therefore in CI), so the scale is the smallest one that
#: still makes a lost or duplicated claim REACHABLE: more claimers than the
#: box has spare cores, all on one queue, each looping with no think time, so
#: every claim overlaps several others' snapshots and the serialised critical
#: section is contended continuously for the whole drain. Exactly-once is an
#: invariant -- it does not become true at 2,000 jobs and false at 600 -- so
#: the only thing scale buys here is the chance to break it, and 600 jobs
#: across 12 claimers is tens of thousands of overlapping claim attempts in
#: well under a second.
EXCLUSIVITY_CLAIMERS = 12
EXCLUSIVITY_JOBS = 600


async def _drain_collecting_ids(
    pool: asyncpg.Pool, queue: str, claimers: int, hang_guard: float = 60.0
) -> list[int]:
    """Drain `queue` with `claimers` racing claimers; return the ids claimed.

    Ids, not a count: a lost claim and a doubly-claimed job are different
    defects with the same total, and only the ids separate them.

    `hang_guard` bounds a wedged run so the failure is a test failure rather
    than a session that never ends. Nothing here asserts on elapsed time.
    """
    claimed: list[int] = []

    async def one(worker_id: int) -> None:
        async with pool.acquire() as conn:
            while True:
                row = await claim_once(conn, queue, worker_id=worker_id)
                if row is None:
                    # An empty return means "nothing claimable right now",
                    # which under contention is not the same as "empty": only
                    # an empty queue ends this claimer.
                    left = await conn.fetchval(
                        "SELECT count(*) FROM jorb WHERE queue = $1 AND state = 'queued'",
                        queue,
                    )
                    if not left:
                        return
                    continue
                claimed.append(row["id"])

    await asyncio.wait_for(
        asyncio.gather(*(one(i) for i in range(claimers))), timeout=hang_guard
    )
    return claimed


class TestClaimExclusivityUnderContention:
    """Exactly once, under contention, at a scale the default suite can afford.

    This is the invariant the whole claim path exists to keep, and it is
    asserted HERE rather than only inside the benchmarks in section 5 because
    the benchmarks are excluded by ``addopts`` and never run in CI: the same
    assertion behind ``-m performance`` proves nothing about any commit.

    Both tests run against the schema exactly as shipped -- no sweep, no
    reinstated try-lock, nothing altered in the catalog. The timeout sweep
    lives in ``TestClaimLockTimeout``, which is a comparison between
    configurations and not a statement about correctness.
    """

    async def test_a_capped_queue_drains_completely_under_concurrent_claimers(
        self, db_pool, db_params, unique_queue
    ):
        """No job lost, no job claimed twice, nothing left behind -- WITH the lock.

        The cap is set far above the job count so it can never bind: every
        claim therefore takes the queue's advisory lock and none is ever
        refused by the cap, which makes a short drain unambiguous evidence
        about the lock rather than about the cap.

        Three ways to fail, and the assertions separate them: fewer ids than
        jobs is a lost claim, a repeated id is two claimers admitted to the
        same job (what serialising exists to prevent), and a row left in any
        state but ``claimed`` is work stranded in the queue. It is also where
        the claiming ``UPDATE ... FOR UPDATE SKIP LOCKED`` meets a lock taken
        inside ``claim_queue_lock``'s ``EXCEPTION`` subtransaction: if that
        combination misbehaved, it would surface here.
        """
        await cap_queue(db_pool, unique_queue, EXCLUSIVITY_JOBS + 1000)
        await enqueue_many(db_pool, unique_queue, EXCLUSIVITY_JOBS)

        pool = await asyncpg.create_pool(
            **db_params, min_size=EXCLUSIVITY_CLAIMERS, max_size=EXCLUSIVITY_CLAIMERS
        )
        try:
            claimed = await _drain_collecting_ids(
                pool, unique_queue, EXCLUSIVITY_CLAIMERS
            )
        finally:
            await pool.close()

        assert len(claimed) == EXCLUSIVITY_JOBS, (
            f"{EXCLUSIVITY_CLAIMERS} claimers drained {len(claimed)} of "
            f"{EXCLUSIVITY_JOBS}: a claim was lost"
        )
        assert len(set(claimed)) == EXCLUSIVITY_JOBS, (
            f"{len(claimed) - len(set(claimed))} job(s) were claimed by two "
            f"claimers at once: the queue's claim lock did not serialise"
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1 AND state <> 'claimed'",
                unique_queue,
            )
            == 0
        ), "the drain returned with claimable work still in the queue"

    async def test_an_uncapped_queue_drains_completely_under_concurrent_claimers(
        self, db_pool, db_params, unique_queue
    ):
        """The same invariant on the path that takes NO lock at all.

        An uncontrolled queue -- no ``jorb_queue`` row, which is the common
        case -- never calls ``claim_queue_lock``, so exclusivity rests
        entirely on the claiming UPDATE's ``FOR UPDATE SKIP LOCKED``. That
        path carries almost all of the traffic and is the one a change to the
        locked path can quietly break (the lock would then be covering for it
        everywhere the previous test looks).
        """
        assert not await db_pool.fetchval(
            "SELECT count(*) FROM jorb_queue WHERE name = $1", unique_queue
        ), "the queue is controlled, so this is not the lock-free path"
        await enqueue_many(db_pool, unique_queue, EXCLUSIVITY_JOBS)

        pool = await asyncpg.create_pool(
            **db_params, min_size=EXCLUSIVITY_CLAIMERS, max_size=EXCLUSIVITY_CLAIMERS
        )
        try:
            claimed = await _drain_collecting_ids(
                pool, unique_queue, EXCLUSIVITY_CLAIMERS
            )
        finally:
            await pool.close()

        assert len(claimed) == EXCLUSIVITY_JOBS, (
            f"{EXCLUSIVITY_CLAIMERS} claimers drained {len(claimed)} of "
            f"{EXCLUSIVITY_JOBS} from an uncapped queue: a claim was lost"
        )
        assert len(set(claimed)) == EXCLUSIVITY_JOBS, (
            "the same job was claimed twice with no lock held: SKIP LOCKED is "
            "no longer keeping claimers off each other's rows"
        )
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb WHERE queue = $1 AND state <> 'claimed'",
                unique_queue,
            )
            == 0
        ), "the drain returned with claimable work still in the queue"


# ============================================================================
# 4. lock contention is not the same thing as a full cap
# ============================================================================


async def _drain_counting_empties(
    pool: asyncpg.Pool, queue: str, claimers: int, budget: int
) -> dict[str, int]:
    """Hammer `queue` with `claimers` until `budget` claims or nothing is left.

    Returns the claims and the empty-handed returns taken while claimable work
    was still sitting in the queue -- the only empties that mean anything.
    """
    counts = {"claims": 0, "empty_with_work": 0}
    remaining = {"left": budget}
    deadline = time.monotonic() + 60.0

    async def one(worker_id: int) -> None:
        async with pool.acquire() as conn:
            while remaining["left"] > 0 and time.monotonic() < deadline:
                row = await claim_once(conn, queue, worker_id=worker_id)
                if row is not None:
                    counts["claims"] += 1
                    remaining["left"] -= 1
                    continue
                if await conn.fetchval(
                    "SELECT count(*) FROM jorb WHERE queue = $1 AND state = 'queued'",
                    queue,
                ):
                    counts["empty_with_work"] += 1
                else:
                    remaining["left"] = 0

    await asyncio.gather(*(one(i) for i in range(claimers)))
    return counts


async def test_cap_refusals_and_lock_contention_are_different_empty_returns(
    db_pool, db_params, unique_queue
):
    """Split the two reasons a capped queue hands back nothing.

    ``pj-bench claim`` sets ``max_concurrency`` to ``jobs + 1000`` so the cap
    can never bind, which is what makes its empty-return count a pure
    contention signal -- but "can never bind" is an argument about the
    benchmark's arithmetic, and this asserts it. Same claimers, same jobs,
    two caps:

    * cap far above the job count -- the cap provably never binds, so any
      empty return is lock contention, measured under both locks so the two
      are comparable rather than guessed at.
    * cap of 2, jobs never completed -- the cap fills and STAYS full, so
      every empty return after the second claim is the cap correctly
      refusing, and no amount of lock tuning may reduce it.
    """
    claimers, jobs = 8, 400
    shipped = await db_pool.fetchval(
        "SELECT pg_get_functiondef('claim_queue_lock(text)'::regprocedure)"
    )
    pool = await asyncpg.create_pool(**db_params, min_size=claimers, max_size=claimers)
    try:
        await cap_queue(db_pool, unique_queue, jobs + 1000)

        async def loose_leg(lock_sql: str) -> dict[str, int]:
            await db_pool.execute(lock_sql)
            await reseed(db_pool, unique_queue, jobs)
            return await _drain_counting_empties(pool, unique_queue, claimers, jobs)

        trylock = await loose_leg(TRYLOCK_CONDITION)
        loose = await loose_leg(shipped)
        peak = await db_pool.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1 AND state = 'claimed'",
            unique_queue,
        )

        await cap_queue(db_pool, unique_queue, 2)
        await reseed(db_pool, unique_queue, jobs)
        # The cap can never admit more than 2, so ask for 2 and then let the
        # claimers spend a fixed number of attempts being refused.
        tight = await _drain_counting_empties(pool, unique_queue, claimers, 2)
        refused = await _drain_counting_empties(pool, unique_queue, claimers, 1)
    finally:
        await pool.close()
        await db_pool.execute(shipped)

    def miss_rate(counts: dict[str, int]) -> float:
        return counts["empty_with_work"] / max(sum(counts.values()), 1)

    print(
        f"\ncapped-queue empty returns, {claimers} claimers, {jobs} jobs:\n"
        f"  cap {jobs + 1000} (never binds), try-lock:  "
        f"{trylock['empty_with_work']:>6,} empty ({miss_rate(trylock):.1%}) "
        f"-- ALL lock contention\n"
        f"  cap {jobs + 1000} (never binds), 50ms wait: "
        f"{loose['empty_with_work']:>6,} empty ({miss_rate(loose):.1%}) "
        f"-- ALL lock contention\n"
        f"  cap 2 (full, nothing completes):            "
        f"{refused['empty_with_work']:>6,} empty "
        f"-- ALL legitimate cap refusals, on {tight['claims']} admissions"
    )

    assert peak < jobs + 1000, (
        "the loose cap was actually reached, so its empty returns are not a "
        "pure contention signal"
    )
    assert loose["claims"] == jobs
    assert loose["empty_with_work"] < trylock["empty_with_work"] * 0.25, (
        f"{loose['empty_with_work']:,} empty returns for {jobs:,} claims "
        f"against a cap that never binds, versus the try-lock's "
        f"{trylock['empty_with_work']:,}: this is still lock thrash, not the cap"
    )
    assert tight["claims"] == 2, "the cap of 2 admitted the wrong number"
    assert refused["empty_with_work"] > 0, (
        "a full cap refused nothing -- this leg proves nothing"
    )


# ============================================================================
# 5. what a lost lock costs a REAL worker (which is not a round trip)
# ============================================================================


async def test_every_worker_on_a_capped_queue_gets_to_work(
    live_worker, db_pool, unique_queue
):
    """A capped queue must be able to use all of its workers.

    ``pj-bench claim`` retries in a tight loop, so it prices a lost lock at
    one wasted round trip. A real worker does something far more expensive:
    an empty claim makes it publish demand (``_set_idle(True)``, which
    switches this queue's enqueue notifications back on for every producer),
    claim once more, and then PARK for ``checkInterval`` -- five seconds by
    default -- waiting for a wakeup that will never come, because the work it
    wanted was already enqueued.

    Under the try-lock that was the normal outcome of eight claimers meeting
    one queue: whoever won kept winning, and everybody else went to sleep on
    a queue that was full of claimable work and nowhere near its cap. Waiting
    for the lock puts them in a FIFO queue instead, so they are served.
    """
    workers, jobs = 4, 40
    shipped = await db_pool.fetchval(
        "SELECT pg_get_functiondef('claim_queue_lock(text)'::regprocedure)"
    )
    await cap_queue(db_pool, unique_queue, jobs + 1000)  # can never bind
    for _ in range(workers):
        await live_worker(checkInterval=2.0)

    async def drain_once(lock_sql: str) -> tuple[int, float]:
        await db_pool.execute(lock_sql)
        await db_pool.execute("DELETE FROM jorb WHERE queue = $1", unique_queue)
        await enqueue_many(db_pool, unique_queue, jobs)
        started = time.perf_counter()
        while time.perf_counter() - started < 30:
            if (
                await db_pool.fetchval(
                    "SELECT count(*) FROM jorb WHERE queue = $1 AND state = 'finished'",
                    unique_queue,
                )
                == jobs
            ):
                break
            await asyncio.sleep(0.05)
        elapsed = time.perf_counter() - started
        used: int = await db_pool.fetchval(
            "SELECT count(DISTINCT claimed_by) FROM jorb "
            "WHERE queue = $1 AND claimed_by IS NOT NULL",
            unique_queue,
        )
        finished = await db_pool.fetchval(
            "SELECT count(*) FROM jorb WHERE queue = $1 AND state = 'finished'",
            unique_queue,
        )
        assert finished == jobs, (
            f"the capped queue drained only {finished} of {jobs} in {elapsed:.1f}s"
        )
        return used, elapsed

    try:
        trylock_used, trylock_seconds = await drain_once(TRYLOCK_CONDITION)
        wait_used, wait_seconds = await drain_once(shipped)
    finally:
        await db_pool.execute(shipped)

    print(
        f"\n{workers} workers, {jobs} jobs, cap that never binds:\n"
        f"  try-lock:    {trylock_used} of {workers} workers ever claimed "
        f"({trylock_seconds:.2f}s)\n"
        f"  50ms wait:   {wait_used} of {workers} workers ever claimed "
        f"({wait_seconds:.2f}s)"
    )

    assert wait_used > trylock_used, (
        f"waiting for the lock got {wait_used} of {workers} workers into the "
        f"queue against the try-lock's {trylock_used}: workers are still "
        f"losing the claim lock and parking instead of waiting for it"
    )
    assert trylock_used < workers, (
        "the try-lock also managed to use every worker, so this test no "
        "longer demonstrates anything -- retune it or delete it"
    )


# ============================================================================
# 6. what the bounded wait is worth (-m performance -s)
# ============================================================================
# CONFIGURATION COMPARISONS, not correctness. Everything below is excluded
# from the default suite by `addopts` and therefore never runs in CI, so no
# invariant may live here: exactly-once is pinned in section 3, at CI scale,
# where a regression can actually fail a build.

#: Conditions are measured round-robin and reduced by median: a single ordered
#: pass would report accumulating dead tuples, an autovacuum waking up or a
#: checkpoint as if it were a result.
PERF_ROUNDS = 3
PERF_CLAIMERS = 8
PERF_JOBS = 2000


#: How long a claimer is allowed to wait is the one tunable this change has,
#: so it is swept rather than asserted about. 1 ms stands in for the try-lock
#: that used to be here (give up essentially immediately).
TIMEOUT_SWEEP = ("1ms", "5ms", "20ms", "50ms", "200ms")


@pytest.mark.slow
@pytest.mark.performance
class TestClaimLockTimeout:
    """Why 50 ms, measured: where does waiting stop buying anything?

    The timeout is the maximum a claimer stalls behind a held-open claim, so
    it trades claim latency against wasted round trips. Too small and the
    stampede comes back; too large and a stuck transaction stalls claimers
    for longer than it has to. The knee is an empirical question about how
    long the serialised critical section actually takes on this hardware.

    A COMPARISON BETWEEN CONFIGURATIONS, and nothing more. That claims stay
    exactly-once under contention is not asserted here -- it is
    ``TestClaimExclusivityUnderContention`` in section 3, which runs in the
    default suite. This class only runs when someone asks for it, so an
    invariant asserted here would be an invariant nobody checks.
    """

    async def test_sweep_the_claim_lock_timeout(self, db_pool, db_params, unique_queue):
        installed = await lock_timeout_seconds(db_pool)
        pool = await asyncpg.create_pool(
            **db_params, min_size=PERF_CLAIMERS, max_size=PERF_CLAIMERS
        )
        rates: dict[str, list[float]] = {t: [] for t in TIMEOUT_SWEEP}
        empties: dict[str, list[int]] = {t: [] for t in TIMEOUT_SWEEP}
        try:
            await cap_queue(db_pool, unique_queue, PERF_JOBS + 1000)
            for round_no in range(PERF_ROUNDS + 1):
                for timeout in TIMEOUT_SWEEP:
                    # Persistent catalog state, scoped to one measured drain
                    # and restored on the way out of every exit -- see
                    # claim_lock_timeout().
                    async with claim_lock_timeout(db_pool, timeout):
                        await reseed(db_pool, unique_queue, PERF_JOBS)
                        started = time.perf_counter()
                        counts = await _drain_counting_empties(
                            pool, unique_queue, PERF_CLAIMERS, PERF_JOBS
                        )
                        elapsed = time.perf_counter() - started
                    # A measurement precondition, not the exclusivity
                    # invariant: it says the interval just timed covers a
                    # WHOLE drain, so claims/s below is a rate for the same
                    # work under every swept timeout. (It would be satisfied
                    # by a drain that claimed a job twice; what may not be
                    # satisfied is comparing 2,000 claims against 400.)
                    assert counts["claims"] == PERF_JOBS, (
                        f"the drain at {timeout} ended after "
                        f"{counts['claims']} of {PERF_JOBS} claims, so this "
                        f"round measured less work than the others"
                    )
                    if round_no:  # round 0 is warmup
                        rates[timeout].append(counts["claims"] / elapsed)
                        empties[timeout].append(counts["empty_with_work"])
        finally:
            await pool.close()

        lines = "\n".join(
            f"  {timeout:>6}: {statistics.median(rates[timeout]):>9,.0f} claims/s, "
            f"{statistics.median(empties[timeout]):>7,.0f} empty returns per "
            f"{PERF_JOBS:,} claims"
            for timeout in TIMEOUT_SWEEP
        )
        print(
            f"\nCLAIM LOCK TIMEOUT sweep, {PERF_CLAIMERS} claimers x "
            f"{PERF_JOBS:,} jobs on a cap that never binds, median of "
            f"{PERF_ROUNDS}:\n{lines}\n"
            f"  installed: {installed * 1000:.0f}ms"
        )

        # Gate on the waste, not on the rate. Claim rate across the sweep is
        # noise on a shared machine -- the same five conditions measured 627 /
        # 839 / 766 / 830 / 862 claims/s on a quiet box and 219 / 616 / 691 /
        # 618 / 239 on a loaded one -- while the empty-return counts stay a
        # clean step function in both. So the assertions are: the installed
        # timeout has taken the stampede away, and it did not cost rate
        # against the near-instant give-up it replaced.
        key = f"{installed * 1000:.0f}ms"
        instant, chosen = TIMEOUT_SWEEP[0], key
        assert chosen in rates, f"{key} is not in the sweep; retune TIMEOUT_SWEEP"
        waste = {t: statistics.median(empties[t]) for t in TIMEOUT_SWEEP}
        rate = {t: statistics.median(rates[t]) for t in TIMEOUT_SWEEP}

        assert waste[chosen] <= waste[instant] * 0.05, (
            f"{waste[chosen]:,.0f} wasted round trips at the installed "
            f"{key} against {waste[instant]:,.0f} at {instant} (the try-lock's "
            f"regime): the timeout is too short for this critical section"
        )
        assert rate[chosen] > rate[instant] * 0.9, (
            f"waiting {key} for the lock costs throughput against giving up "
            f"at {instant} ({rate[instant]:,.0f} -> {rate[chosen]:,.0f} "
            f"claims/s)"
        )


@pytest.mark.slow
@pytest.mark.performance
class TestCappedClaimThroughput:
    """What the bounded wait buys, against the try-lock, in the same run.

    Same measurement ``pj-bench claim`` makes -- 8 claimers draining a capped
    queue whose cap is far above the job count, so it never binds and every
    empty return is contention -- with the try-lock reinstated as a third
    condition and all three interleaved round-robin.

    WHAT THIS MEASURES, and what it does not. Removing ~98% of the wasted
    round trips does not reliably move capped claims/second -- 0.96x on an
    idle box, 2.1x on a saturated one -- and the reason is structural: a
    claimer that loses the try-lock holds no lock, so its retry never delayed
    the winner. Capped throughput is 1 / (serialised critical section) and
    always was; the round trips were being wasted BESIDE the bottleneck, not
    inside it, which is why taking them away helps exactly as much as the
    CPU they were stealing and no more.

    So the capped/uncapped ratio is not a contention number to be tuned away.
    Only two things can raise it: making the critical section cheaper, or
    doing more than one claim per acquisition -- BATCHING. Batching is the
    real answer for throughput and it is a pj.py change (the worker's
    one-job-at-a-time model), not a schema one. This test therefore asserts
    what the lock change is actually responsible for -- the wasted round trips
    are gone and throughput did not regress -- and leaves the ratio alone.
    The win that made the change worth shipping is not here: it is
    ``test_every_worker_on_a_capped_queue_gets_to_work``, which is the same
    contention priced the way a real worker pays for it.

    Nor is the correctness of any of these three conditions here. That every
    job is claimed exactly once -- capped (lock held) and uncapped (no lock at
    all), the two conditions this benchmark contrasts -- is asserted by
    ``TestClaimExclusivityUnderContention`` in section 3, which runs in the
    default suite and in CI.
    """

    async def test_the_bounded_wait_stops_the_stampede_without_costing_rate(
        self, db_pool, db_params, unique_queue
    ):
        shipped = await db_pool.fetchval(
            "SELECT pg_get_functiondef('claim_queue_lock(text)'::regprocedure)"
        )
        pool = await asyncpg.create_pool(
            **db_params, min_size=PERF_CLAIMERS, max_size=PERF_CLAIMERS
        )
        conditions = ("uncapped", "capped_trylock", "capped_wait")
        rates: dict[str, list[float]] = {c: [] for c in conditions}
        misses: dict[str, list[int]] = {c: [] for c in conditions}
        try:
            for round_no in range(PERF_ROUNDS + 1):
                for condition in conditions:
                    await db_pool.execute(
                        "DELETE FROM jorb_queue WHERE name = $1", unique_queue
                    )
                    if condition != "uncapped":
                        # Far above the job count: the cap can never bind, so
                        # every empty return is contention and nothing else.
                        await cap_queue(db_pool, unique_queue, PERF_JOBS + 1000)
                    await db_pool.execute(
                        TRYLOCK_CONDITION if condition == "capped_trylock" else shipped
                    )
                    await reseed(db_pool, unique_queue, PERF_JOBS)

                    started = time.perf_counter()
                    counts = await _drain_counting_empties(
                        pool, unique_queue, PERF_CLAIMERS, PERF_JOBS
                    )
                    elapsed = time.perf_counter() - started
                    # As in the sweep: a measurement precondition (the timed
                    # interval covers a whole drain, so the three conditions
                    # are rates for the same work), not the exactly-once
                    # invariant, which is section 3's and runs in CI.
                    assert counts["claims"] == PERF_JOBS, (
                        f"the {condition} drain ended after {counts['claims']} "
                        f"of {PERF_JOBS} claims, so this condition measured "
                        f"less work than the others"
                    )
                    if round_no:  # round 0 is warmup: connections, plans, cache
                        rates[condition].append(counts["claims"] / elapsed)
                        misses[condition].append(counts["empty_with_work"])
        finally:
            await pool.close()
            await db_pool.execute(shipped)

        rate = {c: statistics.median(rates[c]) for c in conditions}
        empty = {c: statistics.median(misses[c]) for c in conditions}
        print(
            f"\nCLAIM, {PERF_CLAIMERS} claimers x {PERF_JOBS:,} jobs, "
            f"median of {PERF_ROUNDS}:\n"
            f"  uncapped       (no lock taken):  {rate['uncapped']:>9,.0f} "
            f"claims/s\n"
            f"  capped, trylock (as it was):     "
            f"{rate['capped_trylock']:>9,.0f} claims/s  "
            f"{rate['capped_trylock'] / rate['uncapped']:.2f}x uncapped, "
            f"{empty['capped_trylock']:,.0f} wasted round trips\n"
            f"  capped, 50ms wait (as shipped):  "
            f"{rate['capped_wait']:>9,.0f} claims/s  "
            f"{rate['capped_wait'] / rate['uncapped']:.2f}x uncapped, "
            f"{empty['capped_wait']:,.0f} wasted round trips\n"
            f"  round trips saved: "
            f"{1 - empty['capped_wait'] / max(empty['capped_trylock'], 1):.1%}, "
            f"claim rate: "
            f"{rate['capped_wait'] / rate['capped_trylock']:.2f}x"
        )

        assert rate["capped_wait"] > rate["capped_trylock"] * 0.9, (
            f"waiting for the lock COST throughput "
            f"({rate['capped_trylock']:,.0f} -> {rate['capped_wait']:,.0f} "
            f"claims/s): claimers are timing out where the try-lock let them "
            f"retry, so the timeout is too short for this critical section"
        )
        assert empty["capped_wait"] < empty["capped_trylock"] * 0.1, (
            f"the bounded wait still wastes {empty['capped_wait']:,.0f} round "
            f"trips per {PERF_JOBS:,} claims against the try-lock's "
            f"{empty['capped_trylock']:,.0f}; claimers are losing the lock "
            f"rather than waiting for it"
        )
