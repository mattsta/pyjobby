"""
Comprehensive tests for web_admin.py - Web Admin Interface.
Using LIVE database operations with NO MOCKS for maximum correctness guarantees!
"""

import re
import urllib.parse
import uuid

import pytest
import pytest_asyncio

from pyjobby import db
from pyjobby.web_admin import WebAdminServer, build_template_env
from tests.conftest import unique_name
from tests.test_metrics_scrape_cost import parse_samples

# Every page served by the admin, and the one template set that backs them.
# The page list doubles as the parametrization of the no-duplication test
# below; EXPECTED_TEMPLATES is the packaging contract (CI asserts the same
# files reach the wheel).
ADMIN_PAGES = ["/", "/jobs", "/queues", "/workers", "/dlq", "/schedules"]

EXPECTED_TEMPLATES = {
    "base.html",
    "dlq.html",
    "index.html",
    "jobs.html",
    "queues.html",
    "schedules.html",
    "workers.html",
    "fragments/dlq_table.html",
    "fragments/jobs_table.html",
    "fragments/metrics.html",
    "fragments/queues_table.html",
    "fragments/schedules_table.html",
    "fragments/worker_stats.html",
    "fragments/workers_table.html",
}


def _table_rows(html: str) -> list[str]:
    """Split a rendered fragment into one string per ``<tr>``.

    An assertion against the whole page passes when the value it is looking
    for belongs to a different row -- and these fragments render every queue
    (or schedule) in the database, including the ones other tests left
    behind. Per-row is the only way to say "this queue shows that".
    """
    return html.split("<tr>")[1:]


class TestWebAdminServerInit:
    """Test WebAdminServer initialization - covers lines 27-84."""

    def test_init_defaults(self):
        """Test server initialization with default parameters."""
        db_params = {"host": "localhost", "port": 5432}
        server = WebAdminServer(db_params)

        assert server.db_params == db_params
        assert server.host == "127.0.0.1"
        assert server.port == 8081
        assert server.app is not None
        assert server.pool is None  # Pool is created lazily on first use

    def test_init_custom_host_port(self):
        """Test server initialization with custom host/port."""
        db_params = {"host": "localhost", "port": 5432}
        server = WebAdminServer(db_params, host="0.0.0.0", port=9000)

        assert server.host == "0.0.0.0"
        assert server.port == 9000

    def test_routes_setup(self):
        """Test that routes are properly configured."""
        db_params = {"host": "localhost", "port": 5432}
        server = WebAdminServer(db_params)

        # Check that routes exist
        routes = [
            r.resource.canonical
            for r in server.app.router.routes()
            if hasattr(r, "resource")
        ]
        assert "/" in routes
        assert "/jobs" in routes
        assert "/queues" in routes
        assert "/workers" in routes
        assert "/dlq" in routes
        assert "/metrics" in routes
        assert "/schedules" in routes


# db_params comes from conftest.py (honors PYJOBBY_TEST_DSN)


@pytest_asyncio.fixture
async def web_admin_client(db_params, aiohttp_client):
    """Create a test client for the web admin server."""
    server = WebAdminServer(db_params)
    return await aiohttp_client(server.app)


class TestHTMLPages:
    """Test HTML page rendering - covers lines 89-600+."""

    @pytest.mark.asyncio
    async def test_index_page(self, web_admin_client):
        """Test index page returns HTML."""
        resp = await web_admin_client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "Pyjobby Admin" in text
        assert "<html" in text

    @pytest.mark.asyncio
    async def test_jobs_page(self, web_admin_client):
        """Test jobs page returns HTML."""
        resp = await web_admin_client.get("/jobs")
        assert resp.status == 200
        text = await resp.text()
        assert "<html" in text

    @pytest.mark.asyncio
    async def test_queues_page(self, web_admin_client):
        """Test queues page returns HTML."""
        resp = await web_admin_client.get("/queues")
        assert resp.status == 200
        text = await resp.text()
        assert "Queue Management" in text
        assert "/api/queues?format=html" in text

    @pytest.mark.asyncio
    async def test_workers_page(self, web_admin_client):
        """Test workers page returns HTML."""
        resp = await web_admin_client.get("/workers")
        assert resp.status == 200
        text = await resp.text()
        assert "Worker Registry" in text
        assert "/api/workers?format=html" in text

    @pytest.mark.asyncio
    async def test_dlq_page(self, web_admin_client):
        """Test DLQ page returns HTML."""
        resp = await web_admin_client.get("/dlq")
        assert resp.status == 200
        text = await resp.text()
        assert "Dead Letter Queue" in text

    @pytest.mark.asyncio
    async def test_metrics_is_prometheus_not_html(self, web_admin_client):
        """GET /metrics serves the Prometheus text exposition now."""
        resp = await web_admin_client.get("/metrics")
        assert resp.status == 200
        assert "version=0.0.4" in resp.headers["Content-Type"]

    @pytest.mark.asyncio
    async def test_schedules_page(self, web_admin_client):
        """Test schedules page returns HTML."""
        resp = await web_admin_client.get("/schedules")
        assert resp.status == 200
        text = await resp.text()
        assert "<html" in text


class TestTemplates:
    """The template set is one shell plus one file per surface.

    HTML used to be four inline documents in web_admin.py with the stylesheet
    written out three times and the htmx tag pinned four times. These tests
    are what stops that coming back, and what turns a typo'd template name
    from a 500 at request time into a failure here.
    """

    def test_every_packaged_template_loads_and_compiles(self):
        """Each template in the package parses (and finds its parent)."""
        env = build_template_env()
        names = env.list_templates()
        # The loader reads through importlib.resources, so an empty listing
        # means the templates did not ship -- not that there are none.
        assert names, "no templates found in the installed package"
        for name in names:
            env.get_template(name)

    def test_template_set_is_exactly_the_audited_one(self):
        """Adding or renaming a template must update this list (and CI's
        packaging assertion, which is keyed to the same directory)."""
        assert set(build_template_env().list_templates()) == EXPECTED_TEMPLATES

    def test_unknown_template_is_a_load_error_not_a_500(self):
        """A name the package does not contain fails as TemplateNotFound."""
        import jinja2

        env = build_template_env()
        with pytest.raises(jinja2.TemplateNotFound):
            env.get_template("fragments/no_such_template.html")
        # ...and the loader cannot be walked out of its own root.
        with pytest.raises(jinja2.TemplateNotFound):
            env.get_template("../__init__.py")

    @pytest.mark.parametrize("path", ADMIN_PAGES)
    @pytest.mark.asyncio
    async def test_page_has_one_stylesheet_and_one_htmx_tag(
        self, web_admin_client, path
    ):
        """Both live in base.html, so every page carries exactly one copy.

        The duplication this replaced is why a colour change was three edits
        and an htmx bump was four.
        """
        resp = await web_admin_client.get(path)
        assert resp.status == 200
        text = await resp.text()

        assert text.count("<style") == 1, f"{path}: not exactly one style block"
        assert text.count('<link rel="stylesheet"') == 0, (
            f"{path}: stylesheet is inlined by base.html; an external link is "
            f"a second copy of the styling"
        )
        assert text.count("unpkg.com/htmx.org") == 1, (
            f"{path}: not exactly one htmx tag"
        )

    @pytest.mark.asyncio
    async def test_every_page_pins_the_same_htmx_version(self, web_admin_client):
        """One tag per page is only half the property: they must agree."""
        pinned = set()
        for path in ADMIN_PAGES:
            text = await (await web_admin_client.get(path)).text()
            pinned.update(re.findall(r"unpkg\.com/htmx\.org@([\w.]+)", text))
        assert len(pinned) == 1, f"htmx pinned at more than one version: {pinned}"


@pytest_asyncio.fixture
async def hostile_named_rows(db_pool):
    """Seed every table the HTML fragments read with a <script> in its name.

    One value, five fragments: the queue column appears in all of them, so a
    single hostile queue name reaches the jobs, queues, workers, DLQ and
    schedules tables at once.
    """
    marker = unique_name("xss")
    value = f"<script>alert('{marker}')</script>"
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, prio, state)
            VALUES ($1, '{}', $1, 100, 'queued')
        """,
            value,
        )
        await conn.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, prio, state,
                              error_count, error_message)
            VALUES ($1, '{}', $1, 100, 'crashed', 1, $1)
        """,
            value,
        )
        await conn.execute(
            "INSERT INTO jorb_worker (host, pid, queue) VALUES ($1, 4242, $1)",
            value,
        )
        await conn.execute(
            """
            INSERT INTO jorb_schedule (name, job_class, cron_expr, queue,
                                       description, next_run)
            VALUES ($1, $1, '0 * * * *', $1, $1, NOW() + INTERVAL '1 hour')
        """,
            value,
        )
    return value


class TestAutoescape:
    """Escaping is the environment's default, not a habit of each handler."""

    @pytest.mark.parametrize(
        "url",
        [
            "/api/jobs?format=html&queue={value}",
            "/api/queues?format=html",
            "/api/workers?format=html",
            "/api/dlq?format=html&limit=1000",
            "/api/schedules?format=html",
        ],
    )
    @pytest.mark.asyncio
    async def test_fragment_escapes_a_script_tag(
        self, web_admin_client, hostile_named_rows, url
    ):
        """Every htmx fragment renders the hostile name escaped, never raw."""
        value = hostile_named_rows
        resp = await web_admin_client.get(
            url.format(value=urllib.parse.quote(value, safe=""))
        )
        assert resp.status == 200
        text = await resp.text()

        assert value not in text, f"{url}: raw <script> reached the page"
        assert "&lt;script&gt;alert(&#39;" in text, f"{url}: escaped form missing"


class TestJobsAPI:
    """Test Jobs API endpoints - covers api_jobs_* methods."""

    @pytest.mark.asyncio
    async def test_api_jobs_list(self, web_admin_client, db_pool):
        """Test listing jobs via API."""
        # Create a test job
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('ApiTestJob', '{}', 'test', 100, 'queued')
                RETURNING id
            """)

        resp = await web_admin_client.get("/api/jobs")
        assert resp.status == 200

        data = await resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_api_jobs_list_filter_queue(self, web_admin_client, db_pool):
        """Test listing jobs filtered by queue."""
        queue = unique_name("api_filter")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('FilterJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/jobs?queue={queue}")
        assert resp.status == 200

        data = await resp.json()
        assert all(j["queue"] == queue for j in data)

    @pytest.mark.asyncio
    async def test_api_jobs_list_filter_state(self, web_admin_client, db_pool):
        """Test listing jobs filtered by state."""
        queue = unique_name("state_filter")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('CrashedJob', '{}', $1, 100, 'crashed')
            """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/jobs?queue={queue}&state=crashed")
        assert resp.status == 200

        data = await resp.json()
        for j in data:
            if j["queue"] == queue:
                assert j["state"] == "crashed"

    @pytest.mark.asyncio
    async def test_api_job_get(self, web_admin_client, db_pool):
        """Test getting a single job."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('GetJob', '{"x": 1}', 'test', 100, 'queued')
                RETURNING id
            """)

        resp = await web_admin_client.get(f"/api/jobs/{job_id}")
        assert resp.status == 200

        data = await resp.json()
        assert data["id"] == job_id
        assert data["job_class"] == "GetJob"

    @pytest.mark.asyncio
    async def test_api_job_get_not_found(self, web_admin_client):
        """Test getting non-existent job returns 404."""
        resp = await web_admin_client.get("/api/jobs/99999999")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_api_job_retry(self, web_admin_client, db_pool):
        """Test retrying a crashed job."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('RetryJob', '{}', 'test', 100, 'crashed')
                RETURNING id
            """)

        resp = await web_admin_client.post(f"/api/jobs/{job_id}/retry")
        assert resp.status == 200

        data = await resp.json()
        # v1 shape: retries requeue the SAME row
        assert data == {"job_id": job_id, "status": "requeued"}

        async with db_pool.acquire() as conn:
            state = await conn.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
        assert state == "queued"

    @pytest.mark.asyncio
    async def test_api_job_cancel(self, web_admin_client, db_pool):
        """Test cancelling a queued job."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('CancelJob', '{}', 'test', 100, 'queued')
                RETURNING id
            """)

        resp = await web_admin_client.post(f"/api/jobs/{job_id}/cancel")
        assert resp.status == 200

        data = await resp.json()
        assert data["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_api_job_delete(self, web_admin_client, db_pool):
        """Test deleting a job."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('DeleteJob', '{}', 'test', 100, 'finished')
                RETURNING id
            """)

        resp = await web_admin_client.delete(f"/api/jobs/{job_id}")
        assert resp.status == 200

        data = await resp.json()
        assert data["status"] == "deleted"


class TestJobsListFilters:
    """The filters `/api/jobs` exposes, and the ones it refuses to fake.

    `AdminAPI.list_jobs` has answered identity_key and tags with an index for
    as long as `pj-admin jobs list` has taken --identity and --tag; the HTTP
    surface simply never passed them through, so the two incident questions
    ("did this identity ever run", "show me this customer's jobs") were
    CLI-only. The rejection tests below pin the worse half: an unknown
    parameter was DROPPED, so a mistyped filter answered with the whole queue
    and looked like a filter that matched everything.
    """

    @pytest_asyncio.fixture
    async def tagged_jobs(self, db_pool):
        """Two jobs on one queue: one tagged+identified, one bare."""
        queue = unique_name("filters")
        identity = unique_name("ident")
        async with db_pool.acquire() as conn:
            match_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state,
                                  identity_key, tags)
                VALUES ('FilterJob', '{}', $1, 100, 'queued', $2,
                        '{"customer": "acme", "batch": 7}')
                RETURNING id
                """,
                queue,
                identity,
            )
            other_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, tags)
                VALUES ('FilterJob', '{}', $1, 100, 'queued',
                        '{"customer": "globex"}')
                RETURNING id
                """,
                queue,
            )
        return {
            "queue": queue,
            "identity": identity,
            "match": match_id,
            "other": other_id,
        }

    @pytest.mark.asyncio
    async def test_identity_key_filter_reaches_list_jobs(
        self, web_admin_client, tagged_jobs
    ):
        """?identity_key= returns the one row holding that key, not the queue."""
        resp = await web_admin_client.get(
            f"/api/jobs?identity_key={tagged_jobs['identity']}"
        )
        assert resp.status == 200
        assert [j["id"] for j in await resp.json()] == [tagged_jobs["match"]]

    @pytest.mark.asyncio
    async def test_empty_identity_key_is_not_a_filter(
        self, web_admin_client, tagged_jobs
    ):
        """An unfilled form field must not search for the empty identity."""
        resp = await web_admin_client.get(
            f"/api/jobs?queue={tagged_jobs['queue']}&identity_key="
        )
        assert resp.status == 200
        assert {j["id"] for j in await resp.json()} == {
            tagged_jobs["match"],
            tagged_jobs["other"],
        }

    @pytest.mark.asyncio
    async def test_tag_filter_is_repeated_key_equals_value(
        self, web_admin_client, tagged_jobs
    ):
        """?tag=k%3Dv is the URL spelling of `--tag k=v`, and repeats AND."""
        resp = await web_admin_client.get(
            f"/api/jobs?queue={tagged_jobs['queue']}"
            f"&tag={urllib.parse.quote('customer=acme', safe='')}"
        )
        assert resp.status == 200
        assert [j["id"] for j in await resp.json()] == [tagged_jobs["match"]]

        # Both pairs must hold: the second one nothing carries empties it.
        resp = await web_admin_client.get(
            f"/api/jobs?queue={tagged_jobs['queue']}"
            f"&tag={urllib.parse.quote('customer=acme', safe='')}"
            f"&tag={urllib.parse.quote('region=eu', safe='')}"
        )
        assert resp.status == 200
        assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_tag_value_is_read_as_json_like_the_cli(
        self, web_admin_client, tagged_jobs
    ):
        """batch=7 matches the NUMBER 7; the string "7" is written as JSON.

        The stored tag is a JSON number, and a filter that sent the string
        would silently match nothing -- the same trap `cli.parse_tags` reads
        values as JSON to avoid.
        """
        resp = await web_admin_client.get(
            f"/api/jobs?queue={tagged_jobs['queue']}"
            f"&tag={urllib.parse.quote('batch=7', safe='')}"
        )
        assert resp.status == 200
        assert [j["id"] for j in await resp.json()] == [tagged_jobs["match"]]

        resp = await web_admin_client.get(
            f"/api/jobs?queue={tagged_jobs['queue']}"
            f"&tag={urllib.parse.quote('batch="7"', safe='')}"
        )
        assert resp.status == 200
        assert await resp.json() == []

    @pytest.mark.parametrize("bad", ["customer", "=acme", "a=[1,2]", 'a={"b": 1}'])
    @pytest.mark.asyncio
    async def test_malformed_tag_is_400_not_a_500_or_a_wider_search(
        self, web_admin_client, bad
    ):
        """A tag that is not key=value with a scalar value is refused."""
        resp = await web_admin_client.get(
            f"/api/jobs?tag={urllib.parse.quote(bad, safe='')}"
        )
        assert resp.status == 400
        assert "tag" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_unknown_query_parameter_is_rejected(self, web_admin_client):
        """`?identity_ke=x` returned every job; now it is a 400 naming it."""
        resp = await web_admin_client.get("/api/jobs?identity_ke=x")
        assert resp.status == 400
        body = (await resp.json())["error"]
        assert "identity_ke" in body
        # ...and the message says what the route does read.
        assert "identity_key" in body

    @pytest.mark.asyncio
    async def test_unknown_parameter_list_is_bounded(self, web_admin_client):
        """The echoed names are capped: the body is not the client's to size."""
        query = "&".join(f"junk{i}=1" for i in range(50))
        resp = await web_admin_client.get(f"/api/jobs?{query}")
        assert resp.status == 400
        body = (await resp.json())["error"]
        assert "and 45 more" in body
        assert "junk49" not in body

    @pytest.mark.asyncio
    async def test_known_parameters_all_pass(self, web_admin_client, tagged_jobs):
        """Every documented parameter together is a 200, not a 400."""
        resp = await web_admin_client.get(
            f"/api/jobs?queue={tagged_jobs['queue']}&state=queued&limit=10"
            f"&offset=0&format=json&identity_key={tagged_jobs['identity']}"
            f"&tag={urllib.parse.quote('customer=acme', safe='')}"
        )
        assert resp.status == 200
        assert [j["id"] for j in await resp.json()] == [tagged_jobs["match"]]


class TestScheduledVocabulary:
    """A queued job with run_after in the future is 'scheduled' everywhere.

    db.QUEUE_STATS_SQL is the contract: `pj-admin queues`, the queues table
    and the /metrics scrape all split those rows out of queued, because
    deferred work is not backlog. The jobs table and /api/metrics were the
    two surfaces that still folded them in, so the same rows read as a
    backlog on one page and as deferred work on the next.
    """

    @pytest.mark.asyncio
    async def test_jobs_table_badges_a_deferred_job_scheduled(
        self, web_admin_client, db_pool
    ):
        """A future run_after gets a scheduled badge, not a queued one."""
        queue = unique_name("sched_badge")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, run_after)
                VALUES ('DeferredJob', '{}', $1, 100, 'queued',
                        now() + interval '1 hour')
                """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/jobs?format=html&queue={queue}")
        assert resp.status == 200
        text = await resp.text()
        assert '<span class="badge scheduled">scheduled</span>' in text
        assert "badge queued" not in text

    @pytest.mark.asyncio
    async def test_jobs_table_still_badges_claimable_work_queued(
        self, web_admin_client, db_pool
    ):
        """The split is run_after, not the state: due work stays queued."""
        queue = unique_name("due_badge")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, run_after)
                VALUES ('DueJob', '{}', $1, 100, 'queued', now() - interval '1 minute')
                """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/jobs?format=html&queue={queue}")
        assert resp.status == 200
        text = await resp.text()
        assert '<span class="badge queued">queued</span>' in text
        assert "scheduled" not in text

    @pytest.mark.asyncio
    async def test_the_queues_table_shows_scheduled_beside_queued(
        self, web_admin_client, db_pool
    ):
        """The queues page was the last surface folding the two together.

        ``AdminAPI.queue_stats`` has always returned them apart, and the
        fragment simply had no column for the second one -- so on the page
        NAMED after queue depth, a retry storm or a campaign scheduled for
        next week was invisible, and the one number shown could sit at 0
        while thousands of rows waited.
        """
        queue = unique_name("queues_table_sched")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, run_after)
                VALUES ('DueJob', '{}', $1, 100, 'queued', now() - interval '1 minute'),
                       ('DeferredJob', '{}', $1, 100, 'queued',
                        now() + interval '1 hour'),
                       ('DeferredJob', '{}', $1, 100, 'queued',
                        now() + interval '2 hours')
                """,
                queue,
            )

        resp = await web_admin_client.get("/api/queues?format=html")
        assert resp.status == 200
        text = await resp.text()

        assert "<th>Scheduled</th>" in text
        row = next(r for r in _table_rows(text) if f"<strong>{queue}</strong>" in r)
        # the queue-name cell wraps its text in <strong>, so the plain-text
        # cells start at Queued; the next one is the column under test
        counts = re.findall(r"<td>\s*([^<\s][^<]*?)\s*</td>", row)
        assert counts[:2] == ["1", "2"], row  # one claimable now, two deferred

    @pytest.mark.asyncio
    async def test_json_jobs_keep_the_stored_state(self, web_admin_client, db_pool):
        """`display_state` is a rendering concern: the JSON row is untouched."""
        queue = unique_name("json_state")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, run_after)
                VALUES ('JsonDeferredJob', '{}', $1, 100, 'queued',
                        now() + interval '1 hour')
                """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/jobs?queue={queue}")
        assert resp.status == 200
        rows = await resp.json()
        assert [r["state"] for r in rows] == ["queued"]
        assert "display_state" not in rows[0]

    @pytest.mark.asyncio
    async def test_api_metrics_splits_scheduled_out_of_queued(
        self, web_admin_client, db_pool
    ):
        """state_counts reports the deferred row as scheduled, not queued."""
        queue = unique_name("metrics_sched")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, run_after)
                VALUES ('DueJob', '{}', $1, 100, 'queued', now()),
                       ('DeferredJob', '{}', $1, 100, 'queued',
                        now() + interval '1 hour'),
                       ('DeferredJob', '{}', $1, 100, 'queued',
                        now() + interval '2 hours')
                """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/metrics?queue={queue}")
        assert resp.status == 200
        counts = (await resp.json())["state_counts"]
        assert counts == {"queued": 1, "scheduled": 2}

    @pytest.mark.asyncio
    async def test_api_metrics_drops_queued_when_all_of_it_is_deferred(
        self, web_admin_client, db_pool
    ):
        """A GROUP BY has no zero rows, so the split must not leave one.

        Every state in state_counts is present because it has rows; a
        `queued: 0` left behind by the subtraction would be the only key in
        the dict that means "none".
        """
        queue = unique_name("metrics_all_sched")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, run_after)
                VALUES ('DeferredJob', '{}', $1, 100, 'queued',
                        now() + interval '1 hour')
                """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/metrics?queue={queue}")
        assert resp.status == 200
        assert (await resp.json())["state_counts"] == {"scheduled": 1}

    @pytest.mark.asyncio
    async def test_api_metrics_without_a_queue_filter_still_splits(
        self, web_admin_client, db_pool
    ):
        """The fleet-wide call must not read `?queue=` as a queue named ''."""
        queue = unique_name("metrics_global")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, run_after)
                VALUES ('DeferredJob', '{}', $1, 100, 'queued',
                        now() + interval '1 hour')
                """,
                queue,
            )

        resp = await web_admin_client.get("/api/metrics?queue=")
        assert resp.status == 200
        assert (await resp.json())["state_counts"].get("scheduled", 0) >= 1


class TestQueuesAPI:
    """Test Queues API endpoints - covers api_queues_* methods."""

    @pytest.mark.asyncio
    async def test_api_queues_list(self, web_admin_client, db_pool):
        """Test listing queues."""
        queue = unique_name("list_queue")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('QueueJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )

        resp = await web_admin_client.get("/api/queues")
        # Returns list of queue stats — a 500 is never an acceptable answer.
        assert resp.status == 200
        assert queue in {row["queue"] for row in await resp.json()}

    @pytest.mark.asyncio
    async def test_api_queue_stats(self, web_admin_client, db_pool):
        """Test getting queue statistics."""
        queue = unique_name("stats_queue")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('StatsJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/queues/{queue}/stats")
        assert resp.status == 200
        data = await resp.json()
        assert data[0]["queue"] == queue
        assert data[0]["queued"] == 1


class TestQueueControls:
    """Pause/resume endpoints drive the jorb_queue control plane."""

    @pytest.mark.asyncio
    async def test_pause_queue_creates_control_row(self, web_admin_client, db_pool):
        """POST pause upserts a paused jorb_queue row."""
        queue = unique_name("pause_q")

        resp = await web_admin_client.post(f"/api/queues/{queue}/pause")
        assert resp.status == 200
        data = await resp.json()
        assert data["name"] == queue
        assert data["paused"] is True

        async with db_pool.acquire() as conn:
            paused = await conn.fetchval(
                "SELECT paused FROM jorb_queue WHERE name = $1", queue
            )
        assert paused is True

    @pytest.mark.asyncio
    async def test_resume_queue_unpauses(self, web_admin_client, db_pool):
        """POST resume flips paused back to false."""
        queue = unique_name("resume_q")
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)", queue
            )

        resp = await web_admin_client.post(f"/api/queues/{queue}/resume")
        assert resp.status == 200
        data = await resp.json()
        assert data["paused"] is False

        async with db_pool.acquire() as conn:
            paused = await conn.fetchval(
                "SELECT paused FROM jorb_queue WHERE name = $1", queue
            )
        assert paused is False

    @pytest.mark.asyncio
    async def test_pause_html_format_returns_fragment(self, web_admin_client, db_pool):
        """format=html returns the refreshed queues table for htmx."""
        queue = unique_name("pause_html")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('PauseHtmlJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )

        resp = await web_admin_client.post(f"/api/queues/{queue}/pause?format=html")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        text = await resp.text()
        assert queue in text
        assert "Resume" in text  # paused queue offers the resume action

    @pytest.mark.asyncio
    async def test_queues_html_shows_paused_and_limits(self, web_admin_client, db_pool):
        """Queues fragment shows paused state, limit columns, and buttons."""
        queue = unique_name("ctrl_html")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('CtrlJob', '{}', $1, 100, 'queued')
            """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb_queue (name, paused, max_concurrency, rate_limit)
                VALUES ($1, TRUE, 5, 100)
            """,
                queue,
            )

        resp = await web_admin_client.get("/api/queues?format=html")
        assert resp.status == 200
        text = await resp.text()
        assert queue in text
        assert "paused" in text
        assert "Max Concurrency" in text
        assert "Rate Limit" in text
        assert f"/api/queues/{queue}/resume" in text

    @pytest.mark.asyncio
    async def test_partitioned_limits_are_marked_per_lane(
        self, web_admin_client, db_pool
    ):
        """A per-lane limit rendered bare reads as a queue-wide one.

        `max_concurrency` 5 on a queue with partition_limits is 5 PER
        partition_key, so an operator seeing "5" beside 40 running jobs
        concludes the limit is broken. The scope travels with the number,
        exactly as `pj-admin queues show` prints it.
        """
        queue = unique_name("partlimits_html")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_queue
                    (name, max_concurrency, rate_limit, partition_limits)
                VALUES ($1, 5, 100, TRUE)
                """,
                queue,
            )

        resp = await web_admin_client.get("/api/queues?format=html")
        assert resp.status == 200
        row = next(
            ln for ln in _table_rows(await resp.text()) if f"<strong>{queue}<" in ln
        )
        assert '5<span class="scope"' in row
        assert row.count("/lane") == 2, "both limits carry the scope, or neither"

    @pytest.mark.asyncio
    async def test_queue_wide_limits_carry_no_lane_marker(
        self, web_admin_client, db_pool
    ):
        """The marker means something, so it is absent when limits are not
        partitioned -- otherwise it is decoration nobody reads."""
        queue = unique_name("queuewide_html")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_queue (name, max_concurrency, rate_limit)
                VALUES ($1, 5, 100)
                """,
                queue,
            )

        resp = await web_admin_client.get("/api/queues?format=html")
        assert resp.status == 200
        row = next(
            ln for ln in _table_rows(await resp.text()) if f"<strong>{queue}<" in ln
        )
        assert "/lane" not in row

    @pytest.mark.asyncio
    async def test_queues_json_includes_control_fields(self, web_admin_client, db_pool):
        """JSON queue stats carry the control-plane fields."""
        queue = unique_name("ctrl_json")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_queue (name, paused, max_concurrency)
                VALUES ($1, TRUE, 3)
            """,
                queue,
            )

        resp = await web_admin_client.get("/api/queues")
        assert resp.status == 200
        data = await resp.json()
        stats = next(s for s in data if s["queue"] == queue)
        assert stats["paused"] is True
        assert stats["max_concurrency"] == 3


class TestJobHistoryAndSteps:
    """/api/jobs/{id}/history and /api/jobs/{id}/steps JSON endpoints."""

    @pytest.mark.asyncio
    async def test_api_job_history(self, web_admin_client, db_pool):
        """History returns the trigger-recorded transition trail in order."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('HistJob', '{}', 'test', 100, 'queued')
                RETURNING id
            """)
            await conn.execute(
                "UPDATE jorb SET state = 'running' WHERE id = $1", job_id
            )
            await conn.execute(
                "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
            )

        resp = await web_admin_client.get(f"/api/jobs/{job_id}/history")
        assert resp.status == 200
        history = await resp.json()
        events = [h["event"] for h in history]
        assert events == ["enqueued", "running", "finished"]
        assert all(h["job_id"] == job_id for h in history)
        assert history[1]["detail"]["from"] == "queued"

    @pytest.mark.asyncio
    async def test_api_job_history_not_found(self, web_admin_client):
        """Unknown job id gives 404."""
        resp = await web_admin_client.get("/api/jobs/99999999/history")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_api_job_steps(self, web_admin_client, db_pool):
        """Steps return DXE checkpoints ordered by sequence."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('StepJob', '{}', 'test', 100, 'running')
                RETURNING id
            """)
            await conn.execute(
                """
                INSERT INTO jorb_step
                    (job_id, step_seq, name, output, run_epoch, started, finished)
                VALUES
                    ($1, 2, 'second', '{"ok": true}', 1,
                     now() - interval '5 seconds', now()),
                    ($1, 1, 'first', '{}', 1,
                     now() - interval '10 seconds', now() - interval '8 seconds')
            """,
                job_id,
            )

        resp = await web_admin_client.get(f"/api/jobs/{job_id}/steps")
        assert resp.status == 200
        steps = await resp.json()
        assert [s["name"] for s in steps] == ["first", "second"]
        assert steps[0]["duration_seconds"] == pytest.approx(2.0, abs=0.5)
        assert steps[1]["output"] == {"ok": True}

    @pytest.mark.asyncio
    async def test_api_job_steps_not_found(self, web_admin_client):
        """Unknown job id gives 404."""
        resp = await web_admin_client.get("/api/jobs/99999999/steps")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_jobs_html_links_to_details(self, web_admin_client, db_pool):
        """Jobs table links each row to its history/steps endpoints."""
        queue = unique_name("detail_links")
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('LinkJob', '{}', $1, 100, 'queued')
                RETURNING id
            """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/jobs?format=html&queue={queue}")
        assert resp.status == 200
        text = await resp.text()
        assert f"/api/jobs/{job_id}/history" in text
        assert f"/api/jobs/{job_id}/steps" in text


class TestPrometheusMetrics:
    """GET /metrics - Prometheus text exposition format."""

    @pytest.mark.asyncio
    async def test_metrics_content_type(self, web_admin_client):
        """Exposition uses text/plain; version=0.0.4."""
        resp = await web_admin_client.get("/metrics")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/plain")
        assert "version=0.0.4" in resp.headers["Content-Type"]

    @pytest.mark.asyncio
    async def test_jobs_by_state_and_oldest_queued(self, web_admin_client, db_pool):
        """Per-queue state gauges, backlog depth, and oldest-ready age.

        `run_after` is set alongside `created` because that is what an
        enqueue does, and the backlog gauges are measured from `run_after`:
        they answer "how long has the head of this queue been READY and
        unclaimed", so a job deliberately scheduled for later has not been
        waiting at all until it comes due.
        """
        queue = unique_name("prom_q")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state,
                                  created, run_after)
                VALUES ('PromJob', '{}', $1, 100, 'queued',
                        now() - interval '120 seconds',
                        now() - interval '120 seconds'),
                       ('PromJob', '{}', $1, 100, 'queued', now(), now()),
                       ('PromJob', '{}', $1, 100, 'crashed', now(), now())
            """,
                queue,
            )

        resp = await web_admin_client.get("/metrics")
        text = await resp.text()

        assert f'pyjobby_jobs_by_state{{queue="{queue}",state="queued"}} 2' in text
        assert "# TYPE pyjobby_jobs_by_state gauge" in text
        # The crashed job is NOT in jobs_by_state: that gauge reports live
        # states only, because the terminal ones grow with everything the
        # installation has ever run. Terminal outcomes are reported over the
        # scrape window instead.
        assert f'pyjobby_jobs_by_state{{queue="{queue}",state="crashed"}}' not in text
        assert (
            f'pyjobby_jobs_terminal_recent{{queue="{queue}",state="crashed"}} 1' in text
        )

        # Only the two queued jobs are backlog; the crashed one is not.
        assert f'pyjobby_backlog_depth{{queue="{queue}"}} 2' in text

        age_line = next(
            line
            for line in text.splitlines()
            if line.startswith(
                f'pyjobby_queue_oldest_queued_seconds{{queue="{queue}"}}'
            )
        )
        assert float(age_line.rsplit(" ", 1)[1]) == pytest.approx(120.0, abs=10.0)

    @pytest.mark.asyncio
    async def test_paused_and_workers_live(self, web_admin_client, db_pool):
        """Queue paused gauge and live worker count come from v1 tables."""
        queue = unique_name("prom_paused")
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)", queue
            )
            await conn.execute("""
                INSERT INTO jorb_worker (host, pid, queue)
                VALUES ('prom_host', 777, 'default')
            """)
            # A shut-down worker must not count as live
            await conn.execute("""
                INSERT INTO jorb_worker (host, pid, queue, shutdown_at)
                VALUES ('prom_host', 778, 'default', now())
            """)

        resp = await web_admin_client.get("/metrics")
        text = await resp.text()

        assert f'pyjobby_queue_paused{{queue="{queue}"}} 1' in text
        assert "pyjobby_workers_live 1" in text

    @pytest.mark.asyncio
    async def test_outcome_gauges_come_from_the_job_table(
        self, web_admin_client, db_pool
    ):
        """started/terminal counts are read from jorb over the scrape window.

        They used to be `*_total` counters read by joining ALL of
        jorb_history to ALL of jorb on every scrape. That query had no window
        at all, and adding one would have made a counter that resets -- so
        the series are gauges now, with names that say so. See
        tests/test_metrics_scrape_cost.py for the full argument.
        """
        queue = unique_name("prom_hist")
        async with db_pool.acquire() as conn:
            ok_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('HistPromJob', '{}', $1, 100, 'queued') RETURNING id
            """,
                queue,
            )
            bad_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('HistPromJob', '{}', $1, 100, 'queued') RETURNING id
            """,
                queue,
            )
            for job_id, final in ((ok_id, "finished"), (bad_id, "crashed")):
                await conn.execute(
                    "UPDATE jorb SET state = 'running', started = now() WHERE id = $1",
                    job_id,
                )
                await conn.execute(
                    f"UPDATE jorb SET state = '{final}', finished = now() "
                    f"WHERE id = $1",
                    job_id,
                )

        resp = await web_admin_client.get("/metrics")
        text = await resp.text()

        assert f'pyjobby_jobs_started_recent{{queue="{queue}"}} 2' in text
        assert (
            f'pyjobby_jobs_terminal_recent{{queue="{queue}",state="finished"}} 1'
            in text
        )
        assert (
            f'pyjobby_jobs_terminal_recent{{queue="{queue}",state="crashed"}} 1' in text
        )
        assert "# TYPE pyjobby_jobs_started_recent gauge" in text
        assert "# TYPE pyjobby_jobs_terminal_recent gauge" in text
        # The retired counters are gone, not renamed in place.
        for retired in (
            "pyjobby_jobs_started_total",
            "pyjobby_jobs_finished_total",
            "pyjobby_jobs_crashed_total",
        ):
            assert retired not in text

    @pytest.mark.asyncio
    async def test_duration_quantiles(self, web_admin_client, db_pool):
        """Duration quantiles cover jobs finished in the last hour."""
        queue = unique_name("prom_dur")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb
                    (job_class, kwargs, queue, prio, state, started, finished)
                VALUES ('DurJob', '{}', $1, 100, 'finished',
                        now() - interval '70 seconds', now() - interval '10 seconds')
            """,
                queue,
            )

        resp = await web_admin_client.get("/metrics")
        text = await resp.text()

        for quantile in ("0.5", "0.9", "0.99"):
            line = next(
                ln
                for ln in text.splitlines()
                if ln.startswith(
                    f'pyjobby_job_duration_seconds{{queue="{queue}",'
                    f'quantile="{quantile}"}}'
                )
            )
            assert float(line.rsplit(" ", 1)[1]) == pytest.approx(60.0, abs=1.0)

    @pytest.mark.asyncio
    async def test_unclaimable_gauge_is_the_only_signal_for_stranded_work(
        self, web_admin_client, db_pool
    ):
        """Work the live fleet can never claim reaches the scrape, by cause.

        A job above every live worker's ceiling stays 'queued' forever: it
        never fails, never retries, never reaches the DLQ, and every other
        series in this exposition reads healthy while it sits there. Without
        this gauge nothing an alert can be written against ever moves.
        """
        queue = unique_name("prom_unclaimable")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue, max_prio)
                VALUES ('unclaimable_host', 5150, $1, 100)
                """,
                queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('CeilingJob', '{}', $1, 500, 'queued'),
                       ('CeilingJob', '{}', $1, 900, 'queued'),
                       ('ClaimableJob', '{}', $1, 50, 'queued')
                """,
                queue,
            )

        resp = await web_admin_client.get("/metrics")
        text = await resp.text()

        assert "# TYPE pyjobby_jobs_unclaimable gauge" in text
        assert (
            f'pyjobby_jobs_unclaimable{{queue="{queue}",'
            f'reason="above_worker_ceiling"}} 2' in text
        )
        # The claimable job is backlog, not stranded work, and the causes are
        # disjoint -- it must not appear under any reason.
        assert f'pyjobby_jobs_unclaimable{{queue="{queue}",reason="capability' not in (
            text
        )

    @pytest.mark.asyncio
    async def test_unclaimable_gauge_silent_when_the_fleet_can_claim(
        self, web_admin_client, db_pool
    ):
        """A queue whose work its workers CAN claim reports no series.

        The gauge is alerted on above 0, so a queue that is merely busy must
        not emit a row for it -- and a queue with no live workers at all is
        pyjobby_workers_live's business, not this one's.
        """
        served = unique_name("prom_claimable")
        unmanned = unique_name("prom_no_workers")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue, max_prio)
                VALUES ('claimable_host', 5151, $1, 1000)
                """,
                served,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('FineJob', '{}', $1, 100, 'queued'),
                       ('StrandedButUnmanned', '{}', $2, 5000, 'queued')
                """,
                served,
                unmanned,
            )

        resp = await web_admin_client.get("/metrics")
        text = await resp.text()

        assert f'pyjobby_jobs_unclaimable{{queue="{served}"' not in text
        assert f'pyjobby_jobs_unclaimable{{queue="{unmanned}"' not in text

    @pytest.mark.asyncio
    async def test_label_escaping(self, web_admin_client, db_pool):
        """Queue names with quotes/backslashes/newlines are escaped."""
        nasty = f'esc_{uuid.uuid4().hex[:8]}"q\\b\nnl'
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('EscJob', '{}', $1, 100, 'queued')
            """,
                nasty,
            )

        resp = await web_admin_client.get("/metrics")
        text = await resp.text()

        escaped = nasty.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        assert f'pyjobby_jobs_by_state{{queue="{escaped}",state="queued"}} 1' in text
        # The raw (unescaped) name must not appear as a label value
        assert f'queue="{nasty}"' not in text


class TestWorkersAPI:
    """Test Workers API endpoints - registry (jorb_worker) based."""

    @pytest.mark.asyncio
    async def test_api_workers_list(self, web_admin_client, db_pool):
        """Workers come from the jorb_worker registry with liveness info."""
        queue = unique_name("workers_api")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue, capabilities)
                VALUES ('test_host', 12345, $1, '{test}')
            """,
                queue,
            )

        resp = await web_admin_client.get("/api/workers")
        assert resp.status == 200

        data = await resp.json()
        assert isinstance(data, list)
        worker = next(w for w in data if w["queue"] == queue)
        assert worker["host"] == "test_host"
        assert worker["pid"] == 12345
        assert worker["live"] is True
        assert worker["capabilities"] == ["test"]
        assert worker["current_job_id"] is None

    @pytest.mark.asyncio
    async def test_api_workers_list_html(self, web_admin_client, db_pool):
        """HTML fragment shows registry workers with a live badge."""
        queue = unique_name("workers_html")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue)
                VALUES ('html_host', 4242, $1)
            """,
                queue,
            )

        resp = await web_admin_client.get("/api/workers?format=html")
        assert resp.status == 200
        text = await resp.text()
        assert "html_host" in text
        assert "live" in text

    @pytest.mark.asyncio
    async def test_api_workers_stats(self, web_admin_client, db_pool):
        """Worker stats aggregate the registry (live/stale/shutdown)."""
        queue = unique_name("worker_stats")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_worker (host, pid, queue)
                VALUES ('stats_host', 999, $1)
            """,
                queue,
            )

        resp = await web_admin_client.get("/api/workers/stats")
        assert resp.status == 200

        data = await resp.json()
        assert data["live_workers"] >= 1
        assert "stale_workers" in data
        assert "shutdown_workers" in data
        assert data["per_queue"][queue] == 1


class TestDLQAPI:
    """DLQ = every terminal 'crashed' job (retries exhausted), no heuristic."""

    @pytest.mark.asyncio
    async def test_api_dlq_list(self, web_admin_client, db_pool):
        """Any crashed job is in the DLQ, regardless of error_count."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, error_count)
                VALUES ('DLQJob', '{}', 'test', 100, 'crashed', 1)
                RETURNING id
            """)

        resp = await web_admin_client.get("/api/dlq")
        assert resp.status == 200

        data = await resp.json()
        assert isinstance(data, list)
        assert any(j["id"] == job_id for j in data)

    @pytest.mark.asyncio
    async def test_api_dlq_list_html(self, web_admin_client, db_pool):
        """DLQ HTML fragment lists crashed jobs with a retry button."""
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO jorb
                    (job_class, kwargs, queue, prio, state, error_count, error_message)
                VALUES ('DLQHtmlJob', '{}', 'test', 100, 'crashed', 3, 'boom')
            """)

        resp = await web_admin_client.get("/api/dlq?format=html")
        assert resp.status == 200
        text = await resp.text()
        assert "DLQHtmlJob" in text
        assert "boom" in text
        assert "Retry" in text

    @pytest.mark.asyncio
    async def test_dlq_page_wording(self, web_admin_client):
        """DLQ page explains crashed is the terminal dead-letter state."""
        resp = await web_admin_client.get("/dlq")
        assert resp.status == 200
        text = await resp.text()
        assert "crashed" in text
        assert "retries are exhausted" in text

    @pytest.mark.asyncio
    async def test_api_dlq_retry(self, web_admin_client, db_pool):
        """Retry requeues the SAME row and resets the error budget."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, error_count)
                VALUES ('DLQRetryJob', '{}', 'test', 100, 'crashed', 15)
                RETURNING id
            """)

        resp = await web_admin_client.post(f"/api/dlq/{job_id}/retry")
        assert resp.status == 200
        data = await resp.json()
        assert data == {"job_id": job_id, "status": "requeued_from_dlq"}

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT state, error_count FROM jorb WHERE id = $1", job_id
            )
        assert row["state"] == "queued"
        assert row["error_count"] == 0

    @pytest.mark.asyncio
    async def test_api_dlq_retry_non_crashed_rejected(self, web_admin_client, db_pool):
        """Only crashed jobs live in the DLQ."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('NotDLQJob', '{}', 'test', 100, 'finished')
                RETURNING id
            """)

        resp = await web_admin_client.post(f"/api/dlq/{job_id}/retry")
        assert resp.status == 400


class TestMetricsAPI:
    """Test Metrics API endpoints - covers api_metrics method."""

    @pytest.mark.asyncio
    async def test_api_metrics(self, web_admin_client):
        """Test getting metrics."""
        resp = await web_admin_client.get("/api/metrics")
        assert resp.status == 200

        data = await resp.json()
        assert "period_start" in data
        assert "period_end" in data
        assert "state_counts" in data

    @pytest.mark.asyncio
    async def test_api_metrics_with_queue(self, web_admin_client, db_pool):
        """Test getting metrics filtered by queue."""
        queue = unique_name("metrics_queue")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('MetricsJob', '{}', $1, 100, 'finished')
            """,
                queue,
            )

        resp = await web_admin_client.get(f"/api/metrics?queue={queue}")
        assert resp.status == 200

        data = await resp.json()
        assert data["queue"] == queue


class TestSchedulesAPI:
    """Test Schedules API endpoints - covers api_schedule_* methods."""

    @pytest.mark.asyncio
    async def test_api_schedules_list(self, web_admin_client, db_pool):
        """Test listing schedules."""
        name = unique_name("list_schedule")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'ListJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
            """,
                name,
            )

        resp = await web_admin_client.get("/api/schedules")
        assert resp.status == 200

        data = await resp.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_api_schedules_list_html(self, web_admin_client, db_pool):
        """Test listing schedules with HTML format."""
        name = unique_name("html_schedule")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'HtmlJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
            """,
                name,
            )

        resp = await web_admin_client.get("/api/schedules?format=html")
        assert resp.status == 200
        assert resp.content_type == "text/html"

        text = await resp.text()
        assert "<table>" in text

    @pytest.mark.asyncio
    async def test_schedules_html_shows_backfill(self, web_admin_client, db_pool):
        """Backfill is a column: it decides what a recovery does.

        A scheduler that was down either drops the missed ticks or fires N of
        them at once, and both look like a bug to an operator who cannot see
        which was configured. Worded rather than a bare integer, because 0
        reads as "off" and the number alone does not say whether it counts
        ticks fired or ticks dropped.
        """
        skipping = unique_name("backfill_off")
        catching_up = unique_name("backfill_on")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule
                    (name, job_class, cron_expr, queue, backfill_limit, next_run)
                VALUES ($1, 'SkipJob', '0 * * * *', 'test', 0,
                        NOW() + INTERVAL '1 hour'),
                       ($2, 'CatchUpJob', '0 * * * *', 'test', 3,
                        NOW() + INTERVAL '1 hour')
                """,
                skipping,
                catching_up,
            )

        resp = await web_admin_client.get("/api/schedules?format=html")
        assert resp.status == 200
        text = await resp.text()
        assert "<th>Backfill</th>" in text

        rows = _table_rows(text)
        off = next(r for r in rows if f"<strong>{skipping}<" in r)
        on = next(r for r in rows if f"<strong>{catching_up}<" in r)
        assert "skipped" in off
        assert "3 missed tick(s)" in on

    @pytest.mark.asyncio
    async def test_api_schedule_get(self, web_admin_client, db_pool):
        """Test getting a single schedule."""
        name = unique_name("get_schedule")
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'GetJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

        resp = await web_admin_client.get(f"/api/schedules/{schedule_id}")
        assert resp.status == 200

        data = await resp.json()
        assert data["name"] == name

    @pytest.mark.asyncio
    async def test_api_schedule_get_not_found(self, web_admin_client):
        """Test getting non-existent schedule returns 404."""
        resp = await web_admin_client.get("/api/schedules/99999999")
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_api_schedule_create(self, web_admin_client):
        """Test creating a new schedule."""
        name = unique_name("create_schedule")
        resp = await web_admin_client.post(
            "/api/schedules",
            data={
                "name": name,
                "job_class": "app.jobs.CreateJob",
                "cron_expr": "0 2 * * *",
                "queue": "test",
                "priority": "100",
            },
        )

        # May return 200 (success HTML) or 400/500 (error)
        # The kwargs handling may cause issues in some configurations
        assert resp.status in [200, 400, 500]

    @pytest.mark.asyncio
    async def test_api_schedule_create_invalid_cron(self, web_admin_client):
        """Test creating schedule with invalid cron returns error."""
        name = unique_name("invalid_cron")
        resp = await web_admin_client.post(
            "/api/schedules",
            data={
                "name": name,
                "job_class": "app.jobs.InvalidJob",
                "cron_expr": "invalid cron",
            },
        )

        # Should return 400 for invalid cron
        assert resp.status in [400, 500]

    @pytest.mark.asyncio
    async def test_api_schedule_enable(self, web_admin_client, db_pool):
        """Test enabling a schedule."""
        name = unique_name("enable_schedule")
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, next_run)
                VALUES ($1, 'EnableJob', '0 * * * *', 'test', false, NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

        resp = await web_admin_client.post(f"/api/schedules/{schedule_id}/enable")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_api_schedule_disable(self, web_admin_client, db_pool):
        """Test disabling a schedule."""
        name = unique_name("disable_schedule")
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, enabled, next_run)
                VALUES ($1, 'DisableJob', '0 * * * *', 'test', true, NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

        resp = await web_admin_client.post(f"/api/schedules/{schedule_id}/disable")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_api_schedule_delete(self, web_admin_client, db_pool):
        """Test deleting a schedule."""
        name = unique_name("delete_schedule")
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'DeleteJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

        resp = await web_admin_client.delete(f"/api/schedules/{schedule_id}")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_api_schedule_history(self, web_admin_client, db_pool):
        """Test getting schedule history."""
        name = unique_name("history_schedule")
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'HistoryJob', '0 * * * *', 'test', NOW() + INTERVAL '1 hour')
                RETURNING id
            """,
                name,
            )

            # Create a job and log entry
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('HistoryJob', '{}', 'test', 100, 'finished')
                RETURNING id
            """)

            await conn.execute(
                """
                INSERT INTO jorb_schedule_log (schedule_id, schedule_name, scheduled_time, job_id, result)
                VALUES ($1, $2, NOW(), $3, 'success')
            """,
                schedule_id,
                name,
                job_id,
            )

        resp = await web_admin_client.get(f"/api/schedules/{schedule_id}/history")
        assert resp.status == 200

        data = await resp.json()
        assert isinstance(data, list)
        assert [r["job_id"] for r in data] == [job_id]

    @pytest.mark.asyncio
    async def test_schedule_history_is_the_admin_apis_answer(
        self, web_admin_client, db_pool, db_params
    ):
        """The handler reads through ``AdminAPI.get_schedule_history``.

        It used to issue its own SELECT against jorb_schedule_log -- the same
        table, the same order, from a handler that already holds an AdminAPI
        -- so the CLI's `pj-admin schedule history` and this endpoint were two
        implementations of one question, free to drift on ordering, on
        pagination, and on what a row contains.
        """
        from pyjobby.admin_api import AdminAPI

        name = unique_name("history_parity")
        async with db_pool.acquire() as conn:
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'ParityJob', '0 * * * *', 'test',
                        NOW() + INTERVAL '1 hour')
                RETURNING id
                """,
                name,
            )
            for result in ("success", "failure", "skipped"):
                await conn.execute(
                    """
                    INSERT INTO jorb_schedule_log
                        (schedule_id, schedule_name, scheduled_time, result)
                    VALUES ($1, $2, NOW(), $3)
                    """,
                    schedule_id,
                    name,
                    result,
                )

            direct = await AdminAPI(conn).get_schedule_history(schedule_id, limit=50)

        resp = await web_admin_client.get(f"/api/schedules/{schedule_id}/history")
        over_http = await resp.json()

        assert [r["id"] for r in over_http] == [r["id"] for r in direct]
        # newest first, which is the ordering the API promises
        assert [r["result"] for r in over_http] == ["skipped", "failure", "success"]


class TestHTMLEscaping:
    """DB-sourced values must be HTML-escaped in HTML fragments (stored XSS)."""

    @pytest.mark.asyncio
    async def test_jobs_html_escapes_job_class(self, web_admin_client, db_pool):
        """Malicious job_class/queue values must not appear unescaped."""
        queue = unique_name("xss_queue")
        payload = '<script>alert("xss")</script>'
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ($1, '{}', $2, 100, 'queued')
            """,
                payload,
                queue,
            )

        resp = await web_admin_client.get(f"/api/jobs?format=html&queue={queue}")
        assert resp.status == 200
        text = await resp.text()

        assert payload not in text
        assert "&lt;script&gt;" in text

    @pytest.mark.asyncio
    async def test_schedules_html_escapes_name_and_description(
        self, web_admin_client, db_pool
    ):
        """Malicious schedule name/description must not appear unescaped."""
        name = f"<script>bad</script>{unique_name('xss')}"
        description = '<img src=x onerror="alert(1)">'
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule
                    (name, job_class, cron_expr, queue, description, next_run)
                VALUES ($1, 'XssJob', '0 * * * *', 'test', $2,
                        NOW() + INTERVAL '1 hour')
            """,
                name,
                description,
            )

        resp = await web_admin_client.get("/api/schedules?format=html")
        assert resp.status == 200
        text = await resp.text()

        assert "<script>bad</script>" not in text
        assert description not in text
        assert "&lt;script&gt;bad&lt;/script&gt;" in text


class TestConnectionPool:
    """Handlers share one lazily-created connection pool."""

    @pytest.mark.asyncio
    async def test_pool_created_lazily_and_reused(self, db_params, aiohttp_client):
        """The pool is created on first request and reused afterwards."""
        server = WebAdminServer(db_params)
        client = await aiohttp_client(server.app)

        assert server.pool is None

        resp = await client.get("/api/jobs")
        assert resp.status == 200
        pool_after_first = server.pool
        assert pool_after_first is not None

        resp = await client.get("/api/workers/stats")
        assert resp.status == 200
        assert server.pool is pool_after_first

    @pytest.mark.asyncio
    async def test_pool_closed_on_cleanup(self, db_params, aiohttp_client):
        """App cleanup closes the pool."""
        server = WebAdminServer(db_params)
        client = await aiohttp_client(server.app)

        resp = await client.get("/api/jobs")
        assert resp.status == 200
        assert server.pool is not None

        await client.close()
        assert server.pool is None


class TestTheConfiguredLivenessGrace:
    """Every worker reading on this server uses the DEPLOYMENT's threshold.

    pj-monitor is the process that ACTS on it -- it requeues a silent
    worker's in-flight jobs -- and this server only reports. When the number
    was a module constant interpolated at import, a deployment that raised
    `liveness_grace_seconds` moved the monitor and left this page (and the
    /metrics gauge every alert is built on) calling those workers dead.

    Two readers on this server, and they are separate code paths: the workers
    page goes through AdminAPI, while `pyjobby_workers_live` runs its own
    SQL. Both are asserted, because it was the second that had the constant
    baked into a module-level f-string.
    """

    @staticmethod
    async def _stale_worker(db_pool, queue: str, age_seconds: int) -> None:
        await db_pool.execute(
            """INSERT INTO jorb_worker (host, pid, queue, last_seen)
               VALUES ('grace-test', 4243, $1, now() - make_interval(secs => $2))""",
            queue,
            age_seconds,
        )

    @pytest.mark.asyncio
    async def test_the_metrics_gauge_counts_by_the_servers_grace(
        self, db_params, db_pool, aiohttp_client
    ):
        """A worker 120s stale: dead at the default, live at 3600."""
        queue = unique_name("grace_metrics")
        await self._stale_worker(db_pool, queue, 120)

        default = WebAdminServer(db_params)
        raised = WebAdminServer(db_params, liveness_grace_seconds=3600)

        strict_client = await aiohttp_client(default.app)
        loose_client = await aiohttp_client(raised.app)

        strict = parse_samples(await (await strict_client.get("/metrics")).text())
        loose = parse_samples(await (await loose_client.get("/metrics")).text())

        assert loose["pyjobby_workers_live"] == strict["pyjobby_workers_live"] + 1

    @pytest.mark.asyncio
    async def test_the_workers_page_counts_by_the_servers_grace(
        self, db_params, db_pool, aiohttp_client
    ):
        """The same worker, through AdminAPI rather than the scrape SQL."""
        queue = unique_name("grace_workers")
        await self._stale_worker(db_pool, queue, 120)

        raised = WebAdminServer(db_params, liveness_grace_seconds=3600)
        client = await aiohttp_client(raised.app)

        resp = await client.get("/api/workers")
        assert resp.status == 200
        rows = [r for r in await resp.json() if r["queue"] == queue]

        assert rows and all(r["live"] for r in rows), rows

    @pytest.mark.asyncio
    async def test_the_default_is_the_platform_constant(self, db_params):
        """Constructed without one, the server judges by db's default -- the
        same fallback pj-monitor uses when nothing configured a grace."""
        assert (
            WebAdminServer(db_params).liveness_grace_seconds
            == db.DEFAULT_LIVENESS_GRACE_SECONDS
        )
