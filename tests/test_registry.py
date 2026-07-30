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


# ---------------------------------------------------------------------------
# The option list, and what shadows what
# ---------------------------------------------------------------------------


class TestTheRoutingOptions:
    """`partition_key` and `app_version` are the two row-level knobs the typed
    enqueue could not reach.

    They are options, not task kwargs, so without explicit parameters they
    landed in `**task_kwargs` and were refused as unknown parameters -- a
    decorated job simply could not be enqueued into a fair-share lane or
    pinned to a build, and the workaround was to stop using the decorator's
    enqueue at all. Declared here for the same reason `identity_key` and
    `deadline_key` are: an option name SHADOWS a task parameter of the same
    name, which the module docstring says and which is the price of the
    explicit list.
    """

    async def test_a_decorated_job_enqueues_into_a_lane_with_a_pin(
        self, client, db_pool, unique_queue
    ):
        @job
        class LaneReport(Job):
            async def task(self, day: str) -> dict[str, Any]:
                return {"day": day}

        job_id = await LaneReport.enqueue(
            client,
            queue=unique_queue,
            partition_key="tenant-42",
            app_version="2026.07.29",
            day="mon",
        )

        row = await db_pool.fetchrow(
            "SELECT partition_key, app_version, kwargs FROM jorb WHERE id = $1", job_id
        )
        assert row["partition_key"] == "tenant-42"
        assert row["app_version"] == "2026.07.29"
        assert row["kwargs"] == {"day": "mon"}, "options must not leak into kwargs"

    async def test_omitting_app_version_still_inherits_the_clients_pin(
        self, db_pool, unique_queue
    ):
        """None is not "unpinned" here, it is "whatever the client declared" --
        the same precedence every untyped enqueue follows (JobClient._app_version).
        A typed enqueue that hard-coded None would silently write unpinned work
        from a client whose whole purpose is pinning."""

        @job
        class PinnedReport(Job):
            async def task(self, day: str) -> dict[str, Any]:
                return {"day": day}

        pinned = JobClient(db_pool, app_version="2026.07.29")
        job_id = await PinnedReport.enqueue(pinned, queue=unique_queue, day="tue")

        assert (
            await db_pool.fetchval("SELECT app_version FROM jorb WHERE id = $1", job_id)
            == "2026.07.29"
        )

    async def test_the_refusals_reach_the_typed_enqueue_too(self, client, unique_queue):
        """The options go through the same shared row builder, so the same
        validation applies -- there is no second door."""

        @job
        class KeyedReport(Job):
            async def task(self, day: str) -> dict[str, Any]:
                return {"day": day}

        with pytest.raises(ValueError, match="partition_key is empty"):
            await KeyedReport.enqueue(
                client, queue=unique_queue, partition_key="", day="wed"
            )
        with pytest.raises(ValueError, match="cannot be combined with deadline_key"):
            await KeyedReport.enqueue(
                client,
                queue=unique_queue,
                identity_key="i",
                deadline_key="d",
                day="wed",
            )
