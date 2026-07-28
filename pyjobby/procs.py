"""Launch real pyjobby daemons and reap them completely.

Part of the package rather than the test tree because `pj-bench e2e` needs
it too: a benchmark that measures end-to-end latency has to drive real
worker processes, and importing test helpers from shipped code meant
`pj-bench e2e` raised ModuleNotFoundError for anyone who installed pyjobby
instead of cloning it -- the wheel does not package tests/.

A console script that parses ``--help`` proves nothing about whether the
subsystem behind it is wired up at all. The helpers here start the actual
process, wait for an observable effect, and then kill the whole process
group so nothing survives into later tests.

Usage::

    async with daemon("pj-monitor", "--dsn", dsn, "--check-interval", "1"):
        row = await wait_until(lambda: job_was_requeued(conn, job_id))

Every helper kills by process GROUP: the worker launcher forks children,
and killing only the direct child leaves pollers behind that claim other
tests' jobs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

#: The checkout root: ``<root>/pyjobby/procs.py`` -> ``<root>``. This is the
#: ``cwd`` every spawned daemon inherits, so a relative config path in a test
#: resolves against the repo. (It carried one ``dirname()`` too many until
#: 2026-07 and pointed at the checkout's PARENT, which made the ``.venv/bin``
#: lookup below always miss and always take the fallback branch.)
REPO_ROOT = Path(__file__).resolve().parent.parent


def dsn_from(db_params: dict[str, Any]) -> str:
    """Build a DSN string from the conftest db_params fixture."""
    return (
        f"postgresql://{db_params['user']}:{db_params['password']}"
        f"@{db_params['host']}:{db_params['port']}/{db_params['database']}"
    )


def script_path(name: str) -> Path:
    """Locate installed console script ``name``.

    Prefers the checkout's ``.venv/bin`` so the entry point an operator gets
    from ``poetry install`` is the one exercised, and falls back to the
    running interpreter's own script directory (a venv installed elsewhere,
    or an editable install into a system Python).

    Single source of truth on purpose: ``spawn()`` and the synchronous
    ``run_to_completion()`` in the suite both resolve scripts through here,
    so they cannot drift into disagreeing about which binary is under test.
    """
    executable = REPO_ROOT / ".venv" / "bin" / name
    if not executable.exists():
        return Path(sys.executable).parent / name
    return executable


def write_config_toml(
    path: Path, db_params: dict[str, object], **extra: object
) -> Path:
    """Write a pyjobby.toml holding ``db_params`` (plus optional scalar
    ``extra`` keys like prio_ceiling) and return its path.

    One writer for every harness that spawns real daemons (tests and
    pj-bench e2e), because hand-formatted config strings copied per file
    are how the last format's writers drifted. Values are TOML basic
    strings/ints only — exactly what connection parameters are.
    """

    def _v(value: object) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return str(value)
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    lines = [f"{k} = {_v(v)}" for k, v in extra.items()]
    lines.append("")
    lines.append("[db_params]")
    lines.extend(f"{k} = {_v(v)}" for k, v in db_params.items() if v is not None)
    path.write_text("\n".join(lines) + "\n")
    return path


def spawn(
    *args: str,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.Popen[bytes]:
    """Start a console script in its own process group.

    Runs through ``poetry run`` equivalent (the venv's bin directory) so the
    installed entry point is exercised exactly as an operator would.
    """
    executable = script_path(args[0])

    full_env = {**os.environ, **(env or {})}
    pipe = subprocess.PIPE if capture else subprocess.DEVNULL
    return subprocess.Popen(
        [str(executable), *args[1:]],
        stdout=pipe,
        stderr=pipe,
        cwd=REPO_ROOT,
        env=full_env,
        start_new_session=True,  # own process group -> group kill reaps children
    )


def terminate(proc: subprocess.Popen[bytes], grace: float = 5.0) -> None:
    """SIGTERM the whole group, then SIGKILL anything left."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=grace)


@asynccontextmanager
async def daemon(
    *args: str,
    env: dict[str, str] | None = None,
    capture: bool = False,
    startup: float = 0.6,
) -> AsyncIterator[subprocess.Popen[bytes]]:
    """Run a daemon for the duration of the block, then reap its group.

    Fails the test if the process dies during startup — a daemon that exits
    immediately (bad flag, unreadable config, import error) would otherwise
    look identical to one that is running quietly.
    """
    proc = spawn(*args, env=env, capture=capture)
    try:
        await asyncio.sleep(startup)
        if proc.poll() is not None:
            out, err = proc.communicate() if capture else (b"", b"")
            raise AssertionError(
                f"{args[0]} exited during startup with code {proc.returncode}\n"
                f"stdout: {out.decode(errors='replace')}\n"
                f"stderr: {err.decode(errors='replace')}"
            )
        yield proc
    finally:
        terminate(proc)


async def wait_until(
    condition: Callable[[], Awaitable[Any]],
    timeout: float = 15.0,
    interval: float = 0.2,
    what: str = "condition",
) -> Any:
    """Poll an async predicate until it returns something truthy.

    Returns the truthy value so callers can assert on it.
    """
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await condition()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"{what} never became true within {timeout}s (last: {last!r})")


async def port_is_open(host: str, port: int) -> bool:
    """True once something accepts TCP connections on host:port."""
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except ConnectionRefusedError, OSError:
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def free_port() -> int:
    """An unused localhost TCP port (for binding test servers)."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return int(port)
