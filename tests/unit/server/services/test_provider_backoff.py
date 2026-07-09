"""Unit tests for provider_backoff.execute_with_backoff (Bug #1078 Phase 1).

All tests use controllable fake callables — NO mocked providers.
Sleeps are patched to run at zero cost and to assert bound compliance.
"""

from typing import List, Optional
from unittest.mock import patch

import httpx
import pytest

from code_indexer.services.provider_backoff import (
    ProviderRateLimitedError,
    execute_with_backoff,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _429_response(retry_after: Optional[str] = None) -> httpx.Response:
    """Build a minimal httpx.Response with status 429."""
    headers = {}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    request = httpx.Request("POST", "https://api.voyageai.com/v1/embed")
    return httpx.Response(429, headers=headers, request=request)


def _raise_429(retry_after: Optional[str] = None):
    """Callable that raises an httpx.HTTPStatusError with status 429."""

    def fn():
        raise httpx.HTTPStatusError(
            "429 Too Many Requests",
            request=httpx.Request("POST", "https://api.example.com"),
            response=_429_response(retry_after),
        )

    return fn


def _succeed_on_attempt(target: int, results: List[int]):
    """Callable that raises 429 on attempts 1..target-1, then returns 'ok'."""
    call_count = [0]

    def fn():
        call_count[0] += 1
        if call_count[0] < target:
            raise httpx.HTTPStatusError(
                "429",
                request=httpx.Request("POST", "https://x"),
                response=_429_response(),
            )
        results.append(call_count[0])
        return "ok"

    return fn


# ---------------------------------------------------------------------------
# Basic success / failure
# ---------------------------------------------------------------------------


class TestBasicBehavior:
    def test_success_on_first_attempt(self):
        """No retries needed — returns immediately."""
        result = execute_with_backoff(lambda: "hello", max_retries=2)
        assert result == "hello"

    def test_non_429_exception_propagates_immediately(self):
        """Non-429 errors are NOT retried."""
        call_count = [0]

        def fn():
            call_count[0] += 1
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            execute_with_backoff(fn, max_retries=2)

        assert call_count[0] == 1, "Should not retry non-429 errors"

    def test_success_after_one_retry(self):
        """Succeeds on second attempt after one 429."""
        results: List[int] = []
        slept: List[float] = []

        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            result = execute_with_backoff(
                _succeed_on_attempt(2, results), max_retries=2
            )

        assert result == "ok"
        assert len(slept) == 1, "Should sleep once between attempts"
        assert results == [2]

    def test_success_after_two_retries(self):
        """Succeeds on third attempt (max_retries=2, so 3 total attempts)."""
        results: List[int] = []
        slept: List[float] = []

        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            result = execute_with_backoff(
                _succeed_on_attempt(3, results), max_retries=2
            )

        assert result == "ok"
        assert len(slept) == 2, "Should sleep twice between attempts"


# ---------------------------------------------------------------------------
# Attempt count limits
# ---------------------------------------------------------------------------


class TestAttemptLimits:
    def test_exhausts_all_retries_and_raises(self):
        """After max_retries+1 attempts all returning 429, raise ProviderRateLimitedError."""
        call_count = [0]

        def always_429():
            call_count[0] += 1
            raise httpx.HTTPStatusError(
                "429",
                request=httpx.Request("POST", "https://x"),
                response=_429_response(),
            )

        with patch("code_indexer.services.provider_backoff.time.sleep"):
            with pytest.raises(ProviderRateLimitedError):
                execute_with_backoff(always_429, max_retries=2)

        assert call_count[0] == 3, f"Expected 3 attempts, got {call_count[0]}"

    def test_zero_retries_fails_on_single_429(self):
        """max_retries=0 means only 1 attempt; 429 raises immediately."""
        call_count = [0]

        def always_429():
            call_count[0] += 1
            raise httpx.HTTPStatusError(
                "429",
                request=httpx.Request("POST", "https://x"),
                response=_429_response(),
            )

        with pytest.raises(ProviderRateLimitedError):
            execute_with_backoff(always_429, max_retries=0)

        assert call_count[0] == 1


# ---------------------------------------------------------------------------
# Per-attempt cap
# ---------------------------------------------------------------------------


class TestPerAttemptCap:
    def test_sleep_clamped_to_per_attempt_cap(self):
        """Retry-After value exceeding per_attempt_cap is clamped."""
        slept: List[float] = []

        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            with pytest.raises(ProviderRateLimitedError):
                execute_with_backoff(
                    _raise_429(retry_after="9999"),
                    max_retries=2,
                    per_attempt_cap=15.0,
                )

        assert all(s <= 15.0 for s in slept), f"Sleep exceeded per_attempt_cap: {slept}"

    def test_retry_after_honored_when_below_cap(self):
        """Retry-After value below per_attempt_cap is used as-is (within jitter tolerance)."""
        slept: List[float] = []

        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            with pytest.raises(ProviderRateLimitedError):
                execute_with_backoff(
                    _raise_429(retry_after="3.0"),
                    max_retries=2,
                    per_attempt_cap=15.0,
                    cumulative_cap=45.0,
                )

        # Each sleep should be <= 3.0 (full jitter on top of Retry-After in [0, min(RA, cap)])
        for s in slept:
            assert s <= 3.0 + 0.01, f"Sleep {s} exceeded Retry-After + tolerance"


# ---------------------------------------------------------------------------
# Cumulative cap
# ---------------------------------------------------------------------------


class TestCumulativeCap:
    def test_stops_early_when_cumulative_would_exceed_budget(self):
        """Stop and raise ProviderRateLimitedError before sleeping if cumulative cap reached."""
        cumulative_sleep = [0.0]
        cap = 10.0
        per_cap = 8.0

        def counting_sleep(s: float) -> None:
            cumulative_sleep[0] += s

        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=counting_sleep,
        ):
            with pytest.raises(ProviderRateLimitedError):
                execute_with_backoff(
                    _raise_429(retry_after="8.0"),
                    max_retries=5,
                    per_attempt_cap=per_cap,
                    cumulative_cap=cap,
                )

        assert cumulative_sleep[0] <= cap + 0.01, (
            f"Cumulative sleep {cumulative_sleep[0]} exceeded cap {cap}"
        )

    def test_cumulative_cap_within_60s_caller_timeout(self):
        """Default cumulative_cap (45s) is well within the 60s caller timeout."""
        from code_indexer.services.provider_backoff import _DEFAULT_CUMULATIVE_CAP

        assert _DEFAULT_CUMULATIVE_CAP < 60.0


# ---------------------------------------------------------------------------
# Jitter
# ---------------------------------------------------------------------------


class TestJitter:
    def test_sleep_values_are_non_negative(self):
        """Full jitter always produces non-negative sleep duration."""
        slept: List[float] = []

        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            with pytest.raises(ProviderRateLimitedError):
                execute_with_backoff(_raise_429(), max_retries=2)

        assert all(s >= 0.0 for s in slept), f"Got negative sleep: {slept}"

    def test_sleep_values_respect_per_attempt_cap(self):
        """No individual sleep exceeds per_attempt_cap."""
        slept: List[float] = []
        cap = 5.0

        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            with pytest.raises(ProviderRateLimitedError):
                execute_with_backoff(_raise_429(), max_retries=2, per_attempt_cap=cap)

        assert all(s <= cap + 0.01 for s in slept), f"Sleep exceeded cap: {slept}"


# ---------------------------------------------------------------------------
# ProviderRateLimitedError attributes
# ---------------------------------------------------------------------------


class TestErrorAttributes:
    def test_error_carries_attempt_count(self):
        """ProviderRateLimitedError exposes the number of attempts made."""
        with patch("code_indexer.services.provider_backoff.time.sleep"):
            with pytest.raises(ProviderRateLimitedError) as exc_info:
                execute_with_backoff(_raise_429(), max_retries=2)

        err = exc_info.value
        assert hasattr(err, "attempts")
        assert err.attempts == 3
