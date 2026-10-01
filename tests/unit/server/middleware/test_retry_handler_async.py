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
from unittest.mock import AsyncMock, patch

import pytest

from code_indexer.server.middleware.retry_handler import DatabaseRetryHandler
from code_indexer.server.models.error_models import (
    RetryConfiguration,
    DatabaseRetryableError,
    DatabasePermanentError,
)


@pytest.fixture
def cleared_main_thread_event_loop():
    """Simulate the event-loop-policy state a huge test suite can leave
    behind (Bug #1700).

    In the FULL `tests/unit/server/` sweep (22000+ tests, ~2.5h), by the
    time this file's tests run, some earlier test elsewhere in the suite
    has called `asyncio.set_event_loop(None)` (a common pytest-asyncio
    -style teardown action). Once that happens in the MainThread,
    CPython's default event loop policy no longer auto-creates a loop on
    `asyncio.get_event_loop()` -- it raises `RuntimeError: There is no
    current event loop in thread 'MainThread'.` instead.

    Saves whatever loop is currently registered (if any), clears it, then
    restores the ORIGINAL loop afterward -- never creates or leaks a new
    one.
    """
    try:
        original_loop = asyncio.get_event_loop()
    except RuntimeError:
        original_loop = None
    asyncio.set_event_loop(None)
    try:
        yield
    finally:
        asyncio.set_event_loop(original_loop)


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
        sleep_calls = []  # type: ignore[var-annotated]

        async def failing_then_succeeding():
            attempt_count.append(1)
            if len(attempt_count) < 2:
                raise DatabaseRetryableError("transient error")
            return "success"

        async def run_test():
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await handler.async_execute_with_retry(failing_then_succeeding)
                sleep_calls.extend(mock_sleep.call_args_list)
            return result

        result = asyncio.run(run_test())

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

        asyncio.run(run_test())


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

        result = asyncio.run(run_test())
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

        result = asyncio.run(run_test())
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

        asyncio.run(run_test())
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

        asyncio.run(run_test())
        assert len(attempts) == make_config(max_attempts=2).max_attempts + 1


class TestAsyncExecuteWithRetryEventLoopPolicyResilience:
    """Regression guard (Bug #1700): retry-handler async tests must not
    depend on `asyncio.get_event_loop()` implicitly auto-creating a loop.

    See the `cleared_main_thread_event_loop` fixture docstring above for
    the full failure-mode rationale. This directly invokes an EXISTING
    test method above under the exact precondition a huge suite run
    leaves behind, so it genuinely discriminates the bug: it fails while
    that method still uses `asyncio.get_event_loop()`, and passes once
    that method is switched to `asyncio.run()`.
    """

    def test_survives_prior_set_event_loop_none(self, cleared_main_thread_event_loop):
        TestAsyncExecuteWithRetryUsesAsyncioSleep().test_uses_asyncio_sleep_not_time_sleep()


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
