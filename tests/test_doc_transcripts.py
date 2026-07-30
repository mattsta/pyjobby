"""The docs' console transcripts, run against a real database.

Every operator-facing document in `docs/` shows what a command PRINTS. Those
blocks are the fastest way to learn the platform and the slowest thing in the
repository to notice when it goes stale: nothing imports them, nothing runs
them, and a check added to `doctor` or a key added to `jobs why --json` leaves
them silently describing a release that no longer exists. Three separate
audits have now found the same class of defect -- a trigger count that was
right when it was written, a doctor transcript missing a check, a `--json`
sample missing a field -- and each was fixed by hand, which is the fix that
does not hold.

This is the structural fix. It executes the real commands against the test
database and compares them to what the documents claim, so a transcript can
only go stale by failing here first.

WHAT IS COMPARED, AND WHAT DELIBERATELY IS NOT. The comparison is of
STRUCTURED FACTS -- which checks exist, how many triggers, which JSON keys,
which table columns -- and never of prose. A doc that pinned byte-identical
output would fail on a reworded remedy, on a hostname, on a row count, and on
a column width; it would be reverted within the month and the transcripts
would go back to rotting. So: names and counts are pinned, and everything a
run can legitimately differ in (PASS vs WARN, ids, timings, the wording of a
remedy) is not.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from pyjobby.cli import cli
from pyjobby.migrations import REQUIRED_TRIGGERS

from .test_cli_errors import dsn_for

pytestmark = pytest.mark.asyncio

DOCS = Path(__file__).resolve().parent.parent / "docs"


@pytest.fixture
def dsn(db_params: dict) -> str:
    """This session's database, as the `--dsn` a real operator would pass."""
    return dsn_for(db_params)


#: Every document that shows a full `pj-admin doctor` report.
DOCTOR_TRANSCRIPT_DOCS = ("TROUBLESHOOTING.md", "OPERATIONS.md", "ADMIN_TOOLS.md")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FENCE = re.compile(r"^```(\w*)\s*$")
_DOCTOR_LINE = re.compile(r"^(PASS|WARN|FAIL) ([^:]+): (.*)$")
_TRIGGER_COUNT = re.compile(r"all schema triggers present \((\d+)\)")

#: A `doctor` block with fewer lines than this is an EXCERPT -- the docs quote
#: one or two lines all over the place to explain a single check, and holding
#: those to the full inventory would be wrong. A full report has fourteen.
_FULL_REPORT_MIN_LINES = 8


# =============================================================================
# Reading the documents
# =============================================================================


def console_blocks(name: str) -> list[str]:
    """Every ```console fenced block in `docs/<name>`, in order."""
    body: list[str] | None = None
    language = ""
    blocks: list[str] = []
    for line in (DOCS / name).read_text().splitlines():
        fence = _FENCE.match(line)
        if fence is not None:
            if body is None:
                body, language = [], fence.group(1)
            else:
                if language == "console":
                    blocks.append("\n".join(body))
                body = None
            continue
        if body is not None:
            body.append(line)
    return blocks


def normalise_check(name: str) -> str:
    """A doctor check name with the per-queue one collapsed.

    `doctor` emits one check PER QUEUE with a backlog (`queue reports`), so
    the name carries data. The inventory is about which checks exist.
    """
    return "queue *" if name.startswith("queue ") else name


def doctor_lines(block: str) -> list[tuple[str, str, str]]:
    """(status, check, message) for every doctor line in a console block."""
    found = []
    for line in _ANSI.sub("", block).splitlines():
        match = _DOCTOR_LINE.match(line.strip())
        if match is not None:
            found.append((match.group(1), match.group(2), match.group(3)))
    return found


def full_doctor_reports(name: str) -> list[list[tuple[str, str, str]]]:
    """Every block in `docs/<name>` that shows a WHOLE doctor report.

    Identified by shape rather than by a marker in the document: a full report
    opens on the database check and runs to the end. Everything shorter is an
    excerpt of one check, and the docs are full of those on purpose.
    """
    reports = []
    for block in console_blocks(name):
        lines = doctor_lines(block)
        if len(lines) >= _FULL_REPORT_MIN_LINES and lines[0][1] == "database":
            reports.append(lines)
    return reports


#: Every (doc, index, report) the suite has to check, flattened for
#: parametrize so a failure names the document it is about.
DOCTOR_CASES = [
    (doc, index, report)
    for doc in DOCTOR_TRANSCRIPT_DOCS
    for index, report in enumerate(full_doctor_reports(doc))
]


# =============================================================================
# Running the real commands
# =============================================================================


async def run_admin(*args: str) -> str:
    """`pj-admin <args>`, in a worker thread, with the colours stripped."""

    def _invoke():
        return CliRunner().invoke(cli, list(args))

    result = await asyncio.to_thread(_invoke)
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception
    )
    return _ANSI.sub("", result.output)


async def real_doctor(dsn: str) -> list[tuple[str, str, str]]:
    return doctor_lines(await run_admin("--dsn", dsn, "doctor"))


async def seed_one_backlogged_queue(db_pool, queue: str) -> None:
    """One queued job, so `doctor` emits its per-queue backlog check."""
    await db_pool.execute(
        "INSERT INTO jorb (job_class, kwargs, queue, state) "
        "VALUES ('docs.Job', '{}', $1, 'queued')",
        queue,
    )


# =============================================================================
# 1. doctor's check inventory
# =============================================================================


class TestDoctorTranscripts:
    """Which checks `doctor` prints, held against every doc that shows them.

    The inventory is the part of the report a reader navigates by: the
    troubleshooting table has a row per check name, and a check the docs do
    not list is a check nobody looks up. `backfill` shipped and stayed out of
    two of the three transcripts for exactly that long.
    """

    @pytest.mark.parametrize(
        "doc,index,report",
        DOCTOR_CASES,
        ids=[f"{doc}#{index}" for doc, index, _ in DOCTOR_CASES],
    )
    async def test_the_transcript_lists_exactly_the_checks_doctor_runs(
        self, db_pool, dsn, unique_queue, doc, index, report
    ):
        """Both directions, because they fail differently.

        A check the docs show and the command does not is a reader hunting for
        a line that will never appear. A check the command runs and the docs
        do not show is a WARN nobody has been told how to read.

        The real run is shaped to match the transcript's own premise -- a doc
        showing a per-queue backlog line gets a database with a backlog -- so
        the two are compared on the same question.
        """
        shown = [normalise_check(check) for _, check, _ in report]
        if "queue *" in shown:
            await seed_one_backlogged_queue(db_pool, unique_queue)

        actual = [normalise_check(check) for _, check, _ in await real_doctor(dsn)]

        assert set(shown) == set(actual), (
            f"docs/{doc} transcript #{index} lists checks that do not exist "
            f"({sorted(set(shown) - set(actual))}) and misses checks that do "
            f"({sorted(set(actual) - set(shown))})"
        )

    @pytest.mark.parametrize(
        "doc,index,report",
        DOCTOR_CASES,
        ids=[f"{doc}#{index}" for doc, index, _ in DOCTOR_CASES],
    )
    async def test_the_transcript_lists_the_checks_in_the_order_they_run(
        self, db_pool, dsn, unique_queue, doc, index, report
    ):
        """The order is the report's argument, not a layout choice.

        `job-threads` sits under `workers` because it is the count above that
        is misleading; `unclaimable` sits under the backlog because that is
        what hides it. A transcript in a different order teaches a different
        reading of the same output.
        """
        shown = [normalise_check(check) for _, check, _ in report]
        if "queue *" in shown:
            await seed_one_backlogged_queue(db_pool, unique_queue)

        actual = [normalise_check(check) for _, check, _ in await real_doctor(dsn)]

        assert shown == actual, f"docs/{doc} transcript #{index} is out of order"

    @pytest.mark.parametrize(
        "doc,index,report",
        DOCTOR_CASES,
        ids=[f"{doc}#{index}" for doc, index, _ in DOCTOR_CASES],
    )
    async def test_the_transcript_counts_the_triggers_the_schema_installs(
        self, dsn, doc, index, report
    ):
        """`all schema triggers present (N)` is the one number in the report.

        It went stale the moment an eighth trigger shipped, and it went stale
        in three documents at once, because it is a number a reader has no way
        to check and an author has no reason to revisit.
        """
        line = next(message for _, check, message in report if check == "triggers")
        match = _TRIGGER_COUNT.search(line)
        assert match is not None, f"docs/{doc}: unreadable triggers line: {line}"

        assert int(match.group(1)) == len(REQUIRED_TRIGGERS), (
            f"docs/{doc} transcript #{index} says {match.group(1)} triggers; "
            f"the schema installs {len(REQUIRED_TRIGGERS)}"
        )
        # ...and the command really does print that number, so the manifest
        # and the transcript cannot agree with each other while both being
        # wrong about the database.
        actual = next(
            m for _, check, m in await real_doctor(dsn) if check == "triggers"
        )
        assert _TRIGGER_COUNT.search(actual).group(1) == match.group(1)

    async def test_the_docs_show_a_full_report_at_all(self):
        """Guard the guard: every assertion above is parametrized over blocks
        the parser found, so a parser that found none would pass silently."""
        for doc in DOCTOR_TRANSCRIPT_DOCS:
            assert full_doctor_reports(doc), (
                f"docs/{doc} shows no full doctor report, or the parser stopped "
                f"recognising one -- every transcript assertion here is vacuous"
            )


# =============================================================================
# 2. `jobs why --json`
# =============================================================================


class TestJobsWhyJsonTranscript:
    """The `--json` sample is a schema a monitoring script is written against.

    A field missing from it is a field nobody knows they can branch on;
    `identity_key` was added to the answer precisely because it changes what
    an operator should DO (re-submitting the work returns this very job), and
    the sample went on not mentioning it.
    """

    DOC = "ADMIN_TOOLS.md"

    def documented_sample(self) -> dict:
        """The `jobs why ... --json` payload the doc prints."""
        for block in console_blocks(self.DOC):
            if "jobs why" not in block or "--json" not in block:
                continue
            start = block.index("{")
            depth = 0
            for offset, char in enumerate(block[start:], start):
                depth += (char == "{") - (char == "}")
                if depth == 0:
                    return json.loads(block[start : offset + 1])
        raise AssertionError(f"docs/{self.DOC} shows no `jobs why --json` sample")

    async def test_the_sample_shows_every_key_the_command_emits(
        self, db_pool, dsn, unique_queue
    ):
        """Both directions again: a key in the sample that the command does
        not emit is a script branching on None forever."""
        await db_pool.execute(
            """INSERT INTO jorb_worker (host, pid, queue, max_prio, last_seen)
               VALUES ('doc-transcripts', 4242, $1, 100, now())""",
            unique_queue,
        )
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state, prio)
               VALUES ('myapp.jobs.NightlyRollup', '{}', $1, 'queued', 5000)
               RETURNING id""",
            unique_queue,
        )

        emitted = json.loads(
            await run_admin("--dsn", dsn, "jobs", "why", str(job_id), "--json")
        )
        documented = self.documented_sample()

        assert set(documented) == set(emitted), (
            f"docs/{self.DOC}'s `jobs why --json` sample shows keys the "
            f"command does not emit ({sorted(set(documented) - set(emitted))}) "
            f"and omits keys it does ({sorted(set(emitted) - set(documented))})"
        )
        # The sample is of the above_worker_ceiling answer, so its `details`
        # are that reason's details -- the part a script reads after branching
        # on `reason`, and the part with no other home in the document.
        assert emitted["reason"] == "above_worker_ceiling", emitted
        assert documented["reason"] == emitted["reason"]
        assert set(documented["details"]) == set(emitted["details"]), (
            f"the sample's details keys are "
            f"{sorted(set(documented['details']) ^ set(emitted['details']))} "
            f"away from the real ones"
        )


# =============================================================================
# 3. `queues stats`
# =============================================================================


def table_rows(block: str, command: str) -> tuple[list[str], dict[str, list[str]]]:
    """(headers, {first cell: row}) for the table `command` prints in `block`.

    Cells are split on runs of two or more spaces, which is exactly what
    ``termout.print_table`` puts between columns.
    """
    lines = _ANSI.sub("", block).splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"$ {command}")
    headers = re.split(r"\s{2,}", lines[start + 1].strip())
    rows = {}
    for line in lines[start + 3 :]:
        if not line.strip() or line.startswith("$"):
            break
        cells = re.split(r"\s{2,}", line.strip())
        rows[cells[0]] = cells
    return headers, rows


class TestQueuesStatsTranscript:
    """The stats table's columns and its Limits cell.

    Columns because the row is meant to ADD UP -- every state `Total` sums has
    one, and a transcript missing a column teaches an operator that the
    numbers do not reconcile. The Limits cell because it is the one cell whose
    content is computed rather than counted, and because the lane scope on it
    is the difference between a limit that looks blown and a limit that is
    doing its job.
    """

    DOC = "ADMIN_TOOLS.md"

    def documented(self) -> tuple[list[str], dict[str, list[str]]]:
        for block in console_blocks(self.DOC):
            if "$ pj-admin queues stats" in block:
                return table_rows(block, "pj-admin queues stats")
        raise AssertionError(f"docs/{self.DOC} shows no `queues stats` transcript")

    async def seed(self, db_pool) -> None:
        """The two queues the transcript is written about: one partitioned
        with a concurrency cap, one queue-wide with both limits."""
        headers, rows = self.documented()
        for name in rows:
            partitioned = any(
                cell.endswith("/lane") or "/lane" in cell for cell in rows[name]
            )
            await db_pool.execute(
                """INSERT INTO jorb_queue
                       (name, max_concurrency, rate_limit, rate_period_seconds,
                        partition_limits)
                   VALUES ($1, $2, $3, 60, $4)""",
                name,
                4 if partitioned else 8,
                None if partitioned else 100,
                partitioned,
            )

    async def test_the_transcript_shows_the_columns_the_command_prints(
        self, db_pool, dsn
    ):
        await self.seed(db_pool)

        headers, _ = self.documented()
        actual_headers, _ = table_rows(
            f"$ pj-admin queues stats\n"
            + await run_admin("--dsn", dsn, "queues", "stats"),
            "pj-admin queues stats",
        )

        assert headers == actual_headers, (
            f"docs/{self.DOC}'s `queues stats` header is stale: shows "
            f"{headers}, prints {actual_headers}"
        )

    async def test_the_transcript_renders_the_limits_cell_the_command_renders(
        self, db_pool, dsn
    ):
        """Including the lane scope.

        `queues list` carried `/lane` and this table did not, which is the
        worse of the two omissions: `Limits` sits on the same row as `Running`,
        so a per-lane cap reads as a queue-wide one that has been blown.
        """
        await self.seed(db_pool)

        _, documented = self.documented()
        _, actual = table_rows(
            f"$ pj-admin queues stats\n"
            + await run_admin("--dsn", dsn, "queues", "stats"),
            "pj-admin queues stats",
        )

        assert set(documented) == set(actual), (
            f"the transcript shows queues {sorted(documented)}, the command "
            f"printed {sorted(actual)}"
        )
        for name in documented:
            assert documented[name][-1] == actual[name][-1], (
                f"docs/{self.DOC}: the Limits cell for '{name}' is "
                f"{documented[name][-1]!r}, the command renders "
                f"{actual[name][-1]!r}"
            )
        # The transcript has to include a PARTITIONED queue, or the scope it
        # is being checked for is not on the page at all.
        assert any("/lane" in row[-1] for row in documented.values()), (
            f"docs/{self.DOC}'s `queues stats` transcript shows no partitioned "
            f"queue, so it demonstrates nothing about the lane scope"
        )
