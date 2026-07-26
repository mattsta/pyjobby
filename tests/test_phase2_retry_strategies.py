"""
Phase 2: Retry Strategies Tests

Comprehensive tests for configurable retry strategies:
- Exponential backoff
- Linear backoff
- Fibonacci backoff
- Fixed (legacy) backoff
"""

from datetime import datetime, timedelta

import pytest

from pyjobby.retry_strategies import (
    RetryStrategy,
    calculate_retry_delay,
    calculate_retry_from_job,
    get_retry_config,
)
from tests.utils.factories import create_job, get_job


class TestRetryDelayCalculation:
    """Test retry delay calculation algorithms."""

    def test_exponential_backoff(self):
        """Test exponential backoff: 1s, 2s, 4s, 8s, 16s..."""
        delays = [
            calculate_retry_delay(attempt, strategy="exponential", initial_delay=1)
            for attempt in range(1, 11)
        ]

        # Should be roughly: 1, 2, 4, 8, 16, 32, 64, 128, 256, 512
        # (with jitter up to 10% or 5 seconds, whichever is less)
        assert 0.9 <= delays[0].total_seconds() <= 1.5  # ~1s + 10% = 1.1
        assert 1.9 <= delays[1].total_seconds() <= 2.6  # ~2s + 10% = 2.2
        assert 3.9 <= delays[2].total_seconds() <= 4.9  # ~4s + 10% = 4.4
        assert 7.9 <= delays[3].total_seconds() <= 9.3  # ~8s + 10% = 8.8
        assert 15.9 <= delays[4].total_seconds() <= 18.1  # ~16s + 10% = 17.6

    def test_linear_backoff(self):
        """Test linear backoff: 1s, 2s, 3s, 4s, 5s..."""
        delays = [
            calculate_retry_delay(attempt, strategy="linear", initial_delay=1)
            for attempt in range(1, 6)
        ]

        # Should be roughly: 1, 2, 3, 4, 5
        assert 0.9 <= delays[0].total_seconds() <= 1.5
        assert 1.9 <= delays[1].total_seconds() <= 2.5
        assert 2.9 <= delays[2].total_seconds() <= 3.5
        assert 3.9 <= delays[3].total_seconds() <= 4.5
        assert 4.9 <= delays[4].total_seconds() <= 5.5

    def test_fibonacci_backoff(self):
        """Test fibonacci backoff: 1, 1, 2, 3, 5, 8, 13..."""
        delays = [
            calculate_retry_delay(attempt, strategy="fibonacci", initial_delay=1)
            for attempt in range(1, 8)
        ]

        # Fibonacci: 1, 1, 2, 3, 5, 8, 13 (with jitter up to 10% or 5 seconds)
        assert 0.9 <= delays[0].total_seconds() <= 1.5  # ~1
        assert 0.9 <= delays[1].total_seconds() <= 1.5  # ~1
        assert 1.9 <= delays[2].total_seconds() <= 2.5  # ~2
        assert 2.9 <= delays[3].total_seconds() <= 3.5  # ~3
        assert 4.9 <= delays[4].total_seconds() <= 5.6  # ~5
        assert 7.9 <= delays[5].total_seconds() <= 8.9  # ~8
        assert 12.9 <= delays[6].total_seconds() <= 14.3  # ~13 (13 + 10% jitter = 14.3)

    def test_fixed_backoff_legacy(self):
        """Test fixed (legacy) backoff: quadratic."""
        delays = [
            calculate_retry_delay(attempt, strategy="fixed") for attempt in range(1, 5)
        ]

        # Fixed: 2*(n^2) + jitter
        # 1: 2*1 + jitter = ~2-7
        # 2: 2*4 + jitter = ~8-13
        # 3: 2*9 + jitter = ~18-23
        # 4: 2*16 + jitter = ~32-37
        assert 1 <= delays[0].total_seconds() <= 8
        assert 7 <= delays[1].total_seconds() <= 14
        assert 17 <= delays[2].total_seconds() <= 24

    def test_max_delay_cap(self):
        """Test that delays are capped at max_delay."""
        # With exponential and high attempts, delay should cap at max
        delay = calculate_retry_delay(
            20,  # Would be huge without cap
            strategy="exponential",
            initial_delay=1,
            max_delay=60,  # Cap at 60 seconds
        )

        assert delay.total_seconds() <= 60

    def test_initial_delay_parameter(self):
        """Test that initial_delay parameter works."""
        # Start with 10 seconds
        delay = calculate_retry_delay(1, strategy="exponential", initial_delay=10)

        assert 9.5 <= delay.total_seconds() <= 10.5

    def test_jitter_prevents_exact_values(self):
        """Test that jitter makes delays non-deterministic."""
        delays = [
            calculate_retry_delay(5, strategy="exponential", initial_delay=1)
            for _ in range(10)
        ]

        # All should be around 16s but slightly different
        unique_delays = {d.total_seconds() for d in delays}
        assert len(unique_delays) > 1  # Not all identical

    def test_returns_timedelta(self):
        """Test that function returns timedelta."""
        delay = calculate_retry_delay(1, strategy="exponential")
        assert isinstance(delay, timedelta)


class TestRetryConfigExtraction:
    """Test retry configuration extraction from admin_data."""

    def test_get_retry_config_with_full_config(self):
        """Test extracting full retry configuration."""
        admin_data = {
            "retry_strategy": "linear",
            "max_retries": 15,
            "initial_retry_delay": 5,
            "max_retry_delay": 600,
        }

        config = get_retry_config(admin_data)

        assert config["retry_strategy"] == "linear"
        assert config["max_retries"] == 15
        assert config["initial_retry_delay"] == 5
        assert config["max_retry_delay"] == 600

    def test_get_retry_config_with_defaults(self):
        """Test that defaults are used when config is missing."""
        config = get_retry_config(None)

        assert config["retry_strategy"] == "exponential"
        assert config["max_retries"] == 10
        assert config["initial_retry_delay"] == 1
        assert config["max_retry_delay"] == 3600

    def test_get_retry_config_partial(self):
        """Test partial config with some defaults."""
        admin_data = {
            "retry_strategy": "fibonacci",
            "max_retries": 20,
            # Missing initial_retry_delay and max_retry_delay
        }

        config = get_retry_config(admin_data)

        assert config["retry_strategy"] == "fibonacci"
        assert config["max_retries"] == 20
        assert config["initial_retry_delay"] == 1  # Default
        assert config["max_retry_delay"] == 3600  # Default

    def test_calculate_retry_from_job(self):
        """Test calculating retry delay from job record."""
        job = {
            "id": 123,
            "admin_data": {
                "retry_strategy": "exponential",
                "initial_retry_delay": 2,
                "max_retry_delay": 300,
            },
        }

        delay = calculate_retry_from_job(job, error_count=3)

        # Exponential with initial=2: 2, 4, 8...
        # Attempt 3 should be ~8 seconds
        assert 7.5 <= delay.total_seconds() <= 8.5


class TestAdminDataRetryConfig:
    """Test storing retry configuration in admin_data."""

    @pytest.mark.asyncio
    async def test_store_retry_config_in_admin_data(self, db_connection):
        """Test storing retry configuration in job's admin_data."""
        admin_data = {
            "retry_strategy": "exponential",
            "max_retries": 15,
            "initial_retry_delay": 1,
            "max_retry_delay": 300,
        }

        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """,
            "test.Job",
            "{}",
            "default",
            admin_data,
        )

        job = await get_job(db_connection, job_id)
        assert job["admin_data"]["retry_strategy"] == "exponential"
        assert job["admin_data"]["max_retries"] == 15
        assert job["admin_data"]["initial_retry_delay"] == 1
        assert job["admin_data"]["max_retry_delay"] == 300

    @pytest.mark.asyncio
    async def test_default_retry_config(self, db_connection):
        """Test job with no retry config uses defaults."""
        job_id = await create_job(db_connection, job_class="test.Job")
        job = await get_job(db_connection, job_id)

        # Extract config (should get defaults)
        config = get_retry_config(job.get("admin_data"))
        assert config["retry_strategy"] == "exponential"
        assert config["max_retries"] == 10


class TestRetryStrategiesIntegration:
    """Integration tests for retry strategies with actual retries."""

    @pytest.mark.asyncio
    async def test_job_retry_with_exponential_strategy(self, db_connection):
        """Test job retry with exponential backoff."""
        admin_data = {
            "retry_strategy": "exponential",
            "max_retries": 5,
            "initial_retry_delay": 1,
            "max_retry_delay": 60,
        }

        # Create job that will fail and retry
        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """,
            "test.FailingJob",
            "{}",
            "default",
            admin_data,
            "queued",
        )

        # Simulate first failure
        await db_connection.execute(
            """
            UPDATE jorb
            SET state = 'crashed',
                error_count = 1,
                error_message = 'Test failure'
            WHERE id = $1
        """,
            job_id,
        )

        # Create retry job (this is what the worker would do)
        job = await get_job(db_connection, job_id)
        retry_delay = calculate_retry_from_job(job, error_count=1)

        retry_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (
                job_class, kwargs, queue, admin_data, state, error_count,
                run_after
            )
            SELECT
                job_class, kwargs, queue, admin_data, 'queued', $2,
                TIMEZONE('utc', clock_timestamp()) + $3::interval
            FROM jorb
            WHERE id = $1
            RETURNING id
        """,
            job_id,
            1,
            retry_delay,
        )

        retry_job = await get_job(db_connection, retry_id)
        assert retry_job["error_count"] == 1
        assert retry_job["state"] == "queued"
        # run_after should be ~1 second from now
        assert retry_job["run_after"] > datetime.utcnow()

    @pytest.mark.asyncio
    async def test_max_retries_enforcement(self, db_connection):
        """Test that max_retries limit is enforced."""
        admin_data = {"retry_strategy": "exponential", "max_retries": 3}

        job_id = await db_connection.fetchval(
            """
            INSERT INTO jorb (job_class, kwargs, queue, admin_data, state, error_count)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """,
            "test.Job",
            "{}",
            "default",
            admin_data,
            "crashed",
            3,
        )

        job = await get_job(db_connection, job_id)
        config = get_retry_config(job["admin_data"])

        # Should not retry - at max
        assert job["error_count"] >= config["max_retries"]

    @pytest.mark.asyncio
    async def test_different_strategies_produce_different_delays(self, db_connection):
        """Test that different strategies produce different retry delays."""
        strategies = ["exponential", "linear", "fibonacci"]
        delays = {}

        for strategy in strategies:
            admin_data = {"retry_strategy": strategy, "initial_retry_delay": 1}

            job_id = await db_connection.fetchval(
                """
                INSERT INTO jorb (job_class, kwargs, queue, admin_data)
                VALUES ($1, $2, $3, $4)
                RETURNING id
            """,
                "test.Job",
                "{}",
                "default",
                admin_data,
            )

            job = await get_job(db_connection, job_id)
            delay = calculate_retry_from_job(job, error_count=6)
            delays[strategy] = delay.total_seconds()

        # All should be different for attempt 6 (linear=6, fib=8, exp=32)
        assert delays["exponential"] != delays["linear"]
        assert delays["exponential"] != delays["fibonacci"]
        assert delays["linear"] != delays["fibonacci"]

        # Exponential should be largest for high attempts
        assert delays["exponential"] > delays["linear"]


class TestRetryStrategyEnum:
    """Test RetryStrategy enum."""

    def test_enum_values(self):
        """Test that all strategies are in enum."""
        assert RetryStrategy.FIXED == "fixed"
        assert RetryStrategy.EXPONENTIAL == "exponential"
        assert RetryStrategy.LINEAR == "linear"
        assert RetryStrategy.FIBONACCI == "fibonacci"

    def test_enum_can_be_used_in_calculation(self):
        """Test that enum values work with calculate_retry_delay."""
        delay = calculate_retry_delay(
            1, strategy=RetryStrategy.EXPONENTIAL, initial_delay=1
        )
        assert isinstance(delay, timedelta)


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_zero_initial_delay(self):
        """Test with zero initial delay."""
        delay = calculate_retry_delay(1, strategy="exponential", initial_delay=0)
        # Should still have jitter
        assert delay.total_seconds() >= 0

    def test_very_large_attempt_number(self):
        """Test with very large attempt number."""
        delay = calculate_retry_delay(
            100, strategy="exponential", initial_delay=1, max_delay=3600
        )
        # Should be capped at max_delay
        assert delay.total_seconds() <= 3600

    def test_unknown_strategy_defaults_to_exponential(self):
        """Test that unknown strategy falls back to exponential."""
        delay = calculate_retry_delay(2, strategy="unknown_strategy", initial_delay=1)
        # Should behave like exponential (attempt 2 = ~2s)
        assert 1.5 <= delay.total_seconds() <= 2.5
