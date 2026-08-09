"""Unit tests for Bug #1539's RepeatedFailureGuard."""

import pytest

from code_indexer.global_repos.repeated_failure_guard import RepeatedFailureGuard


def test_first_failure_returns_count_one():
    guard = RepeatedFailureGuard(threshold=3)
    assert guard.record_failure("repo-a", "shape-x") == 1


def test_same_fingerprint_increments_count():
    guard = RepeatedFailureGuard(threshold=3)
    guard.record_failure("repo-a", "shape-x")
    guard.record_failure("repo-a", "shape-x")
    assert guard.record_failure("repo-a", "shape-x") == 3


def test_different_fingerprint_resets_count_to_one():
    guard = RepeatedFailureGuard(threshold=3)
    guard.record_failure("repo-a", "shape-x")
    guard.record_failure("repo-a", "shape-x")
    assert guard.record_failure("repo-a", "shape-y") == 1


def test_is_exhausted_true_at_and_above_threshold():
    guard = RepeatedFailureGuard(threshold=3)
    assert guard.is_exhausted(3) is True
    assert guard.is_exhausted(4) is True


def test_is_exhausted_false_below_threshold():
    guard = RepeatedFailureGuard(threshold=3)
    assert guard.is_exhausted(1) is False
    assert guard.is_exhausted(2) is False


def test_reset_clears_tracked_state():
    guard = RepeatedFailureGuard(threshold=3)
    guard.record_failure("repo-a", "shape-x")
    guard.record_failure("repo-a", "shape-x")
    guard.reset("repo-a")
    # Same fingerprint after reset must start over at count 1, not 3.
    assert guard.record_failure("repo-a", "shape-x") == 1


def test_reset_on_unknown_key_is_a_no_op():
    guard = RepeatedFailureGuard(threshold=3)
    guard.reset("never-seen")  # must not raise


def test_keys_are_tracked_independently():
    guard = RepeatedFailureGuard(threshold=3)
    guard.record_failure("repo-a", "shape-x")
    guard.record_failure("repo-a", "shape-x")
    assert guard.record_failure("repo-b", "shape-x") == 1


def test_constructor_rejects_non_positive_threshold():
    with pytest.raises(ValueError):
        RepeatedFailureGuard(threshold=0)
