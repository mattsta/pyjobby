#!/usr/bin/env python3
"""
Pyjobby CLI Management Tools

Command-line interface for managing jobs, queues, and workers.
Built on top of the admin API for clean separation of concerns.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta

import asyncpg  # type: ignore[import-untyped]
import click

from . import db
from .admin_api import AdminAPI
from .configloader import load_config_from_file


# ANSI color codes for terminal output
class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


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
        h[: col_widths[i]].ljust(col_widths[i]) for i, h in enumerate(headers)
    )
    click.echo(f"{Colors.BOLD}{header_row}{Colors.ENDC}")
    click.echo("-" * len(header_row))

    # Print rows
    for row in rows:
        row_str = "  ".join(
            str(cell)[: col_widths[i]].ljust(col_widths[i])
            for i, cell in enumerate(row)
        )
        click.echo(row_str)


async def get_connection(
    config_path: str, dsn: str | None = None
) -> asyncpg.Connection:
    """Get database connection from a DSN (if given) or a config file"""
    try:
        if dsn:
            return await db.connect(dsn)
        config = load_config_from_file(config_path, keys=["db_params"])
        db_params = config.get("db_params")
        if not db_params:
            print_error(f"No db_params found in config file: {config_path}")
            print_error("Config file must define db_params dict")
            sys.exit(1)
        conn = await db.connect(**db_params)
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
@click.option("--config", "-c", default="./pyjobby.conf.py", help="Config file path")
@click.option(
    "--dsn",
    envvar="PYJOBBY_DSN",
    default=None,
    help="PostgreSQL DSN (overrides --config; also read from PYJOBBY_DSN)",
)
@click.pass_context
def cli(ctx: click.Context, config: str, dsn: str | None) -> None:
    """Pyjobby job queue management CLI"""
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["dsn"] = dsn


# =========================================================================
# Job Management Commands
# =========================================================================


@cli.group()
def jobs() -> None:
    """Manage jobs"""
    pass


@jobs.command("list")
@click.option("--queue", "-q", help="Filter by queue")
@click.option("--state", "-s", help="Filter by state (queued, running, etc.)")
@click.option("--job-class", help="Filter by job class (supports patterns)")
@click.option("--uid", type=int, help="Filter by user ID")
@click.option("--limit", "-l", default=50, help="Max results (default: 50)")
@click.option("--offset", "-o", default=0, help="Offset for pagination")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def jobs_list(
    ctx: click.Context,
    queue: str | None,
    state: str | None,
    job_class: str | None,
    uid: int | None,
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List jobs with optional filtering"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            jobs = await api.list_jobs(
                queue=queue,
                state=state,
                job_class=job_class,
                uid=uid,
                limit=limit,
                offset=offset,
            )

            if output_json:
                click.echo(json.dumps(jobs, indent=2))
            else:
                if not jobs:
                    print_warning("No jobs found")
                    return

                headers = ["ID", "State", "Queue", "Job Class", "Priority", "Created"]
                rows = []
                for job in jobs:
                    created = job["created"][:19] if job["created"] else ""
                    rows.append(
                        [
                            str(job["id"]),
                            job["state"],
                            job["queue"],
                            job["job_class"],
                            str(job["prio"]),
                            created,
                        ]
                    )

                print_table(headers, rows)
                print_warning(
                    f"\nShowing {len(jobs)} job(s). Use --limit and --offset for pagination."
                )
        finally:
            await conn.close()

    asyncio.run(_list())


@jobs.command("inspect")
@click.argument("job_id", type=int)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def jobs_inspect(ctx: click.Context, job_id: int, output_json: bool) -> None:
    """Show detailed information about a job"""

    async def _inspect() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
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

                if job["capability"]:
                    click.echo(f"Capability:      {job['capability']}")
                if job["uid"]:
                    click.echo(f"User ID:         {job['uid']}")
                if job["worker_host"]:
                    click.echo(
                        f"Worker:          {job['worker_host']}:{job['worker_pid']}"
                    )

                click.echo(f"\nArguments:")
                click.echo(json.dumps(job["kwargs"], indent=2))

                if job["result"]:
                    click.echo(f"\nResult:")
                    click.echo(json.dumps(job["result"], indent=2))

                if job["error_message"]:
                    click.echo(f"\n{Colors.FAIL}Error:{Colors.ENDC}")
                    click.echo(job["error_message"])

                if job["error_backtrace"]:
                    click.echo(f"\n{Colors.FAIL}Backtrace:{Colors.ENDC}")
                    click.echo(job["error_backtrace"])

        finally:
            await conn.close()

    asyncio.run(_inspect())


@jobs.command("retry")
@click.argument("job_ids", nargs=-1, type=int, required=True)
@click.pass_context
def jobs_retry(ctx: click.Context, job_ids: tuple[int, ...]) -> None:
    """Retry one or more crashed jobs"""

    async def _retry() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            if len(job_ids) == 1:
                # Single job
                try:
                    result = await api.retry_job(job_ids[0])
                    print_success(
                        f"Job {result['job_id']} requeued for retry"
                    )
                except ValueError as e:
                    print_error(str(e))
                    sys.exit(1)
            else:
                # Multiple jobs
                results = await api.retry_jobs(list(job_ids))
                success_count = sum(1 for r in results if r["status"] != "error")
                error_count = len(results) - success_count

                for result in results:
                    if result["status"] == "error":
                        print_error(
                            f"Job {result['job_id']}: {result['error']}"
                        )
                    else:
                        print_success(
                            f"Job {result['job_id']} requeued"
                        )

                click.echo(f"\n{Colors.BOLD}Summary:{Colors.ENDC}")
                print_success(f"  Retried: {success_count}")
                if error_count:
                    print_error(f"  Failed: {error_count}")

        finally:
            await conn.close()

    asyncio.run(_retry())


@jobs.command("cancel")
@click.argument("job_ids", nargs=-1, type=int, required=True)
@click.pass_context
def jobs_cancel(ctx: click.Context, job_ids: tuple[int, ...]) -> None:
    """Cancel one or more queued/waiting jobs"""

    async def _cancel() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
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
                success_count = sum(1 for r in results if r["status"] != "error")
                error_count = len(results) - success_count

                for result in results:
                    if result["status"] == "error":
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


@jobs.command("delete")
@click.argument("job_id", type=int)
@click.option("--force", "-f", is_flag=True, help="Skip confirmation")
@click.pass_context
def jobs_delete(ctx: click.Context, job_id: int, force: bool) -> None:
    """Delete a job (permanent!)"""

    async def _delete() -> None:
        if not force and not click.confirm(f"Delete job {job_id}? This is permanent"):
            click.echo("Cancelled")
            return

        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
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
def queues() -> None:
    """Manage queues"""
    pass


@queues.command("list")
@click.pass_context
def queues_list(ctx: click.Context) -> None:
    """List all queues"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
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


@queues.command("stats")
@click.option("--queue", "-q", help="Specific queue (default: all)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def queues_stats(ctx: click.Context, queue: str | None, output_json: bool) -> None:
    """Show queue statistics"""

    async def _stats() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            stats = await api.queue_stats(queue=queue)

            if output_json:
                click.echo(json.dumps(stats, indent=2))
            else:
                if not stats:
                    print_warning("No stats available")
                    return

                headers = [
                    "Queue",
                    "Queued",
                    "Running",
                    "Waiting",
                    "Finished",
                    "Crashed",
                    "Total",
                ]
                rows = []
                for s in stats:
                    rows.append(
                        [
                            s["queue"],
                            str(s["queued"]),
                            str(s["running"]),
                            str(s["waiting"]),
                            str(s["finished"]),
                            str(s["crashed"]),
                            str(s["total"]),
                        ]
                    )

                print_table(headers, rows)

                # Show oldest queued job age if available
                for s in stats:
                    if s.get("oldest_queued_age_seconds"):
                        age = int(s["oldest_queued_age_seconds"])
                        minutes = age // 60
                        click.echo(
                            f"\nOldest queued job in '{s['queue']}': {minutes} minutes ago"
                        )

        finally:
            await conn.close()

    asyncio.run(_stats())


@queues.command("clear")
@click.argument("queue")
@click.option("--state", "-s", help="Only clear jobs in this state")
@click.option("--older-than-days", type=int, help="Only clear jobs older than N days")
@click.option("--force", "-f", is_flag=True, help="Skip confirmation")
@click.pass_context
def queues_clear(
    ctx: click.Context,
    queue: str,
    state: str | None,
    older_than_days: int | None,
    force: bool,
) -> None:
    """Clear (delete) jobs from a queue"""

    async def _clear() -> None:
        # Build description
        desc = f"queue '{queue}'"
        if state:
            desc += f" with state '{state}'"
        if older_than_days:
            desc += f" older than {older_than_days} days"

        if not force and not click.confirm(
            f"Delete all jobs in {desc}? This is permanent"
        ):
            click.echo("Cancelled")
            return

        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            count = await api.clear_queue(
                queue=queue, state=state, older_than_days=older_than_days
            )

            print_success(f"Deleted {count} job(s) from {desc}")

        finally:
            await conn.close()

    asyncio.run(_clear())


# =========================================================================
# Worker Management Commands
# =========================================================================


@cli.group()
def workers() -> None:
    """Manage workers"""
    pass


@workers.command("list")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def workers_list(ctx: click.Context, output_json: bool) -> None:
    """List active workers"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            workers = await api.list_workers()

            if output_json:
                click.echo(json.dumps(workers, indent=2))
            else:
                if not workers:
                    print_warning("No active workers")
                    return

                headers = ["Host", "PID", "Job ID", "Job Class", "State"]
                rows = []
                for w in workers:
                    rows.append(
                        [
                            w["worker_host"],
                            str(w["worker_pid"]),
                            str(w["job_id"]),
                            w["job_class"],
                            w["state"],
                        ]
                    )

                print_table(headers, rows)

        finally:
            await conn.close()

    asyncio.run(_list())


@workers.command("stats")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def workers_stats(ctx: click.Context, output_json: bool) -> None:
    """Show worker statistics"""

    async def _stats() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            stats = await api.worker_stats()

            if output_json:
                click.echo(json.dumps(stats, indent=2))
            else:
                click.echo(f"\n{Colors.BOLD}Worker Statistics{Colors.ENDC}")
                click.echo("-" * 50)
                click.echo(f"Active Workers: {stats['active_workers']}")

                if stats["workers"]:
                    click.echo(f"\n{Colors.BOLD}Worker Details:{Colors.ENDC}")
                    headers = ["Host", "PID", "Jobs", "Oldest Job Started"]
                    rows = []
                    for w in stats["workers"]:
                        oldest = (
                            w["oldest_job_started"][:19]
                            if w["oldest_job_started"]
                            else ""
                        )
                        rows.append(
                            [w["host"], str(w["pid"]), str(w["job_count"]), oldest]
                        )
                    print_table(headers, rows)

        finally:
            await conn.close()

    asyncio.run(_stats())


# =========================================================================
# Dead Letter Queue Commands
# =========================================================================


@cli.group()
def dlq() -> None:
    """Manage Dead Letter Queue"""
    pass


@dlq.command("list")
@click.option("--limit", "-l", default=100, help="Max results (default: 100)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def dlq_list(ctx: click.Context, limit: int, output_json: bool) -> None:
    """List jobs in Dead Letter Queue"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
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

                headers = ["ID", "Job Class", "Error Count", "Last Error"]
                rows = []
                for job in jobs:
                    error_msg = job["error_message"] or ""
                    if len(error_msg) > 40:
                        error_msg = error_msg[:37] + "..."

                    rows.append(
                        [
                            str(job["id"]),
                            job["job_class"],
                            str(job["error_count"]),
                            error_msg,
                        ]
                    )

                print_table(headers, rows)

        finally:
            await conn.close()

    asyncio.run(_list())


@dlq.command("retry")
@click.argument("job_id", type=int)
@click.pass_context
def dlq_retry(ctx: click.Context, job_id: int) -> None:
    """Retry a job from Dead Letter Queue"""

    async def _retry() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            result = await api.retry_from_dlq(job_id)

            print_success(
                f"DLQ job {result['job_id']} requeued (error count reset to 0)"
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
@click.option("--queue", "-q", help="Filter by queue")
@click.option(
    "--since-hours", type=int, default=24, help="Hours to look back (default: 24)"
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def metrics(
    ctx: click.Context, queue: str | None, since_hours: int, output_json: bool
) -> None:
    """Show system metrics"""

    async def _metrics() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            since = datetime.utcnow() - timedelta(hours=since_hours)
            metrics_data = await api.get_metrics(since=since, queue=queue)

            if output_json:
                click.echo(json.dumps(metrics_data, indent=2))
            else:
                click.echo(
                    f"\n{Colors.BOLD}System Metrics (last {since_hours}h){Colors.ENDC}"
                )
                if queue:
                    click.echo(f"Queue: {queue}")
                click.echo("-" * 50)

                click.echo(f"Finished:          {metrics_data['finished_count']}")
                click.echo(f"Crashed:           {metrics_data['crashed_count']}")
                click.echo(
                    f"Avg Duration:      {metrics_data['avg_duration_seconds']:.2f}s"
                )

                if metrics_data.get("state_counts"):
                    click.echo(f"\n{Colors.BOLD}Jobs by State:{Colors.ENDC}")
                    for state, count in sorted(metrics_data["state_counts"].items()):
                        click.echo(f"  {state:12} {count}")

                if metrics_data.get("top_errors"):
                    click.echo(f"\n{Colors.BOLD}Top Errors:{Colors.ENDC}")
                    for error in metrics_data["top_errors"][:5]:
                        click.echo(f"  {error['job_class']} ({error['error_count']})")
                        if error["latest_error"]:
                            msg = error["latest_error"][:60]
                            click.echo(f"    {msg}...")

        finally:
            await conn.close()

    asyncio.run(_metrics())


# =========================================================================
# Schedule Management Commands
# =========================================================================


@cli.group()
def schedule() -> None:
    """Manage recurring schedules"""
    pass


@schedule.command("list")
@click.option("--enabled", type=bool, help="Filter by enabled status (true/false)")
@click.option("--queue", "-q", help="Filter by queue")
@click.option("--limit", "-l", default=100, help="Max results (default: 100)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def schedule_list(
    ctx: click.Context,
    enabled: bool | None,
    queue: str | None,
    limit: int,
    output_json: bool,
) -> None:
    """List recurring schedules"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            schedules = await api.list_schedules(
                enabled=enabled, queue=queue, limit=limit
            )

            if output_json:
                click.echo(json.dumps(schedules, indent=2, default=str))
            else:
                if not schedules:
                    print_warning("No schedules found")
                    return

                headers = [
                    "ID",
                    "Name",
                    "Enabled",
                    "Cron",
                    "Queue",
                    "Next Run",
                    "Last Success",
                ]
                rows = []
                for s in schedules:
                    rows.append(
                        [
                            str(s["id"]),
                            s["name"][:30],
                            "✓" if s["enabled"] else "✗",
                            s["cron_expr"],
                            s["queue"],
                            s["next_run"].strftime("%Y-%m-%d %H:%M")
                            if s.get("next_run")
                            else "-",
                            s["last_success"].strftime("%Y-%m-%d %H:%M")
                            if s.get("last_success")
                            else "Never",
                        ]
                    )

                print_table(headers, rows)
                click.echo(f"\nTotal: {len(schedules)} schedule(s)")

        finally:
            await conn.close()

    asyncio.run(_list())


@schedule.command("show")
@click.argument("name_or_id")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def schedule_show(ctx: click.Context, name_or_id: str, output_json: bool) -> None:
    """Show schedule details"""

    async def _show() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            # Try as ID first, then as name
            try:
                schedule_id = int(name_or_id)
                sched = await api.get_schedule(schedule_id=schedule_id)
            except ValueError:
                sched = await api.get_schedule(name=name_or_id)

            if not sched:
                print_error(f"Schedule not found: {name_or_id}")
                return

            if output_json:
                click.echo(json.dumps(sched, indent=2, default=str))
            else:
                click.echo(f"\n{Colors.BOLD}Schedule: {sched['name']}{Colors.ENDC}")
                click.echo("-" * 60)
                click.echo(f"ID:                    {sched['id']}")
                click.echo(
                    f"Enabled:               {'✓ Yes' if sched['enabled'] else '✗ No'}"
                )
                click.echo(f"Description:           {sched.get('description') or '-'}")
                click.echo(f"\n{Colors.BOLD}Schedule:{Colors.ENDC}")
                click.echo(f"Cron Expression:       {sched['cron_expr']}")
                click.echo(f"Timezone:              {sched['timezone']}")
                click.echo(f"Next Run:              {sched.get('next_run')}")
                click.echo(f"\n{Colors.BOLD}Job Configuration:{Colors.ENDC}")
                click.echo(f"Job Class:             {sched['job_class']}")
                click.echo(f"Queue:                 {sched['queue']}")
                click.echo(f"Priority:              {sched['prio']}")
                click.echo(f"Capability:            {sched.get('capability') or '-'}")
                click.echo(f"Arguments:             {json.dumps(sched['kwargs'])}")
                click.echo(f"\n{Colors.BOLD}Safety Features:{Colors.ENDC}")
                click.echo(f"Max Concurrent Jobs:   {sched['max_concurrent_jobs']}")
                click.echo(f"Jitter (seconds):      {sched['jitter_seconds']}")
                click.echo(
                    f"Backpressure Threshold:{sched.get('backpressure_threshold') or 'None'}"
                )
                click.echo(
                    f"Circuit Breaker:       {sched['circuit_breaker_threshold']} failures"
                )
                click.echo(f"\n{Colors.BOLD}Statistics:{Colors.ENDC}")
                click.echo(f"Total Runs:            {sched['run_count']}")
                click.echo(f"Successes:             {sched['success_count']}")
                click.echo(f"Failures:              {sched['failure_count']}")
                click.echo(f"Skips:                 {sched['skip_count']}")
                click.echo(f"Consecutive Failures:  {sched['consecutive_failures']}")
                click.echo(f"Last Run:              {sched.get('last_run') or 'Never'}")
                click.echo(
                    f"Last Success:          {sched.get('last_success') or 'Never'}"
                )
                click.echo(
                    f"Last Failure:          {sched.get('last_failure') or 'Never'}"
                )

        finally:
            await conn.close()

    asyncio.run(_show())


@schedule.command("add")
@click.argument("name")
@click.argument("job_class")
@click.argument("cron_expr")
@click.option("--queue", "-q", default="default", help="Target queue")
@click.option("--kwargs", help="Job kwargs as JSON")
@click.option("--prio", "-p", type=int, default=100, help="Priority (default: 100)")
@click.option("--capability", help="Required worker capability")
@click.option("--timezone", default="UTC", help="Timezone (default: UTC)")
@click.option(
    "--max-concurrent", type=int, default=1, help="Max concurrent jobs (default: 1)"
)
@click.option(
    "--jitter", type=int, default=0, help="Random jitter in seconds (default: 0)"
)
@click.option(
    "--backpressure",
    type=int,
    default=1000,
    help="Backpressure threshold (default: 1000)",
)
@click.option(
    "--circuit-breaker",
    type=int,
    default=5,
    help="Circuit breaker threshold (default: 5)",
)
@click.option("--description", help="Schedule description")
@click.option("--disabled", is_flag=True, help="Create schedule in disabled state")
@click.pass_context
def schedule_add(
    ctx: click.Context,
    name: str,
    job_class: str,
    cron_expr: str,
    queue: str,
    kwargs: str | None,
    prio: int,
    capability: str | None,
    timezone: str,
    max_concurrent: int,
    jitter: int,
    backpressure: int,
    circuit_breaker: int,
    description: str | None,
    disabled: bool,
) -> None:
    """Create new recurring schedule

    Examples:
        pj-admin schedule add daily-cleanup CleanupJob "0 2 * * *"
        pj-admin schedule add hourly-report ReportJob "0 * * * *" --queue reports
        pj-admin schedule add sync SyncJob "*/5 * * * *" --jitter 60 --max-concurrent 3
    """

    async def _add() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            # Parse kwargs if provided
            job_kwargs = {}
            if kwargs:
                try:
                    job_kwargs = json.loads(kwargs)
                except json.JSONDecodeError as e:
                    print_error(f"Invalid JSON for kwargs: {e}")
                    return

            sched = await api.create_schedule(
                name=name,
                job_class=job_class,
                cron_expr=cron_expr,
                queue=queue,
                kwargs=job_kwargs,
                prio=prio,
                capability=capability,
                timezone=timezone,
                enabled=not disabled,
                max_concurrent_jobs=max_concurrent,
                jitter_seconds=jitter,
                backpressure_threshold=backpressure,
                circuit_breaker_threshold=circuit_breaker,
                description=description,
            )

            print_success(f"✓ Schedule created: {sched['name']} (ID: {sched['id']})")
            click.echo(f"  Next run: {sched['next_run']}")
            click.echo(f"  Cron:     {sched['cron_expr']}")
            click.echo(f"  Queue:    {sched['queue']}")

        except ValueError as e:
            print_error(str(e))
        except Exception as e:
            print_error(f"Failed to create schedule: {e}")
        finally:
            await conn.close()

    asyncio.run(_add())


@schedule.command("enable")
@click.argument("name_or_id")
@click.pass_context
def schedule_enable(ctx: click.Context, name_or_id: str) -> None:
    """Enable a disabled schedule"""

    async def _enable() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            # Try as ID first, then as name
            schedule_id: int | None
            try:
                schedule_id = int(name_or_id)
                sched = await api.get_schedule(schedule_id=schedule_id)
            except ValueError:
                sched = await api.get_schedule(name=name_or_id)
                schedule_id = sched["id"] if sched else None

            if not sched:
                print_error(f"Schedule not found: {name_or_id}")
                return

            assert schedule_id is not None

            await api.enable_schedule(schedule_id)
            print_success(f"✓ Schedule enabled: {sched['name']}")

        except Exception as e:
            print_error(f"Failed to enable schedule: {e}")
        finally:
            await conn.close()

    asyncio.run(_enable())


@schedule.command("disable")
@click.argument("name_or_id")
@click.pass_context
def schedule_disable(ctx: click.Context, name_or_id: str) -> None:
    """Disable an enabled schedule"""

    async def _disable() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            # Try as ID first, then as name
            schedule_id: int | None
            try:
                schedule_id = int(name_or_id)
                sched = await api.get_schedule(schedule_id=schedule_id)
            except ValueError:
                sched = await api.get_schedule(name=name_or_id)
                schedule_id = sched["id"] if sched else None

            if not sched:
                print_error(f"Schedule not found: {name_or_id}")
                return

            assert schedule_id is not None

            await api.disable_schedule(schedule_id)
            print_success(f"✓ Schedule disabled: {sched['name']}")

        except Exception as e:
            print_error(f"Failed to disable schedule: {e}")
        finally:
            await conn.close()

    asyncio.run(_disable())


@schedule.command("delete")
@click.argument("name_or_id")
@click.confirmation_option(prompt="Are you sure you want to delete this schedule?")
@click.pass_context
def schedule_delete(ctx: click.Context, name_or_id: str) -> None:
    """Delete a recurring schedule"""

    async def _delete() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            # Try as ID first, then as name
            schedule_id: int | None
            try:
                schedule_id = int(name_or_id)
                sched = await api.get_schedule(schedule_id=schedule_id)
            except ValueError:
                sched = await api.get_schedule(name=name_or_id)
                schedule_id = sched["id"] if sched else None

            if not sched:
                print_error(f"Schedule not found: {name_or_id}")
                return

            assert schedule_id is not None

            await api.delete_schedule(schedule_id)
            print_success(f"✓ Schedule deleted: {sched['name']}")

        except Exception as e:
            print_error(f"Failed to delete schedule: {e}")
        finally:
            await conn.close()

    asyncio.run(_delete())


@schedule.command("history")
@click.argument("name_or_id")
@click.option("--result", help="Filter by result (success, failure, skipped)")
@click.option("--limit", "-l", default=50, help="Max results (default: 50)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def schedule_history(
    ctx: click.Context,
    name_or_id: str,
    result: str | None,
    limit: int,
    output_json: bool,
) -> None:
    """Show schedule execution history"""

    async def _history() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            # Try as ID first, then as name
            schedule_id: int | None
            try:
                schedule_id = int(name_or_id)
                sched = await api.get_schedule(schedule_id=schedule_id)
            except ValueError:
                sched = await api.get_schedule(name=name_or_id)
                schedule_id = sched["id"] if sched else None

            if not sched:
                print_error(f"Schedule not found: {name_or_id}")
                return

            assert schedule_id is not None

            history = await api.get_schedule_history(
                schedule_id=schedule_id, result_filter=result, limit=limit
            )

            if output_json:
                click.echo(json.dumps(history, indent=2, default=str))
            else:
                if not history:
                    print_warning(f"No execution history for {sched['name']}")
                    return

                click.echo(
                    f"\n{Colors.BOLD}Execution History: {sched['name']}{Colors.ENDC}"
                )
                headers = ["Time", "Result", "Job ID", "Duration", "Details"]
                rows = []
                for h in history:
                    result_icon = {
                        "success": f"{Colors.OKGREEN}✓{Colors.ENDC}",
                        "failure": f"{Colors.FAIL}✗{Colors.ENDC}",
                        "skipped": f"{Colors.WARNING}-{Colors.ENDC}",
                    }.get(h["result"], h["result"])

                    details = ""
                    if h["result"] == "skipped" and h.get("skip_reason"):
                        details = h["skip_reason"]
                    elif h["result"] == "failure" and h.get("error_message"):
                        details = h["error_message"][:40]

                    rows.append(
                        [
                            h["actual_time"].strftime("%Y-%m-%d %H:%M:%S")
                            if h.get("actual_time")
                            else "-",
                            result_icon,
                            str(h.get("job_id") or "-"),
                            f"{h['duration_ms']}ms" if h.get("duration_ms") else "-",
                            details,
                        ]
                    )

                print_table(headers, rows)
                click.echo(f"\nTotal: {len(history)} execution(s)")

        finally:
            await conn.close()

    asyncio.run(_history())


@schedule.command("stats")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def schedule_stats(ctx: click.Context, output_json: bool) -> None:
    """Show execution statistics for all schedules"""

    async def _stats() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            stats = await api.get_schedule_stats()

            if output_json:
                click.echo(json.dumps(stats, indent=2, default=str))
            else:
                if not stats:
                    print_warning("No schedules found")
                    return

                click.echo(f"\n{Colors.BOLD}Schedule Statistics{Colors.ENDC}")
                headers = [
                    "Name",
                    "Enabled",
                    "Runs",
                    "Success",
                    "Fails",
                    "Skips",
                    "Rate",
                    "Next",
                ]
                rows = []
                for s in stats:
                    success_rate = s.get("success_rate_pct")
                    rate_str = (
                        f"{success_rate:.1f}%" if success_rate is not None else "-"
                    )

                    # Color code success rate
                    if success_rate is not None:
                        if success_rate >= 95:
                            rate_str = f"{Colors.OKGREEN}{rate_str}{Colors.ENDC}"
                        elif success_rate >= 80:
                            rate_str = f"{Colors.WARNING}{rate_str}{Colors.ENDC}"
                        else:
                            rate_str = f"{Colors.FAIL}{rate_str}{Colors.ENDC}"

                    rows.append(
                        [
                            s["name"][:25],
                            "✓" if s["enabled"] else "✗",
                            str(s["run_count"]),
                            str(s["success_count"]),
                            str(s["failure_count"]),
                            str(s["skip_count"]),
                            rate_str,
                            s["next_run"].strftime("%m-%d %H:%M")
                            if s.get("next_run")
                            else "-",
                        ]
                    )

                print_table(headers, rows)
                click.echo(f"\nTotal: {len(stats)} schedule(s)")

        finally:
            await conn.close()

    asyncio.run(_stats())


# =========================================================================
# Phase 2: DAG Management Commands
# =========================================================================


@cli.group()
def dag() -> None:
    """Manage DAGs (Directed Acyclic Graphs)"""
    pass


@dag.command("list")
@click.option("--limit", "-l", default=50, help="Max results (default: 50)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def dag_list(ctx: click.Context, limit: int, output_json: bool) -> None:
    """List DAGs"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            # Get DAGs with their status
            dags = await conn.fetch(
                """
                SELECT
                    d.id,
                    d.name,
                    d.created,
                    d.completed,
                    s.total_jobs,
                    s.finished_jobs,
                    s.running_jobs,
                    s.queued_jobs,
                    s.crashed_jobs,
                    s.dag_state,
                    s.completion_percentage
                FROM jorb_dag d
                LEFT JOIN jorb_dag_status s ON s.dag_id = d.id
                ORDER BY d.created DESC
                LIMIT $1
            """,
                limit,
            )

            if output_json:
                # Convert to dict for JSON serialization
                dag_list = [dict(d) for d in dags]
                click.echo(json.dumps(dag_list, indent=2, default=str))
            else:
                if not dags:
                    print_warning("No DAGs found")
                    return

                headers = ["ID", "Name", "State", "Progress", "Jobs", "Created"]
                rows = []
                for d in dags:
                    name = d["name"][:30] if d["name"] else f"DAG-{d['id']}"

                    # State with color
                    state = d["dag_state"] or "unknown"
                    if state == "complete":
                        state_colored = f"{Colors.OKGREEN}{state}{Colors.ENDC}"
                    elif state == "failed":
                        state_colored = f"{Colors.FAIL}{state}{Colors.ENDC}"
                    elif state == "running":
                        state_colored = f"{Colors.OKCYAN}{state}{Colors.ENDC}"
                    else:
                        state_colored = state

                    # Progress
                    pct = d["completion_percentage"] or 0
                    progress = f"{pct:.0f}%"

                    # Job counts
                    total = d["total_jobs"] or 0
                    finished = d["finished_jobs"] or 0
                    jobs_str = f"{finished}/{total}"

                    # Created time
                    created = (
                        d["created"].strftime("%Y-%m-%d %H:%M") if d["created"] else "-"
                    )

                    rows.append(
                        [str(d["id"]), name, state_colored, progress, jobs_str, created]
                    )

                print_table(headers, rows)
                click.echo(f"\nShowing {len(dags)} DAG(s). Use --limit for more.")

        finally:
            await conn.close()

    asyncio.run(_list())


@dag.command("show")
@click.argument("dag_id", type=int)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def dag_show(ctx: click.Context, dag_id: int, output_json: bool) -> None:
    """Show DAG details and job status"""

    async def _show() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            # Get DAG info
            dag = await conn.fetchrow(
                """
                SELECT * FROM jorb_dag_status WHERE dag_id = $1
            """,
                dag_id,
            )

            if not dag:
                print_error(f"DAG {dag_id} not found")
                sys.exit(1)

            # Get jobs in DAG
            jobs = await conn.fetch(
                """
                SELECT
                    id, job_class, state,
                    created, started, finished,
                    waitfor_job, waitfor_group,
                    error_message
                FROM jorb
                WHERE dag_id = $1
                ORDER BY created
            """,
                dag_id,
            )

            if output_json:
                result = {"dag": dict(dag), "jobs": [dict(j) for j in jobs]}
                click.echo(json.dumps(result, indent=2, default=str))
            else:
                name = dag["dag_name"] or f"DAG-{dag_id}"
                click.echo(f"\n{Colors.BOLD}DAG: {name} (ID: {dag_id}){Colors.ENDC}")
                click.echo("-" * 60)

                # Overall status
                state = dag["dag_state"]
                if state == "complete":
                    state_str = f"{Colors.OKGREEN}Complete{Colors.ENDC}"
                elif state == "failed":
                    state_str = f"{Colors.FAIL}Failed{Colors.ENDC}"
                elif state == "running":
                    state_str = f"{Colors.OKCYAN}Running{Colors.ENDC}"
                else:
                    state_str = state

                click.echo(f"State:       {state_str}")
                click.echo(f"Created:     {dag['created']}")
                click.echo(f"Completed:   {dag['completed'] or 'Not yet'}")
                click.echo(f"Progress:    {dag['completion_percentage']:.1f}%")

                click.echo(f"\n{Colors.BOLD}Job Counts:{Colors.ENDC}")
                click.echo(f"Total:       {dag['total_jobs']}")
                click.echo(f"Finished:    {dag['finished_jobs']}")
                click.echo(f"Running:     {dag['running_jobs']}")
                click.echo(f"Queued:      {dag['queued_jobs']}")
                click.echo(f"Crashed:     {dag['crashed_jobs']}")
                click.echo(f"Cancelled:   {dag['cancelled_jobs']}")

                # Job list
                if jobs:
                    click.echo(f"\n{Colors.BOLD}Jobs in DAG:{Colors.ENDC}")
                    headers = ["Job ID", "State", "Job Class", "Dependencies"]
                    rows = []
                    for job in jobs:
                        # State with color
                        state_icon = {
                            "finished": f"{Colors.OKGREEN}✓{Colors.ENDC}",
                            "running": f"{Colors.OKCYAN}▶{Colors.ENDC}",
                            "queued": f"{Colors.WARNING}⏳{Colors.ENDC}",
                            "crashed": f"{Colors.FAIL}✗{Colors.ENDC}",
                            "cancelled": f"{Colors.WARNING}⊘{Colors.ENDC}",
                        }.get(job["state"], job["state"])

                        # Dependencies
                        deps = []
                        if job["waitfor_job"]:
                            deps.append(f"job:{job['waitfor_job']}")
                        if job["waitfor_group"]:
                            deps.append(f"group:{job['waitfor_group']}")
                        deps_str = ", ".join(deps) if deps else "-"

                        rows.append(
                            [
                                str(job["id"]),
                                state_icon,
                                job["job_class"][:30],
                                deps_str[:20],
                            ]
                        )

                    print_table(headers, rows)

        finally:
            await conn.close()

    asyncio.run(_show())


@dag.command("visualize")
@click.argument("dag_id", type=int)
@click.pass_context
def dag_visualize(ctx: click.Context, dag_id: int) -> None:
    """Visualize DAG structure (ASCII art)"""

    async def _visualize() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            # Get DAG dependencies
            deps = await conn.fetch(
                """
                SELECT * FROM get_dag_dependencies($1)
            """,
                dag_id,
            )

            if not deps:
                print_error(f"DAG {dag_id} not found or has no jobs")
                sys.exit(1)

            # Get DAG name
            dag_name = await conn.fetchval(
                """
                SELECT name FROM jorb_dag WHERE id = $1
            """,
                dag_id,
            )

            click.echo(
                f"\n{Colors.BOLD}DAG: {dag_name or f'DAG-{dag_id}'}{Colors.ENDC}"
            )
            click.echo("=" * 60)
            click.echo()

            # Build dependency map
            dep_map = {}
            for row in deps:
                dep_map[row["job_id"]] = {
                    "job_class": row["job_class"],
                    "depends_on": row["depends_on"] or [],
                }

            # Calculate levels (topological sort)
            levels = []
            remaining = set(dep_map.keys())
            in_degree = {
                job_id: len(deps)
                for job_id, deps in [
                    (jid, d["depends_on"]) for jid, d in dep_map.items()
                ]
            }

            while remaining:
                # Find jobs with no remaining dependencies
                level = [job_id for job_id in remaining if in_degree[job_id] == 0]

                if not level:
                    click.echo(
                        f"{Colors.FAIL}ERROR: Cycle detected in DAG!{Colors.ENDC}"
                    )
                    break

                levels.append(level)

                # Remove from remaining and update in-degrees
                for job_id in level:
                    remaining.remove(job_id)
                    # Update dependents
                    for other_id in remaining:
                        if job_id in dep_map[other_id]["depends_on"]:
                            in_degree[other_id] -= 1

            # Display levels
            for level_num, level in enumerate(levels):
                click.echo(f"{Colors.BOLD}Level {level_num}:{Colors.ENDC}")
                for job_id in level:
                    job_info = dep_map[job_id]
                    deps_str = (
                        ", ".join(str(d) for d in job_info["depends_on"]) or "none"
                    )
                    click.echo(f"  • Job {job_id}: {job_info['job_class']}")
                    click.echo(f"    Depends on: {deps_str}")
                click.echo()

            click.echo(f"Total: {len(levels)} level(s), {len(dep_map)} job(s)")

        finally:
            await conn.close()

    asyncio.run(_visualize())


# =========================================================================
# Phase 2: Job Statistics Commands
# =========================================================================


@jobs.command("retry-stats")
@click.option("--queue", "-q", help="Filter by queue")
@click.option(
    "--since-hours", type=int, default=24, help="Hours to look back (default: 24)"
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def jobs_retry_stats(
    ctx: click.Context, queue: str | None, since_hours: int, output_json: bool
) -> None:
    """Show retry statistics (Phase 2)"""

    async def _retry_stats() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            # Build WHERE clause
            where_clauses = ["error_count > 0"]
            params = []

            if since_hours:
                where_clauses.append(
                    "created > now() - $1::interval"
                )
                params.append(f"{since_hours} hours")

            if queue:
                where_clauses.append("queue = $2" if since_hours else "queue = $1")
                params.append(queue)

            where_str = " AND ".join(where_clauses)

            # Get retry statistics
            stats = await conn.fetch(
                f"""
                SELECT
                    admin_data->>'retry_strategy' as strategy,
                    COUNT(*) as job_count,
                    AVG(error_count) as avg_retries,
                    MAX(error_count) as max_retries,
                    COUNT(*) FILTER (WHERE state = 'finished') as eventually_succeeded,
                    COUNT(*) FILTER (WHERE state = 'crashed') as permanently_failed
                FROM jorb
                WHERE {where_str}
                GROUP BY admin_data->>'retry_strategy'
                ORDER BY job_count DESC
            """,
                *params,
            )

            # Get most retried jobs
            top_retries = await conn.fetch(
                f"""
                SELECT
                    id, job_class, queue, state, error_count,
                    admin_data->>'retry_strategy' as strategy,
                    SUBSTRING(error_message, 1, 60) as error_preview
                FROM jorb
                WHERE {where_str}
                ORDER BY error_count DESC
                LIMIT 10
            """,
                *params,
            )

            if output_json:
                result = {
                    "stats_by_strategy": [dict(s) for s in stats],
                    "top_retries": [dict(j) for j in top_retries],
                }
                click.echo(json.dumps(result, indent=2, default=str))
            else:
                click.echo(
                    f"\n{Colors.BOLD}Retry Statistics (last {since_hours}h){Colors.ENDC}"
                )
                if queue:
                    click.echo(f"Queue: {queue}")
                click.echo("-" * 60)

                if stats:
                    click.echo(f"\n{Colors.BOLD}By Retry Strategy:{Colors.ENDC}")
                    headers = [
                        "Strategy",
                        "Jobs",
                        "Avg Retries",
                        "Max",
                        "Succeeded",
                        "Failed",
                    ]
                    rows = []
                    for s in stats:
                        strategy = s["strategy"] or "default"
                        rows.append(
                            [
                                strategy,
                                str(s["job_count"]),
                                f"{s['avg_retries']:.1f}",
                                str(s["max_retries"]),
                                str(s["eventually_succeeded"]),
                                str(s["permanently_failed"]),
                            ]
                        )
                    print_table(headers, rows)
                else:
                    print_warning("No retry data found")

                if top_retries:
                    click.echo(f"\n{Colors.BOLD}Top Retried Jobs:{Colors.ENDC}")
                    for job in top_retries:
                        state_icon = {
                            "finished": f"{Colors.OKGREEN}✓{Colors.ENDC}",
                            "crashed": f"{Colors.FAIL}✗{Colors.ENDC}",
                        }.get(job["state"], job["state"])

                        click.echo(
                            f"\nJob {job['id']} {state_icon} - {job['job_class']}"
                        )
                        click.echo(f"  Retries: {job['error_count']}")
                        click.echo(f"  Strategy: {job['strategy'] or 'default'}")
                        if job["error_preview"]:
                            click.echo(f"  Error: {job['error_preview']}...")

        finally:
            await conn.close()

    asyncio.run(_retry_stats())


@jobs.command("timeout-stats")
@click.option("--queue", "-q", help="Filter by queue")
@click.option(
    "--since-hours", type=int, default=24, help="Hours to look back (default: 24)"
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def jobs_timeout_stats(
    ctx: click.Context, queue: str | None, since_hours: int, output_json: bool
) -> None:
    """Show timeout statistics (Phase 2)"""

    async def _timeout_stats() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            # Build WHERE clause
            where_clauses = ["admin_data ? 'timeout_seconds'"]
            params = []

            if since_hours:
                where_clauses.append(
                    "created > now() - $1::interval"
                )
                params.append(f"{since_hours} hours")

            if queue:
                where_clauses.append("queue = $2" if since_hours else "queue = $1")
                params.append(queue)

            where_str = " AND ".join(where_clauses)

            # Get timeout statistics
            stats = await conn.fetchrow(
                f"""
                SELECT
                    COUNT(*) as total_with_timeout,
                    COUNT(*) FILTER (WHERE state = 'finished') as completed,
                    COUNT(*) FILTER (WHERE state = 'crashed' AND
                                     error_message LIKE '%imeout%') as timed_out,
                    COUNT(*) FILTER (WHERE state = 'running' AND
                                     timeout_at < NOW()) as currently_timed_out,
                    AVG((admin_data->>'timeout_seconds')::int) as avg_timeout_seconds
                FROM jorb
                WHERE {where_str}
            """,
                *params,
            )

            # Get current timeout violations
            violations = await conn.fetch("""
                SELECT * FROM jorb_timeout_violations
                ORDER BY timeout_at
                LIMIT 20
            """)

            # Get jobs that timed out
            timed_out_jobs = await conn.fetch(
                f"""
                SELECT
                    id, job_class, queue, error_count,
                    admin_data->>'timeout_seconds' as timeout_config,
                    admin_data->>'on_timeout' as on_timeout,
                    SUBSTRING(error_message, 1, 60) as error_preview
                FROM jorb
                WHERE {where_str}
                  AND state = 'crashed'
                  AND error_message LIKE '%imeout%'
                ORDER BY created DESC
                LIMIT 10
            """,
                *params,
            )

            if output_json:
                result = {
                    "summary": dict(stats) if stats else {},
                    "current_violations": [dict(v) for v in violations],
                    "recent_timeouts": [dict(j) for j in timed_out_jobs],
                }
                click.echo(json.dumps(result, indent=2, default=str))
            else:
                click.echo(
                    f"\n{Colors.BOLD}Timeout Statistics (last {since_hours}h){Colors.ENDC}"
                )
                if queue:
                    click.echo(f"Queue: {queue}")
                click.echo("-" * 60)

                if stats:
                    click.echo(
                        f"\nJobs with timeout config: {stats['total_with_timeout']}"
                    )
                    click.echo(f"Completed successfully:   {stats['completed']}")
                    click.echo(f"Timed out (crashed):      {stats['timed_out']}")
                    click.echo(
                        f"Avg timeout setting:      {stats['avg_timeout_seconds']:.0f}s"
                    )

                    if (
                        stats["currently_timed_out"]
                        and stats["currently_timed_out"] > 0
                    ):
                        print_warning(
                            f"\n⚠️  Currently timed out:    {stats['currently_timed_out']}"
                        )
                else:
                    print_warning("No timeout data found")

                if violations:
                    print_warning(
                        f"\n{Colors.BOLD}Current Timeout Violations:{Colors.ENDC}"
                    )
                    for v in violations:
                        click.echo(f"\nJob {v['id']} - {v['job_class']}")
                        click.echo(f"  Timeout at: {v['timeout_at']}")
                        click.echo(
                            f"  Config: {v['timeout_seconds']}s, Action: {v['on_timeout']}"
                        )

                if timed_out_jobs:
                    click.echo(f"\n{Colors.BOLD}Recently Timed Out Jobs:{Colors.ENDC}")
                    for job in timed_out_jobs:
                        click.echo(f"\nJob {job['id']} - {job['job_class']}")
                        click.echo(f"  Timeout: {job['timeout_config']}s")
                        click.echo(f"  Action: {job['on_timeout']}")
                        click.echo(f"  Retries: {job['error_count']}")
                        if job["error_preview"]:
                            click.echo(f"  Error: {job['error_preview']}...")

        finally:
            await conn.close()

    asyncio.run(_timeout_stats())


# =========================================================================
# Database Schema Commands
# =========================================================================


@cli.group("db")
def db_group() -> None:
    """Manage the database schema (install / migrate / status)"""
    pass


@db_group.command("migrate")
@click.pass_context
def db_migrate(ctx: click.Context) -> None:
    """Install the base schema if missing, then apply pending migrations"""

    async def _migrate() -> None:
        from . import migrations

        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            applied = await migrations.migrate(conn)
            if applied:
                print_success(f"Applied migrations: {applied}")
            else:
                print_success("Database schema is up to date")
        finally:
            await conn.close()

    asyncio.run(_migrate())


@db_group.command("status")
@click.pass_context
def db_status(ctx: click.Context) -> None:
    """Show applied vs pending schema migrations"""

    async def _status() -> None:
        from . import migrations

        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            info = await migrations.status(conn)
            click.echo(
                f"Base schema installed: {'yes' if info['base_schema_installed'] else 'no'}"
            )
            click.echo(f"Applied migrations:    {info['applied'] or 'none'}")
            click.echo(f"Pending migrations:    {info['pending'] or 'none'}")
        finally:
            await conn.close()

    asyncio.run(_status())


if __name__ == "__main__":
    cli(obj={})
