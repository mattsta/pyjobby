#!/usr/bin/env python3
"""
Pyjobby Web Admin Interface

HTTP server providing web-based management interface using htmx.
Built on top of the admin API for clean separation.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import asyncpg  # type: ignore[import-untyped]
from aiohttp import web

from . import db
from .admin_api import AdminAPI


class WebAdminServer:
    """
    Web-based administration interface for pyjobby.

    Provides REST API endpoints and HTML interface for managing jobs, queues, and workers.
    Uses htmx for dynamic updates without full page reloads.
    """

    def __init__(self, db_params: dict, host: str = "127.0.0.1", port: int = 8081):
        """
        Initialize web admin server.

        Args:
            db_params: Database connection parameters
            host: Host to bind to (default: 127.0.0.1)
            port: Port to listen on (default: 8081)
        """
        self.db_params = db_params
        self.host = host
        self.port = port
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
        self.app.router.add_get("/metrics", self.metrics_page)
        self.app.router.add_get("/schedules", self.schedules_page)

        # API endpoints for htmx
        self.app.router.add_get("/api/jobs", self.api_jobs_list)
        self.app.router.add_get("/api/jobs/{job_id}", self.api_job_get)
        self.app.router.add_post("/api/jobs/{job_id}/retry", self.api_job_retry)
        self.app.router.add_post("/api/jobs/{job_id}/cancel", self.api_job_cancel)
        self.app.router.add_delete("/api/jobs/{job_id}", self.api_job_delete)

        self.app.router.add_get("/api/queues", self.api_queues_list)
        self.app.router.add_get("/api/queues/{queue}/stats", self.api_queue_stats)

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
            yield AdminAPI(conn)

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

    async def queues_page(self, request: web.Request) -> web.Response:
        """Queues management page - Placeholder"""
        return web.Response(
            text="<h1>Queues Page - Coming Soon</h1>", content_type="text/html"
        )

    async def workers_page(self, request: web.Request) -> web.Response:
        """Workers management page - Placeholder"""
        return web.Response(
            text="<h1>Workers Page - Coming Soon</h1>", content_type="text/html"
        )

    async def dlq_page(self, request: web.Request) -> web.Response:
        """DLQ management page - Placeholder"""
        return web.Response(
            text="<h1>DLQ Page - Coming Soon</h1>", content_type="text/html"
        )

    async def metrics_page(self, request: web.Request) -> web.Response:
        """Metrics page - Placeholder"""
        return web.Response(
            text="<h1>Metrics Page - Coming Soon</h1>", content_type="text/html"
        )

    # =========================================================================
    # API Endpoints
    # =========================================================================

    async def api_jobs_list(self, request: web.Request) -> web.Response:
        """List jobs (JSON or HTML)"""
        async with self.api() as api:
            # Parse query parameters
            queue = request.query.get("queue")
            state = request.query.get("state")
            limit = int(request.query.get("limit", 50))
            offset = int(request.query.get("offset", 0))
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
                    html += "</tr></thead><tbody>"

                    for job in jobs:
                        created = html_mod.escape(
                            job["created"][:19] if job["created"] else ""
                        )
                        job_state = html_mod.escape(str(job["state"]))
                        job_queue = html_mod.escape(str(job["queue"]))
                        job_class = html_mod.escape(str(job["job_class"]))
                        html += '<tr style="border-bottom: 1px solid #eee;">'
                        html += f'<td style="padding: 0.75rem;">{int(job["id"])}</td>'
                        html += f'<td style="padding: 0.75rem;"><span class="badge {job_state}">{job_state}</span></td>'
                        html += f'<td style="padding: 0.75rem;">{job_queue}</td>'
                        html += f'<td style="padding: 0.75rem;">{job_class}</td>'
                        html += f'<td style="padding: 0.75rem;">{created}</td>'
                        html += "</tr>"

                    html += "</tbody></table>"

                return web.Response(text=html, content_type="text/html")
            else:
                return web.json_response(jobs)

    async def api_job_get(self, request: web.Request) -> web.Response:
        """Get single job"""
        job_id = int(request.match_info["job_id"])
        async with self.api() as api:
            job = await api.get_job(job_id)
            if not job:
                return web.json_response({"error": "Job not found"}, status=404)
            return web.json_response(job)

    async def api_job_retry(self, request: web.Request) -> web.Response:
        """Retry a job"""
        job_id = int(request.match_info["job_id"])
        async with self.api() as api:
            try:
                result = await api.retry_job(job_id)
                return web.json_response(result)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

    async def api_job_cancel(self, request: web.Request) -> web.Response:
        """Cancel a job"""
        job_id = int(request.match_info["job_id"])
        async with self.api() as api:
            try:
                result = await api.cancel_job(job_id)
                return web.json_response(result)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

    async def api_job_delete(self, request: web.Request) -> web.Response:
        """Delete a job"""
        job_id = int(request.match_info["job_id"])
        async with self.api() as api:
            deleted = await api.delete_job(job_id)
            if deleted:
                return web.json_response({"status": "deleted", "job_id": job_id})
            else:
                return web.json_response({"error": "Job not found"}, status=404)

    async def api_queues_list(self, request: web.Request) -> web.Response:
        """List queue statistics"""
        async with self.api() as api:
            stats = await api.queue_stats()
            format_type = request.query.get("format", "json")

            if format_type == "html":
                if not stats:
                    html = "<p>No queue data</p>"
                else:
                    html = '<div class="stats-grid">'
                    for s in stats:
                        html += '<div class="stat-item">'
                        html += f"<span><strong>{html_mod.escape(str(s['queue']))}</strong></span>"
                        html += "<span>"
                        if s["queued"] > 0:
                            html += f'<span class="badge queued">{int(s["queued"])} queued</span> '
                        if s["running"] > 0:
                            html += f'<span class="badge running">{int(s["running"])} running</span> '
                        if s["crashed"] > 0:
                            html += f'<span class="badge crashed">{int(s["crashed"])} crashed</span>'
                        html += "</span></div>"
                    html += "</div>"
                return web.Response(text=html, content_type="text/html")
            else:
                return web.json_response(stats)

    async def api_queue_stats(self, request: web.Request) -> web.Response:
        """Get stats for specific queue"""
        queue = request.match_info["queue"]
        async with self.api() as api:
            stats = await api.queue_stats(queue=queue)
            return web.json_response(stats)

    async def api_workers_list(self, request: web.Request) -> web.Response:
        """List active workers"""
        async with self.api() as api:
            workers = await api.list_workers()
            return web.json_response(workers)

    async def api_workers_stats(self, request: web.Request) -> web.Response:
        """Get worker statistics"""
        async with self.api() as api:
            stats = await api.worker_stats()
            format_type = request.query.get("format", "json")

            if format_type == "html":
                html = f'<div class="stat-value">{int(stats["active_workers"])}</div>'
                html += '<div class="stat-label">Active Workers</div>'
                return web.Response(text=html, content_type="text/html")
            else:
                return web.json_response(stats)

    async def api_dlq_list(self, request: web.Request) -> web.Response:
        """List Dead Letter Queue jobs"""
        async with self.api() as api:
            limit = int(request.query.get("limit", 100))
            jobs = await api.list_dlq(limit=limit)
            return web.json_response(jobs)

    async def api_dlq_retry(self, request: web.Request) -> web.Response:
        """Retry job from DLQ"""
        job_id = int(request.match_info["job_id"])
        async with self.api() as api:
            try:
                result = await api.retry_from_dlq(job_id)
                return web.json_response(result)
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)

    async def api_metrics(self, request: web.Request) -> web.Response:
        """Get system metrics"""
        async with self.api() as api:
            since_hours = int(request.query.get("since_hours", 24))
            queue = request.query.get("queue")
            format_type = request.query.get("format", "json")

            # jorb timestamps are naive-UTC, so compare with a naive-UTC value
            since = datetime.now(UTC) - timedelta(
                hours=since_hours
            )
            metrics = await api.get_metrics(since=since, queue=queue)

            if format_type == "html":
                html = '<div class="stats-grid">'
                html += '<div class="stat-item">'
                html += f'<span>Finished</span><span class="badge finished">{int(metrics["finished_count"])}</span>'
                html += "</div>"
                html += '<div class="stat-item">'
                html += f'<span>Crashed</span><span class="badge crashed">{int(metrics["crashed_count"])}</span>'
                html += "</div>"
                html += '<div class="stat-item">'
                html += f"<span>Avg Duration</span><span>{metrics['avg_duration_seconds']:.2f}s</span>"
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
                        <input type="number" name="prio" value="100">
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
        async with self.api() as api:
            schedule_id = int(request.match_info["schedule_id"])
            schedule = await api.get_schedule(schedule_id=schedule_id)

            if not schedule:
                return web.json_response({"error": "Schedule not found"}, status=404)

            return web.json_response(
                schedule, dumps=lambda x: json.dumps(x, default=str)
            )

    async def api_schedule_create(self, request: web.Request) -> web.Response:
        """Create new schedule"""
        async with self.api() as api:
            try:
                data = await request.post()

                schedule = await api.create_schedule(
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

    async def api_schedule_enable(self, request: web.Request) -> web.Response:
        """Enable schedule"""
        schedule_id = int(request.match_info["schedule_id"])
        async with self.api() as api:
            await api.enable_schedule(schedule_id)

        # Return refreshed list
        return await self.api_schedules_list(request)

    async def api_schedule_disable(self, request: web.Request) -> web.Response:
        """Disable schedule"""
        schedule_id = int(request.match_info["schedule_id"])
        async with self.api() as api:
            await api.disable_schedule(schedule_id)

        # Return refreshed list
        return await self.api_schedules_list(request)

    async def api_schedule_delete(self, request: web.Request) -> web.Response:
        """Delete schedule"""
        schedule_id = int(request.match_info["schedule_id"])
        async with self.api() as api:
            await api.delete_schedule(schedule_id)

        # Return refreshed list
        return await self.api_schedules_list(request)

    async def api_schedule_history(self, request: web.Request) -> web.Response:
        """Get schedule execution history"""
        async with self.api() as api:
            schedule_id = int(request.match_info["schedule_id"])
            limit = int(request.query.get("limit", 50))

            history = await api.get_schedule_history(
                schedule_id=schedule_id, limit=limit
            )
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

        # Keep running
        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            print("\n👋 Shutting down...")
        finally:
            # Cleans up the app, which also closes the connection pool
            await runner.cleanup()


async def serve(db_params: dict, host: str, port: int) -> None:
    """Create and run a WebAdminServer until interrupted."""
    server = WebAdminServer(db_params, host=host, port=port)
    await server.start()


def main() -> None:
    """CLI entry point for the web admin server."""
    import click

    @click.command()
    @click.argument("config", default="./pyjobby.conf.py")
    @click.option(
        "--host",
        default="127.0.0.1",
        show_default=True,
        help="Bind address (use 0.0.0.0 to expose; the admin UI has no authentication)",
    )
    @click.option("--port", default=8081, show_default=True, help="Bind port")
    def cli(config: str, host: str, port: int) -> None:
        """Run the pyjobby web admin interface."""
        from .configloader import load_config_from_file

        cfg = load_config_from_file(config, keys=["db_params"])
        db_params = cfg.get("db_params")
        if not db_params:
            raise click.ClickException(f"No db_params found in config: {config}")

        asyncio.run(serve(db_params, host, port))

    cli()


if __name__ == "__main__":
    main()
