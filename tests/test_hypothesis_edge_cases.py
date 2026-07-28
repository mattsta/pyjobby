"""
Property-based edge-case tests for the retry math and the DAG builder.

Every property here is asserted against pyjobby code -- either
``pyjobby.retry_strategies`` (delay math, config resolution) or
``pyjobby.dag`` (topological sort, cycle refusal). Properties that only
restated Python's own semantics (a sorted list is sorted, ``json.dumps``
round-trips, ``now + timedelta > now``) used to live here; they asserted
nothing about the platform and are gone.

Test categories:
1. Retry strategy edge cases (large error counts, boundary values, the
   jitter band every strategy's growth has to stay inside)
2. DAG topology (parallel branches, self-dependency refusal, and the
   ordering guarantee topological_sort() exists to provide)
3. admin_data retry settings reaching the calculation that uses them
"""

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from pyjobby.dag import DAGBuilder
from pyjobby.retry_strategies import (
    calculate_retry_delay,
    calculate_retry_from_job,
    get_retry_config,
)

#: calculate_retry_delay() adds jitter of uniform(0, min(delay * 0.1, 5)),
#: so every returned delay sits in [base, base * 1.1]. Two delays computed
#: from the SAME base are therefore within a factor of 1.1 of each other,
#: which is what lets these properties compare the platform's outputs
#: without re-deriving the platform's arithmetic.
JITTER_FACTOR = 1.1


@pytest.mark.hypothesis
class TestRetryStrategyEdgeCases:
    """Property-based tests for retry strategy edge cases."""

    @given(
        error_count=st.integers(min_value=1, max_value=1000),
        initial_delay=st.integers(min_value=1, max_value=3600),
        max_delay=st.integers(min_value=60, max_value=86400),
    )
    @settings(suppress_health_check=[HealthCheck.filter_too_much])
    def test_retry_delay_never_exceeds_max(self, error_count, initial_delay, max_delay):
        """Property: Retry delay must never exceed max_delay."""
        assume(max_delay >= initial_delay)

        for strategy in ["exponential", "linear", "fibonacci", "fixed", "quadratic"]:
            delay = calculate_retry_delay(
                error_count,
                strategy=strategy,
                initial_delay=initial_delay,
                max_delay=max_delay,
            )
            assert delay.total_seconds() <= max_delay, (
                f"{strategy} strategy violated max_delay: {delay.total_seconds()} > {max_delay}"
            )

    @given(
        error_count=st.integers(min_value=1, max_value=100),
        multiplier=st.floats(min_value=1.1, max_value=10.0),
    )
    def test_exponential_backoff_increases_monotonically(self, error_count, multiplier):
        """Property: Exponential backoff increases with error count."""
        if error_count < 2:
            return

        delay1 = calculate_retry_delay(
            error_count - 1,
            strategy="exponential",
            multiplier=multiplier,
            max_delay=1000000,  # Large max to avoid capping
        )
        delay2 = calculate_retry_delay(
            error_count,
            strategy="exponential",
            multiplier=multiplier,
            max_delay=1000000,
        )

        # With jitter, delay might not be strictly monotonic, but should be close
        # Allow for jitter (up to 10% + 5s)
        assert delay2.total_seconds() >= delay1.total_seconds() * 0.8, (
            f"Exponential backoff not increasing: {delay1} -> {delay2}"
        )

    @given(
        error_count=st.integers(min_value=3, max_value=25),
        initial_delay=st.integers(min_value=1, max_value=10),
    )
    def test_fibonacci_delays_obey_the_fibonacci_recurrence(
        self, error_count, initial_delay
    ):
        """Property: the fibonacci strategy's own delays satisfy
        F(n) = F(n-1) + F(n-2).

        Asserted on what calculate_retry_delay() RETURNS rather than on a
        copy of the sequence: a private fib() in the test agreeing with
        itself said nothing about the delay a failed job actually waits.
        The comparison is a band because each delay carries jitter (see
        JITTER_FACTOR); max_delay is far above the largest base here, so
        the cap never truncates the recurrence.
        """
        max_delay = 10**9

        def delay_for(n: int) -> float:
            return calculate_retry_delay(
                n,
                strategy="fibonacci",
                initial_delay=initial_delay,
                max_delay=max_delay,
            ).total_seconds()

        previous_two = delay_for(error_count - 1) + delay_for(error_count - 2)
        this_one = delay_for(error_count)

        assert previous_two / JITTER_FACTOR - 1e-9 <= this_one, (
            f"fibonacci delay {this_one} is below F(n-1)+F(n-2)={previous_two}"
        )
        assert this_one <= previous_two * JITTER_FACTOR + 1e-9, (
            f"fibonacci delay {this_one} is above F(n-1)+F(n-2)={previous_two}"
        )

    @given(
        error_count=st.integers(min_value=1, max_value=100),
        initial_delay=st.integers(min_value=1, max_value=60),
    )
    def test_linear_backoff_proportional(self, error_count, initial_delay):
        """Property: Linear backoff is proportional to error count."""
        delay = calculate_retry_delay(
            error_count,
            strategy="linear",
            initial_delay=initial_delay,
            max_delay=1000000,
        )

        # Linear: delay = initial * error_count (+ jitter)
        expected_base = initial_delay * error_count
        # Allow jitter (0-10% of delay or 5s, whichever is smaller)
        max_jitter = min(expected_base * 0.1, 5)

        assert expected_base <= delay.total_seconds() <= expected_base + max_jitter + 1

    @given(
        admin_data=st.one_of(
            st.none(),
            st.dictionaries(
                keys=st.sampled_from(
                    [
                        "retry_strategy",
                        "max_retries",
                        "initial_retry_delay",
                        "max_retry_delay",
                    ]
                ),
                values=st.one_of(
                    st.integers(),
                    st.text(
                        alphabet=st.characters(
                            exclude_characters="\x00", exclude_categories=["Cs"]
                        ),
                        max_size=20,
                    ),
                ),
            ),
        )
    )
    def test_get_retry_config_handles_invalid_data(self, admin_data):
        """Property: get_retry_config handles any admin_data without crashing."""
        # Should not raise exception, should return valid defaults
        config = get_retry_config(admin_data)

        assert "retry_strategy" in config
        assert "max_retries" in config
        assert "initial_retry_delay" in config
        assert "max_retry_delay" in config


@pytest.mark.hypothesis
class TestAdminDataReachesTheRetryMath:
    """A job's admin_data is where an operator writes retry policy. These
    pin that the written values are the ones a retry actually waits for --
    the failure mode being a silent fall back to the platform defaults."""

    @given(
        retry_strategy=st.sampled_from(
            ["exponential", "linear", "fibonacci", "fixed", "quadratic"]
        ),
        initial_retry_delay=st.integers(min_value=2, max_value=60),
        max_retry_delay=st.integers(min_value=600, max_value=7200),
        max_retries=st.integers(min_value=1, max_value=50),
        error_count=st.integers(min_value=1, max_value=8),
    )
    def test_a_jobs_retry_settings_are_the_ones_used(
        self,
        retry_strategy,
        initial_retry_delay,
        max_retry_delay,
        max_retries,
        error_count,
    ):
        admin_data = {
            "retry_strategy": retry_strategy,
            "max_retries": max_retries,
            "initial_retry_delay": initial_retry_delay,
            "max_retry_delay": max_retry_delay,
        }

        # 1. the config resolver hands back what the job declared, verbatim
        assert get_retry_config(admin_data) == admin_data

        # 2. ...and the job-level entry point computes the delay those
        #    settings describe, not the delay the defaults would give
        from_job = calculate_retry_from_job(
            {"id": 1, "admin_data": admin_data}, error_count
        ).total_seconds()
        direct = calculate_retry_delay(
            error_count,
            strategy=retry_strategy,
            initial_delay=initial_retry_delay,
            max_delay=max_retry_delay,
        ).total_seconds()

        # both carry independent jitter over the same base, so they agree
        # within the jitter band and no further
        assert from_job <= direct * JITTER_FACTOR + 1e-9
        assert direct <= from_job * JITTER_FACTOR + 1e-9


@pytest.mark.hypothesis
class TestDAGTopologyProperties:
    """Property-based tests for DAG structure invariants."""

    @given(
        num_branches=st.integers(min_value=2, max_value=10),
        jobs_per_branch=st.integers(min_value=1, max_value=10),
    )
    def test_parallel_branches_advance_one_level_at_a_time(
        self, num_branches, jobs_per_branch
    ):
        """Property: N independent chains of M jobs sort into M levels of N.

        Branches never join, so nothing in one branch may be pushed to a
        later level by the length of another -- the whole point of running
        them in parallel.
        """
        dag = DAGBuilder(name="Parallel")
        for branch in range(num_branches):
            previous = None
            for step in range(jobs_per_branch):
                previous = dag.add(
                    f"B{branch}S{step}",
                    {},
                    depends_on=[previous] if previous is not None else None,
                )

        levels = dag.topological_sort()

        assert len(levels) == jobs_per_branch
        assert all(len(level) == num_branches for level in levels)
        assert sum(len(level) for level in levels) == num_branches * jobs_per_branch

    @given(
        node_count=st.integers(min_value=1, max_value=10),
        loop_at=st.integers(min_value=0, max_value=9),
    )
    def test_a_node_depending_on_itself_is_refused(self, node_count, loop_at):
        """Property: a self-edge is a cycle, and both DAG entry points say so.

        DAGBuilder.add() cannot express a self-edge (the node does not exist
        until add() returns), so the loop is attached the way a caller
        assembling DAGNodes by hand would produce it. Neither validate() nor
        topological_sort() may accept it: a self-dependent job waits for
        itself forever.
        """
        assume(loop_at < node_count)

        dag = DAGBuilder(name="SelfLoop")
        nodes = [dag.add(f"Job{i}", {}) for i in range(node_count)]
        nodes[loop_at].depends_on.append(nodes[loop_at])

        with pytest.raises(ValueError, match="cycle"):
            dag.validate()
        with pytest.raises(ValueError, match="cycle"):
            dag.topological_sort()

    @given(
        node_count=st.integers(min_value=2, max_value=10),
        edges=st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=9),
                st.integers(min_value=0, max_value=9),
            ),
            max_size=25,
        ),
    )
    def test_every_dependency_lands_in_an_earlier_level(self, node_count, edges):
        """Property: topological_sort() places each dependency strictly
        before every node that depends on it, and loses no node.

        This is the guarantee execution rests on -- a level is only started
        once the previous one finished -- asserted over arbitrary graphs
        rather than the three hand-built shapes the fixed tests cover. Edges
        are kept only when they point from a lower index to a higher one,
        which is what makes the generated graph acyclic by construction.
        """
        dag = DAGBuilder(name="Random")
        nodes = [dag.add(f"Job{i}", {}) for i in range(node_count)]

        kept = {(a, b) for a, b in edges if a < b < node_count}
        for upstream, downstream in kept:
            nodes[downstream].depends_on.append(nodes[upstream])

        levels = dag.topological_sort()

        assert sum(len(level) for level in levels) == node_count
        level_of = {
            node.node_id: index for index, level in enumerate(levels) for node in level
        }
        for upstream, downstream in kept:
            assert (
                level_of[nodes[upstream].node_id] < level_of[nodes[downstream].node_id]
            ), f"Job{upstream} must run before Job{downstream}"
