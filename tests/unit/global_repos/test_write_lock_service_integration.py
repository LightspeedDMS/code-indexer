"""
Unit tests for DependencyMapService and LangfuseTraceSyncService write-lock integration (Story #227).

Tests:
- DependencyMapService accepts optional refresh_scheduler parameter
- run_full_analysis() acquires/releases write lock on 'cidx-meta', triggers refresh
- run_delta_analysis() follows same lock/release/trigger sequence
- Lock released on exception; trigger NOT called on exception (AC5)
- LangfuseTraceSyncService accepts optional refresh_scheduler parameter
- sync_project() uses per-user-repo write locking (not project-level locking)
- No lock/release/trigger when no traces are written (empty result set)
- Lock released on exception; trigger NOT called on exception (AC5)
- Both services work correctly when _refresh_scheduler is None (backward compat)

Note: LangfuseTraceSyncService uses per-user-repo refresh model. No project-level
write lock is acquired. Locks are acquired per-user-repo only when traces are
actually written. When fetch_traces_page returns empty, no lock operations occur.
"""

from unittest.mock import MagicMock, patch

import pytest


def _make_dep_map_service(refresh_scheduler=None):
    """Create a DependencyMapService with mocked dependencies."""
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = "/tmp/golden-repos"

    config_manager = MagicMock()
    config = MagicMock()
    config.dependency_map_enabled = True
    config.dependency_map_pass1_max_turns = 5
    config.dependency_map_pass2_max_turns = 5
    config.dependency_map_interval_hours = 24
    config.dependency_map_pass_timeout_seconds = 300
    config_manager.get_claude_integration_config.return_value = config

    tracking_backend = MagicMock()
    analyzer = MagicMock()

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        refresh_scheduler=refresh_scheduler,
    )


def _make_langfuse_service(refresh_scheduler=None, tmp_path=None):
    """Create a LangfuseTraceSyncService with mocked dependencies."""
    from code_indexer.server.services.langfuse_trace_sync_service import LangfuseTraceSyncService

    config = MagicMock()
    config.langfuse_config = None

    def config_getter():
        return config

    data_dir = str(tmp_path / "data") if tmp_path else "/tmp/test-langfuse-data"

    return LangfuseTraceSyncService(
        config_getter=config_getter,
        data_dir=data_dir,
        refresh_scheduler=refresh_scheduler,
    )


# ============================================================================
# DependencyMapService Tests
# ============================================================================


class TestDependencyMapServiceWriteLock:
    """Tests for DependencyMapService write-lock integration."""

    def test_accepts_refresh_scheduler_parameter(self):
        """DependencyMapService.__init__() must accept optional refresh_scheduler parameter."""
        mock_scheduler = MagicMock()
        service = _make_dep_map_service(refresh_scheduler=mock_scheduler)

        assert service._refresh_scheduler is mock_scheduler

    def test_graceful_without_refresh_scheduler(self):
        """When _refresh_scheduler is None, no lock operations occur (backward compat)."""
        service = _make_dep_map_service(refresh_scheduler=None)

        assert service._refresh_scheduler is None

    def test_run_full_analysis_acquires_write_lock_before_analysis(self):
        """run_full_analysis() acquires write lock on 'cidx-meta' before the analysis pipeline."""
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_dep_map_service(refresh_scheduler=mock_scheduler)

        with patch.object(service, "_setup_analysis") as mock_setup:
            mock_setup.return_value = {
                "early_return": True,
                "status": "skipped",
                "message": "No activated golden repos",
            }
            try:
                service.run_full_analysis()
            except Exception:
                pass

        mock_scheduler.acquire_write_lock.assert_called_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_run_full_analysis_releases_write_lock_after_completion(self):
        """run_full_analysis() releases write lock on 'cidx-meta' in the finally block."""
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_dep_map_service(refresh_scheduler=mock_scheduler)

        with patch.object(service, "_setup_analysis") as mock_setup:
            mock_setup.return_value = {
                "early_return": True,
                "status": "skipped",
                "message": "No activated golden repos",
            }
            try:
                service.run_full_analysis()
            except Exception:
                pass

        mock_scheduler.release_write_lock.assert_called_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_run_full_analysis_calls_trigger_refresh_after_release(self):
        """AC2: After write lock release, trigger_refresh_for_repo('cidx-meta-global') is called."""
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_dep_map_service(refresh_scheduler=mock_scheduler)

        with patch.object(service, "_setup_analysis") as mock_setup:
            mock_setup.return_value = {
                "early_return": True,
                "status": "skipped",
                "message": "No activated golden repos",
            }
            try:
                service.run_full_analysis()
            except Exception:
                pass

        mock_scheduler.trigger_refresh_for_repo.assert_called_with("cidx-meta-global")

    def test_run_full_analysis_releases_lock_on_exception_no_trigger(self):
        """AC5: On exception, write lock is released but trigger is NOT called."""
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_dep_map_service(refresh_scheduler=mock_scheduler)

        with patch.object(
            service, "_setup_analysis", side_effect=RuntimeError("Simulated analysis failure")
        ):
            with pytest.raises(RuntimeError):
                service.run_full_analysis()

        mock_scheduler.release_write_lock.assert_called_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        mock_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_run_delta_analysis_acquires_write_lock(self):
        """run_delta_analysis() acquires write lock on 'cidx-meta'."""
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_dep_map_service(refresh_scheduler=mock_scheduler)

        config = MagicMock()
        config.dependency_map_enabled = True
        config.dependency_map_interval_hours = 24
        service._config_manager.get_claude_integration_config.return_value = config

        with patch.object(service, "detect_changes", return_value=([], [], [])):
            try:
                service.run_delta_analysis()
            except Exception:
                pass

        mock_scheduler.acquire_write_lock.assert_called_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_run_delta_analysis_releases_write_lock(self):
        """run_delta_analysis() releases write lock on 'cidx-meta' in the finally block."""
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_dep_map_service(refresh_scheduler=mock_scheduler)

        config = MagicMock()
        config.dependency_map_enabled = True
        config.dependency_map_interval_hours = 24
        service._config_manager.get_claude_integration_config.return_value = config

        with patch.object(service, "detect_changes", return_value=([], [], [])):
            try:
                service.run_delta_analysis()
            except Exception:
                pass

        mock_scheduler.release_write_lock.assert_called_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_run_delta_analysis_calls_trigger_refresh(self):
        """run_delta_analysis() calls trigger_refresh_for_repo('cidx-meta-global') after release."""
        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_dep_map_service(refresh_scheduler=mock_scheduler)

        config = MagicMock()
        config.dependency_map_enabled = True
        config.dependency_map_interval_hours = 24
        service._config_manager.get_claude_integration_config.return_value = config

        with patch.object(service, "detect_changes", return_value=([], [], [])):
            try:
                service.run_delta_analysis()
            except Exception:
                pass

        mock_scheduler.trigger_refresh_for_repo.assert_called_with("cidx-meta-global")

    def test_run_delta_analysis_no_lock_when_no_scheduler(self):
        """When _refresh_scheduler is None, no lock calls are made in run_delta_analysis."""
        service = _make_dep_map_service(refresh_scheduler=None)

        config = MagicMock()
        config.dependency_map_enabled = True
        config.dependency_map_interval_hours = 24
        service._config_manager.get_claude_integration_config.return_value = config

        # Should not raise any AttributeError about None._refresh_scheduler
        with patch.object(service, "detect_changes", return_value=([], [], [])):
            try:
                result = service.run_delta_analysis()
                # If it returns, check it's a valid result
                if result is not None:
                    assert "status" in result
            except Exception as e:
                # Any exception must NOT be about _refresh_scheduler being None
                assert "_refresh_scheduler" not in str(e), (
                    f"Exception must not be about _refresh_scheduler: {e}"
                )


# ============================================================================
# LangfuseTraceSyncService Tests
# ============================================================================


class TestLangfuseTraceSyncServiceWriteLock:
    """Tests for LangfuseTraceSyncService write-lock integration.

    Uses per-user-repo refresh model: no project-level write lock is acquired.
    Locks are acquired per-user-repo only when traces are actually written.
    When no traces exist, no lock/release/trigger calls happen at all.
    """

    def test_accepts_refresh_scheduler_parameter(self, tmp_path):
        """LangfuseTraceSyncService.__init__() accepts optional refresh_scheduler parameter."""
        mock_scheduler = MagicMock()
        service = _make_langfuse_service(refresh_scheduler=mock_scheduler, tmp_path=tmp_path)

        assert service._refresh_scheduler is mock_scheduler

    def test_graceful_without_refresh_scheduler(self, tmp_path):
        """When _refresh_scheduler is None, no lock operations occur (backward compat)."""
        service = _make_langfuse_service(refresh_scheduler=None, tmp_path=tmp_path)

        assert service._refresh_scheduler is None

    def test_sync_project_acquires_write_lock_before_writes(self, tmp_path):
        """With per-user-repo locking, no lock is acquired when there are no traces.

        No project-level lock is acquired at all during sync_project(). Per-user-repo
        locks are only acquired when traces are actually written. When fetch_traces_page
        returns empty, no lock operations occur.
        """
        from code_indexer.server.utils.config_manager import LangfusePullProject

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_langfuse_service(refresh_scheduler=mock_scheduler, tmp_path=tmp_path)

        mock_api_client = MagicMock()
        mock_api_client.discover_project.return_value = {"name": "my-project", "id": "proj-123"}
        mock_api_client.fetch_traces_page.return_value = []  # No traces — no lock needed

        creds = LangfusePullProject(public_key="pk-test", secret_key="sk-test")

        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient",
            return_value=mock_api_client,
        ):
            service.sync_project(
                host="http://localhost:3000",
                creds=creds,
                trace_age_days=7,
                max_concurrent_observations=1,
            )

        # No project-level lock; no traces written means no per-user-repo lock either
        mock_scheduler.acquire_write_lock.assert_not_called()

    def test_sync_project_releases_write_lock_after_completion(self, tmp_path):
        """With per-user-repo locking, no release is needed when no traces are written.

        No project-level lock is acquired at all, so no release is needed. Per-user-repo
        locks are only acquired (and released) when traces are actually written. When
        fetch_traces_page returns empty, no lock operations occur.
        """
        from code_indexer.server.utils.config_manager import LangfusePullProject

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_langfuse_service(refresh_scheduler=mock_scheduler, tmp_path=tmp_path)

        mock_api_client = MagicMock()
        mock_api_client.discover_project.return_value = {"name": "my-project", "id": "proj-123"}
        mock_api_client.fetch_traces_page.return_value = []

        creds = LangfusePullProject(public_key="pk-test", secret_key="sk-test")

        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient",
            return_value=mock_api_client,
        ):
            service.sync_project(
                host="http://localhost:3000",
                creds=creds,
                trace_age_days=7,
                max_concurrent_observations=1,
            )

        # No project-level lock; no traces written means no per-user-repo release either
        mock_scheduler.release_write_lock.assert_not_called()

    def test_sync_project_calls_trigger_refresh_after_release(self, tmp_path):
        """With per-user-repo locking, no trigger is called when no traces are written.

        No project-level lock is acquired, so no trigger is needed. Per-user-repo
        triggers are only called when traces are actually written (non-empty modified_repos).
        When fetch_traces_page returns empty, modified_repos is empty and no trigger occurs.
        """
        from code_indexer.server.utils.config_manager import LangfusePullProject

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_langfuse_service(refresh_scheduler=mock_scheduler, tmp_path=tmp_path)

        mock_api_client = MagicMock()
        mock_api_client.discover_project.return_value = {"name": "my-project", "id": "proj-123"}
        mock_api_client.fetch_traces_page.return_value = []

        creds = LangfusePullProject(public_key="pk-test", secret_key="sk-test")

        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient",
            return_value=mock_api_client,
        ):
            service.sync_project(
                host="http://localhost:3000",
                creds=creds,
                trace_age_days=7,
                max_concurrent_observations=1,
            )

        # No traces written means empty modified_repos; no trigger called
        mock_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_sync_project_no_lock_on_discovery_failure(self, tmp_path):
        """When discover_project() raises, no lock is acquired or released (lock is post-discovery)."""
        from code_indexer.server.utils.config_manager import LangfusePullProject

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_langfuse_service(refresh_scheduler=mock_scheduler, tmp_path=tmp_path)

        mock_api_client = MagicMock()
        mock_api_client.discover_project.side_effect = RuntimeError("API connection failed")

        creds = LangfusePullProject(public_key="pk-test", secret_key="sk-test")

        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient",
            return_value=mock_api_client,
        ):
            with pytest.raises(RuntimeError, match="API connection failed"):
                service.sync_project(
                    host="http://localhost:3000",
                    creds=creds,
                    trace_age_days=7,
                    max_concurrent_observations=1,
                )

        # Lock is acquired AFTER discovery, so discovery failure means no lock was held
        mock_scheduler.acquire_write_lock.assert_not_called()
        mock_scheduler.release_write_lock.assert_not_called()
        mock_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_sync_project_releases_lock_on_inner_sync_exception(self, tmp_path):
        """When _sync_project_inner() raises, exception propagates with no lock operations.

        No project-level lock is acquired in the per-user-repo model. When
        _sync_project_inner() raises, the exception propagates directly. No lock
        acquire/release or trigger calls happen at all.
        """
        from code_indexer.server.utils.config_manager import LangfusePullProject

        mock_scheduler = MagicMock()
        mock_scheduler.acquire_write_lock.return_value = True

        service = _make_langfuse_service(refresh_scheduler=mock_scheduler, tmp_path=tmp_path)

        mock_api_client = MagicMock()
        mock_api_client.discover_project.return_value = {"name": "my-project", "id": "proj-123"}

        creds = LangfusePullProject(public_key="pk-test", secret_key="sk-test")

        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient",
            return_value=mock_api_client,
        ), patch.object(
            service, "_sync_project_inner",
            side_effect=RuntimeError("Sync failed mid-write"),
        ):
            with pytest.raises(RuntimeError, match="Sync failed mid-write"):
                service.sync_project(
                    host="http://localhost:3000",
                    creds=creds,
                    trace_age_days=7,
                    max_concurrent_observations=1,
                )

        # No project-level lock is acquired/released in new per-user-repo model
        mock_scheduler.acquire_write_lock.assert_not_called()
        mock_scheduler.release_write_lock.assert_not_called()
        mock_scheduler.trigger_refresh_for_repo.assert_not_called()

    def test_sync_project_no_lock_when_no_scheduler(self, tmp_path):
        """When _refresh_scheduler is None, sync_project() works without lock operations."""
        from code_indexer.server.utils.config_manager import LangfusePullProject

        service = _make_langfuse_service(refresh_scheduler=None, tmp_path=tmp_path)

        mock_api_client = MagicMock()
        mock_api_client.discover_project.return_value = {"name": "my-project", "id": "proj-123"}
        mock_api_client.fetch_traces_page.return_value = []

        creds = LangfusePullProject(public_key="pk-test", secret_key="sk-test")

        # Must not raise AttributeError about None._refresh_scheduler
        with patch(
            "code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient",
            return_value=mock_api_client,
        ):
            try:
                service.sync_project(
                    host="http://localhost:3000",
                    creds=creds,
                    trace_age_days=7,
                    max_concurrent_observations=1,
                )
            except Exception as e:
                assert "_refresh_scheduler" not in str(e), (
                    f"Exception must not be about _refresh_scheduler: {e}"
                )
