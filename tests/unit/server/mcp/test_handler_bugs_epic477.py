"""Tests proving bugs in list_files, get_file_content, and repository statistics handlers.

These tests expose runtime errors caused by calling non-existent methods or using wrong parameters.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from code_indexer.server.mcp.handlers import list_files
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.models.api_models import FileListQueryParams
import json


@pytest.fixture
def mock_user():
    """Create a mock user for testing."""
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    return user


class TestListFilesBug:
    """Test list_files handler bug - wrong method parameters."""

    def test_list_files_expects_query_params_object_not_primitives(self, mock_user):
        """
        BUG FIX VERIFICATION: Handler must pass FileListQueryParams object.

        The actual service method signature is:
        list_files(repo_id: str, username: str, query_params: FileListQueryParams)

        Handler must pass query_params as FileListQueryParams instance (not primitive values).
        """
        params = {
            "repository_alias": "my-repo",
            "path": "src/",
        }

        # Create mock that enforces correct signature
        mock_file_service = MagicMock()

        # Track what arguments the handler actually passes
        received_query_params = None

        def capture_call(*args, **kwargs):
            nonlocal received_query_params
            # Check if query_params is passed (positional or keyword)
            if len(args) >= 3:
                received_query_params = args[2]
            elif "query_params" in kwargs:
                received_query_params = kwargs["query_params"]
            # Return valid response
            return {"files": [], "pagination": {"page": 1, "total": 0}}

        mock_file_service.list_files = Mock(side_effect=capture_call)

        with patch("code_indexer.server.app.file_service", mock_file_service):
            list_files(params, mock_user)

            # Verify the call was made
            assert mock_file_service.list_files.called

            # Verify query_params is FileListQueryParams instance
            assert received_query_params is not None, (
                "Handler must pass query_params parameter"
            )
            assert isinstance(received_query_params, FileListQueryParams), (
                f"query_params must be FileListQueryParams instance, got {type(received_query_params).__name__}"
            )

            # Verify query_params has expected values
            # Handler now builds recursive path pattern: src/ -> src/**/*
            assert received_query_params.path_pattern == "src/**/*", (
                f"Expected path_pattern='src/**/*', got '{received_query_params.path_pattern}'"
            )


class TestGetFileContentBug:
    """Test get_file_content handler bug - method doesn't exist."""

    def test_file_listing_service_has_get_file_content_method(self):
        """
        BUG FIX VERIFICATION: FileListingService now has get_file_content method.

        Previously the method didn't exist, now it should be implemented.
        """
        from code_indexer.server.services.file_service import file_service

        # Verify method now exists
        assert hasattr(file_service, "get_file_content"), (
            "FileListingService should have get_file_content method (bug is fixed)"
        )

        # Verify it's callable
        assert callable(file_service.get_file_content), (
            "get_file_content should be callable"
        )

    def test_get_file_content_handler_needs_working_service_method(self, mock_user):
        """
        Test that handler can call get_file_content successfully when implemented.

        This test will fail initially because the method doesn't exist,
        then will pass once we implement it.
        """
        from code_indexer.server.mcp.handlers import get_file_content

        params = {
            "repository_alias": "my-repo",
            "file_path": "src/main.py",
        }

        # Mock the service to have the method
        mock_file_service = MagicMock()
        mock_file_service.get_file_content = Mock(
            return_value={
                "content": "def main():\n    pass",
                "metadata": {"size": 100, "language": "python"},
            }
        )

        with patch("code_indexer.server.app.file_service", mock_file_service):
            result = get_file_content(params, mock_user)

            # Parse MCP response
            data = json.loads(result["content"][0]["text"])

            # Verify successful call
            assert data["success"] is True
            assert "file_content" in data
            # file_content should be array of content blocks per MCP spec
            assert isinstance(data["file_content"], list)
            assert len(data["file_content"]) > 0
            assert data["file_content"][0]["text"] == "def main():\n    pass"

            # Verify service was called with correct parameters
            # Story #33: Handler now passes offset, limit, and skip_truncation
            mock_file_service.get_file_content.assert_called_once_with(
                repository_alias="my-repo",
                file_path="src/main.py",
                username=mock_user.username,
                offset=None,
                limit=None,
                skip_truncation=True,
            )


class TestGetRepositoryStatisticsBug:
    """Test get_repository_statistics handler bug - uses wrong manager."""

    def test_stats_service_now_uses_activated_repo_manager(self):
        """
        BUG FIX VERIFICATION: RepositoryStatsService now uses ActivatedRepoManager.

        Previously used GoldenRepoManager, now uses ActivatedRepoManager.
        """
        from code_indexer.server.services.stats_service import RepositoryStatsService
        import inspect

        service = RepositoryStatsService()

        # Read the source code of _get_repository_path
        source = inspect.getsource(service._get_repository_path)

        # Verify it now uses ActivatedRepoManager (bug is fixed)
        assert "ActivatedRepoManager" in source, (
            "RepositoryStatsService should use ActivatedRepoManager (bug is fixed)"
        )

        # Verify it does NOT use GoldenRepoManager anymore
        assert "GoldenRepoManager" not in source, (
            "RepositoryStatsService should not use GoldenRepoManager anymore"
        )
