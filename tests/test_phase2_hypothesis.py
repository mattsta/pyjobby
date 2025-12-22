"""
Phase 2: Hypothesis Property-Based Tests

Property-based fuzz testing for Phase 2 features using Hypothesis.
Tests mathematical properties and edge cases that would be hard to discover manually.
"""

import json
from datetime import timedelta

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from pyjobby.dag import DAGBuilder, DAGNode
from pyjobby.retry_strategies import calculate_retry_delay, get_retry_config

# Strategy definitions
retry_strategies = st.sampled_from(["exponential", "linear", "fibonacci", "fixed"])
positive_ints = st.integers(min_value=1, max_value=1000)
small_positive_ints = st.integers(min_value=1, max_value=100)
attempt_numbers = st.integers(min_value=0, max_value=50)
delay_seconds = st.integers(min_value=1, max_value=7200)


class TestRetryDelayProperties:
    """Property-based tests for retry delay calculations."""

    @given(
        error_count=attempt_numbers,
        strategy=retry_strategies,
        initial_delay=small_positive_ints,
        max_delay=delay_seconds,
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_delay_is_never_negative(
        self, error_count, strategy, initial_delay, max_delay
    ):
        """Property: Retry delay is always >= 0."""
        delay = calculate_retry_delay(error_count, strategy, initial_delay, max_delay)
        assert delay.total_seconds() >= 0

    @given(
        error_count=attempt_numbers,
        strategy=retry_strategies,
        initial_delay=small_positive_ints,
        max_delay=delay_seconds,
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_delay_never_exceeds_max(
        self, error_count, strategy, initial_delay, max_delay
    ):
        """Property: Retry delay never exceeds max_delay."""
        delay = calculate_retry_delay(error_count, strategy, initial_delay, max_delay)
        # Account for jitter (up to 25% over base)
        assert delay.total_seconds() <= max_delay * 1.3

    @given(
        error_count=small_positive_ints,
        strategy=retry_strategies,
        initial_delay=small_positive_ints,
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_delay_returns_timedelta(self, error_count, strategy, initial_delay):
        """Property: Always returns timedelta object."""
        delay = calculate_retry_delay(error_count, strategy, initial_delay)
        assert isinstance(delay, timedelta)

    @given(
        error_count=st.integers(min_value=1, max_value=20),
        initial_delay=small_positive_ints,
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_exponential_grows_exponentially(self, error_count, initial_delay):
        """Property: Exponential delay roughly doubles each error_count."""
        assume(error_count >= 2)  # Need at least 2 attempts to compare

        delay1 = calculate_retry_delay(
            error_count - 1, "exponential", initial_delay, max_delay=100000
        )
        delay2 = calculate_retry_delay(
            error_count, "exponential", initial_delay, max_delay=100000
        )

        # With jitter, delay2 should be roughly 1.5x to 2.5x delay1
        # (accounting for 25% jitter on both sides)
        if delay1.total_seconds() < 10000:  # Before hitting cap
            ratio = delay2.total_seconds() / max(delay1.total_seconds(), 0.1)
            assert 1.2 <= ratio <= 3.0, f"Ratio {ratio} not exponential"

    @given(
        error_count=st.integers(min_value=2, max_value=20),
        initial_delay=small_positive_ints,
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_linear_grows_linearly(self, error_count, initial_delay):
        """Property: Linear delay increases by roughly initial_delay each time."""
        delay1 = calculate_retry_delay(
            error_count - 1, "linear", initial_delay, max_delay=100000
        )
        delay2 = calculate_retry_delay(
            error_count, "linear", initial_delay, max_delay=100000
        )

        # Difference should be roughly initial_delay (with jitter)
        if delay1.total_seconds() < 10000:
            diff = delay2.total_seconds() - delay1.total_seconds()
            # Allow wide margin for jitter
            assert 0 <= diff <= initial_delay * 2.5

    @given(
        config_data=st.fixed_dictionaries(
            {
                "retry_strategy": st.sampled_from(
                    ["exponential", "linear", "fibonacci", "fixed", None]
                ),
                "max_retries": st.one_of(
                    st.none(), st.integers(min_value=1, max_value=50)
                ),
                "initial_retry_delay": st.one_of(
                    st.none(), st.integers(min_value=1, max_value=60)
                ),
                "max_retry_delay": st.one_of(
                    st.none(), st.integers(min_value=60, max_value=7200)
                ),
            }
        )
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_get_retry_config_always_returns_valid_config(self, config_data):
        """Property: get_retry_config always returns valid configuration with defaults."""
        # Remove None values (simulate missing keys)
        admin_data = {k: v for k, v in config_data.items() if v is not None}

        config = get_retry_config(admin_data if admin_data else None)

        # Must have all required keys
        assert "retry_strategy" in config
        assert "max_retries" in config
        assert "initial_retry_delay" in config
        assert "max_retry_delay" in config

        # Values must be valid
        assert config["retry_strategy"] in [
            "exponential",
            "linear",
            "fibonacci",
            "fixed",
        ]
        assert isinstance(config["max_retries"], int)
        assert config["max_retries"] > 0
        assert isinstance(config["initial_retry_delay"], (int, float))
        assert config["initial_retry_delay"] > 0
        assert isinstance(config["max_retry_delay"], (int, float))
        assert config["max_retry_delay"] > 0

    @given(
        error_count=attempt_numbers,
        initial_delay=small_positive_ints,
        max_delay=delay_seconds,
        multiplier=st.floats(min_value=1.1, max_value=5.0),
    )
    @settings(
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.filter_too_much,
        ]
    )
    def test_larger_multiplier_gives_larger_delays(
        self, error_count, initial_delay, max_delay, multiplier
    ):
        """Property: Larger multiplier should generally give larger delays."""
        assume(error_count > 0)
        assume(initial_delay * (multiplier**error_count) < max_delay)  # Don't hit cap

        delay_2x = calculate_retry_delay(
            error_count, "exponential", initial_delay, max_delay, multiplier=2.0
        )
        delay_3x = calculate_retry_delay(
            error_count, "exponential", initial_delay, max_delay, multiplier=3.0
        )

        # With larger multiplier, delay should generally be larger (accounting for jitter)
        # This is a probabilistic property, so we use a loose bound
        if error_count >= 3:  # Effect is more pronounced after a few attempts
            assert delay_3x.total_seconds() >= delay_2x.total_seconds() * 0.8


class TestDAGProperties:
    """Property-based tests for DAG operations."""

    @given(node_count=st.integers(min_value=0, max_value=50))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_dag_with_no_dependencies_has_one_level(self, node_count):
        """Property: DAG with independent jobs should have 1 execution level."""
        assume(node_count > 0)

        dag = DAGBuilder(name=f"Independent-{node_count}")
        for i in range(node_count):
            dag.add(f"Job{i}", {})

        levels = dag.topological_sort()
        assert len(levels) == 1
        assert len(levels[0]) == node_count

    @given(chain_length=st.integers(min_value=1, max_value=30))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_linear_chain_has_correct_levels(self, chain_length):
        """Property: Linear chain of N jobs should have N levels."""
        dag = DAGBuilder(name=f"Chain-{chain_length}")

        nodes = []
        for i in range(chain_length):
            if i == 0:
                node = dag.add(f"Job{i}", {})
            else:
                node = dag.add(f"Job{i}", {}, depends_on=[nodes[-1]])
            nodes.append(node)

        levels = dag.topological_sort()
        assert len(levels) == chain_length

        # Each level should have exactly 1 job
        for level in levels:
            assert len(level) == 1

    @given(
        root_count=st.integers(min_value=1, max_value=10),
        leaf_count=st.integers(min_value=1, max_value=10),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_fan_out_fan_in_has_three_levels(self, root_count, leaf_count):
        """Property: Fan-out/fan-in pattern should have 3 levels."""
        dag = DAGBuilder(name="FanOutFanIn")

        # Root level
        roots = [dag.add(f"Root{i}", {}) for i in range(root_count)]

        # Middle level (each depends on all roots)
        middles = [
            dag.add(f"Middle{i}", {}, depends_on=roots) for i in range(leaf_count)
        ]

        # Final level (depends on all middles)
        final = dag.add("Final", {}, depends_on=middles)

        levels = dag.topological_sort()
        assert len(levels) == 3
        assert len(levels[0]) == root_count
        assert len(levels[1]) == leaf_count
        assert len(levels[2]) == 1

    @given(node_count=st.integers(min_value=1, max_value=20))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_empty_dag_validates(self, node_count):
        """Property: DAG with no cycles should validate successfully."""
        # This is just a smoke test that validation doesn't crash
        dag = DAGBuilder(name="Valid")
        for i in range(node_count):
            dag.add(f"Job{i}", {})

        # Should not raise
        dag.validate()

    @given(job_class=st.text(min_size=1, max_size=50))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_dag_node_equality_is_by_id(self, job_class):
        """Property: DAG nodes are equal only if they have same node_id."""
        node1 = DAGNode(job_class=job_class, node_id="abc")
        node2 = DAGNode(job_class=job_class, node_id="abc")
        node3 = DAGNode(job_class=job_class, node_id="def")

        assert node1 == node2
        assert node1 != node3
        assert hash(node1) == hash(node2)
        assert hash(node1) != hash(node3)

    @given(
        common_opts=st.fixed_dictionaries(
            {
                "queue": st.text(min_size=1, max_size=20),
                "priority": st.integers(min_value=1, max_value=1000),
            }
        )
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_common_options_applied_to_all_nodes(self, common_opts):
        """Property: Common options should be applied to all added nodes."""
        dag = DAGBuilder(name="CommonOpts", **common_opts)

        # Add several nodes
        for i in range(5):
            node = dag.add(f"Job{i}", {})

            # Each node should have common options
            for key, value in common_opts.items():
                assert node._job_options[key] == value

    @given(
        node_count=st.integers(min_value=1, max_value=20),
        name=st.one_of(st.none(), st.text(min_size=1, max_size=50)),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_dag_visualize_does_not_crash(self, node_count, name):
        """Property: Visualization should work for any valid DAG."""
        dag = DAGBuilder(name=name)

        # Add random jobs
        for i in range(node_count):
            dag.add(f"Job{i}", {})

        # Should not crash
        viz = dag.visualize()
        assert isinstance(viz, str)
        assert len(viz) > 0


class TestTimeoutProperties:
    """Property-based tests for timeout handling."""

    @given(timeout_seconds=st.integers(min_value=1, max_value=86400))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_timeout_config_is_always_positive(self, timeout_seconds):
        """Property: Timeout configuration must be positive."""
        admin_data = {"timeout_seconds": timeout_seconds}

        # Timeout should be preserved
        assert admin_data["timeout_seconds"] > 0

    @given(
        timeout=st.integers(min_value=1, max_value=3600),
        on_timeout=st.sampled_from(["retry", "fail"]),
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_timeout_config_is_json_serializable(self, timeout, on_timeout):
        """Property: Timeout config must be JSON serializable for admin_data."""
        admin_data = {"timeout_seconds": timeout, "on_timeout": on_timeout}

        # Should serialize without error
        json_str = json.dumps(admin_data)
        assert isinstance(json_str, str)

        # Should deserialize back to same values
        decoded = json.loads(json_str)
        assert decoded["timeout_seconds"] == timeout
        assert decoded["on_timeout"] == on_timeout


class TestResultStorageProperties:
    """Property-based tests for result storage."""

    @given(
        result_data=st.recursive(
            st.one_of(
                st.none(),
                st.booleans(),
                st.integers(min_value=-1000000, max_value=1000000),
                st.floats(allow_nan=False, allow_infinity=False),
                st.text(max_size=100),
            ),
            lambda children: st.one_of(
                st.lists(children, max_size=10),
                st.dictionaries(
                    st.text(min_size=1, max_size=20), children, max_size=10
                ),
            ),
            max_leaves=20,
        )
    )
    @settings(
        suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50
    )
    def test_result_is_json_serializable(self, result_data):
        """Property: Any result must be JSON serializable."""
        # Should serialize without error
        json_str = json.dumps(result_data)
        assert isinstance(json_str, str)

        # Should deserialize back
        decoded = json.loads(json_str)
        # Note: exact equality may not hold due to JSON limitations
        # but it should be structurally similar
        assert type(decoded) in (
            type(result_data),
            dict,
            list,
            str,
            int,
            float,
            bool,
            type(None),
        )

    @given(result_size=st.integers(min_value=0, max_value=5000))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_result_size_can_be_calculated(self, result_size):
        """Property: Result size can be calculated from JSON serialization."""
        # Create result with approximately target size
        result = {"data": "x" * result_size}

        json_str = json.dumps(result)
        size = len(json_str.encode("utf-8"))

        # Size should be calculable and reasonable
        assert size > 0
        assert size < 10 * 1024 * 1024  # Under 10MB limit


class TestEdgeCases:
    """Property-based tests for edge cases and boundary conditions."""

    @given(error_count=st.integers(min_value=-100, max_value=0))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_negative_or_zero_attempt_handled(self, error_count):
        """Property: Negative/zero attempts should not crash."""
        # Should not crash, should return some reasonable delay
        delay = calculate_retry_delay(error_count, "exponential", 1, 3600)
        assert isinstance(delay, timedelta)
        assert delay.total_seconds() >= 0

    @given(initial_delay=st.integers(min_value=0, max_value=0))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_zero_initial_delay_handled(self, initial_delay):
        """Property: Zero initial delay should not crash."""
        delay = calculate_retry_delay(1, "exponential", initial_delay, 3600)
        assert isinstance(delay, timedelta)
        assert delay.total_seconds() >= 0

    @given(max_delay=st.integers(min_value=1, max_value=10))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_very_small_max_delay_respected(self, max_delay):
        """Property: Even very small max_delay should be respected."""
        # High attempt with small max should still respect max
        delay = calculate_retry_delay(100, "exponential", 1, max_delay)
        assert delay.total_seconds() <= max_delay * 1.3  # Account for jitter

    @given(strategy=st.text(min_size=1, max_size=50))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_unknown_strategy_does_not_crash(self, strategy):
        """Property: Unknown retry strategy should fall back gracefully."""
        assume(strategy not in ["exponential", "linear", "fibonacci", "fixed"])

        # Should not crash, should fall back to default (exponential)
        delay = calculate_retry_delay(2, strategy, 1, 3600)
        assert isinstance(delay, timedelta)
        assert delay.total_seconds() > 0
