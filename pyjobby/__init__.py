"""pyjobby — PostgreSQL job consumer platform.

Public API::

    from pyjobby import Job, JobClient, JobState

    # define work
    class SendEmail(Job):
        async def task(self, to: str):
            ...

    # enqueue work
    async with JobClient.from_config("./pyjobby.conf.py") as client:
        await client.enqueue("myapp.jobs.SendEmail", to="user@example.com")

Heavier subsystems (AdminAPI, SchedulerWorker, WebSocketServer, migrations)
live in their own submodules and are imported explicitly.
"""

from importlib import metadata

try:
    __version__ = metadata.version(__name__)
except metadata.PackageNotFoundError:
    __version__ = "dev"

from .client import JobClient, JobInfo
from .dag import DAGBuilder
from .db import JobState
from .pj import Job, JobSystem
from .retry_strategies import RetryStrategy

__all__ = [
    "DAGBuilder",
    "Job",
    "JobClient",
    "JobInfo",
    "JobState",
    "JobSystem",
    "RetryStrategy",
    "__version__",
]
