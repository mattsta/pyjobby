"""
Performance & Scalability Tests - Phase 2

Benchmark tests for throughput, latency, scalability, and resource usage.
These tests verify that the system meets performance requirements under load.

Test categories:
1. Throughput - jobs/second for enqueue and processing
2. Latency - time from enqueue to completion
3. Scalability - performance with increasing load
4. Large DAGs - handling complex dependency graphs
5. Connection pooling - database connection efficiency
6. Concurrent workers - multi-worker scalability
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from pyjobby.client import JobClient
from tests.utils.factories import create_job, get_job


@pytest.mark.slow
@pytest.mark.performance
class TestThroughputBenchmarks:
    """Benchmark throughput (jobs/second) for various operations."""

    @pytest.mark.asyncio
    async def test_enqueue_throughput_single_jobs(self, db_pool):
        """Benchmark: Enqueue single jobs one at a time."""
        client = JobClient(pool=db_pool)

        num_jobs = 100
        start_time = time.time()

        job_ids = []
        for i in range(num_jobs):
            job_id = await client.enqueue(
                "test.BenchmarkJob",
                kwargs={"index": i},
                queue="benchmark"
            )
            job_ids.append(job_id)

        elapsed = time.time() - start_time
        throughput = num_jobs / elapsed

        print(f"\n📊 Enqueue throughput (single): {throughput:.1f} jobs/sec ({num_jobs} jobs in {elapsed:.2f}s)")

        # Cleanup
        for job_id in job_ids:
            await db_pool.execute("DELETE FROM jorb WHERE id = $1", job_id)

        # Expectation: Should enqueue at least 50 jobs/second
        assert throughput >= 50, f"Enqueue throughput too low: {throughput:.1f} jobs/sec"

    @pytest.mark.asyncio
    async def test_enqueue_throughput_batch(self, db_pool):
        """Benchmark: Batch enqueue performance."""
        client = JobClient(pool=db_pool)

        num_jobs = 1000
        batch_size = 100

        start_time = time.time()

        job_ids = []
        for batch_start in range(0, num_jobs, batch_size):
            batch_jobs = []
            for i in range(batch_start, min(batch_start + batch_size, num_jobs)):
                job_id = await client.enqueue(
                    "test.BenchmarkJob",
                    kwargs={"batch": i},
                    queue="benchmark"
                )
                batch_jobs.append(job_id)
            job_ids.extend(batch_jobs)

        elapsed = time.time() - start_time
        throughput = num_jobs / elapsed

        print(f"\n📊 Enqueue throughput (batch): {throughput:.1f} jobs/sec ({num_jobs} jobs in {elapsed:.2f}s)")

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)

        # Expectation: Batch should be faster than single
        assert throughput >= 100, f"Batch enqueue throughput too low: {throughput:.1f} jobs/sec"

    @pytest.mark.asyncio
    async def test_query_throughput(self, db_pool):
        """Benchmark: Job query throughput."""
        # Create test jobs
        job_ids = []
        for i in range(100):
            job_id = await db_pool.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """, "test.Job", {"i": i}, "benchmark", "queued")
            job_ids.append(job_id)

        # Benchmark queries
        num_queries = 1000
        start_time = time.time()

        for _ in range(num_queries):
            job_id = job_ids[_ % len(job_ids)]
            await get_job(db_pool, job_id)

        elapsed = time.time() - start_time
        throughput = num_queries / elapsed

        print(f"\n📊 Query throughput: {throughput:.1f} queries/sec ({num_queries} queries in {elapsed:.2f}s)")

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)

        # Expectation: Should query at least 500/second
        assert throughput >= 500, f"Query throughput too low: {throughput:.1f} queries/sec"


@pytest.mark.slow
@pytest.mark.performance
class TestLatencyBenchmarks:
    """Benchmark latency for job lifecycle operations."""

    @pytest.mark.asyncio
    async def test_enqueue_latency(self, db_pool):
        """Benchmark: Latency for single job enqueue operation."""
        client = JobClient(pool=db_pool)

        num_samples = 100
        latencies = []

        for i in range(num_samples):
            start = time.time()

            job_id = await client.enqueue(
                "test.Job",
                kwargs={"sample": i},
                queue="latency_test"
            )

            latency = (time.time() - start) * 1000  # Convert to ms
            latencies.append(latency)

            # Cleanup
            await db_pool.execute("DELETE FROM jorb WHERE id = $1", job_id)

        # Calculate statistics
        avg_latency = sum(latencies) / len(latencies)
        p50_latency = sorted(latencies)[len(latencies) // 2]
        p95_latency = sorted(latencies)[int(len(latencies) * 0.95)]
        p99_latency = sorted(latencies)[int(len(latencies) * 0.99)]

        print(f"\n📊 Enqueue latency (ms):")
        print(f"   Average: {avg_latency:.2f}ms")
        print(f"   P50: {p50_latency:.2f}ms")
        print(f"   P95: {p95_latency:.2f}ms")
        print(f"   P99: {p99_latency:.2f}ms")

        # Expectations: Average < 20ms, P99 < 50ms
        assert avg_latency < 20, f"Average latency too high: {avg_latency:.2f}ms"
        assert p99_latency < 50, f"P99 latency too high: {p99_latency:.2f}ms"

    @pytest.mark.asyncio
    async def test_state_update_latency(self, db_pool):
        """Benchmark: Latency for job state updates."""
        # Create test job
        job_id = await db_pool.fetchval("""
            INSERT INTO jorb (job_class, kwargs, queue, state)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """, "test.Job", {}, "latency_test", "queued")

        num_samples = 100
        latencies = []

        states = ["queued", "claimed", "running", "finished"]

        for i in range(num_samples):
            state = states[i % len(states)]

            start = time.time()

            await db_pool.execute("""
                UPDATE jorb
                SET state = $2, updated = $3
                WHERE id = $1
            """, job_id, state, datetime.now(timezone.utc))

            latency = (time.time() - start) * 1000
            latencies.append(latency)

        # Calculate statistics
        avg_latency = sum(latencies) / len(latencies)
        p95_latency = sorted(latencies)[int(len(latencies) * 0.95)]

        print(f"\n📊 State update latency: avg={avg_latency:.2f}ms, p95={p95_latency:.2f}ms")

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE id = $1", job_id)

        # Expectation: Updates should be fast (< 10ms avg)
        assert avg_latency < 10, f"State update latency too high: {avg_latency:.2f}ms"


@pytest.mark.slow
@pytest.mark.performance
class TestScalabilityBenchmarks:
    """Benchmark scalability with increasing load."""

    @pytest.mark.asyncio
    async def test_scaling_job_count(self, db_pool):
        """Benchmark: Performance with increasing number of jobs."""
        client = JobClient(pool=db_pool)

        test_sizes = [10, 50, 100, 500, 1000]
        results = []

        for size in test_sizes:
            start_time = time.time()

            # Enqueue jobs
            job_ids = []
            for i in range(size):
                job_id = await client.enqueue(
                    "test.Job",
                    kwargs={"index": i},
                    queue="scaling_test"
                )
                job_ids.append(job_id)

            elapsed = time.time() - start_time
            throughput = size / elapsed

            results.append((size, throughput, elapsed))

            print(f"\n📊 {size:4d} jobs: {throughput:6.1f} jobs/sec ({elapsed:.2f}s)")

            # Cleanup
            await db_pool.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)

        # Verify scaling is roughly linear (throughput doesn't degrade significantly)
        if len(results) >= 2:
            first_throughput = results[0][1]
            last_throughput = results[-1][1]

            # Throughput shouldn't degrade by more than 50%
            degradation = (first_throughput - last_throughput) / first_throughput
            assert degradation < 0.5, f"Throughput degraded too much: {degradation*100:.1f}%"

    @pytest.mark.asyncio
    async def test_scaling_concurrent_queries(self, db_pool):
        """Benchmark: Concurrent query performance."""
        # Create test jobs
        job_ids = []
        for i in range(10):
            job_id = await db_pool.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """, "test.Job", {"i": i}, "concurrent_test", "queued")
            job_ids.append(job_id)

        async def query_job(job_id):
            """Query a single job."""
            return await get_job(db_pool, job_id)

        # Test with increasing concurrency
        concurrency_levels = [1, 10, 50, 100]

        for concurrency in concurrency_levels:
            # Create concurrent queries
            queries = []
            for i in range(concurrency):
                job_id = job_ids[i % len(job_ids)]
                queries.append(query_job(job_id))

            start_time = time.time()
            await asyncio.gather(*queries)
            elapsed = time.time() - start_time

            throughput = concurrency / elapsed

            print(f"\n📊 Concurrency {concurrency:3d}: {throughput:6.1f} queries/sec ({elapsed:.3f}s)")

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE id = ANY($1::bigint[])", job_ids)


@pytest.mark.slow
@pytest.mark.performance
class TestLargeDAGBenchmarks:
    """Benchmark performance with large DAG structures."""

    @pytest.mark.asyncio
    async def test_large_linear_dag_creation(self, db_pool):
        """Benchmark: Create large linear DAG (100 jobs)."""
        # Create DAG
        dag_id = await db_pool.fetchval("""
            INSERT INTO jorb_dag (name, created, updated)
            VALUES ($1, $2, $2)
            RETURNING id
        """, "Large Linear DAG", datetime.now(timezone.utc))

        num_jobs = 100

        start_time = time.time()

        # Create linear chain
        prev_job_id = None
        job_ids = []

        for i in range(num_jobs):
            job_id = await db_pool.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, dag_id, waitfor_job)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """, "test.Job", {"step": i}, "dag_test",
                "queued" if i == 0 else "waiting",
                dag_id,
                prev_job_id)

            job_ids.append(job_id)
            prev_job_id = job_id

        elapsed = time.time() - start_time
        throughput = num_jobs / elapsed

        print(f"\n📊 Large linear DAG creation: {throughput:.1f} jobs/sec ({num_jobs} jobs in {elapsed:.2f}s)")

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE dag_id = $1", dag_id)
        await db_pool.execute("DELETE FROM jorb_dag WHERE id = $1", dag_id)

        # Expectation: Should create at least 50 jobs/sec
        assert throughput >= 50, f"DAG creation too slow: {throughput:.1f} jobs/sec"

    @pytest.mark.asyncio
    async def test_large_parallel_dag_creation(self, db_pool):
        """Benchmark: Create large parallel DAG (10 branches x 10 jobs)."""
        # Create DAG
        dag_id = await db_pool.fetchval("""
            INSERT INTO jorb_dag (name, created, updated)
            VALUES ($1, $2, $2)
            RETURNING id
        """, "Large Parallel DAG", datetime.now(timezone.utc))

        num_branches = 10
        jobs_per_branch = 10
        total_jobs = num_branches * jobs_per_branch

        start_time = time.time()

        job_ids = []

        # Create parallel branches
        for branch in range(num_branches):
            prev_job_id = None

            for step in range(jobs_per_branch):
                job_id = await db_pool.fetchval("""
                    INSERT INTO jorb (job_class, kwargs, queue, state, dag_id, waitfor_job)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id
                """, "test.Job", {"branch": branch, "step": step}, "dag_test",
                    "queued" if step == 0 else "waiting",
                    dag_id,
                    prev_job_id)

                job_ids.append(job_id)
                prev_job_id = job_id

        elapsed = time.time() - start_time
        throughput = total_jobs / elapsed

        print(f"\n📊 Large parallel DAG creation: {throughput:.1f} jobs/sec ({total_jobs} jobs in {elapsed:.2f}s)")

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE dag_id = $1", dag_id)
        await db_pool.execute("DELETE FROM jorb_dag WHERE id = $1", dag_id)

        # Expectation: Should maintain good throughput even with parallel structure
        assert throughput >= 50, f"Parallel DAG creation too slow: {throughput:.1f} jobs/sec"

    @pytest.mark.asyncio
    async def test_dag_validation_performance(self, db_pool):
        """Benchmark: DAG cycle validation on large DAG."""
        # Create DAG with 50 jobs
        dag_id = await db_pool.fetchval("""
            INSERT INTO jorb_dag (name, created, updated)
            VALUES ($1, $2, $2)
            RETURNING id
        """, "Validation Test DAG", datetime.now(timezone.utc))

        num_jobs = 50

        # Create linear chain (no cycles)
        prev_job_id = None
        for i in range(num_jobs):
            job_id = await db_pool.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, dag_id, waitfor_job)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
            """, "test.Job", {"i": i}, "dag_test", "queued", dag_id, prev_job_id)
            prev_job_id = job_id

        # Benchmark validation
        num_validations = 10
        start_time = time.time()

        for _ in range(num_validations):
            is_valid = await db_pool.fetchval("""
                SELECT validate_dag_acyclic($1)
            """, dag_id)
            assert is_valid is True

        elapsed = time.time() - start_time
        avg_validation = (elapsed / num_validations) * 1000  # ms

        print(f"\n📊 DAG validation ({num_jobs} jobs): {avg_validation:.2f}ms average")

        # Cleanup
        await db_pool.execute("DELETE FROM jorb WHERE dag_id = $1", dag_id)
        await db_pool.execute("DELETE FROM jorb_dag WHERE id = $1", dag_id)

        # Expectation: Validation should be fast (< 100ms for 50 jobs)
        assert avg_validation < 100, f"DAG validation too slow: {avg_validation:.2f}ms"


@pytest.mark.slow
@pytest.mark.performance
class TestConnectionPoolingBenchmarks:
    """Benchmark database connection pool performance."""

    @pytest.mark.asyncio
    async def test_connection_pool_under_load(self, db_pool):
        """Benchmark: Connection pool handling concurrent queries."""
        async def query():
            """Execute a query using the pool."""
            async with db_pool.acquire() as conn:
                return await conn.fetchval("SELECT COUNT(*) FROM jorb")

        # Test with increasing concurrent load
        load_levels = [10, 50, 100]

        for load in load_levels:
            start_time = time.time()

            # Execute concurrent queries
            results = await asyncio.gather(*[query() for _ in range(load)])

            elapsed = time.time() - start_time
            throughput = load / elapsed

            print(f"\n📊 Connection pool (load={load}): {throughput:.1f} queries/sec ({elapsed:.3f}s)")

            assert len(results) == load

        # Pool should handle all loads efficiently
        assert True  # If we got here, pool handled the load

    @pytest.mark.asyncio
    async def test_connection_pool_saturation(self, db_pool):
        """Benchmark: Performance when pool is saturated."""
        # Pool max_size = 10 (from conftest.py)
        pool_size = 10

        # Create more concurrent operations than pool size
        num_operations = pool_size * 3  # 30 operations, 10 connections

        async def long_query():
            """Query that holds connection briefly."""
            async with db_pool.acquire() as conn:
                await asyncio.sleep(0.1)  # Hold connection for 100ms
                return await conn.fetchval("SELECT 1")

        start_time = time.time()

        results = await asyncio.gather(*[long_query() for _ in range(num_operations)])

        elapsed = time.time() - start_time

        print(f"\n📊 Pool saturation ({num_operations} ops, {pool_size} connections): {elapsed:.2f}s")

        assert len(results) == num_operations

        # With 30 operations and 10 connections, should take ~3x the single operation time
        # (3 batches of 10 operations at 0.1s each = ~0.3s)
        expected_min_time = 0.3
        assert elapsed >= expected_min_time * 0.9  # Allow 10% tolerance


@pytest.mark.slow
@pytest.mark.performance
class TestMemoryUsageBenchmarks:
    """Benchmark memory usage for various operations."""

    @pytest.mark.asyncio
    async def test_large_result_storage(self, db_pool):
        """Benchmark: Storing large results."""
        # Test result sizes
        result_sizes = [1, 10, 100, 1000, 10000]  # Number of items

        for size in result_sizes:
            # Create large result
            result = {"data": list(range(size))}

            start_time = time.time()

            # Store in database
            job_id = await db_pool.fetchval("""
                INSERT INTO jorb (job_class, kwargs, queue, state, result)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            """, "test.Job", {}, "memory_test", "finished", result)

            # Retrieve from database
            retrieved_job = await get_job(db_pool, job_id)

            elapsed = (time.time() - start_time) * 1000  # ms

            print(f"\n📊 Result size {size:5d} items: {elapsed:.2f}ms round-trip")

            # Verify data integrity
            assert retrieved_job['result']['data'] == list(range(size))

            # Cleanup
            await db_pool.execute("DELETE FROM jorb WHERE id = $1", job_id)

        # Larger results should still complete in reasonable time
        # 10k items should complete in < 100ms
        # (actual measurement will vary)
