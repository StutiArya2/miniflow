from __future__ import annotations

import math
import random
import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from miniflow import task
from miniflow.retry import Deadline, RetryPolicy, TaskTimeoutError, deadline

from .conftest import noop

finite = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
policies = st.builds(
    RetryPolicy, base=finite, cap=finite, jitter=st.floats(min_value=0.0, max_value=10.0)
)
attempts = st.integers(min_value=1, max_value=5000)
seeds = st.integers(min_value=0, max_value=2**32 - 1)


def test_defaults_and_frozen():
    policy = RetryPolicy()
    assert (policy.base, policy.cap, policy.jitter) == (0.1, 30.0, 0.1)
    with pytest.raises(AttributeError):
        policy.base = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [{"base": -1.0}, {"cap": -0.5}, {"jitter": -0.1}, {"base": math.nan}, {"cap": math.inf}],
)
def test_rejects_invalid_parameters(kwargs):
    with pytest.raises(ValueError, match="RetryPolicy"):
        RetryPolicy(**kwargs)


def test_rejects_attempt_below_one():
    with pytest.raises(ValueError, match="attempt"):
        RetryPolicy().delay(0)


def test_exponential_schedule_without_jitter():
    policy = RetryPolicy(base=0.5, cap=3.0, jitter=0.0)
    assert [policy.delay(n) for n in range(1, 6)] == [0.5, 1.0, 2.0, 3.0, 3.0]


def test_huge_attempt_is_capped_not_overflowing():
    assert RetryPolicy(base=1.0, cap=7.0, jitter=0.0).delay(10**6) == 7.0


@given(policies, attempts, seeds)
def test_delay_is_never_negative(policy, attempt, seed):
    assert policy.delay(attempt, random.Random(seed)) >= 0.0


@given(policies, attempts, seeds)
def test_delay_never_exceeds_jittered_cap(policy, attempt, seed):
    bound = policy.cap * (1 + policy.jitter)
    d = policy.delay(attempt, random.Random(seed))
    assert d <= bound or math.isclose(d, bound, rel_tol=1e-12)


@given(policies, attempts, seeds)
def test_jitter_stays_within_fraction_of_backoff(policy, attempt, seed):
    backoff = RetryPolicy(policy.base, policy.cap, 0.0).delay(attempt)
    d = policy.delay(attempt, random.Random(seed))
    assert backoff <= d
    assert d <= backoff * (1 + policy.jitter) or math.isclose(
        d, backoff * (1 + policy.jitter), rel_tol=1e-12
    )


@given(finite, finite, st.integers(min_value=1, max_value=2000))
def test_delay_is_monotonic_without_jitter(base, cap, attempt):
    policy = RetryPolicy(base=base, cap=cap, jitter=0.0)
    assert policy.delay(attempt) <= policy.delay(attempt + 1)


@given(policies, st.lists(attempts, min_size=1, max_size=20), seeds)
def test_delay_is_reproducible_with_seeded_rng(policy, sequence, seed):
    rng_a, rng_b = random.Random(seed), random.Random(seed)
    assert [policy.delay(n, rng_a) for n in sequence] == [policy.delay(n, rng_b) for n in sequence]


@pytest.mark.parametrize("retries", [0, 1, 3])
def test_should_retry_at_attempt_boundary(retries):
    t = task(noop, name="t", retries=retries)
    policy = RetryPolicy()
    for attempt in range(1, t.max_attempts):
        assert policy.should_retry(t, attempt)
    assert not policy.should_retry(t, t.max_attempts)
    assert not policy.should_retry(t, t.max_attempts + 1)


@given(st.integers(min_value=0, max_value=20), st.integers(min_value=1, max_value=30))
def test_should_retry_matches_budget(retries, attempt):
    t = task(noop, name="t", retries=retries)
    assert RetryPolicy().should_retry(t, attempt) == (attempt <= retries)


def test_task_timeout_error_is_timeout_error():
    assert issubclass(TaskTimeoutError, TimeoutError)
    with pytest.raises(TimeoutError):
        raise TaskTimeoutError("slow")


def test_unbounded_deadline_never_expires():
    with deadline(None) as d:
        assert isinstance(d, Deadline)
        assert d.remaining() is None
        assert not d.expired()


def test_deadline_counts_down_and_expires():
    with deadline(0.05) as d:
        first = d.remaining()
        assert first is not None
        assert 0.0 < first <= 0.05
        assert not d.expired()
        time.sleep(0.1)
        assert d.expired()
        assert d.remaining() == 0.0


def test_zero_deadline_is_already_expired():
    with deadline(0) as d:
        assert d.expired()
        assert d.remaining() == 0.0


def test_deadline_repr():
    assert repr(Deadline(None)) == "Deadline(seconds=None, remaining=None)"
