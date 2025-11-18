#!/usr/bin/env python3
"""
Pyjobby CLI Management Tools

Command-line interface for managing jobs, queues, and workers.
Built on top of the admin API for clean separation of concerns.
"""

import asyncio
import click
import asyncpg
import sys
from typing import Optional
from datetime import datetime, timedelta
import json

from .admin_api import AdminAPI
from .configloader import load_config_from_file


# ANSI color codes for terminal output
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def print_success(msg: str) -> None:
    """Print success message in green"""
    click.echo(f"{Colors.OKGREEN}{msg}{Colors.ENDC}")


def print_error(msg: str) -> None:
    """Print error message in red"""
    click.echo(f"{Colors.FAIL}Error: {msg}{Colors.ENDC}", err=True)


def print_warning(msg: str) -> None:
    """Print warning message in yellow"""
    click.echo(f"{Colors.WARNING}{msg}{Colors.ENDC}")


def print_table(headers: list[str], rows: list[list[str]], max_width: int = 80) -> None:
    """Print data as formatted table"""
    if not rows:
        print_warning("No data to display")
        return

    # Calculate column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    # Limit column widths to fit terminal
    for i in range(len(col_widths)):
        col_widths[i] = min(col_widths[i], max_width // len(headers))

    # Print header
    header_row = "  ".join(
        h[:col_widths[i]].ljust(col_widths[i])
        for i, h in enumerate(headers)
    )
    click.echo(f"{Colors.BOLD}{header_row}{Colors.ENDC}")
    click.echo("-" * len(header_row))

    # Print rows
    for row in rows:
        row_str = "  ".join(
            str(cell)[:col_widths[i]].ljust(col_widths[i])
            for i, cell in enumerate(row)
        )
        click.echo(row_str)


async def get_connection(config_path: str) -> asyncpg.Connection:
    """Get database connection from config file"""
    try:
        config = load_config_from_file(config_path, keys=["db_params"])
        db_params = config.get("db_params")
        if not db_params:
            print_error(f"No db_params found in config file: {config_path}")
            print_error("Config file must define db_params dict")
            sys.exit(1)
        conn = await asyncpg.connect(**db_params)
        return conn
    except FileNotFoundError:
        print_error(f"Config file not found: {config_path}")
        print_error("Use --config to specify config file path")
        sys.exit(1)
    except Exception as e:
        print_error(f"Failed to connect to database: {e}")
        sys.exit(1)


# =========================================================================
# Main CLI group
# =========================================================================

@click.group()
@click.option('--config', '-c', default='./pyjobby.conf.py',
              help='Config file path')
@click.pass_context
def cli(ctx, config):
    """Pyjobby job queue management CLI"""
    ctx.ensure_object(dict)
    ctx.obj['config'] = config


# =========================================================================
# Job Management Commands
# =========================================================================

@cli.group()
def jobs():
    """Manage jobs"""
    pass


@jobs.command('list')
@click.option('--queue', '-q', help='Filter by queue')
@click.option('--state', '-s', help='Filter by state (queued, running, etc.)')
@click.option('--job-class', help='Filter by job class (supports patterns)')
@click.option('--uid', type=int, help='Filter by user ID')
@click.option('--limit', '-l', default=50, help='Max results (default: 50)')
@click.option('--offset', '-o', default=0, help='Offset for pagination')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def jobs_list(ctx, queue, state, job_class, uid, limit, offset, output_json):
    """List jobs with optional filtering"""
    async def _list():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            jobs = await api.list_jobs(
                queue=queue,
                state=state,
                job_class=job_class,
                uid=uid,
                limit=limit,
                offset=offset
            )

            if output_json:
                click.echo(json.dumps(jobs, indent=2))
            else:
                if not jobs:
                    print_warning("No jobs found")
                    return

                headers = ['ID', 'State', 'Queue', 'Job Class', 'Priority', 'Created']
                rows = []
                for job in jobs:
                    created = job['created'][:19] if job['created'] else ''
                    rows.append([
                        str(job['id']),
                        job['state'],
                        job['queue'],
                        job['job_class'],
                        str(job['prio']),
                        created
                    ])

                print_table(headers, rows)
                print_warning(f"\nShowing {len(jobs)} job(s). Use --limit and --offset for pagination.")
        finally:
            await conn.close()

    asyncio.run(_list())


@jobs.command('inspect')
@click.argument('job_id', type=int)
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def jobs_inspect(ctx, job_id, output_json):
    """Show detailed information about a job"""
    async def _inspect():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            job = await api.get_job(job_id)

            if not job:
                print_error(f"Job {job_id} not found")
                sys.exit(1)

            if output_json:
                click.echo(json.dumps(job, indent=2))
            else:
                click.echo(f"\n{Colors.BOLD}Job {job_id} Details{Colors.ENDC}")
                click.echo("-" * 50)
                click.echo(f"State:           {job['state']}")
                click.echo(f"Queue:           {job['queue']}")
                click.echo(f"Job Class:       {job['job_class']}")
                click.echo(f"Priority:        {job['prio']}")
                click.echo(f"Created:         {job['created']}")
                click.echo(f"Updated:         {job['updated']}")
                click.echo(f"Run After:       {job['run_after']}")
                click.echo(f"Run Count:       {job['run_count']}")
                click.echo(f"Error Count:     {job['error_count']}")

                if job['capability']:
                    click.echo(f"Capability:      {job['capability']}")
                if job['uid']:
                    click.echo(f"User ID:         {job['uid']}")
                if job['worker_host']:
                    click.echo(f"Worker:          {job['worker_host']}:{job['worker_pid']}")

                click.echo(f"\nArguments:")
                click.echo(json.dumps(job['kwargs'], indent=2))

                if job['result']:
                    click.echo(f"\nResult:")
                    click.echo(json.dumps(job['result'], indent=2))

                if job['error_message']:
                    click.echo(f"\n{Colors.FAIL}Error:{Colors.ENDC}")
                    click.echo(job['error_message'])

                if job['error_backtrace']:
                    click.echo(f"\n{Colors.FAIL}Backtrace:{Colors.ENDC}")
                    click.echo(job['error_backtrace'])

        finally:
            await conn.close()

    asyncio.run(_inspect())


@jobs.command('retry')
@click.argument('job_ids', nargs=-1, type=int, required=True)
@click.pass_context
def jobs_retry(ctx, job_ids):
    """Retry one or more crashed jobs"""
    async def _retry():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)

            if len(job_ids) == 1:
                # Single job
                try:
                    result = await api.retry_job(job_ids[0])
                    print_success(
                        f"Job {result['original_job_id']} retry queued as "
                        f"job {result['new_job_id']}"
                    )
                except ValueError as e:
                    print_error(str(e))
                    sys.exit(1)
            else:
                # Multiple jobs
                results = await api.retry_jobs(list(job_ids))
                success_count = sum(1 for r in results if r['status'] != 'error')
                error_count = len(results) - success_count

                for result in results:
                    if result['status'] == 'error':
                        print_error(f"Job {result['original_job_id']}: {result['error']}")
                    else:
                        print_success(
                            f"Job {result['original_job_id']} → {result['new_job_id']}"
                        )

                click.echo(f"\n{Colors.BOLD}Summary:{Colors.ENDC}")
                print_success(f"  Retried: {success_count}")
                if error_count:
                    print_error(f"  Failed: {error_count}")

        finally:
            await conn.close()

    asyncio.run(_retry())


@jobs.command('cancel')
@click.argument('job_ids', nargs=-1, type=int, required=True)
@click.pass_context
def jobs_cancel(ctx, job_ids):
    """Cancel one or more queued/waiting jobs"""
    async def _cancel():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)

            if len(job_ids) == 1:
                # Single job
                try:
                    result = await api.cancel_job(job_ids[0])
                    print_success(f"Job {result['job_id']} cancelled")
                except ValueError as e:
                    print_error(str(e))
                    sys.exit(1)
            else:
                # Multiple jobs
                results = await api.cancel_jobs(list(job_ids))
                success_count = sum(1 for r in results if r['status'] != 'error')
                error_count = len(results) - success_count

                for result in results:
                    if result['status'] == 'error':
                        print_error(f"Job {result['job_id']}: {result['error']}")
                    else:
                        print_success(f"Job {result['job_id']} cancelled")

                click.echo(f"\n{Colors.BOLD}Summary:{Colors.ENDC}")
                print_success(f"  Cancelled: {success_count}")
                if error_count:
                    print_error(f"  Failed: {error_count}")

        finally:
            await conn.close()

    asyncio.run(_cancel())


@jobs.command('delete')
@click.argument('job_id', type=int)
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
@click.pass_context
def jobs_delete(ctx, job_id, force):
    """Delete a job (permanent!)"""
    async def _delete():
        if not force:
            if not click.confirm(f"Delete job {job_id}? This is permanent"):
                click.echo("Cancelled")
                return

        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            deleted = await api.delete_job(job_id)

            if deleted:
                print_success(f"Job {job_id} deleted")
            else:
                print_error(f"Job {job_id} not found")
                sys.exit(1)

        finally:
            await conn.close()

    asyncio.run(_delete())


# =========================================================================
# Queue Management Commands
# =========================================================================

@cli.group()
def queues():
    """Manage queues"""
    pass


@queues.command('list')
@click.pass_context
def queues_list(ctx):
    """List all queues"""
    async def _list():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            queues = await api.list_queues()

            if not queues:
                print_warning("No queues found")
                return

            click.echo(f"\n{Colors.BOLD}Queues:{Colors.ENDC}")
            for queue in queues:
                click.echo(f"  • {queue}")

        finally:
            await conn.close()

    asyncio.run(_list())


@queues.command('stats')
@click.option('--queue', '-q', help='Specific queue (default: all)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def queues_stats(ctx, queue, output_json):
    """Show queue statistics"""
    async def _stats():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            stats = await api.queue_stats(queue=queue)

            if output_json:
                click.echo(json.dumps(stats, indent=2))
            else:
                if not stats:
                    print_warning("No stats available")
                    return

                headers = ['Queue', 'Queued', 'Running', 'Waiting', 'Finished', 'Crashed', 'Total']
                rows = []
                for s in stats:
                    rows.append([
                        s['queue'],
                        str(s['queued']),
                        str(s['running']),
                        str(s['waiting']),
                        str(s['finished']),
                        str(s['crashed']),
                        str(s['total'])
                    ])

                print_table(headers, rows)

                # Show oldest queued job age if available
                for s in stats:
                    if s.get('oldest_queued_age_seconds'):
                        age = int(s['oldest_queued_age_seconds'])
                        minutes = age // 60
                        click.echo(
                            f"\nOldest queued job in '{s['queue']}': "
                            f"{minutes} minutes ago"
                        )

        finally:
            await conn.close()

    asyncio.run(_stats())


@queues.command('clear')
@click.argument('queue')
@click.option('--state', '-s', help='Only clear jobs in this state')
@click.option('--older-than-days', type=int, help='Only clear jobs older than N days')
@click.option('--force', '-f', is_flag=True, help='Skip confirmation')
@click.pass_context
def queues_clear(ctx, queue, state, older_than_days, force):
    """Clear (delete) jobs from a queue"""
    async def _clear():
        # Build description
        desc = f"queue '{queue}'"
        if state:
            desc += f" with state '{state}'"
        if older_than_days:
            desc += f" older than {older_than_days} days"

        if not force:
            if not click.confirm(f"Delete all jobs in {desc}? This is permanent"):
                click.echo("Cancelled")
                return

        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            count = await api.clear_queue(
                queue=queue,
                state=state,
                older_than_days=older_than_days
            )

            print_success(f"Deleted {count} job(s) from {desc}")

        finally:
            await conn.close()

    asyncio.run(_clear())


# =========================================================================
# Worker Management Commands
# =========================================================================

@cli.group()
def workers():
    """Manage workers"""
    pass


@workers.command('list')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def workers_list(ctx, output_json):
    """List active workers"""
    async def _list():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            workers = await api.list_workers()

            if output_json:
                click.echo(json.dumps(workers, indent=2))
            else:
                if not workers:
                    print_warning("No active workers")
                    return

                headers = ['Host', 'PID', 'Job ID', 'Job Class', 'State']
                rows = []
                for w in workers:
                    rows.append([
                        w['worker_host'],
                        str(w['worker_pid']),
                        str(w['job_id']),
                        w['job_class'],
                        w['state']
                    ])

                print_table(headers, rows)

        finally:
            await conn.close()

    asyncio.run(_list())


@workers.command('stats')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def workers_stats(ctx, output_json):
    """Show worker statistics"""
    async def _stats():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            stats = await api.worker_stats()

            if output_json:
                click.echo(json.dumps(stats, indent=2))
            else:
                click.echo(f"\n{Colors.BOLD}Worker Statistics{Colors.ENDC}")
                click.echo("-" * 50)
                click.echo(f"Active Workers: {stats['active_workers']}")

                if stats['workers']:
                    click.echo(f"\n{Colors.BOLD}Worker Details:{Colors.ENDC}")
                    headers = ['Host', 'PID', 'Jobs', 'Oldest Job Started']
                    rows = []
                    for w in stats['workers']:
                        oldest = w['oldest_job_started'][:19] if w['oldest_job_started'] else ''
                        rows.append([
                            w['host'],
                            str(w['pid']),
                            str(w['job_count']),
                            oldest
                        ])
                    print_table(headers, rows)

        finally:
            await conn.close()

    asyncio.run(_stats())


# =========================================================================
# Dead Letter Queue Commands
# =========================================================================

@cli.group()
def dlq():
    """Manage Dead Letter Queue"""
    pass


@dlq.command('list')
@click.option('--limit', '-l', default=100, help='Max results (default: 100)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def dlq_list(ctx, limit, output_json):
    """List jobs in Dead Letter Queue"""
    async def _list():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            jobs = await api.list_dlq(limit=limit)

            if output_json:
                click.echo(json.dumps(jobs, indent=2))
            else:
                if not jobs:
                    print_success("Dead Letter Queue is empty!")
                    return

                print_warning(f"Found {len(jobs)} permanently failed job(s):")

                headers = ['ID', 'Job Class', 'Error Count', 'Last Error']
                rows = []
                for job in jobs:
                    error_msg = job['error_message'] or ''
                    if len(error_msg) > 40:
                        error_msg = error_msg[:37] + '...'

                    rows.append([
                        str(job['id']),
                        job['job_class'],
                        str(job['error_count']),
                        error_msg
                    ])

                print_table(headers, rows)

        finally:
            await conn.close()

    asyncio.run(_list())


@dlq.command('retry')
@click.argument('job_id', type=int)
@click.pass_context
def dlq_retry(ctx, job_id):
    """Retry a job from Dead Letter Queue"""
    async def _retry():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            result = await api.retry_from_dlq(job_id)

            print_success(
                f"DLQ job {result['original_job_id']} retry queued as "
                f"job {result['new_job_id']} (error count reset to 0)"
            )

        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
        finally:
            await conn.close()

    asyncio.run(_retry())


# =========================================================================
# Metrics Commands
# =========================================================================

@cli.command()
@click.option('--queue', '-q', help='Filter by queue')
@click.option('--since-hours', type=int, default=24, help='Hours to look back (default: 24)')
@click.option('--json', 'output_json', is_flag=True, help='Output as JSON')
@click.pass_context
def metrics(ctx, queue, since_hours, output_json):
    """Show system metrics"""
    async def _metrics():
        conn = await get_connection(ctx.obj['config'])
        try:
            api = AdminAPI(conn)
            since = datetime.utcnow() - timedelta(hours=since_hours)
            metrics_data = await api.get_metrics(since=since, queue=queue)

            if output_json:
                click.echo(json.dumps(metrics_data, indent=2))
            else:
                click.echo(f"\n{Colors.BOLD}System Metrics (last {since_hours}h){Colors.ENDC}")
                if queue:
                    click.echo(f"Queue: {queue}")
                click.echo("-" * 50)

                click.echo(f"Finished:          {metrics_data['finished_count']}")
                click.echo(f"Crashed:           {metrics_data['crashed_count']}")
                click.echo(f"Avg Duration:      {metrics_data['avg_duration_seconds']:.2f}s")

                if metrics_data.get('state_counts'):
                    click.echo(f"\n{Colors.BOLD}Jobs by State:{Colors.ENDC}")
                    for state, count in sorted(metrics_data['state_counts'].items()):
                        click.echo(f"  {state:12} {count}")

                if metrics_data.get('top_errors'):
                    click.echo(f"\n{Colors.BOLD}Top Errors:{Colors.ENDC}")
                    for error in metrics_data['top_errors'][:5]:
                        click.echo(f"  {error['job_class']} ({error['error_count']})")
                        if error['latest_error']:
                            msg = error['latest_error'][:60]
                            click.echo(f"    {msg}...")

        finally:
            await conn.close()

    asyncio.run(_metrics())


if __name__ == '__main__':
    cli(obj={})
