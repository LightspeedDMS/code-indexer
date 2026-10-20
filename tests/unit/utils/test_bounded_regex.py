"""Caller-supplied patterns share one bounded evaluation budget per use."""

from __future__ import annotations

import logging
import re
import time

import pytest

from code_indexer.utils.bounded_regex import BoundedPattern, PatternBudgetExhausted

pytestmark = pytest.mark.timeout(30)
SLOW_PATTERN = r"(a|a)*b"
SLOW_TEXT = "a" * 40 + "!b"


def test_matches_like_re() -> None:
    pattern = BoundedPattern(r"wid(g)et", re.IGNORECASE, purpose="test")
    found = pattern.search("a WIDGET here")
    assert found is not None and found.group(0) == "WIDGET"
    assert pattern.search("nothing") is None
    assert [m.start() for m in pattern.finditer("widget widget")] == [0, 7]


def test_invalid_pattern_raises_re_error() -> None:
    with pytest.raises(re.error):
        BoundedPattern("(", purpose="test")


def test_slow_pattern_stops_within_the_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    pattern = BoundedPattern(SLOW_PATTERN, budget_seconds=0.3, purpose="test search")
    started = time.monotonic()
    with pytest.raises(PatternBudgetExhausted):
        pattern.search(SLOW_TEXT)
    assert time.monotonic() - started < 2.0
    assert pattern.exhausted
    started = time.monotonic()
    for _ in range(100):
        with pytest.raises(PatternBudgetExhausted):
            pattern.search("aaab")
    assert time.monotonic() - started < 0.5
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert SLOW_PATTERN not in warnings[0].getMessage()
    assert "test search" in warnings[0].getMessage()


def test_call_cap_times_out_one_call_without_spending_the_budget() -> None:
    pattern = BoundedPattern(
        SLOW_PATTERN, budget_seconds=5.0, call_seconds=0.05, purpose="test"
    )
    with pytest.raises(PatternBudgetExhausted):
        pattern.search(SLOW_TEXT)
    assert not pattern.exhausted
    found = pattern.search("aab")
    assert found is not None and found.group(0) == "aab"


def test_finditer_shares_the_budget() -> None:
    pattern = BoundedPattern(SLOW_PATTERN, budget_seconds=0.3, purpose="test")
    started = time.monotonic()
    with pytest.raises(PatternBudgetExhausted):
        list(pattern.finditer((SLOW_TEXT + "\n") * 5))
    assert time.monotonic() - started < 2.0
    assert pattern.exhausted


def test_finditer_returns_a_match_without_scanning_the_rest() -> None:
    pattern = BoundedPattern(SLOW_PATTERN + "|c", budget_seconds=0.5, purpose="test")
    matches = pattern.finditer("c" + SLOW_TEXT)
    first = next(matches)
    assert first.group(0) == "c"
    assert not pattern.exhausted
    with pytest.raises(PatternBudgetExhausted):
        next(matches)
