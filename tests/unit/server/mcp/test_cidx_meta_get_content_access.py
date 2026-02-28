"""
Unit tests for Bug #336 AC2: get_file_content on cidx-meta denies access to
unauthorized repo files for non-admin users.

TDD: Tests written FIRST before implementation (red phase).
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.mcp.handlers import get_file_content

from .conftest import extract_mcp_data


def _mock_file_service_with_content(content: str) -> MagicMock:
    """Return a mock file_service that returns the given content."""
    mock = MagicMock()
    mock.get_file_content.return_value = {"content": content, "metadata": {}}
    return mock


class TestGetFileContentCidxMetaAccessFiltering:
    """AC2: get_file_content on cidx-meta denies unauthorized repo files."""

    def test_regular_user_denied_for_unauthorized_repo_file(
        self, regular_user, access_filtering_service
    ):
        """
        AC2: regular_user (cidx-meta only) requests repo-a.md.
        Must receive success=False with an access-related error message.
        """
        mock_file_service = _mock_file_service_with_content("# repo-a description")
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            mock_app.app.state.payload_cache = None
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = get_file_content(
                    {"repository_alias": "cidx-meta", "file_path": "repo-a.md"},
                    regular_user,
                )

        data = extract_mcp_data(result)
        assert data["success"] is False
        error_lower = data.get("error", "").lower()
        assert "access" in error_lower or "denied" in error_lower or "authorized" in error_lower

    def test_power_user_denied_for_repo_outside_their_group(
        self, power_user, access_filtering_service
    ):
        """
        AC2: power_user (repo-a, repo-b) requests repo-c.md. Must be denied.
        """
        mock_file_service = _mock_file_service_with_content("# repo-c description")
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            mock_app.app.state.payload_cache = None
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = get_file_content(
                    {"repository_alias": "cidx-meta", "file_path": "repo-c.md"},
                    power_user,
                )

        data = extract_mcp_data(result)
        assert data["success"] is False

    def test_regular_user_allowed_for_non_repo_file(
        self, regular_user, access_filtering_service
    ):
        """
        AC2: regular_user may access README.md (not a repo-specific file).
        Must succeed.
        """
        mock_file_service = _mock_file_service_with_content("# cidx-meta README")
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            mock_app.app.state.payload_cache = None
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = get_file_content(
                    {"repository_alias": "cidx-meta", "file_path": "README.md"},
                    regular_user,
                )

        data = extract_mcp_data(result)
        assert data["success"] is True

    def test_power_user_allowed_for_accessible_repo_file(
        self, power_user, access_filtering_service
    ):
        """
        AC2: power_user (repo-a, repo-b) can read repo-a.md — it is accessible.
        """
        mock_file_service = _mock_file_service_with_content("# repo-a description")
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            mock_app.app.state.payload_cache = None
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=access_filtering_service,
            ):
                result = get_file_content(
                    {"repository_alias": "cidx-meta", "file_path": "repo-a.md"},
                    power_user,
                )

        data = extract_mcp_data(result)
        assert data["success"] is True

    def test_no_access_filtering_service_allows_all(self, regular_user):
        """If access_filtering_service is not configured, content is returned."""
        mock_file_service = _mock_file_service_with_content("# repo-a description")
        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            mock_app.app.state.payload_cache = None
            with patch(
                "code_indexer.server.mcp.handlers._get_access_filtering_service",
                return_value=None,
            ):
                result = get_file_content(
                    {"repository_alias": "cidx-meta", "file_path": "repo-a.md"},
                    regular_user,
                )

        data = extract_mcp_data(result)
        assert data["success"] is True
