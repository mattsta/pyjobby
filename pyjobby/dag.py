"""
DAG Builder - dynamic job graphs

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
    # Identity for hashing/equality, so it must be unique per node even when
    # a node is constructed directly instead of through DAGBuilder.add().
    node_id: str = field(default_factory=lambda: str(uuid.uuid4()))
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
        #: Set by execute(): the jorb_dag row this run created. This is the
        #: handle wait_for_dag()/get_dag_status() take — without it a caller
        #: had to dig dag_id out of a job row with raw SQL.
        self.dag_id: int | None = None

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
        # De-duplicate dependencies: the same upstream listed twice is one
        # edge, and duplicates would otherwise inflate the in-degree in
        # topological_sort() beyond what it can ever decrement.
        deps: list[DAGNode] = []
        for dep in depends_on or []:
            if dep not in deps:
                deps.append(dep)

        node = DAGNode(
            job_class=job_class,
            kwargs=kwargs or {},
            depends_on=deps,
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

        # Raises if the fan-in nodes cannot be expressed with run_group
        self.dependency_groups()

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

    def dependency_groups(self) -> dict[DAGNode, list[DAGNode]]:
        """
        Assign a run_group to every fan-in node (a node with 2+ dependencies).

        A job waits for several upstreams via ``waitfor_group``, which fires
        when every job carrying that ``run_group`` has finished. ``run_group``
        is a single column, so a job can belong to exactly ONE group: two
        fan-in nodes that share a dependency MUST share a group, and that
        group is the union of their dependency sets. Sharing therefore makes
        a node wait for more than it strictly depends on — never for less.

        Returns:
            Dict mapping each fan-in node to the members of the group it
            waits for (a superset of its dependencies). Nodes with 0 or 1
            dependencies are absent — they need no group.

        Raises:
            ValueError: If a merged group would contain a node that is
                downstream of one of its waiters, which would deadlock.
        """
        fan_in = [node for node in self.nodes if len(node.depends_on) > 1]
        if not fan_in:
            return {}

        # Union-find over dependency sets: overlapping sets become one group.
        parent: dict[DAGNode, DAGNode] = {}

        def find(node: DAGNode) -> DAGNode:
            root = parent.setdefault(node, node)
            while root is not parent[root]:
                root = parent[root]
            parent[node] = root
            return root

        for node in fan_in:
            first, *rest = node.depends_on
            for dep in rest:
                parent[find(dep)] = find(first)

        members: dict[DAGNode, list[DAGNode]] = {}
        for node in fan_in:
            for dep in node.depends_on:
                members.setdefault(find(dep), []).append(dep)
        # Preserve DAG order and drop the duplicates union-find collapsed
        for root, group in members.items():
            seen = set(group)
            members[root] = [node for node in self.nodes if node in seen]

        # A group that contains a node downstream of one of its waiters can
        # never finish: the waiter blocks the very job it is waiting for.
        downstream = self._downstream_sets()
        for node in fan_in:
            group = members[find(node.depends_on[0])]
            blocked = [other for other in group if other in downstream[node]]
            if blocked:
                names = ", ".join(other.job_class for other in blocked)
                raise ValueError(
                    f"Node {node.job_class} shares a dependency with a fan-in "
                    f"node it is upstream of, so its wait group would contain "
                    f"{names}, which cannot finish until {node.job_class} "
                    f"does. Split the shared dependency into separate jobs."
                )

        return {node: members[find(node.depends_on[0])] for node in fan_in}

    def _downstream_sets(self) -> dict[DAGNode, set[DAGNode]]:
        """Map each node to itself plus every node reachable from it."""
        dependents: dict[DAGNode, list[DAGNode]] = {node: [] for node in self.nodes}
        for node in self.nodes:
            for dep in node.depends_on:
                dependents[dep].append(node)

        downstream: dict[DAGNode, set[DAGNode]] = {}
        for start in self.nodes:
            reached = {start}
            stack = [start]
            while stack:
                for child in dependents[stack.pop()]:
                    if child not in reached:
                        reached.add(child)
                        stack.append(child)
            downstream[start] = reached
        return downstream

    async def execute(self, client: JobClient) -> dict[DAGNode, int]:
        """
        Execute the DAG using the provided client.

        The whole graph is created in ONE transaction: no job of the DAG is
        visible to a worker until every job and every dependency link exists.
        That is what makes the graph safe to submit against live workers — a
        level-0 job that finished while later levels were still being written
        would leave its dependents blocked forever, because the wake-up is
        performed by the worker that finishes the upstream job, not by a
        trigger. It also means a mid-way failure leaves no partial DAG.

        Args:
            client: JobClient instance

        Returns:
            Dict mapping DAGNode to job_id

        Raises:
            ValueError: If DAG is invalid
        """
        self.validate()
        levels = self.topological_sort()
        wait_groups = self.dependency_groups()

        # Each group is keyed by its first member; group_id holds the
        # run_group value, which is the job id of whichever member is
        # created first (job ids are only known as we go).
        group_key: dict[DAGNode, DAGNode] = {
            member: members[0] for members in wait_groups.values() for member in members
        }

        node_to_job: dict[DAGNode, int] = {}
        group_id: dict[DAGNode, int] = {}
        group_members: dict[DAGNode, list[int]] = {}
        edges: list[tuple[int, int]] = []

        async with client.pool.acquire() as conn, conn.transaction():
            dag_id: int = await conn.fetchval(
                """
                INSERT INTO jorb_dag (name, metadata)
                VALUES ($1, $2)
                RETURNING id
                """,
                self.name,
                {"total_nodes": len(self.nodes)},
            )

            logger.info(
                f"DAG '{self.name}' ({dag_id}): Creating "
                f"{len(self.nodes)} jobs in {len(levels)} levels"
            )

            for level in levels:
                for node in level:
                    job_options = {**node._job_options}

                    if len(node.depends_on) == 1:
                        job_options["waitfor_job"] = node_to_job[node.depends_on[0]]
                    elif node.depends_on:
                        # Every member of the group is a dependency of some
                        # earlier level, so the group already has an id.
                        key = group_key[node.depends_on[0]]
                        job_options["waitfor_group"] = group_id[key]

                    job_id = await client.enqueue_in_transaction(
                        conn, node.job_class, **node.kwargs, **job_options
                    )

                    node_to_job[node] = job_id
                    node.job_id = job_id
                    edges.extend((job_id, node_to_job[dep]) for dep in node.depends_on)

                    member_of = group_key.get(node)
                    if member_of is not None:
                        group_id.setdefault(member_of, job_id)
                        group_members.setdefault(member_of, []).append(job_id)

            if node_to_job:
                await conn.execute(
                    "UPDATE jorb SET dag_id = $1 WHERE id = ANY($2)",
                    dag_id,
                    list(node_to_job.values()),
                )
            for key, member_ids in group_members.items():
                await conn.execute(
                    "UPDATE jorb SET run_group = $1 WHERE id = ANY($2)",
                    group_id[key],
                    member_ids,
                )
            if edges:
                await conn.executemany(
                    "INSERT INTO jorb_dependencies (job_id, depends_on) "
                    "VALUES ($1, $2)",
                    edges,
                )

        logger.info(
            f"DAG '{self.name}' ({dag_id}): All {len(self.nodes)} jobs enqueued "
            f"(job IDs: {sorted(node_to_job.values())})"
        )

        self.dag_id = dag_id
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
    pool: asyncpg.Pool,
    dag_id: int,
    timeout: float | None = None,
    poll_interval: float = 1.0,
) -> bool:
    """
    Wait for a DAG to reach its outcome.

    The three ways this can end are three different answers, and they are
    reported as three different things so no caller has to guess which one
    a `False` meant:

    * every job finished           -> returns True
    * a job crashed or cancelled   -> returns False (the DAG's outcome:
      everything downstream stays blocked, so waiting longer cannot help;
      get_dag_status() has the counts)
    * `timeout` elapsed first      -> raises TimeoutError (NOT an outcome:
      the DAG is still running and may yet go either way)
    * the DAG does not exist       -> raises LookupError immediately

    Args:
        pool: Database connection pool
        dag_id: DAG ID (``builder.dag_id`` after ``execute()``)
        timeout: Maximum wait in seconds (default: wait forever, like every
            other wait in the client)
        poll_interval: How often to check status (seconds)
    """
    import asyncio
    import time

    start = time.monotonic()

    while True:
        status = await get_dag_status(pool, dag_id)

        if "error" in status:
            raise LookupError(
                f"DAG {dag_id} does not exist, so it can never complete"
            )

        # Derive terminal state from the jorb_dag_status counts. A crashed or
        # cancelled job means the DAG did not run to completion: everything
        # downstream of it stays blocked, so success is not recoverable and
        # waiting longer cannot change the answer.
        if status["crashed_jobs"] or status["cancelled_jobs"]:
            logger.error(
                f"DAG {dag_id} failed: {status['crashed_jobs']} crashed, "
                f"{status['cancelled_jobs']} cancelled, "
                f"{status['finished_jobs']}/{status['total_jobs']} finished"
            )
            return False

        if status["total_jobs"] and not status["pending_jobs"]:
            logger.info(
                f"DAG {dag_id} completed: "
                f"{status['finished_jobs']}/{status['total_jobs']} jobs finished"
            )
            return True

        if timeout is not None and time.monotonic() - start > timeout:
            raise TimeoutError(
                f"DAG {dag_id} still running after {timeout}s: "
                f"{status['finished_jobs']}/{status['total_jobs']} finished, "
                f"{status['pending_jobs']} still pending"
            )

        await asyncio.sleep(poll_interval)
