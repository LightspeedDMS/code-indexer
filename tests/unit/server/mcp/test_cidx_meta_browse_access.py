"""
Unit tests for Bug #336 AC3: browse_directory on cidx-meta filters unauthorized
repo entries for non-admin users.

TDD: Tests written FIRST before implementation (red phase).
"""

from unittest.mock import patch

from code_indexer.server.mcp.handlers import browse_directory

from .conftest import extract_mcp_data, make_file_service_with_cidx_meta


class TestBrowseDirectoryCidxMetaAccessFiltering:
    """AC3: browse_directory on cidx-meta is filtered for non-admin users."""

    def test_regular_user_sees_no_repo_files_in_browse(
        self, regular_user, access_filtering_service
    ):
        """
        AC3: regular_user (cidx-meta only) browses cidx-meta root.
        Only README.md should appear — no repo-specific .md files.
        """
        mock_file_service = make_file_service_with_cidx_meta()
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = browse_directory(
                    {"repository_alias": "cidx-meta"}, regular_user
                )

        data = extract_mcp_data(result)
        assert data["success"] is True
        file_paths = [f["path"] for f in data["structure"]["files"]]
        assert "repo-a.md" not in file_paths
        assert "repo-b.md" not in file_paths
        assert "repo-c.md" not in file_paths
        assert "README.md" in file_paths

    def test_power_user_sees_only_accessible_repo_files_in_browse(
        self, power_user, access_filtering_service
    ):
        """
        AC3: power_user (repo-a, repo-b) browses cidx-meta root.
        repo-a.md and repo-b.md visible; repo-c.md hidden.
        """
        mock_file_service = make_file_service_with_cidx_meta()
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = browse_directory(
                    {"repository_alias": "cidx-meta"}, power_user
                )

        data = extract_mcp_data(result)
        assert data["success"] is True
        file_paths = [f["path"] for f in data["structure"]["files"]]
        assert "repo-a.md" in file_paths
        assert "repo-b.md" in file_paths
        assert "README.md" in file_paths
        assert "repo-c.md" not in file_paths

    def test_no_access_filtering_service_returns_all_entries(self, regular_user):
        """If access_filtering_service is not configured, all entries are returned."""
        mock_file_service = make_file_service_with_cidx_meta()
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=None,
            ):
                result = browse_directory(
                    {"repository_alias": "cidx-meta"}, regular_user
                )

        data = extract_mcp_data(result)
        assert data["success"] is True
        assert len(data["structure"]["files"]) == 4  # all 4 files when no service
