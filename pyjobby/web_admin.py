#!/usr/bin/env python3
"""
Pyjobby Web Admin Interface

HTTP server providing web-based management interface using htmx.
Built on top of the admin API for clean separation.
"""

import asyncio
import asyncpg
from aiohttp import web
import json
from typing import Optional
from datetime import datetime, timedelta

from .admin_api import AdminAPI


class WebAdminServer:
    """
    Web-based administration interface for pyjobby.

    Provides REST API endpoints and HTML interface for managing jobs, queues, and workers.
    Uses htmx for dynamic updates without full page reloads.
    """

    def __init__(self, db_params: dict, host: str = '0.0.0.0', port: int = 8081):
        """
        Initialize web admin server.

        Args:
            db_params: Database connection parameters
            host: Host to bind to (default: 0.0.0.0)
            port: Port to listen on (default: 8081)
        """
        self.db_params = db_params
        self.host = host
        self.port = port
        self.app = web.Application()
        self.setup_routes()

    def setup_routes(self):
        """Setup HTTP routes"""
        # HTML pages
        self.app.router.add_get('/', self.index)
        self.app.router.add_get('/jobs', self.jobs_page)
        self.app.router.add_get('/queues', self.queues_page)
        self.app.router.add_get('/workers', self.workers_page)
        self.app.router.add_get('/dlq', self.dlq_page)
        self.app.router.add_get('/metrics', self.metrics_page)

        # API endpoints for htmx
        self.app.router.add_get('/api/jobs', self.api_jobs_list)
        self.app.router.add_get('/api/jobs/{job_id}', self.api_job_get)
        self.app.router.add_post('/api/jobs/{job_id}/retry', self.api_job_retry)
        self.app.router.add_post('/api/jobs/{job_id}/cancel', self.api_job_cancel)
        self.app.router.add_delete('/api/jobs/{job_id}', self.api_job_delete)

        self.app.router.add_get('/api/queues', self.api_queues_list)
        self.app.router.add_get('/api/queues/{queue}/stats', self.api_queue_stats)

        self.app.router.add_get('/api/workers', self.api_workers_list)
        self.app.router.add_get('/api/workers/stats', self.api_workers_stats)

        self.app.router.add_get('/api/dlq', self.api_dlq_list)
        self.app.router.add_post('/api/dlq/{job_id}/retry', self.api_dlq_retry)

        self.app.router.add_get('/api/metrics', self.api_metrics)

    async def get_api(self) -> AdminAPI:
        """Get AdminAPI instance with fresh database connection"""
        conn = await asyncpg.connect(**self.db_params)
        return AdminAPI(conn)

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
        return web.Response(text=html, content_type='text/html')

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
        return web.Response(text=html, content_type='text/html')

    async def queues_page(self, request: web.Request) -> web.Response:
        """Queues management page - Placeholder"""
        return web.Response(text="<h1>Queues Page - Coming Soon</h1>", content_type='text/html')

    async def workers_page(self, request: web.Request) -> web.Response:
        """Workers management page - Placeholder"""
        return web.Response(text="<h1>Workers Page - Coming Soon</h1>", content_type='text/html')

    async def dlq_page(self, request: web.Request) -> web.Response:
        """DLQ management page - Placeholder"""
        return web.Response(text="<h1>DLQ Page - Coming Soon</h1>", content_type='text/html')

    async def metrics_page(self, request: web.Request) -> web.Response:
        """Metrics page - Placeholder"""
        return web.Response(text="<h1>Metrics Page - Coming Soon</h1>", content_type='text/html')

    # =========================================================================
    # API Endpoints
    # =========================================================================

    async def api_jobs_list(self, request: web.Request) -> web.Response:
        """List jobs (JSON or HTML)"""
        api = await self.get_api()
        try:
            # Parse query parameters
            queue = request.query.get('queue')
            state = request.query.get('state')
            limit = int(request.query.get('limit', 50))
            offset = int(request.query.get('offset', 0))
            format_type = request.query.get('format', 'json')

            jobs = await api.list_jobs(
                queue=queue,
                state=state,
                limit=limit,
                offset=offset
            )

            if format_type == 'html':
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
                    html += '</tr></thead><tbody>'

                    for job in jobs:
                        created = job['created'][:19] if job['created'] else ''
                        html += f'<tr style="border-bottom: 1px solid #eee;">'
                        html += f'<td style="padding: 0.75rem;">{job["id"]}</td>'
                        html += f'<td style="padding: 0.75rem;"><span class="badge {job["state"]}">{job["state"]}</span></td>'
                        html += f'<td style="padding: 0.75rem;">{job["queue"]}</td>'
                        html += f'<td style="padding: 0.75rem;">{job["job_class"]}</td>'
                        html += f'<td style="padding: 0.75rem;">{created}</td>'
                        html += '</tr>'

                    html += '</tbody></table>'

                return web.Response(text=html, content_type='text/html')
            else:
                return web.json_response(jobs)

        finally:
            await api.conn.close()

    async def api_job_get(self, request: web.Request) -> web.Response:
        """Get single job"""
        job_id = int(request.match_info['job_id'])
        api = await self.get_api()
        try:
            job = await api.get_job(job_id)
            if not job:
                return web.json_response({'error': 'Job not found'}, status=404)
            return web.json_response(job)
        finally:
            await api.conn.close()

    async def api_job_retry(self, request: web.Request) -> web.Response:
        """Retry a job"""
        job_id = int(request.match_info['job_id'])
        api = await self.get_api()
        try:
            result = await api.retry_job(job_id)
            return web.json_response(result)
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)
        finally:
            await api.conn.close()

    async def api_job_cancel(self, request: web.Request) -> web.Response:
        """Cancel a job"""
        job_id = int(request.match_info['job_id'])
        api = await self.get_api()
        try:
            result = await api.cancel_job(job_id)
            return web.json_response(result)
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)
        finally:
            await api.conn.close()

    async def api_job_delete(self, request: web.Request) -> web.Response:
        """Delete a job"""
        job_id = int(request.match_info['job_id'])
        api = await self.get_api()
        try:
            deleted = await api.delete_job(job_id)
            if deleted:
                return web.json_response({'status': 'deleted', 'job_id': job_id})
            else:
                return web.json_response({'error': 'Job not found'}, status=404)
        finally:
            await api.conn.close()

    async def api_queues_list(self, request: web.Request) -> web.Response:
        """List queue statistics"""
        api = await self.get_api()
        try:
            stats = await api.queue_stats()
            format_type = request.query.get('format', 'json')

            if format_type == 'html':
                if not stats:
                    html = '<p>No queue data</p>'
                else:
                    html = '<div class="stats-grid">'
                    for s in stats:
                        html += '<div class="stat-item">'
                        html += f'<span><strong>{s["queue"]}</strong></span>'
                        html += '<span>'
                        if s['queued'] > 0:
                            html += f'<span class="badge queued">{s["queued"]} queued</span> '
                        if s['running'] > 0:
                            html += f'<span class="badge running">{s["running"]} running</span> '
                        if s['crashed'] > 0:
                            html += f'<span class="badge crashed">{s["crashed"]} crashed</span>'
                        html += '</span></div>'
                    html += '</div>'
                return web.Response(text=html, content_type='text/html')
            else:
                return web.json_response(stats)
        finally:
            await api.conn.close()

    async def api_queue_stats(self, request: web.Request) -> web.Response:
        """Get stats for specific queue"""
        queue = request.match_info['queue']
        api = await self.get_api()
        try:
            stats = await api.queue_stats(queue=queue)
            return web.json_response(stats)
        finally:
            await api.conn.close()

    async def api_workers_list(self, request: web.Request) -> web.Response:
        """List active workers"""
        api = await self.get_api()
        try:
            workers = await api.list_workers()
            return web.json_response(workers)
        finally:
            await api.conn.close()

    async def api_workers_stats(self, request: web.Request) -> web.Response:
        """Get worker statistics"""
        api = await self.get_api()
        try:
            stats = await api.worker_stats()
            format_type = request.query.get('format', 'json')

            if format_type == 'html':
                html = f'<div class="stat-value">{stats["active_workers"]}</div>'
                html += f'<div class="stat-label">Active Workers</div>'
                return web.Response(text=html, content_type='text/html')
            else:
                return web.json_response(stats)
        finally:
            await api.conn.close()

    async def api_dlq_list(self, request: web.Request) -> web.Response:
        """List Dead Letter Queue jobs"""
        api = await self.get_api()
        try:
            limit = int(request.query.get('limit', 100))
            jobs = await api.list_dlq(limit=limit)
            return web.json_response(jobs)
        finally:
            await api.conn.close()

    async def api_dlq_retry(self, request: web.Request) -> web.Response:
        """Retry job from DLQ"""
        job_id = int(request.match_info['job_id'])
        api = await self.get_api()
        try:
            result = await api.retry_from_dlq(job_id)
            return web.json_response(result)
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)
        finally:
            await api.conn.close()

    async def api_metrics(self, request: web.Request) -> web.Response:
        """Get system metrics"""
        api = await self.get_api()
        try:
            since_hours = int(request.query.get('since_hours', 24))
            queue = request.query.get('queue')
            format_type = request.query.get('format', 'json')

            since = datetime.utcnow() - timedelta(hours=since_hours)
            metrics = await api.get_metrics(since=since, queue=queue)

            if format_type == 'html':
                html = '<div class="stats-grid">'
                html += '<div class="stat-item">'
                html += f'<span>Finished</span><span class="badge finished">{metrics["finished_count"]}</span>'
                html += '</div>'
                html += '<div class="stat-item">'
                html += f'<span>Crashed</span><span class="badge crashed">{metrics["crashed_count"]}</span>'
                html += '</div>'
                html += '<div class="stat-item">'
                html += f'<span>Avg Duration</span><span>{metrics["avg_duration_seconds"]:.2f}s</span>'
                html += '</div>'
                html += '</div>'
                return web.Response(text=html, content_type='text/html')
            else:
                return web.json_response(metrics)
        finally:
            await api.conn.close()

    async def start(self):
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
            await runner.cleanup()


async def async_main():
    """Run web admin server standalone (async)"""
    import sys
    from .configloader import load_config_from_file

    config_path = sys.argv[1] if len(sys.argv) > 1 else './pyjobby.conf.py'
    config = load_config_from_file(config_path, keys=["db_params"])
    db_params = config.get("db_params")

    if not db_params:
        print("Error: No db_params found in config file")
        sys.exit(1)

    server = WebAdminServer(db_params)
    await server.start()


def main():
    """Sync entry point for poetry script"""
    asyncio.run(async_main())


if __name__ == '__main__':
    main()
