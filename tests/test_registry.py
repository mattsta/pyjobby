"""Contract tests for the @job registry's typed enqueue.

The registry's whole point is that a mistake surfaces at enqueue time (or
earlier) instead of becoming a crash inside a worker minutes later, so these
tests are about what the decorator REFUSES:

- an enqueue reached through an undecorated subclass (it would run the
  parent's code under the parent's job_class)
- a task whose required arguments the worker could never supply

Plus the positive path: a decorated subclass enqueues itself, and the jobs
really do run.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from pyjobby import Job, JobClient, job
from pyjobby.registry import registry

from .conftest import wait_for_job_state

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Module-scope jobs (stable dotted paths: tests.test_registry.<Name>)
# ---------------------------------------------------------------------------


@job
class BaseReport(Job):
    """A registered job someone will inevitably subclass."""

    async def task(self, day: str) -> dict[str, Any]:
        return {"report": "base", "day": day}


class UndecoratedReport(BaseReport):
    """Subclass that forgot @job — it must not be silently enqueueable."""

    async def task(self, day: str) -> dict[str, Any]:
        return {"report": "undecorated", "day": day}


@job
class DecoratedReport(BaseReport):
    """Subclass that did decorate: enqueues itself, runs its own task."""

    async def task(self, day: str, detail: bool = False) -> dict[str, Any]:
        return {"report": "decorated", "day": day, "detail": detail}


@pytest_asyncio.fixture
async def client(db_pool):
    return JobClient(db_pool)


# ---------------------------------------------------------------------------
# Inherited enqueue
# ---------------------------------------------------------------------------


class TestInheritedEnqueue:
    async def test_undecorated_subclass_is_refused(self, client, unique_queue):
        """The generated helper is closed over ONE class's dotted path and
        task signature. Inherited, it enqueued the parent's job_class — so a
        worker ran BaseReport while the caller believed it queued the
        subclass."""
        with pytest.raises(TypeError, match="inherits the typed enqueue"):
            await UndecoratedReport.enqueue(client, queue=unique_queue, day="mon")

        with pytest.raises(TypeError, match="inherits the typed enqueue"):
            await UndecoratedReport.enqueue_handle(
                client, queue=unique_queue, day="mon"
            )

        assert await client.queue_depth(unique_queue) == 0

    async def test_refusal_names_the_fix(self, client, unique_queue):
        with pytest.raises(TypeError, match="decorate it with @job") as excinfo:
            await UndecoratedReport.enqueue(client, queue=unique_queue, day="mon")
        assert "UndecoratedReport" in str(excinfo.value)

    async def test_decorated_subclass_enqueues_itself(
        self, client, db_pool, unique_queue
    ):
        job_id = await DecoratedReport.enqueue(
            client, queue=unique_queue, day="tue", detail=True
        )
        row = await db_pool.fetchrow(
            "SELECT job_class, kwargs FROM jorb WHERE id = $1", job_id
        )
        assert row["job_class"] == "tests.test_registry.DecoratedReport"
        assert row["kwargs"] == {"day": "tue", "detail": True}

        # ...and it validates against ITS OWN signature, not the parent's
        assert registry.resolve("tests.test_registry.DecoratedReport") is (
            DecoratedReport
        )

    async def test_parent_enqueue_still_works(self, client, db_pool, unique_queue):
        job_id = await BaseReport.enqueue(client, queue=unique_queue, day="wed")
        job_class = await db_pool.fetchval(
            "SELECT job_class FROM jorb WHERE id = $1", job_id
        )
        assert job_class == "tests.test_registry.BaseReport"

    async def test_decorated_subclass_runs_its_own_task(
        self, client, db_pool, live_worker, unique_queue
    ):
        await live_worker()
        job_id = await DecoratedReport.enqueue(client, queue=unique_queue, day="thu")

        row = await wait_for_job_state(db_pool, job_id, ("finished",), timeout=20)
        assert row["result"] == {"report": "decorated", "day": "thu", "detail": False}


# ---------------------------------------------------------------------------
# Unsatisfiable task signatures
# ---------------------------------------------------------------------------


class TestUnsatisfiableSignatures:
    async def test_required_positional_only_parameter_is_rejected(self):
        """The worker calls task(**kwargs), so a required positional-only
        parameter can never be supplied: enqueue accepted the job with the
        argument missing and every attempt crashed in a worker."""
        with pytest.raises(TypeError, match="positional-only parameters"):

            @job
            def unsatisfiable(url, /, width: int = 128) -> None: ...

    async def test_required_positional_only_on_a_class_task_is_rejected(self):
        with pytest.raises(TypeError, match=r"positional-only parameters \['a'\]"):

            @job
            class BadTask(Job):
                async def task(self, a, /) -> None: ...

    async def test_positional_only_with_a_default_is_allowed(
        self, client, db_pool, unique_queue
    ):
        """Only REQUIRED positional-only parameters are unsatisfiable; one
        with a default simply always takes its default."""

        @job
        def defaulted(width: int = 128, /, *, height: int = 64) -> None: ...

        job_id = await defaulted.enqueue(client, queue=unique_queue, height=10)
        kwargs = await db_pool.fetchval("SELECT kwargs FROM jorb WHERE id = $1", job_id)
        assert kwargs == {"height": 10}

        with pytest.raises(TypeError, match="unknown parameters"):
            await defaulted.enqueue(client, queue=unique_queue, width=5)
