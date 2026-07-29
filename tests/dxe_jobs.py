"""Reusable job classes for exercising the worker and DXE primitives.

These are real Job subclasses resolved by the worker via their dotted path
(``tests.dxe_jobs.<Name>``); any test that drives a live worker can use
them. Keep them small, deterministic, and composable — they are shared
infrastructure, not per-test one-offs.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pyjobby.pj import Job


class OkJob(Job):
    """Succeeds immediately; doubles its input."""

    async def task(self, x: int = 1) -> dict[str, Any]:
        return {"doubled": x * 2}


class FailJob(Job):
    """Always raises (exercises retry -> DLQ)."""

    def task(self) -> None:
        raise ValueError("intentional failure")


class SlowJob(Job):
    """Runs long enough to be observed 'running' and cancelled."""

    async def task(self, seconds: float = 30) -> str:
        await asyncio.sleep(seconds)
        return "done"


class StepPipelineJob(Job):
    """Three checkpointed steps; step 2 fails on the first attempt only.

    The step SEQUENCE is deterministic (required); the failing behavior
    lives INSIDE the step, which is legal. Retries must fast-forward step 1
    and re-execute step 2.
    """

    async def task(self) -> dict[str, Any]:
        a = await self.step("fetch", lambda: {"n": 7})
        await self.step("maybe-explode", self._maybe_boom)
        c = await self.step("double", lambda: {"n2": a["n"] * 2})
        return {"final": c["n2"]}

    def _maybe_boom(self) -> dict[str, Any]:
        if self.job["error_count"] == 0:
            raise RuntimeError("mid-pipeline crash")
        return {"ok": True}


class SleeperJob(Job):
    """Publishes an event, durably sleeps, publishes again, finishes."""

    async def task(self, seconds: float = 2) -> str:
        await self.set_event("phase", {"at": "before-sleep"})
        await self.sleep(seconds)
        await self.set_event("phase", {"at": "after-sleep"})
        return "woke"


class SyncBlockFirstAttemptJob(Job):
    """SYNCHRONOUS task that blocks straight past its timeout on attempt 1.

    A timed-out synchronous task cannot be interrupted: the worker records
    the timeout on time but the thread runs on, abandoned -- the exact
    condition the abandoned-thread accounting and NOT CLAIMING refusal
    exist for. The second attempt returns immediately, so the worker
    recovers once the thread drains.
    """

    def task(self, seconds: float = 8) -> str:
        if self.job["run_count"] <= 1:
            time.sleep(seconds)
        return "done"


class EpochSleeperJob(Job):
    """Sleeps, then reports the run_epoch it executed under.

    The fencing probe: kill/stall the worker mid-sleep, let the monitor
    reclaim the job, and the surviving result must carry the NEW epoch --
    a stale execution that wakes up later is fenced out of overwriting it.
    """

    async def task(self, seconds: float = 8) -> dict[str, Any]:
        await asyncio.sleep(seconds)
        return {"epoch": self.job["run_epoch"]}


class FirstAttemptBlocksStepJob(Job):
    """Step "first" checkpoints fast; step "blocker" hangs on attempt 1 only.

    Kill the worker while "blocker" sleeps: the reclaimed retry must
    fast-forward "first" (returning the RECORDED value, with the original
    epoch inside) and re-execute only "blocker", which sails through on
    run_count 2. The result carries both epochs so a test can prove which
    steps re-ran from the outside.
    """

    async def task(self) -> dict[str, Any]:
        first = await self.step("first", self._mark)
        blocker = await self.step("blocker", self._block)
        return {"first": first, "blocker": blocker}

    def _mark(self) -> dict[str, Any]:
        return {"epoch": self.job["run_epoch"]}

    async def _block(self) -> dict[str, Any]:
        if self.job["run_count"] <= 1:
            await asyncio.sleep(600)
        return {"epoch": self.job["run_epoch"]}


class PingJob(Job):
    """Sends one durable message to another job."""

    async def task(self, dest: int) -> str:
        await self.send(dest, {"ping": True}, topic="game")
        return "pinged"


class PongJob(Job):
    """Receives one durable message (blocks up to `timeout`)."""

    async def task(self, timeout: float = 10) -> dict[str, Any]:
        msg = await self.recv(topic="game", timeout=timeout)
        return {"got": msg}
