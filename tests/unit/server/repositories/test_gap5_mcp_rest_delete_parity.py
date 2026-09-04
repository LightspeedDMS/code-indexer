"""GAP-5: MCP vs REST API delete parity -- job cancellation in manager.

Validates that GoldenRepoManager.remove_golden_repo() cancels active
background jobs (add, refresh, global_repo_refresh) for the alias BEFORE
submitting the removal job, and that add_golden_repo() checks the global
registry and on-disk clone directory for stale entries.

Previously, only the REST handler (inline_admin_ops.py) cancelled jobs;
the MCP handler called the manager directly without cancellation. Moving
the logic into the manager makes both API surfaces safe automatically.
"""

import os
import pytest
from unittest.mock import MagicMock, Mock, patch
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    GoldenRepoError,
)


@pytest.fixture
def manager(tmp_path):
    """Create a GoldenRepoManager with mocked dependencies."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    mgr = GoldenRepoManager(data_dir=data_dir)
    # Inject a mock background_job_manager (normally done by app.py at runtime)
    mgr.background_job_manager = MagicMock()
    mgr.activated_repo_manager = None
    return mgr


class TestRemoveGoldenRepoJobCancellation:
    """GAP-5: remove_golden_repo cancels active jobs before submitting removal."""

    def test_cancels_pending_and_running_jobs(self, manager):
        """Active (pending/running) jobs for the alias are cancelled before removal."""
        # Register a repo so _resolve_golden_repo finds it
        manager.golden_repos["test-repo"] = MagicMock()

        # Mock the background job manager
        manager.background_job_manager.get_jobs_by_operation_and_params.return_value = [
            {"job_id": "job-1", "status": "running"},
            {"job_id": "job-2", "status": "pending"},
            {"job_id": "job-3", "status": "completed"},  # should NOT be cancelled
        ]
        manager.background_job_manager.cancel_job.return_value = {
            "success": True,
            "message": "Cancelled",
        }
        manager.background_job_manager.submit_job.return_value = "removal-job-id"

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False
            job_id = manager.remove_golden_repo("test-repo", submitter_username="admin")

        # Verify job lookup was called with correct operation types
        manager.background_job_manager.get_jobs_by_operation_and_params.assert_called_once_with(
            operation_types=[
                "add_golden_repo",
                "refresh_golden_repo",
                "global_repo_refresh",
            ],
            params_filter={"alias": "test-repo"},
        )

        # Verify only pending/running jobs were cancelled (not completed)
        assert manager.background_job_manager.cancel_job.call_count == 2
        cancelled_job_ids = [
            call.args[0]
            for call in manager.background_job_manager.cancel_job.call_args_list
        ]
        assert "job-1" in cancelled_job_ids
        assert "job-2" in cancelled_job_ids

        # Verify removal job was still submitted
        assert job_id == "removal-job-id"

    def test_cancellation_failure_does_not_block_removal(self, manager):
        """If job cancellation fails, removal still proceeds."""
        manager.golden_repos["test-repo"] = MagicMock()

        manager.background_job_manager.get_jobs_by_operation_and_params.side_effect = (
            RuntimeError("DB connection lost")
        )
        manager.background_job_manager.submit_job.return_value = "removal-job-id"

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False
            job_id = manager.remove_golden_repo("test-repo", submitter_username="admin")

        # Removal job is still submitted despite cancellation failure
        assert job_id == "removal-job-id"
        manager.background_job_manager.submit_job.assert_called_once()

    def test_no_jobs_to_cancel(self, manager):
        """When no active jobs exist, removal proceeds normally."""
        manager.golden_repos["test-repo"] = MagicMock()

        manager.background_job_manager.get_jobs_by_operation_and_params.return_value = []
        manager.background_job_manager.submit_job.return_value = "removal-job-id"

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False
            job_id = manager.remove_golden_repo("test-repo", submitter_username="admin")

        # No cancel calls, but removal still submitted
        manager.background_job_manager.cancel_job.assert_not_called()
        assert job_id == "removal-job-id"

    def test_submitter_username_passed_to_cancel(self, manager):
        """The submitter_username is forwarded to cancel_job for audit trail."""
        manager.golden_repos["test-repo"] = MagicMock()

        manager.background_job_manager.get_jobs_by_operation_and_params.return_value = [
            {"job_id": "job-1", "status": "running"},
        ]
        manager.background_job_manager.cancel_job.return_value = {
            "success": True,
            "message": "Cancelled",
        }
        manager.background_job_manager.submit_job.return_value = "removal-job-id"

        with patch(
            "code_indexer.server.services.maintenance_service.get_maintenance_state"
        ) as mock_maint:
            mock_maint.return_value.is_maintenance_mode.return_value = False
            manager.remove_golden_repo("test-repo", submitter_username="charlie")

        # Verify username was passed through
        manager.background_job_manager.cancel_job.assert_called_once_with(
            "job-1", "charlie", is_admin=True
        )


class TestAddGoldenRepoGhostChecks:
    """GAP-5: add_golden_repo checks global registry and on-disk for stale entries."""

    def test_rejects_alias_with_stale_global_registry_entry(self, manager):
        """Adding an alias that has a stale global registry entry is rejected."""
        # No golden_repos row (the primary check passes)
        # But a stale global registry entry exists
        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = {
            "alias": "test-repo-global",
            "repo_url": "https://example.com/repo.git",
        }

        with (
            patch(
                "code_indexer.server.services.maintenance_service.get_maintenance_state"
            ) as mock_maint,
            patch(
                "code_indexer.global_repos.global_activation.GlobalActivator"
            ) as mock_activator_cls,
        ):
            mock_maint.return_value.is_maintenance_mode.return_value = False
            mock_activator_cls.return_value.registry = mock_registry

            with pytest.raises(GoldenRepoError, match="stale global registry"):
                manager.add_golden_repo(
                    repo_url="https://example.com/repo.git",
                    alias="test-repo",
                )

    def test_allows_alias_with_no_global_registry_entry(self, manager):
        """Adding an alias with no global registry entry proceeds normally."""
        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = None

        with (
            patch(
                "code_indexer.server.services.maintenance_service.get_maintenance_state"
            ) as mock_maint,
            patch(
                "code_indexer.global_repos.global_activation.GlobalActivator"
            ) as mock_activator_cls,
        ):
            mock_maint.return_value.is_maintenance_mode.return_value = False
            mock_activator_cls.return_value.registry = mock_registry
            # Mock git validation to avoid real network call
            manager._validate_git_repository = Mock(return_value=True)
            manager.background_job_manager.submit_job.return_value = "add-job-id"

            job_id = manager.add_golden_repo(
                repo_url="https://example.com/repo.git",
                alias="test-repo",
            )

        assert job_id == "add-job-id"

    def test_global_registry_check_failure_is_non_fatal(self, manager):
        """If the global registry check fails, add proceeds (existing behavior)."""
        with (
            patch(
                "code_indexer.server.services.maintenance_service.get_maintenance_state"
            ) as mock_maint,
            patch(
                "code_indexer.global_repos.global_activation.GlobalActivator"
            ) as mock_activator_cls,
        ):
            mock_maint.return_value.is_maintenance_mode.return_value = False
            mock_activator_cls.side_effect = RuntimeError("Registry unavailable")
            # Mock git validation
            manager._validate_git_repository = Mock(return_value=True)
            manager.background_job_manager.submit_job.return_value = "add-job-id"

            # Should NOT raise -- the check is non-fatal
            job_id = manager.add_golden_repo(
                repo_url="https://example.com/repo.git",
                alias="test-repo",
            )

        assert job_id == "add-job-id"

    def test_logs_warning_for_orphaned_clone_directory(self, manager, tmp_path):
        """An orphaned on-disk clone directory triggers a warning log."""
        # Create the orphaned clone directory
        clone_dir = os.path.join(manager.golden_repos_dir, "test-repo")
        os.makedirs(clone_dir, exist_ok=True)

        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = None

        with (
            patch(
                "code_indexer.server.services.maintenance_service.get_maintenance_state"
            ) as mock_maint,
            patch(
                "code_indexer.global_repos.global_activation.GlobalActivator"
            ) as mock_activator_cls,
            patch(
                "code_indexer.server.repositories.golden_repo_manager.logging"
            ) as mock_log,
        ):
            mock_maint.return_value.is_maintenance_mode.return_value = False
            mock_activator_cls.return_value.registry = mock_registry
            manager._validate_git_repository = Mock(return_value=True)
            manager.background_job_manager.submit_job.return_value = "add-job-id"

            job_id = manager.add_golden_repo(
                repo_url="https://example.com/repo.git",
                alias="test-repo",
            )

        assert job_id == "add-job-id"
        # Verify warning was logged about the orphaned clone
        warning_calls = [
            call
            for call in mock_log.warning.call_args_list
            if "orphaned clone" in str(call).lower() or "GAP-5" in str(call)
        ]
        assert len(warning_calls) > 0, (
            "Expected a warning about orphaned clone directory"
        )

    def test_no_warning_for_local_url_with_existing_dir(self, manager):
        """local:// URLs with existing directories do NOT trigger the warning."""
        clone_dir = os.path.join(manager.golden_repos_dir, "local-repo")
        os.makedirs(clone_dir, exist_ok=True)

        mock_registry = MagicMock()
        mock_registry.get_global_repo.return_value = None

        with (
            patch(
                "code_indexer.server.services.maintenance_service.get_maintenance_state"
            ) as mock_maint,
            patch(
                "code_indexer.global_repos.global_activation.GlobalActivator"
            ) as mock_activator_cls,
            patch(
                "code_indexer.server.repositories.golden_repo_manager.logging"
            ) as mock_log,
        ):
            mock_maint.return_value.is_maintenance_mode.return_value = False
            mock_activator_cls.return_value.registry = mock_registry
            manager.background_job_manager.submit_job.return_value = "add-job-id"

            job_id = manager.add_golden_repo(
                repo_url="local:///some/path",
                alias="local-repo",
                skip_pre_flight_git_validation=True,
            )

        assert job_id == "add-job-id"
        # No warning about orphaned clone for local:// URLs
        warning_calls = [
            call
            for call in mock_log.warning.call_args_list
            if "orphaned clone" in str(call).lower()
        ]
        assert len(warning_calls) == 0, (
            "Should not warn about orphaned clone for local:// URLs"
        )
