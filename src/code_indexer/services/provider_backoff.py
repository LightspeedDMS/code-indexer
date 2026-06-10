"""Unified bounded 429 backoff for embedding and rerank providers (Bug #1078 Phase 1).

``execute_with_backoff`` retries the wrapped callable on HTTP 429 responses with
full jitter, per-attempt sleep cap, and a hard cumulative sleep budget that stays
well within the 60-second caller timeout.

The sleep happens OUTSIDE the governor slot so each retry re-acquires a slot:

    execute_with_backoff(
        lambda: governor.execute(budget, do_http, acquire_timeout=...),
        health_key="voyage-ai",
    )

This means:
  - Semaphore is not held during the backoff sleep.
  - Other callers can use the freed slot while this caller waits.
  - Each retry attempt goes through the sinbin pre-check again.
"""

import logging
import random
import time
from typing import Callable, Optional, TypeVar

import httpx

logger = logging.getLogger(__name__)

# Default retry / timing constants (overridable per call-site via kwargs).
_DEFAULT_MAX_RETRIES: int = 2  # 3 total attempts
_DEFAULT_PER_ATTEMPT_CAP: float = 15.0  # seconds; cap per individual sleep
_DEFAULT_CUMULATIVE_CAP: float = (
    45.0  # seconds; total sleep budget (< 60s caller timeout)
)

# Base sleep duration used when the provider returns no Retry-After header.
# 1 second gives the provider a short breathing room before we retry; full
# jitter is applied so actual sleep is uniform in [0, 1.0].
_DEFAULT_NO_HEADER_BASE: float = 1.0

T = TypeVar("T")


class ProviderRateLimitedError(RuntimeError):
    """Raised when all retry attempts have been exhausted due to HTTP 429 responses.

    Attributes:
        attempts: Total number of call attempts made before giving up.
        last_status_code: HTTP status code of the last response (always 429 here).
    """

    def __init__(self, attempts: int, last_status_code: int = 429) -> None:
        self.attempts = attempts
        self.last_status_code = last_status_code
        super().__init__(
            f"Provider rate-limited after {attempts} attempt(s) "
            f"(HTTP {last_status_code})"
        )


def get_http_status_error(exc: BaseException) -> Optional[httpx.HTTPStatusError]:
    """Return the underlying ``httpx.HTTPStatusError`` for an exception, else None.

    Canonical extraction so ``_compute_sleep`` (and any AIMD signal consumer)
    can reach the original HTTP response headers (e.g. Retry-After) regardless
    of how the rate-limit signal was raised:

      - The exception itself is an ``httpx.HTTPStatusError`` -> return it.
      - A ``ProviderRateLimitedError`` chained ``from`` a 429 (``__cause__``) ->
        return the chained ``httpx.HTTPStatusError``.
      - Anything else -> None.

    Pure and provider-agnostic.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, httpx.HTTPStatusError):
        return cause
    return None


def is_rate_limited(exc: BaseException) -> bool:
    """Return True iff ``exc`` is a structurally-intact rate-limit signal.

    Canonical, provider-agnostic 429 classifier (Story #1079 Phase A). Returns
    True for:
      - an ``httpx.HTTPStatusError`` whose ``response.status_code == 429``, OR
      - a ``ProviderRateLimitedError`` (already-normalized exhaustion signal), OR
      - a ``ProviderRateLimitedError`` chained ``from`` a 429.

    Returns False for any other exception — including a 429 that a provider
    stringified into a generic ``RuntimeError(str(exc))``. Once the status code
    is lost to string formatting it is unrecoverable; that is precisely why
    providers MUST re-raise the ``httpx.HTTPStatusError`` intact rather than
    wrapping it.

    Pure: no side effects, no I/O.
    """
    if isinstance(exc, ProviderRateLimitedError):
        return True
    http_error = get_http_status_error(exc)
    if http_error is not None:
        return bool(http_error.response.status_code == 429)
    return False


def execute_with_backoff(
    fn: Callable[[], T],
    *,
    health_key: Optional[str] = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    per_attempt_cap: float = _DEFAULT_PER_ATTEMPT_CAP,
    cumulative_cap: float = _DEFAULT_CUMULATIVE_CAP,
) -> T:
    """Execute fn with bounded retries on rate-limit (429) signals.

    The retry decision is made by the canonical ``is_rate_limited`` classifier,
    so a 429 is retried regardless of how a provider wrapped it (bare
    ``httpx.HTTPStatusError`` 429, ``ProviderRateLimitedError``, or a
    ``ProviderRateLimitedError`` chained ``from`` a 429). A 429 that a provider
    stringified into a generic ``RuntimeError`` is NOT classifiable and will
    propagate — which is why providers must re-raise the HTTP error intact
    (Story #1079 Phase A).

    Behavior:
    - Up to (max_retries + 1) total attempts.
    - On a rate-limit signal: extract the underlying httpx error (if any) and
      parse its Retry-After header; clamp to per_attempt_cap; apply full jitter
      (uniform in [0, clamp]); sleep; retry.
    - Before each sleep, check whether the cumulative sleep budget would be
      exceeded; if so, raise ProviderRateLimitedError immediately (fail-fast).
    - Non-rate-limited errors (is_rate_limited(exc) is False) are re-raised
      immediately — NOT retried.
    - On exhaustion of all retries, raise ProviderRateLimitedError.

    Args:
        fn: Zero-argument callable to execute. Typically a lambda that calls
            ``governor.execute(budget, do_http, acquire_timeout=...)``.
        health_key: Optional ProviderHealthMonitor key for future observability
            hooks (currently unused in Phase 1; kept for Phase 2 wiring).
        max_retries: Number of additional attempts after the first failure.
            Default 2 -> 3 total attempts.
        per_attempt_cap: Maximum seconds to sleep between any two attempts.
            Default 15.0 s.
        cumulative_cap: Maximum total seconds slept across all retries.
            Default 45.0 s (safely below the 60 s caller timeout).

    Returns:
        Whatever fn() returns on success.

    Raises:
        ProviderRateLimitedError: All attempts were rate-limited, or the
            cumulative sleep budget would be exceeded before a retry attempt.
        Any non-rate-limited exception from fn(): re-raised immediately.
    """
    total_attempts = max_retries + 1
    cumulative_slept: float = 0.0
    last_exc: Optional[BaseException] = None

    for attempt in range(total_attempts):
        try:
            return fn()
        except Exception as exc:
            # Canonical classification: retry iff this is a rate-limit signal,
            # regardless of provider wrapping. Anything else propagates intact.
            if not is_rate_limited(exc):
                raise
            last_exc = exc

            # This was the last allowed attempt
            if attempt >= total_attempts - 1:
                raise ProviderRateLimitedError(
                    attempts=total_attempts,
                    last_status_code=429,
                ) from exc

            # Determine sleep duration. Pull the underlying httpx 429 (if any)
            # so Retry-After remains reachable through wrapped signals.
            sleep_duration = _compute_sleep(get_http_status_error(exc), per_attempt_cap)

            # Fail-fast if cumulative budget would be exceeded
            if cumulative_slept + sleep_duration > cumulative_cap:
                logger.warning(
                    "execute_with_backoff: cumulative sleep budget (%.1fs) would be "
                    "exceeded (already slept %.1fs, next sleep %.1fs); "
                    "failing fast after %d attempt(s)",
                    cumulative_cap,
                    cumulative_slept,
                    sleep_duration,
                    attempt + 1,
                )
                raise ProviderRateLimitedError(
                    attempts=attempt + 1,
                    last_status_code=429,
                ) from exc

            logger.debug(
                "execute_with_backoff: HTTP 429 on attempt %d/%d; sleeping %.2fs "
                "(cumulative %.2fs / %.1fs budget)",
                attempt + 1,
                total_attempts,
                sleep_duration,
                cumulative_slept + sleep_duration,
                cumulative_cap,
            )
            time.sleep(sleep_duration)
            cumulative_slept += sleep_duration

    # Should be unreachable — loop covers all attempts
    if last_exc is not None:
        raise ProviderRateLimitedError(
            attempts=total_attempts,
            last_status_code=429,
        ) from last_exc
    raise ProviderRateLimitedError(attempts=total_attempts)  # pragma: no cover


def _compute_sleep(
    exc: Optional[httpx.HTTPStatusError], per_attempt_cap: float
) -> float:
    """Compute the sleep duration for a rate-limit retry with full jitter.

    Algorithm:
      1. Read Retry-After header (if an httpx response is available and numeric).
      2. Clamp to per_attempt_cap: ceiling = min(retry_after_or_default, per_attempt_cap).
      3. Full jitter: uniform in [0, ceiling].

    This keeps sleep strictly within per_attempt_cap while honoring server hints.

    Args:
        exc: The underlying ``httpx.HTTPStatusError`` whose response carries the
            Retry-After header, or None when the rate-limit signal was wrapped
            in a way that lost the HTTP response (e.g. a normalized error). When
            None, the default no-header base is used for the jitter ceiling.
    """
    retry_after_header = (
        exc.response.headers.get("retry-after") if exc is not None else None
    )
    if retry_after_header is not None:
        try:
            base = float(retry_after_header)
        except (ValueError, TypeError):
            logger.debug(
                "execute_with_backoff: malformed Retry-After header %r — "
                "using per_attempt_cap %.1fs as base",
                retry_after_header,
                per_attempt_cap,
            )
            base = per_attempt_cap
    else:
        # No Retry-After header — use a modest base for jitter ceiling
        base = _DEFAULT_NO_HEADER_BASE

    ceiling = min(base, per_attempt_cap)
    return random.uniform(0.0, ceiling)
