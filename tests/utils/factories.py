"""
Factory functions for creating test data.

Provides helpers for creating jobs, workers, and test scenarios.
"""

import secrets
from datetime import datetime, timedelta
from typing import Any, Optional


def make_job_kwargs(
    job_class: str = "test.TestJob",
    kwargs: Optional[dict[str, Any]] = None,
    queue: str = "test_queue",
    prio: int = 100,
    state: str = "queued",
    run_after: Optional[datetime] = None,
    uid: Optional[int] = None,
    run_group: Optional[int] = None,
    waitfor_job: Optional[int] = None,
    waitfor_group: Optional[int] = None,
    capability: Optional[str] = None,
    deadline_key: Optional[str] = None,
    admin_data: Optional[dict] = None,
) -> dict[str, Any]:
    """
    Create parameters for inserting a job into the database.

    Args:
        job_class: Python class path for the job
        kwargs: Job arguments (will be JSON encoded)
        queue: Queue name
        prio: Priority (lower = higher priority)
        state: Initial job state
        run_after: Minimum start time
        uid: User ID
        run_group: Group ID for this job
        waitfor_job: Job ID this job depends on
        waitfor_group: Group ID this job waits for
        capability: Required capability
        deadline_key: Unique key for singleton scheduling
        admin_data: Admin metadata

    Returns:
        dict: Parameters ready for INSERT statement
    """
    if kwargs is None:
        kwargs = {}

    if run_after is None:
        run_after = datetime.utcnow()

    return {
        "job_class": job_class,
        "kwargs": kwargs,  # Pass raw dict - asyncpg will serialize with orjson
        "queue": queue,
        "prio": prio,
        "state": state,
        "run_after": run_after,
        "uid": uid,
        "run_group": run_group,
        "waitfor_job": waitfor_job,
        "waitfor_group": waitfor_group,
        "capability": capability,
        "deadline_key": deadline_key,
        "admin_data": admin_data,  # Pass raw dict - asyncpg will serialize with orjson
    }


async def create_job(
    conn,
    job_class: str = "test.TestJob",
    kwargs: Optional[dict[str, Any]] = None,
    **options
) -> int:
    """
    Insert a job into the database and return its ID.

    Args:
        conn: Database connection
        job_class: Python class path for the job
        kwargs: Job arguments
        **options: Additional job parameters (see make_job_kwargs)

    Returns:
        int: Created job ID
    """
    params = make_job_kwargs(job_class=job_class, kwargs=kwargs, **options)

    job_id = await conn.fetchval(
        """
        INSERT INTO jorb (
            job_class, kwargs, queue, prio, state, run_after,
            uid, run_group, waitfor_job, waitfor_group,
            capability, deadline_key, admin_data
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        RETURNING id
        """,
        params["job_class"],
        params["kwargs"],
        params["queue"],
        params["prio"],
        params["state"],
        params["run_after"],
        params["uid"],
        params["run_group"],
        params["waitfor_job"],
        params["waitfor_group"],
        params["capability"],
        params["deadline_key"],
        params["admin_data"],
    )

    return job_id


async def create_job_batch(
    conn,
    count: int,
    job_class: str = "test.TestJob",
    queue: str = "test_queue",
    **options
) -> list[int]:
    """
    Create multiple jobs at once.

    Args:
        conn: Database connection
        count: Number of jobs to create
        job_class: Python class path for the job
        queue: Queue name
        **options: Additional job parameters

    Returns:
        list[int]: List of created job IDs
    """
    job_ids = []
    for i in range(count):
        job_id = await create_job(
            conn,
            job_class=job_class,
            kwargs={"batch_index": i},
            queue=queue,
            **options
        )
        job_ids.append(job_id)

    return job_ids


async def create_dependency_chain(
    conn,
    depth: int,
    queue: str = "test_queue"
) -> list[int]:
    """
    Create a chain of dependent jobs.

    Example with depth=3:
        Job 1 (queued) → Job 2 (waiting) → Job 3 (waiting)

    Args:
        conn: Database connection
        depth: Length of dependency chain
        queue: Queue name

    Returns:
        list[int]: Job IDs in order [parent, child1, child2, ...]
    """
    job_ids = []

    # Create first job (no dependency)
    job_id = await create_job(
        conn,
        job_class="test.TestJob",
        kwargs={"chain_index": 0},
        queue=queue,
        state="queued",
    )
    job_ids.append(job_id)

    # Create dependent jobs
    for i in range(1, depth):
        job_id = await create_job(
            conn,
            job_class="test.TestJob",
            kwargs={"chain_index": i},
            queue=queue,
            state="waiting",
            waitfor_job=job_ids[-1],
        )
        job_ids.append(job_id)

    return job_ids


async def create_job_group(
    conn,
    group_size: int,
    queue: str = "test_queue",
    run_group: Optional[int] = None,
) -> tuple[int, list[int]]:
    """
    Create a group of jobs with the same run_group.

    Args:
        conn: Database connection
        group_size: Number of jobs in group
        queue: Queue name
        run_group: Group ID (generated if None)

    Returns:
        tuple: (run_group, [job_ids])
    """
    if run_group is None:
        run_group = secrets.randbits(63)

    job_ids = []
    for i in range(group_size):
        job_id = await create_job(
            conn,
            job_class="test.TestJob",
            kwargs={"group_index": i},
            queue=queue,
            run_group=run_group,
        )
        job_ids.append(job_id)

    return run_group, job_ids


async def create_group_dependency(
    conn,
    parent_group_size: int,
    queue: str = "test_queue",
) -> tuple[int, list[int], int]:
    """
    Create a group of jobs and a job that waits for the entire group.

    Args:
        conn: Database connection
        parent_group_size: Number of jobs in parent group
        queue: Queue name

    Returns:
        tuple: (run_group, parent_job_ids, dependent_job_id)
    """
    # Create parent group
    run_group, parent_ids = await create_job_group(
        conn,
        group_size=parent_group_size,
        queue=queue,
    )

    # Create dependent job
    dependent_id = await create_job(
        conn,
        job_class="test.AggregatorJob",
        kwargs={"group_id": run_group},
        queue=queue,
        state="waiting",
        waitfor_group=run_group,
    )

    return run_group, parent_ids, dependent_id


async def get_job(conn, job_id: int) -> Optional[dict]:
    """
    Fetch a job by ID.

    Args:
        conn: Database connection
        job_id: Job ID

    Returns:
        dict or None: Job record
    """
    return await conn.fetchrow("SELECT * FROM jorb WHERE id = $1", job_id)


async def get_jobs_by_state(conn, state: str) -> list[dict]:
    """
    Fetch all jobs in a given state.

    Args:
        conn: Database connection
        state: Job state

    Returns:
        list[dict]: Job records
    """
    return await conn.fetch("SELECT * FROM jorb WHERE state = $1 ORDER BY id", state)


async def count_jobs_by_state(conn, state: str) -> int:
    """
    Count jobs in a given state.

    Args:
        conn: Database connection
        state: Job state

    Returns:
        int: Number of jobs
    """
    return await conn.fetchval("SELECT count(*) FROM jorb WHERE state = $1", state)
