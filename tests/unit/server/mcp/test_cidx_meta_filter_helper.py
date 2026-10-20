"""
Unit tests for Bug #336 AC5: filter_cidx_meta_files() helper on
AccessFilteringService.

Tests the new method that decides which filenames from cidx-meta are visible
to a given user.

TDD: Tests written FIRST before implementation (red phase).
"""

import pytest


@pytest.mark.slow
class TestFilterCidxMetaFilesHelper:
    """AC5: Unit tests for AccessFilteringService.filter_cidx_meta_files()."""

    def test_admin_passes_all_files_through(self, access_filtering_service):
        """Admin user receives the full unmodified file list."""
        files = ["repo-a.md", "repo-b.md", "repo-c.md", "README.md"]
        result = access_filtering_service.filter_cidx_meta_files(files, "admin_user")
        assert result == files

    def test_regular_user_sees_no_catalog_files(self, access_filtering_service):
        """regular_user (cidx-meta only) sees no description and no README.md."""
        files = ["repo-a.md", "repo-b.md", "repo-c.md", "README.md"]
        result = access_filtering_service.filter_cidx_meta_files(files, "regular_user")
        assert result == []

    def test_power_user_sees_accessible_repo_files(self, access_filtering_service):
        """power_user (repo-a, repo-b) sees exactly repo-a.md and repo-b.md."""
        files = ["repo-a.md", "repo-b.md", "repo-c.md", "README.md"]
        result = access_filtering_service.filter_cidx_meta_files(files, "power_user")
        assert result == ["repo-a.md", "repo-b.md"]

    def test_empty_file_list_returns_empty(self, access_filtering_service):
        """Empty input always produces empty output."""
        result = access_filtering_service.filter_cidx_meta_files([], "power_user")
        assert result == []

    def test_unlisted_files_are_denied(self, access_filtering_service):
        """Paths the catalog policy does not name (e.g. .gitignore) are denied."""
        files = [".gitignore", "repo-c.md", "README.txt"]
        result = access_filtering_service.filter_cidx_meta_files(files, "regular_user")
        assert result == []
