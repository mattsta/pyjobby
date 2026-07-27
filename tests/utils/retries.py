"""Assertions about retry backoff, which is deliberately not deterministic.

`calculate_retry_delay` adds jitter so that a batch of jobs failing together
does not come back together. That makes every delay a range rather than a
value, and the range is **one-sided**: jitter only ever runs upward, by 0-10%
of the base delay capped at 5 seconds.

Getting that wrong is not hypothetical. Several tests asserted a symmetric
window (`9.5 <= d <= 10.5` for a base of 10) or an exact equality (`== 1.0`),
and they passed for a while because the delay was truncated with `int()` --
which floored every sample back onto the base second and, in doing so,
quantized away the jitter that is the whole point. Both the truncation and the
assertions that depended on it are gone; this module is what replaced them, in
one place, so the contract is stated once.
"""

from __future__ import annotations

from datetime import timedelta


def jitter_window(base: float) -> tuple[float, float]:
    """The inclusive range a jittered delay of `base` seconds can land in."""
    return base, base + min(base * 0.1, 5)


def assert_jittered(delay: timedelta, base: float) -> None:
    """`delay` is `base` seconds plus jitter, and jitter only runs upward."""
    low, high = jitter_window(base)
    seconds = delay.total_seconds()
    assert low <= seconds <= high, (
        f"{seconds}s is outside the jitter window [{low}, {high}]"
    )
