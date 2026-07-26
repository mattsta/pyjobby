"""Reusable job classes for exercising the worker and DXE primitives.

These are real Job subclasses resolved by the worker via their dotted path
(``tests.dxe_jobs.<Name>``); any test that drives a live worker can use
them. Keep them small, deterministic, and composable — they are shared
infrastructure, not per-test one-offs.
"""

from __future__ import annotations

import asyncio
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
