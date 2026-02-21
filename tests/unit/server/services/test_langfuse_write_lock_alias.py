"""
Unit tests for Langfuse write lock behavior.

The original Story #227 bug: write lock was acquired with the generic "langfuse" alias
before project discovery. That was fixed to acquire AFTER discovery.

The current bug (per-user-repo fix): write lock was still acquired at project level
("langfuse_myproject") even though that alias does NOT exist in RefreshScheduler.
Only per-user repos exist (e.g. "langfuse_myproject_alice_example_com").

The fix: no project-level write lock is acquired at all. Instead, per-user-repo
refresh is triggered after sync completes (handled in test_langfuse_per_user_repo_refresh.py).
The overlap window + content hash strategy makes partial snapshots self-healing.
"""

import pytest
from unittest.mock import Mock, patch

from code_indexer.server.services.langfuse_trace_sync_service import LangfuseTraceSyncService
from code_indexer.server.utils.config_manager import LangfuseConfig, LangfusePullProject, ServerConfig


@pytest.fixture
def tmp_service(tmp_path):
    """Create a LangfuseTraceSyncService with a mock refresh_scheduler."""
    config = ServerConfig(
        server_dir=str(tmp_path),
        langfuse_config=LangfuseConfig(
            pull_enabled=True,
            pull_projects=[
                LangfusePullProject(public_key="pk_test", secret_key="sk_test")
            ],
        ),
    )
    service = LangfuseTraceSyncService(
        config_getter=lambda: config,
        data_dir=str(tmp_path),
    )
    mock_scheduler = Mock()
    mock_scheduler.acquire_write_lock.return_value = True
    service._refresh_scheduler = mock_scheduler
    return service, mock_scheduler


class TestWriteLockAliasAfterDiscovery:
    """Verify that no project-level write lock is acquired.

    Project-level aliases ("langfuse_myproject") do not exist in RefreshScheduler.
    Only per-user repos exist. Acquiring a non-existent alias raises ValueError.
    The correct behavior is: no project-level lock at all.
    Per-user refresh is tested in test_langfuse_per_user_repo_refresh.py.
    """

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_no_project_level_acquire_when_no_traces(self, mock_client_class, tmp_service, tmp_path):
        """When no traces exist, acquire_write_lock must not be called at all.

        Previously the code acquired 'langfuse_MyProject' which does not exist
        in RefreshScheduler and caused ValueError every 60 seconds.
        """
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_traces_page.return_value = []

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        # No traces = no writes = no lock needed at any level
        mock_scheduler.acquire_write_lock.assert_not_called()

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_no_project_level_release_when_no_traces(self, mock_client_class, tmp_service, tmp_path):
        """When no traces exist, release_write_lock must not be called at all."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_traces_page.return_value = []

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        mock_scheduler.release_write_lock.assert_not_called()

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_acquire_alias_does_not_use_generic_langfuse(self, mock_client_class, tmp_service, tmp_path):
        """acquire_write_lock must never be called with the generic 'langfuse' alias."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_traces_page.return_value = []

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        # Confirm the generic "langfuse" alias was NOT used (if acquire was called at all)
        for call_args in mock_scheduler.acquire_write_lock.call_args_list:
            alias_used = call_args[0][0] if call_args[0] else call_args[1].get("alias")
            assert alias_used != "langfuse", (
                "acquire_write_lock was called with generic alias 'langfuse'; "
                "this alias does not exist in RefreshScheduler"
            )

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_no_project_level_acquire_alias_used(self, mock_client_class, tmp_service, tmp_path):
        """acquire_write_lock must not be called with the project-level alias 'langfuse_MyProject'.

        This alias does not exist in RefreshScheduler - only per-user repos do.
        Calling acquire with this alias raises ValueError on staging every 60 seconds.
        """
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_traces_page.return_value = []

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        project_alias = "langfuse_MyProject"
        acquire_aliases = [
            mock_scheduler.acquire_write_lock.call_args_list[i][0][0]
            for i in range(len(mock_scheduler.acquire_write_lock.call_args_list))
        ]
        assert project_alias not in acquire_aliases, (
            f"acquire_write_lock was called with project-level alias '{project_alias}' "
            f"which does not exist in RefreshScheduler registry. "
            f"All acquire calls: {acquire_aliases}"
        )


class TestWriteLockNotAcquiredOnDiscoveryFailure:
    """Verify that if discover_project() raises, no lock release is attempted."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_no_release_when_discover_raises(self, mock_client_class, tmp_service, tmp_path):
        """If discover_project() raises, release_write_lock must not be called."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.side_effect = RuntimeError("API unreachable")

        with pytest.raises(RuntimeError, match="API unreachable"):
            service.sync_project(
                host="https://example.com",
                creds=LangfusePullProject(public_key="pk", secret_key="sk"),
                trace_age_days=7,
            )

        # Lock was never acquired (discover failed before acquire), so release must not be called
        mock_scheduler.acquire_write_lock.assert_not_called()
        mock_scheduler.release_write_lock.assert_not_called()

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_no_trigger_refresh_when_discover_raises(self, mock_client_class, tmp_service, tmp_path):
        """If discover_project() raises, trigger_refresh_for_repo must not be called."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.side_effect = RuntimeError("API unreachable")

        with pytest.raises(RuntimeError):
            service.sync_project(
                host="https://example.com",
                creds=LangfusePullProject(public_key="pk", secret_key="sk"),
                trace_age_days=7,
            )

        mock_scheduler.trigger_refresh_for_repo.assert_not_called()
