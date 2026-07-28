"""pyjobby — PostgreSQL job consumer platform.

Public API::

    from pyjobby import Job, JobClient, JobState

    # define work
    class SendEmail(Job):
        async def task(self, to: str):
            ...

    # enqueue work
    async with await JobClient.from_config("./pyjobby.toml") as client:
        await client.enqueue("myapp.jobs.SendEmail", to="user@example.com")

Heavier subsystems (AdminAPI, SchedulerWorker, WebSocketServer, migrations)
live in their own submodules and are imported explicitly. ``Job``,
``JobSystem`` and ``StateMachineJob`` are resolved lazily (PEP 562) for the
same reason: they live in the WORKER runtime, which drags in click, aiohttp
and the thread machinery — an application that only enqueues should not pay
that import on every process start, and before this it did.
"""

from importlib import metadata
from typing import TYPE_CHECKING

try:
    __version__ = metadata.version(__name__)
except metadata.PackageNotFoundError:
    __version__ = "dev"

from .client import (
    JobCancelledError,
    JobClient,
    JobError,
    JobFailedError,
    JobHandle,
    JobInfo,
    MachineHandle,
    SyncJobClient,
    SyncMachine,
    UnhandledEventError,
)
from .dag import DAGBuilder
from .db import JobState
from .dxe import DXEError, NondeterminismError, StaleExecutionError, StepTimeoutError

# MachineDefinitionError comes from .fsm, not .statemachine: the declaration
# format has no imports of its own, so exporting the error a bad machine
# declaration raises does not drag in the worker runtime.
from .fsm import MachineDefinitionError
from .registry import JobRegistry, job, registry
from .retry_strategies import RetryStrategy

if TYPE_CHECKING:
    from .pj import Job, JobSystem
    from .statemachine import StateMachineJob

#: name -> submodule, for the lazy worker-runtime exports below.
_LAZY = {
    "Job": "pj",
    "JobSystem": "pj",
    "StateMachineJob": "statemachine",
}


def __getattr__(name: str) -> object:
    """PEP 562 lazy loading for the worker-runtime classes (see module
    docstring). Explicit metaprogramming: this IS the import statement,
    deferred until the first `from pyjobby import Job`."""
    submodule = _LAZY.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{submodule}", __name__), name)


__all__ = [
    "DAGBuilder",
    "DXEError",
    "Job",
    "JobCancelledError",
    "JobClient",
    "JobError",
    "JobFailedError",
    "JobHandle",
    "JobInfo",
    "JobRegistry",
    "JobState",
    "JobSystem",
    "MachineDefinitionError",
    "MachineHandle",
    "NondeterminismError",
    "RetryStrategy",
    "StaleExecutionError",
    "StateMachineJob",
    "StepTimeoutError",
    "SyncJobClient",
    "SyncMachine",
    "UnhandledEventError",
    "__version__",
    "job",
    "registry",
]
