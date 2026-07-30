#!/usr/bin/env python3
"""
Pyjobby Web Admin Interface

HTTP server providing web-based management interface using htmx.
Built on top of the admin API for clean separation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import signal
import urllib.parse
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any, cast

import asyncpg  # type: ignore[import-untyped]
import jinja2
from aiohttp import web
from aiohttp.typedefs import Handler

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
# The largest page any listing endpoint will return. A `limit` is a promise to
# materialize that many rows in Postgres, ship them over the wire and (for
# format=html) concatenate them into one string in this process: an unbounded
# limit is a one-request memory amplifier for an anonymous client, and no
# dashboard has ever needed more than a thousand rows in a single fragment.
# Paging past it is what `offset` is for.
MAX_PAGE_LIMIT = 1000
# Most unknown query parameter names one 400 will name. The list is echoed
# back out of the request, so it is bounded like everything else here: a
# client that sends a thousand junk parameters must not be handed a
# thousand-line error body it wrote itself.
MAX_REPORTED_UNKNOWN_PARAMS = 5
# Longest parameter name or tag pair quoted back in an error message. Same
# reason, one value at a time.
MAX_ECHOED_PARAM_LENGTH = 64
# The widest metrics window. This is a *cost* bound, not a datetime bound: the
# windowed statements are index-backed only while the window is small, because
# they ride time-ordered indexes whose whole value is that they stop early.
# Ask for a century and the planner correctly gives up and reads the entire
# jorb table, which is exactly the request an anonymous client wants to make.
# 90 days is longer than any retention policy this system ships with.
MAX_SINCE_HOURS = 24 * 90
# How long an admin request may hold a pooled connection before asyncpg
# cancels the statement. See WebAdminServer._get_pool for why the admin pool
# sets one when db.create_pool deliberately does not.
POOL_COMMAND_TIMEOUT_SECONDS = 30.0
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
#                      enqueue-at). Not a backlog, and not counted as one.
#
# db.QUEUE_STATS_SQL is the semantic contract for those names and for the
# windowed terminal counts below; these strings stay separate only because
# their plans are pinned per scrape arm.
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

# The 'scheduled' split of the arrival cohort lives in
# AdminAPI.get_metrics' own GROUP BY, not here. It was a second statement in
# this handler first, which fixed /api/metrics and left `pj-admin metrics`
# -- the same get_metrics call, printing the same dict -- still folding
# deferred work into `queued`. One surface disagreeing with the rest was the
# defect; two surfaces disagreeing with each other is not an improvement on
# it. In the GROUP BY it is also ONE snapshot, so the split can never be
# arithmetic against a count taken a moment earlier.

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


#: Every query parameter ``/api/jobs`` reads. Anything else is a 400 -- see
#: _reject_unknown_query for what silence cost. `tag` is repeatable.
JOBS_QUERY_PARAMS = frozenset(
    {"queue", "state", "identity_key", "tag", "limit", "offset", "format"}
)


def _reject_unknown_query(request: web.Request, allowed: frozenset[str]) -> None:
    """Raise 400 for any query parameter the route does not read.

    A parameter nobody reads used to be dropped in silence, and a dropped
    FILTER is not a no-op: ``/api/jobs?identity_ke=x`` answered with every
    job in the queue, which reads exactly like a filter that matched
    everything. The operator's conclusion ("this identity is on every job")
    is the opposite of the truth, and nothing in the response says the
    parameter was never applied. Same judgment as the ``state`` rejection
    above -- malformed input is a 400 naming the parameter, never a quietly
    different query.
    """
    unknown = sorted(set(request.query) - allowed)
    if not unknown:
        return
    shown = ", ".join(
        repr(name[:MAX_ECHOED_PARAM_LENGTH])
        for name in unknown[:MAX_REPORTED_UNKNOWN_PARAMS]
    )
    if len(unknown) > MAX_REPORTED_UNKNOWN_PARAMS:
        shown += f", and {len(unknown) - MAX_REPORTED_UNKNOWN_PARAMS} more"
    raise _api_error(
        web.HTTPBadRequest,
        f"Unknown query parameter(s): {shown} "
        f"(this route reads: {', '.join(sorted(allowed))})",
    )


def _query_tags(request: web.Request) -> dict[str, Any] | None:
    """Parse repeated ``tag=key=value`` parameters into a tags filter, or 400.

    THE ENCODING IS THE CLI's, written for a URL. ``pj-admin jobs list --tag
    customer=acme --tag region=eu`` is ``?tag=customer%3Dacme&tag=region%3Deu``
    -- one parameter per pair, repeated, because that is what `--tag`
    repeated means and because a single packed parameter would need a
    separator that cannot appear in a tag value. Repetition ANDs: the filter
    matches jobs CONTAINING every pair, so extra tags on the job are fine.

    Values go through JSON first, exactly as `cli.parse_tags` does, so a tag
    stored as a number or a boolean is reachable (``tag=batch%3D7`` finds the
    number 7) and anything JSON does not recognise stays the plain string it
    looked like. A value that must be the *string* "7" is written the way
    JSON writes it: ``tag=batch%3D%227%22``.

    Not shared with `cli.parse_tags` on purpose: that one reports a malformed
    pair through `fail()`, which exits the process -- right for a command,
    fatal for a server that has to answer 400 and stay up. The syntax it
    accepts is deliberately identical, so an operator moving a filter from
    the shell into the dashboard is not learning a second one.
    """
    pairs = request.query.getall("tag", [])
    if not pairs:
        return None

    tags: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise _api_error(
                web.HTTPBadRequest,
                f"Malformed tag {pair[:MAX_ECHOED_PARAM_LENGTH]!r}: expected "
                f"key=value, url-encoded (tag=customer%3Dacme)",
            )
        try:
            value: Any = json.loads(raw)
        except ValueError:
            value = raw  # a bare word, which is the common case
        if isinstance(value, dict | list):
            # The same shape enqueue_rules.validate_tags refuses downstream,
            # refused here so it is a 400 naming the pair rather than a
            # ValueError escaping list_jobs as a 500.
            raise _api_error(
                web.HTTPBadRequest,
                f"Malformed tag {pair[:MAX_ECHOED_PARAM_LENGTH]!r}: tag values "
                f"must be a string, number, boolean or null, not an object or "
                f"an array",
            )
        tags[key] = value
    return tags


def _path_queue_name(request: web.Request) -> str:
    """Parse the ``queue`` path segment, or raise 400 if it is over-long."""
    queue = request.match_info["queue"]
    if len(queue) > MAX_QUEUE_NAME_LENGTH:
        raise _api_error(
            web.HTTPBadRequest,
            f"Invalid queue name: longer than {MAX_QUEUE_NAME_LENGTH} characters",
        )
    return queue


# =============================================================================
# Templates
#
# Every byte of HTML this server serves lives in pyjobby/templates. It used to
# live in this file: four complete <!DOCTYPE html> documents, the stylesheet
# written out three times, the htmx tag pinned four times, and sixteen places
# that built <tr>/<td> by string concatenation. A colour change was three
# edits, an htmx bump was four, and the three stylesheets had already drifted
# apart from each other.
#
# WHY Jinja2 AND NOT str.format. Two properties, and both are load-bearing:
#
#   INHERITANCE. base.html carries the doctype, the ONE stylesheet and the ONE
#   htmx tag; every page extends it. There is no second copy left to forget,
#   which is what made the duplication possible in the first place.
#
#   autoescape=True. This surface is unauthenticated and renders queue names,
#   job classes, schedule names and error messages that an anonymous client
#   put in the database. The old code defended that with seventeen hand-
#   written html.escape() calls -- a scheme that is correct exactly until
#   somebody adds the eighteenth interpolation and forgets, which is a stored
#   XSS hole rather than a formatting slip. Escaping is now the default and
#   NOT escaping is what has to be spelled out (`| safe`).
#
# The templates are addressed the way migrations.py addresses its SQL and
# websocket_server.py addresses its dashboard: through importlib.resources, so
# they are read out of the INSTALLED package -- a wheel, or a zip on sys.path
# -- and not out of a source checkout that `pip install pyjobby` never has.
# =============================================================================

#: Root of the packaged template set.
TEMPLATE_ROOT = files("pyjobby") / "templates"


class PackageTemplateLoader(jinja2.BaseLoader):
    """Jinja2 loader reading templates from an importlib.resources root.

    Deliberately a Traversable rather than a filesystem path: a Traversable is
    whatever the import system says the package is (a directory today, a zip
    entry if the package is ever imported from one), so the loader works from
    an installed wheel and not only from a checkout.
    """

    def __init__(self, root: Traversable) -> None:
        self.root = root

    def _resolve(self, template: str) -> Traversable | None:
        """Map a template name onto a Traversable inside the root, or None."""
        node = self.root
        for part in template.split("/"):
            # Template names come from this module and never from a request,
            # but a loader that can be walked out of its own root is not a
            # thing to leave next to an anonymous HTTP surface.
            if part in ("", ".", ".."):
                return None
            node = node / part
        return node

    def get_source(
        self, environment: jinja2.Environment, template: str
    ) -> tuple[str, str | None, Callable[[], bool] | None]:
        node = self._resolve(template)
        if node is not None:
            try:
                # uptodate is None: packaged assets do not change under a
                # running process, so a compiled template is cached forever.
                return node.read_text(encoding="utf-8"), None, None
            except OSError:
                # Missing, or a directory: both are "no such template".
                pass
        raise jinja2.TemplateNotFound(template)

    def list_templates(self) -> list[str]:
        """Every ``.html`` in the package, including the fragment subtree."""
        found: list[str] = []

        def walk(node: Traversable, prefix: str) -> None:
            for child in node.iterdir():
                name = f"{prefix}{child.name}"
                if child.is_dir():
                    walk(child, f"{name}/")
                elif name.endswith(".html"):
                    found.append(name)

        walk(self.root, "")
        return sorted(found)


def _url_path_segment(value: object) -> str:
    """Percent-encode a value for use as ONE URL path segment.

    ``safe=""``, unlike Jinja's built-in ``urlencode`` filter, which leaves
    ``/`` alone: a queue name is free-form text, and a slash left unencoded
    addresses a different route than the one the button says it does.
    """
    return urllib.parse.quote(str(value), safe="")


def build_template_env() -> jinja2.Environment:
    """Build the Jinja2 environment (see the section note above for why)."""
    env = jinja2.Environment(
        loader=PackageTemplateLoader(TEMPLATE_ROOT),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        # A missing key is a bug in the handler, not a blank cell in an
        # operator's dashboard: fail loudly, exactly as the KeyError of the
        # string-concatenation code did.
        undefined=jinja2.StrictUndefined,
    )
    env.filters["urlpath"] = _url_path_segment
    return env


# =============================================================================
# Cross-site request defense
#
# THE ATTACK. This surface is unauthenticated by design (bind to localhost, or
# put a proxy in front). "No credentials" is usually taken to mean CSRF does
# not apply, and here it means the opposite: every mutating route is reachable
# by anyone who can send bytes to the port, and a browser sends bytes to the
# port on any page's say-so. The mutations take HTML form encoding, which
# makes them CORS *simple requests* -- no preflight, no permission asked. Any
# page an operator visits while the admin is up can auto-submit a form to
# http://127.0.0.1:8081/api/queues/critical/pause, or DELETE a job, or create
# a schedule naming a class the workers will import. The attacker never reads
# the response (the same-origin policy still hides that) and does not need to:
# the write already happened.
#
# THE RULE, for every method other than GET and HEAD:
#
#   * `Sec-Fetch-Site` present and not `same-origin` or `none` -> 403. Browsers
#     attach this header themselves and scripts cannot forge it, so it is a
#     trustworthy statement of where the request came from. `same-origin` is
#     the admin's own htmx buttons; `none` is the user typing the URL.
#   * `Origin` present and naming a different host -> 403. This is the same
#     judgment for the browsers that send Origin, and it also catches a
#     same-origin claim that contradicts the Origin header.
#   * Neither header -> allowed.
#
# WHY THE LAST LINE IS NOT A HOLE. curl, a deploy script and the test client
# send neither header; a browser always sends at least one on a cross-site
# form post. So the rule refuses exactly the browser-driven cross-site
# submission -- which is the attack -- while leaving scripting alone. It is
# not, and is not trying to be, authentication: anything that can open a
# socket to this port can still do anything. Nothing here replaces binding to
# localhost or putting authentication in the proxy.
# =============================================================================

# Methods that do not mutate, and so do not need the check. Note OPTIONS is
# absent on purpose: a CORS preflight arrives with cross-site fetch metadata
# and should be refused like the request it is asking permission for.
_SAFE_METHODS = frozenset({"GET", "HEAD"})

# The `Sec-Fetch-Site` values a mutation is allowed to carry. Everything else
# the header can say (`cross-site`, `same-site`, and any value added later) is
# a request this surface has no reason to accept.
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


@web.middleware
async def cross_site_guard(
    request: web.Request, handler: Handler
) -> web.StreamResponse:
    """Refuse browser-initiated cross-site mutations (see the note above)."""
    if request.method in _SAFE_METHODS:
        return await handler(request)

    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site is not None and fetch_site not in _ALLOWED_FETCH_SITES:
        raise _api_error(
            web.HTTPForbidden,
            f"Cross-site {request.method} refused (Sec-Fetch-Site: "
            f"{fetch_site!r}): the admin surface accepts mutations only from "
            f"its own pages",
        )

    # Host and port only. The scheme is deliberately not compared: a proxy that
    # terminates TLS forwards `Origin: https://admin.example.com` on a
    # plain-http hop, and the operator's own dashboard must not be the thing
    # this rejects. An opaque origin ("null", from a sandboxed frame or a data:
    # URL) has no netloc and so never matches.
    origin = request.headers.get("Origin")
    if origin is not None and urllib.parse.urlsplit(origin).netloc != request.host:
        raise _api_error(
            web.HTTPForbidden,
            f"Cross-origin {request.method} refused (Origin: {origin!r} is "
            f"not {request.host!r}): the admin surface accepts mutations "
            f"only from its own pages",
        )

    return await handler(request)


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
        # Built once and cached on the instance: compiling a template is not
        # per-request work, and the environment is also the compiled-template
        # cache. See the Templates section above for why it exists at all.
        self.templates = build_template_env()
        self.app = web.Application(middlewares=[cross_site_guard])
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
                    # command_timeout is set HERE and not in db.create_pool:
                    # workers legitimately hold a connection for as long as a
                    # job takes, so a global default would kill real work.
                    # An admin request is the opposite -- every statement on
                    # this surface answers a dashboard, and one that cannot
                    # answer in 30 seconds is either a bug or an attack.
                    # Either way it must not keep a pooled connection: there
                    # are ten, and ten anonymous slow requests are the whole
                    # admin interface, parked, for as long as the caller
                    # cares to hold it.
                    self.pool = await db.create_pool(
                        **self.db_params,
                        min_size=1,
                        max_size=10,
                        command_timeout=POOL_COMMAND_TIMEOUT_SECONDS,
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
    async def _with_display_state(
        api: AdminAPI, jobs: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Add the state name the platform actually uses to each job row.

        `jorb.state` has no 'scheduled' value: a job parked in the future --
        retry backoff, enqueue-at -- is stored as `queued` with `run_after`
        ahead of now. Rendering that enum raw put a `queued` badge on work
        nothing will claim for an hour, so a table of deferred retries read
        as a backlog, and it disagreed with the queues table one page over,
        which counts those rows as `scheduled` (db.QUEUE_STATS_SQL) like
        `pj-admin queues` and the /metrics scrape do.

        The cutoff is the DATABASE's clock, one `SELECT now()`, and not this
        process's: `run_after` was written by the database, every other
        surface compares it against `now()` there, and a web host a few
        seconds adrift would otherwise badge boundary rows differently from
        the queues table beside it. Only asked when there are rows to badge.
        """
        if not jobs:
            return jobs
        now = await api.conn.fetchval("SELECT now()")
        for job in jobs:
            job["display_state"] = (
                "scheduled"
                if job["state"] == "queued"
                and datetime.fromisoformat(job["run_after"]) > now
                else job["state"]
            )
        return jobs

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
    # Rendering
    # =========================================================================

    def render(self, template: str, **context: Any) -> web.Response:
        """Render a packaged template into a ``text/html`` response.

        The only way HTML leaves this module. Values go in raw and are escaped
        on the way out by the environment's autoescape.
        """
        page = self.templates.get_template(template).render(**context)
        return web.Response(text=page, content_type="text/html")

    def _queues_table(self, stats: list[dict[str, Any]]) -> web.Response:
        """The queues fragment: three routes swap it into #queues-table."""
        return self.render("fragments/queues_table.html", stats=stats)

    def _dlq_table(self, jobs: list[dict[str, Any]]) -> web.Response:
        """The DLQ fragment: two routes swap it into #dlq-table."""
        return self.render("fragments/dlq_table.html", jobs=jobs)

    def _schedules_table(self, schedules: list[dict[str, Any]]) -> web.Response:
        """The schedules fragment: every schedule route swaps it in."""
        return self.render("fragments/schedules_table.html", schedules=schedules)

    # =========================================================================
    # HTML Pages
    # =========================================================================

    async def index(self, request: web.Request) -> web.Response:
        """Dashboard index page"""
        return self.render("index.html", title="Dashboard", active="/")

    async def jobs_page(self, request: web.Request) -> web.Response:
        """Jobs management page"""
        return self.render("jobs.html", title="Jobs", active="/jobs")

    async def queues_page(self, request: web.Request) -> web.Response:
        """Queues management page: depths plus pause/resume controls."""
        return self.render("queues.html", title="Queues", active="/queues")

    async def workers_page(self, request: web.Request) -> web.Response:
        """Workers page backed by the jorb_worker registry."""
        return self.render("workers.html", title="Workers", active="/workers")

    async def dlq_page(self, request: web.Request) -> web.Response:
        """DLQ page: terminal crashed jobs (retries exhausted)."""
        return self.render("dlq.html", title="Dead Letter Queue", active="/dlq")

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

            # The other silent failure, and like pyjobby_workers_not_claiming
            # the only signal that names it. A job above every live worker's
            # priority ceiling, wanting a capability none of them advertises,
            # or pinned to an app_version nobody runs is simply 'queued'
            # forever: it never fails, never retries, never reaches the DLQ,
            # and never appears in any other counter here -- the fleet is up,
            # the queue drains, and this work is invisible to it. Alert on it
            # above 0.
            #
            # Labelled by cause because the causes have different remedies
            # (raise the fleet's ceiling, start a worker advertising the
            # capability, run the version the job wants) and the reasons are
            # admin_api.UNCLAIMABLE_REASONS, the same strings `pj-admin
            # doctor` and `pj-admin jobs why` report.
            #
            # Affordable at scrape cadence for the reason the whole section
            # requires: its cost is bounded by the live fleet and by
            # UNCLAIMABLE_SCAN_LIMIT rows per queue per cause, never by how
            # much the installation has run. The same bound is why the value
            # saturates -- past the limit it reads "at least this many",
            # which an alert on > 0 does not care about.
            unclaimable = await api.unclaimable_jobs()
            lines.append(
                "# HELP pyjobby_jobs_unclaimable Queued, runnable jobs that "
                "no live worker on their queue could ever claim, by cause "
                "(above_worker_ceiling, capability_unmet, app_version_unmet). "
                "They never fail, never retry and never reach the DLQ, so no "
                "other series here goes non-zero for them; alert on it above "
                "0 and read the cause with `pj-admin doctor` or `pj-admin "
                "jobs why ID`. A queue with NO live workers is deliberately "
                "not reported: that is pyjobby_workers_live, and a different "
                "remedy. Counts saturate per queue per cause."
            )
            lines.append("# TYPE pyjobby_jobs_unclaimable gauge")
            for r in unclaimable:
                lines.append(
                    f'pyjobby_jobs_unclaimable{{queue="{esc(r["queue"])}",'
                    f'reason="{esc(r["reason"])}"}} {r["count"]}'
                )

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
        """List jobs (JSON or HTML).

        Every filter `AdminAPI.list_jobs` answers with an index is reachable
        from here: queue, state, the at-most-once `identity_key`, and `tag`
        pairs. The two searches an operator reaches for during an incident
        ("did this identity ever run, and what became of it", "show me this
        customer's jobs") were CLI-only, so the answer to a dashboard
        question was to leave the dashboard.
        """
        _reject_unknown_query(request, JOBS_QUERY_PARAMS)
        queue = request.query.get("queue")
        state = _query_job_state(request)
        # An empty value is "not filtering", the same reading _query_int
        # gives an empty limit: `?identity_key=` is a form field nobody
        # filled in, and no job holds the empty string as its identity.
        identity_key = request.query.get("identity_key") or None
        tags = _query_tags(request)
        limit = _query_int(request, "limit", 50, maximum=MAX_PAGE_LIMIT)
        offset = _query_int(request, "offset", 0)
        async with self.api() as api:
            format_type = request.query.get("format", "json")

            jobs = await api.list_jobs(
                queue=queue,
                state=state,
                identity_key=identity_key,
                tags=tags,
                limit=limit,
                offset=offset,
            )

            if format_type == "html":
                return self.render(
                    "fragments/jobs_table.html",
                    jobs=await self._with_display_state(api, jobs),
                )
            else:
                return web.json_response(jobs)

    async def api_job_get(self, request: web.Request) -> web.Response:
        """Get single job"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            return web.json_response(await self._job_or_404(api, job_id))

    async def api_job_history(self, request: web.Request) -> web.Response:
        """Get a job's transition history (jorb_history, oldest first).

        Paged like every other listing: a durable job parked for a month has
        an unbounded trail, and "one job's history" is not a bound.
        """
        job_id = _path_id(request, "job_id")
        limit = _query_int(request, "limit", MAX_PAGE_LIMIT, maximum=MAX_PAGE_LIMIT)
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            history = await api.get_job_history(job_id, limit=limit)
            return web.json_response(history)

    async def api_job_steps(self, request: web.Request) -> web.Response:
        """Get a job's DXE step checkpoints (jorb_step, in sequence order).

        Paged for the same reason as the history beside it: a long-running
        durable machine writes a checkpoint per step, without limit.
        """
        job_id = _path_id(request, "job_id")
        limit = _query_int(request, "limit", MAX_PAGE_LIMIT, maximum=MAX_PAGE_LIMIT)
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            steps = await api.get_job_steps(job_id, limit=limit)
            return web.json_response(steps)

    async def api_job_retry(self, request: web.Request) -> web.Response:
        """Retry a job (404 if it does not exist, 400 if its state forbids it)"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            result = await api.retry_job(job_id)
            if result["status"] == "not_retriable":
                # The job exists (404 already ruled absence out), so the only
                # thing left is a state the retry verb refuses. The message is
                # built from the RETURNED status, never from the row read
                # above: that read is a snapshot from before the verb ran, so
                # a concurrent operator made the refusal quote a state the job
                # was no longer in ("is in state 'crashed', can only retry
                # crashed"). The status is what the statement itself decided.
                return web.json_response(
                    {
                        "error": f"Job {job_id} was not retried "
                        f"({result['status']}): only crashed or cancelled "
                        f"jobs can be retried"
                    },
                    status=400,
                )
            return web.json_response(result)

    async def api_job_cancel(self, request: web.Request) -> web.Response:
        """Cancel a job (404 if it does not exist, 400 if it is terminal)"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            result = await api.cancel_job(job_id)
            if result["status"] == "not_cancellable":
                # The job exists (404 already ruled absence out), so it is
                # terminal. Message from the RETURNED status, not the row read
                # above: see api_job_retry for why quoting that snapshot made
                # the refusal contradict itself under a concurrent operator.
                return web.json_response(
                    {
                        "error": f"Job {job_id} was not cancelled "
                        f"({result['status']}): it has already reached a "
                        f"terminal state"
                    },
                    status=400,
                )
            return web.json_response(result)

    async def api_job_delete(self, request: web.Request) -> web.Response:
        """Delete a job"""
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            deleted = await api.delete_job(job_id)
            if deleted:
                return web.json_response({"status": "deleted", "job_id": job_id})
            raise _api_error(web.HTTPNotFound, f"Job {job_id} not found")

    async def api_queues_list(self, request: web.Request) -> web.Response:
        """List queue statistics (with paused/limit control-plane columns)"""
        async with self.api() as api:
            stats = await api.queue_stats()
            format_type = request.query.get("format", "json")

            if format_type == "html":
                return self._queues_table(stats)
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
                return self._queues_table(await api.queue_stats())
            return web.json_response(control)

    async def api_queue_resume(self, request: web.Request) -> web.Response:
        """Resume a paused queue (creates the control row if absent, exactly
        like api_queue_pause, and bounds the queue name the same way)"""
        queue = _path_queue_name(request)
        async with self.api() as api:
            control = await api.resume_queue(queue)
            if request.query.get("format") == "html":
                return self._queues_table(await api.queue_stats())
            return web.json_response(control)

    async def api_workers_list(self, request: web.Request) -> web.Response:
        """List workers from the jorb_worker registry"""
        async with self.api() as api:
            workers = await api.list_workers()
            format_type = request.query.get("format", "json")

            if format_type != "html":
                return web.json_response(workers)

            return self.render("fragments/workers_table.html", workers=workers)

    async def api_workers_stats(self, request: web.Request) -> web.Response:
        """Get worker statistics"""
        async with self.api() as api:
            stats = await api.worker_stats()
            format_type = request.query.get("format", "json")

            if format_type == "html":
                return self.render("fragments/worker_stats.html", stats=stats)
            else:
                return web.json_response(stats)

    async def api_dlq_list(self, request: web.Request) -> web.Response:
        """List Dead Letter Queue jobs (terminal crashed state)"""
        limit = _query_int(request, "limit", 100, maximum=MAX_PAGE_LIMIT)
        async with self.api() as api:
            jobs = await api.list_dlq(limit=limit)
            if request.query.get("format") == "html":
                return self._dlq_table(jobs)
            return web.json_response(jobs)

    async def api_dlq_retry(self, request: web.Request) -> web.Response:
        """Retry job from DLQ (requeues the same row with errors reset).

        404 if the job does not exist, 400 if it exists but is not in the DLQ.
        """
        job_id = _path_id(request, "job_id")
        async with self.api() as api:
            await self._job_or_404(api, job_id)
            result = await api.retry_from_dlq(job_id)
            if result["status"] == "not_retriable":
                # The job exists (404 already ruled absence out), so it is
                # simply not a DLQ job. Message from the RETURNED status, not
                # the row read above: see api_job_retry for why that snapshot
                # is not allowed to speak for the verb's decision.
                return web.json_response(
                    {
                        "error": f"Job {job_id} was not retried "
                        f"({result['status']}): the DLQ is the crashed jobs, "
                        f"and this one is not crashed"
                    },
                    status=400,
                )
            if request.query.get("format") == "html":
                # Refresh the DLQ table for htmx buttons
                return self._dlq_table(await api.list_dlq(limit=100))
            return web.json_response(result)

    async def api_metrics(self, request: web.Request) -> web.Response:
        """Get system metrics"""
        since_hours = _query_int(request, "since_hours", 24, maximum=MAX_SINCE_HOURS)
        async with self.api() as api:
            queue = request.query.get("queue")
            format_type = request.query.get("format", "json")

            # jorb timestamps are naive-UTC, so compare with a naive-UTC value
            since = datetime.now(UTC) - timedelta(hours=since_hours)
            # `state_counts` already reports deferred rows as 'scheduled'
            # rather than 'queued' -- the split is in get_metrics' GROUP BY, so
            # every surface that calls it agrees.
            metrics = await api.get_metrics(since=since, queue=queue)

            if format_type == "html":
                return self.render("fragments/metrics.html", metrics=metrics)
            else:
                return web.json_response(metrics)

    # =========================================================================
    # Schedule Management Pages & API
    # =========================================================================

    async def schedules_page(self, request: web.Request) -> web.Response:
        """Schedules management page"""
        return self.render(
            "schedules.html",
            title="Schedules",
            active="/schedules",
            prio_ceiling=self.prio_ceiling,
        )

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
                return self._schedules_table(schedules)
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
                    priority=int(cast(str | int, data.get("priority", 100))),
                    description=cast(str | None, data.get("description")),
                    max_concurrent_jobs=int(
                        cast(str | int, data.get("max_concurrent_jobs", 1))
                    ),
                    jitter_seconds=int(cast(str | int, data.get("jitter_seconds", 0))),
                    backfill_limit=int(cast(str | int, data.get("backfill_limit", 0))),
                    backpressure_threshold=int(
                        cast(str | int, data.get("backpressure_threshold", 1000))
                    ),
                    circuit_breaker_threshold=int(
                        cast(str | int, data.get("circuit_breaker_threshold", 5))
                    ),
                )

                # Return refreshed schedules list as HTML
                return self._schedules_table(await api.list_schedules())
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
        limit = _query_int(request, "limit", 50, maximum=MAX_PAGE_LIMIT)
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
        default="./pyjobby.toml",
        show_default=True,
        # Spelled out rather than "the same as every other daemon": pj-monitor
        # is not, it takes --config with no -c and no default, so the sentence
        # that claimed uniformity sent operators to write `pj-monitor -c ...`
        # and get "no such option".
        help="Config file path (must define db_params; may define "
        "prio_ceiling) — the same -c/--config pj, pj-admin, pj-scheduler, "
        "pj-ws and pj-bench take (pj-monitor takes --config only)",
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
            # `is not None`, not `or`: a configured prio_ceiling = 0 is a
            # real value, not "unset".
            configured = cfg.get("prio_ceiling")
            max_prio = DEFAULT_PRIO_CEILING if configured is None else configured

        asyncio.run(serve(db_params, host, port, max_prio))

    cli()


if __name__ == "__main__":
    main()
