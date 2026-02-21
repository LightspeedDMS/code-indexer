"""
Unit tests for Story #227 / P0-1 bug fix: Langfuse write lock alias mismatch.

The write lock must be acquired AFTER project discovery so that the alias
matches the actual repo folder name RefreshScheduler uses
(e.g. "langfuse_myproject"), not the generic "langfuse" alias that was used
before discovery and that RefreshScheduler would never match.
"""

import pytest
from unittest.mock import Mock, patch, call

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
    """Verify that the write lock uses the project-specific alias, not generic 'langfuse'."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_acquire_uses_project_specific_alias(self, mock_client_class, tmp_service, tmp_path):
        """acquire_write_lock must be called with 'langfuse_myproject', not 'langfuse'."""
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

        # The lock alias must be the sanitized project name, NOT generic "langfuse"
        mock_scheduler.acquire_write_lock.assert_called_once_with(
            "langfuse_myproject", owner_name="langfuse_trace_sync"
        )

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

        # Confirm the generic "langfuse" alias was NOT used
        for call_args in mock_scheduler.acquire_write_lock.call_args_list:
            alias_used = call_args[0][0] if call_args[0] else call_args[1].get("alias")
            assert alias_used != "langfuse", (
                f"acquire_write_lock was called with generic alias 'langfuse'; "
                f"expected project-specific alias"
            )

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_release_uses_same_alias_as_acquire(self, mock_client_class, tmp_service, tmp_path):
        """release_write_lock must be called with the same alias as acquire_write_lock."""
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

        acquire_alias = mock_scheduler.acquire_write_lock.call_args[0][0]
        release_alias = mock_scheduler.release_write_lock.call_args[0][0]
        assert acquire_alias == release_alias, (
            f"acquire used '{acquire_alias}' but release used '{release_alias}'"
        )

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_release_uses_project_specific_alias(self, mock_client_class, tmp_service, tmp_path):
        """release_write_lock must be called with 'langfuse_myproject'."""
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

        mock_scheduler.release_write_lock.assert_called_once_with(
            "langfuse_myproject", owner_name="langfuse_trace_sync"
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
