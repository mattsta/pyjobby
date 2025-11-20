#!/usr/bin/env python3
"""
Comprehensive tests for retry_strategies module.

Tests all retry backoff strategies, edge cases, and helper functions.
Coverage target: 95%+
"""

import pytest
import datetime
from pyjobby.retry_strategies import (
    RetryStrategy,
    calculate_retry_delay,
    get_retry_config,
    calculate_retry_from_job,
)


class TestRetryStrategyEnum:
    """Test RetryStrategy enum"""

    def test_enum_values(self):
        """Verify all strategy enum values"""
        assert RetryStrategy.FIXED == "fixed"
        assert RetryStrategy.EXPONENTIAL == "exponential"
        assert RetryStrategy.LINEAR == "linear"
        assert RetryStrategy.FIBONACCI == "fibonacci"


class TestCalculateRetryDelay:
    """Test calculate_retry_delay function with all strategies"""

    # ===== Exponential Strategy =====

    def test_exponential_first_retry(self):
        """Test exponential backoff for first retry"""
        delay = calculate_retry_delay(1, "exponential", initial_delay=1, max_delay=3600)
        # 1 * (2^0) = 1 second + jitter (0-0.1)
        assert 1 <= delay.total_seconds() <= 1.1

    def test_exponential_second_retry(self):
        """Test exponential backoff for second retry"""
        delay = calculate_retry_delay(2, "exponential", initial_delay=1, max_delay=3600)
        # 1 * (2^1) = 2 seconds + jitter (0-0.2)
        assert 2 <= delay.total_seconds() <= 2.2

    def test_exponential_fifth_retry(self):
        """Test exponential backoff for fifth retry"""
        delay = calculate_retry_delay(5, "exponential", initial_delay=1, max_delay=3600)
        # 1 * (2^4) = 16 seconds + jitter (0-1.6)
        assert 16 <= delay.total_seconds() <= 17.6

    def test_exponential_custom_multiplier(self):
        """Test exponential backoff with custom multiplier"""
        delay = calculate_retry_delay(3, "exponential", initial_delay=1, max_delay=3600, multiplier=3.0)
        # 1 * (3^2) = 9 seconds + jitter
        assert 9 <= delay.total_seconds() <= 9.9

    def test_exponential_custom_initial_delay(self):
        """Test exponential backoff with custom initial delay"""
        delay = calculate_retry_delay(2, "exponential", initial_delay=5, max_delay=3600)
        # 5 * (2^1) = 10 seconds + jitter
        assert 10 <= delay.total_seconds() <= 11

    def test_exponential_max_delay_cap(self):
        """Test exponential backoff respects max_delay"""
        delay = calculate_retry_delay(20, "exponential", initial_delay=1, max_delay=60)
        # Would be 2^19 = 524288 seconds, capped at 60
        assert delay.total_seconds() <= 65  # 60 + max jitter (5)

    # ===== Linear Strategy =====

    def test_linear_first_retry(self):
        """Test linear backoff for first retry"""
        delay = calculate_retry_delay(1, "linear", initial_delay=1, max_delay=3600)
        # 1 * 1 = 1 second + jitter
        assert 1 <= delay.total_seconds() <= 1.1

    def test_linear_fifth_retry(self):
        """Test linear backoff for fifth retry"""
        delay = calculate_retry_delay(5, "linear", initial_delay=1, max_delay=3600)
        # 1 * 5 = 5 seconds + jitter
        assert 5 <= delay.total_seconds() <= 5.5

    def test_linear_custom_initial_delay(self):
        """Test linear backoff with custom initial delay"""
        delay = calculate_retry_delay(3, "linear", initial_delay=10, max_delay=3600)
        # 10 * 3 = 30 seconds + jitter
        assert 30 <= delay.total_seconds() <= 33

    def test_linear_max_delay_cap(self):
        """Test linear backoff respects max_delay"""
        delay = calculate_retry_delay(100, "linear", initial_delay=10, max_delay=200)
        # Would be 10 * 100 = 1000, capped at 200
        assert delay.total_seconds() <= 205  # 200 + max jitter

    # ===== Fibonacci Strategy =====

    def test_fibonacci_first_retry(self):
        """Test fibonacci backoff for first retry"""
        delay = calculate_retry_delay(1, "fibonacci", initial_delay=1, max_delay=3600)
        # fib(1) = 1, 1 * 1 = 1 second + jitter
        assert 1 <= delay.total_seconds() <= 1.1

    def test_fibonacci_second_retry(self):
        """Test fibonacci backoff for second retry"""
        delay = calculate_retry_delay(2, "fibonacci", initial_delay=1, max_delay=3600)
        # fib(2) = 1, 1 * 1 = 1 second + jitter
        assert 1 <= delay.total_seconds() <= 1.1

    def test_fibonacci_fifth_retry(self):
        """Test fibonacci backoff for fifth retry"""
        delay = calculate_retry_delay(5, "fibonacci", initial_delay=1, max_delay=3600)
        # fib(5) = 5, 1 * 5 = 5 seconds + jitter
        assert 5 <= delay.total_seconds() <= 5.5

    def test_fibonacci_tenth_retry(self):
        """Test fibonacci backoff for tenth retry"""
        delay = calculate_retry_delay(10, "fibonacci", initial_delay=1, max_delay=3600)
        # fib(10) = 55, 1 * 55 = 55 seconds + jitter
        assert 55 <= delay.total_seconds() <= 60

    def test_fibonacci_max_delay_cap(self):
        """Test fibonacci backoff respects max_delay"""
        delay = calculate_retry_delay(20, "fibonacci", initial_delay=1, max_delay=100)
        # fib(20) = 6765, would be large, capped at 100
        assert delay.total_seconds() <= 105  # 100 + max jitter

    # ===== Fixed (Legacy) Strategy =====

    def test_fixed_first_retry(self):
        """Test fixed/legacy backoff for first retry"""
        delay = calculate_retry_delay(1, "fixed")
        # 2 * (1^2) + random(1,5) + jitter
        # = 2 + 1-5 + jitter = 3-7 + jitter
        assert 3 <= delay.total_seconds() <= 12

    def test_fixed_fifth_retry(self):
        """Test fixed/legacy backoff for fifth retry"""
        delay = calculate_retry_delay(5, "fixed")
        # 2 * (5^2) + random(1,5) + jitter
        # = 50 + 1-5 + jitter = 51-55 + jitter
        assert 50 <= delay.total_seconds() <= 65

    # ===== Unknown/Default Strategy =====

    def test_unknown_strategy_defaults_to_exponential(self):
        """Test unknown strategy defaults to exponential"""
        delay1 = calculate_retry_delay(3, "unknown_strategy", initial_delay=1, max_delay=3600)
        delay2 = calculate_retry_delay(3, "exponential", initial_delay=1, max_delay=3600)
        # Both should be approximately 4 seconds (1 * 2^2)
        assert 4 <= delay1.total_seconds() <= 5
        assert 4 <= delay2.total_seconds() <= 5

    # ===== Jitter Behavior =====

    def test_jitter_capped_at_5_seconds(self):
        """Test jitter is capped at 5 seconds for large delays"""
        delay = calculate_retry_delay(15, "exponential", initial_delay=1, max_delay=10000)
        # Base delay would be 2^14 = 16384, jitter capped at 5
        # So delay should be between base and base+5
        assert delay.total_seconds() <= 16389  # 16384 + 5

    # ===== Edge Cases =====

    def test_zero_error_count(self):
        """Test with zero error count"""
        delay = calculate_retry_delay(0, "exponential", initial_delay=1, max_delay=3600)
        # 1 * (2^-1) = 0.5 seconds + jitter
        assert 0 <= delay.total_seconds() <= 5

    def test_negative_error_count(self):
        """Test with negative error count"""
        delay = calculate_retry_delay(-1, "exponential", initial_delay=1, max_delay=3600)
        # 1 * (2^-2) = 0.25 seconds + jitter
        assert 0 <= delay.total_seconds() <= 5

    def test_very_small_max_delay(self):
        """Test with very small max_delay"""
        delay = calculate_retry_delay(10, "exponential", initial_delay=1, max_delay=1)
        # Capped at 1 second + jitter (up to 0.1)
        assert delay.total_seconds() <= 1.1

    def test_zero_initial_delay(self):
        """Test with zero initial delay"""
        delay = calculate_retry_delay(5, "exponential", initial_delay=0, max_delay=3600)
        # 0 * (2^4) = 0 + jitter
        assert 0 <= delay.total_seconds() <= 5

    def test_fibonacci_zero_error_count(self):
        """Test fibonacci with zero/negative error count"""
        # This tests the fib(n <= 0) case (line 68)
        delay = calculate_retry_delay(0, "fibonacci", initial_delay=1, max_delay=3600)
        # fib(0) = 0, so delay should be just jitter
        assert 0 <= delay.total_seconds() <= 5


class TestGetRetryConfig:
    """Test get_retry_config helper function"""

    def test_none_admin_data(self):
        """Test with None admin_data"""
        config = get_retry_config(None)
        assert config == {
            "retry_strategy": "exponential",
            "max_retries": 10,
            "initial_retry_delay": 1,
            "max_retry_delay": 3600,
        }

    def test_empty_admin_data(self):
        """Test with empty dict admin_data"""
        config = get_retry_config({})
        assert config == {
            "retry_strategy": "exponential",
            "max_retries": 10,
            "initial_retry_delay": 1,
            "max_retry_delay": 3600,
        }

    def test_custom_retry_strategy(self):
        """Test with custom retry strategy"""
        admin_data = {"retry_strategy": "linear"}
        config = get_retry_config(admin_data)
        assert config["retry_strategy"] == "linear"
        assert config["max_retries"] == 10  # default

    def test_custom_max_retries(self):
        """Test with custom max_retries"""
        admin_data = {"max_retries": 5}
        config = get_retry_config(admin_data)
        assert config["max_retries"] == 5
        assert config["retry_strategy"] == "exponential"  # default

    def test_custom_initial_delay(self):
        """Test with custom initial_retry_delay"""
        admin_data = {"initial_retry_delay": 10}
        config = get_retry_config(admin_data)
        assert config["initial_retry_delay"] == 10

    def test_custom_max_delay(self):
        """Test with custom max_retry_delay"""
        admin_data = {"max_retry_delay": 7200}
        config = get_retry_config(admin_data)
        assert config["max_retry_delay"] == 7200

    def test_all_custom_config(self):
        """Test with all custom configuration"""
        admin_data = {
            "retry_strategy": "fibonacci",
            "max_retries": 20,
            "initial_retry_delay": 5,
            "max_retry_delay": 1800,
        }
        config = get_retry_config(admin_data)
        assert config == admin_data

    def test_extra_fields_ignored(self):
        """Test that extra fields in admin_data are ignored"""
        admin_data = {
            "retry_strategy": "linear",
            "extra_field": "ignored",
            "another_field": 123,
        }
        config = get_retry_config(admin_data)
        assert "extra_field" not in config
        assert "another_field" not in config
        assert config["retry_strategy"] == "linear"


class TestCalculateRetryFromJob:
    """Test calculate_retry_from_job integration function"""

    def test_job_with_no_admin_data(self):
        """Test job with no admin_data"""
        job = {"id": 1, "job_class": "Test"}
        delay = calculate_retry_from_job(job, error_count=3)
        # Should use defaults: exponential, 1 * (2^2) = 4 seconds
        assert 4 <= delay.total_seconds() <= 5

    def test_job_with_empty_admin_data(self):
        """Test job with empty admin_data"""
        job = {"id": 1, "admin_data": {}}
        delay = calculate_retry_from_job(job, error_count=2)
        # Should use defaults: exponential, 1 * (2^1) = 2 seconds
        assert 2 <= delay.total_seconds() <= 2.5

    def test_job_with_custom_strategy(self):
        """Test job with custom retry strategy"""
        job = {"id": 1, "admin_data": {"retry_strategy": "linear", "initial_retry_delay": 10}}
        delay = calculate_retry_from_job(job, error_count=3)
        # Linear: 10 * 3 = 30 seconds
        assert 30 <= delay.total_seconds() <= 33

    def test_job_with_full_config(self):
        """Test job with full retry configuration"""
        job = {
            "id": 1,
            "admin_data": {
                "retry_strategy": "fibonacci",
                "initial_retry_delay": 2,
                "max_retry_delay": 100,
                "max_retries": 15,
            },
        }
        delay = calculate_retry_from_job(job, error_count=5)
        # Fibonacci: fib(5) = 5, 2 * 5 = 10 seconds
        assert 10 <= delay.total_seconds() <= 11

    def test_job_with_high_error_count(self):
        """Test job with high error count and max_delay cap"""
        job = {"id": 1, "admin_data": {"max_retry_delay": 60}}
        delay = calculate_retry_from_job(job, error_count=20)
        # Would be huge, but capped at 60
        assert delay.total_seconds() <= 65  # 60 + max jitter


class TestRetryStrategyIntegration:
    """Integration tests for full retry workflow"""

    def test_retry_progression_exponential(self):
        """Test retry delay progression with exponential strategy"""
        job = {"admin_data": {"retry_strategy": "exponential", "initial_retry_delay": 1}}

        delays = [calculate_retry_from_job(job, i) for i in range(1, 6)]

        # Delays should generally increase exponentially
        # (allowing for jitter, they should be roughly 1, 2, 4, 8, 16)
        assert delays[0].total_seconds() < 2
        assert delays[1].total_seconds() < 3
        assert delays[2].total_seconds() < 5
        assert delays[3].total_seconds() < 10
        assert delays[4].total_seconds() < 20

    def test_retry_progression_linear(self):
        """Test retry delay progression with linear strategy"""
        job = {"admin_data": {"retry_strategy": "linear", "initial_retry_delay": 5}}

        delays = [calculate_retry_from_job(job, i) for i in range(1, 6)]

        # Delays should increase linearly (5, 10, 15, 20, 25)
        for i, delay in enumerate(delays, 1):
            expected = 5 * i
            assert expected <= delay.total_seconds() <= expected + 1

    def test_max_delay_prevents_infinite_growth(self):
        """Test that max_delay prevents unbounded growth"""
        job = {"admin_data": {"max_retry_delay": 30}}

        # Even with very high error counts, delay should be capped
        delay1 = calculate_retry_from_job(job, 50)
        delay2 = calculate_retry_from_job(job, 100)

        assert delay1.total_seconds() <= 35  # 30 + jitter
        assert delay2.total_seconds() <= 35  # Also capped
        # Both should be similar since they're both capped
        assert abs(delay1.total_seconds() - delay2.total_seconds()) < 10
