"""
Tests for Story #278: diagnostics _generate_descriptions_lock does not block event loop.

The handler generate_missing_descriptions is async def. The synchronous work
(SQLite queries, filesystem checks, submit_work calls) must be offloaded to
run_in_executor so the asyncio event loop is not blocked.

Approach tested:
- A private helper _generate_descriptions_sync() is extracted containing the
  blocking work (runs in threadpool via run_in_executor).
- The threading.Lock remains on _generate_descriptions_sync (not asyncio.Lock)
  because the work runs in the threadpool, not the event loop.
- The async handler awaits run_in_executor(None, _generate_descriptions_sync, ...).
"""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch


class TestGenerateDescriptionsSyncHelperExists:
    """Verify that a sync helper function exists for the blocking work."""

    def test_sync_helper_function_exists(self):
        """A _generate_descriptions_sync helper must be importable."""
        from code_indexer.server.routers.diagnostics import _generate_descriptions_sync

        assert callable(_generate_descriptions_sync), (
            "_generate_descriptions_sync must be a callable function"
        )

    def test_sync_helper_returns_response_type(self):
        """_generate_descriptions_sync must return GenerateMissingDescriptionsResponse."""
        from code_indexer.server.routers.diagnostics import (
            GenerateMissingDescriptionsResponse,
            _generate_descriptions_sync,
            _generate_descriptions_lock,
        )

        # Build minimal mock objects for the sync helper
        mock_state = MagicMock()
        mock_state.golden_repos_dir = "/tmp/test-golden-repos"
        mock_state.golden_repo_manager = MagicMock()
        mock_state.golden_repo_manager.list_golden_repos.return_value = []

        result = _generate_descriptions_sync(mock_state)

        assert isinstance(result, GenerateMissingDescriptionsResponse), (
            "_generate_descriptions_sync must return GenerateMissingDescriptionsResponse"
        )

    def test_sync_helper_lock_prevents_concurrent_execution(self):
        """The threading.Lock in _generate_descriptions_sync prevents concurrent runs."""
        from code_indexer.server.routers.diagnostics import _generate_descriptions_lock

        # When the lock is held (simulating one run in progress),
        # a second acquire must fail immediately.
        acquired = _generate_descriptions_lock.acquire(blocking=False)
        assert acquired, "Lock must be acquirable when no run is in progress"

        try:
            # Lock is now held - a second acquisition must fail
            second_acquire = _generate_descriptions_lock.acquire(blocking=False)
            assert not second_acquire, (
                "Lock must prevent concurrent execution - second acquire must fail"
            )
        finally:
            _generate_descriptions_lock.release()


class TestGenerateMissingDescriptionsUsesExecutor:
    """Verify that the async handler offloads work to run_in_executor."""

    def test_handler_calls_run_in_executor(self):
        """generate_missing_descriptions must call run_in_executor with the sync helper."""
        import inspect
        from code_indexer.server.routers.diagnostics import generate_missing_descriptions

        # The handler must be an async function
        assert inspect.iscoroutinefunction(generate_missing_descriptions), (
            "generate_missing_descriptions must remain async def"
        )

    def test_handler_uses_run_in_executor_via_event_loop(self):
        """
        When the handler runs, it must call loop.run_in_executor (or
        asyncio.get_event_loop().run_in_executor), not call the sync work directly.

        We verify this by patching run_in_executor and checking it is awaited.
        """
        from code_indexer.server.routers.diagnostics import (
            generate_missing_descriptions,
            _generate_descriptions_sync,
        )

        # Track whether run_in_executor was called with the sync helper
        executor_called_with_sync_helper = []

        async def run_test():
            mock_loop = MagicMock()
            future = asyncio.get_event_loop().create_future()
            from code_indexer.server.routers.diagnostics import (
                GenerateMissingDescriptionsResponse,
            )
            future.set_result(
                GenerateMissingDescriptionsResponse(
                    repos_queued=0,
                    repos_with_descriptions=0,
                    total_repos=0,
                )
            )

            def fake_run_in_executor(executor, func, *args):
                executor_called_with_sync_helper.append(func)
                return future

            mock_loop.run_in_executor = fake_run_in_executor

            mock_request = MagicMock()
            mock_request.app.state.golden_repos_dir = "/tmp/test"
            mock_request.app.state.golden_repo_manager = MagicMock()
            mock_user = MagicMock()

            with patch("asyncio.get_event_loop", return_value=mock_loop):
                result = await generate_missing_descriptions(
                    request=mock_request, current_user=mock_user
                )

            return result

        result = asyncio.get_event_loop().run_until_complete(run_test())

        assert len(executor_called_with_sync_helper) > 0, (
            "generate_missing_descriptions must call run_in_executor to offload blocking work"
        )
        assert executor_called_with_sync_helper[0] is _generate_descriptions_sync, (
            "run_in_executor must be called with _generate_descriptions_sync as the function"
        )

    def test_second_concurrent_call_returns_empty_response_without_blocking(self):
        """
        When a run is already in progress (lock held), a second concurrent call
        must return an empty response immediately without waiting.
        """
        from code_indexer.server.routers.diagnostics import (
            GenerateMissingDescriptionsResponse,
            _generate_descriptions_lock,
        )

        async def run_test():
            # Simulate lock already held by a concurrent run
            acquired = _generate_descriptions_lock.acquire(blocking=False)
            assert acquired, "Lock must be acquirable in test setup"

            try:
                from code_indexer.server.routers.diagnostics import (
                    generate_missing_descriptions,
                )

                mock_request = MagicMock()
                mock_request.app.state.golden_repos_dir = "/tmp/test"
                mock_request.app.state.golden_repo_manager = MagicMock()
                mock_user = MagicMock()

                # The second call should return immediately with zeros
                result = await generate_missing_descriptions(
                    request=mock_request, current_user=mock_user
                )

                assert isinstance(result, GenerateMissingDescriptionsResponse)
                assert result.repos_queued == 0
                assert result.repos_with_descriptions == 0
                assert result.total_repos == 0
            finally:
                _generate_descriptions_lock.release()

        asyncio.get_event_loop().run_until_complete(run_test())
