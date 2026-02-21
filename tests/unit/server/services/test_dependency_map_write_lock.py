"""
Unit tests for write lock acquire/release behavior in DependencyMapService.

Verifies that:
1. When acquire_write_lock returns True, release_write_lock IS called in finally
2. When acquire_write_lock returns False, release_write_lock is NOT called
3. Both run_full_analysis() and run_delta_analysis() paths are covered

Story #227: write-lock coordination with RefreshScheduler.
"""
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(refresh_scheduler=None):
    """Create a DependencyMapService with minimal mocked dependencies."""
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = "/tmp/golden-repos"

    config_manager = MagicMock()
    tracking_backend = MagicMock()
    analyzer = MagicMock()

    service = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        refresh_scheduler=refresh_scheduler,
    )
    return service, config_manager, tracking_backend, golden_repos_manager


def _make_disabled_config():
    """Return a config mock where dependency_map_enabled is False."""
    config = MagicMock()
    config.dependency_map_enabled = False
    return config


class TestFullAnalysisWriteLock:
    """Tests for write lock behavior in run_full_analysis()."""

    def test_releases_lock_when_acquire_returns_true(self):
        """When acquire_write_lock returns True, release_write_lock must be called."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # Make _setup_analysis return early (disabled) to avoid complex mock setup
        config_manager.get_claude_integration_config.return_value = _make_disabled_config()

        service.run_full_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_does_not_release_lock_when_acquire_returns_false(self):
        """When acquire_write_lock returns False, release_write_lock must NOT be called."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # Make _setup_analysis return early (disabled) to avoid complex mock setup
        config_manager.get_claude_integration_config.return_value = _make_disabled_config()

        service.run_full_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_not_called()

    def test_releases_lock_on_exception_when_acquire_returned_true(self):
        """release_write_lock is still called in finally even when an exception occurs."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # Make _setup_analysis raise to simulate an analysis failure
        config_manager.get_claude_integration_config.side_effect = RuntimeError("config error")

        with pytest.raises(RuntimeError, match="config error"):
            service.run_full_analysis()

        # Lock must have been acquired and then released in finally
        refresh_scheduler.acquire_write_lock.assert_called_once()
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_does_not_release_lock_on_exception_when_acquire_returned_false(self):
        """release_write_lock is NOT called even on exception when acquire returned False."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        config_manager.get_claude_integration_config.side_effect = RuntimeError("config error")

        with pytest.raises(RuntimeError, match="config error"):
            service.run_full_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once()
        refresh_scheduler.release_write_lock.assert_not_called()

    def test_no_acquire_or_release_when_refresh_scheduler_is_none(self):
        """When no refresh_scheduler, neither acquire nor release is attempted."""
        service, config_manager, _, _ = _make_service(refresh_scheduler=None)
        config_manager.get_claude_integration_config.return_value = _make_disabled_config()

        # Should not raise and should not call any scheduler method
        service.run_full_analysis()
        # No assertions needed - the fact that it completes without AttributeError confirms it


class TestDeltaAnalysisWriteLock:
    """Tests for write lock behavior in run_delta_analysis()."""

    def _make_delta_config(self):
        """Return a config mock where dependency_map_enabled is False to short-circuit delta."""
        config = MagicMock()
        config.dependency_map_enabled = False
        return config

    def test_releases_lock_when_acquire_returns_true(self):
        """When acquire_write_lock returns True, release_write_lock must be called."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # dependency_map_enabled=False causes early return inside try block
        config_manager.get_claude_integration_config.return_value = self._make_delta_config()

        service.run_delta_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_does_not_release_lock_when_acquire_returns_false(self):
        """When acquire_write_lock returns False, release_write_lock must NOT be called."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # dependency_map_enabled=False causes early return inside try block
        config_manager.get_claude_integration_config.return_value = self._make_delta_config()

        service.run_delta_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_not_called()

    def test_releases_lock_on_exception_when_acquire_returned_true(self):
        """release_write_lock is still called in finally even when exception occurs."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        config_manager.get_claude_integration_config.side_effect = RuntimeError("config error")

        with pytest.raises(RuntimeError, match="config error"):
            service.run_delta_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once()
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_does_not_release_lock_on_exception_when_acquire_returned_false(self):
        """release_write_lock is NOT called even on exception when acquire returned False."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        config_manager.get_claude_integration_config.side_effect = RuntimeError("config error")

        with pytest.raises(RuntimeError, match="config error"):
            service.run_delta_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once()
        refresh_scheduler.release_write_lock.assert_not_called()

    def test_no_acquire_or_release_when_refresh_scheduler_is_none(self):
        """When no refresh_scheduler, neither acquire nor release is attempted."""
        service, config_manager, _, _ = _make_service(refresh_scheduler=None)
        config = MagicMock()
        config.dependency_map_enabled = False
        config_manager.get_claude_integration_config.return_value = config

        # Should complete without AttributeError
        service.run_delta_analysis()
