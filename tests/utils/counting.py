"""Count the database round trips a client call actually makes.

Some defects are invisible by reading and invisible in a pass/fail result: a
waiter that spins at 4 Hz returns exactly the same answer as one that sleeps on
a notification, only slower and at a cost paid by the database rather than by
the test. The only way that shows up is by counting.

Counting is done at the CLIENT boundary, not in the database. `pg_stat_statements`
is an extension that may not be installed, and the database-wide counters are
useless here anyway: a live worker heartbeating and a machine polling its own
mailbox produce far more traffic than the thing under test, so any shared
counter is swamped by noise it cannot separate.

The proxy forwards the five methods `JobClient` actually uses. Deliberately
explicit rather than a `__getattr__` catch-all: a client that started using a
sixth should fail loudly here rather than slip through uncounted, because an
uncounted call is exactly the one a regression would hide behind.
"""

from __future__ import annotations

from typing import Any

import asyncpg  # type: ignore[import-untyped]


class CountingPool:
    """Wraps a pool and counts every statement sent through it."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self.calls = 0

    def acquire(self, *args: Any, **kwargs: Any) -> Any:
        # Connections taken out of the pool are used directly, so anything
        # sent on them is NOT counted. No client path under test does this;
        # if one starts to, its traffic silently stops being measured.
        return self._pool.acquire(*args, **kwargs)

    async def fetch(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self._pool.fetch(*args, **kwargs)

    async def fetchrow(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self._pool.fetchrow(*args, **kwargs)

    async def fetchval(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self._pool.fetchval(*args, **kwargs)

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return await self._pool.execute(*args, **kwargs)


class counted_client:  # noqa: N801 - a context manager used as a verb
    """Swap a client's pool for a counting proxy for the duration.

    Swaps the attribute on the CLIENT rather than instrumenting the pool, so
    nothing else sharing that pool — other tests, a live worker — is affected
    or counted.

        with counted_client(client) as counter:
            await handle.wait_for_state("shipped", timeout=6)
        assert counter.calls < 12
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._original = client.pool
        self.counter = CountingPool(client.pool)

    def __enter__(self) -> CountingPool:
        self._client.pool = self.counter
        return self.counter

    def __exit__(self, *exc: object) -> None:
        self._client.pool = self._original
