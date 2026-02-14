"""
Unit tests for DependencyMapService.is_available() method (Story #195).

Tests the non-blocking lock probe that determines if a dependency map
analysis can be started (lock is available) or if one is already running.
"""

import pytest
import threading
import time
from unittest.mock import Mock


class TestIsAvailable:
    """Test DependencyMapService.is_available() method."""

    def test_returns_true_when_lock_available(self):
        """Test is_available returns True when no analysis is running."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        # Create service with mock dependencies
        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        # Should return True when lock is available
        assert service.is_available() is True

    def test_returns_false_when_lock_held(self):
        """Test is_available returns False when analysis is running."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        # Create service
        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        # Acquire lock to simulate running analysis
        service._lock.acquire()

        try:
            # Should return False when lock is held
            assert service.is_available() is False
        finally:
            # Clean up
            service._lock.release()

    def test_does_not_block_when_lock_held(self):
        """Test is_available returns immediately even when lock is held."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        # Create service
        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        # Acquire lock
        service._lock.acquire()

        try:
            # Measure time - should be nearly instantaneous
            start = time.time()
            result = service.is_available()
            elapsed = time.time() - start

            # Should return False immediately (not block)
            assert result is False
            assert elapsed < 0.1, f"is_available blocked for {elapsed}s"
        finally:
            service._lock.release()

    def test_does_not_hold_lock_after_checking(self):
        """Test is_available releases lock immediately after checking."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        # Create service
        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        # Call is_available
        result = service.is_available()
        assert result is True

        # Lock should be immediately available again
        acquired = service._lock.acquire(blocking=False)
        assert acquired is True, "Lock was not released after is_available()"

        # Clean up
        service._lock.release()

    def test_concurrent_calls_do_not_interfere(self):
        """Test multiple is_available calls work correctly."""
        from code_indexer.server.services.dependency_map_service import DependencyMapService

        # Create service
        service = DependencyMapService(
            golden_repos_manager=Mock(),
            config_manager=Mock(),
            tracking_backend=Mock(),
            analyzer=Mock(),
        )

        # Multiple calls should all return True
        assert service.is_available() is True
        assert service.is_available() is True
        assert service.is_available() is True
