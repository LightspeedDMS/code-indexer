"""
Tests for SemanticSearchService._get_repository_path bug fix (Story #44).

BUG: _get_repository_path uses GoldenRepoManager.list_golden_repos() which returns
repos with `alias` field (e.g., "evolution"), but users pass global repo names
like "evolution-global" with `alias_name` field.

FIX: Use GlobalRegistry with `alias_name` lookup and AliasManager for current path.

These tests are written FIRST (TDD) to demonstrate the bug before implementing the fix.
"""

import inspect
import pytest
from unittest.mock import patch, MagicMock


class TestGetRepositoryPathBugFix:
    """Test SemanticSearchService._get_repository_path uses correct registry lookup."""

    def test_get_repository_path_source_uses_global_registry_not_golden_repo_manager(
        self,
    ):
        """
        BUG FIX VERIFICATION: _get_repository_path must use GlobalRegistry, not GoldenRepoManager.

        The bug is that it uses GoldenRepoManager.list_golden_repos() which returns repos
        with `alias` field (e.g., "evolution"), but users pass global repo names like
        "evolution-global" which need `alias_name` lookup from GlobalRegistry.

        This test reads the source code to verify correct classes are used.
        """
        from code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        # Read the source code of _get_repository_path
        source = inspect.getsource(service._get_repository_path)

        # Verify it does NOT use GoldenRepoManager anymore (the bug)
        assert (
            "GoldenRepoManager" not in source
        ), "_get_repository_path should NOT use GoldenRepoManager (causes alias mismatch)"

        # Verify it uses correct components:
        # 1. GlobalRegistry for looking up repos by alias_name
        assert (
            "GlobalRegistry" in source or "get_server_global_registry" in source
        ), "_get_repository_path should use GlobalRegistry or get_server_global_registry"

        # 2. AliasManager for getting current target path
        assert (
            "AliasManager" in source
        ), "_get_repository_path should use AliasManager for current target path"

    def test_get_repository_path_source_uses_alias_name_not_alias(self):
        """
        BUG FIX VERIFICATION: _get_repository_path must look up by 'alias_name', not 'alias'.

        GoldenRepoManager returns repos with 'alias' field (e.g., "evolution").
        GlobalRegistry returns repos with 'alias_name' field (e.g., "evolution-global").

        Users pass global repo names like "evolution-global", so we need 'alias_name' lookup.
        """
        from code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        # Read the source code of _get_repository_path
        source = inspect.getsource(service._get_repository_path)

        # Verify it uses alias_name for lookup (GlobalRegistry field), not alias (GoldenRepoManager field)
        assert (
            'alias_name' in source
        ), "_get_repository_path should look up by 'alias_name' (GlobalRegistry field)"

    def test_get_repository_path_error_message_says_global_not_golden(self):
        """
        BUG FIX VERIFICATION: Error messages should say 'global repositories' not 'golden repositories'.

        The old error message says "not found in golden repositories" which is confusing
        since users work with global repos (alias_name ending in -global).
        """
        from code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        # Read the source code of _get_repository_path
        source = inspect.getsource(service._get_repository_path)

        # Verify error messages use "global" terminology
        assert (
            "golden repositories" not in source.lower()
        ), "Error messages should not mention 'golden repositories'"

        # Should use "global repositories" for user-facing errors
        assert (
            "global repositor" in source.lower()
        ), "Error messages should reference 'global repositories'"


class TestGetRepositoryPathFunctional:
    """Functional tests for SemanticSearchService._get_repository_path with mocked dependencies."""

    def test_get_repository_path_finds_global_repo_by_alias_name(self, tmp_path):
        """
        _get_repository_path should find repo when passed alias_name (e.g., "my-repo-global").

        This tests the CORRECT behavior after the fix.
        """
        from code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        # Create a mock index directory
        index_dir = tmp_path / "indexes" / "my-repo_12345"
        index_dir.mkdir(parents=True)

        # Mock GlobalRegistry to return repo with alias_name
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "my-repo-global",
                "repo_name": "my-repo",
                "index_path": str(index_dir),
            }
        ]

        # Mock AliasManager to return the target path
        mock_alias_manager_instance = MagicMock()
        mock_alias_manager_instance.read_alias.return_value = str(index_dir)

        # Patch at the origin locations (since imports are lazy inside the method)
        with patch(
            "code_indexer.server.utils.registry_factory.get_server_global_registry",
            return_value=mock_registry,
        ), patch(
            "code_indexer.global_repos.alias_manager.AliasManager",
            return_value=mock_alias_manager_instance,
        ), patch(
            "code_indexer.server.services.search_service._get_golden_repos_dir",
            return_value=str(tmp_path / "golden-repos"),
        ):
            # This should find the repo using alias_name lookup
            result = service._get_repository_path("my-repo-global")

            assert result == str(index_dir)
            mock_registry.list_global_repos.assert_called_once()
            mock_alias_manager_instance.read_alias.assert_called_once_with("my-repo-global")

    def test_get_repository_path_raises_for_nonexistent_global_repo(self, tmp_path):
        """
        _get_repository_path should raise FileNotFoundError with 'global repositories' message.
        """
        from code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        # Mock GlobalRegistry to return empty list
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = []

        # Patch at origin location
        with patch(
            "code_indexer.server.utils.registry_factory.get_server_global_registry",
            return_value=mock_registry,
        ), patch(
            "code_indexer.server.services.search_service._get_golden_repos_dir",
            return_value=str(tmp_path / "golden-repos"),
        ):
            with pytest.raises(FileNotFoundError) as exc_info:
                service._get_repository_path("nonexistent-global")

            # Error message should mention "global repositories"
            error_msg = str(exc_info.value)
            assert (
                "global repositor" in error_msg.lower()
            ), f"Error should mention 'global repositories', got: {error_msg}"
            assert (
                "golden" not in error_msg.lower()
            ), f"Error should NOT mention 'golden', got: {error_msg}"

    def test_get_repository_path_raises_when_alias_has_no_target(self, tmp_path):
        """
        _get_repository_path should raise FileNotFoundError when alias exists but has no target.
        """
        from code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        # Mock GlobalRegistry to return repo
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "orphan-repo-global",
                "repo_name": "orphan-repo",
                "index_path": "/some/path",
            }
        ]

        # Mock AliasManager to return None (alias file missing or corrupted)
        mock_alias_manager_instance = MagicMock()
        mock_alias_manager_instance.read_alias.return_value = None

        # Patch at origin locations
        with patch(
            "code_indexer.server.utils.registry_factory.get_server_global_registry",
            return_value=mock_registry,
        ), patch(
            "code_indexer.global_repos.alias_manager.AliasManager",
            return_value=mock_alias_manager_instance,
        ), patch(
            "code_indexer.server.services.search_service._get_golden_repos_dir",
            return_value=str(tmp_path / "golden-repos"),
        ):
            with pytest.raises(FileNotFoundError) as exc_info:
                service._get_repository_path("orphan-repo-global")

            # Error message should mention alias
            error_msg = str(exc_info.value)
            assert (
                "alias" in error_msg.lower() or "orphan-repo-global" in error_msg.lower()
            ), f"Error should mention alias or repo name, got: {error_msg}"

    def test_get_repository_path_raises_when_target_path_does_not_exist(self, tmp_path):
        """
        _get_repository_path should raise FileNotFoundError when target path doesn't exist.
        """
        from code_indexer.server.services.search_service import SemanticSearchService

        service = SemanticSearchService()

        # Path that doesn't exist
        nonexistent_path = str(tmp_path / "does-not-exist")

        # Mock GlobalRegistry to return repo
        mock_registry = MagicMock()
        mock_registry.list_global_repos.return_value = [
            {
                "alias_name": "stale-repo-global",
                "repo_name": "stale-repo",
                "index_path": nonexistent_path,
            }
        ]

        # Mock AliasManager to return the nonexistent path
        mock_alias_manager_instance = MagicMock()
        mock_alias_manager_instance.read_alias.return_value = nonexistent_path

        # Patch at origin locations
        with patch(
            "code_indexer.server.utils.registry_factory.get_server_global_registry",
            return_value=mock_registry,
        ), patch(
            "code_indexer.global_repos.alias_manager.AliasManager",
            return_value=mock_alias_manager_instance,
        ), patch(
            "code_indexer.server.services.search_service._get_golden_repos_dir",
            return_value=str(tmp_path / "golden-repos"),
        ):
            with pytest.raises(FileNotFoundError) as exc_info:
                service._get_repository_path("stale-repo-global")

            # Error message should mention path
            error_msg = str(exc_info.value)
            assert (
                "does not exist" in error_msg.lower() or nonexistent_path in error_msg
            ), f"Error should mention path doesn't exist, got: {error_msg}"
