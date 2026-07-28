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

The helpers belong to the class they were generated for: decorate every
subclass you enqueue (an inherited ``enqueue`` is refused rather than
silently enqueueing the parent's job_class), and since the worker invokes
``task(**kwargs)``, a task with a REQUIRED positional-only parameter is
rejected at decoration time — nothing could ever supply it.

Tooling access:

    from pyjobby.registry import registry
    registry.resolve("myapp.jobs.SendEmail")   # -> the class
    registry.all_jobs()                        # -> {dotted: class}
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, overload

from .client import JobClient, JobHandle

if TYPE_CHECKING:
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


def _check_enqueueable_signature(dotted: str, signature: inspect.Signature) -> None:
    """Reject a task whose required arguments can never be supplied.

    The worker invokes ``task(**kwargs)``, so a required POSITIONAL-ONLY
    parameter is unsatisfiable: enqueue could only ever omit it and the job
    would crash inside a worker. Fail at decoration time instead."""
    unsatisfiable = [
        p.name
        for name, p in signature.parameters.items()
        if name not in _IGNORED_TASK_PARAMS
        and p.kind is inspect.Parameter.POSITIONAL_ONLY
        and p.default is inspect.Parameter.empty
    ]
    if unsatisfiable:
        raise TypeError(
            f"{dotted}: positional-only parameters {unsatisfiable} can never be "
            "supplied — jobs are invoked with keyword arguments only"
        )


def _reject_inherited_enqueue(caller: type[Job], owner: type[Job], dotted: str) -> None:
    """Refuse a typed enqueue reached through an undecorated subclass.

    The helpers are generated per class and closed over that class's dotted
    path and task signature, so an inherited one would enqueue the PARENT's
    job_class (running the wrong code) and validate against the parent's
    signature."""
    if caller is not owner:
        raise TypeError(
            f"{caller.__module__}.{caller.__qualname__} inherits the typed enqueue "
            f"of {dotted}; decorate it with @job so it enqueues itself"
        )


def _attach_typed_enqueue(
    cls: type[Job], dotted: str, signature: inspect.Signature
) -> None:
    """Attach validated `enqueue` / `enqueue_handle` classmethods to cls."""
    _check_enqueueable_signature(dotted, signature)

    async def enqueue(
        caller: type[Job],
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
        unknown or missing required parameters, or when reached through an
        undecorated subclass.
        """
        _reject_inherited_enqueue(caller, cls, dotted)
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

    async def enqueue_handle(
        caller: type[Job], client: JobClient, **kwargs: Any
    ) -> JobHandle:
        """Like enqueue() (same options + validated task kwargs) but returns
        a JobHandle for waiting/cancelling/event access."""
        job_id: int = await caller.enqueue(client, **kwargs)  # type: ignore[attr-defined]
        return JobHandle(id=job_id, client=client)

    # Deliberate metaprogramming: the decorator generates per-class enqueue
    # helpers closed over this class's dotted path and task signature, so
    # they must be installed on the class object itself. They are
    # classmethods so a call through an undecorated subclass is visible (and
    # refused) instead of silently enqueueing the parent. (Job declares
    # job_class_path as a ClassVar; the enqueue helpers are dynamic by
    # nature, hence the attr-defined waivers.)
    cls.enqueue = classmethod(enqueue)  # type: ignore[attr-defined]
    cls.enqueue_handle = classmethod(enqueue_handle)  # type: ignore[attr-defined]
    cls.job_class_path = dotted


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
    from .pj import Job  # lazy for the same reason as in job()

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
    # imported HERE, not at module top: registry is on the enqueue-only
    # import path (pyjobby/__init__), and Job lives in the worker runtime —
    # an application that only enqueues should not pay that import until it
    # actually decorates a job class in-process
    from .pj import Job

    if isinstance(target, type):
        if not issubclass(target, Job):
            raise TypeError(
                f"@job on a class requires a pyjobby.Job subclass, got {target!r}"
            )
        return _register_job_class(target)
    if callable(target):
        return _register_function(target)
    raise TypeError(f"@job target must be a Job subclass or callable, got {target!r}")
