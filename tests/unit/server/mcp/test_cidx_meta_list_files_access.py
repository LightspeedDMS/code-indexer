"""
Unit tests for Bug #336 AC1: list_files on cidx-meta filters unauthorized repo
files for non-admin users.

TDD: Tests written FIRST before implementation (red phase).
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.mcp.handlers import list_files

from .conftest import extract_mcp_data, make_file_info, make_file_service_with_cidx_meta


class TestListFilesCidxMetaAccessFiltering:
    """AC1: list_files on cidx-meta is filtered for non-admin users."""

    def test_regular_user_sees_only_non_repo_files(
        self, regular_user, access_filtering_service
    ):
        """
        AC1: regular_user belongs to 'users' group (cidx-meta only).
        repo-a.md, repo-b.md, repo-c.md must be hidden; README.md passes through.
        """
        mock_file_service = make_file_service_with_cidx_meta()
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = list_files({"repository_alias": "cidx-meta"}, regular_user)

        data = extract_mcp_data(result)
        assert data["success"] is True
        file_paths = [f["path"] for f in data["files"]]
        assert "repo-a.md" not in file_paths
        assert "repo-b.md" not in file_paths
        assert "repo-c.md" not in file_paths
        assert "README.md" in file_paths

    def test_power_user_sees_only_accessible_repo_files(
        self, power_user, access_filtering_service
    ):
        """
        AC1: power_user belongs to 'powerusers' group (repo-a, repo-b, cidx-meta).
        repo-a.md and repo-b.md are visible; repo-c.md is hidden.
        """
        mock_file_service = make_file_service_with_cidx_meta()
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = list_files({"repository_alias": "cidx-meta"}, power_user)

        data = extract_mcp_data(result)
        assert data["success"] is True
        file_paths = [f["path"] for f in data["files"]]
        assert "repo-a.md" in file_paths
        assert "repo-b.md" in file_paths
        assert "README.md" in file_paths
        assert "repo-c.md" not in file_paths

    def test_non_cidx_meta_repo_is_not_filtered(
        self, regular_user, access_filtering_service
    ):
        """Non-cidx-meta repos must not be affected by this filtering logic."""
        mock_file_service = MagicMock()
        mock_file_service.list_files.return_value = MagicMock(
            files=[
                make_file_info("src/auth.py"),
                make_file_info("src/secret.py"),
            ]
        )
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = list_files(
                    {"repository_alias": "some-other-repo"}, regular_user
                )

        data = extract_mcp_data(result)
        assert data["success"] is True
        assert len(data["files"]) == 2

    def test_no_access_filtering_service_returns_all_files(self, regular_user):
        """If access_filtering_service is not configured, all files are returned."""
        mock_file_service = make_file_service_with_cidx_meta()
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=None,
            ):
                result = list_files({"repository_alias": "cidx-meta"}, regular_user)

        data = extract_mcp_data(result)
        assert data["success"] is True
        assert len(data["files"]) == 4  # all files returned when no service

