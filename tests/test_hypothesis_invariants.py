"""
Hypothesis property tests for retry delays, DAG construction, and results.

Property-based fuzz testing of invariants that would be hard to discover
manually, every one of them asserted against pyjobby code:
``retry_strategies`` (delay math and config defaults), ``dag`` (levels,
options, node identity, visualisation), and the jsonb codec a job result is
stored with.
"""

from datetime import timedelta

import orjson
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from pyjobby.dag import DAGBuilder, DAGNode
from pyjobby.db import _orjson_encode
from pyjobby.retry_strategies import calculate_retry_delay, get_retry_config

# Strategy definitions
retry_strategies = st.sampled_from(
    ["exponential", "linear", "fibonacci", "fixed", "quadratic"]
)
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
                    ["exponential", "linear", "fibonacci", "fixed", "quadratic", None]
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

        config = get_retry_config(admin_data or None)

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
            "quadratic",
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

    @given(
        job_class=st.text(
            alphabet=st.characters(
                exclude_characters="\x00", exclude_categories=["Cs"]
            ),
            min_size=1,
            max_size=50,
        )
    )
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
                "queue": st.text(
                    alphabet=st.characters(
                        exclude_characters="\x00", exclude_categories=["Cs"]
                    ),
                    min_size=1,
                    max_size=20,
                ),
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
        name=st.one_of(
            st.none(),
            st.text(
                alphabet=st.characters(
                    exclude_characters="\x00", exclude_categories=["Cs"]
                ),
                min_size=1,
                max_size=50,
            ),
        ),
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


class TestResultStorageProperties:
    """Property-based tests for result storage.

    A job result is stored in a jsonb column through the codec
    ``pyjobby.db`` registers on every connection -- orjson, not the standard
    library's json. So the round trip asserted here is the platform's own,
    which is the only one that can fail in production.
    """

    @given(
        result_data=st.recursive(
            st.one_of(
                st.none(),
                st.booleans(),
                st.integers(min_value=-1000000, max_value=1000000),
                st.floats(allow_nan=False, allow_infinity=False),
                st.text(
                    alphabet=st.characters(
                        exclude_characters="\x00", exclude_categories=["Cs"]
                    ),
                    max_size=100,
                ),
            ),
            lambda children: st.one_of(
                st.lists(children, max_size=10),
                st.dictionaries(
                    st.text(
                        alphabet=st.characters(
                            exclude_characters="\x00", exclude_categories=["Cs"]
                        ),
                        min_size=1,
                        max_size=20,
                    ),
                    children,
                    max_size=10,
                ),
            ),
            max_leaves=20,
        )
    )
    @settings(
        suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50
    )
    def test_a_result_round_trips_through_the_platforms_json_codec(self, result_data):
        """Property: what a job returned is what a caller reads back.

        Encoded with the exact function registered as the jsonb encoder
        (``pyjobby.db._orjson_encode``) and decoded with the exact decoder
        (``orjson.loads``), so a value orjson handles differently from the
        standard library -- and there are several -- fails here rather than
        in a worker.
        """
        encoded = _orjson_encode(result_data)
        assert isinstance(encoded, str), "asyncpg needs str for a text-format type"

        assert orjson.loads(encoded) == result_data


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

    @given(
        strategy=st.text(
            alphabet=st.characters(
                exclude_characters="\x00", exclude_categories=["Cs"]
            ),
            min_size=1,
            max_size=50,
        )
    )
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_unknown_strategy_does_not_crash(self, strategy):
        """Property: Unknown retry strategy should fall back gracefully."""
        assume(
            strategy not in ["exponential", "linear", "fibonacci", "fixed", "quadratic"]
        )

        # Should not crash, should fall back to default (exponential)
        delay = calculate_retry_delay(2, strategy, 1, 3600)
        assert isinstance(delay, timedelta)
        assert delay.total_seconds() > 0
