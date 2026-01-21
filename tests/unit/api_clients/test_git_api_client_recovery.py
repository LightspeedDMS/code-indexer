"""
Unit tests for GitAPIClient recovery methods.

Story #737: CLI remote mode git workflow operations.
Testing reset, clean, merge_abort, checkout_file methods.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestGitAPIClientRecoveryMethods:
    """Tests for git recovery methods."""

    @pytest.fixture
    def git_client(self):
        """Create a GitAPIClient for testing."""
        from code_indexer.api_clients.git_client import GitAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return GitAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_reset_method_exists(self, git_client):
        """Test that reset method exists."""
        assert hasattr(git_client, "reset")
        assert callable(git_client.reset)

    @pytest.mark.asyncio
    async def test_reset_calls_correct_endpoint(self, git_client):
        """Test reset calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "reset_mode": "mixed",
                "target_commit": "HEAD",
            }
            mock_request.return_value = mock_response

            await git_client.reset("test-repo", mode="mixed")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/git/reset" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_clean_method_exists(self, git_client):
        """Test that clean method exists."""
        assert hasattr(git_client, "clean")
        assert callable(git_client.clean)

    @pytest.mark.asyncio
    async def test_clean_calls_correct_endpoint(self, git_client):
        """Test clean calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "removed_files": [],
            }
            mock_request.return_value = mock_response

            await git_client.clean("test-repo", confirmation_token="token")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/git/clean" in call_args[0][1]
