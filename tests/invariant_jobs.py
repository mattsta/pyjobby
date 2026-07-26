"""Job classes for the invariant suite.

IMPORTANT: job classes must live in a module that contains NO test
decorators. The worker calls ``importlib.reload()`` on a job's module before
every execution (that is the hot-reload feature), so if the module also
defined ``@given`` tests, reloading would re-evaluate those decorators
mid-test and hypothesis would abort with "Nesting @given tests…". Keep this
module to plain job definitions — same reason ``tests/dxe_jobs.py`` exists.
"""

from __future__ import annotations

from pyjobby.pj import Job


class SucceedJob(Job):
    """Always succeeds."""

    async def task(self, n: int = 1) -> dict:
        return {"n": n}


class FlakyJob(Job):
    """Fails its first ``fail_times`` attempts, then succeeds.

    Reads ``error_count`` from its own row, so behavior follows the real
    retry bookkeeping rather than in-process state."""

    async def task(self, fail_times: int = 1) -> dict:
        if self.job["error_count"] < fail_times:
            raise RuntimeError(f"attempt {self.job['error_count'] + 1} fails")
        return {"attempts": self.job["error_count"] + 1}


class AlwaysFailJob(Job):
    """Never succeeds — exercises the retry budget and the terminal DLQ."""

    async def task(self) -> None:
        raise RuntimeError("always fails")


class CountingStepJob(Job):
    """Records every ACTUAL execution of its step in a side-effect table.

    Proves exactly-once step semantics: if a completed step were recomputed
    on a later attempt, the side-effect table would hold more than one row
    for this job.
    """

    async def task(self, fail_after_step: bool = False) -> dict:
        marker = await self.step("side-effect", self._record)
        if fail_after_step and self.job["error_count"] < 1:
            raise RuntimeError("fails after the step committed")
        return {"marker": marker}

    async def _record(self) -> dict:
        await self.s.cxn.execute(
            "INSERT INTO jorb_step_effects (job_id) VALUES ($1)", self.job["id"]
        )
        return {"recorded_for": self.job["id"]}
