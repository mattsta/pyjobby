"""
Comprehensive tests for retry_strategies.py - all retry backoff strategies.
Using LIVE calculations with NO MOCKS for maximum correctness guarantees!
"""

import datetime

from pyjobby.retry_strategies import (
    RetryStrategy,
    calculate_retry_delay,
    calculate_retry_from_job,
    get_retry_config,
)


class TestRetryStrategies:
    """Test all retry backoff strategies - covers lines 52-80."""

    def test_fixed_strategy(self):
        """Test fixed (legacy) strategy with quadratic backoff - covers line 54."""
        delay = calculate_retry_delay(1, strategy="fixed")
        assert isinstance(delay, datetime.timedelta)
        # Fixed: 2 * (1^2) + jitter(1-5) = 2 + jitter, so 3-7 seconds
        assert 2 <= delay.total_seconds() <= 10

        delay2 = calculate_retry_delay(3, strategy="fixed")
        # Fixed: 2 * (3^2) + jitter = 18 + jitter
        assert 18 <= delay2.total_seconds() <= 30

    def test_exponential_strategy(self):
        """Test exponential backoff strategy - covers line 58."""
        delay1 = calculate_retry_delay(1, strategy="exponential", initial_delay=1)
        # 1 * (2^0) = 1 + jitter
        assert 1 <= delay1.total_seconds() <= 5

        delay3 = calculate_retry_delay(3, strategy="exponential", initial_delay=1)
        # 1 * (2^2) = 4 + jitter
        assert 4 <= delay3.total_seconds() <= 10

        delay5 = calculate_retry_delay(5, strategy="exponential", initial_delay=1)
        # 1 * (2^4) = 16 + jitter
        assert 16 <= delay5.total_seconds() <= 25

    def test_linear_strategy(self):
        """Test linear backoff strategy - covers lines 60-62."""
        delay1 = calculate_retry_delay(1, strategy="linear", initial_delay=10)
        # 10 * 1 = 10 + jitter
        assert 10 <= delay1.total_seconds() <= 15

        delay5 = calculate_retry_delay(5, strategy="linear", initial_delay=10)
        # 10 * 5 = 50 + jitter
        assert 50 <= delay5.total_seconds() <= 60

    def test_fibonacci_strategy(self):
        """Test fibonacci backoff strategy - covers lines 64-76."""
        delay1 = calculate_retry_delay(1, strategy="fibonacci", initial_delay=1)
        # fib(1) = 1, so 1 * 1 = 1 + jitter
        assert 1 <= delay1.total_seconds() <= 5

        delay2 = calculate_retry_delay(2, strategy="fibonacci", initial_delay=1)
        # fib(2) = 1, so 1 * 1 = 1 + jitter
        assert 1 <= delay2.total_seconds() <= 5

        delay5 = calculate_retry_delay(5, strategy="fibonacci", initial_delay=1)
        # fib(5) = 5, so 1 * 5 = 5 + jitter
        assert 5 <= delay5.total_seconds() <= 10

        delay7 = calculate_retry_delay(7, strategy="fibonacci", initial_delay=1)
        # fib(7) = 13, so 1 * 13 = 13 + jitter
        assert 13 <= delay7.total_seconds() <= 20

    def test_fibonacci_edge_case_zero_error_count(self):
        """Test fibonacci with edge case error_count - covers line 68."""
        # When error_count is 0 or negative, fib should return 0
        # This tests the n <= 0 branch in the fib function
        delay = calculate_retry_delay(0, strategy="fibonacci", initial_delay=1)
        # fib(0) = 0, so 1 * 0 = 0 + jitter
        assert 0 <= delay.total_seconds() <= 5

    def test_unknown_strategy_defaults_to_exponential(self):
        """Test unknown strategy defaults to exponential - covers lines 78-80."""
        delay = calculate_retry_delay(1, strategy="unknown_strategy", initial_delay=1)
        assert isinstance(delay, datetime.timedelta)
        # Should use exponential: 1 * (2^0) = 1 + jitter
        assert 1 <= delay.total_seconds() <= 5

    def test_max_delay_cap(self):
        """Test delay is capped at max_delay - covers line 87."""
        delay = calculate_retry_delay(
            100, strategy="exponential", initial_delay=1, max_delay=60
        )
        # Should be capped at 60 seconds
        assert delay.total_seconds() <= 65  # max_delay + jitter

    def test_custom_multiplier(self):
        """Test custom multiplier for exponential."""
        delay = calculate_retry_delay(
            3, strategy="exponential", initial_delay=1, multiplier=3.0
        )
        # 1 * (3^2) = 9 + jitter
        assert 9 <= delay.total_seconds() <= 15


class TestRetryConfig:
    """Test retry configuration extraction - covers lines 92-115."""

    def test_get_retry_config_with_none(self):
        """Test config extraction with None admin_data - covers line 107-108."""
        config = get_retry_config(None)
        assert config["retry_strategy"] == "exponential"
        assert config["max_retries"] == 10
        assert config["initial_retry_delay"] == 1
        assert config["max_retry_delay"] == 3600

    def test_get_retry_config_with_empty_dict(self):
        """Test config extraction with empty dict."""
        config = get_retry_config({})
        assert config["retry_strategy"] == "exponential"
        assert config["max_retries"] == 10

    def test_get_retry_config_with_custom_values(self):
        """Test config extraction with custom values - covers lines 110-115."""
        admin_data = {
            "retry_strategy": "linear",
            "max_retries": 5,
            "initial_retry_delay": 10,
            "max_retry_delay": 300,
        }
        config = get_retry_config(admin_data)
        assert config["retry_strategy"] == "linear"
        assert config["max_retries"] == 5
        assert config["initial_retry_delay"] == 10
        assert config["max_retry_delay"] == 300


class TestCalculateRetryFromJob:
    """Test job-based retry calculation - covers lines 118-136."""

    def test_calculate_retry_from_job_with_config(self):
        """Test retry calculation using job config."""
        job = {
            "id": 1,
            "admin_data": {
                "retry_strategy": "linear",
                "initial_retry_delay": 5,
                "max_retry_delay": 100,
            },
        }
        delay = calculate_retry_from_job(job, error_count=3)
        # Linear: 5 * 3 = 15 + jitter
        assert 15 <= delay.total_seconds() <= 25

    def test_calculate_retry_from_job_with_defaults(self):
        """Test retry calculation with default config."""
        job = {"id": 1, "admin_data": None}
        delay = calculate_retry_from_job(job, error_count=2)
        # Exponential default: 1 * (2^1) = 2 + jitter
        assert 2 <= delay.total_seconds() <= 10


class TestRetryStrategyEnum:
    """Test RetryStrategy enum."""

    def test_enum_values(self):
        """Test enum string values."""
        assert RetryStrategy.FIXED == "fixed"
        assert RetryStrategy.EXPONENTIAL == "exponential"
        assert RetryStrategy.LINEAR == "linear"
        assert RetryStrategy.FIBONACCI == "fibonacci"
