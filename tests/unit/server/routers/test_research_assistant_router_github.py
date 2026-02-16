"""
Unit tests for Research Assistant Router GitHub integration (Story #202).

Tests _get_github_token() helper function for retrieving tokens from CITokenManager.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_github_token_cache():
    """
    Reset module-level GitHub token cache before each test.

    Prevents test pollution where Test 1's cached token causes Tests 2 and 3
    to fail by returning the cached value instead of exercising mock setups.
    """
    import code_indexer.server.routers.research_assistant as ra_module
    ra_module._github_token_cache = None
    ra_module._github_token_cache_time = 0
    yield
    # Clean up after test as well
    ra_module._github_token_cache = None
    ra_module._github_token_cache_time = 0


class TestGetGitHubToken:
    """Test _get_github_token() helper function."""

    @patch("code_indexer.server.services.ci_token_manager.CITokenManager")
    @patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": "/test/server/dir"})
    def test_get_github_token_returns_token(self, mock_token_manager_class: MagicMock) -> None:
        """Test _get_github_token() returns token string when CITokenManager has a GitHub token."""
        # Import here to ensure patching is in effect
        from code_indexer.server.routers.research_assistant import _get_github_token

        # Setup mock
        mock_token_manager = MagicMock()
        mock_token_data = MagicMock()
        mock_token_data.token = "github_token_abc123"
        mock_token_manager.get_token.return_value = mock_token_data
        mock_token_manager_class.return_value = mock_token_manager

        # Execute
        result = _get_github_token()

        # Verify
        assert result == "github_token_abc123"
        mock_token_manager.get_token.assert_called_once_with("github")

    @patch("code_indexer.server.services.ci_token_manager.CITokenManager")
    @patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": "/test/server/dir"})
    def test_get_github_token_returns_none_when_missing(self, mock_token_manager_class: MagicMock) -> None:
        """Test _get_github_token() returns None when no GitHub token is stored."""
        # Import here to ensure patching is in effect
        from code_indexer.server.routers.research_assistant import _get_github_token

        # Setup mock - get_token returns None
        mock_token_manager = MagicMock()
        mock_token_manager.get_token.return_value = None
        mock_token_manager_class.return_value = mock_token_manager

        # Execute
        result = _get_github_token()

        # Verify
        assert result is None
        mock_token_manager.get_token.assert_called_once_with("github")

    @patch("code_indexer.server.services.ci_token_manager.CITokenManager")
    @patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": "/test/server/dir"})
    def test_get_github_token_returns_none_on_error(self, mock_token_manager_class: MagicMock) -> None:
        """Test _get_github_token() returns None when CITokenManager throws an exception."""
        # Import here to ensure patching is in effect
        from code_indexer.server.routers.research_assistant import _get_github_token

        # Setup mock to raise exception
        mock_token_manager_class.side_effect = Exception("Database connection failed")

        # Execute - should not raise
        result = _get_github_token()

        # Verify
        assert result is None
