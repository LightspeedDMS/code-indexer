"""
Unit tests for Story #441: Langfuse sync acquires per-user write lock before writing traces.

Verifies that:
1. acquire_write_lock is called with per-user repo alias (without -global suffix)
   and owner_name="langfuse_sync" before triggering refresh
2. release_write_lock is called with the same args after trigger completes
3. release_write_lock is called even when trigger_refresh raises an exception
4. No errors when _refresh_scheduler is None
5. Independent locks per user (one acquire/release pair per per-user repo)
"""

import pytest
from unittest.mock import Mock, patch

from code_indexer.server.services.langfuse_trace_sync_service import (
    LangfuseTraceSyncService,
)
from code_indexer.server.utils.config_manager import (
    LangfuseConfig,
    LangfusePullProject,
    ServerConfig,
)


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


class TestSyncAcquiresWriteLockBeforeRefreshTrigger:
    """Story #441: acquire_write_lock must be called with per-user repo alias before refresh."""

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_sync_acquires_write_lock_before_refresh_trigger(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """acquire_write_lock must be called with repo_folder_name (not -global) and
        owner 'langfuse_sync' before triggering refresh for a per-user repo.
        """
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_observations.return_value = []

        traces_page1 = [_make_trace("trace-001", "alice@example.com")]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        # The per-user repo folder name is the alias WITHOUT -global suffix
        expected_repo = "langfuse_MyProject_alice_example.com"
        acquire_calls = mock_scheduler.acquire_write_lock.call_args_list
        acquire_aliases = [
            c[0][0] if c[0] else c[1].get("alias") for c in acquire_calls
        ]

        assert expected_repo in acquire_aliases, (
            f"Expected acquire_write_lock to be called with '{expected_repo}', "
            f"but got calls with aliases: {acquire_aliases}"
        )

        # Verify owner_name is 'langfuse_sync'
        for c in acquire_calls:
            if (c[0][0] if c[0] else c[1].get("alias")) == expected_repo:
                owner = c[1].get("owner_name") or (c[0][1] if len(c[0]) > 1 else None)
                assert (
                    owner == "langfuse_sync"
                ), f"Expected owner_name='langfuse_sync' but got '{owner}'"

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_sync_releases_write_lock_after_refresh(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """release_write_lock must be called with same args after refresh trigger completes."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_observations.return_value = []

        traces_page1 = [_make_trace("trace-001", "alice@example.com")]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]

        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        expected_repo = "langfuse_MyProject_alice_example.com"
        release_calls = mock_scheduler.release_write_lock.call_args_list
        release_aliases = [
            c[0][0] if c[0] else c[1].get("alias") for c in release_calls
        ]

        assert expected_repo in release_aliases, (
            f"Expected release_write_lock to be called with '{expected_repo}', "
            f"but got calls with aliases: {release_aliases}"
        )

        # Verify owner_name matches acquire
        for c in release_calls:
            if (c[0][0] if c[0] else c[1].get("alias")) == expected_repo:
                owner = c[1].get("owner_name") or (c[0][1] if len(c[0]) > 1 else None)
                assert (
                    owner == "langfuse_sync"
                ), f"Expected owner_name='langfuse_sync' in release but got '{owner}'"

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_sync_releases_write_lock_on_refresh_failure(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """release_write_lock must be called even when trigger_refresh_for_repo raises."""
        service, mock_scheduler = tmp_service

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_observations.return_value = []

        traces_page1 = [_make_trace("trace-001", "alice@example.com")]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]

        # Make trigger_refresh raise (not DuplicateJobError - a generic exception)
        mock_scheduler.trigger_refresh_for_repo.side_effect = RuntimeError(
            "network error"
        )

        # sync_project should NOT re-raise (exception is caught internally)
        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )

        expected_repo = "langfuse_MyProject_alice_example.com"

        # Lock must have been acquired
        acquire_aliases = [
            c[0][0] if c[0] else c[1].get("alias")
            for c in mock_scheduler.acquire_write_lock.call_args_list
        ]
        assert (
            expected_repo in acquire_aliases
        ), f"Expected acquire_write_lock called with '{expected_repo}', got: {acquire_aliases}"

        # Lock must be released even though trigger raised
        release_aliases = [
            c[0][0] if c[0] else c[1].get("alias")
            for c in mock_scheduler.release_write_lock.call_args_list
        ]
        assert expected_repo in release_aliases, (
            f"Expected release_write_lock called with '{expected_repo}' despite exception, "
            f"got: {release_aliases}"
        )

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_sync_no_lock_when_no_scheduler(self, mock_client_class, tmp_path):
        """When _refresh_scheduler is None, no lock errors and no crash."""
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
        # Explicitly set to None - no scheduler
        service._refresh_scheduler = None

        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "MyProject"}
        mock_client.fetch_observations.return_value = []

        traces_page1 = [_make_trace("trace-001", "alice@example.com")]
        mock_client.fetch_traces_page.side_effect = [traces_page1, []]

        # Must not raise
        service.sync_project(
            host="https://example.com",
            creds=LangfusePullProject(public_key="pk", secret_key="sk"),
            trace_age_days=7,
        )
        # Test passes if no exception was raised

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_sync_independent_locks_per_user(
        self, mock_client_class, tmp_service, tmp_path
    ):
        """Two repos modified must produce two separate acquire/release pairs."""
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

        alice_repo = "langfuse_MyProject_alice_example.com"
        bob_repo = "langfuse_MyProject_bob_example.com"

        acquire_aliases = [
            c[0][0] if c[0] else c[1].get("alias")
            for c in mock_scheduler.acquire_write_lock.call_args_list
        ]
        release_aliases = [
            c[0][0] if c[0] else c[1].get("alias")
            for c in mock_scheduler.release_write_lock.call_args_list
        ]

        assert (
            alice_repo in acquire_aliases
        ), f"Expected acquire for '{alice_repo}', got: {acquire_aliases}"
        assert (
            bob_repo in acquire_aliases
        ), f"Expected acquire for '{bob_repo}', got: {acquire_aliases}"
        assert (
            alice_repo in release_aliases
        ), f"Expected release for '{alice_repo}', got: {release_aliases}"
        assert (
            bob_repo in release_aliases
        ), f"Expected release for '{bob_repo}', got: {release_aliases}"
