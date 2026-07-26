"""Typed job registry for pyjobby.

The ``@job`` decorator registers work under its dotted path and attaches
TYPED enqueue helpers that validate keyword arguments against the task's
signature BEFORE anything hits the database:

    from pyjobby import Job, job

    @job
    class SendEmail(Job):
        async def task(self, to: str, subject: str = "hi") -> None: ...

    job_id = await SendEmail.enqueue(client, to="x@y.com")
    handle = await SendEmail.enqueue_handle(client, to="x@y.com", priority=5)

    SendEmail.enqueue(client, bogus=1)   # TypeError before any INSERT

Plain functions work too — they are wrapped into a generated Job subclass
(registered under ``module.funcname``; the decorator RETURNS that class, so
the worker's dotted-path import resolves to it):

    @job
    async def resize_image(url: str, width: int = 128) -> dict: ...

    await resize_image.enqueue(client, url="...")

Function jobs receive only their own kwargs — no ``self`` — so DXE
primitives (self.step / self.sleep / self.send / self.recv / self.set_event)
require a Job subclass. The original function stays reachable as ``.fn``.

Enqueue-time options are explicit keyword-only parameters on the attached
``enqueue`` (queue, priority, run_after, timeout_seconds, max_retries,
retry_strategy, deadline_key, waitfor_job, use_result_from, admin_data);
everything else in ``**task_kwargs`` is validated against the task
signature (``self`` and the worker-injected ``upstream_result`` are
ignored). An option name therefore shadows any task parameter of the same
name — pick different names for task parameters.

Tooling access:

    from pyjobby.registry import registry
    registry.resolve("myapp.jobs.SendEmail")   # -> the class
    registry.all_jobs()                        # -> {dotted: class}
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import datetime
from typing import Any, overload

from .client import JobClient, JobHandle
from .pj import Job

# Parameter names never supplied by enqueue callers: `self` is the job
# instance, `upstream_result` is injected by the worker at execution time.
_IGNORED_TASK_PARAMS = frozenset({"self", "upstream_result"})


class JobRegistry:
    """Registry of @job-decorated classes, keyed by dotted path."""

    def __init__(self) -> None:
        self._jobs: dict[str, type[Job]] = {}

    def register(self, dotted: str, cls: type[Job]) -> None:
        self._jobs[dotted] = cls

    def resolve(self, dotted: str) -> type[Job]:
        """Return the registered class for a dotted path (KeyError if absent)."""
        return self._jobs[dotted]

    def all_jobs(self) -> dict[str, type[Job]]:
        """A copy of the full registry (dotted path -> class) for tooling."""
        return dict(self._jobs)


#: The process-wide default registry the @job decorator populates.
registry = JobRegistry()


def _validate_task_kwargs(
    dotted: str, signature: inspect.Signature, task_kwargs: dict[str, Any]
) -> None:
    """Raise TypeError listing unknown / missing-required parameters."""
    params = [
        p
        for name, p in signature.parameters.items()
        if name not in _IGNORED_TASK_PARAMS
    ]
    accepts_arbitrary = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
    named = [
        p
        for p in params
        if p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]

    unknown = (
        [] if accepts_arbitrary else sorted(set(task_kwargs) - {p.name for p in named})
    )
    missing = sorted(
        p.name
        for p in named
        if p.default is inspect.Parameter.empty and p.name not in task_kwargs
    )
    if unknown or missing:
        problems = []
        if unknown:
            problems.append(f"unknown parameters {unknown}")
        if missing:
            problems.append(f"missing required parameters {missing}")
        raise TypeError(
            f"{dotted}: invalid task kwargs — " + "; ".join(problems)
        ) from None


def _attach_typed_enqueue(
    cls: type[Job], dotted: str, signature: inspect.Signature
) -> None:
    """Attach validated `enqueue` / `enqueue_handle` staticmethods to cls."""

    async def enqueue(
        client: JobClient,
        *,
        queue: str = "default",
        priority: int = 100,
        run_after: datetime | None = None,
        timeout_seconds: int | None = None,
        max_retries: int = 10,
        retry_strategy: str = "exponential",
        deadline_key: str | None = None,
        waitfor_job: int | None = None,
        use_result_from: int | None = None,
        admin_data: dict[str, Any] | None = None,
        **task_kwargs: Any,
    ) -> int:
        """Enqueue this job with kwargs validated against its task signature.

        Raises TypeError (before any database work) when **task_kwargs has
        unknown or missing required parameters.
        """
        _validate_task_kwargs(dotted, signature, task_kwargs)
        return await client.enqueue(
            dotted,
            queue=queue,
            priority=priority,
            run_after=run_after,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_strategy=retry_strategy,
            deadline_key=deadline_key,
            waitfor_job=waitfor_job,
            use_result_from=use_result_from,
            admin_data=admin_data,
            **task_kwargs,
        )

    async def enqueue_handle(client: JobClient, **kwargs: Any) -> JobHandle:
        """Like enqueue() (same options + validated task kwargs) but returns
        a JobHandle for waiting/cancelling/event access."""
        job_id = await enqueue(client, **kwargs)
        return JobHandle(id=job_id, client=client)

    setattr(cls, "enqueue", staticmethod(enqueue))  # noqa: B010
    setattr(cls, "enqueue_handle", staticmethod(enqueue_handle))  # noqa: B010
    setattr(cls, "job_class_path", dotted)  # noqa: B010


def _register_job_class[J: Job](cls: type[J]) -> type[J]:
    dotted = f"{cls.__module__}.{cls.__name__}"
    signature = inspect.signature(cls.task)
    _attach_typed_enqueue(cls, dotted, signature)
    registry.register(dotted, cls)
    return cls


def _register_function(fn: Callable[..., Any]) -> type[Job]:
    """Wrap a plain (async or sync) function into a registered Job subclass.

    The generated class replaces the function at its module-level name (the
    decorator returns it), so the worker's dotted-path lookup resolves to a
    Job subclass. The task receives ONLY the enqueued kwargs — no self —
    so DXE primitives need a real Job subclass instead.
    """
    if inspect.iscoroutinefunction(fn):

        async def task(self: Job, **kwargs: Any) -> Any:
            return await fn(**kwargs)

    else:

        def task(self: Job, **kwargs: Any) -> Any:  # type: ignore[misc]
            return fn(**kwargs)

    cls = type(
        fn.__name__,
        (Job,),
        {
            "task": task,
            "fn": staticmethod(fn),
            "__module__": fn.__module__,
            "__qualname__": fn.__qualname__,
            "__doc__": fn.__doc__,
        },
    )
    dotted = f"{fn.__module__}.{fn.__name__}"
    _attach_typed_enqueue(cls, dotted, inspect.signature(fn))
    registry.register(dotted, cls)
    return cls


@overload
def job[J: Job](target: type[J]) -> type[J]: ...


@overload
def job(target: Callable[..., Any]) -> type[Job]: ...


def job[J: Job](target: type[J] | Callable[..., Any]) -> type[J] | type[Job]:
    """Register a Job subclass or a plain function as a typed, enqueueable
    job (see module docstring for the full contract)."""
    if isinstance(target, type):
        if not issubclass(target, Job):
            raise TypeError(
                f"@job on a class requires a pyjobby.Job subclass, got {target!r}"
            )
        return _register_job_class(target)
    if callable(target):
        return _register_function(target)
    raise TypeError(f"@job target must be a Job subclass or callable, got {target!r}")
