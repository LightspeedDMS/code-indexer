"""
Tests for RepositoryStatsService.get_repository_metadata bug fix (Story #46).

BUG: get_repository_metadata uses GoldenRepoManager.list_golden_repos() which returns
repos with `alias` field (e.g., "evolution"), but users pass global repo names
like "evolution-global" with `alias_name` field.

FIX: Use GlobalRegistry with `alias_name` lookup and AliasManager for current path.

These tests are written FIRST (TDD) to demonstrate the bug before implementing the fix.
"""

import inspect
import pytest
from unittest.mock import patch, MagicMock


class TestGetRepositoryMetadataBugFix:
    """Test RepositoryStatsService.get_repository_metadata uses correct registry lookup."""

    def test_get_repository_metadata_source_uses_global_registry_not_golden_repo_manager(
        self,
    ):
        """
        BUG FIX VERIFICATION: get_repository_metadata must use GlobalRegistry, not GoldenRepoManager.

        The bug is that it uses GoldenRepoManager.list_golden_repos() which returns repos
        with `alias` field (e.g., "evolution"), but users pass global repo names like
        "evolution-global" which need `alias_name` lookup from GlobalRegistry.

        This test reads the source code to verify correct classes are used.
        """
        from code_indexer.server.services.stats_service import RepositoryStatsService

        service = RepositoryStatsService()

        # Read the source code of get_repository_metadata
        source = inspect.getsource(service.get_repository_metadata)

        # Verify it does NOT use GoldenRepoManager anymore (the bug)
        assert (
            "GoldenRepoManager" not in source
        ), "get_repository_metadata should NOT use GoldenRepoManager (causes alias mismatch)"

        # Verify it uses correct components:
        # 1. GlobalRegistry for looking up repos by alias_name
        assert (
            "GlobalRegistry" in source or "get_server_global_registry" in source
        ), "get_repository_metadata should use GlobalRegistry or get_server_global_registry"

        # 2. AliasManager for getting current target path
        assert (
            "AliasManager" in source
        ), "get_repository_metadata should use AliasManager for current target path"

    def test_get_repository_metadata_source_uses_alias_name_not_alias(self):
        """
        BUG FIX VERIFICATION: get_repository_metadata must look up by 'alias_name', not 'alias'.

        GoldenRepoManager returns repos with 'alias' field (e.g., "evolution").
        GlobalRegistry returns repos with 'alias_name' field (e.g., "evolution-global").

        Users pass global repo names like "evolution-global", so we need 'alias_name' lookup.
        """
        from code_indexer.server.services.stats_service import RepositoryStatsService

        service = RepositoryStatsService()

        # Read the source code of get_repository_metadata
        source = inspect.getsource(service.get_repository_metadata)

        # Verify it uses alias_name for lookup (GlobalRegistry field), not alias (GoldenRepoManager field)
        assert (
            "alias_name" in source
        ), "get_repository_metadata should look up by 'alias_name' (GlobalRegistry field)"

    def test_get_repository_metadata_error_message_says_global_not_golden(self):
        """
        BUG FIX VERIFICATION: Error messages should say 'global repositories' not 'golden repositories'.

        The old error message says "not found in golden repositories" which is confusing
        since users work with global repos (alias_name ending in -global).
        """
        from code_indexer.server.services.stats_service import RepositoryStatsService

        service = RepositoryStatsService()

        # Read the source code of get_repository_metadata
        source = inspect.getsource(service.get_repository_metadata)

        # Verify error messages use "global" terminology
        assert (
            "golden repositories" not in source.lower()
        ), "Error messages should not mention 'golden repositories'"

        # Should use "global repositories" for user-facing errors
        assert (
            "global repositor" in source.lower()
        ), "Error messages should reference 'global repositories'"


class TestGetRepositoryMetadataFunctional:
    """Functional tests for RepositoryStatsService.get_repository_metadata with mocked dependencies."""

    def test_get_repository_metadata_finds_global_repo_by_alias_name(self, tmp_path):
        """
        get_repository_metadata should find repo when passed alias_name (e.g., "my-repo-global").

        This tests the CORRECT behavior after the fix.
        """
        from code_indexer.server.services.stats_service import RepositoryStatsService

        service = RepositoryStatsService()

        # Create a mock index directory
        index_dir = tmp_path / "indexes" / "my-repo_12345"
        index_dir.mkdir(parents=True)

        # Mock GlobalRegistry to return repo with alias_name
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_name": "my-repo",
                "repo_url": "https://github.com/org/my-repo.git",
                "default_branch": "main",
                "created_at": "2024-01-01T00:00:00Z",
                "index_path": str(index_dir),
            }
        ]

        # Mock AliasManager to return the target path
        mock_alias_manager_instance = MagicMock()
        mock_alias_manager_instance.read_alias.return_value = str(index_dir)

        # Set up golden_repos_dir for helper function
        golden_repos_dir = str(tmp_path / "golden-repos")

        # Patch at the origin locations (since imports are lazy inside the method)
        with (
            patch(
                "code_indexer.server.utils.registry_factory.get_server_global_registry",
                return_value=mock_registry,
            ),
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
                return_value=mock_alias_manager_instance,
            ),
            patch(
                "code_indexer.server.services.stats_service._get_golden_repos_dir",
                return_value=golden_repos_dir,
            ),
        ):
            # This should find the repo using alias_name lookup
            result = service.get_repository_metadata("my-repo-global")

            # Verify result contains expected metadata
            assert result is not None
            assert result["repo_url"] == "https://github.com/org/my-repo.git"
            assert result["default_branch"] == "main"
            assert result["created_at"] == "2024-01-01T00:00:00Z"
            assert result["clone_path"] == str(index_dir)

            # Verify correct methods were called
            mock_registry.list_global_repos.assert_called_once()
            mock_alias_manager_instance.read_alias.assert_called_once_with(
                "my-repo-global"
            )

    def test_get_repository_metadata_raises_for_nonexistent_global_repo(self, tmp_path):
        """
        get_repository_metadata should raise FileNotFoundError with 'global repositories' message.
        """
        from code_indexer.server.services.stats_service import RepositoryStatsService

        service = RepositoryStatsService()

        # Mock GlobalRegistry to return empty list
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = []

        # Set up golden_repos_dir for helper function
        golden_repos_dir = str(tmp_path / "golden-repos")

        # Patch at origin location
        with (
            patch(
                "code_indexer.server.utils.registry_factory.get_server_global_registry",
                return_value=mock_registry,
            ),
            patch(
                "code_indexer.server.services.stats_service._get_golden_repos_dir",
                return_value=golden_repos_dir,
            ),
        ):
            with pytest.raises(FileNotFoundError) as exc_info:
                service.get_repository_metadata("nonexistent-global")

            # Error message should mention "global repositories"
            error_msg = str(exc_info.value)
            assert (
                "global repositor" in error_msg.lower()
            ), f"Error should mention 'global repositories', got: {error_msg}"
            assert (
                "golden" not in error_msg.lower()
            ), f"Error should NOT mention 'golden', got: {error_msg}"

    def test_get_repository_metadata_raises_when_alias_has_no_target(self, tmp_path):
        """
        get_repository_metadata should raise FileNotFoundError when alias exists but has no target.
        """
        from code_indexer.server.services.stats_service import RepositoryStatsService

        service = RepositoryStatsService()

        # Mock GlobalRegistry to return repo
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "orphan-repo-global",
                "repo_name": "orphan-repo",
                "repo_url": "https://github.com/org/orphan-repo.git",
                "default_branch": "main",
                "created_at": "2024-01-01T00:00:00Z",
                "index_path": "/some/path",
            }
        ]

        # Mock AliasManager to return None (alias file missing or corrupted)
        mock_alias_manager_instance = MagicMock()
        mock_alias_manager_instance.read_alias.return_value = None

        # Set up golden_repos_dir for helper function
        golden_repos_dir = str(tmp_path / "golden-repos")

        # Patch at origin locations
        with (
            patch(
                "code_indexer.server.utils.registry_factory.get_server_global_registry",
                return_value=mock_registry,
            ),
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
                return_value=mock_alias_manager_instance,
            ),
            patch(
                "code_indexer.server.services.stats_service._get_golden_repos_dir",
                return_value=golden_repos_dir,
            ),
        ):
            with pytest.raises(FileNotFoundError) as exc_info:
                service.get_repository_metadata("orphan-repo-global")

            # Error message should mention alias
            error_msg = str(exc_info.value)
            assert (
                "alias" in error_msg.lower()
                or "orphan-repo-global" in error_msg.lower()
            ), f"Error should mention alias or repo name, got: {error_msg}"

    def test_get_repository_metadata_returns_correct_structure(self, tmp_path):
        """
        get_repository_metadata should return dict with all expected keys.
        """
        from code_indexer.server.services.stats_service import RepositoryStatsService

        service = RepositoryStatsService()

        # Create a mock index directory
        index_dir = tmp_path / "indexes" / "test-repo_12345"
        index_dir.mkdir(parents=True)

        # Mock GlobalRegistry to return repo with alias_name
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "test-repo-global",
                "repo_name": "test-repo",
                "repo_url": "https://github.com/org/test-repo.git",
                "default_branch": "develop",
                "created_at": "2024-06-15T10:30:00Z",
                "index_path": str(index_dir),
            }
        ]

        # Mock AliasManager to return the target path
        mock_alias_manager_instance = MagicMock()
        mock_alias_manager_instance.read_alias.return_value = str(index_dir)

        # Set up golden_repos_dir for helper function
        golden_repos_dir = str(tmp_path / "golden-repos")

        # Patch at the origin locations
        with (
            patch(
                "code_indexer.server.utils.registry_factory.get_server_global_registry",
                return_value=mock_registry,
            ),
            patch(
                "code_indexer.global_repos.alias_manager.AliasManager",
                return_value=mock_alias_manager_instance,
            ),
            patch(
                "code_indexer.server.services.stats_service._get_golden_repos_dir",
                return_value=golden_repos_dir,
            ),
        ):
            result = service.get_repository_metadata("test-repo-global")

            # Verify all expected keys are present
            expected_keys = {
                "created_at",
                "last_sync_at",
                "sync_count",
                "repo_url",
                "default_branch",
                "clone_path",
            }
            assert (
                set(result.keys()) == expected_keys
            ), f"Expected keys {expected_keys}, got {set(result.keys())}"

            # Verify specific values
            assert result["created_at"] == "2024-06-15T10:30:00Z"
            assert result["repo_url"] == "https://github.com/org/test-repo.git"
            assert result["default_branch"] == "develop"
            assert result["clone_path"] == str(index_dir)
            # last_sync_at and sync_count are placeholder values for now
            assert result["last_sync_at"] is None
            assert result["sync_count"] == 0
