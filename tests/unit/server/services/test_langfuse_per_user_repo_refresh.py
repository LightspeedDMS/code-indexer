"""
Unit tests for per-user-repo write-lock and refresh bug fix.

The bug: sync_project() acquires write lock and triggers refresh at the PROJECT level
using alias "langfuse_{project}" and trigger "langfuse_{project}-global".
These repo aliases DO NOT EXIST in RefreshScheduler - per-user repos do:
  - langfuse_Claude_Code_seba_battig_lightspeeddms_com
  - langfuse_Claude_Code_no_user
  - langfuse_Claude_Code_unknown

This produces the error on staging every 60 seconds:
  ValueError: Repository 'langfuse_Claude_Code-global' not found in global registry

Fix: sync_project() must trigger refresh per-user-repo, not per-project.
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


def _make_trace(trace_id: str, user_id: str, session_id: str = "session1") -> dict:
    """Helper to build a minimal trace dict."""
    return {
        "id": trace_id,
        "userId": user_id,
        "sessionId": session_id,
        "updatedAt": "2024-01-01T00:00:00Z",
        "timestamp": "2024-01-01T00:00:00Z",
        "name": "test-trace",
    }


class TestSyncProjectTriggersPerUserRefresh:
    """Verify that trigger_refresh_for_repo uses per-user-repo aliases, not project-level."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_trigger_refresh_uses_per_user_alias_not_project_alias(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """trigger_refresh_for_repo must be called with per-user repo alias, not project alias.

        When a sync writes traces for user 'seba.battig@lightspeeddms.com',
        the refresh must target 'langfuse_Claude_Code_seba_battig_lightspeeddms_com-global',
        NOT 'langfuse_Claude_Code-global'.
        """
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude Code"}
        mock_client.fetch_observations.return_value = []

        # One trace from a specific user
        traces_page1 = [_make_trace("trace-001", "seba.battig@lightspeeddms.com")]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]  # page 1 has data, page 2 empty

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        # The project-level alias must NOT be used for refresh
        project_level_alias = "langfuse_Claude_Code-global"
        trigger_calls = [str(c) for c in mock_scheduler.trigger_refresh_for_repo.call_args_list]
        for c in trigger_calls:
            assert project_level_alias not in c, (
                f"trigger_refresh_for_repo was called with project-level alias "
                f"'{project_level_alias}' but should use per-user-repo alias"
            )

        # The per-user repo alias MUST be used for refresh
        expected_alias = "langfuse_Claude_Code_seba.battig_lightspeeddms.com-global"
        trigger_aliases = [
            mock_scheduler.trigger_refresh_for_repo.call_args_list[i][0][0]
            for i in range(len(mock_scheduler.trigger_refresh_for_repo.call_args_list))
        ]
        assert expected_alias in trigger_aliases, (
            f"Expected trigger_refresh_for_repo to be called with '{expected_alias}', "
            f"but got calls: {trigger_aliases}"
        )

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_trigger_refresh_per_unique_user_only_once(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """Each per-user repo gets exactly one refresh trigger, even if multiple traces exist."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude Code"}
        mock_client.fetch_observations.return_value = []

        # Two traces from the same user
        traces_page1 = [
            _make_trace("trace-001", "seba@example.com", "session1"),
            _make_trace("trace-002", "seba@example.com", "session2"),
        ]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        # Should trigger exactly once for this user's repo
        expected_alias = "langfuse_Claude_Code_seba_example.com-global"
        trigger_aliases = [
            mock_scheduler.trigger_refresh_for_repo.call_args_list[i][0][0]
            for i in range(len(mock_scheduler.trigger_refresh_for_repo.call_args_list))
        ]
        count = trigger_aliases.count(expected_alias)
        assert count == 1, (
            f"Expected exactly 1 trigger for '{expected_alias}', got {count}. "
            f"All triggers: {trigger_aliases}"
        )

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_trigger_refresh_for_multiple_users(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """Each unique user gets their own refresh trigger."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_observations.return_value = []

        traces_page1 = [
            _make_trace("trace-001", "alice@example.com"),
            _make_trace("trace-002", "bob@example.com"),
        ]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        trigger_aliases = {
            mock_scheduler.trigger_refresh_for_repo.call_args_list[i][0][0]
            for i in range(len(mock_scheduler.trigger_refresh_for_repo.call_args_list))
        }

        alice_alias = "langfuse_MyProject_alice_example.com-global"
        bob_alias = "langfuse_MyProject_bob_example.com-global"

        assert alice_alias in trigger_aliases, (
            f"Expected '{alice_alias}' in trigger calls, got: {trigger_aliases}"
        )
        assert bob_alias in trigger_aliases, (
            f"Expected '{bob_alias}' in trigger calls, got: {trigger_aliases}"
        )

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_no_refresh_when_no_traces_written(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """When no traces are fetched, no refresh should be triggered."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_traces_page.return_value = []  # Empty - no traces

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        # No traces = no repos touched = no refresh
        mock_scheduler.trigger_refresh_for_repo.assert_not_called()

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_no_user_traces_use_no_user_repo(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """Traces with no userId go into the 'no_user' per-user repo."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "TestProject"}
        mock_client.fetch_observations.return_value = []

        # Trace with no userId
        traces_page1 = [
            {
                "id": "trace-001",
                "userId": None,
                "sessionId": "session1",
                "updatedAt": "2024-01-01T00:00:00Z",
                "timestamp": "2024-01-01T00:00:00Z",
                "name": "test",
            }
        ]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        trigger_aliases = [
            mock_scheduler.trigger_refresh_for_repo.call_args_list[i][0][0]
            for i in range(len(mock_scheduler.trigger_refresh_for_repo.call_args_list))
        ]
        expected_alias = "langfuse_TestProject_no_user-global"
        assert expected_alias in trigger_aliases, (
            f"Expected '{expected_alias}' in trigger calls, got: {trigger_aliases}"
        )


class TestSyncProjectAcquiresPerUserWriteLock:
    """Verify write-lock is acquired for per-user repos, not project-level.

    Note: The simplest correct approach (per the bug description) is to skip
    per-page locking since overlap window + content hash makes partial snapshots
    self-healing. The key requirement is that refresh is triggered per-user-repo.
    Write locks at the project level are the wrong target even if acquired.
    """

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_no_project_level_write_lock_acquired(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """acquire_write_lock must NOT be called with project-level alias.

        The project-level alias 'langfuse_Claude_Code' does not exist in the
        RefreshScheduler registry - only per-user repos do.
        """
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude Code"}
        mock_client.fetch_observations.return_value = []

        traces_page1 = [_make_trace("trace-001", "user@example.com")]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        # Project-level alias must not be acquired
        project_alias = "langfuse_Claude_Code"
        acquire_aliases = [
            mock_scheduler.acquire_write_lock.call_args_list[i][0][0]
            for i in range(len(mock_scheduler.acquire_write_lock.call_args_list))
        ]
        assert project_alias not in acquire_aliases, (
            f"acquire_write_lock was called with project-level alias '{project_alias}' "
            f"which does not exist in RefreshScheduler registry. "
            f"All acquire calls: {acquire_aliases}"
        )
