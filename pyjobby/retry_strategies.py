"""
Retry Strategies for Pyjobby Phase 2

Provides configurable retry backoff strategies to replace fixed retry intervals.
Supports exponential, linear, fibonacci, fixed, and quadratic strategies.
"""

from __future__ import annotations

import datetime
import random
from enum import StrEnum


class RetryStrategy(StrEnum):
    """Retry backoff strategies"""

    FIXED = "fixed"  # Constant delay: initial_delay every attempt
    QUADRATIC = "quadratic"  # Steeply growing: initial * attempt^2
    EXPONENTIAL = "exponential"  # Exponential backoff (recommended)
    LINEAR = "linear"  # Linear increase
    FIBONACCI = "fibonacci"  # Fibonacci sequence


def calculate_retry_delay(
    error_count: int,
    strategy: str = "exponential",
    initial_delay: int = 1,
    max_delay: int = 3600,
    multiplier: float = 2.0,
) -> datetime.timedelta:
    """
    Calculate next retry delay based on strategy.

    Args:
        error_count: Number of previous failures (1-based)
        strategy: 'fixed', 'exponential', 'linear', 'fibonacci'
        initial_delay: Starting delay in seconds
        max_delay: Maximum delay in seconds
        multiplier: Backoff multiplier (for exponential)

    Returns:
        timedelta for retry delay

    Examples:
        >>> calculate_retry_delay(1, 'exponential')  # 1 second
        >>> calculate_retry_delay(5, 'exponential')  # 16 seconds
        >>> calculate_retry_delay(10, 'exponential')  # 512 seconds (capped at max_delay)

    Exponential (default): 1, 2, 4, 8, 16, 32, 64, 128...
    Linear: 1, 2, 3, 4, 5, 6, 7, 8...
    Fibonacci: 1, 1, 2, 3, 5, 8, 13, 21...
    Fixed: 1, 1, 1, 1, 1... (constant initial_delay)
    Quadratic: 1, 4, 9, 16, 25... (initial * attempt^2)
    """
    delay: float
    if strategy == "fixed":
        # Constant delay: every attempt waits the same amount
        delay = initial_delay

    elif strategy == "quadratic":
        # Quadratic backoff: initial * attempts^2 (steeper than linear,
        # gentler than exponential at low attempt counts)
        delay = initial_delay * (error_count**2)

    elif strategy == "exponential":
        # Exponential backoff: initial * (multiplier ^ attempts)
        delay = initial_delay * (multiplier ** (error_count - 1))

    elif strategy == "linear":
        # Linear backoff: initial * attempts
        delay = initial_delay * error_count

    elif strategy == "fibonacci":
        # Fibonacci backoff: F(1)=1, F(2)=1, F(3)=2, F(4)=3, F(5)=5...
        def fib(n: int) -> int:
            if n <= 0:
                return 0
            if n == 1 or n == 2:
                return 1
            a, b = 1, 1
            for _ in range(n - 2):
                a, b = b, a + b
            return b

        delay = initial_delay * fib(error_count)

    else:
        # Default to exponential for unknown strategies
        delay = initial_delay * (multiplier ** (error_count - 1))

    # Add jitter to prevent thundering herd (0-10% of delay, max 5 seconds)
    jitter = random.uniform(0, min(delay * 0.1, 5))
    delay = delay + jitter

    # Cap at max_delay. Deliberately NOT int(): truncating to whole seconds
    # quantizes the jitter away, which is the one thing jitter must not do.
    # A batch failing together at error_count=5 spreads over [16.0, 17.6);
    # truncated, all of it collapses onto 16 or 17 -- two buckets, i.e. the
    # thundering herd the jitter was added to prevent. run_after is
    # TIMESTAMPTZ with microsecond resolution, so there is nothing to round
    # for.
    delay = min(delay, float(max_delay))

    return datetime.timedelta(seconds=delay)


def get_retry_config(admin_data: dict | None) -> dict:
    """
    Extract retry configuration from admin_data.

    Args:
        admin_data: Job's admin_data dict (may be None or JSON string)

    Returns:
        Dict with retry configuration:
        - retry_strategy: str
        - max_retries: int
        - initial_retry_delay: int
        - max_retry_delay: int
    """
    # admin_data is automatically decoded by asyncpg custom codec
    if not admin_data:
        admin_data = {}

    return {
        "retry_strategy": admin_data.get("retry_strategy", "exponential"),
        "max_retries": admin_data.get("max_retries", 10),
        "initial_retry_delay": admin_data.get("initial_retry_delay", 1),
        "max_retry_delay": admin_data.get("max_retry_delay", 3600),
    }


def calculate_retry_from_job(job: dict, error_count: int) -> datetime.timedelta:
    """
    Calculate retry delay for a job based on its admin_data configuration.

    Args:
        job: Job record from database
        error_count: Number of errors so far

    Returns:
        timedelta for retry delay
    """
    config = get_retry_config(job.get("admin_data"))

    return calculate_retry_delay(
        error_count,
        strategy=config["retry_strategy"],
        initial_delay=config["initial_retry_delay"],
        max_delay=config["max_retry_delay"],
    )
