"""Job tags: the caller's OWN labels, and what indexing them costs.

`jorb.tags` exists so a job can be found by something the application means
-- customer, tenant, region, batch -- rather than by something the platform
means. That is a different column from `admin_data` on purpose: admin_data
holds retry counts, timeouts and schedule bookkeeping, which nobody filters
on, so indexing it would tax every enqueue to make no query faster.

Three things have to hold at once, and each one is a test class below:

* the filter is EXACT (tagged jobs, and only those, by id);
* the untagged enqueue path -- the hot one -- does not pay for the index,
  which is what the partial predicate `WHERE tags <> '{}'` is for;
* the filter is answered by the index at scale, not by reading the table.

The write-cost class is a MEASUREMENT, kept here rather than in a scratch
script because the number is the whole justification for the index: if
tagged enqueue ever gets materially more expensive than untagged enqueue,
this file is where that shows up.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
from click.testing import CliRunner

from pyjobby.admin_api import AdminAPI
from pyjobby.cli import cli
from pyjobby.client import tags_filter_sql
from tests.utils.plans import (
    assert_no_seq_scan,
    assert_reads_far_less_than_a_scan,
    plan_for,
    reset_job_tables,
    rows_removed_by_filter,
    settle,
)

pytestmark = pytest.mark.asyncio

# Enough rows that the planner has a real choice; below this a sequential
# scan genuinely IS the cheaper plan and a plan assertion proves nothing.
ROWS = 20_000

# A value no ASCII-only round trip would survive by accident.
NON_ASCII = "Ünïcodé — 東京 — café"


def dsn_for(db_params: dict) -> str:
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
    )


async def run_cli(*args: str):
    """Invoke pj-admin in a worker thread (the CLI owns its own event loop)."""

    def _invoke():
        return CliRunner().invoke(cli, list(args))

    return await asyncio.to_thread(_invoke)


class TestTagsRoundTrip:
    """What went in comes back out, unchanged and unflattened."""

    async def test_tags_survive_the_round_trip(self, client, unique_queue):
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob",
            queue=unique_queue,
            tags={
                "customer": "acme",
                "region": NON_ASCII,
                "batch": 7,
                "reprocess": True,
                "note": None,
            },
        )

        stored = await client.pool.fetchval(
            "SELECT tags FROM jorb WHERE id = $1", job_id
        )

        assert stored == {
            "customer": "acme",
            "region": NON_ASCII,
            "batch": 7,
            "reprocess": True,
            "note": None,
        }

    async def test_an_untagged_job_stores_the_empty_object(self, client, unique_queue):
        """NOT NULL DEFAULT '{}', which is what the partial index keys off."""
        job_id = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue)

        assert (
            await client.pool.fetchval("SELECT tags FROM jorb WHERE id = $1", job_id)
            == {}
        )

    async def test_tags_are_copied_not_captured(self, client, unique_queue):
        """The stored row is not a live view of the caller's dict."""
        tags = {"customer": "acme"}
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, tags=tags
        )
        tags["customer"] = "changed-after-enqueue"

        assert await client.pool.fetchval(
            "SELECT tags FROM jorb WHERE id = $1", job_id
        ) == {"customer": "acme"}

    async def test_tags_stay_out_of_admin_data(self, client, unique_queue):
        """The two columns are separate on purpose; keep them separate."""
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob",
            queue=unique_queue,
            tags={"customer": "acme"},
            max_retries=3,
        )

        row = await client.pool.fetchrow(
            "SELECT tags, admin_data FROM jorb WHERE id = $1", job_id
        )

        assert row["tags"] == {"customer": "acme"}
        assert "customer" not in row["admin_data"]
        assert row["admin_data"]["max_retries"] == 3

    @pytest.mark.parametrize(
        "tags",
        [
            {"nested": {"a": 1}},
            {"listy": [1, 2]},
            {"": "empty key"},
            {7: "int key"},
        ],
    )
    async def test_unfilterable_tags_are_refused_at_enqueue(
        self, client, unique_queue, tags
    ):
        """A tag that cannot be expressed as `--tag key=value` is rejected.

        Accepting it would store something that no filter can ever find,
        which is worse than refusing it: the caller believes the job is
        labelled.
        """
        with pytest.raises(ValueError):
            await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue, tags=tags)


class TestTagFiltering:
    """Exactly the tagged jobs, by id -- never a count."""

    @pytest.fixture(autouse=True)
    async def jobs(self, client, unique_queue):
        """Three tagged jobs and one untagged one, all in the same queue."""
        self.acme = await client.enqueue(
            "tests.dxe_jobs.OkJob",
            queue=unique_queue,
            tags={"customer": "acme", "region": "eu"},
        )
        self.acme_us = await client.enqueue(
            "tests.dxe_jobs.OkJob",
            queue=unique_queue,
            tags={"customer": "acme", "region": "us"},
        )
        self.globex = await client.enqueue(
            "tests.dxe_jobs.OkJob",
            queue=unique_queue,
            tags={"customer": "globex", "region": "eu"},
        )
        self.untagged = await client.enqueue("tests.dxe_jobs.OkJob", queue=unique_queue)

    async def test_filter_returns_exactly_the_tagged_jobs(
        self, db_connection, unique_queue
    ):
        api = AdminAPI(db_connection)

        jobs = await api.list_jobs(queue=unique_queue, tags={"customer": "acme"})

        assert {j["id"] for j in jobs} == {self.acme, self.acme_us}

    async def test_another_customers_tag_is_excluded(self, db_connection, unique_queue):
        api = AdminAPI(db_connection)

        jobs = await api.list_jobs(queue=unique_queue, tags={"customer": "globex"})

        assert {j["id"] for j in jobs} == {self.globex}

    async def test_repeated_tags_intersect(self, db_connection, unique_queue):
        """Two pairs mean AND, not OR."""
        api = AdminAPI(db_connection)

        jobs = await api.list_jobs(
            queue=unique_queue, tags={"customer": "acme", "region": "eu"}
        )

        assert {j["id"] for j in jobs} == {self.acme}

    async def test_extra_tags_on_the_job_do_not_disqualify_it(
        self, db_connection, unique_queue
    ):
        """Containment, not equality: one pair finds a job carrying two."""
        api = AdminAPI(db_connection)

        jobs = await api.list_jobs(queue=unique_queue, tags={"region": "eu"})

        assert {j["id"] for j in jobs} == {self.acme, self.globex}

    async def test_an_unknown_tag_matches_nothing(self, db_connection, unique_queue):
        api = AdminAPI(db_connection)

        assert (
            await api.list_jobs(queue=unique_queue, tags={"customer": "initech"}) == []
        )

    async def test_no_tag_filter_still_returns_everything(
        self, db_connection, unique_queue
    ):
        """The untagged job is unaffected by the feature existing."""
        api = AdminAPI(db_connection)

        jobs = await api.list_jobs(queue=unique_queue)

        assert {j["id"] for j in jobs} == {
            self.acme,
            self.acme_us,
            self.globex,
            self.untagged,
        }

    async def test_tags_are_reported_on_every_job(self, db_connection, unique_queue):
        """Including the untagged one, which reports '{}' rather than null."""
        api = AdminAPI(db_connection)

        by_id = {j["id"]: j for j in await api.list_jobs(queue=unique_queue)}

        assert by_id[self.acme]["tags"] == {"customer": "acme", "region": "eu"}
        assert by_id[self.untagged]["tags"] == {}

    async def test_client_search_filters_by_tag(self, client, unique_queue):
        jobs = await client.search_jobs(tags={"customer": "acme"})

        assert {j["id"] for j in jobs} == {self.acme, self.acme_us}

    async def test_non_ascii_tag_values_filter_exactly(self, client, unique_queue):
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, tags={"city": NON_ASCII}
        )

        jobs = await client.search_jobs(tags={"city": NON_ASCII})

        assert {j["id"] for j in jobs} == {job_id}


class TestUntaggedJobsStayOutOfTheIndex:
    """The partial predicate, asserted rather than assumed.

    `WHERE tags <> '{}'` is the entire reason this index is affordable on
    the hottest table in the system. If the predicate were wrong -- or if
    the column defaulted to something that is not literally `'{}'` -- every
    enqueue would write a GIN entry and nothing else here would notice.
    """

    async def test_ten_thousand_untagged_jobs_do_not_grow_the_index(
        self, db_pool, unique_queue
    ):
        await reset_job_tables(db_pool)
        await settle(db_pool)
        empty = await index_size(db_pool)

        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, queue, kwargs)
            SELECT 'tags.Job', $1, '{}' FROM generate_series(1, 10000)
            """,
            unique_queue,
        )
        await settle(db_pool)

        assert await index_size(db_pool) == empty, (
            "the tags index grew for jobs that set no tags: the partial "
            "predicate is not doing its job"
        )

    async def test_tagged_jobs_do_grow_the_index(self, db_pool, unique_queue):
        """The control: without this, the test above passes on a dead index."""
        await reset_job_tables(db_pool)
        await settle(db_pool)
        empty = await index_size(db_pool)

        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, queue, kwargs, tags)
            SELECT 'tags.Job', $1, '{}',
                   jsonb_build_object('customer', 'c' || (i % 50))
            FROM generate_series(1, 10000) i
            """,
            unique_queue,
        )
        await settle(db_pool)

        assert await index_size(db_pool) > empty


async def index_size(pool: asyncpg.Pool) -> int:
    """Bytes held by the tags index, with GIN's pending list flushed.

    `fastupdate` parks new entries in an unsorted pending list and merges
    them later, so an index measured without the flush reports whatever the
    background happened to have done -- which is not a fact a test can
    assert on. VACUUM merges the list, so the number below is the index's
    real size either way.
    """
    size: int = await pool.fetchval("SELECT pg_relation_size('jorb_tags_idx')")
    return size


class TestTagFilterPlan:
    """At 20,000 rows the filter must probe the index, not read the table."""

    @pytest.fixture(autouse=True)
    async def seeded(self, db_pool):
        """A large untagged history with a small tagged slice inside it.

        That asymmetry is the point: tags are rare, which is what makes the
        index worth having and what makes a sequential scan the wrong plan.
        Truncates first, so the measurement does not depend on what ran
        before it.
        """
        await reset_job_tables(db_pool)
        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, state, created, updated,
                              run_after, tags)
            SELECT 'tags.Job', '{}', 'plan_tags', 'finished'::jorbstate,
                   now() - (i % 60) * interval '1 day',
                   now() - (i % 60) * interval '1 day',
                   now() - (i % 60) * interval '1 day',
                   -- Rare on purpose. Tags being rare is both why the
                   -- partial index is affordable and why a sequential scan
                   -- is the wrong plan; seeding them onto a big fraction of
                   -- the table would make a scan correct and the assertion
                   -- meaningless.
                   CASE WHEN i % 1000 = 0
                        THEN jsonb_build_object('customer', 'acme',
                                                'region', 'eu')
                        ELSE '{}'::jsonb END
            FROM generate_series(1, $1) i
            """,
            ROWS,
        )
        await settle(db_pool)

    # The filter is bound as a DICT, never as json.dumps(...): every pyjobby
    # connection carries a jsonb codec, so a string parameter is encoded as a
    # JSON string and `tags @> '"{\"customer\": \"acme\"}"'` matches nothing
    # while still looking like a real query in EXPLAIN.

    async def test_tag_filter_uses_the_gin_index(self, db_pool):
        plan = await plan_for(
            db_pool,
            f"SELECT id FROM jorb WHERE {tags_filter_sql(1)}",
            {"customer": "acme"},
        )

        assert "jorb_tags_idx" in plan, plan
        assert_no_seq_scan(plan)

    async def test_tag_filter_does_not_read_the_table_to_answer(self, db_pool):
        plan = await plan_for(
            db_pool,
            f"SELECT id FROM jorb WHERE {tags_filter_sql(1)}",
            {"customer": "acme"},
        )

        # Necessary alongside the index assertion above: an index scan that
        # reads everything and discards it costs the same as a scan.
        assert rows_removed_by_filter(plan) == 0, plan
        await assert_reads_far_less_than_a_scan(db_pool, plan)

    async def test_an_absent_tag_is_still_answered_by_the_index(self, db_pool):
        """The empty answer is the expensive one if it is not indexed."""
        plan = await plan_for(
            db_pool,
            f"SELECT id FROM jorb WHERE {tags_filter_sql(1)}",
            {"customer": "initech"},
        )

        assert "jorb_tags_idx" in plan, plan
        assert_no_seq_scan(plan)
        await assert_reads_far_less_than_a_scan(db_pool, plan)

    async def test_the_admin_api_query_itself_uses_the_index(self, db_pool):
        """Not a hand-written probe -- the SQL list_jobs actually builds."""
        plan = await plan_for(
            db_pool,
            "SELECT * FROM jorb WHERE queue = $1 AND "
            f"{tags_filter_sql(2)} ORDER BY created DESC LIMIT 50",
            "plan_tags",
            {"customer": "acme"},
        )

        assert "jorb_tags_idx" in plan, plan
        assert_no_seq_scan(plan)

    async def test_dropping_the_predicate_costs_the_index(self, db_pool):
        """Why tags_filter_sql emits `tags <> '{}'` beside the containment.

        This is the trap the whole shape of that helper exists to avoid, and
        it is asserted rather than described because the failure is
        invisible: the query is CORRECT either way, returns the same rows,
        and simply reads the entire table to do it. Anyone "simplifying" the
        redundant-looking clause away breaks the index and nothing else in
        the suite notices.
        """
        plan = await plan_for(
            db_pool,
            "SELECT id FROM jorb WHERE tags @> $1::jsonb",
            {"customer": "acme"},
        )

        assert "jorb_tags_idx" not in plan, (
            "PostgreSQL now proves `tags <> '{}'` from `tags @> ...` -- if "
            "that is real, tags_filter_sql can drop the extra clause"
        )
        assert rows_removed_by_filter(plan) >= ROWS - 100, plan


class TestTagCliFiltering:
    """`pj-admin jobs list --tag key=value`, including how it fails."""

    @pytest.fixture
    def dsn(self, db_params: dict) -> str:
        return dsn_for(db_params)

    async def test_tag_filter_lists_only_the_tagged_job(
        self, client, dsn, unique_queue
    ):
        tagged = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, tags={"customer": "acme"}
        )
        other = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, tags={"customer": "globex"}
        )

        result = await run_cli(
            "--dsn", dsn, "jobs", "list", "--tag", "customer=acme", "--json"
        )

        assert result.exit_code == 0, result.output
        assert [j["id"] for j in json.loads(result.output)] == [tagged]
        assert str(other) not in result.output

    async def test_a_json_value_matches_a_numeric_tag(self, client, dsn, unique_queue):
        """`--tag batch=7` has to find the number 7, not the string "7"."""
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, tags={"batch": 7}
        )

        result = await run_cli(
            "--dsn", dsn, "jobs", "list", "--tag", "batch=7", "--json"
        )

        assert result.exit_code == 0, result.output
        assert [j["id"] for j in json.loads(result.output)] == [job_id]

    async def test_repeating_the_flag_intersects(self, client, dsn, unique_queue):
        both = await client.enqueue(
            "tests.dxe_jobs.OkJob",
            queue=unique_queue,
            tags={"customer": "acme", "region": "eu"},
        )
        await client.enqueue(
            "tests.dxe_jobs.OkJob",
            queue=unique_queue,
            tags={"customer": "acme", "region": "us"},
        )

        result = await run_cli(
            "--dsn",
            dsn,
            "jobs",
            "list",
            "--tag",
            "customer=acme",
            "--tag",
            "region=eu",
            "--json",
        )

        assert result.exit_code == 0, result.output
        assert [j["id"] for j in json.loads(result.output)] == [both]

    async def test_inspect_shows_the_tags(self, client, dsn, unique_queue):
        job_id = await client.enqueue(
            "tests.dxe_jobs.OkJob", queue=unique_queue, tags={"customer": "acme"}
        )

        result = await run_cli("--dsn", dsn, "jobs", "inspect", str(job_id))

        assert result.exit_code == 0, result.output
        assert "acme" in result.output

    @pytest.mark.parametrize(
        "bad",
        ["customer", "=acme", 'k={"a": 1}', "k=[1, 2]"],
    )
    async def test_a_malformed_tag_fails_loudly(self, dsn, bad):
        """Never a silent widening to "all jobs" with exit 0.

        A mistyped filter that returns the whole table while reporting
        success is the failure this repo's non-zero-exit rule exists to
        prevent: a runbook chaining `pj-admin ... && next-step` would take
        the wrong action on every job in the system.
        """
        result = await run_cli("--dsn", dsn, "jobs", "list", "--tag", bad)

        # 2, the status click itself uses for bad arguments: this filter was
        # refused before anything was attempted
        assert result.exit_code == 2
        assert f"Error: Malformed --tag {bad!r}" in result.stderr

    async def test_a_malformed_tag_never_reaches_the_database(self):
        """Parsed before connecting: the message must not depend on the db."""
        result = await run_cli(
            "--dsn",
            "postgresql://nobody:nobody@127.0.0.1:1/nothing",
            "jobs",
            "list",
            "--tag",
            "customer",
        )

        assert result.exit_code == 2
        assert "Error: Malformed --tag 'customer': expected key=value" in result.stderr
        assert "Failed to connect to database" not in result.stderr


@pytest.mark.performance
class TestTagWriteCost:
    """What the GIN index costs the write path, measured rather than assumed.

    The write path is the scarcest resource in this system (docs/SCALE.md
    rejected a rollup table for exactly this reason), so the index only
    earns its place if:

    * an UNTAGGED enqueue is unaffected -- it never matches `tags <> '{}'`,
      so it must not touch the index at all; and
    * a TAGGED enqueue costs a few percent, not a multiple.

    Both arms run interleaved in one process against the same database, so
    a busy machine slows both equally instead of manufacturing a finding.
    Asserted against a deliberately loose bound (tagged enqueue within 2x of
    untagged) because a tight one on a shared box is a flake, not a gate;
    the numbers themselves are printed for the operator reading the log.
    """

    # Bounded by the test pool (max_size 10). The concurrency is what makes
    # the measurement mean anything -- a single connection never exposes
    # what concurrent commits cost each other -- but the two arms only have
    # to be comparable to each other, so the exact width is not the point.
    CONCURRENCY = 8
    PER_CONNECTION = 250
    ROUNDS = 7

    async def test_tagged_enqueue_does_not_cost_a_multiple(self, db_pool, unique_queue):
        arms: dict[str, list[float]] = {"untagged": [], "shared": [], "unique": []}
        # Warm up every arm first: the first round pays for connection setup,
        # statement preparation and an unwritten relation, and whichever arm
        # went first would otherwise wear all of it.
        for name in arms:
            await self._round(db_pool, unique_queue, name)
        for _ in range(self.ROUNDS):
            # Round-robin, not "all of A then all of B": a machine that gets
            # busy halfway through would otherwise hand back a clean-looking
            # regression that is entirely someone else's load.
            for name, samples in arms.items():
                samples.append(await self._round(db_pool, unique_queue, name))

        rates = {name: median(samples) for name, samples in arms.items()}
        baseline = rates["untagged"]
        print(
            f"\nenqueue {self.CONCURRENCY}x{self.PER_CONNECTION}, one txn per "
            f"job, median of {self.ROUNDS}:\n"
            + "\n".join(
                f"  {name:<9} {rate:>9,.0f} jobs/s  ({rate / baseline:.2f}x)"
                for name, rate in rates.items()
            )
        )

        for name, rate in rates.items():
            assert rate * 2 > baseline, (
                f"{name} enqueue at {rate:,.0f} jobs/s against untagged "
                f"{baseline:,.0f}: the GIN index is not paying for itself"
            )

    #: What each arm is FOR. "unique" is the pessimal case on purpose: GIN
    #: amortises repeated keys into one posting list, so a benchmark that
    #: writes the same tag every time measures the friendly half of the
    #: distribution and calls the index free.
    ARMS = {
        "untagged": "no tags at all -- must never touch the index",
        "shared": "one tag every job shares (a customer, a region)",
        "unique": "a distinct tag value per job -- worst case for GIN",
    }

    async def _round(self, pool, queue: str, arm: str) -> float:
        """Jobs per second for one round of concurrent single-job commits.

        One transaction per job on several connections at once, because that
        is what a real enqueue path does and the only shape that exposes the
        commit-time costs -- a bulk insert amortises them away (see
        pyjobby/bench.py).
        """
        await pool.execute("DELETE FROM jorb WHERE queue = $1", queue)

        async def worker(n: int) -> None:
            async with pool.acquire() as conn:
                for i in range(self.PER_CONNECTION):
                    if arm == "untagged":
                        tags: dict[str, str] = {}
                    elif arm == "shared":
                        tags = {"customer": "acme"}
                    else:
                        tags = {"customer": f"acme-{n}-{i}"}
                    await conn.execute(
                        "INSERT INTO jorb (job_class, queue, kwargs, tags) "
                        "VALUES ('tags.Job', $1, '{}', $2)",
                        queue,
                        tags,
                    )

        loop = asyncio.get_running_loop()
        started = loop.time()
        await asyncio.gather(*(worker(n) for n in range(self.CONCURRENCY)))
        elapsed = loop.time() - started

        return (self.CONCURRENCY * self.PER_CONNECTION) / elapsed


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
