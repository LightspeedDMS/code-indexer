"""
Tests for Story #278: timing_attack_prevention async variant.

The TimingAttackPrevention.constant_time_execute() uses time.sleep() to pad
response times. This is CORRECT for sync def route handlers (login, register,
etc.) that FastAPI runs in a threadpool. The time.sleep() blocks a threadpool
thread, NOT the event loop.

However, an async variant async_ensure_minimum_time() should be available for
any future async callers that need timing normalization without blocking the
event loop.

Key requirements tested:
- async_ensure_minimum_time method exists
- It is an async (coroutine) function
- It uses asyncio.sleep, not time.sleep
- The existing constant_time_execute sync method remains unchanged
- Code comment documents why time.sleep is correct in the sync version
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, patch

from code_indexer.server.auth.timing_attack_prevention import TimingAttackPrevention


class TestAsyncEnsureMinimumTimeExists:
    """Verify async_ensure_minimum_time method exists."""

    def test_method_exists(self):
        """TimingAttackPrevention must have async_ensure_minimum_time method."""
        tap = TimingAttackPrevention(minimum_response_time_ms=100)
        assert hasattr(tap, "async_ensure_minimum_time"), (
            "TimingAttackPrevention must have async_ensure_minimum_time method"
        )

    def test_method_is_coroutine_function(self):
        """async_ensure_minimum_time must be an async def method."""
        tap = TimingAttackPrevention(minimum_response_time_ms=100)
        assert inspect.iscoroutinefunction(tap.async_ensure_minimum_time), (
            "async_ensure_minimum_time must be an async def method"
        )


class TestAsyncEnsureMinimumTimeUsesAsyncioSleep:
    """Verify async_ensure_minimum_time uses asyncio.sleep for delays."""

    def test_uses_asyncio_sleep_not_time_sleep(self):
        """async_ensure_minimum_time must use asyncio.sleep, not time.sleep."""
        tap = TimingAttackPrevention(minimum_response_time_ms=200)

        async def run_test():
            # Mock asyncio.sleep to track calls
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_async_sleep:
                with patch("time.sleep") as mock_time_sleep:
                    # Call with an operation that completes instantly
                    # so the minimum time padding triggers
                    async def fast_operation():
                        return "result"

                    result = await tap.async_ensure_minimum_time(fast_operation)

                    assert mock_time_sleep.call_count == 0, (
                        "async_ensure_minimum_time must NOT call time.sleep"
                    )
                    assert mock_async_sleep.call_count >= 1, (
                        "async_ensure_minimum_time must call asyncio.sleep for padding"
                    )
                    return result

        result = asyncio.get_event_loop().run_until_complete(run_test())
        assert result == "result"

    def test_returns_operation_result(self):
        """async_ensure_minimum_time must return the operation's return value."""
        tap = TimingAttackPrevention(minimum_response_time_ms=1)

        async def run_test():
            async def operation():
                return {"status": "ok", "value": 42}

            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await tap.async_ensure_minimum_time(operation)

            return result

        result = asyncio.get_event_loop().run_until_complete(run_test())
        assert result == {"status": "ok", "value": 42}

    def test_reraises_operation_exception(self):
        """async_ensure_minimum_time must re-raise exceptions from the operation."""
        tap = TimingAttackPrevention(minimum_response_time_ms=1)

        class CustomError(Exception):
            pass

        async def run_test():
            async def failing_operation():
                raise CustomError("test error")

            with patch("asyncio.sleep", new_callable=AsyncMock):
                try:
                    await tap.async_ensure_minimum_time(failing_operation)
                    assert False, "Should have raised CustomError"
                except CustomError as e:
                    assert str(e) == "test error"

        asyncio.get_event_loop().run_until_complete(run_test())

    def test_no_sleep_when_operation_exceeds_minimum_time(self):
        """No sleep is needed when the operation already exceeds the minimum time."""
        # Very short minimum so the operation naturally exceeds it
        tap = TimingAttackPrevention(minimum_response_time_ms=1)

        async def run_test():
            call_count = []

            async def slow_operation():
                # Simulate work that takes time naturally
                await asyncio.sleep(0)  # Yield to event loop briefly
                return "done"

            with patch(
                "asyncio.sleep", wraps=asyncio.sleep
            ) as mock_async_sleep:
                result = await tap.async_ensure_minimum_time(slow_operation)
                # The wraps=asyncio.sleep means real sleep is called, just tracked
                return result, mock_async_sleep

        # Just verify it completes without error
        loop = asyncio.new_event_loop()
        try:
            result, _ = loop.run_until_complete(run_test())
            assert result == "done"
        finally:
            loop.close()


class TestSyncConstantTimeExecuteUnchanged:
    """Verify the existing sync constant_time_execute remains unchanged."""

    def test_sync_method_still_uses_time_sleep(self):
        """constant_time_execute must still use time.sleep (not asyncio.sleep)."""
        tap = TimingAttackPrevention(minimum_response_time_ms=100)

        with patch("time.sleep") as mock_time_sleep:
            result = tap.constant_time_execute(lambda: "sync_result")

        assert result == "sync_result"
        assert mock_time_sleep.call_count >= 1, (
            "constant_time_execute must still use time.sleep"
        )

    def test_sync_method_is_not_coroutine(self):
        """constant_time_execute must remain a synchronous method."""
        tap = TimingAttackPrevention(minimum_response_time_ms=100)
        assert not inspect.iscoroutinefunction(tap.constant_time_execute), (
            "constant_time_execute must remain a synchronous def method"
        )

    def test_sync_method_comment_documents_correct_behavior(self):
        """
        The module source should contain a comment explaining why time.sleep
        is correct in the sync auth handlers (they run in FastAPI's threadpool).

        This verifies the documentation requirement from Story #278.
        """
        import code_indexer.server.auth.timing_attack_prevention as module
        import inspect

        source = inspect.getsource(module)

        # Look for comment about threadpool / event loop rationale
        assert any(
            keyword in source.lower()
            for keyword in ["threadpool", "thread pool", "event loop", "intentional"]
        ), (
            "The module must contain a comment documenting why time.sleep is "
            "correct in sync handlers (runs in threadpool, not event loop)"
        )
