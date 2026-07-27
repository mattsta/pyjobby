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
from datetime import timedelta
from typing import Any, NoReturn

import asyncpg  # type: ignore[import-untyped]
import click

from . import db, migrations
from .admin_api import UNSET, AdminAPI, Unset
from .client import DEFAULT_PRIO_CEILING, validate_priority
from .configloader import load_config_from_file
from .db import JobState


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


class ConfigProblem(SystemExit):
    """The operator's configuration is wrong -- not the database.

    A SystemExit subclass, so every caller that simply lets it propagate
    exits exactly as before; callers that need to tell the two apart (the
    doctor's per-subsystem report) can catch this specifically.
    """


class DatabaseProblem(SystemExit):
    """The database could not be reached, or refused the operation."""


def fail(
    *messages: str, code: int = 1, problem: type[SystemExit] = SystemExit
) -> NoReturn:
    """Report an operator-facing failure and exit non-zero.

    Every failure path goes through here so a command can never report a
    problem while exiting 0 — scripts chaining `pj-admin ... && next-step`
    depend on that.
    """
    for message in messages:
        print_error(message)
    raise problem(code)


#: The errors PostgreSQL raises when the code addresses an object the database
#: does not have. Every one of them means the same thing here -- this database
#: was installed from a different revision of schema.sql, or from none at all
#: -- and none of them means the operator typed something wrong.
SCHEMA_ERRORS = (
    asyncpg.UndefinedTableError,
    asyncpg.UndefinedColumnError,
    asyncpg.UndefinedFunctionError,
    asyncpg.UndefinedObjectError,
    asyncpg.InvalidSchemaNameError,
)


class PyjobbyCLI(click.Group):
    """The root group, with one job beyond click's: turning a missing or stale
    schema into an answer instead of a stack trace.

    Every command here opens a connection and immediately queries a table,
    column or function that `pj-admin db migrate` is responsible for creating.
    When one of them is absent, asyncpg raises from deep inside the driver and
    the operator gets forty lines of traceback whose last line is
    `column "job_threads" does not exist` -- true, and useless: it names a
    column nobody asked for by name, does not say the schema is out of date,
    and does not name the command that fixes it.

    Catching it HERE rather than in each command is deliberate: click routes
    every subcommand and subgroup invocation through this method, so one
    handler covers `jobs`, `queues`, `workers`, `dag`, `dlq`, `schedule`,
    `stats` and everything added later -- and a new command cannot forget to
    opt in. The exit code is unchanged (1), so scripts that already branch on
    it keep working; only the noise is replaced.
    """

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except SCHEMA_ERRORS as e:
            fail(
                f"The database schema is missing or out of date: {e}",
                "Install or upgrade it with `pj-admin db migrate`, then "
                "confirm with `pj-admin doctor`.",
                problem=DatabaseProblem,
            )


def report_cancel(result: dict[str, Any]) -> None:
    """Report a cancellation truthfully.

    db.cancel_job returns 'cancelled' when the job was stopped outright and
    'cancel_requested' when the request still has to reach a worker; saying
    "cancelled" for the second case tells operators a running job stopped
    when it may not have.
    """
    if result["status"] == "cancel_requested":
        print_warning(
            f"Job {result['job_id']}: cancellation requested "
            f"(running — the worker stops it at its next await point)"
        )
    else:
        print_success(f"Job {result['job_id']} cancelled")


def parse_tags(pairs: tuple[str, ...]) -> dict[str, Any] | None:
    """Turn repeated `--tag key=value` arguments into a tags filter.

    Values go through JSON first so a tag stored as a number or a boolean is
    reachable from a shell (`--tag batch=7` finds 7, not "7"), and anything
    JSON does not recognise stays the plain string it looked like
    (`--tag region=us-east-1`). A value that must be the *string* "7" is
    written the way JSON writes it: `--tag 'batch="7"'`.

    Every malformed form exits non-zero via fail(): a filter the operator
    mistyped must not quietly widen to "all jobs" and report success.
    """
    if not pairs:
        return None

    tags: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            fail(
                f"Malformed --tag {pair!r}: expected key=value",
                "Examples: --tag customer=acme --tag region=us-east-1 --tag batch=7",
            )
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw  # a bare word, which is the common case
        if isinstance(value, dict | list):
            fail(
                f"Malformed --tag {pair!r}: tag values must be a string, "
                "number, boolean or null, not an object or an array",
            )
        tags[key] = value
    return tags


def validate_state(state: str | None) -> str | None:
    """Reject unknown job states before they reach the jorbstate enum."""
    if state is None:
        return None
    valid = [s.value for s in JobState]
    if state not in valid:
        fail(f"Unknown job state: {state!r}", f"Valid states: {', '.join(valid)}")
    return state


async def get_connection(
    config_path: str, dsn: str | None = None
) -> asyncpg.Connection:
    """Get database connection from a DSN (if given) or a config file.

    Config problems and connection problems are reported distinctly: a
    missing or malformed config file is not a database failure, and telling
    an operator otherwise sends them debugging the wrong system.
    """
    db_params: dict[str, Any] | None = None

    if not dsn:
        try:
            config = load_config_from_file(config_path, keys=["db_params"])
        except RuntimeError as e:  # ConfigError: missing, unreadable, or bad
            fail(
                f"Could not load config file: {config_path}",
                str(e),
                "Use --config to point at a pyjobby conf file, or --dsn to "
                "connect directly.",
                problem=ConfigProblem,
            )
        db_params = config.get("db_params")
        if not db_params:
            fail(
                f"No db_params found in config file: {config_path}",
                "Config file must define a db_params dict",
                problem=ConfigProblem,
            )

    try:
        if dsn:
            return await db.connect(dsn)
        assert db_params is not None  # set above when no dsn was given
        return await db.connect(**db_params)
    except Exception as e:
        fail(f"Failed to connect to database: {e}", problem=DatabaseProblem)


# =========================================================================
# Main CLI group
# =========================================================================


@click.group(cls=PyjobbyCLI)
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
@click.option(
    "--tag",
    "tag_pairs",
    multiple=True,
    metavar="KEY=VALUE",
    help="Filter by a job tag; repeat for AND. Matches jobs CONTAINING the "
    "pair, so extra tags on the job are fine. Values are read as JSON when "
    "they look like it (batch=7 matches the number 7; write batch='\"7\"' "
    "for the string).",
)
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
    tag_pairs: tuple[str, ...],
    limit: int,
    offset: int,
    output_json: bool,
) -> None:
    """List jobs with optional filtering"""

    async def _list() -> None:
        validate_state(state)
        # Parsed BEFORE connecting: a mistyped filter is the operator's
        # problem, and reporting it should not depend on the database being
        # reachable.
        tags = parse_tags(tag_pairs)
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            jobs = await api.list_jobs(
                queue=queue,
                state=state,
                job_class=job_class,
                uid=uid,
                tags=tags,
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
                if job["tags"]:
                    click.echo(f"Tags:            {json.dumps(job['tags'])}")
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
                    print_success(f"Job {result['job_id']} requeued for retry")
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
                        print_error(f"Job {result['job_id']}: {result['error']}")
                    else:
                        print_success(f"Job {result['job_id']} requeued")

                click.echo(f"\n{Colors.BOLD}Summary:{Colors.ENDC}")
                print_success(f"  Retried: {success_count}")
                if error_count:
                    # a bulk operation that could not do what was asked must
                    # exit non-zero, exactly like the single-job form
                    fail(f"  Failed: {error_count}")

        finally:
            await conn.close()

    asyncio.run(_retry())


@jobs.command("cancel")
@click.argument("job_ids", nargs=-1, type=int, required=True)
@click.pass_context
def jobs_cancel(ctx: click.Context, job_ids: tuple[int, ...]) -> None:
    """Cancel one or more jobs.

    Queued and waiting jobs are cancelled immediately. A claimed or running
    job gets a cancellation REQUEST delivered to its worker, which stops the
    task at its next await point — reported distinctly, because a job whose
    worker has died stays running with only the request recorded.
    """

    async def _cancel() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            if len(job_ids) == 1:
                # Single job
                try:
                    result = await api.cancel_job(job_ids[0])
                except ValueError as e:
                    fail(str(e))
                report_cancel(result)
            else:
                # Multiple jobs
                results = await api.cancel_jobs(list(job_ids))
                success_count = sum(1 for r in results if r["status"] != "error")
                error_count = len(results) - success_count

                for result in results:
                    if result["status"] == "error":
                        print_error(f"Job {result['job_id']}: {result['error']}")
                    else:
                        report_cancel(result)

                click.echo(f"\n{Colors.BOLD}Summary:{Colors.ENDC}")
                print_success(f"  Cancelled: {success_count}")
                if error_count:
                    fail(f"  Failed: {error_count}")

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


@jobs.command("history")
@click.argument("job_id", type=int)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def jobs_history(ctx: click.Context, job_id: int, output_json: bool) -> None:
    """Show a job's full transition trail (including per-attempt errors)"""

    async def _history() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            history = await api.get_job_history(job_id)

            if output_json:
                click.echo(json.dumps(history, indent=2, default=str))
                return

            if not history:
                print_warning(f"No history for job {job_id}")
                return

            click.echo(f"\n{Colors.BOLD}Job {job_id} History{Colors.ENDC}")
            headers = ["At", "Event", "From", "Epoch", "Errors", "Worker", "Error"]
            rows = []
            for h in history:
                detail = h["detail"] or {}
                worker = (
                    f"{detail['worker_host']}:{detail['worker_pid']}"
                    if detail.get("worker_host")
                    else "-"
                )
                error = detail.get("error") or ""
                if len(error) > 40:
                    error = error[:37] + "..."
                rows.append(
                    [
                        h["at"][:19],
                        h["event"],
                        str(detail.get("from") or "-"),
                        str(detail.get("run_epoch", "-")),
                        str(detail.get("error_count", "-")),
                        worker,
                        error,
                    ]
                )

            print_table(headers, rows, max_width=140)
            click.echo(f"\nTotal: {len(history)} transition(s)")

        finally:
            await conn.close()

    asyncio.run(_history())


@jobs.command("steps")
@click.argument("job_id", type=int)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def jobs_steps(ctx: click.Context, job_id: int, output_json: bool) -> None:
    """Show a job's DXE step checkpoints"""

    async def _steps() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            steps = await api.get_job_steps(job_id)

            if output_json:
                click.echo(json.dumps(steps, indent=2, default=str))
                return

            if not steps:
                print_warning(f"No step checkpoints for job {job_id}")
                return

            click.echo(f"\n{Colors.BOLD}Job {job_id} Steps{Colors.ENDC}")
            headers = ["Seq", "Name", "Epoch", "Status", "Duration", "Error"]
            rows = []
            for s in steps:
                if s["error"]:
                    status = f"{Colors.FAIL}error{Colors.ENDC}"
                elif s["finished"]:
                    status = f"{Colors.OKGREEN}ok{Colors.ENDC}"
                else:
                    status = "in-progress"

                duration = (
                    f"{s['duration_seconds']:.3f}s"
                    if s["duration_seconds"] is not None
                    else "-"
                )
                error = s["error"] or ""
                if len(error) > 40:
                    error = error[:37] + "..."

                rows.append(
                    [
                        str(s["step_seq"]),
                        s["name"],
                        str(s["run_epoch"]),
                        status,
                        duration,
                        error,
                    ]
                )

            print_table(headers, rows, max_width=120)
            click.echo(f"\nTotal: {len(steps)} step(s)")

        finally:
            await conn.close()

    asyncio.run(_steps())


@jobs.command("requeue")
@click.argument("job_id", type=int)
@click.option(
    "--fresh",
    is_flag=True,
    help="Wipe DXE checkpoints first: restart from step 1 instead of resuming",
)
@click.pass_context
def jobs_requeue(ctx: click.Context, job_id: int, fresh: bool) -> None:
    """Requeue a terminal job for another run (also: RESUME an interrupted job)

    By default the job keeps its DXE step checkpoints, so completed steps
    fast-forward and execution resumes where it left off — use this to
    resume interrupted durable jobs. Pass --fresh to wipe the checkpoints
    and restart from step 1.
    """

    async def _requeue() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            result = await api.requeue_job(job_id, fresh=fresh)
            mode = "fresh restart" if result["fresh"] else "resume with checkpoints"
            print_success(f"Job {result['job_id']} requeued ({mode})")
        except ValueError as e:
            print_error(str(e))
            sys.exit(1)
        finally:
            await conn.close()

    asyncio.run(_requeue())


# =========================================================================
# Queue Management Commands
# =========================================================================


@cli.group()
def queues() -> None:
    """Manage queues"""
    pass


def _fmt_limit(value: int | None) -> str:
    """Render an optional numeric limit ('-' means unlimited)."""
    return str(value) if value is not None else "-"


@queues.command("list")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def queues_list(ctx: click.Context, output_json: bool) -> None:
    """List all queues with their pause/limit controls"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            queues = await api.list_queues()

            if output_json:
                click.echo(json.dumps(queues, indent=2, default=str))
                return

            if not queues:
                print_warning("No queues found")
                return

            headers = ["Queue", "Paused", "Max Conc", "Rate Limit", "Rate Period"]
            rows = []
            for q in queues:
                rows.append(
                    [
                        q["name"],
                        "yes" if q["paused"] else "no",
                        _fmt_limit(q["max_concurrency"]),
                        _fmt_limit(q["rate_limit"]),
                        f"{q['rate_period_seconds']:g}s",
                    ]
                )
            print_table(headers, rows)

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
                    "Paused",
                    "Queued",
                    "Running",
                    "Waiting",
                    "Finished",
                    "Crashed",
                    "Total",
                    "Limits",
                ]
                rows = []
                for s in stats:
                    limits = []
                    if s["max_concurrency"] is not None:
                        limits.append(f"conc={s['max_concurrency']}")
                    if s["rate_limit"] is not None:
                        limits.append(
                            f"rate={s['rate_limit']}/{s['rate_period_seconds']:g}s"
                        )
                    rows.append(
                        [
                            s["queue"],
                            "yes" if s["paused"] else "no",
                            str(s["queued"]),
                            str(s["running"]),
                            str(s["waiting"]),
                            str(s["finished"]),
                            str(s["crashed"]),
                            str(s["total"]),
                            ", ".join(limits) or "-",
                        ]
                    )

                print_table(headers, rows, max_width=120)

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
        validate_state(state)
        if not queue.strip():
            fail(
                "Queue name must not be empty",
                "Refusing to run: an empty name filters nothing and would "
                "target every job.",
            )

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


@queues.command("pause")
@click.argument("queue")
@click.pass_context
def queues_pause(ctx: click.Context, queue: str) -> None:
    """Pause a queue (workers stop claiming from it immediately)"""

    async def _pause() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            await api.pause_queue(queue)
            print_success(f"Queue '{queue}' paused")
        finally:
            await conn.close()

    asyncio.run(_pause())


@queues.command("resume")
@click.argument("queue")
@click.pass_context
def queues_resume(ctx: click.Context, queue: str) -> None:
    """Resume a paused queue"""

    async def _resume() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            await api.resume_queue(queue)
            print_success(f"Queue '{queue}' resumed")
        finally:
            await conn.close()

    asyncio.run(_resume())


def _parse_optional_limit(value: str | None, option: str) -> int | None | Unset:
    """Parse an N|none CLI limit value; absent option means 'leave alone'."""
    if value is None:
        return UNSET
    if value.lower() == "none":
        return None
    try:
        return int(value)
    except ValueError:
        print_error(f"{option} must be an integer or 'none' (got '{value}')")
        sys.exit(2)


def _echo_queue_control(control: dict) -> None:
    click.echo(f"Paused:              {'yes' if control['paused'] else 'no'}")
    click.echo(
        f"Max concurrency:     {_fmt_limit(control['max_concurrency'])}"
        " (claimed+running cap; '-' = unlimited)"
    )
    click.echo(
        f"Rate limit:          {_fmt_limit(control['rate_limit'])}"
        f" start(s) per {control['rate_period_seconds']:g}s"
        " ('-' = unlimited)"
    )


@queues.command("limits")
@click.argument("queue")
@click.option(
    "--max-concurrency",
    "max_concurrency",
    default=None,
    help="Cap on claimed+running jobs (integer, or 'none' for unlimited)",
)
@click.option(
    "--rate-limit",
    "rate_limit",
    default=None,
    help="Max job starts per rate period (integer, or 'none' for unlimited)",
)
@click.option(
    "--rate-period",
    "rate_period",
    type=float,
    default=None,
    help="Rate limit window in seconds (default window: 60)",
)
@click.pass_context
def queues_limits(
    ctx: click.Context,
    queue: str,
    max_concurrency: str | None,
    rate_limit: str | None,
    rate_period: float | None,
) -> None:
    """Set (or show, with no options) a queue's concurrency/rate limits

    Only the options you pass are changed; workers enforce the new values
    on their very next claim attempt.
    """
    mc = _parse_optional_limit(max_concurrency, "--max-concurrency")
    rl = _parse_optional_limit(rate_limit, "--rate-limit")

    async def _limits() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)

            if isinstance(mc, Unset) and isinstance(rl, Unset) and rate_period is None:
                # No changes requested: show the current control row.
                control = await api.get_queue_control(queue)
                if not control:
                    print_warning(
                        f"Queue '{queue}' has no control row "
                        "(unpaused, unlimited defaults)"
                    )
                    return
                click.echo(f"\n{Colors.BOLD}Queue '{queue}' controls{Colors.ENDC}")
                _echo_queue_control(control)
                return

            control = await api.set_queue_control(
                queue,
                max_concurrency=mc,
                rate_limit=rl,
                rate_period_seconds=rate_period,
            )
            print_success(f"Queue '{queue}' limits updated")
            _echo_queue_control(control)

        finally:
            await conn.close()

    asyncio.run(_limits())


@queues.command("show")
@click.argument("queue")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def queues_show(ctx: click.Context, queue: str, output_json: bool) -> None:
    """Show one queue's controls and statistics"""

    async def _show() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            control = await api.get_queue_control(queue)
            stats = await api.queue_stats(queue=queue)

            if output_json:
                click.echo(
                    json.dumps(
                        {"control": control, "stats": stats},
                        indent=2,
                        default=str,
                    )
                )
                return

            if not control and not stats:
                print_warning(f"Queue '{queue}' has no jobs and no control row")
                return

            click.echo(f"\n{Colors.BOLD}Queue '{queue}'{Colors.ENDC}")
            click.echo("-" * 50)
            if control:
                _echo_queue_control(control)
            else:
                click.echo("No control row (unpaused, unlimited defaults)")

            if stats:
                s = stats[0]
                click.echo(f"\n{Colors.BOLD}Depths:{Colors.ENDC}")
                for state in (
                    "queued",
                    "claimed",
                    "running",
                    "waiting",
                    "finished",
                    "crashed",
                    "cancelled",
                ):
                    click.echo(f"  {state:12} {s[state]}")
                click.echo(f"  {'total':12} {s['total']}")
                if s.get("oldest_queued_age_seconds"):
                    age_min = int(s["oldest_queued_age_seconds"]) // 60
                    click.echo(f"\nOldest queued job: {age_min} minute(s) old")

        finally:
            await conn.close()

    asyncio.run(_show())


# =========================================================================
# Worker Management Commands
# =========================================================================


@cli.group()
def workers() -> None:
    """Manage workers"""
    pass


def _worker_status(worker: dict) -> str:
    """Human status of a registry row: live / not claiming / stale / shutdown.

    "not claiming" outranks "live" because it is the more specific truth: the
    worker is beating and would show up as fleet capacity, but abandoned job
    threads fill its pool and it is claiming nothing (see `doctor`'s
    job-threads check)."""
    if worker["shutdown_at"]:
        return "shutdown"
    if not worker["live"]:
        return "stale"
    return "not claiming" if worker["not_claiming"] else "live"


def _worker_threads(worker: dict) -> str:
    """`abandoned/pool` for a registry row, or `-` if it reported no pool."""
    if not worker["job_threads"]:
        return "-"
    return f"{worker['job_threads_abandoned']}/{worker['job_threads']}"


def _fmt_age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds:.0f}s ago"
    if seconds < 7200:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


@workers.command("list")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def workers_list(ctx: click.Context, output_json: bool) -> None:
    """List registered workers (live and recently dead)"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            api = AdminAPI(conn)
            workers = await api.list_workers()

            if output_json:
                click.echo(json.dumps(workers, indent=2))
            else:
                if not workers:
                    print_warning("No registered workers")
                    return

                headers = [
                    "ID",
                    "Host",
                    "PID",
                    "Queue",
                    "Status",
                    "Threads",
                    "Last Seen",
                    "Current Job",
                ]
                rows = []
                for w in workers:
                    job = (
                        f"{w['current_job_id']} ({w['current_job_class']})"
                        if w["current_job_id"]
                        else "-"
                    )
                    rows.append(
                        [
                            str(w["id"]),
                            w["host"],
                            str(w["pid"]),
                            w["queue"],
                            _worker_status(w),
                            _worker_threads(w),
                            _fmt_age(w["last_seen_age_seconds"]),
                            job,
                        ]
                    )

                print_table(headers, rows, max_width=120)

        finally:
            await conn.close()

    asyncio.run(_list())


@workers.command("stats")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def workers_stats(ctx: click.Context, output_json: bool) -> None:
    """Show worker registry statistics"""

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
                click.echo(f"Live workers:      {stats['live_workers']}")
                click.echo(f"Stale workers:     {stats['stale_workers']}")
                click.echo(f"Shut down:         {stats['shutdown_workers']}")
                click.echo(f"Total registered:  {stats['total_registered']}")

                if stats["per_queue"]:
                    click.echo(f"\n{Colors.BOLD}Live Workers by Queue:{Colors.ENDC}")
                    headers = ["Queue", "Live Workers"]
                    rows = [
                        [queue, str(count)]
                        for queue, count in sorted(stats["per_queue"].items())
                    ]
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


def _fmt_bytes(num: float) -> str:
    """Byte count in the largest unit that keeps it readable."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}TB"


def _fmt_duration(seconds: float) -> str:
    """A wait, in the coarsest unit that still reads honestly."""
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


@cli.command()
@click.option("--queue", "-q", help="Filter by queue")
@click.option(
    "--since-hours",
    type=click.IntRange(min=1),
    default=24,
    help="Hours to look back (default: 24)",
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
            since = db.utcnow() - timedelta(hours=since_hours)
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

                # Throughput first and next to arrivals, because the
                # comparison IS the answer: arrivals sustained above
                # completions is what "falling behind" means, and neither
                # number says it alone.
                throughput = metrics_data["throughput_per_second"]
                arrivals = metrics_data["arrival_rate_per_second"]
                click.echo(f"Throughput:        {throughput:.2f} jobs/s (completed)")
                click.echo(f"Arrivals:          {arrivals:.2f} jobs/s (created)")
                verdict = "falling behind" if arrivals > throughput else "keeping up"
                click.echo(
                    f"Balance:           {arrivals - throughput:+.2f} jobs/s ({verdict})"
                )
                click.echo(
                    f"Retry Pressure:    "
                    f"{metrics_data['retry_rate_per_second']:.2f} attempts/s"
                )
                click.echo(
                    f"DLQ Growth:        "
                    f"{metrics_data['dlq_growth_per_second']:.4f} jobs/s"
                )
                click.echo(f"Finished:          {metrics_data['finished_count']}")
                click.echo(f"Crashed:           {metrics_data['crashed_count']}")
                click.echo(f"Cancelled:         {metrics_data['cancelled_count']}")
                click.echo(
                    f"Avg Duration:      {metrics_data['avg_duration_seconds']:.2f}s"
                )
                click.echo(
                    f"Avg Queue Wait:    {metrics_data['avg_wait_seconds']:.2f}s"
                )
                click.echo(
                    f"Max Queue Wait:    {metrics_data['max_wait_seconds']:.2f}s"
                )

                backlog = metrics_data["backlog"]
                inflight = metrics_data["inflight"]
                click.echo(
                    f"Backlog:           {backlog['depth']} claimable, "
                    f"oldest ready {_fmt_duration(backlog['oldest_age_seconds'])}"
                )
                click.echo(
                    f"In Flight:         {inflight['inflight']} "
                    f"({inflight['stuck']} stuck > "
                    f"{_fmt_duration(inflight['stuck_after_seconds'])}, "
                    f"oldest {_fmt_duration(inflight['oldest_age_seconds'])})"
                )

                # The cliff: at 1.0 every NOTIFY-issuing transaction fails,
                # which means no job can be enqueued or completed anywhere.
                usage = metrics_data["notify_queue_usage"]
                click.echo(f"NOTIFY Queue:      {usage:.1%} used")

                storage = metrics_data["storage"]
                click.echo(
                    f"Dead Tuples:       {storage['dead_tuple_ratio']:.1%} of jorb"
                )

                if backlog.get("per_queue"):
                    click.echo(f"\n{Colors.BOLD}Backlog by Queue:{Colors.ENDC}")
                    for qname, stats in sorted(backlog["per_queue"].items()):
                        click.echo(
                            f"  {qname:20} depth {stats['depth']:<8} "
                            f"oldest ready "
                            f"{_fmt_duration(stats['oldest_age_seconds'])}"
                        )

                if storage.get("tables"):
                    click.echo(f"\n{Colors.BOLD}Storage:{Colors.ENDC}")
                    for tname, stats in sorted(storage["tables"].items()):
                        click.echo(
                            f"  {tname:14} {_fmt_bytes(stats['total_bytes']):>9} "
                            f"total ({_fmt_bytes(stats['table_bytes'])} table + "
                            f"{_fmt_bytes(stats['index_bytes'])} index), "
                            f"dead {stats['dead_tuple_ratio']:.1%}"
                        )

                if metrics_data.get("state_counts"):
                    click.echo(
                        f"\n{Colors.BOLD}Jobs Created in Window, by State:{Colors.ENDC}"
                    )
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
                fail(f"Schedule not found: {name_or_id}")

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
@click.option(
    "--prio",
    "-p",
    type=int,
    default=100,
    help=(
        "Priority, LOWER is MORE urgent (default: 100). Refused above the "
        "worker priority ceiling -- see --max-prio"
    ),
)
@click.option(
    "--max-prio",
    type=int,
    default=DEFAULT_PRIO_CEILING,
    help=(
        f"The priority ceiling this fleet's workers run with (`pj "
        f"--max-prio`, default {DEFAULT_PRIO_CEILING}). --prio above it is "
        "refused: every firing would mint a job no worker can claim"
    ),
)
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
    max_prio: int,
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
    # Checked before a connection is opened, because this failure is about
    # the operator's arguments and not the database. The predicate and the
    # message are the client's, so the schedule door and the enqueue door
    # cannot drift; only the hint is CLI-shaped, since `JobClient(...)` is
    # not the thing an operator typing this command would change.
    try:
        validate_priority(prio, max_prio)
    except ValueError as e:
        fail(
            str(e),
            f"If this fleet really runs `pj --max-prio {prio}` (or higher), "
            f"say so here too: `pj-admin schedule add ... --prio {prio} "
            f"--max-prio {prio}`.",
        )

    async def _add() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        problem: str | None = None
        try:
            api = AdminAPI(conn, prio_ceiling=max_prio)

            # Parse kwargs if provided
            job_kwargs = {}
            if kwargs:
                try:
                    job_kwargs = json.loads(kwargs)
                except json.JSONDecodeError as e:
                    fail(f"Invalid JSON for kwargs: {e}")

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
            # invalid cron expression, unknown timezone, or a priority above
            # the ceiling (rejected up front, but the API checks it too)
            problem = str(e)
        except Exception as e:
            # duplicate name, constraint violation, ...
            problem = f"Failed to create schedule: {e}"
        finally:
            await conn.close()

        # reported after the connection is released, so a failing add still
        # exits non-zero without leaking the connection
        if problem is not None:
            fail(problem)

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
                fail(f"Schedule not found: {name_or_id}")

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
                fail(f"Schedule not found: {name_or_id}")

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
                fail(f"Schedule not found: {name_or_id}")

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
                fail(f"Schedule not found: {name_or_id}")

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


def _dag_state(status: dict) -> str:
    """Derive a DAG's overall state from the jorb_dag_status counts."""
    total = status["total_jobs"] or 0
    if total == 0:
        return "empty"
    if status["crashed_jobs"]:
        return "failed"
    if (status["pending_jobs"] or 0) > 0:
        return "running"
    return "complete"


def _dag_completion_pct(status: dict) -> float:
    """Finished-job percentage from the jorb_dag_status counts."""
    total = status["total_jobs"] or 0
    if total == 0:
        return 0.0
    return 100.0 * (status["finished_jobs"] or 0) / total


@dag.command("list")
@click.option("--limit", "-l", default=50, help="Max results (default: 50)")
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def dag_list(ctx: click.Context, limit: int, output_json: bool) -> None:
    """List DAGs"""

    async def _list() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            # Get DAGs with their status (jorb_dag_status view)
            dags = await conn.fetch(
                """
                SELECT
                    dag_id AS id,
                    name,
                    created,
                    completed,
                    total_jobs,
                    finished_jobs,
                    crashed_jobs,
                    cancelled_jobs,
                    pending_jobs
                FROM jorb_dag_status
                ORDER BY created DESC
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
                    state = _dag_state(d)
                    if state == "complete":
                        state_colored = f"{Colors.OKGREEN}{state}{Colors.ENDC}"
                    elif state == "failed":
                        state_colored = f"{Colors.FAIL}{state}{Colors.ENDC}"
                    elif state == "running":
                        state_colored = f"{Colors.OKCYAN}{state}{Colors.ENDC}"
                    else:
                        state_colored = state

                    # Progress
                    progress = f"{_dag_completion_pct(d):.0f}%"

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
                name = dag["name"] or f"DAG-{dag_id}"
                click.echo(f"\n{Colors.BOLD}DAG: {name} (ID: {dag_id}){Colors.ENDC}")
                click.echo("-" * 60)

                # Overall status
                state = _dag_state(dict(dag))
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
                click.echo(f"Progress:    {_dag_completion_pct(dict(dag)):.1f}%")

                click.echo(f"\n{Colors.BOLD}Job Counts:{Colors.ENDC}")
                click.echo(f"Total:       {dag['total_jobs']}")
                click.echo(f"Finished:    {dag['finished_jobs']}")
                click.echo(f"Pending:     {dag['pending_jobs']}")
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
            # Get DAG jobs with their dependency edges (jorb_dependencies
            # plus waitfor_job; the old get_dag_dependencies() SQL function
            # no longer exists)
            deps = await conn.fetch(
                """
                SELECT j.id AS job_id,
                       j.job_class,
                       ARRAY(
                           SELECT d.depends_on FROM jorb_dependencies d
                           WHERE d.job_id = j.id
                           UNION
                           SELECT j.waitfor_job WHERE j.waitfor_job IS NOT NULL
                       ) AS depends_on
                FROM jorb j
                WHERE j.dag_id = $1
                ORDER BY j.id
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
                    # A cyclic DAG can never run. Rendering the acyclic part
                    # and exiting 0 would tell a script it is fine, so name
                    # the jobs still in the cycle and fail.
                    stuck = ", ".join(str(job_id) for job_id in sorted(remaining))
                    fail(
                        "Cycle detected in DAG: these jobs depend on each "
                        f"other and can never run: {stuck}"
                    )

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
    "--since-hours",
    type=click.IntRange(min=1),
    default=24,
    help="Hours to look back (default: 24)",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def jobs_retry_stats(
    ctx: click.Context, queue: str | None, since_hours: int, output_json: bool
) -> None:
    """Show retry statistics from the jorb_history audit trail

    An attempt is a 'running' event in jorb_history; jobs with more than
    one attempt were retried (retries requeue the same row).
    """

    async def _retry_stats() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            # Build WHERE clause over the joined job rows
            where_clauses = ["a.attempts > 1"]
            params: list = []

            if since_hours:
                params.append(timedelta(hours=since_hours))
                where_clauses.append(f"j.created > now() - ${len(params)}::interval")

            if queue:
                params.append(queue)
                where_clauses.append(f"j.queue = ${len(params)}")

            where_str = " AND ".join(where_clauses)
            attempts_cte = """
                WITH a AS (
                    SELECT job_id, COUNT(*) AS attempts
                    FROM jorb_history
                    WHERE event = 'running'
                    GROUP BY job_id
                )
            """

            # Aggregate retried jobs by job class
            stats = await conn.fetch(
                f"""
                {attempts_cte}
                SELECT
                    j.job_class,
                    COUNT(*) as job_count,
                    AVG(a.attempts) as avg_attempts,
                    MAX(a.attempts) as max_attempts,
                    COUNT(*) FILTER (WHERE j.state = 'finished')
                        as eventually_succeeded,
                    COUNT(*) FILTER (WHERE j.state = 'crashed')
                        as permanently_failed
                FROM jorb j
                JOIN a ON a.job_id = j.id
                WHERE {where_str}
                GROUP BY j.job_class
                ORDER BY job_count DESC
            """,
                *params,
            )

            # Most retried individual jobs
            top_retries = await conn.fetch(
                f"""
                {attempts_cte}
                SELECT
                    j.id, j.job_class, j.queue, j.state, j.error_count,
                    a.attempts,
                    SUBSTRING(j.error_message, 1, 60) as error_preview
                FROM jorb j
                JOIN a ON a.job_id = j.id
                WHERE {where_str}
                ORDER BY a.attempts DESC
                LIMIT 10
            """,
                *params,
            )

            if output_json:
                result = {
                    "stats_by_job_class": [dict(s) for s in stats],
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
                    click.echo(f"\n{Colors.BOLD}Retried Jobs by Class:{Colors.ENDC}")
                    headers = [
                        "Job Class",
                        "Jobs",
                        "Avg Attempts",
                        "Max",
                        "Succeeded",
                        "Failed",
                    ]
                    rows = []
                    for s in stats:
                        rows.append(
                            [
                                s["job_class"],
                                str(s["job_count"]),
                                f"{s['avg_attempts']:.1f}",
                                str(s["max_attempts"]),
                                str(s["eventually_succeeded"]),
                                str(s["permanently_failed"]),
                            ]
                        )
                    print_table(headers, rows)
                else:
                    print_warning("No retried jobs found")

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
                        click.echo(f"  Attempts: {job['attempts']}")
                        click.echo(f"  Errors:   {job['error_count']}")
                        if job["error_preview"]:
                            click.echo(f"  Error: {job['error_preview']}...")

        finally:
            await conn.close()

    asyncio.run(_retry_stats())


@jobs.command("timeout-stats")
@click.option("--queue", "-q", help="Filter by queue")
@click.option(
    "--since-hours",
    type=click.IntRange(min=1),
    default=24,
    help="Hours to look back (default: 24)",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON")
@click.pass_context
def jobs_timeout_stats(
    ctx: click.Context, queue: str | None, since_hours: int, output_json: bool
) -> None:
    """Show timeout statistics (from jorb.timeout_at/state)"""

    async def _timeout_stats() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            # Build WHERE clause (positional params numbered as appended;
            # asyncpg binds intervals from timedelta, never from a string)
            where_clauses = ["admin_data ? 'timeout_seconds'"]
            params: list[Any] = []

            if since_hours:
                params.append(timedelta(hours=since_hours))
                where_clauses.append(f"created > now() - ${len(params)}::interval")

            if queue:
                params.append(queue)
                where_clauses.append(f"queue = ${len(params)}")

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

            # Running jobs past their deadline (plain query over
            # timeout_at/state; the old jorb_timeout_violations view is gone)
            violations = await conn.fetch("""
                SELECT
                    id, job_class, queue, timeout_at,
                    admin_data->>'timeout_seconds' as timeout_seconds,
                    admin_data->>'on_timeout' as on_timeout
                FROM jorb
                WHERE state = 'running'
                  AND timeout_at IS NOT NULL
                  AND timeout_at < now()
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

                if stats and stats["total_with_timeout"]:
                    click.echo(
                        f"\nJobs with timeout config: {stats['total_with_timeout']}"
                    )
                    click.echo(f"Completed successfully:   {stats['completed']}")
                    click.echo(f"Timed out (crashed):      {stats['timed_out']}")
                    avg = stats["avg_timeout_seconds"]
                    if avg is not None:
                        click.echo(f"Avg timeout setting:      {avg:.0f}s")

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
# Doctor (health checks)
# =========================================================================


#: Every trigger schema.sql installs, checked by name. Not a second list: it
#: IS the migration runner's manifest, so a trigger added to the schema is
#: checked here the moment it is declared there.
DOCTOR_REQUIRED_TRIGGERS = migrations.REQUIRED_TRIGGERS

#: How many missing objects doctor names before summarising. A database
#: installed two releases ago is missing dozens; the operator needs to see
#: that it is stale and what to run, not a wall of catalog names.
DOCTOR_MISSING_NAMED = 5


def missing_shape_summary(missing: list[str]) -> str:
    """doctor's FAIL line for a database whose schema is the wrong shape."""
    named = ", ".join(missing[:DOCTOR_MISSING_NAMED])
    if len(missing) > DOCTOR_MISSING_NAMED:
        named += f", and {len(missing) - DOCTOR_MISSING_NAMED} more"
    return (
        f"installed, but {len(missing)} object(s) this release needs are "
        f"missing: {named} ({migrations.MIGRATE_REMEDY})"
    )


# Fill fractions of PostgreSQL's shared async-NOTIFY queue at which doctor
# stops calling it healthy. The queue is server-wide and bounded, and it
# drains only as fast as the SLOWEST connected listener -- so a single
# listening session that has stopped consuming fills it for everyone. At 1.0
# every transaction that issues a NOTIFY fails, and pyjobby issues one on
# enqueue, on every state transition, and on completion: no job can be
# enqueued or completed anywhere in the system.
#
# The thresholds are low on purpose. This is a cliff, not a gradient --
# nothing degrades on the way up, so the only useful warning is an early one.
DOCTOR_NOTIFY_WARN = 0.25
DOCTOR_NOTIFY_FAIL = 0.5

# What to actually DO about it, since the number alone tells an operator
# nothing. There is no volume lever left to pull: the per-transition dashboard
# feed that used to be the obvious thing to disable is gone (the websocket
# server polls aggregates instead), and every remaining channel is gated on a
# consumer that has registered demand -- so what is in the queue is what
# somebody asked for, and all of it is load-bearing. That makes this check
# purely a consumer problem: something LISTENed and stopped reading.
DOCTOR_NOTIFY_REMEDY = (
    "a listening session has stopped draining it -- find it with "
    '"SELECT pid, state, query FROM pg_stat_activity WHERE wait_event = '
    "'NotifyQueue' OR query ILIKE '%LISTEN%'\" and disconnect it. There is no "
    "volume to trim: every remaining channel is demand-gated, so anything in "
    "the queue has a consumer waiting for it (enqueue/done/cancel/event are "
    "all load-bearing and must stay enabled)"
)


# A worker that is registered, heartbeating, and claiming nothing. WARN and
# not FAIL, deliberately, and the ladder this doctor already uses is the
# argument: FAIL is reserved for "the platform cannot function" (no schema,
# pending migrations, missing NOTIFY triggers, a NOTIFY queue past half full),
# while losing capacity is a WARN -- "no live workers AT ALL" is a WARN here.
# One worker of ten refusing to claim cannot be graver than all ten being
# gone. It is also self-healing by construction: the abandoned threads finish
# on their own and the worker resumes, so a FAIL would exit 1 on a condition
# that may already be over, and doctor's exit code is what wakes people up.
# If the refusal really is costing throughput, the backlog check below says so
# in the unit that matters, from the queue's side.
DOCTOR_THREADS_REMEDY = (
    "they heartbeat normally and count as live capacity, but they claim "
    "nothing: synchronous jobs that exceeded their deadline left threads "
    "behind, and a running thread cannot be interrupted. A worker whose pool "
    "is full of them refuses to claim rather than admit a job it cannot "
    "start, and recovers by itself once they finish. If it does not recover, "
    'find the job class ("pj-admin dlq list", error "Job timed out") and give '
    "it a shorter timeout, an interruptible implementation, or its own queue "
    "and worker -- raising --job-threads only buys tolerance, it does not "
    "make those threads stoppable"
)

# How many of them doctor names before summarising. Enough to see whether it
# is one bad host or the whole fleet, short enough to stay one report line.
DOCTOR_THREADS_NAMED = 3


def stuck_worker_summary(rows: list[asyncpg.Record]) -> str:
    """Name the workers that are refusing to claim, for doctor's WARN line."""
    named = "; ".join(
        f"worker {r['id']} ({r['host']}:{r['pid']}, queue {r['queue']}) "
        f"{r['job_threads_abandoned']}/{r['job_threads']} job threads abandoned"
        for r in rows[:DOCTOR_THREADS_NAMED]
    )
    if len(rows) > DOCTOR_THREADS_NAMED:
        named += f"; and {len(rows) - DOCTOR_THREADS_NAMED} more"
    return named


def notify_queue_verdict(usage: float) -> tuple[str, str]:
    """Grade a NOTIFY-queue fill fraction into (status, message).

    Split out from `doctor` so the thresholds are testable: the queue is
    server-wide and 8GB by default, so no test can honestly fill it, and a
    check whose WARN and FAIL branches have never been executed is a check
    that has not been written.
    """
    usage_pct = f"{usage:.1%} full"
    if usage > DOCTOR_NOTIFY_FAIL:
        return (
            "FAIL",
            f"{usage_pct} -- at 100% every enqueue and completion fails "
            f"platform-wide; {DOCTOR_NOTIFY_REMEDY}",
        )
    if usage >= DOCTOR_NOTIFY_WARN:
        return (
            "WARN",
            f"{usage_pct} and it should be near empty -- {DOCTOR_NOTIFY_REMEDY}",
        )
    return "PASS", usage_pct


class _Doctor:
    """Accumulates PASS/WARN/FAIL check lines for `pj-admin doctor`."""

    def __init__(self) -> None:
        self.failed = False

    def report(self, status: str, name: str, message: str) -> None:
        color = {
            "PASS": Colors.OKGREEN,
            "WARN": Colors.WARNING,
            "FAIL": Colors.FAIL,
        }[status]
        if status == "FAIL":
            self.failed = True
        click.echo(f"{color}{status}{Colors.ENDC} {name}: {message}")

    def check(self, ok: bool, name: str, ok_msg: str, fail_msg: str) -> None:
        if ok:
            self.report("PASS", name, ok_msg)
        else:
            self.report("FAIL", name, fail_msg)

    def warn_if(self, bad: bool, name: str, ok_msg: str, warn_msg: str) -> None:
        if bad:
            self.report("WARN", name, warn_msg)
        else:
            self.report("PASS", name, ok_msg)


@cli.command()
@click.option(
    "--max-depth",
    type=int,
    default=10000,
    help="WARN when a queue's backlog exceeds this many queued jobs",
)
@click.option(
    "--max-age-minutes",
    type=int,
    default=60,
    help="WARN when a queue's oldest queued job is older than this",
)
@click.pass_context
def doctor(ctx: click.Context, max_depth: int, max_age_minutes: int) -> None:
    """Run health checks against the job platform (exit 1 on any FAIL)

    Checks: database reachability, schema/migrations, NOTIFY triggers,
    NOTIFY queue saturation, live workers, workers that are alive but
    claiming nothing, queue backlogs, the DLQ, and overdue schedules.
    """

    async def _doctor() -> int:
        doc = _Doctor()

        try:
            conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        except ConfigProblem:
            # get_connection printed the specific reason; blaming the database
            # here would send the operator to debug the wrong system.
            doc.report("FAIL", "config", "unusable")
            return 1
        except SystemExit:
            doc.report("FAIL", "database", "unreachable")
            return 1
        doc.report("PASS", "database", "connected")

        try:
            # THE SCHEMA CHECK, and the one that used to be a lie. It asked
            # two questions -- is `jorb` there, and does schema_migrations
            # record a version this release does not ship -- and a database
            # installed from an OLDER schema.sql answers both the way a
            # healthy one does: jorb exists, and it records nothing at all, so
            # nothing is "pending". Doctor printed PASS schema and the very
            # next check died on `column "job_threads" does not exist`.
            #
            # So it now asks what the operator was actually asking: can the
            # code that is running address this database? That is the SHAPE --
            # the tables, columns, functions, views, indexes and enum labels
            # the release needs -- read out of the catalog and compared
            # against migrations.py's manifest, which is machine-checked
            # against a fresh install by the test suite.
            #
            # Any FAIL here returns immediately, exactly as "no schema at all"
            # always did: every check below this line queries a column or
            # function this one just reported missing, and a health report
            # that crashes halfway through is worse than one that stops.
            info = await migrations.status(conn)
            if not info["base_schema_installed"]:
                doc.report(
                    "FAIL",
                    "schema",
                    f"base schema not installed ({migrations.MIGRATE_REMEDY})",
                )
                return 1  # nothing else can be checked
            if info["missing"]:
                doc.report("FAIL", "schema", missing_shape_summary(info["missing"]))
                return 1
            if info["pending"]:
                # PASS, and the reasoning is the same one that made the check
                # above a FAIL. This check answers "can the code that is
                # running address this database", and the shape check just
                # proved that every object the pending files install is
                # already here -- so the answer is yes, and only the
                # BOOKKEEPING is behind. That is what a database installed
                # from the current schema.sql by a release that did not record
                # migrations looks like, and waking someone at 3am over a
                # missing row in schema_migrations is how a health probe
                # teaches people to ignore it.
                #
                # It is still said out loud, because the record is what the
                # NEXT upgrade reads: until `db migrate` runs, a later release
                # cannot tell this database from one that never applied the
                # migration at all.
                doc.report(
                    "PASS",
                    "schema",
                    f"installed and complete; migrations {info['pending']} "
                    f"are not recorded yet, which the next upgrade reads "
                    f"({migrations.MIGRATE_REMEDY})",
                )
            else:
                applied = info["applied"] or "baseline"
                doc.report(
                    "PASS", "schema", f"installed, migrations current ({applied})"
                )

            # Triggers, checked by name for the same reason the shape is:
            # nothing raises when one is missing, the platform just stops
            # doing something. A dropped jorb_history_record loses the audit
            # trail silently; a dropped NOTIFY trigger degrades every waiter
            # to its polling fallback.
            rows = await conn.fetch(
                "SELECT tgname FROM pg_trigger WHERE tgname = ANY($1::text[])",
                list(DOCTOR_REQUIRED_TRIGGERS),
            )
            present = {r["tgname"] for r in rows}
            missing = [t for t in DOCTOR_REQUIRED_TRIGGERS if t not in present]
            doc.check(
                not missing,
                "triggers",
                f"all schema triggers present ({len(DOCTOR_REQUIRED_TRIGGERS)})",
                f"missing triggers: {', '.join(missing)} ({migrations.MIGRATE_REMEDY})",
            )

            # NOTIFY queue saturation. Checked right after the triggers that
            # fill it, and before anything about jobs, because when this one
            # goes nothing else in the report can still be true: enqueue and
            # completion both fail outright at 1.0.
            notify_usage = float(
                await conn.fetchval("SELECT pg_notification_queue_usage()") or 0.0
            )
            status, message = notify_queue_verdict(notify_usage)
            doc.report(status, "notify-queue", message)

            # Live workers (heartbeats arrive every ~10s)
            live_workers = await conn.fetchval("""
                SELECT COUNT(*) FROM jorb_worker
                WHERE shutdown_at IS NULL
                  AND last_seen > now() - interval '60 seconds'
            """)
            doc.warn_if(
                not live_workers,
                "workers",
                f"{live_workers} live worker(s) seen in last 60s",
                "no live workers seen in last 60s",
            )

            # Live workers that are claiming nothing. Checked immediately
            # after the count above because it is the count above that is
            # misleading: these workers are IN it. Every other health signal
            # the platform has -- the heartbeat, the metrics endpoint, the
            # dashboard, this doctor's own worker check -- reads them as fleet
            # capacity, and until this check existed the condition was only
            # discoverable in one worker's log.
            #
            # One row per worker, filtered on the same live predicate
            # jorb_worker_live_idx exists for: bounded by fleet size, never by
            # the job table.
            stuck = await conn.fetch("""
                SELECT id, host, pid, queue, job_threads, job_threads_abandoned
                FROM jorb_worker
                WHERE shutdown_at IS NULL
                  AND last_seen > now() - interval '60 seconds'
                  AND job_threads > 0
                  AND job_threads_abandoned >= job_threads
                ORDER BY id
            """)
            doc.warn_if(
                bool(stuck),
                "job-threads",
                f"{live_workers} live worker(s) claiming",
                f"{len(stuck)} of {live_workers} live worker(s) not "
                f"claiming -- {stuck_worker_summary(stuck)}. "
                f"{DOCTOR_THREADS_REMEDY}",
            )

            # Queue backlogs
            backlogs = await conn.fetch("""
                SELECT queue,
                       COUNT(*) AS depth,
                       EXTRACT(EPOCH FROM (now() - MIN(run_after))) / 60.0
                           AS oldest_minutes
                FROM jorb
                WHERE state = 'queued'
                GROUP BY queue
                ORDER BY queue
            """)
            if not backlogs:
                doc.report("PASS", "queues", "no queued jobs")
            for b in backlogs:
                oldest = max(b["oldest_minutes"] or 0.0, 0.0)
                summary = f"depth {b['depth']}, oldest queued {oldest:.0f}m"
                doc.warn_if(
                    b["depth"] > max_depth or oldest > max_age_minutes,
                    f"queue {b['queue']}",
                    summary,
                    f"{summary} (thresholds: depth {max_depth}, "
                    f"age {max_age_minutes}m)",
                )

            # DLQ ('crashed' is the terminal dead-letter state)
            dlq_count = await conn.fetchval(
                "SELECT COUNT(*) FROM jorb WHERE state = 'crashed'"
            )
            doc.warn_if(
                bool(dlq_count),
                "dlq",
                "empty",
                f"{dlq_count} dead-lettered job(s) (inspect: pj-admin dlq list)",
            )

            # Overdue schedules
            overdue = await conn.fetchval("""
                SELECT COUNT(*) FROM jorb_schedule
                WHERE enabled AND next_run < now() - interval '5 minutes'
            """)
            doc.warn_if(
                bool(overdue),
                "schedules",
                "no overdue schedules",
                f"{overdue} enabled schedule(s) overdue by >5m "
                "(is pj-scheduler running?)",
            )
        finally:
            await conn.close()

        return 1 if doc.failed else 0

    sys.exit(asyncio.run(_doctor()))


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
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            result = await migrations.migrate(conn)
        except asyncpg.InsufficientPrivilegeError as e:
            fail(
                f"Not permitted to install the schema: {e}",
                "The connecting role needs CREATE on the target schema.",
            )
        except asyncpg.PostgresError as e:
            fail(f"Migration failed: {e}", "The database was left unchanged.")
        else:
            # Reported distinctly, because "applied" and "recorded" are
            # different events and an operator reading a deploy log needs to
            # tell them apart: a fresh install RECORDS every migration without
            # running any of them (schema.sql already contains their effects),
            # and saying "applied" there would claim DDL that never ran.
            if result.installed_base:
                print_success("Installed base schema")
                if result.recorded:
                    print_success(
                        f"Recorded migrations {result.recorded} "
                        f"(already contained in the base schema)"
                    )
            if result.applied:
                print_success(f"Applied migrations: {result.applied}")
            elif not result.installed_base:
                print_success("Database schema is up to date")
        finally:
            await conn.close()

    asyncio.run(_migrate())


@db_group.command("status")
@click.pass_context
def db_status(ctx: click.Context) -> None:
    """Show applied vs pending schema migrations"""

    async def _status() -> None:
        conn = await get_connection(ctx.obj["config"], ctx.obj.get("dsn"))
        try:
            info = await migrations.status(conn)
            click.echo(
                f"Base schema installed: {'yes' if info['base_schema_installed'] else 'no'}"
            )
            click.echo(f"Applied migrations:    {info['applied'] or 'none'}")
            click.echo(f"Pending migrations:    {info['pending'] or 'none'}")
            # The line that answers the question the other three cannot: a
            # database installed before the runner existed records nothing,
            # so "pending: none" is true and meaningless on it.
            missing = info["missing"]
            click.echo(
                f"Missing objects:       {len(missing)}"
                if missing
                else "Missing objects:       none"
            )
            for name in missing:
                click.echo(f"  {name}")
        finally:
            await conn.close()

    asyncio.run(_status())


if __name__ == "__main__":
    cli(obj={})
