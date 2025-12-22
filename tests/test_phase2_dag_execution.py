"""
Phase 2: DAG Execution Tests

Comprehensive tests for Dynamic Job Graphs (DAGs):
- DAG builder API
- Topological sort and level-based execution
- Cycle detection
- Parallel vs sequential execution
- DAG status tracking
"""

import pytest

from pyjobby.dag import DAGBuilder, DAGNode, execute_dag, get_dag_status, wait_for_dag
from tests.utils.factories import create_job, get_job


class TestDAGNode:
    """Test DAGNode dataclass."""

    def test_dag_node_creation(self):
        """Test creating a DAG node."""
        node = DAGNode(
            job_class="test.Job", kwargs={"param": "value"}, node_id="node_0"
        )

        assert node.job_class == "test.Job"
        assert node.kwargs == {"param": "value"}
        assert node.depends_on == []
        assert node.job_id is None

    def test_dag_node_with_dependencies(self):
        """Test node with dependencies."""
        dep1 = DAGNode(job_class="test.Dep1", node_id="node_0")
        dep2 = DAGNode(job_class="test.Dep2", node_id="node_1")
        node = DAGNode(job_class="test.Job", depends_on=[dep1, dep2], node_id="node_2")

        assert len(node.depends_on) == 2
        assert dep1 in node.depends_on
        assert dep2 in node.depends_on

    def test_dag_node_equality(self):
        """Test node equality based on node_id."""
        node1 = DAGNode(job_class="test.Job", node_id="abc")
        node2 = DAGNode(job_class="test.Job", node_id="abc")
        node3 = DAGNode(job_class="test.Job", node_id="def")

        assert node1 == node2
        assert node1 != node3
        assert hash(node1) == hash(node2)
        assert hash(node1) != hash(node3)


class TestDAGBuilder:
    """Test DAG builder API."""

    def test_create_empty_dag(self):
        """Test creating an empty DAG."""
        dag = DAGBuilder(name="Test DAG")

        assert dag.name == "Test DAG"
        assert len(dag.nodes) == 0

    def test_add_single_job(self):
        """Test adding a single job to DAG."""
        dag = DAGBuilder()
        node = dag.add("test.Job", {"param": "value"})

        assert len(dag.nodes) == 1
        assert node.job_class == "test.Job"
        assert node.kwargs == {"param": "value"}
        assert node.depends_on == []

    def test_add_dependent_job(self):
        """Test adding a job with dependencies."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])

        assert len(dag.nodes) == 2
        assert job2.depends_on == [job1]
        assert job1.depends_on == []

    def test_add_with_common_options(self):
        """Test that common_options are applied to all jobs."""
        dag = DAGBuilder(queue="special", priority=50)
        node = dag.add("test.Job")

        assert node._job_options["queue"] == "special"
        assert node._job_options["priority"] == 50

    def test_add_with_override_options(self):
        """Test that job options override common options."""
        dag = DAGBuilder(queue="default", priority=100)
        node = dag.add("test.Job", priority=10)

        assert node._job_options["queue"] == "default"
        assert node._job_options["priority"] == 10  # Override


class TestDAGValidation:
    """Test DAG validation and cycle detection."""

    def test_validate_simple_dag(self):
        """Test validation of simple linear DAG."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])
        job3 = dag.add("test.Job3", depends_on=[job2])

        # Should not raise
        dag.validate()

    def test_validate_empty_dag(self):
        """Test validation of empty DAG."""
        dag = DAGBuilder()
        # Should not raise
        dag.validate()

    def test_detect_simple_cycle(self):
        """Test cycle detection in simple 2-node cycle."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])

        # Manually create cycle (can't do via API)
        job1.depends_on = [job2]

        with pytest.raises(ValueError, match="cycle"):
            dag.validate()

    def test_detect_self_cycle(self):
        """Test detection of self-dependency."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")

        # Manually create self-cycle
        job1.depends_on = [job1]

        with pytest.raises(ValueError, match="cycle"):
            dag.validate()

    def test_detect_complex_cycle(self):
        """Test cycle detection in complex graph."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])
        job3 = dag.add("test.Job3", depends_on=[job2])

        # Create cycle: job1 -> job2 -> job3 -> job1
        job1.depends_on = [job3]

        with pytest.raises(ValueError, match="cycle"):
            dag.validate()

    def test_validate_dependency_not_in_dag(self):
        """Test validation fails if dependency not in DAG."""
        dag1 = DAGBuilder()
        dag2 = DAGBuilder()

        job1 = dag1.add("test.Job1")
        job2 = dag2.add("test.Job2")

        # Manually add external dependency
        job1.depends_on = [job2]

        with pytest.raises(ValueError, match="not in this DAG"):
            dag1.validate()


class TestTopologicalSort:
    """Test topological sort produces correct execution levels."""

    def test_sort_linear_dag(self):
        """Test sorting linear chain produces sequential levels."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])
        job3 = dag.add("test.Job3", depends_on=[job2])

        levels = dag.topological_sort()

        assert len(levels) == 3
        assert levels[0] == [job1]
        assert levels[1] == [job2]
        assert levels[2] == [job3]

    def test_sort_parallel_jobs(self):
        """Test parallel jobs are in same level."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2")
        job3 = dag.add("test.Job3")

        levels = dag.topological_sort()

        assert len(levels) == 1
        assert set(levels[0]) == {job1, job2, job3}

    def test_sort_diamond_pattern(self):
        """Test diamond pattern: A -> B,C -> D."""
        dag = DAGBuilder()
        job_a = dag.add("test.JobA")
        job_b = dag.add("test.JobB", depends_on=[job_a])
        job_c = dag.add("test.JobC", depends_on=[job_a])
        job_d = dag.add("test.JobD", depends_on=[job_b, job_c])

        levels = dag.topological_sort()

        assert len(levels) == 3
        assert levels[0] == [job_a]
        assert set(levels[1]) == {job_b, job_c}  # Parallel
        assert levels[2] == [job_d]

    def test_sort_fan_out_pattern(self):
        """Test fan-out: 1 -> 2,3,4,5."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])
        job3 = dag.add("test.Job3", depends_on=[job1])
        job4 = dag.add("test.Job4", depends_on=[job1])
        job5 = dag.add("test.Job5", depends_on=[job1])

        levels = dag.topological_sort()

        assert len(levels) == 2
        assert levels[0] == [job1]
        assert set(levels[1]) == {job2, job3, job4, job5}

    def test_sort_fan_in_pattern(self):
        """Test fan-in: 1,2,3,4 -> 5."""
        dag = DAGBuilder()
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2")
        job3 = dag.add("test.Job3")
        job4 = dag.add("test.Job4")
        job5 = dag.add("test.Job5", depends_on=[job1, job2, job3, job4])

        levels = dag.topological_sort()

        assert len(levels) == 2
        assert set(levels[0]) == {job1, job2, job3, job4}
        assert levels[1] == [job5]

    def test_sort_complex_dag(self):
        """Test complex multi-level DAG."""
        dag = DAGBuilder()

        # Level 0: Independent jobs
        a = dag.add("test.A")
        b = dag.add("test.B")

        # Level 1: Depend on level 0
        c = dag.add("test.C", depends_on=[a])
        d = dag.add("test.D", depends_on=[a, b])

        # Level 2: Depend on level 1
        e = dag.add("test.E", depends_on=[c, d])

        levels = dag.topological_sort()

        assert len(levels) == 3
        assert set(levels[0]) == {a, b}
        assert set(levels[1]) == {c, d}
        assert levels[2] == [e]


class TestDAGDatabaseSchema:
    """Test DAG database schema."""

    @pytest.mark.asyncio
    async def test_jorb_dag_table_exists(self, db_connection):
        """Verify jorb_dag table exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'jorb_dag'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_jorb_dependencies_table_exists(self, db_connection):
        """Verify jorb_dependencies table exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'jorb_dependencies'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_dag_id_column_exists(self, db_connection):
        """Verify dag_id column exists in jorb table."""
        result = await db_connection.fetchval("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'jorb' AND column_name = 'dag_id'
        """)
        assert result == "dag_id"

    @pytest.mark.asyncio
    async def test_dag_id_index_exists(self, db_connection):
        """Verify index on dag_id column exists."""
        result = await db_connection.fetchval("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'jorb' AND indexname = 'jorb_dag_id_idx'
        """)
        assert result == "jorb_dag_id_idx"

    @pytest.mark.asyncio
    async def test_create_dag_record(self, db_connection):
        """Test creating a DAG record."""
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name, metadata)
            VALUES ($1, $2)
            RETURNING id
        """,
            "Test DAG",
            {"total_nodes": 5},
        )

        assert dag_id is not None

        # Verify record
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["name"] == "Test DAG"
        assert dag["metadata"]["total_nodes"] == 5
        assert dag["completed"] is None


class TestDAGViews:
    """Test DAG monitoring views."""

    @pytest.mark.asyncio
    async def test_jorb_dag_status_view_exists(self, db_connection):
        """Verify jorb_dag_status view exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.views
                WHERE table_name = 'jorb_dag_status'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_jorb_dag_timeline_view_exists(self, db_connection):
        """Verify jorb_dag_timeline view exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.views
                WHERE table_name = 'jorb_dag_timeline'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_dag_status_view_simple(self, db_connection):
        """Test jorb_dag_status view with simple DAG."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Simple DAG",
        )

        # Create 3 jobs in DAG
        for i in range(3):
            await db_connection.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, dag_id, state)
                VALUES ($1, $2, $3, $4, $5)
            """,
                f"test.Job{i}",
                "{}",
                "default",
                dag_id,
                "queued",
            )

        # Query view
        status = await db_connection.fetchrow(
            """
            SELECT * FROM jorb_dag_status WHERE dag_id = $1
        """,
            dag_id,
        )

        assert status["dag_name"] == "Simple DAG"
        assert status["total_jobs"] == 3
        assert status["queued_jobs"] == 3
        assert status["running_jobs"] == 0
        assert status["finished_jobs"] == 0
        assert status["dag_state"] == "queued"

    @pytest.mark.asyncio
    async def test_dag_status_view_mixed_states(self, db_connection):
        """Test jorb_dag_status with mixed job states."""
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Mixed DAG",
        )

        states = ["finished", "finished", "running", "queued", "crashed"]
        for i, state in enumerate(states):
            await db_connection.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, dag_id, state)
                VALUES ($1, $2, $3, $4, $5)
            """,
                f"test.Job{i}",
                "{}",
                "default",
                dag_id,
                state,
            )

        status = await db_connection.fetchrow(
            """
            SELECT * FROM jorb_dag_status WHERE dag_id = $1
        """,
            dag_id,
        )

        assert status["total_jobs"] == 5
        assert status["finished_jobs"] == 2
        assert status["running_jobs"] == 1
        assert status["queued_jobs"] == 1
        assert status["crashed_jobs"] == 1
        assert status["dag_state"] == "failed"  # Has crashed jobs

    @pytest.mark.asyncio
    async def test_dag_status_completion_percentage(self, db_connection):
        """Test completion_percentage calculation."""
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Progress DAG",
        )

        # 10 jobs: 7 finished, 3 running
        for i in range(7):
            await db_connection.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, dag_id, state)
                VALUES ($1, $2, $3, $4, 'finished')
            """,
                f"test.Job{i}",
                "{}",
                "default",
                dag_id,
            )

        for i in range(3):
            await db_connection.execute(
                """
                INSERT INTO jorb (job_class, kwargs, queue, dag_id, state)
                VALUES ($1, $2, $3, $4, 'running')
            """,
                f"test.Job{i + 7}",
                "{}",
                "default",
                dag_id,
            )

        status = await db_connection.fetchrow(
            """
            SELECT * FROM jorb_dag_status WHERE dag_id = $1
        """,
            dag_id,
        )

        assert status["total_jobs"] == 10
        assert status["finished_jobs"] == 7
        assert status["completion_percentage"] == 70.0


class TestDAGSQLFunctions:
    """Test DAG SQL functions."""

    @pytest.mark.asyncio
    async def test_get_dag_dependencies_function_exists(self, db_connection):
        """Verify get_dag_dependencies function exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = 'public' AND p.proname = 'get_dag_dependencies'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_validate_dag_acyclic_function_exists(self, db_connection):
        """Verify validate_dag_acyclic function exists."""
        exists = await db_connection.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM pg_proc p
                JOIN pg_namespace n ON p.pronamespace = n.oid
                WHERE n.nspname = 'public' AND p.proname = 'validate_dag_acyclic'
            )
        """)
        assert exists is True

    @pytest.mark.asyncio
    async def test_validate_dag_acyclic_valid_dag(self, db_connection):
        """Test validate_dag_acyclic with valid DAG."""
        # Create DAG with linear dependencies
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Valid DAG",
        )

        job1_id = await create_job(db_connection, job_class="test.Job1")
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job1_id
        )

        job2_id = await create_job(
            db_connection, job_class="test.Job2", waitfor_job=job1_id
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job2_id
        )

        # Validate
        is_valid = await db_connection.fetchval(
            "SELECT validate_dag_acyclic($1)", dag_id
        )
        assert is_valid is True

    @pytest.mark.asyncio
    async def test_auto_complete_dag_trigger(self, db_connection):
        """Test that DAG auto-completes when all jobs finish."""
        # Create DAG
        dag_id = await db_connection.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Auto Complete DAG",
        )

        # Create 2 jobs in DAG
        job1_id = await create_job(
            db_connection, job_class="test.Job1", state="running"
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job1_id
        )

        job2_id = await create_job(
            db_connection, job_class="test.Job2", state="running"
        )
        await db_connection.execute(
            "UPDATE jorb SET dag_id = $1 WHERE id = $2", dag_id, job2_id
        )

        # Verify DAG not completed
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["completed"] is None

        # Mark first job finished
        await db_connection.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job1_id
        )

        # DAG still not completed
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["completed"] is None

        # Mark second job finished
        await db_connection.execute(
            "UPDATE jorb SET state = 'finished' WHERE id = $1", job2_id
        )

        # DAG should now be completed
        dag = await db_connection.fetchrow(
            "SELECT * FROM jorb_dag WHERE id = $1", dag_id
        )
        assert dag["completed"] is not None


class TestDAGExecution:
    """Test DAG execution with JobClient."""

    @pytest.mark.asyncio
    async def test_execute_simple_linear_dag(self, db_pool):
        """Test executing a simple linear DAG."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)

        # Build DAG
        dag = DAGBuilder(name="Linear Pipeline")
        job1 = dag.add("test.Step1", {"input": "data"})
        job2 = dag.add("test.Step2", depends_on=[job1])
        job3 = dag.add("test.Step3", depends_on=[job2])

        # Execute
        node_to_job = await dag.execute(client)

        # Verify all jobs created
        assert len(node_to_job) == 3
        assert job1.job_id is not None
        assert job2.job_id is not None
        assert job3.job_id is not None

        # Verify dependencies
        job2_record = await get_job(db_pool, job2.job_id)
        job3_record = await get_job(db_pool, job3.job_id)

        assert job2_record["waitfor_job"] == job1.job_id
        assert job3_record["waitfor_job"] == job2.job_id

        # Verify all jobs have dag_id
        for node in [job1, job2, job3]:
            job = await get_job(db_pool, node.job_id)
            assert job["dag_id"] is not None

    @pytest.mark.asyncio
    async def test_execute_parallel_dag(self, db_pool):
        """Test executing DAG with parallel jobs."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)

        # Build DAG with parallel jobs
        dag = DAGBuilder(name="Parallel Pipeline")
        job1 = dag.add("test.Fetch1")
        job2 = dag.add("test.Fetch2")
        job3 = dag.add("test.Fetch3")
        agg = dag.add("test.Aggregate", depends_on=[job1, job2, job3])

        # Execute
        node_to_job = await dag.execute(client)

        # Verify parallel jobs have no dependencies
        for node in [job1, job2, job3]:
            job = await get_job(db_pool, node.job_id)
            assert job["waitfor_job"] is None

        # Verify aggregator waits for group
        agg_job = await get_job(db_pool, agg.job_id)
        assert agg_job["waitfor_group"] is not None

    @pytest.mark.asyncio
    async def test_execute_diamond_dag(self, db_pool):
        """Test executing diamond pattern DAG."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)

        # Build diamond DAG: A -> B,C -> D
        dag = DAGBuilder(name="Diamond Pattern")
        a = dag.add("test.A")
        b = dag.add("test.B", depends_on=[a])
        c = dag.add("test.C", depends_on=[a])
        d = dag.add("test.D", depends_on=[b, c])

        # Execute
        await dag.execute(client)

        # Verify structure
        b_job = await get_job(db_pool, b.job_id)
        c_job = await get_job(db_pool, c.job_id)
        d_job = await get_job(db_pool, d.job_id)

        assert b_job["waitfor_job"] == a.job_id
        assert c_job["waitfor_job"] == a.job_id
        assert d_job["waitfor_group"] is not None  # Waits for both b and c

    @pytest.mark.asyncio
    async def test_dag_with_common_options(self, db_pool):
        """Test DAG with common options applied to all jobs."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)

        # Build DAG with common queue
        dag = DAGBuilder(name="Common Options", queue="special_queue")
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])

        await dag.execute(client)

        # Verify both jobs use special queue
        job1_record = await get_job(db_pool, job1.job_id)
        job2_record = await get_job(db_pool, job2.job_id)

        assert job1_record["queue"] == "special_queue"
        assert job2_record["queue"] == "special_queue"

    @pytest.mark.asyncio
    async def test_execute_dag_validates_first(self, db_pool):
        """Test that execute() validates DAG before running."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)

        dag = DAGBuilder(name="Invalid DAG")
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])

        # Create cycle
        job1.depends_on = [job2]

        with pytest.raises(ValueError, match="cycle"):
            await dag.execute(client)


class TestDAGHelperFunctions:
    """Test DAG helper functions."""

    @pytest.mark.asyncio
    async def test_execute_dag_helper(self, db_pool):
        """Test execute_dag() convenience function."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)
        dag = DAGBuilder(name="Helper Test")
        dag.add("test.Job1")

        # Use helper function
        node_to_job = await execute_dag(client, dag)

        assert len(node_to_job) == 1

    @pytest.mark.asyncio
    async def test_get_dag_status_helper(self, db_pool):
        """Test get_dag_status() helper function."""
        # Create a DAG with jobs
        dag_id = await db_pool.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Status Test DAG",
        )

        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, dag_id, state)
            VALUES ($1, $2, $3, $4, 'finished')
        """,
            "test.Job",
            "{}",
            "default",
            dag_id,
        )

        # Get status
        status = await get_dag_status(db_pool, dag_id)

        assert status["dag_id"] == dag_id
        assert status["dag_name"] == "Status Test DAG"
        assert status["total_jobs"] == 1
        assert status["finished_jobs"] == 1

    @pytest.mark.asyncio
    async def test_get_dag_status_not_found(self, db_pool):
        """Test get_dag_status() with non-existent DAG."""
        status = await get_dag_status(db_pool, 999999)

        assert "error" in status
        assert status["error"] == "DAG not found"

    @pytest.mark.asyncio
    async def test_wait_for_dag_success(self, db_pool):
        """Test wait_for_dag() completes when DAG finishes."""
        # Create completed DAG
        dag_id = await db_pool.fetchval(
            """
            INSERT INTO jorb_dag (name, completed) VALUES ($1, NOW()) RETURNING id
        """,
            "Completed DAG",
        )

        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, dag_id, state)
            VALUES ($1, $2, $3, $4, 'finished')
        """,
            "test.Job",
            "{}",
            "default",
            dag_id,
        )

        # Should return immediately
        result = await wait_for_dag(db_pool, dag_id, timeout=5)
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_dag_failure(self, db_pool):
        """Test wait_for_dag() fails when jobs crash."""
        # Create DAG with crashed job
        dag_id = await db_pool.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Failed DAG",
        )

        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, dag_id, state)
            VALUES ($1, $2, $3, $4, 'crashed')
        """,
            "test.Job",
            "{}",
            "default",
            dag_id,
        )

        # Should detect failure
        result = await wait_for_dag(db_pool, dag_id, timeout=5)
        assert result is False

    @pytest.mark.asyncio
    async def test_wait_for_dag_timeout(self, db_pool):
        """Test wait_for_dag() times out for incomplete DAG."""
        # Create incomplete DAG
        dag_id = await db_pool.fetchval(
            """
            INSERT INTO jorb_dag (name) VALUES ($1) RETURNING id
        """,
            "Incomplete DAG",
        )

        await db_pool.execute(
            """
            INSERT INTO jorb (job_class, kwargs, queue, dag_id, state)
            VALUES ($1, $2, $3, $4, 'running')
        """,
            "test.Job",
            "{}",
            "default",
            dag_id,
        )

        # Should timeout
        result = await wait_for_dag(db_pool, dag_id, timeout=1, poll_interval=0.5)
        assert result is False


class TestDAGVisualization:
    """Test DAG visualization."""

    def test_visualize_simple_dag(self):
        """Test visualizing a simple DAG."""
        dag = DAGBuilder(name="Test Visualization")
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])

        viz = dag.visualize()

        assert "Test Visualization" in viz
        assert "Level 0:" in viz
        assert "Level 1:" in viz
        assert "test.Job1" in viz
        assert "test.Job2" in viz

    def test_visualize_complex_dag(self):
        """Test visualizing diamond pattern."""
        dag = DAGBuilder(name="Diamond")
        a = dag.add("test.A")
        b = dag.add("test.B", depends_on=[a])
        c = dag.add("test.C", depends_on=[a])
        d = dag.add("test.D", depends_on=[b, c])

        viz = dag.visualize()

        assert "Level 0:" in viz
        assert "Level 1:" in viz
        assert "Level 2:" in viz
        assert "test.A" in viz
        assert "test.B" in viz
        assert "test.C" in viz
        assert "test.D" in viz

    def test_visualize_invalid_dag(self):
        """Test visualizing invalid DAG shows error."""
        dag = DAGBuilder(name="Invalid")
        job1 = dag.add("test.Job1")
        job2 = dag.add("test.Job2", depends_on=[job1])

        # Create cycle
        job1.depends_on = [job2]

        viz = dag.visualize()

        assert "ERROR" in viz


class TestComplexDAGPatterns:
    """Test complex real-world DAG patterns."""

    @pytest.mark.asyncio
    async def test_etl_pipeline_pattern(self, db_pool):
        """Test ETL pipeline: Extract (parallel) -> Transform -> Load."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)

        dag = DAGBuilder(name="ETL Pipeline")

        # Extract phase (parallel)
        extract1 = dag.add("etl.ExtractAPI1", {"url": "api1"})
        extract2 = dag.add("etl.ExtractAPI2", {"url": "api2"})
        extract3 = dag.add("etl.ExtractDB", {"table": "users"})

        # Transform phase (waits for all extracts)
        transform = dag.add(
            "etl.TransformAll", depends_on=[extract1, extract2, extract3]
        )

        # Load phase (waits for transform)
        load = dag.add("etl.LoadWarehouse", depends_on=[transform])

        # Execute
        await dag.execute(client)

        # Verify structure
        levels = dag.topological_sort()
        assert len(levels) == 3
        assert len(levels[0]) == 3  # Parallel extracts
        assert len(levels[1]) == 1  # Transform
        assert len(levels[2]) == 1  # Load

    @pytest.mark.asyncio
    async def test_map_reduce_pattern(self, db_pool):
        """Test map-reduce: Split -> Map (parallel) -> Reduce."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)

        dag = DAGBuilder(name="Map Reduce")

        # Split
        split = dag.add("mr.Split", {"chunks": 10})

        # Map phase (parallel workers)
        mappers = []
        for i in range(5):
            mapper = dag.add(f"mr.Map{i}", {"worker_id": i}, depends_on=[split])
            mappers.append(mapper)

        # Reduce phase
        reduce = dag.add("mr.Reduce", depends_on=mappers)

        await dag.execute(client)

        levels = dag.topological_sort()
        assert len(levels) == 3
        assert len(levels[0]) == 1  # Split
        assert len(levels[1]) == 5  # Mappers (parallel)
        assert len(levels[2]) == 1  # Reduce

    @pytest.mark.asyncio
    async def test_multi_stage_pipeline(self, db_pool):
        """Test multi-stage pipeline with complex dependencies."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)

        dag = DAGBuilder(name="Multi-Stage")

        # Stage 1: Data preparation (parallel)
        prep1 = dag.add("stage1.PrepA")
        prep2 = dag.add("stage1.PrepB")

        # Stage 2: Processing (depends on prep)
        proc1 = dag.add("stage2.ProcessA", depends_on=[prep1])
        proc2 = dag.add("stage2.ProcessB", depends_on=[prep2])

        # Stage 3: Cross-validation (depends on both processors)
        validate = dag.add("stage3.Validate", depends_on=[proc1, proc2])

        # Stage 4: Finalization
        finalize = dag.add("stage4.Finalize", depends_on=[validate])

        await dag.execute(client)

        levels = dag.topological_sort()
        assert len(levels) == 4


class TestDAGEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_empty_dag_execution(self, db_pool):
        """Test executing empty DAG."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)
        dag = DAGBuilder(name="Empty DAG")

        # Should not fail
        node_to_job = await dag.execute(client)
        assert len(node_to_job) == 0

    @pytest.mark.asyncio
    async def test_single_job_dag(self, db_pool):
        """Test DAG with single job."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)
        dag = DAGBuilder(name="Single Job")
        job = dag.add("test.Job")

        await dag.execute(client)

        levels = dag.topological_sort()
        assert len(levels) == 1
        assert levels[0] == [job]

    @pytest.mark.asyncio
    async def test_dag_with_kwargs_none(self, db_pool):
        """Test DAG job with kwargs=None."""
        from pyjobby.client import JobClient

        client = JobClient(pool=db_pool)
        dag = DAGBuilder()
        job = dag.add("test.Job", kwargs=None)

        await dag.execute(client)
        assert job.kwargs == {}

    def test_dag_with_very_long_chain(self):
        """Test DAG with long dependency chain."""
        dag = DAGBuilder(name="Long Chain")

        nodes = []
        for i in range(50):
            if i == 0:
                node = dag.add(f"test.Job{i}")
            else:
                node = dag.add(f"test.Job{i}", depends_on=[nodes[-1]])
            nodes.append(node)

        levels = dag.topological_sort()
        assert len(levels) == 50

    def test_dag_with_many_parallel_jobs(self):
        """Test DAG with many parallel jobs."""
        dag = DAGBuilder(name="Wide Parallel")

        # 100 parallel jobs
        jobs = [dag.add(f"test.Job{i}") for i in range(100)]

        levels = dag.topological_sort()
        assert len(levels) == 1
        assert len(levels[0]) == 100
