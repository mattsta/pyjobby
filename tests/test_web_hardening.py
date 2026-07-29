"""Hostile-input and boundary hardening for the web admin HTTP surface.

``tests/test_web_admin.py`` covers the happy paths and a couple of XSS spot
checks. This file covers the adversarial ones:

1. Stored XSS as a *property* over every HTML-rendering endpoint (the endpoint
   list is derived from the router, so a new HTML surface fails the audit).
2. Prometheus exposition-format injection through label values.
3. /metrics cardinality with many queues.
4. Pool behavior under concurrent requests (acquire/release leaks).
5. The exact status code every JSON endpoint returns for missing ids and
   malformed query parameters — including the ones that are 500s today.

The web admin has **no authentication of any kind**; every assertion below is
made by an anonymous client. See ``tests/test_ws_hardening.py`` for the
equivalent specification of the websocket surface.
"""

from __future__ import annotations

import asyncio
import html
import re
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pyjobby.client import DEFAULT_PRIO_CEILING
from pyjobby.web_admin import (
    MAX_PAGE_LIMIT,
    MAX_QUEUE_NAME_LENGTH,
    MAX_SINCE_HOURS,
    POOL_COMMAND_TIMEOUT_SECONDS,
    WebAdminServer,
)

# =============================================================================
# Harness
# =============================================================================


@dataclass
class Harness:
    """A live WebAdminServer plus an in-process client for it."""

    server: WebAdminServer
    client: TestClient


@pytest_asyncio.fixture
async def web(db_params, aiohttp_client) -> Harness:
    """In-process web admin server + client (same construction as
    tests/test_web_admin.py's web_admin_client, but keeps the server handle)."""
    server = WebAdminServer(db_params)
    client = await aiohttp_client(server.app)
    return Harness(server=server, client=client)


# =============================================================================
# Hostile string generation
# =============================================================================

# Hand-picked payloads: HTML/attribute breakouts, entity confusion, quoting,
# newlines (Prometheus line injection), unicode look-alikes and control chars.
# NUL and lone surrogates are deliberately absent: PostgreSQL TEXT rejects NUL
# and orjson rejects lone surrogates, so they are not reachable input.
HOSTILE_SNIPPETS = (
    "<script>alert(1)</script>",
    '"><script>x</script>',
    "</td></tr></table>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    "javascript:alert(1)",
    "' onmouseover='alert(1)",
    '" onmouseover="alert(1)',
    "&",
    "&amp;",
    "&lt;",
    "<",
    ">",
    '"',
    "'",
    "\\",
    '\\"',
    "\n",
    "\r\n",
    "\t",
    "\x1b[31m",
    "😀",
    "＜script＞",
    "{{7*7}}",
    "${jndi:ldap://x}",
)

SAFE_CHARS = st.characters(exclude_characters="\x00", exclude_categories=("Cs",))

hostile_text = st.one_of(
    st.sampled_from(HOSTILE_SNIPPETS),
    st.lists(st.sampled_from(HOSTILE_SNIPPETS), min_size=1, max_size=3).map("".join),
    st.text(alphabet=SAFE_CHARS, min_size=1, max_size=24),
)

HYPOTHESIS_SETTINGS = settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def marked(hostile: str) -> str:
    """Sandwich `hostile` between two copies of a unique alphanumeric marker.

    The marker makes substring assertions exact: without it, checking that the
    raw value ``&`` is absent from a page is meaningless (``&lt;`` contains it),
    and checking a bare ``<script>`` cannot distinguish this row from another
    test's. With markers on both sides, the raw form is never a substring of
    the escaped form (``m&m`` is not inside ``m&amp;m``).
    """
    m = f"x{uuid.uuid4().hex[:10]}"
    return f"{m}{hostile}{m}"


def assert_html_escaped(body: str, value: str, where: str) -> None:
    """The escaped form of `value` is rendered and the raw form never is."""
    escaped = html.escape(value)
    # The templates escape through MarkupSafe (jinja2 autoescape), which
    # writes the two quote characters as the numeric references `&#34;`/`&#39;`
    # where html.escape writes `&quot;`/`&#x27;`. Same characters, same
    # neutralization, different spelling -- so compare on a body normalized to
    # html.escape's spelling. Only the escaped form is normalized: the
    # raw-value assertion below still runs against the bytes actually served.
    normalized = body.replace("&#34;", "&quot;").replace("&#39;", "&#x27;")
    assert escaped in normalized, f"{where}: escaped form of {value!r} is missing"
    if escaped != value:
        assert value not in body, (
            f"{where}: RAW hostile value {value!r} rendered unescaped (XSS)"
        )


# =============================================================================
# 1. XSS: enumerate the HTML surface from the router, then prove escaping
# =============================================================================

# Pages built from string literals only (no DB values reach them).
STATIC_HTML_PAGES = {"/", "/jobs", "/queues", "/workers", "/dlq", "/schedules"}

# Endpoints that render DB values into HTML fragments (htmx targets). Every one
# of these is exercised by a property test below.
DB_HTML_FRAGMENT_PATHS = {
    "/api/jobs",
    "/api/queues",
    "/api/workers",
    "/api/workers/stats",
    "/api/dlq",
    "/api/metrics",
    "/api/schedules",
}

AUDITED_HTML_URLS = (
    STATIC_HTML_PAGES
    | {f"{p}?format=html" for p in STATIC_HTML_PAGES}
    | {f"{p}?format=html" for p in DB_HTML_FRAGMENT_PATHS}
)

EXPECTED_STATIC_GET_ROUTES = [
    "/",
    "/api/dlq",
    "/api/jobs",
    "/api/metrics",
    "/api/queues",
    "/api/schedules",
    "/api/workers",
    "/api/workers/stats",
    "/dlq",
    "/jobs",
    "/metrics",
    "/queues",
    "/schedules",
    "/workers",
]


class TestHTMLSurfaceEnumeration:
    """The set of HTML-rendering endpoints is derived from the router."""

    def test_static_get_routes_are_exactly_the_audited_set(self, web: Harness):
        """Adding a GET route without a path parameter must update this list."""
        static_gets = sorted(
            {
                r.resource.canonical
                for r in web.server.app.router.routes()
                if r.method == "GET" and "{" not in r.resource.canonical
            }
        )
        assert static_gets == EXPECTED_STATIC_GET_ROUTES

    @pytest.mark.asyncio
    async def test_every_html_response_is_in_the_audited_set(self, web: Harness):
        """Probe every static GET route (plain and format=html); anything that
        answers text/html must be a URL the XSS property tests cover."""
        seen_html: set[str] = set()
        for path in EXPECTED_STATIC_GET_ROUTES:
            for url in (path, f"{path}?format=html"):
                resp = await web.client.get(url)
                assert resp.status == 200, f"{url} -> {resp.status}"
                if resp.content_type == "text/html":
                    seen_html.add(url)

        assert seen_html <= AUDITED_HTML_URLS, (
            f"unaudited HTML endpoints: {sorted(seen_html - AUDITED_HTML_URLS)}"
        )
        # And every audited URL really does render HTML (no dead entries).
        assert seen_html == AUDITED_HTML_URLS

    @pytest.mark.asyncio
    async def test_parameterized_endpoints_are_json_only(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        """Per-id endpoints never render HTML, even when asked to: they cannot
        be an XSS sink."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('JsonOnly', '{}', 'json_only_q', 100, 'crashed')
                RETURNING id
            """)
            schedule_id = await conn.fetchval(
                """
                INSERT INTO jorb_schedule (name, job_class, cron_expr, queue, next_run)
                VALUES ($1, 'JsonOnly', '0 * * * *', 'test',
                        NOW() + INTERVAL '1 hour')
                RETURNING id
                """,
                f"json_only_{uuid.uuid4().hex[:8]}",
            )

        for url in (
            f"/api/jobs/{job_id}?format=html",
            f"/api/jobs/{job_id}/history?format=html",
            f"/api/jobs/{job_id}/steps?format=html",
            "/api/queues/json_only_q/stats?format=html",
            f"/api/schedules/{schedule_id}?format=html",
            f"/api/schedules/{schedule_id}/history?format=html",
        ):
            resp = await web.client.get(url)
            assert resp.status == 200, f"{url} -> {resp.status}"
            assert resp.content_type == "application/json", url


@pytest.mark.hypothesis
class TestStoredXSSProperties:
    """Property: no DB-sourced value ever reaches HTML unescaped."""

    @HYPOTHESIS_SETTINGS
    @given(job_class=hostile_text, queue=hostile_text)
    @pytest.mark.asyncio
    async def test_jobs_fragment_escapes_job_class_and_queue(
        self, web: Harness, db_pool: asyncpg.Pool, job_class: str, queue: str
    ):
        """/api/jobs?format=html escapes job_class and queue."""
        job_class_value = marked(job_class)
        queue_value = marked(queue)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ($1, '{}', $2, 100, 'queued')
                """,
                job_class_value,
                queue_value,
            )

        resp = await web.client.get("/api/jobs?format=html&limit=200")
        assert resp.status == 200
        body = await resp.text()
        assert_html_escaped(body, job_class_value, "/api/jobs job_class")
        assert_html_escaped(body, queue_value, "/api/jobs queue")

    @HYPOTHESIS_SETTINGS
    @given(queue=hostile_text)
    @pytest.mark.asyncio
    async def test_queues_fragment_escapes_queue_name(
        self, web: Harness, db_pool: asyncpg.Pool, queue: str
    ):
        """/api/queues?format=html escapes queue names in the table body."""
        queue_value = marked(queue)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('QueuesFragment', '{}', $1, 100, 'queued')
                """,
                queue_value,
            )

        resp = await web.client.get("/api/queues?format=html")
        assert resp.status == 200
        body = await resp.text()
        assert_html_escaped(body, queue_value, "/api/queues queue")

    @HYPOTHESIS_SETTINGS
    @given(job_class=hostile_text, error_message=hostile_text)
    @pytest.mark.asyncio
    async def test_dlq_fragment_escapes_class_and_error_message(
        self,
        web: Harness,
        db_pool: asyncpg.Pool,
        job_class: str,
        error_message: str,
    ):
        """/api/dlq?format=html escapes job_class and error_message."""
        job_class_value = marked(job_class)
        error_value = marked(error_message)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state,
                                  error_count, error_message)
                VALUES ($1, '{}', 'dlq_xss', 100, 'crashed', 1, $2)
                """,
                job_class_value,
                error_value,
            )

        resp = await web.client.get("/api/dlq?format=html&limit=200")
        assert resp.status == 200
        body = await resp.text()
        assert_html_escaped(body, job_class_value, "/api/dlq job_class")
        # error_message is truncated at 120 chars before escaping; the marked
        # values here are far shorter, so the whole value must be present.
        assert len(error_value) < 120
        assert_html_escaped(body, error_value, "/api/dlq error_message")

    @HYPOTHESIS_SETTINGS
    @given(name=hostile_text, description=hostile_text, cron_expr=hostile_text)
    @pytest.mark.asyncio
    async def test_schedules_fragment_escapes_all_text_columns(
        self,
        web: Harness,
        db_pool: asyncpg.Pool,
        name: str,
        description: str,
        cron_expr: str,
    ):
        """/api/schedules?format=html escapes name, description, cron_expr,
        queue — including inside the hx-confirm attribute."""
        name_value = marked(name)
        description_value = marked(description)
        cron_value = marked(cron_expr)
        queue_value = marked("q")
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb_schedule
                    (name, description, job_class, cron_expr, queue, next_run)
                VALUES ($1, $2, 'SchedXss', $3, $4, NOW() + INTERVAL '1 hour')
                """,
                name_value,
                description_value,
                cron_value,
                queue_value,
            )

        resp = await web.client.get("/api/schedules?format=html")
        assert resp.status == 200
        body = await resp.text()
        assert_html_escaped(body, name_value, "/api/schedules name")
        assert_html_escaped(body, description_value, "/api/schedules description")
        assert_html_escaped(body, cron_value, "/api/schedules cron_expr")
        assert_html_escaped(body, queue_value, "/api/schedules queue")

    @HYPOTHESIS_SETTINGS
    @given(host=hostile_text, queue=hostile_text, job_class=hostile_text)
    @pytest.mark.asyncio
    async def test_workers_fragment_escapes_host_queue_and_current_job(
        self,
        web: Harness,
        db_pool: asyncpg.Pool,
        host: str,
        queue: str,
        job_class: str,
    ):
        """/api/workers?format=html escapes worker host/queue and the claimed
        job's class."""
        host_value = marked(host)
        queue_value = marked(queue)
        job_class_value = marked(job_class)
        async with db_pool.acquire() as conn:
            worker_id = await conn.fetchval(
                """
                INSERT INTO jorb_worker (host, pid, queue)
                VALUES ($1, 4242, $2) RETURNING id
                """,
                host_value,
                queue_value,
            )
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state, claimed_by)
                VALUES ($1, '{}', $2, 100, 'running', $3)
                """,
                job_class_value,
                queue_value,
                worker_id,
            )

        resp = await web.client.get("/api/workers?format=html")
        assert resp.status == 200
        body = await resp.text()
        assert_html_escaped(body, host_value, "/api/workers host")
        assert_html_escaped(body, queue_value, "/api/workers queue")
        assert_html_escaped(body, job_class_value, "/api/workers current_job_class")


# =============================================================================
# 2. /metrics — Prometheus exposition format under hostile labels
# =============================================================================

METRIC_NAMES = {
    "pyjobby_jobs_by_state",
    "pyjobby_queue_oldest_queued_seconds",
    "pyjobby_backlog_depth",
    "pyjobby_queue_paused",
    "pyjobby_workers_live",
    # live workers that are claiming nothing (abandoned job threads fill
    # their pool), and the worst worker's count -- both fleet-wide scalars,
    # deliberately unlabelled: a per-worker label would grow with the fleet
    "pyjobby_workers_not_claiming",
    "pyjobby_worker_job_threads_abandoned_max",
    # windowed job-outcome gauges (see tests/test_metrics_scrape_cost.py for
    # why the three *_total series they replaced could not stay counters)
    "pyjobby_jobs_started_recent",
    "pyjobby_jobs_terminal_recent",
    "pyjobby_job_duration_seconds",
    # the one cumulative counter, sourced from the job id sequence
    "pyjobby_jobs_enqueued_total",
    # platform-health gauges (no queue label)
    "pyjobby_throughput_jobs_per_second",
    "pyjobby_arrival_jobs_per_second",
    "pyjobby_retry_attempts_per_second",
    "pyjobby_dlq_jobs_per_second",
    "pyjobby_jobs_inflight",
    "pyjobby_jobs_stuck",
    "pyjobby_inflight_oldest_age_seconds",
    "pyjobby_notify_queue_usage_ratio",
    # footprint gauges (labelled by table, never by queue)
    "pyjobby_table_total_bytes",
    "pyjobby_table_bytes",
    "pyjobby_table_index_bytes",
    "pyjobby_table_live_tuples",
    "pyjobby_table_dead_tuples",
    "pyjobby_table_dead_tuple_ratio",
}

# Series that carry a queue label, and therefore multiply with the number of
# distinct queue names. The hostile-label tests count them: a queue name is
# attacker-controlled, so every one of these is a place a forged line could
# appear.
QUEUE_LABELLED_FOR_ONE_QUEUED_JOB = (
    "pyjobby_backlog_depth",
    "pyjobby_jobs_by_state",
    "pyjobby_queue_oldest_queued_seconds",
)

# name{labels} value  — labels are a quoted-string soup we only split loosely,
# because the point of the check is that no line can be forged at all.
SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>.*)\})?"
    r" (?P<value>-?(?:\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|NaN|\+Inf|-Inf))$"
)


def prom_escape(value: str) -> str:
    """Independent re-implementation of the exposition label escaping."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def parse_exposition(body: str) -> list[tuple[str, str]]:
    """Parse the body into (metric_name, labels) samples, failing on any line
    that is not a comment or a well-formed sample."""
    samples: list[tuple[str, str]] = []
    lines = body.split("\n")
    assert lines[-1] == "", "exposition body must end with a newline"
    for line in lines[:-1]:
        if line.startswith("#"):
            assert re.match(r"^# (HELP|TYPE) [a-zA-Z_:][a-zA-Z0-9_:]* ", line), (
                f"malformed comment line: {line!r}"
            )
            continue
        m = SAMPLE_RE.match(line)
        assert m, f"line is not a valid Prometheus sample: {line!r}"
        name = m.group("name")
        assert name in METRIC_NAMES, f"unknown/forged metric name {name!r} in {line!r}"
        samples.append((name, m.group("labels") or ""))
    return samples


class TestPrometheusExposition:
    """Label values are attacker-controlled (queue names); the exposition
    format must survive them."""

    @pytest.mark.asyncio
    async def test_newline_in_queue_name_cannot_forge_a_metric_line(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        """A queue named so as to inject a whole extra sample line produces
        exactly one line, with the injection escaped inline."""
        marker = f"x{uuid.uuid4().hex[:10]}"
        nasty = f'{marker}"\npyjobby_jobs_by_state{{queue="forged"}} 999\n{marker}'
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('PromInject', '{}', $1, 100, 'queued')
                """,
                nasty,
            )

        resp = await web.client.get("/metrics")
        assert resp.status == 200
        body = await resp.text()

        # Nothing forged: the escaped label keeps a backslash before every
        # quote, so the forged label text cannot appear.
        assert 'queue="forged"' not in body
        assert "\\nforged" not in body  # sanity: name is not split either
        assert f'queue="{prom_escape(nasty)}"' in body

        marked_lines = [ln for ln in body.split("\n") if marker in ln]
        # Exactly the queue-labelled series mention this queue: the job is
        # queued and due, so it is backlog as well as state (no jorb_queue
        # control row exists, so the paused gauge does not appear).
        assert len(marked_lines) == len(QUEUE_LABELLED_FOR_ONE_QUEUED_JOB), marked_lines
        names = sorted(ln.split("{", 1)[0] for ln in marked_lines)
        assert names == sorted(QUEUE_LABELLED_FOR_ONE_QUEUED_JOB)
        parse_exposition(body)

    @HYPOTHESIS_SETTINGS
    @given(queue=hostile_text)
    @pytest.mark.hypothesis
    @pytest.mark.asyncio
    async def test_hostile_queue_name_yields_exactly_the_expected_lines(
        self, web: Harness, db_pool: asyncpg.Pool, queue: str
    ):
        """Property: whatever the queue name, it contributes exactly the
        expected number of sample lines and every line stays parseable."""
        queue_value = marked(queue)
        marker = queue_value[:11]
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('PromProp', '{}', $1, 100, 'queued')
                """,
                queue_value,
            )

        resp = await web.client.get("/metrics")
        assert resp.status == 200
        body = await resp.text()

        parse_exposition(body)
        marked_lines = [ln for ln in body.split("\n") if marker in ln]
        assert len(marked_lines) == len(QUEUE_LABELLED_FOR_ONE_QUEUED_JOB), marked_lines
        assert (
            f'pyjobby_jobs_by_state{{queue="{prom_escape(queue_value)}",'
            f'state="queued"}} 1'
        ) in body
        assert (
            f'pyjobby_backlog_depth{{queue="{prom_escape(queue_value)}"}} 1'
        ) in body

    @pytest.mark.asyncio
    async def test_one_line_per_series_and_stable_names(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """Every (name, labels) series appears at most once, and the metric
        name/label set is exactly the documented one."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('SeriesJob', '{}', $1, 100, 'queued') RETURNING id
                """,
                unique_queue,
            )
            await conn.execute(
                "UPDATE jorb SET state = 'running' WHERE id = $1", job_id
            )
            await conn.execute(
                """
                UPDATE jorb SET state = 'finished',
                                started = now() - interval '5 seconds',
                                finished = now()
                WHERE id = $1
                """,
                job_id,
            )
            # A second job that stays queued (oldest-queued gauge) and a third
            # that crashes (crashed counter) so every metric family is present.
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('SeriesJob', '{}', $1, 100, 'queued')
                """,
                unique_queue,
            )
            crash_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('SeriesJob', '{}', $1, 100, 'queued') RETURNING id
                """,
                unique_queue,
            )
            await conn.execute(
                "UPDATE jorb SET state = 'running' WHERE id = $1", crash_id
            )
            await conn.execute(
                "UPDATE jorb SET state = 'crashed' WHERE id = $1", crash_id
            )
            await conn.execute(
                "INSERT INTO jorb_queue (name, paused) VALUES ($1, TRUE)", unique_queue
            )
            await conn.execute("""
                INSERT INTO jorb_worker (host, pid, queue)
                VALUES ('series_host', 31337, 'default')
            """)

        resp = await web.client.get("/metrics")
        body = await resp.text()
        samples = parse_exposition(body)

        assert len(samples) == len(set(samples)), (
            f"duplicate series: {[s for s in samples if samples.count(s) > 1]}"
        )
        names = {name for name, _ in samples}
        assert names == METRIC_NAMES, f"metric names drifted: {sorted(names)}"

        # HELP/TYPE appear exactly once per metric name.
        for name in METRIC_NAMES:
            assert body.count(f"# HELP {name} ") == 1, name
            assert body.count(f"# TYPE {name} ") == 1, name

        assert f'pyjobby_queue_paused{{queue="{unique_queue}"}} 1' in body
        assert "pyjobby_workers_live 1" in body

        # The platform-health gauges carry no queue label at all, so a
        # hostile queue name can never reach them.
        for unlabelled in (
            "pyjobby_throughput_jobs_per_second",
            "pyjobby_arrival_jobs_per_second",
            "pyjobby_retry_attempts_per_second",
            "pyjobby_dlq_jobs_per_second",
            "pyjobby_jobs_inflight",
            "pyjobby_jobs_stuck",
            "pyjobby_inflight_oldest_age_seconds",
            "pyjobby_notify_queue_usage_ratio",
            "pyjobby_jobs_enqueued_total",
            # fleet-wide, so no queue label and no per-worker label either
            "pyjobby_workers_not_claiming",
            "pyjobby_worker_job_threads_abandoned_max",
        ):
            assert [s for s in samples if s[0] == unlabelled] == [(unlabelled, "")], (
                f"{unlabelled} must be a single unlabelled series"
            )

        # Footprint gauges are labelled by table name, which is ours.
        assert sorted(
            labels for name, labels in samples if name == "pyjobby_table_total_bytes"
        ) == ['table="jorb"', 'table="jorb_history"', 'table="jorb_step"']

    @pytest.mark.asyncio
    async def test_cardinality_fifty_queues_all_reported(
        self, web: Harness, db_pool: asyncpg.Pool, test_id: str
    ):
        """50 distinct queues are all exposed, one series each.

        NOTE (known scaling consideration): /metrics has no cardinality bound.
        Queue names are free-form and every distinct name creates its own set
        of series, so a system that mints queues dynamically (per tenant, per
        deploy, per request) grows the scrape payload without limit. The
        endpoint is correct here by design; bounding it is an operational
        decision (aggregate, or allowlist queue names) rather than a bug.
        """
        queues = [f"card_{test_id}_{i:03d}" for i in range(50)]
        async with db_pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('CardJob', '{}', $1, 100, 'queued')
                """,
                [(q,) for q in queues],
            )

        resp = await web.client.get("/metrics")
        assert resp.status == 200
        body = await resp.text()
        parse_exposition(body)

        for q in queues:
            assert f'pyjobby_jobs_by_state{{queue="{q}",state="queued"}} 1' in body
        assert body.count("pyjobby_queue_oldest_queued_seconds{queue=") == 50
        assert body.count("pyjobby_backlog_depth{queue=") == 50


# =============================================================================
# 3. Concurrency on the single shared pool
# =============================================================================

CONCURRENT_URLS = (
    "/api/jobs?limit=5",
    "/api/queues",
    "/api/workers",
    "/api/workers/stats",
    "/api/dlq",
    "/api/metrics",
    "/api/schedules",
    "/metrics",
)


class TestConcurrencyAndPoolLifecycle:
    """One pool (min 1, max 10) serves every handler."""

    @pytest.mark.asyncio
    async def test_concurrent_requests_all_succeed_and_release_connections(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """40 concurrent requests over 8 endpoints: all 200, and every pooled
        connection is handed back (an acquire/release leak would leave the pool
        with fewer idle connections than it has, then deadlock)."""
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('ConcJob', '{}', $1, 100, 'queued')
                """,
                unique_queue,
            )

        urls = list(CONCURRENT_URLS) * 5
        assert len(urls) == 40
        responses = await asyncio.gather(*(web.client.get(u) for u in urls))
        statuses = [r.status for r in responses]
        assert statuses == [200] * 40, dict(zip(urls, statuses, strict=True))
        for r in responses:
            await r.read()

        pool = web.server.pool
        assert pool is not None
        assert pool.get_size() <= 10
        assert pool.get_idle_size() == pool.get_size(), (
            "connections were not released back to the pool"
        )

        # A second wave still works with the same pool object.
        again = await asyncio.gather(*(web.client.get(u) for u in CONCURRENT_URLS))
        assert [r.status for r in again] == [200] * len(CONCURRENT_URLS)
        for r in again:
            await r.read()
        assert web.server.pool is pool

    @pytest.mark.asyncio
    async def test_pool_closed_on_app_cleanup(self, web: Harness):
        """App cleanup (on_cleanup hook) closes and drops the pool."""
        resp = await web.client.get("/api/jobs")
        assert resp.status == 200
        pool = web.server.pool
        assert pool is not None

        await web.client.close()

        assert web.server.pool is None
        assert pool._closed is True


# =============================================================================
# 4. Error handling: exact statuses for missing ids and malformed params
# =============================================================================

MISSING_ID = 987654321


class TestMissingIdBehavior:
    """Specification of what every JSON endpoint does with an id that does
    not exist. The rule is uniform: "does not exist" is 404 everywhere, and
    400 is reserved for malformed input (see TestMalformedInput)."""

    # (method, url template, expected status, note)
    CASES: tuple[tuple[str, str, int, str], ...] = (
        ("GET", "/api/jobs/{id}", 404, "ok"),
        ("GET", "/api/jobs/{id}/history", 404, "ok"),
        ("GET", "/api/jobs/{id}/steps", 404, "ok"),
        ("DELETE", "/api/jobs/{id}", 404, "ok"),
        # Mutations agree with the reads: a missing job is 404, not 400.
        ("POST", "/api/jobs/{id}/retry", 404, "ok"),
        ("POST", "/api/jobs/{id}/cancel", 404, "ok"),
        ("POST", "/api/dlq/{id}/retry", 404, "ok"),
        ("GET", "/api/schedules/{id}", 404, "ok"),
        # AdminAPI raises ValueError('Schedule N not found'); the handlers
        # check existence first so this is a 404 like the sibling GET.
        ("POST", "/api/schedules/{id}/enable", 404, "ok"),
        ("POST", "/api/schedules/{id}/disable", 404, "ok"),
        ("DELETE", "/api/schedules/{id}", 404, "ok"),
        # Deliberate exception: an unknown schedule's log is empty, not an
        # error (documented in api_schedule_history).
        ("GET", "/api/schedules/{id}/history", 200, "200 with []"),
    )

    @pytest.mark.asyncio
    async def test_missing_ids_return_documented_statuses(self, web: Harness):
        actual: list[tuple[str, str, int]] = []
        for method, template, _expected, _note in self.CASES:
            url = template.format(id=MISSING_ID)
            resp = await web.client.request(method, url)
            await resp.read()
            actual.append((method, template, resp.status))

        expected = [(m, t, s) for m, t, s, _ in self.CASES]
        assert actual == expected

    @pytest.mark.asyncio
    async def test_missing_id_404s_carry_a_json_error_body(self, web: Harness):
        """A 404 is not an empty aiohttp error page: it names the row."""
        for method, template, expected, _note in self.CASES:
            if expected != 404:
                continue
            url = template.format(id=MISSING_ID)
            resp = await web.client.request(method, url)
            assert resp.content_type == "application/json", url
            body = await resp.json()
            assert str(MISSING_ID) in body["error"], (url, body)

    @pytest.mark.asyncio
    async def test_missing_schedule_history_is_empty_list(self, web: Harness):
        resp = await web.client.get(f"/api/schedules/{MISSING_ID}/history")
        assert resp.status == 200
        assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_unknown_queue_stats_is_empty_list(self, web: Harness):
        """A queue that was never used is not an error: empty stats, 200."""
        resp = await web.client.get("/api/queues/no_such_queue_ever/stats")
        assert resp.status == 200
        assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_pause_creates_control_row_for_unknown_queue(
        self, web: Harness, db_pool: asyncpg.Pool, test_id: str
    ):
        """A mutation against a queue that does not exist *creates* it. Kept
        deliberately: queues are implicit, and pre-emptively pausing one before
        its first job is a legitimate operation (documented in the handler)."""
        queue = f"ghost_{test_id}"
        resp = await web.client.post(f"/api/queues/{queue}/pause")
        assert resp.status == 200
        assert (await resp.json())["paused"] is True

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT paused FROM jorb_queue WHERE name = $1", queue
                )
                is True
            )

    @pytest.mark.asyncio
    async def test_queue_name_length_is_bounded_on_every_queue_route(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        """Pre-emptive pause is allowed, so the row an anonymous client can
        insert is bounded instead: a name over MAX_QUEUE_NAME_LENGTH is 400 on
        every queue route and writes nothing."""
        long_name = "q" * (MAX_QUEUE_NAME_LENGTH + 1)
        at_limit = f"{'q' * (MAX_QUEUE_NAME_LENGTH - 9)}{uuid.uuid4().hex[:9]}"

        for method, url in (
            ("POST", f"/api/queues/{long_name}/pause"),
            ("POST", f"/api/queues/{long_name}/resume"),
            ("GET", f"/api/queues/{long_name}/stats"),
        ):
            resp = await web.client.request(method, url)
            assert resp.status == 400, url
            assert "queue name" in (await resp.json())["error"]

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jorb_queue WHERE length(name) > $1",
                    MAX_QUEUE_NAME_LENGTH,
                )
                == 0
            )

        # Exactly at the bound still works: the limit is not a moved goalpost.
        resp = await web.client.post(f"/api/queues/{at_limit}/pause")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_failed_mutations_have_no_side_effects(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        """A retry/cancel/delete against a missing id must not create rows."""
        async with db_pool.acquire() as conn:
            before_jobs = await conn.fetchval("SELECT COUNT(*) FROM jorb")
            before_scheds = await conn.fetchval("SELECT COUNT(*) FROM jorb_schedule")

        for method, url in (
            ("POST", f"/api/jobs/{MISSING_ID}/retry"),
            ("POST", f"/api/jobs/{MISSING_ID}/cancel"),
            ("DELETE", f"/api/jobs/{MISSING_ID}"),
            ("POST", f"/api/dlq/{MISSING_ID}/retry"),
            ("DELETE", f"/api/schedules/{MISSING_ID}"),
        ):
            resp = await web.client.request(method, url)
            await resp.read()
            assert resp.status == 404, f"{method} {url} -> {resp.status}"

        async with db_pool.acquire() as conn:
            assert await conn.fetchval("SELECT COUNT(*) FROM jorb") == before_jobs
            assert (
                await conn.fetchval("SELECT COUNT(*) FROM jorb_schedule")
                == before_scheds
            )


class TestRefusedMutations:
    """The OTHER half of TestMissingIdBehavior: the job exists, and the verb
    refuses it anyway.

    Every mutation route answers with a ``{job_id, status}`` dict in which the
    status is the whole refusal — 'not_retriable'/'not_cancellable' — and the
    handler must turn that into a 400. Without these, dropping the status
    guard would return 2xx with a refusal body for a mutation that changed
    nothing, and every caller that checks the HTTP code (curl, htmx, a deploy
    script) would read "done".

    The refusal message is also asserted NOT to quote a pre-read state: the
    row is read before the verb runs, so a concurrent operator made the
    message contradict itself. The returned status is the authority.
    """

    @staticmethod
    async def _job(pool: asyncpg.Pool, queue: str, state: str) -> int:
        job_id: int = await pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, prio, state)
               VALUES ('tests.dxe_jobs.OkJob', '{}', $1, 100, $2)
               RETURNING id""",
            queue,
            state,
        )
        return job_id

    @pytest.mark.asyncio
    async def test_retry_of_a_running_job_is_400(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """Retry takes crashed/cancelled only. A running job is refused —
        200 with a 'not_retriable' body would tell the operator their retry
        landed while the original attempt is still executing."""
        job_id = await self._job(db_pool, unique_queue, "running")

        resp = await web.client.post(f"/api/jobs/{job_id}/retry")

        assert resp.status == 400
        body = await resp.json()
        assert str(job_id) in body["error"]
        assert "not_retriable" in body["error"]
        assert "running" not in body["error"], "quotes a state it read before the verb"
        # and the refusal changed nothing
        assert (
            await db_pool.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            == "running"
        )

    @pytest.mark.asyncio
    async def test_cancel_of_a_finished_job_is_400(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """Terminal is terminal: there is nothing left to cancel."""
        job_id = await self._job(db_pool, unique_queue, "finished")

        resp = await web.client.post(f"/api/jobs/{job_id}/cancel")

        assert resp.status == 400
        body = await resp.json()
        assert str(job_id) in body["error"]
        assert "not_cancellable" in body["error"]
        assert "finished" not in body["error"], "quotes a state it read before the verb"
        assert (
            await db_pool.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            == "finished"
        )

    @pytest.mark.asyncio
    async def test_dlq_retry_of_a_non_crashed_job_is_400(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """The DLQ is the crashed jobs by definition, so the DLQ retry has a
        narrower guard than the ordinary one: a queued job is not in it."""
        job_id = await self._job(db_pool, unique_queue, "queued")

        resp = await web.client.post(f"/api/dlq/{job_id}/retry")

        assert resp.status == 400
        body = await resp.json()
        assert str(job_id) in body["error"]
        assert "not_retriable" in body["error"]
        assert (
            await db_pool.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)
            == "queued"
        )

    @pytest.mark.asyncio
    async def test_html_dlq_retry_of_a_non_crashed_job_is_400(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """The htmx button takes the same refusal: ?format=html must not swap
        a refreshed table in as though the retry had happened."""
        job_id = await self._job(db_pool, unique_queue, "cancelled")

        resp = await web.client.post(f"/api/dlq/{job_id}/retry?format=html")

        assert resp.status == 400
        assert resp.content_type == "application/json"


class TestMalformedInput:
    """Path ids and query parameters go through two shared parsers
    (``_path_id``/``_query_int``), so malformed input never reaches ``int()``
    or PostgreSQL: it is a 400 with a JSON body naming the parameter. Nothing
    is silently clamped — a clamped limit/offset would return the wrong page
    of results while claiming success."""

    NON_INTEGER_PATHS: tuple[tuple[str, str, int], ...] = (
        ("GET", "/api/jobs/abc", 400),
        ("GET", "/api/jobs/1.5", 400),
        ("GET", "/api/jobs/-1", 400),
        ("GET", "/api/jobs/1_0", 400),
        ("GET", "/api/jobs/9223372036854775808", 400),
        ("GET", "/api/jobs/abc/history", 400),
        ("GET", "/api/jobs/abc/steps", 400),
        ("POST", "/api/jobs/abc/retry", 400),
        ("POST", "/api/jobs/abc/cancel", 400),
        ("DELETE", "/api/jobs/abc", 400),
        ("POST", "/api/dlq/abc/retry", 400),
        ("GET", "/api/schedules/abc", 400),
        ("POST", "/api/schedules/abc/enable", 400),
        ("POST", "/api/schedules/abc/disable", 400),
        ("DELETE", "/api/schedules/abc", 400),
        ("GET", "/api/schedules/abc/history", 400),
    )

    @pytest.mark.asyncio
    async def test_non_integer_path_ids(self, web: Harness):
        """A path id that is not a decimal integer inside the bigint range is
        malformed input: 400, on every per-id route."""
        actual = []
        for method, url, _expected in self.NON_INTEGER_PATHS:
            resp = await web.client.request(method, url)
            await resp.read()
            actual.append((method, url, resp.status))
        assert actual == list(self.NON_INTEGER_PATHS)

    @pytest.mark.asyncio
    async def test_malformed_path_id_says_which_id(self, web: Harness):
        resp = await web.client.get("/api/jobs/abc/history")
        assert resp.status == 400
        assert (await resp.json())["error"] == (
            "Malformed job_id: 'abc' is not a valid id"
        )

    QUERY_CASES: tuple[tuple[str, int, str], ...] = (
        # Non-integers are rejected by the parser, not by int().
        ("/api/jobs?limit=abc", 400, "not an integer"),
        ("/api/jobs?offset=abc", 400, "not an integer"),
        ("/api/dlq?limit=abc", 400, "not an integer"),
        ("/api/metrics?since_hours=abc", 400, "not an integer"),
        (f"/api/schedules/{MISSING_ID}/history?limit=abc", 400, "not an integer"),
        # Negative LIMIT/OFFSET never reaches PostgreSQL.
        ("/api/jobs?limit=-1", 400, "below minimum"),
        ("/api/jobs?offset=-1", 400, "below minimum"),
        ("/api/dlq?limit=-1", 400, "below minimum"),
        # Absurd values are refused instead of overflowing the bigint bind or
        # the datetime arithmetic.
        ("/api/jobs?limit=99999999999999999999", 400, "above int64"),
        ("/api/metrics?since_hours=999999999999", 400, "above MAX_SINCE_HOURS"),
        # A page is bounded on every listing route, not just the busy one.
        (f"/api/jobs?limit={MAX_PAGE_LIMIT + 1}", 400, "above MAX_PAGE_LIMIT"),
        (f"/api/dlq?limit={MAX_PAGE_LIMIT + 1}", 400, "above MAX_PAGE_LIMIT"),
        (
            f"/api/schedules/{MISSING_ID}/history?limit={MAX_PAGE_LIMIT + 1}",
            400,
            "above MAX_PAGE_LIMIT",
        ),
        (f"/api/jobs/{MISSING_ID}/history?limit={MAX_PAGE_LIMIT + 1}", 400, "ditto"),
        (f"/api/jobs/{MISSING_ID}/steps?limit={MAX_PAGE_LIMIT + 1}", 400, "ditto"),
        (f"/api/metrics?since_hours={MAX_SINCE_HOURS + 1}", 400, "wider than 90 days"),
        # Invalid enum is checked against db.JobState before the query runs.
        ("/api/jobs?state=not_a_state", 400, "invalid state"),
        # Benign cases that DO work, unchanged:
        ("/api/jobs?limit=0", 200, "LIMIT 0 -> empty list"),
        (f"/api/jobs?limit={MAX_PAGE_LIMIT}", 200, "the bound itself is a page"),
        ("/api/jobs?limit=", 200, "empty value -> default"),
        ("/api/metrics?since_hours=0", 200, "no window"),
        (f"/api/metrics?since_hours={MAX_SINCE_HOURS}", 200, "widest window"),
        ("/api/jobs?state=crashed", 200, "valid state"),
        ("/api/jobs?format=bogus", 200, "unknown format falls back to JSON"),
        ("/api/schedules?enabled=maybe", 200, "unparsable bool -> False"),
    )

    @pytest.mark.asyncio
    async def test_malformed_query_parameters(self, web: Harness):
        actual = []
        for url, _expected, _note in self.QUERY_CASES:
            resp = await web.client.get(url)
            await resp.read()
            actual.append((url, resp.status))
        assert actual == [(u, s) for u, s, _ in self.QUERY_CASES]

    @pytest.mark.asyncio
    async def test_rejected_query_parameters_name_themselves(self, web: Harness):
        """The 400 body is useful: it names the parameter and the bound."""
        body = await (await web.client.get("/api/jobs?limit=abc")).json()
        assert body["error"] == "Invalid limit: 'abc' is not an integer"

        body = await (await web.client.get("/api/jobs?offset=-1")).json()
        assert body["error"].startswith("Invalid offset: -1 is out of range")

        body = await (await web.client.get("/api/jobs?state=not_a_state")).json()
        assert body["error"].startswith("Invalid state: 'not_a_state'")
        assert "crashed" in body["error"]  # the valid set is spelled out

    @pytest.mark.asyncio
    async def test_out_of_range_values_never_reach_postgresql(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """A rejected request runs no query at all, so a hostile limit cannot
        be used to make PostgreSQL do work."""
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('RangeJob', '{}', $1, 100, 'queued')
                """,
                unique_queue,
            )
        for url in (
            "/api/jobs?limit=-1",
            "/api/jobs?limit=99999999999999999999",
            "/api/metrics?since_hours=999999999999",
            "/api/jobs?state=not_a_state",
        ):
            resp = await web.client.get(url)
            assert resp.status == 400, url
            assert (await resp.json())["error"], url

        # The pool is untouched and the next real request still works.
        resp = await web.client.get(f"/api/jobs?queue={unique_queue}")
        assert resp.status == 200
        assert len(await resp.json()) == 1

    @pytest.mark.asyncio
    async def test_limit_zero_returns_empty_list(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """limit=0 is not an error and really returns nothing."""
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('LimitJob', '{}', $1, 100, 'queued')
                """,
                unique_queue,
            )
        assert await (await web.client.get("/api/jobs?limit=0")).json() == []
        full = await (await web.client.get(f"/api/jobs?queue={unique_queue}")).json()
        assert len(full) == 1

    @pytest.mark.asyncio
    async def test_huge_offset_is_empty_not_an_error(self, web: Harness):
        resp = await web.client.get("/api/jobs?offset=1000000")
        assert resp.status == 200
        assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_schedule_create_missing_required_field(self, web: Harness):
        """A form without a required field is a 400 that names the field, not
        a KeyError escaping as a 500."""
        resp = await web.client.post(
            "/api/schedules",
            data={"job_class": "app.jobs.NoName", "cron_expr": "0 * * * *"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "Missing required field(s): name"

        resp = await web.client.post("/api/schedules", data={})
        assert resp.status == 400
        assert (await resp.json())["error"] == (
            "Missing required field(s): name, job_class, cron_expr"
        )

        # A present-but-empty field is as useless as an absent one.
        resp = await web.client.post(
            "/api/schedules",
            data={"name": "", "job_class": "app.jobs.NoName", "cron_expr": "0 * * * *"},
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "Missing required field(s): name"

    @pytest.mark.asyncio
    async def test_schedule_create_non_integer_prio_is_400(self, web: Harness):
        """int(data['prio']) raises ValueError, which this handler *does*
        catch -> a clean 400 (unlike the query-parameter int() calls)."""
        resp = await web.client.post(
            "/api/schedules",
            data={
                "name": f"badprio_{uuid.uuid4().hex[:8]}",
                "job_class": "app.jobs.BadPrio",
                "cron_expr": "0 * * * *",
                "priority": "high",
            },
        )
        assert resp.status == 400
        assert "invalid literal for int" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_schedule_create_above_prio_ceiling_is_400_and_writes_no_row(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        """A well-formed integer is not a claimable one. Above the worker
        ceiling every firing of this schedule would mint a job that sits
        `queued` forever, so the row must not be created at all."""
        name = f"overprio_{uuid.uuid4().hex[:8]}"
        resp = await web.client.post(
            "/api/schedules",
            data={
                "name": name,
                "job_class": "app.jobs.OverPrio",
                "cron_expr": "0 * * * *",
                "priority": str(DEFAULT_PRIO_CEILING + 1),
            },
        )
        assert resp.status == 400
        error = (await resp.json())["error"]
        assert (
            f"priority {DEFAULT_PRIO_CEILING + 1} is above the worker "
            f"priority ceiling ({DEFAULT_PRIO_CEILING})" in error
        )
        assert "LOWER numbers are MORE urgent" in error

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jorb_schedule WHERE name = $1", name
                )
                == 0
            )

    @pytest.mark.asyncio
    async def test_schedule_create_at_prio_ceiling_is_accepted(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        """The mirror: the ceiling itself is claimable, so it goes through."""
        name = f"atprio_{uuid.uuid4().hex[:8]}"
        resp = await web.client.post(
            "/api/schedules",
            data={
                "name": name,
                "job_class": "app.jobs.AtPrio",
                "cron_expr": "0 * * * *",
                "priority": str(DEFAULT_PRIO_CEILING),
            },
        )
        assert resp.status == 200

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT prio FROM jorb_schedule WHERE name = $1", name
                )
                == DEFAULT_PRIO_CEILING
            )

    @pytest.mark.asyncio
    async def test_schedule_ceiling_is_the_one_this_server_was_told(
        self, db_params, aiohttp_client, db_pool: asyncpg.Pool
    ):
        """`WebAdminServer(prio_ceiling=N)` is this surface's version of
        `JobClient(pool, prio_ceiling=N)`: a fleet running `pj --max-prio
        5000` declares it once and the dashboard's limit moves with it."""
        server = WebAdminServer(db_params, prio_ceiling=5000)
        client = await aiohttp_client(server.app)

        name = f"declared_{uuid.uuid4().hex[:8]}"
        ok = await client.post(
            "/api/schedules",
            data={
                "name": name,
                "job_class": "app.jobs.Declared",
                "cron_expr": "0 * * * *",
                "priority": "5000",
            },
        )
        assert ok.status == 200

        refused_name = f"{name}_2"
        refused = await client.post(
            "/api/schedules",
            data={
                "name": refused_name,
                "job_class": "app.jobs.Declared",
                "cron_expr": "0 * * * *",
                "priority": "5001",
            },
        )
        assert refused.status == 400
        assert (
            "priority 5001 is above the worker priority ceiling (5000)"
            in (await refused.json())["error"]
        )

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT prio FROM jorb_schedule WHERE name = $1", name
                )
                == 5000
            )
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jorb_schedule WHERE name = $1", refused_name
                )
                == 0
            )

    @pytest.mark.asyncio
    async def test_schedule_form_states_the_ceiling_and_the_ordering(
        self, web: Harness
    ):
        """The form says which way the numbers run, because the inverted
        ordering is what produces the unclaimable value in the first place."""
        resp = await web.client.get("/schedules")
        assert resp.status == 200
        body = await resp.text()

        assert f'name="priority" value="100" max="{DEFAULT_PRIO_CEILING}"' in body
        assert (
            f"LOWER is MORE urgent. Above the worker priority ceiling "
            f"({DEFAULT_PRIO_CEILING}) no worker ever claims the job." in body
        )
        assert "<!--PRIO_FIELD-->" not in body

    @pytest.mark.asyncio
    async def test_schedule_create_invalid_cron_is_400(self, web: Harness):
        """The one validated field: croniter's failure becomes a clean 400."""
        resp = await web.client.post(
            "/api/schedules",
            data={
                "name": f"badcron_{uuid.uuid4().hex[:8]}",
                "job_class": "app.jobs.BadCron",
                "cron_expr": "not a cron",
            },
        )
        assert resp.status == 400
        assert "malformed cron expression" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_duplicate_schedule_name_is_409(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        """The UNIQUE(name) violation is a conflict, not a server error."""
        name = f"dupe_{uuid.uuid4().hex[:8]}"
        form: dict[str, Any] = {
            "name": name,
            "job_class": "app.jobs.Dupe",
            "cron_expr": "0 * * * *",
        }
        first = await web.client.post("/api/schedules", data=form)
        assert first.status == 200
        second = await web.client.post("/api/schedules", data=form)
        assert second.status == 409
        assert name in (await second.json())["error"]

        # And the conflict left exactly one row behind.
        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jorb_schedule WHERE name = $1", name
                )
                == 1
            )

    @pytest.mark.asyncio
    async def test_unknown_route_is_404_and_bad_method_is_405(self, web: Harness):
        """Routing itself is sane: no stack traces for the obvious probes."""
        resp = await web.client.get("/../../etc/passwd")
        await resp.read()
        assert resp.status == 404

        resp = await web.client.post("/api/jobs")
        await resp.read()
        assert resp.status == 405


# =============================================================================
# 6. Cross-site mutation defense
# =============================================================================


def _own_origin(client: TestClient) -> str:
    """The origin the server's own pages are served from."""
    return f"http://{client.host}:{client.port}"


class TestCrossSiteMutations:
    """The admin has no authentication, which makes CSRF *more* dangerous
    rather than irrelevant: every mutation here is a CORS simple request, so
    any page the operator visits can auto-submit a form to 127.0.0.1:8081 and
    the write lands. web_admin.cross_site_guard refuses the browser-initiated
    version of that while leaving curl and deploy scripts alone; these tests
    pin both halves, because a guard that also blocked scripting would be
    quietly removed the first time it broke an operator's shell loop."""

    # One mutation of each shape the router exposes.
    MUTATIONS: tuple[tuple[str, str], ...] = (
        ("POST", f"/api/jobs/{MISSING_ID}/retry"),
        ("POST", f"/api/jobs/{MISSING_ID}/cancel"),
        ("DELETE", f"/api/jobs/{MISSING_ID}"),
        ("POST", "/api/queues/csrf-probe/pause"),
        ("POST", "/api/schedules"),
        ("DELETE", f"/api/schedules/{MISSING_ID}"),
        ("POST", f"/api/dlq/{MISSING_ID}/retry"),
    )

    @pytest.mark.asyncio
    async def test_cross_site_mutations_are_403(self, web: Harness):
        """`Sec-Fetch-Site: cross-site` is the browser saying, unforgeably,
        that another site initiated this. Every mutating route refuses it."""
        actual = []
        for method, url in self.MUTATIONS:
            resp = await web.client.request(
                method, url, headers={"Sec-Fetch-Site": "cross-site"}
            )
            await resp.read()
            actual.append((method, url, resp.status))
        assert actual == [(m, u, 403) for m, u in self.MUTATIONS]

    @pytest.mark.asyncio
    async def test_same_site_is_also_refused(self, web: Harness):
        """`same-site` is a different host on the same registrable domain --
        another origin, and this surface has exactly one."""
        resp = await web.client.post(
            "/api/queues/csrf-probe/pause", headers={"Sec-Fetch-Site": "same-site"}
        )
        await resp.read()
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_403_says_why(self, web: Harness):
        resp = await web.client.post(
            "/api/queues/csrf-probe/pause", headers={"Sec-Fetch-Site": "cross-site"}
        )
        assert resp.status == 403
        error = (await resp.json())["error"]
        assert "Cross-site POST refused" in error
        assert "cross-site" in error

    @pytest.mark.asyncio
    async def test_cross_site_mutation_has_no_side_effect(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """The point of the refusal: the write does not happen. The attacker
        never sees the response either way, so only the row matters."""
        resp = await web.client.post(
            f"/api/queues/{unique_queue}/pause",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        await resp.read()
        assert resp.status == 403

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jorb_queue WHERE name = $1", unique_queue
                )
                == 0
            )

    @pytest.mark.asyncio
    async def test_same_origin_mutations_are_allowed(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """The admin's own htmx buttons: same-origin fetch metadata plus the
        page's own Origin. This is the case the guard must never break."""
        resp = await web.client.post(
            f"/api/queues/{unique_queue}/pause",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Origin": _own_origin(web.client),
            },
        )
        await resp.read()
        assert resp.status == 200

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT paused FROM jorb_queue WHERE name = $1", unique_queue
                )
                is True
            )

    @pytest.mark.asyncio
    async def test_typed_url_is_allowed(self, web: Harness, unique_queue: str):
        """`Sec-Fetch-Site: none` is a user-initiated navigation -- the
        operator typing the URL, not a page acting on their behalf."""
        resp = await web.client.post(
            f"/api/queues/{unique_queue}/pause", headers={"Sec-Fetch-Site": "none"}
        )
        await resp.read()
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_headerless_mutations_are_allowed(
        self, web: Harness, unique_queue: str
    ):
        """curl, a deploy script, this test client: no fetch metadata and no
        Origin. Allowed on purpose -- the guard defends against BROWSERS, and
        anything that can open a socket to an unauthenticated admin port can
        already do everything. Breaking scripting would buy nothing."""
        resp = await web.client.post(f"/api/queues/{unique_queue}/pause")
        await resp.read()
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_mismatched_origin_is_403(self, web: Harness, unique_queue: str):
        """Some browsers send Origin without fetch metadata; a foreign one is
        the same statement and gets the same answer."""
        resp = await web.client.post(
            f"/api/queues/{unique_queue}/pause",
            headers={"Origin": "http://evil.example.com"},
        )
        assert resp.status == 403
        error = (await resp.json())["error"]
        assert "Cross-origin POST refused" in error
        assert "evil.example.com" in error

    @pytest.mark.asyncio
    async def test_origin_contradicting_same_origin_claim_is_403(
        self, web: Harness, unique_queue: str
    ):
        """Both headers are checked, so a forged `same-origin` claim paired
        with a foreign Origin still loses."""
        resp = await web.client.post(
            f"/api/queues/{unique_queue}/pause",
            headers={
                "Sec-Fetch-Site": "same-origin",
                "Origin": "http://evil.example.com",
            },
        )
        await resp.read()
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_opaque_origin_is_403(self, web: Harness, unique_queue: str):
        """`Origin: null` is a sandboxed frame or a data: URL -- an origin
        that deliberately identifies nothing, and matches no host."""
        resp = await web.client.post(
            f"/api/queues/{unique_queue}/pause", headers={"Origin": "null"}
        )
        await resp.read()
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_matching_origin_without_fetch_metadata_is_allowed(
        self, web: Harness, unique_queue: str
    ):
        resp = await web.client.post(
            f"/api/queues/{unique_queue}/pause",
            headers={"Origin": _own_origin(web.client)},
        )
        await resp.read()
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_reads_are_never_blocked(self, web: Harness):
        """GET is not guarded: it changes nothing, and the same-origin policy
        already stops a cross-site page from reading the response."""
        for url in ("/", "/api/jobs", "/api/queues", "/metrics"):
            resp = await web.client.get(url, headers={"Sec-Fetch-Site": "cross-site"})
            await resp.read()
            assert resp.status == 200, url

    @pytest.mark.asyncio
    async def test_cross_site_form_post_shape_is_refused(self, web: Harness):
        """The real attack, verbatim: a form-encoded POST with the headers a
        browser attaches to a cross-site form submission."""
        resp = await web.client.post(
            "/api/schedules",
            data={
                "name": f"csrf_{uuid.uuid4().hex[:8]}",
                "job_class": "app.jobs.Csrf",
                "cron_expr": "0 * * * *",
            },
            headers={
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Origin": "http://evil.example.com",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        await resp.read()
        assert resp.status == 403


# =============================================================================
# 7. job_class shape at the API door
# =============================================================================


class TestJobClassShape:
    """`jorb_schedule.job_class` is the input to an import on every worker:
    the module named by everything before the last dot is imported, and its
    module-level code runs before anything checks that the result is a Job.
    An anonymous HTTP client writes that column, so the door checks that the
    value is at least SHAPED like an import path.

    This bounds the shape only -- resolving an arbitrary dotted path is the
    deliberate feature, and whoever can create a schedule can still name any
    class importable on the workers' path. That is the documented trust model
    (docs/ADMIN_TOOLS.md); the surface must not be exposed to an untrusted
    network."""

    REFUSED: tuple[tuple[str, str], ...] = (
        ("nodots", "a bare name names no module"),
        ("a..b", "empty segment"),
        (".leading.Dot", "leading dot"),
        ("trailing.dot.", "trailing dot"),
        (" x.y", "leading whitespace"),
        ("x.y ", "trailing whitespace"),
        ("mod ule.Class", "interior whitespace"),
        ("mod-ule.Class", "not an identifier"),
        ("os.path/../etc/passwd", "a filesystem path is not an import path"),
        ("http://evil.example.com/x.py", "nor is a URL"),
        ("1mod.Class", "identifiers do not start with a digit"),
        ("mod.Class;rm -rf /", "nor is a shell command"),
        ("a." + "b" * 300, "longer than the 255-character cap"),
    )

    @pytest.mark.asyncio
    async def test_misshapen_job_class_is_400(self, web: Harness):
        actual = []
        for job_class, _why in self.REFUSED:
            resp = await web.client.post(
                "/api/schedules",
                data={
                    "name": f"shape_{uuid.uuid4().hex[:8]}",
                    "job_class": job_class,
                    "cron_expr": "0 * * * *",
                },
            )
            await resp.read()
            actual.append((job_class, resp.status))
        assert actual == [(jc, 400) for jc, _ in self.REFUSED]

    @pytest.mark.asyncio
    async def test_empty_job_class_is_the_missing_field_400(self, web: Harness):
        """An empty string never reaches the validator: it is caught one step
        earlier as a missing required field. Still a 400, still no row."""
        resp = await web.client.post(
            "/api/schedules",
            data={
                "name": f"shape_{uuid.uuid4().hex[:8]}",
                "job_class": "",
                "cron_expr": "0 * * * *",
            },
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "Missing required field(s): job_class"

    @pytest.mark.asyncio
    async def test_the_400_says_what_a_job_class_looks_like(self, web: Harness):
        resp = await web.client.post(
            "/api/schedules",
            data={
                "name": f"shape_{uuid.uuid4().hex[:8]}",
                "job_class": "nodots",
                "cron_expr": "0 * * * *",
            },
        )
        assert resp.status == 400
        error = (await resp.json())["error"]
        assert "invalid job_class 'nodots'" in error
        assert "myapp.jobs.SendEmail" in error

    @pytest.mark.asyncio
    async def test_refused_job_class_writes_no_row(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        name = f"shape_{uuid.uuid4().hex[:8]}"
        resp = await web.client.post(
            "/api/schedules",
            data={"name": name, "job_class": "nodots", "cron_expr": "0 * * * *"},
        )
        await resp.read()
        assert resp.status == 400

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT COUNT(*) FROM jorb_schedule WHERE name = $1", name
                )
                == 0
            )

    @pytest.mark.asyncio
    async def test_a_real_dotted_path_still_works(
        self, web: Harness, db_pool: asyncpg.Pool
    ):
        """The feature the check must not break: any importable dotted path,
        with no allowlist of modules anywhere."""
        name = f"shape_{uuid.uuid4().hex[:8]}"
        resp = await web.client.post(
            "/api/schedules",
            data={
                "name": name,
                "job_class": "tests.dxe_jobs.OkJob",
                "cron_expr": "0 * * * *",
            },
        )
        assert resp.status == 200

        async with db_pool.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT job_class FROM jorb_schedule WHERE name = $1", name
                )
                == "tests.dxe_jobs.OkJob"
            )


# =============================================================================
# 8. Bounded pages and bounded windows
# =============================================================================


class TestBoundedResponses:
    """Every listing route caps its page and the metrics window caps its
    span, because both are a single anonymous request asking this process to
    do unbounded work: MAX_PAGE_LIMIT rows must be materialized, shipped and
    (for format=html) concatenated into one string, and a metrics window is
    index-backed only while it is narrow."""

    @pytest.mark.asyncio
    async def test_over_limit_names_the_bound(self, web: Harness):
        """Refused, not clamped -- a silently clamped limit returns the wrong
        page while claiming success (see TestMalformedInput)."""
        resp = await web.client.get(f"/api/jobs?limit={MAX_PAGE_LIMIT + 1}")
        assert resp.status == 400
        assert (await resp.json())["error"] == (
            f"Invalid limit: {MAX_PAGE_LIMIT + 1} is out of range [0, {MAX_PAGE_LIMIT}]"
        )

    @pytest.mark.asyncio
    async def test_the_metrics_window_is_days_not_centuries(self, web: Harness):
        """A century-wide window makes /api/metrics read the whole jorb
        table; 90 days is wider than any retention policy here."""
        assert MAX_SINCE_HOURS == 24 * 90

        resp = await web.client.get(f"/api/metrics?since_hours={MAX_SINCE_HOURS + 1}")
        assert resp.status == 400
        assert "out of range" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_per_job_trails_are_paged(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        """One job's history is not a bound: a durable machine parked for a
        month writes transitions and checkpoints without limit."""
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('app.jobs.Trail', '{}', $1, 100, 'queued')
                RETURNING id
                """,
                unique_queue,
            )
            # The insert above already produced one trigger-written history
            # row; add enough to page.
            await conn.execute(
                """
                INSERT INTO jorb_history (job_id, event, detail)
                SELECT $1, 'queued', '{}'::jsonb FROM generate_series(1, 9)
                """,
                job_id,
            )

        resp = await web.client.get(f"/api/jobs/{job_id}/history?limit=3")
        assert resp.status == 200
        assert len(await resp.json()) == 3

        resp = await web.client.get(f"/api/jobs/{job_id}/history")
        assert resp.status == 200
        assert len(await resp.json()) == 10

        resp = await web.client.get(f"/api/jobs/{job_id}/history?limit=0")
        assert resp.status == 200
        assert await resp.json() == []

    @pytest.mark.asyncio
    async def test_steps_are_paged(
        self, web: Harness, db_pool: asyncpg.Pool, unique_queue: str
    ):
        async with db_pool.acquire() as conn:
            job_id = await conn.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, prio, state)
                VALUES ('app.jobs.Steps', '{}', $1, 100, 'queued')
                RETURNING id
                """,
                unique_queue,
            )
            await conn.execute(
                """
                INSERT INTO jorb_step (job_id, step_seq, name, run_epoch, started)
                SELECT $1, g, 'step_' || g, 1, now() FROM generate_series(1, 5) g
                """,
                job_id,
            )

        resp = await web.client.get(f"/api/jobs/{job_id}/steps?limit=2")
        assert resp.status == 200
        assert len(await resp.json()) == 2

        resp = await web.client.get(f"/api/jobs/{job_id}/steps")
        assert resp.status == 200
        assert len(await resp.json()) == 5

    @pytest.mark.asyncio
    async def test_admin_pool_bounds_how_long_a_request_holds_a_connection(
        self, web: Harness
    ):
        """Ten anonymous slow requests must not be able to park the whole
        admin pool: an admin statement that cannot answer in 30 seconds is a
        bug or an attack, and either way loses its connection."""
        resp = await web.client.get("/api/jobs")
        await resp.read()
        assert resp.status == 200

        pool = web.server.pool
        assert pool is not None
        # The timeout every statement on this surface actually runs under,
        # read off a real pooled connection rather than off the constructor
        # argument -- db.create_pool wraps the call, and the point is that
        # the value survives the wrapping.
        async with pool.acquire() as conn:
            assert conn._config.command_timeout == POOL_COMMAND_TIMEOUT_SECONDS
