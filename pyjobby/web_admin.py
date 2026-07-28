#!/usr/bin/env python3
"""
Pyjobby Web Admin Interface

HTTP server providing web-based management interface using htmx.
Built on top of the admin API for clean separation.
"""

from __future__ import annotations

import asyncio
import contextlib
import html as html_mod
import json
import re
import signal
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import asyncpg  # type: ignore[import-untyped]
from aiohttp import web

from . import db
from .admin_api import AdminAPI
from .client import DEFAULT_PRIO_CEILING
from .monitor import DEFAULT_LIVENESS_GRACE_SECONDS

# =============================================================================
# Request parsing
#
# The admin surface is unauthenticated, so every path id and query parameter is
# hostile input. Parsing lives in the four helpers below rather than in the
# handlers: a handler added later inherits the bounds by using them.
#
# Policy: malformed or out-of-range input is a 400 with a JSON body naming the
# parameter. Nothing is silently clamped, because a clamped limit/offset
# silently returns the wrong page of results. "Does not exist" is always 404.
# =============================================================================

MAX_BIGINT = 2**63 - 1
# jorb_queue.name is free-form text and pausing an unknown queue inserts a row
# (see api_queue_pause), so the name an anonymous client can store is bounded.
MAX_QUEUE_NAME_LENGTH = 255
# `datetime.now(UTC) - timedelta(hours=N)` must stay inside datetime's range; a
# century of history is far more than any dashboard window asks for.
MAX_SINCE_HOURS = 24 * 365 * 100
# Window the Prometheus scrape computes its rates over. Short on purpose:
# rates exist to show a fleet falling behind, and a wide window averages the
# incident away -- five minutes surfaces it within a scrape or two while
# still smoothing the per-second jitter of an individual worker.
PROM_RATE_WINDOW_SECONDS = 300

# =============================================================================
# The statements /metrics issues against the job tables.
#
# They are constants rather than string literals inside the handler for one
# reason: tests/test_metrics_scrape_cost.py EXPLAINs these exact strings, so
# the plan it certifies is the plan a real scrape gets. A copy of the SQL
# living in a test drifts the first time someone edits the handler, and the
# way this endpoint fails is precisely by staying correct while getting
# slower.
#
# The rule every statement here obeys: its cost must depend on how much work
# is in flight or how much happened in the window -- never on how many jobs
# the installation has run since it was built. At a million jobs an hour with
# 30-day retention, "count everything" is hundreds of millions of rows, and
# Prometheus asks every 15 seconds.
# =============================================================================

# Live states only, and deliberately so: `queued`, `claimed`, `running` and
# `waiting` are all bounded by work in progress however big the table gets,
# while the terminal states are bounded by nothing at all.
#
# Written as a union rather than `state IN (...)` because a single predicate
# spanning four states matches none of the partial indexes and collapses
# straight back into a sequential scan. Each arm is its own index:
# jorb_claim_idx (index-only: queue is its leading column), jorb_inflight_idx,
# and jorb_waitfor_*_idx.
#
# WHY THE QUEUED ARM IS SPLIT IN TWO. A bare `state = 'queued'` predicate
# gives the planner a FILTER but no index CONDITION, so whether it is
# answered index-only or by reading the heap depends on the visibility map,
# which depends on when autovacuum last ran. That is not a plan, it is a coin
# flip, and it flipped: the same statement measured as an index-only scan on
# one run and as a full sequential scan of jorb on the next. Splitting on
# `run_after <= now()` gives BOTH halves a real index condition against
# jorb_claim_idx (queue, prio, run_after), so neither half can degrade.
#
# The split is also the more useful question, and it is exactly the one
# websocket_server.SNAPSHOT_SQL's first two arms already ask -- same
# predicates, same emitted state names, so the dashboard and the scrape
# cannot disagree about what "queued" means:
#
#   state="queued"     claimable RIGHT NOW. The number an operator pages on.
#   state="scheduled"  deliberately parked in the future (retry backoff,
#                      enqueue-at). Not a backlog, and it used to be counted
#                      as one.
PROM_SQL_LIVE_STATES = """
    SELECT queue, 'queued' AS state, COUNT(*) AS n
      FROM jorb WHERE state = 'queued' AND run_after <= now() GROUP BY queue
    UNION ALL
    SELECT queue, 'scheduled', COUNT(*)
      FROM jorb WHERE state = 'queued' AND run_after > now() GROUP BY queue
    UNION ALL
    SELECT queue, state::text, COUNT(*)
      FROM jorb WHERE state IN ('claimed', 'running')
     GROUP BY queue, state
    UNION ALL
    SELECT queue, 'waiting', COUNT(*)
      FROM jorb WHERE state = 'waiting' GROUP BY queue
     ORDER BY 1, 2
"""

# Terminal outcomes, over the window instead of over all of history. The
# predicate is written as COALESCE(finished, updated) because that is the
# expression `jorb_retention_idx` is built on -- for a job that reached a
# terminal state the two are the same instant, and matching the index
# expression is what keeps this off the heap of every job ever run.
PROM_SQL_TERMINAL_RECENT = """
    SELECT queue, state::text AS state, COUNT(*) AS n
      FROM jorb
     WHERE state IN ('finished', 'crashed', 'cancelled')
       AND COALESCE(finished, updated) >= now() - $1::interval
     GROUP BY queue, state
     ORDER BY queue, state
"""

# Attempt starts, from `jorb.started` (written by the claimed -> running
# transition) rather than from the matching jorb_history rows: history has no
# index on time, so a window over it is a scan of the largest table in the
# system. Rides jorb_started_idx.
PROM_SQL_STARTED_RECENT = """
    SELECT queue, COUNT(*) AS n
      FROM jorb
     WHERE started IS NOT NULL AND started >= now() - $1::interval
     GROUP BY queue
     ORDER BY queue
"""

# Duration quantiles. Same COALESCE(finished, updated) predicate for the same
# reason -- filtering on bare `finished` matches no index, because the one
# that exists is on the expression.
PROM_SQL_DURATION_QUANTILES = """
    SELECT queue,
           percentile_cont(ARRAY[0.5, 0.9, 0.99]) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (finished - started))
           ) AS quantiles
      FROM jorb
     WHERE state = 'finished'
       AND started IS NOT NULL
       AND COALESCE(finished, updated) >= now() - $1::interval
     GROUP BY queue
     ORDER BY queue
"""

# The one true cumulative counter on this endpoint, and the only O(1) source
# for one that exists without a schema change.
#
# A Prometheus counter promises it never decreases, and every rate() in every
# dashboard is built on that promise. Counting rows cannot keep it: retention
# deletes the rows, the recount drops, and rate() reads the drop as a counter
# reset and loses the window's traffic. A sequence is immune -- deleting rows
# does not un-issue their ids.
PROM_SQL_ENQUEUED_TOTAL = """
    SELECT COALESCE(
        pg_sequence_last_value(pg_get_serial_sequence('jorb', 'id')), 0
    ) AS n
"""

PROM_SQL_QUEUE_PAUSED = "SELECT name, paused FROM jorb_queue ORDER BY name"

# liveness judged by THE threshold (monitor.DEFAULT_LIVENESS_GRACE_SECONDS),
# interpolated once at import — a literal here drifted from the monitor's
# flag and called live workers dead
PROM_SQL_WORKERS_LIVE = f"""
    SELECT COUNT(*) FROM jorb_worker
     WHERE shutdown_at IS NULL
       AND last_seen > now() - interval '{int(DEFAULT_LIVENESS_GRACE_SECONDS)} seconds'
"""

_ID_RE = re.compile(r"^[0-9]+$")
_INT_RE = re.compile(r"^[+-]?[0-9]+$")


def _api_error(status_cls: type[web.HTTPException], message: str) -> web.HTTPException:
    """Build an HTTP error whose body matches the handlers' JSON error shape."""
    return status_cls(
        text=json.dumps({"error": message}), content_type="application/json"
    )


def _path_id(request: web.Request, name: str) -> int:
    """Parse a row id out of the path, or raise 400.

    Ids are decimal digits inside the bigint range: anything else (``abc``,
    ``1.5``, ``-1``, ``1_0``, 2**63) is malformed input that must never reach
    ``int()`` or a bigint bind parameter.
    """
    raw = request.match_info[name]
    # MAX_BIGINT is 19 digits, so a longer string is out of range by
    # inspection — and never reaches int() (which refuses huge literals).
    if not _ID_RE.match(raw) or len(raw) > 19 or int(raw) > MAX_BIGINT:
        raise _api_error(
            web.HTTPBadRequest,
            f"Malformed {name}: {raw!r} is not a valid id",
        )
    return int(raw)


def _query_int(
    request: web.Request,
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = MAX_BIGINT,
) -> int:
    """Parse an integer query parameter, or raise 400.

    A missing or empty parameter yields ``default``. Non-integers and values
    outside ``[minimum, maximum]`` are rejected rather than clamped.
    """
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    if not _INT_RE.match(raw):
        raise _api_error(
            web.HTTPBadRequest, f"Invalid {name}: {raw!r} is not an integer"
        )
    try:
        value = int(raw)
    except ValueError:  # absurdly long digit strings
        raise _api_error(
            web.HTTPBadRequest, f"Invalid {name}: {raw[:32]!r} is not an integer"
        ) from None
    if not minimum <= value <= maximum:
        raise _api_error(
            web.HTTPBadRequest,
            f"Invalid {name}: {value} is out of range [{minimum}, {maximum}]",
        )
    return value


def _query_job_state(request: web.Request) -> str | None:
    """Parse the ``state`` filter against the JobState enum, or raise 400.

    Without this an unknown state reaches PostgreSQL as a ``jorbstate`` cast
    and the InvalidTextRepresentation error escapes as a 500.
    """
    raw = request.query.get("state")
    if raw is None or raw == "":
        return None
    try:
        return str(db.JobState(raw))
    except ValueError:
        valid = ", ".join(s.value for s in db.JobState)
        raise _api_error(
            web.HTTPBadRequest, f"Invalid state: {raw!r} (expected one of: {valid})"
        ) from None


def _path_queue_name(request: web.Request) -> str:
    """Parse the ``queue`` path segment, or raise 400 if it is over-long."""
    queue = request.match_info["queue"]
    if len(queue) > MAX_QUEUE_NAME_LENGTH:
        raise _api_error(
            web.HTTPBadRequest,
            f"Invalid queue name: longer than {MAX_QUEUE_NAME_LENGTH} characters",
        )
    return queue


class WebAdminServer:
    """
    Web-based administration interface for pyjobby.

    Provides REST API endpoints and HTML interface for managing jobs, queues, and workers.
    Uses htmx for dynamic updates without full page reloads.
    """

    def __init__(
        self,
        db_params: dict,
        host: str = "127.0.0.1",
        port: int = 8081,
        prio_ceiling: int = DEFAULT_PRIO_CEILING,
    ):
        """
        Initialize web admin server.

        Args:
            db_params: Database connection parameters
            host: Host to bind to (default: 127.0.0.1)
            port: Port to listen on (default: 8081)
            prio_ceiling: the priority ceiling this fleet's workers run with
                (`pj --max-prio`, default 1000). Handed to every AdminAPI
                this server builds, so the schedule form cannot create a
                schedule whose every firing mints an unclaimable job.
        """
        self.db_params = db_params
        self.host = host
        self.port = port
        self.prio_ceiling = prio_ceiling
        self.pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self.app = web.Application()
        self.app.on_cleanup.append(self._on_cleanup)
        self.setup_routes()

    def setup_routes(self) -> None:
        """Setup HTTP routes"""
        # HTML pages
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/jobs", self.jobs_page)
        self.app.router.add_get("/queues", self.queues_page)
        self.app.router.add_get("/workers", self.workers_page)
        self.app.router.add_get("/dlq", self.dlq_page)
        # Prometheus text exposition (not an HTML page)
        self.app.router.add_get("/metrics", self.metrics_prometheus)
        self.app.router.add_get("/schedules", self.schedules_page)

        # API endpoints for htmx
        self.app.router.add_get("/api/jobs", self.api_jobs_list)
        self.app.router.add_get("/api/jobs/{job_id}", self.api_job_get)
        self.app.router.add_get("/api/jobs/{job_id}/history", self.api_job_history)
        self.app.router.add_get("/api/jobs/{job_id}/steps", self.api_job_steps)
        self.app.router.add_post("/api/jobs/{job_id}/retry", self.api_job_retry)
        self.app.router.add_post("/api/jobs/{job_id}/cancel", self.api_job_cancel)
        self.app.router.add_delete("/api/jobs/{job_id}", self.api_job_delete)

        self.app.router.add_get("/api/queues", self.api_queues_list)
        self.app.router.add_get("/api/queues/{queue}/stats", self.api_queue_stats)
        self.app.router.add_post("/api/queues/{queue}/pause", self.api_queue_pause)
        self.app.router.add_post("/api/queues/{queue}/resume", self.api_queue_resume)

        self.app.router.add_get("/api/workers", self.api_workers_list)
        self.app.router.add_get("/api/workers/stats", self.api_workers_stats)

        self.app.router.add_get("/api/dlq", self.api_dlq_list)
        self.app.router.add_post("/api/dlq/{job_id}/retry", self.api_dlq_retry)

        self.app.router.add_get("/api/metrics", self.api_metrics)

        # Schedule management endpoints
        self.app.router.add_get("/api/schedules", self.api_schedules_list)
        self.app.router.add_get("/api/schedules/{schedule_id}", self.api_schedule_get)
        self.app.router.add_post("/api/schedules", self.api_schedule_create)
        self.app.router.add_post(
            "/api/schedules/{schedule_id}/enable", self.api_schedule_enable
        )
        self.app.router.add_post(
            "/api/schedules/{schedule_id}/disable", self.api_schedule_disable
        )
        self.app.router.add_delete(
            "/api/schedules/{schedule_id}", self.api_schedule_delete
        )
        self.app.router.add_get(
            "/api/schedules/{schedule_id}/history", self.api_schedule_history
        )

    async def _get_pool(self) -> asyncpg.Pool:
        """Lazily create the shared asyncpg connection pool."""
        if self.pool is None:
            async with self._pool_lock:
                if self.pool is None:
                    self.pool = await db.create_pool(
                        **self.db_params, min_size=1, max_size=10
                    )
        return self.pool

    @asynccontextmanager
    async def api(self) -> AsyncIterator[AdminAPI]:
        """Acquire a pooled connection wrapped in an AdminAPI for one request."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            yield AdminAPI(conn, prio_ceiling=self.prio_ceiling)

    @staticmethod
    async def _job_or_404(api: AdminAPI, job_id: int) -> dict[str, Any]:
        """Return the job row, or raise 404.

        Every handler that acts on a job id checks existence here, so "no such
        job" is a 404 on every route (400 is reserved for malformed input).
        """
        job = await api.get_job(job_id)
        if not job:
            raise _api_error(web.HTTPNotFound, f"Job {job_id} not found")
        return job

    @staticmethod
    async def _schedule_or_404(api: AdminAPI, schedule_id: int) -> dict[str, Any]:
        """Return the schedule row, or raise 404."""
        schedule = await api.get_schedule(schedule_id=schedule_id)
        if not schedule:
            raise _api_error(web.HTTPNotFound, f"Schedule {schedule_id} not found")
        return schedule

    async def close(self) -> None:
        """Close the connection pool (if it was created)."""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def _on_cleanup(self, app: web.Application) -> None:
        """aiohttp on_cleanup hook: release the pool on shutdown."""
        await self.close()

    # =========================================================================
    # HTML Pages
    # =========================================================================

    async def index(self, request: web.Request) -> web.Response:
        """Dashboard index page"""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pyjobby Admin</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: #f5f5f5;
            color: #333;
        }

        .header {
            background: #2c3e50;
            color: white;
            padding: 1rem 2rem;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }

        .header h1 { font-size: 1.5rem; font-weight: 600; }

        .nav {
            background: white;
            border-bottom: 1px solid #ddd;
            padding: 0 2rem;
            display: flex;
            gap: 0;
        }

        .nav a {
            padding: 1rem 1.5rem;
            text-decoration: none;
            color: #555;
            border-bottom: 3px solid transparent;
            transition: all 0.2s;
        }

        .nav a:hover { background: #f8f8f8; color: #2c3e50; }
        .nav a.active { color: #2c3e50; border-bottom-color: #3498db; font-weight: 600; }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }

        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }

        .card {
            background: white;
            border-radius: 8px;
            padding: 1.5rem;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }

        .card h2 {
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: #888;
            margin-bottom: 0.5rem;
        }

        .stat-value {
            font-size: 2.5rem;
            font-weight: 700;
            color: #2c3e50;
        }

        .stat-label {
            color: #888;
            font-size: 0.9rem;
            margin-top: 0.25rem;
        }

        .stats-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 1rem;
            margin-top: 1rem;
        }

        .stat-item {
            display: flex;
            justify-content: space-between;
            padding: 0.5rem 0;
            border-bottom: 1px solid #eee;
        }

        .stat-item:last-child { border-bottom: none; }

        .badge {
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
            font-size: 0.85rem;
            font-weight: 600;
        }

        .badge.queued { background: #e3f2fd; color: #1976d2; }
        .badge.running { background: #fff3e0; color: #f57c00; }
        .badge.finished { background: #e8f5e9; color: #388e3c; }
        .badge.crashed { background: #ffebee; color: #d32f2f; }
        .badge.waiting { background: #f3e5f5; color: #7b1fa2; }

        .loading {
            text-align: center;
            padding: 2rem;
            color: #888;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>📊 Pyjobby Administration</h1>
    </div>

    <div class="nav">
        <a href="/" class="active">Dashboard</a>
        <a href="/jobs">Jobs</a>
        <a href="/queues">Queues</a>
        <a href="/workers">Workers</a>
        <a href="/dlq">Dead Letter Queue</a>
        <a href="/metrics">Metrics</a>
    </div>

    <div class="container">
        <div class="dashboard-grid">
            <div class="card">
                <h2>Queue Statistics</h2>
                <div hx-get="/api/queues?format=html" hx-trigger="load, every 5s" hx-swap="innerHTML">
                    <div class="loading">Loading...</div>
                </div>
            </div>

            <div class="card">
                <h2>Active Workers</h2>
                <div hx-get="/api/workers/stats?format=html" hx-trigger="load, every 5s" hx-swap="innerHTML">
                    <div class="loading">Loading...</div>
                </div>
            </div>

            <div class="card">
                <h2>Recent Activity (24h)</h2>
                <div hx-get="/api/metrics?since_hours=24&format=html" hx-trigger="load, every 10s" hx-swap="innerHTML">
                    <div class="loading">Loading...</div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>Recent Jobs</h2>
            <div hx-get="/api/jobs?limit=10&format=html" hx-trigger="load, every 5s" hx-swap="innerHTML">
                <div class="loading">Loading jobs...</div>
            </div>
        </div>
    </div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def jobs_page(self, request: web.Request) -> web.Response:
        """Jobs management page"""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Jobs - Pyjobby Admin</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <link rel="stylesheet" href="/static/admin.css">
</head>
<body>
    <div class="header">
        <h1>📊 Pyjobby Administration</h1>
    </div>

    <div class="nav">
        <a href="/">Dashboard</a>
        <a href="/jobs" class="active">Jobs</a>
        <a href="/queues">Queues</a>
        <a href="/workers">Workers</a>
        <a href="/dlq">Dead Letter Queue</a>
        <a href="/metrics">Metrics</a>
    </div>

    <div class="container">
        <h1>Job Management</h1>
        <div hx-get="/api/jobs?format=html" hx-trigger="load" hx-swap="innerHTML">
            Loading jobs...
        </div>
    </div>
</body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    def _page(self, title: str, active: str, body: str) -> str:
        """Render a simple admin page with shared nav around `body`."""
        nav_links = [
            ("/", "Dashboard"),
            ("/jobs", "Jobs"),
            ("/queues", "Queues"),
            ("/workers", "Workers"),
            ("/dlq", "Dead Letter Queue"),
            ("/schedules", "Schedules"),
        ]
        nav = ""
        for href, label in nav_links:
            cls = ' class="active"' if href == active else ""
            nav += f'<a href="{href}"{cls}>{label}</a>'

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html_mod.escape(title)} - Pyjobby Admin</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; }}
        .header {{ background: #2c3e50; color: white; padding: 1rem 2rem; }}
        .nav {{ background: white; border-bottom: 1px solid #ddd; padding: 0 2rem; display: flex; }}
        .nav a {{ padding: 1rem 1.5rem; text-decoration: none; color: #555; border-bottom: 3px solid transparent; }}
        .nav a.active {{ color: #2c3e50; border-bottom-color: #3498db; font-weight: 600; }}
        .container {{ max-width: 1400px; margin: 0 auto; padding: 2rem; }}
        table {{ width: 100%; background: white; border-collapse: collapse; }}
        th, td {{ padding: 0.75rem; border-bottom: 1px solid #eee; text-align: left; }}
        .badge {{ padding: 0.25rem 0.75rem; border-radius: 12px; font-size: 0.85rem; font-weight: 600; }}
        .badge.queued {{ background: #e3f2fd; color: #1976d2; }}
        .badge.running {{ background: #fff3e0; color: #f57c00; }}
        .badge.finished {{ background: #e8f5e9; color: #388e3c; }}
        .badge.crashed {{ background: #ffebee; color: #d32f2f; }}
        .badge.paused {{ background: #fff3cd; color: #856404; }}
        .badge.live {{ background: #e8f5e9; color: #388e3c; }}
        .badge.dead {{ background: #eceff1; color: #546e7a; }}
        .btn {{ background: #3498db; color: white; border: none; padding: 0.4rem 0.9rem; border-radius: 4px; cursor: pointer; }}
        .btn-danger {{ background: #e74c3c; }}
        .btn-success {{ background: #27ae60; }}
    </style>
</head>
<body>
    <div class="header"><h1>📊 Pyjobby Administration</h1></div>
    <div class="nav">{nav}</div>
    <div class="container">{body}</div>
</body>
</html>"""

    async def queues_page(self, request: web.Request) -> web.Response:
        """Queues management page: depths plus pause/resume controls."""
        body = """
        <h1>Queue Management</h1>
        <p style="color: #888; margin: 0.5rem 0 1rem;">
            Paused queues stop being claimed immediately; limits are enforced
            live by the worker claim statement.
        </p>
        <div id="queues-table" hx-get="/api/queues?format=html"
             hx-trigger="load, every 5s" hx-swap="innerHTML">
            Loading queues...
        </div>"""
        return web.Response(
            text=self._page("Queues", "/queues", body), content_type="text/html"
        )

    async def workers_page(self, request: web.Request) -> web.Response:
        """Workers page backed by the jorb_worker registry."""
        body = """
        <h1>Worker Registry</h1>
        <p style="color: #888; margin: 0.5rem 0 1rem;">
            A worker is live while it has not shut down and its heartbeat is
            recent; recently shut-down workers stay listed for an hour.
        </p>
        <div id="workers-table" hx-get="/api/workers?format=html"
             hx-trigger="load, every 5s" hx-swap="innerHTML">
            Loading workers...
        </div>"""
        return web.Response(
            text=self._page("Workers", "/workers", body), content_type="text/html"
        )

    async def dlq_page(self, request: web.Request) -> web.Response:
        """DLQ page: terminal crashed jobs (retries exhausted)."""
        body = """
        <h1>Dead Letter Queue</h1>
        <p style="color: #888; margin: 0.5rem 0 1rem;">
            Jobs in the terminal <span class="badge crashed">crashed</span>
            state: their retries are exhausted. Retrying requeues the
            <strong>same</strong> job row with a fresh error budget.
        </p>
        <div id="dlq-table" hx-get="/api/dlq?format=html"
             hx-trigger="load, every 10s" hx-swap="innerHTML">
            Loading dead letter queue...
        </div>"""
        return web.Response(
            text=self._page("Dead Letter Queue", "/dlq", body),
            content_type="text/html",
        )

    # =========================================================================
    # Prometheus metrics
    # =========================================================================

    @staticmethod
    def _prom_escape(value: str) -> str:
        """Escape a Prometheus label value (backslash, quote, newline)."""
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    async def metrics_prometheus(self, request: web.Request) -> web.Response:
        """Prometheus text exposition (text/plain; version 0.0.4)."""
        esc = self._prom_escape
        lines: list[str] = []
        window = timedelta(seconds=PROM_RATE_WINDOW_SECONDS)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            live = await conn.fetch(PROM_SQL_LIVE_STATES)
            lines.append(
                "# HELP pyjobby_jobs_by_state Jobs per queue currently in a "
                "LIVE state. The queued state is reported SPLIT: "
                'state="queued" is work claimable right now (run_after '
                'already due) and state="scheduled" is work parked for the '
                "future (retry backoff, enqueue-at) -- so queued means "
                "backlog rather than backlog plus everything deferred. The "
                'rest are state="claimed", "running" and "waiting". '
                "Terminal states are NOT reported here: their count is "
                "every job the installation has ever run and not yet aged "
                "out, which no index can bound and no scrape can afford -- "
                "see pyjobby_jobs_terminal_recent for terminal outcomes."
            )
            lines.append("# TYPE pyjobby_jobs_by_state gauge")
            for r in live:
                lines.append(
                    f'pyjobby_jobs_by_state{{queue="{esc(r["queue"])}",'
                    f'state="{esc(r["state"])}"}} {r["n"]}'
                )

            terminal = await conn.fetch(PROM_SQL_TERMINAL_RECENT, window)
            lines.append(
                f"# HELP pyjobby_jobs_terminal_recent Jobs per queue that "
                f"reached a terminal state (finished, crashed, cancelled) in "
                f"the last {PROM_RATE_WINDOW_SECONDS}s. A windowed count, so "
                f"a gauge: it is not cumulative and rate() on it is "
                f"meaningless."
            )
            lines.append("# TYPE pyjobby_jobs_terminal_recent gauge")
            for r in terminal:
                lines.append(
                    f'pyjobby_jobs_terminal_recent{{queue="{esc(r["queue"])}",'
                    f'state="{esc(r["state"])}"}} {r["n"]}'
                )

            started = await conn.fetch(PROM_SQL_STARTED_RECENT, window)
            lines.append(
                f"# HELP pyjobby_jobs_started_recent Jobs per queue whose "
                f"current attempt started in the last "
                f"{PROM_RATE_WINDOW_SECONDS}s, from jorb.started. A retry "
                f"overwrites started, so a job that retried inside the window "
                f"is counted once rather than once per attempt."
            )
            lines.append("# TYPE pyjobby_jobs_started_recent gauge")
            for r in started:
                lines.append(
                    f'pyjobby_jobs_started_recent{{queue="{esc(r["queue"])}"}} {r["n"]}'
                )

            enqueued_total = await conn.fetchval(PROM_SQL_ENQUEUED_TOTAL)
            lines.append(
                "# HELP pyjobby_jobs_enqueued_total Cumulative jobs ever "
                "enqueued, read from the job id sequence rather than counted. "
                "Counting cannot back a counter here: retention deletes jobs, "
                "so a recount falls and every rate() reads the fall as a "
                "counter reset. An upper bound -- ids burned by rolled-back "
                "enqueues are included -- and it resets only if the sequence "
                "itself is reset."
            )
            lines.append("# TYPE pyjobby_jobs_enqueued_total counter")
            lines.append(f"pyjobby_jobs_enqueued_total {enqueued_total}")

            # Everything below comes from one AdminAPI.get_metrics() call so
            # the scrape, the dashboard, and `pj-admin metrics` cannot drift
            # apart -- and so the queries stay the indexed ones documented
            # there. The window is short because these are rates: a 5-minute
            # window responds to a fleet falling behind within a scrape or
            # two, where the 24h default would average the incident away.
            api = AdminAPI(conn)
            m = await api.get_metrics(since=db.utcnow() - window)
            backlog = m["backlog"]

            lines.append(
                "# HELP pyjobby_queue_oldest_queued_seconds "
                "How long the oldest CLAIMABLE job in the queue has been "
                "ready and unclaimed (run_after gates claimability, so "
                "jobs scheduled for the future are excluded)."
            )
            lines.append("# TYPE pyjobby_queue_oldest_queued_seconds gauge")
            for qname, stats in backlog["per_queue"].items():
                lines.append(
                    f'pyjobby_queue_oldest_queued_seconds{{queue="{esc(qname)}"}}'
                    f" {stats['oldest_age_seconds']}"
                )

            lines.append(
                "# HELP pyjobby_backlog_depth Claimable jobs waiting per "
                "queue (state queued and run_after already due)."
            )
            lines.append("# TYPE pyjobby_backlog_depth gauge")
            for qname, stats in backlog["per_queue"].items():
                lines.append(
                    f'pyjobby_backlog_depth{{queue="{esc(qname)}"}} {stats["depth"]}'
                )

            paused = await conn.fetch(PROM_SQL_QUEUE_PAUSED)
            lines.append(
                "# HELP pyjobby_queue_paused Whether the queue is paused (1) or not (0)."
            )
            lines.append("# TYPE pyjobby_queue_paused gauge")
            for r in paused:
                lines.append(
                    f'pyjobby_queue_paused{{queue="{esc(r["name"])}"}}'
                    f" {1 if r['paused'] else 0}"
                )

            workers_live = await conn.fetchval(PROM_SQL_WORKERS_LIVE)
            lines.append(
                "# HELP pyjobby_workers_live Live workers "
                "(registered, not shut down, recent heartbeat)."
            )
            lines.append("# TYPE pyjobby_workers_live gauge")
            lines.append(f"pyjobby_workers_live {workers_live}")

            durations = await conn.fetch(PROM_SQL_DURATION_QUANTILES, window)
            lines.append(
                f"# HELP pyjobby_job_duration_seconds "
                f"Duration quantiles of jobs that finished in the last "
                f"{PROM_RATE_WINDOW_SECONDS}s."
            )
            lines.append("# TYPE pyjobby_job_duration_seconds gauge")
            for r in durations:
                for quantile, value in zip(
                    ("0.5", "0.9", "0.99"), r["quantiles"], strict=True
                ):
                    if value is None:
                        continue
                    lines.append(
                        f'pyjobby_job_duration_seconds{{queue="{esc(r["queue"])}",'
                        f'quantile="{quantile}"}} {value}'
                    )

            # Rates, all over the same PROM_RATE_WINDOW_SECONDS window so
            # they can be compared to each other directly. Gauges rather
            # than counters on purpose: these are computed from the job
            # table, not accumulated in the process, so they do not survive
            # a restart the way a counter must.
            inflight = m["inflight"]
            storage = m["storage"]
            for metric, help_text, value in (
                (
                    "pyjobby_throughput_jobs_per_second",
                    f"Jobs reaching a terminal state per second over the "
                    f"last {PROM_RATE_WINDOW_SECONDS}s. Compare against "
                    f"arrivals: sustained arrivals above this is the "
                    f"definition of falling behind.",
                    m["throughput_per_second"],
                ),
                (
                    "pyjobby_arrival_jobs_per_second",
                    f"Jobs created per second over the last "
                    f"{PROM_RATE_WINDOW_SECONDS}s.",
                    m["arrival_rate_per_second"],
                ),
                (
                    "pyjobby_retry_attempts_per_second",
                    f"Attempts beyond the first, per second, burned by jobs "
                    f"that completed in the last {PROM_RATE_WINDOW_SECONDS}s.",
                    m["retry_rate_per_second"],
                ),
                (
                    "pyjobby_dlq_jobs_per_second",
                    f"Jobs entering the dead letter queue (terminal "
                    f"'crashed') per second over the last "
                    f"{PROM_RATE_WINDOW_SECONDS}s.",
                    m["dlq_growth_per_second"],
                ),
                (
                    "pyjobby_jobs_inflight",
                    "Jobs currently held by a worker (claimed or running).",
                    inflight["inflight"],
                ),
                (
                    "pyjobby_jobs_stuck",
                    f"In-flight jobs with no state change for "
                    f"{inflight['stuck_after_seconds']:.0f}s: busy is not "
                    f"the same as wedged.",
                    inflight["stuck"],
                ),
                (
                    "pyjobby_inflight_oldest_age_seconds",
                    "Age of the longest-held in-flight job.",
                    inflight["oldest_age_seconds"],
                ),
                (
                    "pyjobby_workers_not_claiming",
                    "Live workers that are claiming nothing because abandoned "
                    "job threads fill their pool. They heartbeat normally, so "
                    "pyjobby_workers_live counts them as capacity and every "
                    "other signal here reads healthy -- this is the only one "
                    "that says the work is not being picked up. Caused by "
                    "synchronous jobs exceeding their deadline, whose threads "
                    "cannot be interrupted; alert on it above 0.",
                    m["job_threads"]["not_claiming"],
                ),
                (
                    "pyjobby_worker_job_threads_abandoned_max",
                    "Abandoned job threads on the worst-affected live worker: "
                    "the approach to pyjobby_workers_not_claiming. A worker "
                    "holding 7 of its 8 is one timed-out synchronous job away "
                    "from doing no work at all.",
                    m["job_threads"]["max_abandoned"],
                ),
                (
                    "pyjobby_notify_queue_usage_ratio",
                    "Fraction of PostgreSQL's shared async-NOTIFY queue in "
                    "use. At 1.0 every transaction issuing a NOTIFY fails, "
                    "which stops all enqueues and completions; the queue "
                    "drains only as fast as the slowest listener.",
                    m["notify_queue_usage"],
                ),
            ):
                lines.append(f"# HELP {metric} {help_text}")
                lines.append(f"# TYPE {metric} gauge")
                lines.append(f"{metric} {value}")

            # Footprint. At ~4M dead tuples an hour, whether autovacuum is
            # keeping up is a survival question rather than a curiosity.
            for metric, help_text, key in (
                (
                    "pyjobby_table_total_bytes",
                    "Total on-disk size of the table including indexes and TOAST.",
                    "total_bytes",
                ),
                (
                    "pyjobby_table_bytes",
                    "On-disk size of the table's own data.",
                    "table_bytes",
                ),
                (
                    "pyjobby_table_index_bytes",
                    "On-disk size of the table's indexes.",
                    "index_bytes",
                ),
                (
                    "pyjobby_table_live_tuples",
                    "Live tuples estimated by the statistics collector.",
                    "live_tuples",
                ),
                (
                    "pyjobby_table_dead_tuples",
                    "Dead tuples awaiting vacuum.",
                    "dead_tuples",
                ),
                (
                    "pyjobby_table_dead_tuple_ratio",
                    "Dead tuples as a fraction of all tuples: a ratio that "
                    "climbs and stays there means autovacuum is losing.",
                    "dead_tuple_ratio",
                ),
            ):
                lines.append(f"# HELP {metric} {help_text}")
                lines.append(f"# TYPE {metric} gauge")
                for tname, stats in sorted(storage["tables"].items()):
                    lines.append(f'{metric}{{table="{esc(tname)}"}} {stats[key]}')

        body = "\n".join(lines) + "\n"
        return web.Response(
            body=body.encode("utf-8"),
            headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
        )

    # =========================================================================
    # API Endpoints
    # =========================================================================

    async def api_jobs_list(self, request: web.Request) -> web.Response:
        """List jobs (JSON or HTML)"""
        queue = request.query.get("queue")
        state = _query_job_state(request)
        limit = _query_int(request, "limit", 50)
        offset = _query_int(request, "offset", 0)
        async with self.api() as api:
            format_type = request.query.get("format", "json")

            jobs = await api.list_jobs(
                queue=queue, state=state, limit=limit, offset=offset
            )

            if format_type == "html":
                # Return HTML fragment for htmx
                if not jobs:
                    html = '<p style="padding: 1rem; color: #888;">No jobs found</p>'
                else:
                    html = '<table style="width: 100%; border-collapse: collapse;">'
                    html += '<thead><tr style="border-bottom: 2px solid #ddd; text-align: left;">'
                    html += '<th style="padding: 0.75rem;">ID</th>'
                    html += '<th style="padding: 0.75rem;">State</th>'
                    html += '<th style="padding: 0.75rem;">Queue</th>'
                    html += '<th style="padding: 0.75rem;">Job Class</th>'
                    html += '<th style="padding: 0.75rem;">Created</th>'
                    html += '<th style="padding: 0.75rem;">Details</th>'
                    html += "</tr></thead><tbody>"

                    for job in jobs:
                        created = html_mod.escape(
                            job["created"][:19] if job["created"] else ""
                        )
                        job_state = html_mod.escape(str(job["state"]))
                        job_queue = html_mod.escape(str(job["queue"]))
                        job_class = html_mod.escape(str(job["job_class"]))
                        job_id = int(job["id"])
                        html += '<tr style="border-bottom: 1px solid #eee;">'
                        html += f'<td style="padding: 0.75rem;">{job_id}</td>'
                        html += f'<td style="padding: 0.75rem;"><span class="badge {job_state}">{job_state}</span></td>'
                        html += f'<td style="padding: 0.75rem;">{job_queue}</td>'
                        html += f'<td style="padding: 0.75rem;">{job_class}</td>'
                        html += f'<td style="padding: 0.75rem;">{created}</td>'
                        html += (
                            f'<td style="padding: 0.75rem;">'
                            f'<a href="/api/jobs/{job_id}/history">history</a> | '
                            f'<a href="/api/jobs/{job_id}/steps">steps</a></td>'
                        )
                        html += "</tr>"

                    html += "</tbody></table>"

                return web.Response(text=html, content_type="text/html")
            else:
                return web.json_response(jobs)

    async def api_job_get(self, request: web.Request) -> web.Response:
        """Get single job"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            return web.json_response(await self._job_or_404(api, job_id))

    async def api_job_history(self, request: web.Request) -> web.Response:
        """Get a job's full transition history (jorb_history, oldest first)"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            history = await api.get_job_history(job_id)
            return web.json_response(history)

    async def api_job_steps(self, request: web.Request) -> web.Response:
        """Get a job's DXE step checkpoints (jorb_step, in sequence order)"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            steps = await api.get_job_steps(job_id)
            return web.json_response(steps)

    async def api_job_retry(self, request: web.Request) -> web.Response:
        """Retry a job (404 if it does not exist, 400 if its state forbids it)"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            try:
                result = await api.retry_job(job_id)
                return web.json_response(result)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

    async def api_job_cancel(self, request: web.Request) -> web.Response:
        """Cancel a job (404 if it does not exist, 400 if it is terminal)"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            try:
                result = await api.cancel_job(job_id)
                return web.json_response(result)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

    async def api_job_delete(self, request: web.Request) -> web.Response:
        """Delete a job"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            deleted = await api.delete_job(job_id)
            if deleted:
                return web.json_response({"status": "deleted", "job_id": job_id})
            raise _api_error(web.HTTPNotFound, f"Job {job_id} not found")

    def _render_queues_table(self, stats: list[dict[str, Any]]) -> str:
        """Render queue stats + control plane as an HTML fragment."""
        if not stats:
            return '<p style="padding: 1rem; color: #888;">No queue data</p>'

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += "<thead><tr>"
        for col in (
            "Queue",
            "Queued",
            "Running",
            "Crashed",
            "Status",
            "Max Concurrency",
            "Rate Limit",
            "Actions",
        ):
            html += f'<th style="padding: 0.75rem; text-align: left;">{col}</th>'
        html += "</tr></thead><tbody>"

        for s in stats:
            queue_name = str(s["queue"])
            queue_html = html_mod.escape(queue_name)
            queue_url = urllib.parse.quote(queue_name, safe="")
            paused = bool(s.get("paused"))
            status = (
                '<span class="badge paused">paused</span>'
                if paused
                else '<span class="badge running">active</span>'
            )
            max_conc = s.get("max_concurrency")
            rate = s.get("rate_limit")
            rate_html = (
                f"{int(rate)}/{s.get('rate_period_seconds', 60):g}s"
                if rate is not None
                else "unlimited"
            )
            if paused:
                action = (
                    f'<button class="btn btn-success" '
                    f'hx-post="/api/queues/{queue_url}/resume?format=html" '
                    f'hx-target="#queues-table" hx-swap="innerHTML">Resume</button>'
                )
            else:
                action = (
                    f'<button class="btn btn-danger" '
                    f'hx-post="/api/queues/{queue_url}/pause?format=html" '
                    f'hx-target="#queues-table" hx-swap="innerHTML">Pause</button>'
                )

            html += "<tr>"
            html += f'<td style="padding: 0.75rem;"><strong>{queue_html}</strong></td>'
            html += f'<td style="padding: 0.75rem;">{int(s["queued"])}</td>'
            html += f'<td style="padding: 0.75rem;">{int(s["running"])}</td>'
            html += f'<td style="padding: 0.75rem;">{int(s["crashed"])}</td>'
            html += f'<td style="padding: 0.75rem;">{status}</td>'
            html += (
                f'<td style="padding: 0.75rem;">'
                f"{int(max_conc) if max_conc is not None else 'unlimited'}</td>"
            )
            html += f'<td style="padding: 0.75rem;">{rate_html}</td>'
            html += f'<td style="padding: 0.75rem;">{action}</td>'
            html += "</tr>"

        html += "</tbody></table>"
        return html

    async def api_queues_list(self, request: web.Request) -> web.Response:
        """List queue statistics (with paused/limit control-plane columns)"""
        async with self.api() as api:
            stats = await api.queue_stats()
            format_type = request.query.get("format", "json")

            if format_type == "html":
                html = self._render_queues_table(stats)
                return web.Response(text=html, content_type="text/html")
            else:
                # oldest_queued_age_seconds arrives as Decimal from EXTRACT()
                return web.json_response(
                    stats, dumps=lambda x: json.dumps(x, default=float)
                )

    async def api_queue_stats(self, request: web.Request) -> web.Response:
        """Get stats for specific queue"""
        queue = _path_queue_name(request)
        async with self.api() as api:
            stats = await api.queue_stats(queue=queue)
            return web.json_response(
                stats, dumps=lambda x: json.dumps(x, default=float)
            )

    async def api_queue_pause(self, request: web.Request) -> web.Response:
        """Pause a queue (workers stop claiming from it immediately).

        Queues are implicit — they exist because a job names them — so pausing
        a queue that has never been used is *deliberately* allowed: it is how
        an operator stops work before the first job is enqueued. The upsert
        therefore creates the jorb_queue control row on demand. Because this
        route is anonymous, the queue name is length-bounded
        (MAX_QUEUE_NAME_LENGTH) so pre-emptive pausing cannot be turned into
        unbounded row insertion.
        """
        queue = _path_queue_name(request)
        async with self.api() as api:
            control = await api.pause_queue(queue)
            if request.query.get("format") == "html":
                stats = await api.queue_stats()
                return web.Response(
                    text=self._render_queues_table(stats), content_type="text/html"
                )
            return web.json_response(control)

    async def api_queue_resume(self, request: web.Request) -> web.Response:
        """Resume a paused queue (creates the control row if absent, exactly
        like api_queue_pause, and bounds the queue name the same way)"""
        queue = _path_queue_name(request)
        async with self.api() as api:
            control = await api.resume_queue(queue)
            if request.query.get("format") == "html":
                stats = await api.queue_stats()
                return web.Response(
                    text=self._render_queues_table(stats), content_type="text/html"
                )
            return web.json_response(control)

    async def api_workers_list(self, request: web.Request) -> web.Response:
        """List workers from the jorb_worker registry"""
        async with self.api() as api:
            workers = await api.list_workers()
            format_type = request.query.get("format", "json")

            if format_type != "html":
                return web.json_response(workers)

            if not workers:
                html = (
                    '<p style="padding: 1rem; color: #888;">No workers registered</p>'
                )
                return web.Response(text=html, content_type="text/html")

            html = '<table style="width: 100%; border-collapse: collapse;">'
            html += "<thead><tr>"
            for col in (
                "ID",
                "Host",
                "PID",
                "Queue",
                "Status",
                "Job Threads",
                "Last Seen",
                "Current Job",
            ):
                html += f'<th style="padding: 0.75rem; text-align: left;">{col}</th>'
            html += "</tr></thead><tbody>"
            for w in workers:
                if w["shutdown_at"] is not None:
                    status = '<span class="badge dead">shut down</span>'
                elif w["not_claiming"]:
                    # live, beating, and doing nothing: abandoned threads fill
                    # its pool, so a "live" badge here would be a lie
                    status = '<span class="badge crashed">not claiming</span>'
                elif w["live"]:
                    status = '<span class="badge live">live</span>'
                else:
                    status = '<span class="badge paused">stale</span>'
                threads = (
                    f"{int(w['job_threads_abandoned'])} abandoned "
                    f"/ {int(w['job_threads'])}"
                    if w["job_threads"]
                    else "-"
                )
                age = w.get("last_seen_age_seconds")
                age_html = f"{age:.0f}s ago" if age is not None else "-"
                if w.get("current_job_id") is not None:
                    current = (
                        f"#{int(w['current_job_id'])} "
                        f"{html_mod.escape(str(w['current_job_class']))} "
                        f"({html_mod.escape(str(w['current_job_state']))})"
                    )
                else:
                    current = "-"
                html += "<tr>"
                html += f'<td style="padding: 0.75rem;">{int(w["id"])}</td>'
                html += f'<td style="padding: 0.75rem;">{html_mod.escape(str(w["host"]))}</td>'
                html += f'<td style="padding: 0.75rem;">{int(w["pid"])}</td>'
                html += f'<td style="padding: 0.75rem;">{html_mod.escape(str(w["queue"]))}</td>'
                html += f'<td style="padding: 0.75rem;">{status}</td>'
                html += f'<td style="padding: 0.75rem;">{threads}</td>'
                html += f'<td style="padding: 0.75rem;">{age_html}</td>'
                html += f'<td style="padding: 0.75rem;">{current}</td>'
                html += "</tr>"
            html += "</tbody></table>"
            return web.Response(text=html, content_type="text/html")

    async def api_workers_stats(self, request: web.Request) -> web.Response:
        """Get worker statistics"""
        async with self.api() as api:
            stats = await api.worker_stats()
            format_type = request.query.get("format", "json")

            if format_type == "html":
                html = f'<div class="stat-value">{int(stats["live_workers"])}</div>'
                html += '<div class="stat-label">Active Workers</div>'
                return web.Response(text=html, content_type="text/html")
            else:
                return web.json_response(stats)

    def _render_dlq_table(self, jobs: list[dict[str, Any]]) -> str:
        """Render terminal crashed (DLQ) jobs as an HTML fragment."""
        if not jobs:
            return (
                '<p style="padding: 1rem; color: #888;">'
                "Dead letter queue is empty — no crashed jobs.</p>"
            )

        html = '<table style="width: 100%; border-collapse: collapse;">'
        html += "<thead><tr>"
        for col in ("ID", "Queue", "Job Class", "Errors", "Last Error", "Actions"):
            html += f'<th style="padding: 0.75rem; text-align: left;">{col}</th>'
        html += "</tr></thead><tbody>"
        for job in jobs:
            job_id = int(job["id"])
            error = str(job.get("error_message") or "")
            if len(error) > 120:
                error = error[:120] + "…"
            html += "<tr>"
            html += f'<td style="padding: 0.75rem;">{job_id}</td>'
            html += f'<td style="padding: 0.75rem;">{html_mod.escape(str(job["queue"]))}</td>'
            html += f'<td style="padding: 0.75rem;">{html_mod.escape(str(job["job_class"]))}</td>'
            html += f'<td style="padding: 0.75rem;">{int(job["error_count"])}</td>'
            html += f'<td style="padding: 0.75rem;"><code>{html_mod.escape(error)}</code></td>'
            html += (
                f'<td style="padding: 0.75rem;"><button class="btn btn-success" '
                f'hx-post="/api/dlq/{job_id}/retry?format=html" '
                f'hx-target="#dlq-table" hx-swap="innerHTML">Retry</button></td>'
            )
            html += "</tr>"
        html += "</tbody></table>"
        return html

    async def api_dlq_list(self, request: web.Request) -> web.Response:
        """List Dead Letter Queue jobs (terminal crashed state)"""
        limit = _query_int(request, "limit", 100)
        async with self.api() as api:
            jobs = await api.list_dlq(limit=limit)
            if request.query.get("format") == "html":
                return web.Response(
                    text=self._render_dlq_table(jobs), content_type="text/html"
                )
            return web.json_response(jobs)

    async def api_dlq_retry(self, request: web.Request) -> web.Response:
        """Retry job from DLQ (requeues the same row with errors reset).

        404 if the job does not exist, 400 if it exists but is not in the DLQ.
        """
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            try:
                result = await api.retry_from_dlq(job_id)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            if request.query.get("format") == "html":
                # Refresh the DLQ table for htmx buttons
                jobs = await api.list_dlq(limit=100)
                return web.Response(
                    text=self._render_dlq_table(jobs), content_type="text/html"
                )
            return web.json_response(result)

    async def api_metrics(self, request: web.Request) -> web.Response:
        """Get system metrics"""
        since_hours = _query_int(request, "since_hours", 24, maximum=MAX_SINCE_HOURS)
        async with self.api() as api:
            queue = request.query.get("queue")
            format_type = request.query.get("format", "json")

            # jorb timestamps are naive-UTC, so compare with a naive-UTC value
            since = datetime.now(UTC) - timedelta(hours=since_hours)
            metrics = await api.get_metrics(since=since, queue=queue)

            if format_type == "html":
                backlog = metrics["backlog"]
                inflight = metrics["inflight"]
                storage = metrics["storage"]
                # Throughput sits next to arrivals because the comparison is
                # the signal: either number alone says nothing about whether
                # the fleet is keeping up.
                html = '<div class="stats-grid">'
                html += '<div class="stat-item">'
                html += (
                    f"<span>Throughput</span>"
                    f"<span>{metrics['throughput_per_second']:.2f}/s</span>"
                )
                html += (
                    f"<span>Arrivals</span>"
                    f"<span>{metrics['arrival_rate_per_second']:.2f}/s</span>"
                )
                html += (
                    f"<span>Retry Pressure</span>"
                    f"<span>{metrics['retry_rate_per_second']:.2f}/s</span>"
                )
                html += (
                    f"<span>DLQ Growth</span>"
                    f"<span>{metrics['dlq_growth_per_second']:.4f}/s</span>"
                )
                html += "</div>"
                html += '<div class="stat-item">'
                html += f'<span>Finished</span><span class="badge finished">{int(metrics["finished_count"])}</span>'
                html += "</div>"
                html += '<div class="stat-item">'
                html += f'<span>Crashed</span><span class="badge crashed">{int(metrics["crashed_count"])}</span>'
                html += "</div>"
                html += '<div class="stat-item">'
                html += f"<span>Avg Duration</span><span>{metrics['avg_duration_seconds']:.2f}s</span>"
                html += f"<span>Avg Queue Wait</span><span>{metrics['avg_wait_seconds']:.2f}s</span>"
                html += f"<span>Max Queue Wait</span><span>{metrics['max_wait_seconds']:.2f}s</span>"
                html += "</div>"
                html += '<div class="stat-item">'
                html += f"<span>Backlog Depth</span><span>{backlog['depth']}</span>"
                html += (
                    f"<span>Oldest Ready</span>"
                    f"<span>{backlog['oldest_age_seconds']:.1f}s</span>"
                )
                html += f"<span>In Flight</span><span>{inflight['inflight']}</span>"
                html += f"<span>Stuck</span><span>{inflight['stuck']}</span>"
                html += "</div>"
                html += '<div class="stat-item">'
                html += (
                    f"<span>NOTIFY Queue</span>"
                    f"<span>{metrics['notify_queue_usage']:.1%}</span>"
                )
                html += (
                    f"<span>Dead Tuples</span>"
                    f"<span>{storage['dead_tuple_ratio']:.1%}</span>"
                )
                html += (
                    f"<span>Storage</span>"
                    f"<span>{storage['total_bytes'] / (1024 * 1024):.1f} MB</span>"
                )
                html += "</div>"
                html += "</div>"
                return web.Response(text=html, content_type="text/html")
            else:
                return web.json_response(metrics)

    # =========================================================================
    # Schedule Management Pages & API
    # =========================================================================

    async def schedules_page(self, request: web.Request) -> web.Response:
        """Schedules management page"""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Schedules - Pyjobby Admin</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f5f5;
        }

        .header {
            background: #2c3e50;
            color: white;
            padding: 1rem 2rem;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }

        .actions {
            margin-bottom: 2rem;
            display: flex;
            gap: 1rem;
            align-items: center;
        }

        .btn {
            background: #3498db;
            color: white;
            border: none;
            padding: 0.5rem 1rem;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }

        .btn:hover {
            background: #2980b9;
        }

        .btn-danger {
            background: #e74c3c;
        }

        .btn-danger:hover {
            background: #c0392b;
        }

        .btn-success {
            background: #27ae60;
        }

        .btn-success:hover {
            background: #229954;
        }

        table {
            width: 100%;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }

        th {
            background: #34495e;
            color: white;
            padding: 1rem;
            text-align: left;
            font-weight: 500;
        }

        td {
            padding: 1rem;
            border-bottom: 1px solid #ecf0f1;
        }

        tr:hover {
            background: #f8f9fa;
        }

        .badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 500;
        }

        .badge-enabled {
            background: #d4edda;
            color: #155724;
        }

        .badge-disabled {
            background: #f8d7da;
            color: #721c24;
        }

        .badge-success {
            background: #d4edda;
            color: #155724;
        }

        .badge-warning {
            background: #fff3cd;
            color: #856404;
        }

        .modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
        }

        .modal.active {
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .modal-content {
            background: white;
            padding: 2rem;
            border-radius: 8px;
            width: 90%;
            max-width: 600px;
            max-height: 90vh;
            overflow-y: auto;
        }

        .form-group {
            margin-bottom: 1rem;
        }

        .form-group label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: 500;
        }

        .form-group input,
        .form-group select,
        .form-group textarea {
            width: 100%;
            padding: 0.5rem;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 14px;
        }

        .form-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>Pyjobby Admin - Recurring Schedules</h1>
        <nav>
            <a href="/" style="color: white; margin-right: 1rem;">Dashboard</a>
            <a href="/jobs" style="color: white; margin-right: 1rem;">Jobs</a>
            <a href="/queues" style="color: white; margin-right: 1rem;">Queues</a>
            <a href="/schedules" style="color: #3498db; margin-right: 1rem;">Schedules</a>
        </nav>
    </div>

    <div class="container">
        <div class="actions">
            <button class="btn btn-success" onclick="showAddScheduleModal()">+ Add Schedule</button>
            <button class="btn" hx-get="/api/schedules?format=html" hx-target="#schedules-table" hx-swap="innerHTML">
                🔄 Refresh
            </button>
        </div>

        <div id="schedules-table" hx-get="/api/schedules?format=html" hx-trigger="load, every 10s" hx-swap="innerHTML">
            Loading schedules...
        </div>
    </div>

    <!-- Add Schedule Modal -->
    <div id="addScheduleModal" class="modal">
        <div class="modal-content">
            <h2>Create New Schedule</h2>
            <form hx-post="/api/schedules" hx-target="#schedules-table" hx-swap="innerHTML" onsubmit="closeAddScheduleModal()">
                <div class="form-group">
                    <label>Schedule Name *</label>
                    <input type="text" name="name" required placeholder="daily-cleanup">
                </div>

                <div class="form-group">
                    <label>Job Class *</label>
                    <input type="text" name="job_class" required placeholder="myapp.jobs.CleanupJob">
                </div>

                <div class="form-group">
                    <label>Cron Expression *</label>
                    <input type="text" name="cron_expr" required placeholder="0 2 * * *">
                    <small>Examples: "0 2 * * *" (2am daily), "0 * * * *" (hourly), "*/5 * * * *" (every 5 min)</small>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Queue</label>
                        <input type="text" name="queue" value="default">
                    </div>
                    <div class="form-group">
                        <label>Priority</label>
                        <!--PRIO_FIELD-->
                    </div>
                </div>

                <div class="form-group">
                    <label>Description</label>
                    <textarea name="description" rows="2" placeholder="What does this schedule do?"></textarea>
                </div>

                <h3 style="margin-top: 1.5rem; margin-bottom: 1rem;">Safety Features</h3>

                <div class="form-row">
                    <div class="form-group">
                        <label>Max Concurrent Jobs</label>
                        <input type="number" name="max_concurrent_jobs" value="1" min="1">
                    </div>
                    <div class="form-group">
                        <label>Jitter (seconds)</label>
                        <input type="number" name="jitter_seconds" value="0" min="0">
                    </div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Backpressure Threshold</label>
                        <input type="number" name="backpressure_threshold" value="1000" min="0">
                    </div>
                    <div class="form-group">
                        <label>Circuit Breaker Threshold</label>
                        <input type="number" name="circuit_breaker_threshold" value="5" min="1">
                    </div>
                </div>

                <div style="margin-top: 2rem; display: flex; gap: 1rem;">
                    <button type="submit" class="btn btn-success">Create Schedule</button>
                    <button type="button" class="btn" onclick="closeAddScheduleModal()">Cancel</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        function showAddScheduleModal() {
            document.getElementById('addScheduleModal').classList.add('active');
        }

        function closeAddScheduleModal() {
            document.getElementById('addScheduleModal').classList.remove('active');
        }

        window.onclick = function(event) {
            const modal = document.getElementById('addScheduleModal');
            if (event.target === modal) {
                closeAddScheduleModal();
            }
        }
    </script>
</body>
</html>"""
        # The priority field is built here rather than inlined above because
        # it has to carry a number this server was told (the fleet's ceiling)
        # into a page template that is otherwise a constant. The browser-side
        # `max` is a courtesy; POST /api/schedules refuses the value anyway.
        # The wording is the point: the ordering is inverted from everyone's
        # intuition, and that inversion is what mints the unclaimable job.
        html = html.replace(
            "<!--PRIO_FIELD-->",
            '<input type="number" name="prio" value="100" '
            f'max="{self.prio_ceiling}">\n'
            "                        <small>LOWER is MORE urgent. Above the "
            f"worker priority ceiling ({self.prio_ceiling}) no worker ever "
            f"claims the job.</small>",
        )
        return web.Response(text=html, content_type="text/html")

    def _render_schedules_table(self, schedules: list[dict[str, Any]]) -> str:
        """Render the schedules table HTML fragment (all values escaped)."""
        html = "<table><thead><tr>"
        html += "<th>Name</th><th>Status</th><th>Cron</th><th>Queue</th>"
        html += "<th>Next Run</th><th>Stats</th><th>Actions</th>"
        html += "</tr></thead><tbody>"

        for s in schedules:
            status_badge = "badge-enabled" if s["enabled"] else "badge-disabled"
            status_text = "Enabled" if s["enabled"] else "Disabled"

            success_rate = None
            if s["success_count"] + s["failure_count"] > 0:
                success_rate = (
                    s["success_count"] / (s["success_count"] + s["failure_count"])
                ) * 100

            name = html_mod.escape(str(s["name"]))
            description = html_mod.escape(str(s.get("description") or ""))
            cron_expr = html_mod.escape(str(s["cron_expr"]))
            queue = html_mod.escape(str(s["queue"]))

            html += "<tr>"
            html += f"<td><strong>{name}</strong><br><small>{description}</small></td>"
            html += f'<td><span class="badge {status_badge}">{status_text}</span></td>'
            html += f"<td><code>{cron_expr}</code></td>"
            html += f"<td>{queue}</td>"
            html += f"<td>{s['next_run'].strftime('%Y-%m-%d %H:%M') if s.get('next_run') else '-'}</td>"
            html += f"<td>{int(s['run_count'])} runs<br>"
            if success_rate is not None:
                rate_class = "badge-success" if success_rate >= 95 else "badge-warning"
                html += f'<span class="badge {rate_class}">{success_rate:.1f}% success</span>'
            html += "</td>"
            html += "<td>"

            schedule_id = int(s["id"])
            if s["enabled"]:
                html += f'<button class="btn btn-danger" hx-post="/api/schedules/{schedule_id}/disable" hx-target="#schedules-table" hx-swap="innerHTML">Disable</button>'
            else:
                html += f'<button class="btn btn-success" hx-post="/api/schedules/{schedule_id}/enable" hx-target="#schedules-table" hx-swap="innerHTML">Enable</button>'

            html += f' <button class="btn btn-danger" hx-delete="/api/schedules/{schedule_id}" hx-confirm="Delete schedule {name}?" hx-target="#schedules-table" hx-swap="innerHTML">Delete</button>'
            html += "</td>"
            html += "</tr>"

        html += "</tbody></table>"
        return html

    async def api_schedules_list(self, request: web.Request) -> web.Response:
        """List schedules (JSON or HTML)"""
        async with self.api() as api:
            format_type = request.query.get("format", "json")
            enabled = request.query.get("enabled")
            queue = request.query.get("queue")

            # Convert enabled to bool if provided
            enabled_bool = None
            if enabled:
                enabled_bool = enabled.lower() in ("true", "1", "yes")

            schedules = await api.list_schedules(
                enabled=enabled_bool, queue=queue or None
            )

            if format_type == "html":
                html = self._render_schedules_table(schedules)
                return web.Response(text=html, content_type="text/html")
            else:
                # JSON response
                return web.json_response(
                    schedules, dumps=lambda x: json.dumps(x, default=str)
                )

    async def api_schedule_get(self, request: web.Request) -> web.Response:
        """Get single schedule"""
        schedule_id = _path_id(request, "schedule_id")
        async with self.api() as api:
            schedule = await self._schedule_or_404(api, schedule_id)
            return web.json_response(
                schedule, dumps=lambda x: json.dumps(x, default=str)
            )

    async def api_schedule_create(self, request: web.Request) -> web.Response:
        """Create new schedule.

        400 for a missing/unparseable field, 409 when the name is taken.
        """
        data = await request.post()
        missing = [f for f in ("name", "job_class", "cron_expr") if not data.get(f)]
        if missing:
            raise _api_error(
                web.HTTPBadRequest,
                f"Missing required field(s): {', '.join(missing)}",
            )

        async with self.api() as api:
            try:
                await api.create_schedule(
                    name=cast(str, data["name"]),
                    job_class=cast(str, data["job_class"]),
                    cron_expr=cast(str, data["cron_expr"]),
                    queue=cast(str, data.get("queue", "default")),
                    prio=int(cast(str | int, data.get("prio", 100))),
                    description=cast(str | None, data.get("description")),
                    max_concurrent_jobs=int(
                        cast(str | int, data.get("max_concurrent_jobs", 1))
                    ),
                    jitter_seconds=int(cast(str | int, data.get("jitter_seconds", 0))),
                    backpressure_threshold=int(
                        cast(str | int, data.get("backpressure_threshold", 1000))
                    ),
                    circuit_breaker_threshold=int(
                        cast(str | int, data.get("circuit_breaker_threshold", 5))
                    ),
                )

                # Return refreshed schedules list as HTML
                schedules = await api.list_schedules()
                html = self._render_schedules_table(schedules)
                return web.Response(text=html, content_type="text/html")
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            except asyncpg.UniqueViolationError:
                return web.json_response(
                    {"error": f"Schedule {data['name']!r} already exists"}, status=409
                )

    async def api_schedule_enable(self, request: web.Request) -> web.Response:
        """Enable schedule (404 if it does not exist, like the sibling GET)"""
        schedule_id = _path_id(request, "schedule_id")
        async with self.api() as api:
            await self._schedule_or_404(api, schedule_id)
            await api.enable_schedule(schedule_id)

        # Return refreshed list
        return await self.api_schedules_list(request)

    async def api_schedule_disable(self, request: web.Request) -> web.Response:
        """Disable schedule (404 if it does not exist, like the sibling GET)"""
        schedule_id = _path_id(request, "schedule_id")
        async with self.api() as api:
            await self._schedule_or_404(api, schedule_id)
            await api.disable_schedule(schedule_id)

        # Return refreshed list
        return await self.api_schedules_list(request)

    async def api_schedule_delete(self, request: web.Request) -> web.Response:
        """Delete schedule (404 if it does not exist, like the sibling GET)"""
        schedule_id = _path_id(request, "schedule_id")
        async with self.api() as api:
            await self._schedule_or_404(api, schedule_id)
            await api.delete_schedule(schedule_id)

        # Return refreshed list
        return await self.api_schedules_list(request)

    async def api_schedule_history(self, request: web.Request) -> web.Response:
        """Get schedule execution history.

        Deliberately does not check that the schedule exists: an unknown id
        answers 200 with an empty log, the same as a schedule that has never
        run.
        """
        schedule_id = _path_id(request, "schedule_id")
        limit = _query_int(request, "limit", 50)
        async with self.api() as api:
            # Query directly: jorb_schedule_log is ordered by id (schema v1
            # has actual_time, not a 'created' column)
            records = await api.conn.fetch(
                """
                SELECT * FROM jorb_schedule_log
                WHERE schedule_id = $1
                ORDER BY id DESC
                LIMIT $2
                """,
                schedule_id,
                limit,
            )
            history = [dict(r) for r in records]
            return web.json_response(
                history, dumps=lambda x: json.dumps(x, default=str)
            )

    async def start(self) -> None:
        """Start the web server"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        print(f"🌐 Web admin running at http://{self.host}:{self.port}/")

        # SIGTERM/SIGINT set the stop event so the finally below actually
        # runs under systemd/Docker stop — the default SIGTERM disposition
        # kills the process with the pool still open.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        with contextlib.suppress(NotImplementedError):
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, stop.set)

        try:
            await stop.wait()
            print("\n👋 Shutting down...")
        finally:
            # Cleans up the app, which also closes the connection pool
            await runner.cleanup()


async def serve(
    db_params: dict,
    host: str,
    port: int,
    prio_ceiling: int = DEFAULT_PRIO_CEILING,
) -> None:
    """Create and run a WebAdminServer until interrupted."""
    server = WebAdminServer(db_params, host=host, port=port, prio_ceiling=prio_ceiling)
    await server.start()


def main() -> None:
    """CLI entry point for the web admin server."""
    import click

    @click.command()
    @click.option(
        "--config",
        "-c",
        default="./pyjobby.conf.py",
        show_default=True,
        help="Config file path (must define db_params; may define "
        "prio_ceiling) — the same -c/--config every other pyjobby daemon "
        "takes; this one used to be the odd positional argument out",
    )
    @click.option(
        "--host",
        default="127.0.0.1",
        show_default=True,
        help="Bind address (use 0.0.0.0 to expose; the admin UI has no authentication)",
    )
    @click.option("--port", default=8081, show_default=True, help="Bind port")
    @click.option(
        "--max-prio",
        default=None,
        type=int,
        help="The priority ceiling this fleet's workers run with (`pj "
        "--max-prio`). Schedules created here are refused above it: LOWER is "
        "MORE urgent, and a job above the ceiling is never claimed at all. "
        "Defaults to the config file's prio_ceiling, else 1000",
    )
    def cli(config: str, host: str, port: int, max_prio: int | None) -> None:
        """Run the pyjobby web admin interface."""
        from .configloader import load_config_from_file

        cfg = load_config_from_file(config, keys=["db_params", "prio_ceiling"])
        db_params = cfg.get("db_params")
        if not db_params:
            raise click.ClickException(f"No db_params found in config: {config}")
        if max_prio is None:
            max_prio = cfg.get("prio_ceiling") or DEFAULT_PRIO_CEILING

        asyncio.run(serve(db_params, host, port, max_prio))

    cli()


if __name__ == "__main__":
    main()
