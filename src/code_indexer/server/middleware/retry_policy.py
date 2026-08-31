"""
Retry policy primitives for CIDX Server database retry handling.

Pure error-classification and backoff-computation functions, plus the
shared per-attempt retry decision (should this error be retried, how long
to delay, and the warning log for that delay). All extracted from
DatabaseRetryHandler (retry_handler.py) so that module's two orchestration
loops (execute_with_retry / async_execute_with_retry) stay thin, byte-
identical wrappers over this one shared implementation instead of
duplicating it (Bug #1568).

Bug #1468 constraint: this module MUST NOT import fastapi/starlette at
module level, directly or transitively. get_correlation_id() is reached
through this module by both retry loops on a hot import path; verified
here via code_indexer.server.middleware.correlation (TYPE_CHECKING-guarded
fastapi import only) and code_indexer.server.logging_utils (no fastapi at
all).

Follows CLAUDE.md Foundation #1: No mocks - real retry policy logic.
"""

import logging
import random

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log

from ..models.error_models import (
    RetryConfiguration,
    DatabaseRetryableError,
    DatabasePermanentError,
)

logger = logging.getLogger(__name__)

# Known transient database error message patterns that should be retried
TRANSIENT_ERROR_PATTERNS = [
    "connection timeout",
    "connection refused",
    "connection reset",
    "connection pool exhausted",
    "temporary failure",
    "deadlock detected",
    "lock wait timeout",
    "server shutdown",
    "too many connections",
    "connection lost",
]


def should_retry_error(
    error: Exception, attempt: int, config: RetryConfiguration
) -> bool:
    """
    Determine if an error should be retried.

    Args:
        error: The exception that occurred
        attempt: Current attempt number (1-indexed)
        config: Retry configuration (max_attempts is consulted)

    Returns:
        True if the error should be retried, False otherwise
    """
    if attempt > config.max_attempts:
        return False

    # DatabaseRetryableError should be retried
    if isinstance(error, DatabaseRetryableError):
        return True

    # DatabasePermanentError should NOT be retried
    if isinstance(error, DatabasePermanentError):
        return False

    # Check for known transient database errors by message patterns
    error_message = str(error).lower()
    return any(pattern in error_message for pattern in TRANSIENT_ERROR_PATTERNS)


def calculate_retry_delay(attempt: int, config: RetryConfiguration) -> float:
    """
    Calculate delay for retry attempt using exponential backoff with jitter.

    Args:
        attempt: Attempt number (1-indexed)
        config: Retry configuration (base delay, multiplier, max delay, jitter)

    Returns:
        Delay in seconds
    """
    # Exponential backoff: base_delay * (multiplier ^ (attempt - 1))
    base_delay = config.base_delay_seconds * (
        config.backoff_multiplier ** (attempt - 1)
    )

    # Apply maximum delay limit
    delay = min(base_delay, config.max_delay_seconds)

    # Add jitter to prevent thundering herd
    if config.jitter_factor > 0:
        jitter = delay * config.jitter_factor * random.random()
        delay += jitter

    return delay


def log_retry_attempt(attempt: int, delay: float, error: Exception) -> None:
    """Log a warning for a database operation attempt that will be retried."""
    logger.warning(
        format_error_log(
            "REPO-GENERAL-020",
            f"Database operation failed on attempt {attempt}, retrying in {delay:.2f}s: {error}",
            extra={"correlation_id": get_correlation_id()},
        )
    )


def decide_retry_delay(
    error: Exception, attempt: int, config: RetryConfiguration
) -> float:
    """
    Handle a caught exception for one retry attempt.

    Shared decision logic for both execute_with_retry() and
    async_execute_with_retry(). If should_retry_error() says this
    error/attempt combination should not be retried, the original
    exception is re-raised unchanged. Otherwise the delay for the next
    attempt is computed, a warning is logged, and the delay (in seconds)
    is returned for the caller to sleep.

    Args:
        error: The exception that occurred
        attempt: Current attempt number (1-indexed)
        config: Retry configuration

    Returns:
        Delay in seconds to sleep before retrying.

    Raises:
        The original exception, unchanged, if it should not be retried.
    """
    if not should_retry_error(error, attempt, config):
        # Don't retry permanent errors or if max attempts exceeded
        raise error

    delay = calculate_retry_delay(attempt, config)
    log_retry_attempt(attempt, delay, error)
    return delay
