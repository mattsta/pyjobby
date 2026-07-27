"""What `jorb_history.event` can actually contain — and whether the schema
says so truthfully.

`record_jorb_history()` writes exactly two things: the literal `'enqueued'`
on INSERT, and `NEW.state::text` on any UPDATE that changed the state. So the
domain of this column is not a vocabulary somebody chose, it is a *derived*
fact: `'enqueued'` plus whatever labels `jorbstate` happens to have.

The schema used to document it as
`enqueued|claimed|started|finished|retrying|crashed|cancelled|timeout|requeued|recovered`,
of which FIVE values can never be written and TWO real ones were missing —
including `'running'`, which is the row `pj-admin stats` counts to answer
"how many attempts did this job take". A comment that names events the
trigger cannot produce sends the next reader looking for `'timeout'` rows
that do not exist, and lets them write `event = 'started'` in a query that
will match nothing forever.

These tests therefore pin BOTH halves and pin them to each other: the
transitions the trigger really records, and the domain the comment claims,
compared against `jorbstate` itself. Adding a state to the enum without
touching the comment fails here rather than in a query six months later.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.asyncio

#: The one event that is not a state. Everything else in the column is a
#: jorbstate label, read from the catalog rather than repeated here.
INSERT_EVENT = "enqueued"

#: Values the old comment advertised that the trigger cannot write. Kept as
#: an explicit list because each one is a query somebody could plausibly
#: write and get silent emptiness from.
PHANTOM_EVENTS = ("started", "retrying", "timeout", "requeued", "recovered")


async def jorbstate_labels(pool) -> list[str]:
    """Every label of the jorbstate enum, in declaration order."""
    return [
        r["enumlabel"]
        for r in await pool.fetch(
            """
            SELECT e.enumlabel
              FROM pg_enum e
              JOIN pg_type t ON t.oid = e.enumtypid
             WHERE t.typname = 'jorbstate'
             ORDER BY e.enumsortorder
            """
        )
    ]


async def documented_domain(pool) -> list[str]:
    """The domain the column COMMENT advertises, parsed from its `Domain:`
    sentence — the machine-readable half of a comment written for humans."""
    comment: str = await pool.fetchval(
        """
        SELECT col_description('jorb_history'::regclass, a.attnum)
          FROM pg_attribute a
         WHERE a.attrelid = 'jorb_history'::regclass AND a.attname = 'event'
        """
    )
    assert comment, "jorb_history.event has no COMMENT to check"
    match = re.search(r"Domain:\s*([^.]+)\.", comment)
    assert match, f"the comment must open with a `Domain: a, b, c.` list\n{comment}"
    return [value.strip() for value in match.group(1).split(",")]


async def events_for(pool, job_id: int) -> list[str]:
    return [
        r["event"]
        for r in await pool.fetch(
            "SELECT event FROM jorb_history WHERE job_id = $1 ORDER BY id", job_id
        )
    ]


class TestWhatTheTriggerWrites:
    """The empirical domain: drive the column, do not reason about it."""

    async def test_insert_writes_enqueued_and_a_state_change_writes_the_state(
        self, db_pool, unique_queue
    ):
        """One job pushed through every jorbstate there is.

        The exact sequence, not a subset: the INSERT contributes 'enqueued'
        and each transition contributes the name of the state it entered, so
        the whole set of values this column can ever hold is produced here.
        """
        states = await jorbstate_labels(db_pool)
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('domain.Job', '{}', $1, 'queued') RETURNING id""",
            unique_queue,
        )

        # 'queued' is the insert state, so transitioning INTO it needs a
        # different state first; walk the rest and come back to it last.
        walk = [s for s in states if s != "queued"] + ["queued"]
        for state in walk:
            await db_pool.execute(
                "UPDATE jorb SET state = $2 WHERE id = $1", job_id, state
            )

        assert await events_for(db_pool, job_id) == [INSERT_EVENT, *walk]

    async def test_a_repeated_state_records_nothing(self, db_pool, unique_queue):
        """The trigger fires on `UPDATE OF state` but only records a CHANGE,
        which is why the domain has no duplicates to explain."""
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('domain.Job', '{}', $1, 'queued') RETURNING id""",
            unique_queue,
        )
        for _ in range(3):
            await db_pool.execute(
                "UPDATE jorb SET state = 'running' WHERE id = $1", job_id
            )

        assert await events_for(db_pool, job_id) == [INSERT_EVENT, "running"]

    async def test_no_phantom_event_is_reachable(self, db_pool, unique_queue):
        """The five values the old comment invented cannot be produced.

        Proved against the whole table rather than one job: nothing anywhere
        in the schema writes jorb_history except this trigger.
        """
        states = await jorbstate_labels(db_pool)
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, kwargs, queue, state)
               VALUES ('domain.Job', '{}', $1, 'queued') RETURNING id""",
            unique_queue,
        )
        for state in [s for s in states if s != "queued"]:
            await db_pool.execute(
                "UPDATE jorb SET state = $2 WHERE id = $1", job_id, state
            )

        found = [
            r["event"]
            for r in await db_pool.fetch(
                "SELECT DISTINCT event FROM jorb_history WHERE event = ANY($1::text[])",
                list(PHANTOM_EVENTS),
            )
        ]
        assert found == []


class TestTheCommentMatchesTheTrigger:
    """The defect was documentation, so the fix needs a documentation gate."""

    async def test_the_comment_lists_exactly_enqueued_plus_every_state(self, db_pool):
        """Derived from `jorbstate` itself, so adding a state and forgetting
        the comment fails here — the way the last five phantom values got in."""
        expected = [INSERT_EVENT, *await jorbstate_labels(db_pool)]

        assert sorted(await documented_domain(db_pool)) == sorted(expected)

    async def test_the_comment_names_running(self, db_pool):
        """`running` is the row that counts an ATTEMPT (`pj-admin stats`
        derives per-class retry counts from `event = 'running'`), and it was
        the value the old comment left out while listing five that do not
        exist."""
        assert "running" in await documented_domain(db_pool)
