"""
Unit tests for Bug #336 AC4: Admin users retain full access to all cidx-meta files.

TDD: Tests written FIRST before implementation (red phase).
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.mcp.handlers import browse_directory, get_file_content, list_files

from .conftest import extract_mcp_data, make_file_service_with_cidx_meta


class TestAdminFullAccessToCidxMeta:
    """AC4: Admin users retain full access to all cidx-meta files."""

    def test_admin_list_files_sees_all(self, admin_user, access_filtering_service):
        """AC4: Admin calling list_files on cidx-meta sees all four files."""
        mock_file_service = make_file_service_with_cidx_meta()
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = list_files({"repository_alias": "cidx-meta"}, admin_user)

        data = extract_mcp_data(result)
        assert data["success"] is True
        file_paths = [f["path"] for f in data["files"]]
        assert "repo-a.md" in file_paths
        assert "repo-b.md" in file_paths
        assert "repo-c.md" in file_paths
        assert "README.md" in file_paths

    def test_admin_get_file_content_allowed_for_any_repo_file(
        self, admin_user, access_filtering_service
    ):
        """AC4: Admin can read repo-c.md from cidx-meta without restriction."""
        mock_file_service = MagicMock()
        mock_file_service.get_file_content.return_value = {
            "content": "# repo-c description",
            "metadata": {},
        }
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            mock_app.app.state.payload_cache = None
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = get_file_content(
                    {"repository_alias": "cidx-meta", "file_path": "repo-c.md"},
                    admin_user,
                )

        data = extract_mcp_data(result)
        assert data["success"] is True

    def test_admin_browse_directory_sees_all(
        self, admin_user, access_filtering_service
    ):
        """AC4: Admin calling browse_directory on cidx-meta sees all entries."""
        mock_file_service = make_file_service_with_cidx_meta()
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = browse_directory(
                    {"repository_alias": "cidx-meta"}, admin_user
                )

        data = extract_mcp_data(result)
        assert data["success"] is True
        file_paths = [f["path"] for f in data["structure"]["files"]]
        assert "repo-a.md" in file_paths
        assert "repo-b.md" in file_paths
        assert "repo-c.md" in file_paths
        assert "README.md" in file_paths
