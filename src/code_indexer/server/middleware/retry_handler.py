"""
Database retry handler middleware for CIDX Server.

Handles database operation retries with exponential backoff following
CLAUDE.md Foundation #1: No mocks - real retry logic with actual timing.
"""

import asyncio
import time
from typing import Callable, TypeVar

from ..models.error_models import RetryConfiguration
from .retry_policy import (
    should_retry_error,
    calculate_retry_delay,
    decide_retry_delay,
)

# Type variable for retry functions
T = TypeVar("T")


class DatabaseRetryHandler:
    """
    Handles database operation retries with exponential backoff.

    Follows CLAUDE.md Foundation #1: No mocks - real retry logic with actual timing.
    Implements sophisticated retry patterns for different error types.
    """

    def __init__(self, config: RetryConfiguration):
        """Initialize retry handler with configuration."""
        self.config = config

    def should_retry(self, error: Exception, attempt: int) -> bool:
        """
        Determine if an error should be retried.

        Args:
            error: The exception that occurred
            attempt: Current attempt number (1-indexed)

        Returns:
            True if the error should be retried, False otherwise
        """
        return should_retry_error(error, attempt, self.config)

    def calculate_delay(self, attempt: int) -> float:
        """
        Calculate delay for retry attempt using exponential backoff with jitter.

        Args:
            attempt: Attempt number (1-indexed)

        Returns:
            Delay in seconds
        """
        return calculate_retry_delay(attempt, self.config)

    def execute_with_retry(self, operation: Callable[[], T]) -> T:
        """
        Execute database operation with retry logic.

        Args:
            operation: Function to execute that may raise database errors

        Returns:
            Result of the operation

        Raises:
            The final exception if all retries are exhausted
        """
        last_exception = None

        for attempt in range(1, self.config.max_attempts + 2):  # +1 for initial attempt
            try:
                return operation()
            except Exception as e:
                last_exception = e
                delay = decide_retry_delay(e, attempt, self.config)
                time.sleep(delay)

        # This should not be reached, but provide fallback
        raise last_exception or Exception("Retry logic error")

    async def async_execute_with_retry(self, operation: Callable[[], T]) -> T:
        """
        Execute async database operation with retry logic using asyncio.sleep.

        This is the async-compatible variant of execute_with_retry(). Use this
        from async contexts (e.g., async middleware) to avoid blocking a
        threadpool thread during retry delays.

        The operation can be either a coroutine function or a regular function.
        Retry delays use await asyncio.sleep() instead of time.sleep() so the
        event loop remains responsive during backoff waits.

        Args:
            operation: Async or sync function to execute that may raise database errors

        Returns:
            Result of the operation

        Raises:
            The final exception if all retries are exhausted
        """
        last_exception = None

        for attempt in range(1, self.config.max_attempts + 2):  # +1 for initial attempt
            try:
                result = operation()
                # Support both coroutine functions and regular functions
                if asyncio.iscoroutine(result):
                    return await result  # type: ignore[no-any-return]
                return result
            except Exception as e:
                last_exception = e
                delay = decide_retry_delay(e, attempt, self.config)
                await asyncio.sleep(delay)

        # This should not be reached, but provide fallback
        raise last_exception or Exception("Retry logic error")
