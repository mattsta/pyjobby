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

from .utils.faults import record_effect


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


class StreamProducerJob(Job):
    """Streams `n` values on `key`, then closes the stream.

    `delay` spaces the writes out so a reader can be observed consuming them
    WHILE the job runs rather than after it finishes.
    """

    async def task(
        self, key: str = "rows", n: int = 5, delay: float = 0.0
    ) -> dict[str, Any]:
        for i in range(n):
            if delay:
                await asyncio.sleep(delay)
            await self.stream_write(key, {"i": i})
        await self.stream_close(key)
        return {"wrote": n}


class StreamThenCrashJob(Job):
    """Streams `n` values on `key` and then raises, never closing.

    The reader's other termination rule: a job that dies mid-stream ends its
    readers by reaching a terminal state, with no marker to stop on.
    """

    async def task(self, key: str = "rows", n: int = 3) -> None:
        for i in range(n):
            await self.stream_write(key, {"i": i})
        raise RuntimeError("crashed mid-stream")


class StreamRetryJob(Job):
    """Streams `n` values, fails once AFTER them, then closes on the retry.

    The replay probe: every completed ``stream_write`` fast-forwards on
    attempt 2, so the stream must hold `n` rows and not `2n`.
    """

    async def task(self, key: str = "rows", n: int = 3) -> dict[str, Any]:
        for i in range(n):
            await self.stream_write(key, {"i": i})
        await self.step("maybe-explode", self._maybe_boom)
        await self.stream_close(key)
        return {"wrote": n}

    def _maybe_boom(self) -> dict[str, Any]:
        if self.job["error_count"] == 0:
            raise RuntimeError("after the stream writes")
        return {"ok": True}


class GatedStepJob(Job):
    """Two checkpointed steps; the second one fails until a fix is "deployed".

    The fork shape, without asking a job to change its own code: 'gate'
    raises unless a ``jorb_test_effect`` row labelled 'fixed' exists for
    this tag, so a test can let the original crash, insert that row, and
    fork from the failure — the fork fast-forwards 'prepare' and gets
    through 'gate'.

    Every REAL execution of either step appends its own ledger row (labelled
    with the step name, against the executing job's id), so a test counts
    what actually ran per job rather than what the checkpoint table claims.
    """

    async def task(self, tag: str) -> dict[str, Any]:
        prepared = await self.step("prepare", self._prepare, tag)
        passed = await self.step("gate", self._gate, tag)
        return {"prepare": prepared, "gate": passed}

    async def _prepare(self, tag: str) -> dict[str, Any]:
        await record_effect(self.s.cxn, tag, self.job["id"], "prepare")
        return {"by": self.job["id"]}

    async def _gate(self, tag: str) -> dict[str, Any]:
        await record_effect(self.s.cxn, tag, self.job["id"], "gate")
        fixed = await self.s.cxn.fetchval(
            "SELECT count(*) FROM jorb_test_effect WHERE tag = $1 AND label = 'fixed'",
            tag,
        )
        if not fixed:
            raise RuntimeError("gate closed: the fix is not deployed")
        return {"by": self.job["id"]}


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
