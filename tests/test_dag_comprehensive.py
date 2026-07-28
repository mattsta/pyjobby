"""
Comprehensive tests for DAG (Directed Acyclic Graph) functionality.

Tests cover:
- DAGNode creation and equality
- DAG building and validation
- Cycle detection
- Topological sorting
- DAG execution
- Parallel execution levels
- Error handling
- Integration with JobClient
"""

import pytest

from pyjobby.dag import DAGBuilder, DAGNode

# =============================================================================
# Test Fixtures
# =============================================================================




# =============================================================================
# DAGNode Tests
# =============================================================================


class TestDAGNode:
    """Tests for DAGNode functionality."""

    def test_dagnode_creation(self):
        """Test creating a DAGNode."""
        node = DAGNode(job_class="test.Job", kwargs={"data": "test"}, node_id="test123")

        assert node.job_class == "test.Job"
        assert node.kwargs == {"data": "test"}
        assert node.depends_on == []
        assert node.job_id is None
        assert node.node_id == "test123"

    def test_dagnode_with_dependencies(self):
        """Test DAGNode with dependencies."""
        node1 = DAGNode(job_class="test.Job1")
        node2 = DAGNode(job_class="test.Job2", depends_on=[node1])

        assert len(node2.depends_on) == 1
        assert node2.depends_on[0] == node1

    def test_dagnode_equality(self):
        """Test DAGNode equality by node_id."""
        node1 = DAGNode(job_class="test.Job", node_id="abc123")
        node2 = DAGNode(job_class="test.Job", node_id="abc123")
        node3 = DAGNode(job_class="test.Job", node_id="xyz789")

        assert node1 == node2
        assert node1 != node3

    def test_dagnode_hash(self):
        """Test DAGNode can be hashed (used in sets/dicts)."""
        node1 = DAGNode(job_class="test.Job", node_id="abc123")
        node2 = DAGNode(job_class="test.Job", node_id="abc123")

        # Same node_id should hash the same
        assert hash(node1) == hash(node2)

        # Can be used in sets
        node_set = {node1, node2}
        assert len(node_set) == 1

    def test_dagnode_equality_with_non_dagnode(self):
        """Test DAGNode equality with non-DAGNode objects."""
        node = DAGNode(job_class="test.Job", node_id="abc123")

        # Should return False when comparing with non-DAGNode
        assert node != "not a dag node"
        assert node != 123
        assert node != None
        assert node != {"node_id": "abc123"}


# =============================================================================
# DAGBuilder Tests
# =============================================================================


class TestDAGBuilder:
    """Tests for DAG building functionality."""

    def test_dag_creation(self):
        """Test creating a DAG."""
        dag = DAGBuilder(name="Test DAG")

        assert dag.name == "Test DAG"
        assert dag.nodes == []
        assert dag.common_options == {}

    def test_dag_with_common_options(self):
        """Test DAG with common options."""
        dag = DAGBuilder(name="Test DAG", queue="high-priority", priority=500)

        assert dag.common_options == {"queue": "high-priority", "priority": 500}

    def test_add_single_node(self):
        """Test adding a single node."""
        dag = DAGBuilder()
        node = dag.add("test.Job", {"data": "test"})

        assert len(dag.nodes) == 1
        assert node.job_class == "test.Job"
        assert node.kwargs == {"data": "test"}
        assert node in dag.nodes

    def test_add_multiple_independent_nodes(self):
        """Test adding multiple independent nodes."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1", {"data": "1"})
        node2 = dag.add("test.Job2", {"data": "2"})
        node3 = dag.add("test.Job3", {"data": "3"})

        assert len(dag.nodes) == 3
        assert node1.depends_on == []
        assert node2.depends_on == []
        assert node3.depends_on == []

    def test_add_with_dependencies(self):
        """Test adding nodes with dependencies."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2", depends_on=[node1])
        node3 = dag.add("test.Job3", depends_on=[node1, node2])

        assert len(dag.nodes) == 3
        assert node2.depends_on == [node1]
        assert set(node3.depends_on) == {node1, node2}

    def test_add_with_node_specific_options(self):
        """Test adding node with specific options overriding common options."""
        dag = DAGBuilder(queue="default", priority=100)
        node = dag.add("test.Job", priority=500, capability="gpu")

        # Node-specific options should override common options
        assert node._job_options == {
            "queue": "default",
            "priority": 500,
            "capability": "gpu",
        }


# =============================================================================
# DAG Validation Tests
# =============================================================================


class TestDAGValidation:
    """Tests for DAG validation."""

    def test_validate_simple_dag(self):
        """Test validating a simple valid DAG."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2", depends_on=[node1])

        # Should not raise
        dag.validate()

    def test_validate_empty_dag(self):
        """Test validating an empty DAG."""
        dag = DAGBuilder()

        # Should not raise
        dag.validate()

    def test_validate_detects_external_dependency(self):
        """Test that validation detects dependencies outside the DAG."""
        dag1 = DAGBuilder()
        dag2 = DAGBuilder()

        node1 = dag1.add("test.Job1")
        # Try to add node2 to dag2 that depends on node1 from dag1
        node2 = dag2.add("test.Job2", depends_on=[node1])

        # Should raise ValueError
        with pytest.raises(ValueError, match="not in this DAG"):
            dag2.validate()

    def test_validate_detects_self_cycle(self):
        """Test that validation detects self-referencing cycles."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")

        # Manually create a self-cycle
        node1.depends_on = [node1]

        with pytest.raises(ValueError, match="cycle"):
            dag.validate()

    def test_validate_detects_simple_cycle(self):
        """Test that validation detects simple cycles."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2", depends_on=[node1])

        # Create a cycle: node1 -> node2 -> node1
        node1.depends_on = [node2]

        with pytest.raises(ValueError, match="cycle"):
            dag.validate()

    def test_validate_detects_complex_cycle(self):
        """Test that validation detects complex cycles."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2", depends_on=[node1])
        node3 = dag.add("test.Job3", depends_on=[node2])
        node4 = dag.add("test.Job4", depends_on=[node3])

        # Create cycle: node1 -> node2 -> node3 -> node4 -> node1
        node1.depends_on = [node4]

        with pytest.raises(ValueError, match="cycle"):
            dag.validate()


# =============================================================================
# Topological Sort Tests
# =============================================================================


class TestTopologicalSort:
    """Tests for topological sorting."""

    def test_topological_sort_single_node(self):
        """Test topological sort with single node."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")

        levels = dag.topological_sort()

        assert len(levels) == 1
        assert levels[0] == [node1]

    def test_topological_sort_independent_nodes(self):
        """Test topological sort with independent nodes."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2")
        node3 = dag.add("test.Job3")

        levels = dag.topological_sort()

        # All nodes should be in level 0 (can execute in parallel)
        assert len(levels) == 1
        assert set(levels[0]) == {node1, node2, node3}

    def test_topological_sort_linear_chain(self):
        """Test topological sort with linear dependency chain."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2", depends_on=[node1])
        node3 = dag.add("test.Job3", depends_on=[node2])
        node4 = dag.add("test.Job4", depends_on=[node3])

        levels = dag.topological_sort()

        # Should be 4 levels, one for each node
        assert len(levels) == 4
        assert levels[0] == [node1]
        assert levels[1] == [node2]
        assert levels[2] == [node3]
        assert levels[3] == [node4]

    def test_topological_sort_diamond_pattern(self):
        """Test topological sort with diamond dependency pattern."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")  # Root
        node2 = dag.add("test.Job2", depends_on=[node1])  # Left branch
        node3 = dag.add("test.Job3", depends_on=[node1])  # Right branch
        node4 = dag.add("test.Job4", depends_on=[node2, node3])  # Merge

        levels = dag.topological_sort()

        # Should be 3 levels
        assert len(levels) == 3
        assert levels[0] == [node1]
        assert set(levels[1]) == {node2, node3}  # Can execute in parallel
        assert levels[2] == [node4]

    def test_topological_sort_complex_dag(self):
        """Test topological sort with complex DAG."""
        dag = DAGBuilder()

        # Level 0
        n1 = dag.add("Job1")
        n2 = dag.add("Job2")
        n3 = dag.add("Job3")

        # Level 1
        n4 = dag.add("Job4", depends_on=[n1])
        n5 = dag.add("Job5", depends_on=[n2])

        # Level 2
        n6 = dag.add("Job6", depends_on=[n4, n5])
        n7 = dag.add("Job7", depends_on=[n3])

        # Level 3
        n8 = dag.add("Job8", depends_on=[n6, n7])

        levels = dag.topological_sort()

        assert len(levels) == 4
        assert set(levels[0]) == {n1, n2, n3}
        assert set(levels[1]) == {n4, n5, n7}
        assert levels[2] == [n6]
        assert levels[3] == [n8]


# =============================================================================
# DAG Execution Tests
# =============================================================================


@pytest.mark.asyncio
class TestDAGExecution:
    """Tests for DAG execution against live database."""

    async def test_execute_empty_dag(self, db_pool, job_client):
        """Test executing an empty DAG."""
        dag = DAGBuilder(name="Empty DAG")

        result = await dag.execute(job_client)

        assert result == {}

    async def test_execute_single_job(self, db_pool, job_client):
        """Test executing a DAG with single job."""
        dag = DAGBuilder(name="Single Job DAG")
        node1 = dag.add("test.SimpleJob", {"data": "test"})

        result = await dag.execute(job_client)

        assert len(result) == 1
        assert node1 in result
        assert isinstance(result[node1], int)  # job_id

        # Verify job was created
        job = await job_client.get_job(result[node1])
        assert job is not None
        assert job.job_class == "test.SimpleJob"

    async def test_execute_parallel_jobs(self, db_pool, job_client):
        """Test executing parallel jobs."""
        dag = DAGBuilder(name="Parallel Jobs DAG")
        node1 = dag.add("test.Job1", {"data": "1"})
        node2 = dag.add("test.Job2", {"data": "2"})
        node3 = dag.add("test.Job3", {"data": "3"})

        result = await dag.execute(job_client)

        assert len(result) == 3
        assert node1 in result
        assert node2 in result
        assert node3 in result

        # All should have job IDs
        assert all(isinstance(job_id, int) for job_id in result.values())

    async def test_execute_linear_dag(self, db_pool, job_client):
        """Test executing linear dependency chain."""
        dag = DAGBuilder(name="Linear DAG")
        node1 = dag.add("test.Job1", {"step": 1})
        node2 = dag.add("test.Job2", {"step": 2}, depends_on=[node1])
        node3 = dag.add("test.Job3", {"step": 3}, depends_on=[node2])

        result = await dag.execute(job_client)

        assert len(result) == 3

        # Verify dependencies were set correctly
        job2 = await job_client.get_job_full(result[node2])
        job3 = await job_client.get_job_full(result[node3])

        assert job2["waitfor_job"] == result[node1]
        assert job3["waitfor_job"] == result[node2]

    async def test_execute_diamond_dag(self, db_pool, job_client):
        """Test executing diamond pattern DAG."""
        dag = DAGBuilder(name="Diamond DAG")
        root = dag.add("test.Root")
        left = dag.add("test.Left", depends_on=[root])
        right = dag.add("test.Right", depends_on=[root])
        merge = dag.add("test.Merge", depends_on=[left, right])

        result = await dag.execute(job_client)

        assert len(result) == 4

        # Verify DAG structure in database
        async with db_pool.acquire() as conn:
            # Check that all jobs belong to same DAG
            jobs = await conn.fetch(
                "SELECT id, dag_id FROM jorb WHERE id = ANY($1::bigint[])",
                list(result.values()),
            )
            dag_ids = {job["dag_id"] for job in jobs}
            assert len(dag_ids) == 1  # All jobs in same DAG
            assert None not in dag_ids  # DAG ID is set

    async def test_execute_with_common_options(self, db_pool, job_client):
        """Test executing DAG with common options."""
        dag = DAGBuilder(
            name="Common Options DAG", queue="high-priority", priority=500, uid=12345
        )
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2")

        result = await dag.execute(job_client)

        # Verify options were applied
        for job_id in result.values():
            job = await job_client.get_job_full(job_id)
            assert job["queue"] == "high-priority"
            assert job["prio"] == 500
            assert job["uid"] == 12345

    async def test_execute_with_node_specific_options(self, db_pool, job_client):
        """Test executing DAG with node-specific options."""
        dag = DAGBuilder(name="Node Options DAG", queue="default", priority=100)
        node1 = dag.add("test.Job1")  # Uses common options
        node2 = dag.add(
            "test.Job2", priority=800, capability="gpu"
        )  # Overrides priority

        result = await dag.execute(job_client)

        # Verify node1 has common options
        job1 = await job_client.get_job_full(result[node1])
        assert job1["queue"] == "default"
        assert job1["prio"] == 100

        # Verify node2 has overridden options
        job2 = await job_client.get_job_full(result[node2])
        assert job2["queue"] == "default"  # Inherited
        assert job2["prio"] == 800  # Overridden
        assert job2["capability"] == "gpu"  # Added

    async def test_execute_rejects_cyclic_dag(self, db_pool, job_client):
        """Test that executing cyclic DAG raises error."""
        dag = DAGBuilder(name="Cyclic DAG")
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2", depends_on=[node1])

        # Create cycle
        node1.depends_on = [node2]

        with pytest.raises(ValueError, match="cycle"):
            await dag.execute(job_client)

    async def test_execute_complex_dag(self, db_pool, job_client):
        """Test executing complex multi-level DAG."""
        dag = DAGBuilder(name="Complex DAG")

        # Simulate ETL pipeline
        # Extract phase (parallel)
        extract1 = dag.add("test.ExtractDB", {"source": "db1"})
        extract2 = dag.add("test.ExtractDB", {"source": "db2"})
        extract3 = dag.add("test.ExtractAPI", {"url": "api.example.com"})

        # Transform phase (depends on extracts)
        transform1 = dag.add(
            "test.Transform", {"type": "normalize"}, depends_on=[extract1]
        )
        transform2 = dag.add(
            "test.Transform", {"type": "normalize"}, depends_on=[extract2]
        )
        transform3 = dag.add("test.Transform", {"type": "parse"}, depends_on=[extract3])

        # Merge phase
        merge = dag.add("test.Merge", depends_on=[transform1, transform2, transform3])

        # Load phase
        load = dag.add("test.LoadWarehouse", depends_on=[merge])

        result = await dag.execute(job_client)

        # Verify all 8 jobs created
        assert len(result) == 8

        # Verify execution levels
        levels = dag.topological_sort()
        assert len(levels) == 4  # Extract, Transform, Merge, Load

        # Verify final load job depends on merge
        load_job = await job_client.get_job_full(result[load])
        assert load_job["waitfor_job"] == result[merge]


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestDAGErrorHandling:
    """Tests for DAG error handling."""

    def test_dag_node_not_in_dag_error_message(self):
        """Test clear error message for external dependencies."""
        dag1 = DAGBuilder()
        dag2 = DAGBuilder()

        node1 = dag1.add("test.Job1")
        node2 = dag2.add("test.Job2", depends_on=[node1])

        with pytest.raises(ValueError) as exc_info:
            dag2.validate()

        assert "not in this DAG" in str(exc_info.value)
        assert "Job2" in str(exc_info.value)
        assert "Job1" in str(exc_info.value)

    def test_cycle_error_message(self):
        """Test clear error message for cycles."""
        dag = DAGBuilder()
        node1 = dag.add("test.Job1")
        node2 = dag.add("test.Job2", depends_on=[node1])
        node1.depends_on = [node2]

        with pytest.raises(ValueError) as exc_info:
            dag.validate()

        assert "cycle" in str(exc_info.value).lower()
        assert "acyclic" in str(exc_info.value).lower()

    def test_topological_sort_cycle_detection(self):
        """Test that topological_sort raises error on cycles."""
        dag = DAGBuilder()

        # Create a cycle
        node_a = dag.add("JobA")
        node_b = dag.add("JobB", depends_on=[node_a])
        node_c = dag.add("JobC", depends_on=[node_b])
        node_a.depends_on.append(node_c)  # Create cycle: A -> B -> C -> A

        with pytest.raises(ValueError, match="cycle"):
            dag.topological_sort()


# =============================================================================
# DAG Visualization Tests
# =============================================================================


class TestDAGVisualization:
    """Tests for DAG visualization functionality."""

    def test_visualize_simple_dag(self):
        """Test visualization of a simple DAG."""
        dag = DAGBuilder(name="Test Pipeline")
        node1 = dag.add("Step1")
        node2 = dag.add("Step2", depends_on=[node1])

        viz = dag.visualize()

        assert "Test Pipeline" in viz
        assert "Level 0:" in viz
        assert "Step1" in viz
        assert "Level 1:" in viz
        assert "Step2" in viz

    def test_visualize_unnamed_dag(self):
        """Test visualization of unnamed DAG."""
        dag = DAGBuilder()
        dag.add("Job1")

        viz = dag.visualize()

        assert "unnamed" in viz
        assert "Job1" in viz

    def test_visualize_shows_dependencies(self):
        """Test that visualization shows dependencies."""
        dag = DAGBuilder(name="Dependency Test")
        node1 = dag.add("Parent")
        node2 = dag.add("Child", depends_on=[node1])

        viz = dag.visualize()

        assert "Parent" in viz
        assert "Child" in viz
        assert "depends on: Parent" in viz

    def test_visualize_cyclic_dag_shows_error(self):
        """Test visualization of DAG with cycle shows error."""
        dag = DAGBuilder(name="Broken DAG")
        node_a = dag.add("JobA")
        node_b = dag.add("JobB", depends_on=[node_a])
        node_a.depends_on.append(node_b)  # Create cycle

        viz = dag.visualize()

        assert "ERROR:" in viz
        assert "Nodes:" in viz
        assert "JobA" in viz
        assert "JobB" in viz


# =============================================================================
# DAG Helper Function Tests
# =============================================================================


class TestDAGHelperFunctions:
    """Tests for DAG helper functions."""

    @pytest.mark.asyncio
    async def test_execute_dag_convenience_function(self, job_client, db_pool):
        """Test execute_dag convenience function."""
        from pyjobby.dag import execute_dag

        # Clean database
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM jorb")
            await conn.execute("DELETE FROM jorb_dag")

        # Create simple DAG
        dag = DAGBuilder(name="Helper Test")
        dag.add("test.Job1", {"arg": 1})

        # Execute using helper function
        result = await execute_dag(job_client, dag)

        assert len(result) == 1
        assert all(isinstance(job_id, int) for job_id in result.values())

    @pytest.mark.asyncio
    async def test_get_dag_status_not_found(self, db_pool):
        """Test get_dag_status with non-existent DAG."""
        from pyjobby.dag import get_dag_status

        status = await get_dag_status(db_pool, 999999)

        assert "error" in status
        assert status["error"] == "DAG not found"

    @pytest.mark.asyncio
    async def test_get_dag_status_found(self, job_client, db_pool):
        """Test get_dag_status with existing DAG."""
        from pyjobby.dag import get_dag_status

        # Clean database
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM jorb")
            await conn.execute("DELETE FROM jorb_dag")

        # Create and execute DAG
        dag = DAGBuilder(name="Status Test")
        dag.add("test.Job1", {"arg": 1})
        result = await dag.execute(job_client)

        # Get DAG ID from result
        dag_id = await db_pool.fetchval("SELECT MAX(id) FROM jorb_dag")

        # Get status
        status = await get_dag_status(db_pool, dag_id)

        # schema v1 jorb_dag_status columns
        assert status["dag_id"] == dag_id
        assert status["name"] == "Status Test"
        assert status["completed"] is None
        assert status["total_jobs"] == len(result)
        assert status["pending_jobs"] == len(result)
        assert status["finished_jobs"] == 0
        assert status["crashed_jobs"] == 0
        assert status["cancelled_jobs"] == 0

    @pytest.mark.asyncio
    async def test_wait_for_dag_success(self, job_client, db_pool):
        """Test wait_for_dag returns True when DAG completes successfully."""
        from pyjobby.dag import wait_for_dag

        # Clean database
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM jorb")
            await conn.execute("DELETE FROM jorb_dag")

        # Create simple DAG that will complete
        dag = DAGBuilder(name="Wait Success Test")
        dag.add("test.Job1", {"arg": 1})
        result = await dag.execute(job_client)

        # Get DAG ID
        dag_id = await db_pool.fetchval("SELECT MAX(id) FROM jorb_dag")

        # Mark all jobs as finished to simulate completion
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jorb
                SET state = 'finished'
                WHERE dag_id = $1
            """,
                dag_id,
            )

        # Wait for DAG (should return True immediately since jobs are finished)
        success = await wait_for_dag(db_pool, dag_id, timeout=5, poll_interval=0.1)

        assert success is True

    @pytest.mark.asyncio
    async def test_wait_for_dag_failure(self, job_client, db_pool):
        """Test wait_for_dag returns False when DAG fails."""
        from pyjobby.dag import wait_for_dag

        # Clean database
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM jorb")
            await conn.execute("DELETE FROM jorb_dag")

        # Create DAG
        dag = DAGBuilder(name="Wait Failure Test")
        dag.add("test.Job1", {"arg": 1})
        result = await dag.execute(job_client)

        # Get DAG ID
        dag_id = await db_pool.fetchval("SELECT MAX(id) FROM jorb_dag")

        # Mark jobs as crashed to simulate failure
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jorb
                SET state = 'crashed', error_message = 'Test failure'
                WHERE dag_id = $1
            """,
                dag_id,
            )

        # Wait for DAG (should return False due to crashed jobs)
        success = await wait_for_dag(db_pool, dag_id, timeout=5, poll_interval=0.1)

        assert success is False

    @pytest.mark.asyncio
    async def test_wait_for_dag_timeout(self, job_client, db_pool):
        """Test wait_for_dag returns False when timeout is exceeded."""
        from pyjobby.dag import wait_for_dag

        # Clean database
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM jorb")
            await conn.execute("DELETE FROM jorb_dag")

        # Create DAG
        dag = DAGBuilder(name="Wait Timeout Test")
        dag.add("test.Job1", {"arg": 1})
        result = await dag.execute(job_client)

        # execute() records the created DAG's id on the builder — no raw
        # SQL against internal columns needed
        dag_id = dag.dag_id
        assert dag_id is not None

        # Leave jobs in running state (not finished, not crashed)
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jorb
                SET state = 'running'
                WHERE dag_id = $1
            """,
                dag_id,
            )

        # A timeout raises — it is not an outcome, the DAG is still running
        with pytest.raises(TimeoutError):
            await wait_for_dag(db_pool, dag_id, timeout=0.2, poll_interval=0.05)

    @pytest.mark.asyncio
    async def test_wait_for_dag_not_found(self, db_pool):
        """A nonexistent DAG raises immediately rather than reading as a
        real failure."""
        from pyjobby.dag import wait_for_dag

        with pytest.raises(LookupError):
            await wait_for_dag(db_pool, 999999, timeout=1, poll_interval=0.1)


class TestDAGCompletionTrigger:
    """jorb_dag.completed is stamped by trigger when a DAG's last job ends.

    Recorded in the database rather than by any one writer, so the worker,
    the monitor, and operator tooling all keep it truthful."""

    @pytest.mark.asyncio
    async def test_completed_stamped_when_last_job_finishes(
        self, db_pool, unique_queue
    ):
        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id", "completion test"
        )
        job_ids = [
            await db_pool.fetchval(
                """INSERT INTO jorb (job_class, queue, state, dag_id)
                   VALUES ('tests.dxe_jobs.OkJob', $1, 'running', $2) RETURNING id""",
                unique_queue,
                dag_id,
            )
            for _ in range(2)
        ]

        # first job finishing leaves the DAG open
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_ids[0]
        )
        assert (
            await db_pool.fetchval(
                "SELECT completed FROM jorb_dag WHERE id = $1", dag_id
            )
            is None
        )

        # the last one closes it
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_ids[1]
        )
        completed = await db_pool.fetchval(
            "SELECT completed FROM jorb_dag WHERE id = $1", dag_id
        )
        assert completed is not None

    @pytest.mark.asyncio
    async def test_crashed_and_cancelled_jobs_also_complete_the_dag(
        self, db_pool, unique_queue
    ):
        """A DAG is done when nothing is pending — success is not required."""
        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id", "terminal mix"
        )
        crashed = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state, dag_id)
               VALUES ('tests.dxe_jobs.FailJob', $1, 'running', $2) RETURNING id""",
            unique_queue,
            dag_id,
        )
        cancelled = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state, dag_id)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'running', $2) RETURNING id""",
            unique_queue,
            dag_id,
        )

        await db_pool.execute(
            "UPDATE jorb SET state = 'crashed' WHERE id = $1", crashed
        )
        await db_pool.execute(
            "UPDATE jorb SET state = 'cancelled' WHERE id = $1", cancelled
        )

        assert (
            await db_pool.fetchval(
                "SELECT completed FROM jorb_dag WHERE id = $1", dag_id
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_completion_timestamp_is_not_overwritten(self, db_pool, unique_queue):
        """Re-running a job in a finished DAG keeps the original stamp."""
        dag_id = await db_pool.fetchval(
            "INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id", "stable stamp"
        )
        job_id = await db_pool.fetchval(
            """INSERT INTO jorb (job_class, queue, state, dag_id)
               VALUES ('tests.dxe_jobs.OkJob', $1, 'running', $2) RETURNING id""",
            unique_queue,
            dag_id,
        )

        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
        )
        first = await db_pool.fetchval(
            "SELECT completed FROM jorb_dag WHERE id = $1", dag_id
        )

        # operator requeues and it finishes again
        await db_pool.execute("UPDATE jorb SET state = 'queued' WHERE id = $1", job_id)
        await db_pool.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job_id
        )

        assert (
            await db_pool.fetchval(
                "SELECT completed FROM jorb_dag WHERE id = $1", dag_id
            )
            == first
        )
