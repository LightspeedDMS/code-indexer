"""Tests for the Issue #1398 CLI-side remote read-timeout design decision.

The issue explicitly flags api_clients/base_client.py's hardcoded
httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0) as an open
design decision to resolve during implementation: either a .code-indexer
remote-config field, or a `cidx query --timeout` CLI flag. This repo
implements the .remote-config field approach (see base_client.py /
query_execution.py docstrings for the full rationale): a new optional
"api_read_timeout_seconds" key in .code-indexer/.remote-config, read once
at RemoteQueryClient construction time and threaded into the httpx.Timeout
read= value -- a durable, persisted override rather than a per-invocation
flag, since a shard-count-driven slow deployment needs a standing fix, not
a flag to remember on every query.
"""

from unittest.mock import patch

import pytest

from code_indexer.api_clients.base_client import CIDXRemoteAPIClient


def _make_client(read_timeout_seconds=None) -> CIDXRemoteAPIClient:
    return CIDXRemoteAPIClient(
        server_url="https://cidx.example.com",
        credentials={"username": "testuser", "password": "testpass"},
        read_timeout_seconds=read_timeout_seconds,
    )


class TestCIDXRemoteAPIClientReadTimeoutOverride:
    def test_default_read_timeout_is_unchanged_30_seconds(self) -> None:
        client = _make_client()
        session = client.session
        assert session.timeout.read == 30.0

    def test_custom_read_timeout_overrides_session_timeout(self) -> None:
        client = _make_client(read_timeout_seconds=120.0)
        session = client.session
        assert session.timeout.read == 120.0

    def test_connect_write_pool_timeouts_unaffected_by_read_override(self) -> None:
        """Only the read timeout is operator-overridable; connect/write/pool
        stay at their existing hardcoded values -- narrow, surgical fix."""
        client = _make_client(read_timeout_seconds=200.0)
        session = client.session
        assert session.timeout.connect == 10.0
        assert session.timeout.write == 10.0
        assert session.timeout.pool == 5.0


class TestExecuteRemoteQueryReadsConfiguredTimeout:
    """execute_remote_query must read api_read_timeout_seconds from the
    project's .remote-config JSON and thread it through to
    RemoteQueryClient construction."""

    def test_configured_timeout_reaches_remote_query_client(self, tmp_path) -> None:
        from code_indexer.remote import query_execution
        from code_indexer.remote.repository_linking import (
            RepositoryLink,
            RepositoryType,
        )

        mock_repository_link = RepositoryLink(
            alias="test-repo",
            git_url="https://github.com/user/repo.git",
            branch="main",
            repository_type=RepositoryType.ACTIVATED,
            server_url="https://cidx.example.com",
            linked_at="2024-01-01T10:00:00Z",
            display_name="Test Repository",
            description="Test repository",
            access_level="read",
        )

        with (
            patch.object(
                query_execution,
                "_load_remote_configuration",
                return_value={
                    "server_url": "https://cidx.example.com",
                    "username": "testuser",
                    "api_read_timeout_seconds": 180.0,
                },
            ),
            patch.object(
                query_execution,
                "load_repository_link",
                return_value=mock_repository_link,
            ),
            patch.object(
                query_execution, "_get_decrypted_credentials", return_value={}
            ),
            patch.object(query_execution, "RemoteQueryClient") as MockClient,
            patch(
                "code_indexer.remote.staleness_detector.StalenessDetector"
            ) as MockStaleness,
        ):
            MockClient.return_value.__enter__.return_value.execute_query.return_value = []
            MockStaleness.return_value.apply_staleness_detection.return_value = []

            query_execution.execute_remote_query("test query", 10, tmp_path)

        MockClient.assert_called_once()
        assert MockClient.call_args.kwargs.get("read_timeout_seconds") == 180.0

    def test_missing_config_key_passes_none_preserving_default(self, tmp_path) -> None:
        from code_indexer.remote import query_execution
        from code_indexer.remote.repository_linking import (
            RepositoryLink,
            RepositoryType,
        )

        mock_repository_link = RepositoryLink(
            alias="test-repo",
            git_url="https://github.com/user/repo.git",
            branch="main",
            repository_type=RepositoryType.ACTIVATED,
            server_url="https://cidx.example.com",
            linked_at="2024-01-01T10:00:00Z",
            display_name="Test Repository",
            description="Test repository",
            access_level="read",
        )

        with (
            patch.object(
                query_execution,
                "_load_remote_configuration",
                return_value={
                    "server_url": "https://cidx.example.com",
                    "username": "testuser",
                    # no api_read_timeout_seconds key
                },
            ),
            patch.object(
                query_execution,
                "load_repository_link",
                return_value=mock_repository_link,
            ),
            patch.object(
                query_execution, "_get_decrypted_credentials", return_value={}
            ),
            patch.object(query_execution, "RemoteQueryClient") as MockClient,
            patch(
                "code_indexer.remote.staleness_detector.StalenessDetector"
            ) as MockStaleness,
        ):
            MockClient.return_value.__enter__.return_value.execute_query.return_value = []
            MockStaleness.return_value.apply_staleness_detection.return_value = []

            query_execution.execute_remote_query("test query", 10, tmp_path)

        MockClient.assert_called_once()
        assert MockClient.call_args.kwargs.get("read_timeout_seconds") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
