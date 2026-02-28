"""
Unit tests for Bug #336 AC5: filter_cidx_meta_files() helper on
AccessFilteringService.

Tests the new method that decides which filenames from cidx-meta are visible
to a given user.

TDD: Tests written FIRST before implementation (red phase).
"""

from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole

from .conftest import extract_mcp_data, make_file_service_with_cidx_meta


class TestFilterCidxMetaFilesHelper:
    """AC5: Unit tests for AccessFilteringService.filter_cidx_meta_files()."""

    def test_admin_passes_all_files_through(self, access_filtering_service):
        """Admin user receives the full unmodified file list."""
        files = ["repo-a.md", "repo-b.md", "repo-c.md", "README.md"]
        result = access_filtering_service.filter_cidx_meta_files(files, "admin_user")
        assert result == files

    def test_regular_user_only_sees_non_repo_md_files(self, access_filtering_service):
        """regular_user (cidx-meta only) sees README.md but no repo .md files."""
        files = ["repo-a.md", "repo-b.md", "repo-c.md", "README.md"]
        result = access_filtering_service.filter_cidx_meta_files(
            files, "regular_user"
        )
        assert "repo-a.md" not in result
        assert "repo-b.md" not in result
        assert "repo-c.md" not in result
        assert "README.md" in result

    def test_power_user_sees_accessible_repo_files(self, access_filtering_service):
        """power_user (repo-a, repo-b) sees repo-a.md, repo-b.md, README.md."""
        files = ["repo-a.md", "repo-b.md", "repo-c.md", "README.md"]
        result = access_filtering_service.filter_cidx_meta_files(files, "power_user")
        assert "repo-a.md" in result
        assert "repo-b.md" in result
        assert "README.md" in result
        assert "repo-c.md" not in result

    def test_empty_file_list_returns_empty(self, access_filtering_service):
        """Empty input always produces empty output."""
        result = access_filtering_service.filter_cidx_meta_files([], "power_user")
        assert result == []

    def test_non_md_files_always_pass_through(self, access_filtering_service):
        """Files without .md extension (e.g. .gitignore) are always accessible."""
        files = [".gitignore", "repo-c.md", "README.txt"]
        result = access_filtering_service.filter_cidx_meta_files(
            files, "regular_user"
        )
        assert ".gitignore" in result
        assert "README.txt" in result
        assert "repo-c.md" not in result


class TestCidxMetaGlobalAliasFiltering:
    """Ensure cidx-meta-global alias also triggers file-level filtering."""

    def test_cidx_meta_global_list_files_filters_non_admin(
        self, access_filtering_service
    ):
        """
        The cidx-meta-global alias (ending in -global) must also have its
        files filtered for non-admin users via the 'cidx-meta' in alias check.
        """
        from code_indexer.server.mcp.handlers import list_files

        regular = User(
            username="regular_user",
            password_hash="hash",
            role=UserRole.NORMAL_USER,
            created_at=datetime.now(),
        )

        mock_file_service = make_file_service_with_cidx_meta()
        mock_alias_manager = Mock()
        mock_alias_manager.read_alias.return_value = "/fake/path/cidx-meta"

        with patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
            mock_app.file_service = mock_file_service
            with patch(
                "code_indexer.server.mcp.handlers.get_server_global_registry"
            ) as mock_registry_factory:
                mock_registry = Mock()
                mock_registry.list_global_repos.return_value = [
                    {"alias_name": "cidx-meta-global"}
                ]
                mock_registry_factory.return_value = mock_registry

                with patch(
                    "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                    return_value="/fake/golden-repos",
                ):
                    with patch(
                        "code_indexer.global_repos.alias_manager.AliasManager",
                        return_value=mock_alias_manager,
                    ):
                        with patch(
                            "code_indexer.server.mcp.handlers._get_access_filtering_service",
                            return_value=access_filtering_service,
                        ):
                            result = list_files(
                                {"repository_alias": "cidx-meta-global"},
                                regular,
                            )

        data = extract_mcp_data(result)
        assert data["success"] is True
        file_paths = [f["path"] for f in data["files"]]
        assert "repo-c.md" not in file_paths
        assert "README.md" in file_paths
