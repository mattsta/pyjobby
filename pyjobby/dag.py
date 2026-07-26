"""
DAG Builder - Dynamic Job Graphs (Phase 2)

Provides a clean API for building DAGs (Directed Acyclic Graphs) of jobs
with automatic dependency resolution and parallel execution.

Example:
    dag = DAGBuilder(name='ETL Pipeline')

    # Parallel data fetches
    fetch1 = dag.add('FetchAPI1', {'url': '...'})
    fetch2 = dag.add('FetchAPI2', {'url': '...'})

    # Wait for all fetches, then transform
    transform = dag.add('TransformAll', depends_on=[fetch1, fetch2])

    # Execute DAG
    job_ids = await dag.execute(client)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import asyncpg  # type: ignore[import-untyped]
from loguru import logger

if TYPE_CHECKING:
    from .client import JobClient


@dataclass
class DAGNode:
    """A node in the DAG representing a job"""

    job_class: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    depends_on: list[DAGNode] = field(default_factory=list)
    job_id: int | None = None
    node_id: str = ""  # Internal unique ID
    _job_options: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.node_id)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DAGNode):
            return False
        return self.node_id == other.node_id


class DAGBuilder:
    """
    Builder for constructing job dependency graphs (DAGs).

    Features:
    - Automatic cycle detection
    - Topological sorting for execution order
    - Parallel execution of independent jobs
    - Dependency tracking
    - Named DAGs for monitoring
    """

    def __init__(self, name: str | None = None, **common_options: Any):
        """
        Create a new DAG builder.

        Args:
            name: Optional DAG name for debugging/monitoring
            **common_options: Options applied to all jobs (queue, priority, etc.)
        """
        self.name = name
        self.common_options = common_options
        self.nodes: list[DAGNode] = []

    def add(
        self,
        job_class: str,
        kwargs: dict[str, Any] | None = None,
        depends_on: list[DAGNode] | None = None,
        **options: Any,
    ) -> DAGNode:
        """
        Add a job to the DAG.

        Args:
            job_class: Job class name
            kwargs: Job arguments
            depends_on: List of upstream DAGNode objects this job depends on
            **options: Job-specific options (override common_options)

        Returns:
            DAGNode that can be passed to depends_on of other jobs

        Example:
            job1 = dag.add('Step1', {'input': 'data'})
            job2 = dag.add('Step2', depends_on=[job1])
        """
        node = DAGNode(
            job_class=job_class,
            kwargs=kwargs or {},
            depends_on=depends_on or [],
            node_id=str(uuid.uuid4()),
        )

        # Merge common and job-specific options
        node._job_options = {**self.common_options, **options}

        self.nodes.append(node)
        return node

    def validate(self) -> None:
        """
        Validate DAG structure.

        Checks:
        1. No cycles (must be acyclic)
        2. All dependencies are in the DAG

        Raises:
            ValueError: If DAG is invalid
        """
        # Check all dependencies are in this DAG
        for node in self.nodes:
            for dep in node.depends_on:
                if dep not in self.nodes:
                    raise ValueError(
                        f"Node {node.job_class} depends on {dep.job_class} which is not in this DAG"
                    )

        # Check for cycles using DFS
        visited: set[DAGNode] = set()
        rec_stack: set[DAGNode] = set()

        def has_cycle(node: DAGNode) -> bool:
            visited.add(node)
            rec_stack.add(node)

            for dep in node.depends_on:
                if dep not in visited:
                    if has_cycle(dep):
                        return True
                elif dep in rec_stack:
                    return True

            rec_stack.remove(node)
            return False

        for node in self.nodes:
            if node not in visited and has_cycle(node):
                raise ValueError("DAG contains a cycle - dependencies must be acyclic")

    def topological_sort(self) -> list[list[DAGNode]]:
        """
        Return nodes in topological order, grouped by execution level.

        Nodes in the same level have no dependencies on each other
        and can be executed in parallel.

        Returns:
            List of levels, where each level is a list of nodes that can
            execute in parallel.

        Example:
            [[node1, node2], [node3], [node4, node5]]
            # Level 0: node1 and node2 can run in parallel
            # Level 1: node3 runs after level 0 completes
            # Level 2: node4 and node5 run in parallel after node3
        """
        # Calculate in-degree for each node
        in_degree: dict[DAGNode, int] = dict.fromkeys(self.nodes, 0)
        for node in self.nodes:
            for dep in node.depends_on:
                in_degree[node] += 1

        # Build levels
        levels: list[list[DAGNode]] = []
        remaining = set(self.nodes)

        while remaining:
            # Find nodes with no remaining dependencies
            level = [node for node in remaining if in_degree[node] == 0]

            if not level:
                raise ValueError("DAG contains a cycle or invalid dependencies")

            levels.append(level)

            # Remove this level and update in-degrees
            for node in level:
                remaining.remove(node)
                # Find nodes that depend on this one
                for other in remaining:
                    if node in other.depends_on:
                        in_degree[other] -= 1

        return levels

    async def execute(self, client: JobClient) -> dict[DAGNode, int]:
        """
        Execute the DAG using the provided client.

        Args:
            client: JobClient instance

        Returns:
            Dict mapping DAGNode to job_id

        Raises:
            ValueError: If DAG is invalid
        """
        # Validate structure
        self.validate()

        # Get execution levels
        levels = self.topological_sort()

        # Create DAG record
        dag_id = await client.pool.fetchval(
            """
            INSERT INTO jorb_dag (name, metadata)
            VALUES ($1, $2)
            RETURNING id
            """,
            self.name,
            {"total_nodes": len(self.nodes)},
        )

        logger.info(
            f"DAG '{self.name}' ({dag_id}): Starting execution of "
            f"{len(self.nodes)} jobs in {len(levels)} levels"
        )

        # Track node -> job_id mapping
        node_to_job: dict[DAGNode, int] = {}

        # Execute level by level
        for level_num, level in enumerate(levels):
            logger.info(
                f"DAG '{self.name}' ({dag_id}): Executing level {level_num} "
                f"({len(level)} jobs in parallel)"
            )

            # Enqueue all jobs in this level
            for node in level:
                # Build waitfor list from dependencies
                waitfor_jobs = [node_to_job[dep] for dep in node.depends_on]

                # Merge node options with kwargs
                job_options = {**node._job_options}

                # Handle dependencies
                if len(waitfor_jobs) == 1:
                    # Single dependency - use waitfor_job
                    job_options["waitfor_job"] = waitfor_jobs[0]
                elif len(waitfor_jobs) > 1:
                    # Multiple dependencies - create a group
                    # Use the first job's ID as the group ID
                    group_id = waitfor_jobs[0]

                    # Update all dependencies to be in the same group
                    await client.pool.execute(
                        "UPDATE jorb SET run_group = $1 WHERE id = ANY($2)",
                        group_id,
                        waitfor_jobs,
                    )

                    job_options["waitfor_group"] = group_id

                # Enqueue job
                job_id = await client.enqueue(
                    node.job_class, **node.kwargs, **job_options
                )

                # Tag with DAG ID
                await client.pool.execute(
                    "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job_id
                )

                # Track mapping
                node_to_job[node] = job_id
                node.job_id = job_id

            job_ids = [node_to_job[n] for n in level]
            logger.info(
                f"DAG '{self.name}' ({dag_id}): Level {level_num} enqueued (job IDs: {job_ids})"
            )

        logger.info(
            f"DAG '{self.name}' ({dag_id}): All {len(self.nodes)} jobs enqueued"
        )

        return node_to_job

    def visualize(self) -> str:
        """
        Generate a text visualization of the DAG.

        Returns:
            ASCII art representation of the DAG
        """
        lines = [f"DAG: {self.name or 'unnamed'}", "=" * 50, ""]

        try:
            levels = self.topological_sort()
            for level_num, level in enumerate(levels):
                lines.append(f"Level {level_num}:")
                for node in level:
                    deps = ", ".join(d.job_class for d in node.depends_on) or "none"
                    lines.append(f"  - {node.job_class} (depends on: {deps})")
                lines.append("")
        except ValueError as e:
            lines.append(f"ERROR: {e}")
            lines.append("")
            lines.append("Nodes:")
            for node in self.nodes:
                deps = ", ".join(d.job_class for d in node.depends_on) or "none"
                lines.append(f"  - {node.job_class} (depends on: {deps})")

        return "\n".join(lines)


# Convenience functions


async def execute_dag(client: JobClient, dag: DAGBuilder) -> dict[DAGNode, int]:
    """Execute a DAG. Shortcut for dag.execute(client)."""
    return await dag.execute(client)


async def get_dag_status(pool: asyncpg.Pool, dag_id: int) -> dict[str, Any]:
    """
    Get status of a DAG execution.

    Returns:
        Dict with DAG status information
    """
    status = await pool.fetchrow(
        """
        SELECT * FROM jorb_dag_status WHERE dag_id = $1
        """,
        dag_id,
    )

    if not status:
        return {"error": "DAG not found"}

    return dict(status)


async def wait_for_dag(
    pool: asyncpg.Pool, dag_id: int, timeout: int = 3600, poll_interval: int = 1
) -> bool:
    """
    Wait for DAG to complete.

    Args:
        pool: Database connection pool
        dag_id: DAG ID
        timeout: Maximum wait time in seconds
        poll_interval: How often to check status (seconds)

    Returns:
        True if DAG completed successfully, False if failed or timeout
    """
    import asyncio
    import time

    start = time.time()

    while True:
        status = await get_dag_status(pool, dag_id)

        if "error" in status:
            logger.error(f"DAG {dag_id} not found")
            return False

        state = status.get("dag_state")
        if state == "complete":
            logger.info(
                f"DAG {dag_id} completed: {status['finished_jobs']}/{status['total_jobs']} jobs finished"
            )
            return True
        elif state == "failed":
            logger.error(
                f"DAG {dag_id} failed: {status['crashed_jobs']} crashed, "
                f"{status['finished_jobs']}/{status['total_jobs']} finished"
            )
            return False

        if time.time() - start > timeout:
            logger.error(
                f"DAG {dag_id} timeout after {timeout}s: "
                f"{status['finished_jobs']}/{status['total_jobs']} finished, "
                f"{status['running_jobs']} running, {status['queued_jobs']} queued"
            )
            return False

        await asyncio.sleep(poll_interval)
