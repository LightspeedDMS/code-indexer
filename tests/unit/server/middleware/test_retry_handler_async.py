"""
Tests for Story #278: retry_handler async_execute_with_retry uses asyncio.sleep.

The DatabaseRetryHandler.execute_with_retry() uses time.sleep(). For callers
in async contexts (e.g., GlobalErrorHandler middleware), an async-compatible
variant async_execute_with_retry() must be available that uses asyncio.sleep()
instead of time.sleep() to avoid blocking the event loop or threadpool threads.

Key requirements tested:
- async_execute_with_retry method exists on DatabaseRetryHandler
- It uses asyncio.sleep instead of time.sleep for delays
- Retry count and delay calculation are preserved (same logic as sync version)
- Sync execute_with_retry is unchanged (existing callers must still work)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from code_indexer.server.middleware.retry_handler import DatabaseRetryHandler
from code_indexer.server.models.error_models import (
    RetryConfiguration,
    DatabaseRetryableError,
    DatabasePermanentError,
)


def make_config(max_attempts=3, base_delay=0.1, max_delay=1.0):
    """Build a RetryConfiguration for tests."""
    return RetryConfiguration(
        max_attempts=max_attempts,
        base_delay_seconds=base_delay,
        max_delay_seconds=max_delay,
        backoff_multiplier=2.0,
        jitter_factor=0.0,  # No jitter for deterministic tests
    )


class TestAsyncExecuteWithRetryExists:
    """Verify async_execute_with_retry method exists."""

    def test_async_execute_with_retry_method_exists(self):
        """DatabaseRetryHandler must have an async_execute_with_retry method."""
        handler = DatabaseRetryHandler(make_config())
        assert hasattr(handler, "async_execute_with_retry"), (
            "DatabaseRetryHandler must have async_execute_with_retry method"
        )

    def test_async_execute_with_retry_is_coroutine_function(self):
        """async_execute_with_retry must be an async (coroutine) method."""
        import inspect

        handler = DatabaseRetryHandler(make_config())
        assert inspect.iscoroutinefunction(handler.async_execute_with_retry), (
            "async_execute_with_retry must be an async def method"
        )


class TestAsyncExecuteWithRetryUsesAsyncioSleep:
    """Verify async_execute_with_retry uses asyncio.sleep for delays."""

    def test_uses_asyncio_sleep_not_time_sleep(self):
        """async_execute_with_retry must use asyncio.sleep, not time.sleep."""
        handler = DatabaseRetryHandler(make_config(max_attempts=2, base_delay=0.01))

        attempt_count = []
        sleep_calls = []

        async def failing_then_succeeding():
            attempt_count.append(1)
            if len(attempt_count) < 2:
                raise DatabaseRetryableError("transient error")
            return "success"

        async def run_test():
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await handler.async_execute_with_retry(
                    failing_then_succeeding
                )
                sleep_calls.extend(mock_sleep.call_args_list)
            return result

        result = asyncio.get_event_loop().run_until_complete(run_test())

        assert result == "success", "Should return success after retry"
        assert len(sleep_calls) == 1, (
            "asyncio.sleep must be called once between retry attempts"
        )

    def test_does_not_use_time_sleep(self):
        """async_execute_with_retry must NOT call time.sleep."""
        handler = DatabaseRetryHandler(make_config(max_attempts=2, base_delay=0.01))

        attempt_count = []

        async def failing_then_succeeding():
            attempt_count.append(1)
            if len(attempt_count) < 2:
                raise DatabaseRetryableError("transient error")
            return "ok"

        async def run_test():
            with patch("time.sleep") as mock_time_sleep:
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await handler.async_execute_with_retry(failing_then_succeeding)
                    assert mock_time_sleep.call_count == 0, (
                        "async_execute_with_retry must NOT call time.sleep"
                    )

        asyncio.get_event_loop().run_until_complete(run_test())


class TestAsyncExecuteWithRetryPreservesRetryLogic:
    """Verify retry logic is identical to sync version."""

    def test_succeeds_on_first_attempt(self):
        """Returns result immediately on first success."""
        handler = DatabaseRetryHandler(make_config())

        async def always_succeeds():
            return 42

        async def run_test():
            with patch("asyncio.sleep", new_callable=AsyncMock):
                return await handler.async_execute_with_retry(always_succeeds)

        result = asyncio.get_event_loop().run_until_complete(run_test())
        assert result == 42

    def test_retries_on_retryable_error(self):
        """Retries when DatabaseRetryableError is raised."""
        handler = DatabaseRetryHandler(make_config(max_attempts=3))

        attempts = []

        async def fail_twice_then_succeed():
            attempts.append(1)
            if len(attempts) < 3:
                raise DatabaseRetryableError("temporary failure")
            return "done"

        async def run_test():
            with patch("asyncio.sleep", new_callable=AsyncMock):
                return await handler.async_execute_with_retry(fail_twice_then_succeed)

        result = asyncio.get_event_loop().run_until_complete(run_test())
        assert result == "done"
        assert len(attempts) == 3

    def test_raises_immediately_on_permanent_error(self):
        """Does not retry on DatabasePermanentError."""
        handler = DatabaseRetryHandler(make_config(max_attempts=3))

        attempts = []

        async def always_permanent_failure():
            attempts.append(1)
            raise DatabasePermanentError("permanent failure")

        async def run_test():
            with patch("asyncio.sleep", new_callable=AsyncMock):
                try:
                    await handler.async_execute_with_retry(always_permanent_failure)
                    assert False, "Should have raised"
                except DatabasePermanentError:
                    pass

        asyncio.get_event_loop().run_until_complete(run_test())
        assert len(attempts) == 1, "Must NOT retry on permanent error"

    def test_exhausts_max_attempts_and_raises(self):
        """Raises after max_attempts are exhausted."""
        handler = DatabaseRetryHandler(make_config(max_attempts=2))

        attempts = []

        async def always_fails():
            attempts.append(1)
            raise DatabaseRetryableError("always fails")

        async def run_test():
            with patch("asyncio.sleep", new_callable=AsyncMock):
                try:
                    await handler.async_execute_with_retry(always_fails)
                    assert False, "Should have raised"
                except DatabaseRetryableError:
                    pass

        asyncio.get_event_loop().run_until_complete(run_test())
        assert len(attempts) == make_config(max_attempts=2).max_attempts + 1


class TestSyncExecuteWithRetryUnchanged:
    """Verify the existing sync execute_with_retry still works correctly."""

    def test_sync_version_still_uses_time_sleep(self):
        """Sync execute_with_retry must still call time.sleep (not asyncio.sleep)."""
        handler = DatabaseRetryHandler(make_config(max_attempts=2, base_delay=0.01))

        attempts = []

        def fail_then_succeed():
            attempts.append(1)
            if len(attempts) < 2:
                raise DatabaseRetryableError("transient")
            return "sync_result"

        with patch("time.sleep") as mock_time_sleep:
            result = handler.execute_with_retry(fail_then_succeed)

        assert result == "sync_result"
        assert mock_time_sleep.call_count == 1, (
            "Sync execute_with_retry must still use time.sleep"
        )

    def test_sync_version_interface_unchanged(self):
        """Sync execute_with_retry signature is unchanged."""
        import inspect

        handler = DatabaseRetryHandler(make_config())
        assert not inspect.iscoroutinefunction(handler.execute_with_retry), (
            "execute_with_retry must remain a synchronous method"
        )
