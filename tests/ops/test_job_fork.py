"""`pj-admin jobs fork` against a real fleet, the way an incident goes.

The story ADMIN_TOOLS.md and OPERATIONS.md tell: a durable job crashes part
way through, the cause is fixed, and the operator forks it FROM the failure
so the completed prefix is not paid for again. Everything here is the real
thing — a spawned `pj` worker, a spawned `pj-admin`, the checkpoints the
worker really wrote — because the claim under test spans all three: the CLI
must create a row, the row must carry checkpoints the worker fast-forwards,
and the original must be left exactly where it crashed.

A job cannot change its own code between two runs, so "the fix is deployed"
is a marker row the gated step reads (tests.dxe_jobs.GatedStepJob). What is
under test is the fork, not the fix.
"""

from __future__ import annotations

import re

import asyncpg
import pytest

from pyjobby.client import JobClient
from tests.conftest import wait_for_job_state
from tests.ops.conftest import wait_until
from tests.utils.faults import (
    effect_counts_per_job,
    ensure_effects_table,
    record_effect,
)

pytestmark = [pytest.mark.ops, pytest.mark.slow, pytest.mark.e2e]

#: `jobs fork` leads with the line an operator (and a script) reads the new
#: id out of.
FORKED_LINE = re.compile(r"Job (\d+) forked from job (\d+)")


async def job_state(pool: asyncpg.Pool, job_id: int) -> str:
    return await pool.fetchval("SELECT state FROM jorb WHERE id = $1", job_id)


class TestForkFromFailure:
    async def test_the_fork_finishes_while_the_original_stays_crashed(
        self, fleet, admin, db_pool, unique_queue
    ):
        await ensure_effects_table(db_pool)
        fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        crashed = await client.enqueue(
            "tests.dxe_jobs.GatedStepJob",
            queue=unique_queue,
            tag=unique_queue,
            max_retries=0,
        )
        await wait_for_job_state(db_pool, crashed, ("crashed",), timeout=60)
        assert (
            await db_pool.fetchval(
                "SELECT count(*) FROM jorb_step WHERE job_id = $1 AND error IS NULL",
                crashed,
            )
            == 1
        ), "the prefix the fork must NOT re-run was never checkpointed"

        # the fix ships
        await record_effect(db_pool, unique_queue, crashed, "fixed")

        forked = admin("jobs", "fork", str(crashed), "--from-failure")

        assert forked.returncode == 0, forked.stdout + forked.stderr
        match = FORKED_LINE.search(forked.stdout)
        assert match, forked.stdout
        fork_id = int(match.group(1))
        assert int(match.group(2)) == crashed
        assert "starts at step 2 (1 checkpoint(s) copied, fast-forwarded)" in (
            forked.stdout
        )

        await wait_for_job_state(db_pool, fork_id, ("finished",), timeout=60)

        # the original is exactly where it was left
        assert await job_state(db_pool, crashed) == "crashed"
        # and the expensive prefix ran once in total, on the original
        counts = await effect_counts_per_job(db_pool, unique_queue)
        assert counts[(crashed, "prepare")] == 1
        assert (fork_id, "prepare") not in counts
        assert counts[(fork_id, "gate")] == 1

    async def test_the_lineage_is_visible_from_both_jobs(
        self, fleet, admin, db_pool, unique_queue
    ):
        """`jobs inspect` answers "where did this come from" on the fork and
        "what came out of this" on the source — the two questions an operator
        asks days apart."""
        await ensure_effects_table(db_pool)
        fleet.worker(unique_queue)
        client = JobClient(pool=db_pool)
        crashed = await client.enqueue(
            "tests.dxe_jobs.GatedStepJob",
            queue=unique_queue,
            tag=unique_queue,
            max_retries=0,
        )
        await wait_for_job_state(db_pool, crashed, ("crashed",), timeout=60)
        await record_effect(db_pool, unique_queue, crashed, "fixed")

        forked = admin("jobs", "fork", str(crashed), "--from-failure")
        fork_id = int(FORKED_LINE.search(forked.stdout).group(1))
        await wait_until(
            lambda: db_pool.fetchval(
                "SELECT 1 FROM jorb WHERE id = $1 AND state = 'finished'", fork_id
            ),
            describe="the fork finishing",
            timeout=60,
        )

        on_fork = admin("jobs", "inspect", str(fork_id))
        on_source = admin("jobs", "inspect", str(crashed))
        history = admin("jobs", "history", str(fork_id))

        assert f"Forked From:     job {crashed} at step 2" in on_fork.stdout
        assert f"Forked Into:     {fork_id}" in on_source.stdout
        assert f"Forked from job {crashed} at step 2" in history.stdout
