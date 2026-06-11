"""Unit tests for the canonical 429 classifier (Story #1079 Phase A).

``is_rate_limited(exc)`` must recognise a rate-limit signal regardless of how a
provider wrapped it — but only when the signal is structurally intact. A 429
hidden inside a generic ``RuntimeError(str)`` is NOT classifiable (no status
code survives the string formatting); that is exactly why providers must
re-raise the underlying ``httpx.HTTPStatusError`` intact.

``get_http_status_error(exc)`` extracts the underlying ``httpx.HTTPStatusError``
when present (so ``_compute_sleep`` can read the Retry-After header), else None.
"""

from typing import Optional

import httpx
import pytest

from typing import List
from unittest.mock import patch

from code_indexer.services.provider_backoff import (
    ProviderRateLimitedError,
    execute_with_backoff,
    get_http_status_error,
    is_rate_limited,
)


def _http_status_error(
    status_code: int, retry_after: Optional[str] = None
) -> httpx.HTTPStatusError:
    headers = {}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    request = httpx.Request("POST", "https://api.example.com")
    response = httpx.Response(status_code, headers=headers, request=request)
    return httpx.HTTPStatusError(f"{status_code}", request=request, response=response)


class TestIsRateLimitedTrue:
    def test_bare_429_http_status_error_is_rate_limited(self):
        assert is_rate_limited(_http_status_error(429)) is True

    def test_provider_rate_limited_error_is_rate_limited(self):
        assert is_rate_limited(ProviderRateLimitedError(attempts=3)) is True


class TestIsRateLimitedFalse:
    def test_500_http_status_error_is_not_rate_limited(self):
        assert is_rate_limited(_http_status_error(500)) is False

    def test_runtime_error_is_not_rate_limited(self):
        assert is_rate_limited(RuntimeError("boom")) is False

    def test_value_error_is_not_rate_limited(self):
        assert is_rate_limited(ValueError("bad input")) is False

    def test_429_hidden_in_runtime_error_string_is_not_rate_limited(self):
        """A 429 stringified into a RuntimeError loses its structure — NOT classifiable.

        This is the precise reason providers must re-raise the httpx error intact
        instead of wrapping it in RuntimeError(str(exc)).
        """
        masked = RuntimeError(
            "Batch embedding request failed: 429 Too Many Requests "
            "for url 'https://api.voyageai.com/v1/embeddings'"
        )
        assert is_rate_limited(masked) is False


class TestGetHttpStatusError:
    def test_extracts_underlying_http_status_error(self):
        exc = _http_status_error(429, retry_after="3.0")
        extracted = get_http_status_error(exc)
        assert extracted is exc
        assert extracted is not None
        assert extracted.response.headers.get("retry-after") == "3.0"

    def test_returns_none_for_non_http_error(self):
        assert get_http_status_error(RuntimeError("boom")) is None

    def test_returns_none_for_provider_rate_limited_error_without_cause(self):
        assert get_http_status_error(ProviderRateLimitedError(attempts=2)) is None

    def test_extracts_http_error_from_provider_rate_limited_cause(self):
        """ProviderRateLimitedError raised `from` a 429 exposes the underlying error.

        execute_with_backoff raises ProviderRateLimitedError `from exc` where exc
        is the last httpx 429 — get_http_status_error should surface it so the
        Retry-After header remains reachable.
        """
        underlying = _http_status_error(429, retry_after="5.0")
        try:
            raise ProviderRateLimitedError(attempts=3) from underlying
        except ProviderRateLimitedError as wrapped:
            extracted = get_http_status_error(wrapped)
        assert extracted is underlying


class TestExecuteWithBackoffUsesClassifier:
    """execute_with_backoff must base its retry decision on is_rate_limited(exc).

    It retries iff the exception is a rate-limit signal (regardless of provider
    wrapping) and re-raises any non-rate-limited exception immediately.
    """

    def test_retries_classifiable_rate_limit_that_is_not_bare_http_error(self):
        """A ProviderRateLimitedError-from-429 is classifiable -> retried, then succeeds.

        Before the classifier refactor the loop only caught httpx.HTTPStatusError,
        so this normalized rate-limit signal would escape un-retried.
        """
        call_count = [0]

        def fn() -> str:
            call_count[0] += 1
            if call_count[0] < 2:
                underlying = _http_status_error(429, retry_after="1.0")
                raise ProviderRateLimitedError(attempts=1) from underlying
            return "ok"

        slept: List[float] = []
        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            result = execute_with_backoff(fn, max_retries=2)

        assert result == "ok"
        assert call_count[0] == 2
        assert len(slept) == 1

    def test_voyage_style_429_http_error_is_retried_and_succeeds(self):
        """A 429 propagating as httpx.HTTPStatusError is retried, then succeeds."""
        call_count = [0]

        def fn() -> str:
            call_count[0] += 1
            if call_count[0] < 3:
                raise _http_status_error(429)
            return "done"

        slept: List[float] = []
        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            result = execute_with_backoff(fn, max_retries=2)

        assert result == "done"
        assert call_count[0] == 3
        assert len(slept) == 2

    def test_non_rate_limited_500_reraised_immediately(self):
        """A non-429 HTTP error is not rate-limited -> re-raised immediately, no retry."""
        call_count = [0]

        def fn() -> str:
            call_count[0] += 1
            raise _http_status_error(500)

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            execute_with_backoff(fn, max_retries=2)

        assert exc_info.value.response.status_code == 500
        assert call_count[0] == 1, "Non-rate-limited error must not be retried"

    def test_exhaustion_raises_provider_rate_limited_error(self):
        """All attempts rate-limited -> ProviderRateLimitedError after max_retries+1."""
        call_count = [0]

        def fn() -> str:
            call_count[0] += 1
            raise _http_status_error(429)

        with patch("code_indexer.services.provider_backoff.time.sleep"):
            with pytest.raises(ProviderRateLimitedError) as exc_info:
                execute_with_backoff(fn, max_retries=2)

        assert call_count[0] == 3
        assert exc_info.value.attempts == 3

    def test_retry_after_header_honored_via_classifier_path(self):
        """Retry-After on a classifiable 429 still feeds _compute_sleep (header reachable)."""
        slept: List[float] = []

        def fn() -> str:
            raise _http_status_error(429, retry_after="2.0")

        with patch(
            "code_indexer.services.provider_backoff.time.sleep",
            side_effect=slept.append,
        ):
            with pytest.raises(ProviderRateLimitedError):
                execute_with_backoff(
                    fn, max_retries=2, per_attempt_cap=15.0, cumulative_cap=45.0
                )

        # Full jitter on top of Retry-After ceiling 2.0 -> every sleep <= 2.0.
        assert slept, "Expected at least one backoff sleep"
        for s in slept:
            assert s <= 2.0 + 0.01, f"Sleep {s} exceeded Retry-After ceiling"


class TestComputeSleepHelper:
    """_compute_sleep must tolerate a None httpx error (wrapped signal) and a
    malformed Retry-After header, always returning a non-negative bounded value."""

    def test_compute_sleep_with_none_exc_uses_default_base(self):
        """A wrapped signal with no reachable httpx error -> default no-header base."""
        from code_indexer.services.provider_backoff import (
            _DEFAULT_NO_HEADER_BASE,
            _compute_sleep,
        )

        for _ in range(20):
            s = _compute_sleep(None, per_attempt_cap=15.0)
            assert 0.0 <= s <= _DEFAULT_NO_HEADER_BASE

    def test_compute_sleep_malformed_retry_after_falls_back_to_cap(self):
        """A non-numeric Retry-After header falls back to per_attempt_cap ceiling."""
        from code_indexer.services.provider_backoff import _compute_sleep

        exc = _http_status_error(429, retry_after="not-a-number")
        for _ in range(20):
            s = _compute_sleep(exc, per_attempt_cap=4.0)
            assert 0.0 <= s <= 4.0 + 0.01


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
