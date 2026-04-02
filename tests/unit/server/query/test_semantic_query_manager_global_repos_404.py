"""
Unit tests for SemanticQueryManager global repos 404 bug.

This test file reproduces and fixes the issue where global repos are loaded
correctly but filtering by repository_alias returns 404.

The production code loads global repos from app.state.backend_registry
(cluster-aware, database-backed) rather than the old file-based GlobalRegistry.
These tests mock that path to verify correct behavior.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.query.semantic_query_manager import (
    SemanticQueryManager,
    SemanticQueryError,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

GLOBAL_REPO_ALIAS = "cidx-query-e2e-test-7f3a9b2c-global"
GLOBAL_REPO_URL = "https://github.com/jsbattig/tries.git"


@pytest.fixture
def temp_dirs():
    """Create temporary directories for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir) / "data"
        activated_repos_dir = data_dir / "activated-repos"
        activated_repos_dir.mkdir(parents=True, exist_ok=True)
        yield {
            "data_dir": str(data_dir),
            "activated_repos_dir": str(activated_repos_dir),
        }


@pytest.fixture
def activated_repo_manager_mock(temp_dirs):
    """Mock activated repo manager that returns empty user repos."""
    mock = MagicMock()
    mock.activated_repos_dir = temp_dirs["activated_repos_dir"]
    mock.list_activated_repositories.return_value = []
    return mock


@pytest.fixture
def background_job_manager_mock():
    """Mock background job manager."""
    mock = MagicMock()
    mock.submit_job.return_value = "test-job-id"
    return mock


@pytest.fixture
def semantic_query_manager(
    temp_dirs, activated_repo_manager_mock, background_job_manager_mock
):
    """Create SemanticQueryManager with mocked dependencies."""
    return SemanticQueryManager(
        data_dir=temp_dirs["data_dir"],
        activated_repo_manager=activated_repo_manager_mock,
        background_job_manager=background_job_manager_mock,
    )


def _make_backend_registry_mock(alias=GLOBAL_REPO_ALIAS, url=GLOBAL_REPO_URL):
    """Build a mock backend_registry with one global repo."""
    registry = MagicMock()
    registry.global_repos.list_repos.return_value = {
        alias: {
            "alias_name": alias,
            "repo_url": url,
            "repo_name": alias.replace("-global", ""),
        }
    }
    return registry


@pytest.fixture
def mock_backend_registry():
    """Patch app.state.backend_registry to return a mock with one global repo."""
    registry = _make_backend_registry_mock()
    mock_app = MagicMock()
    mock_app.state.backend_registry = registry
    with patch("code_indexer.server.app.app", mock_app):
        yield registry


class TestGlobalRepos404Bug:
    """Test suite for global repos 404 bug reproduction and fix."""

    def test_global_repos_are_loaded_from_backend_registry(
        self,
        semantic_query_manager,
        mock_backend_registry,
    ):
        """Global repos loaded via backend_registry are available for queries."""
        with patch.object(
            semantic_query_manager,
            "_perform_search",
            return_value=[],
        ):
            try:
                result = semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="test",
                    search_mode="semantic",
                    limit=10,
                )
                assert result is not None
            except SemanticQueryError as e:
                if "No activated repositories found" in str(e):
                    pytest.fail(
                        "Global repos were not loaded from backend_registry. "
                        "Expected global repos to be available even when user "
                        "has no activated repos."
                    )
                raise

    def test_global_repo_filtering_by_alias_succeeds(
        self,
        semantic_query_manager,
        mock_backend_registry,
    ):
        """Filtering by global repo alias finds the repo instead of returning 404."""
        with patch.object(
            semantic_query_manager,
            "_perform_search",
            return_value=[],
        ):
            try:
                result = semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="test",
                    repository_alias=GLOBAL_REPO_ALIAS,
                    search_mode="semantic",
                    limit=10,
                )
                assert result is not None
            except SemanticQueryError as e:
                pytest.fail(
                    f"Query should succeed for global repo alias. Got error: {e}"
                )

    def test_global_repo_structure_has_correct_user_alias_field(self):
        """
        Global repo dict formatted by query_user_repositories has user_alias
        matching the alias_name from backend_registry.
        """
        global_repo = {
            "alias_name": GLOBAL_REPO_ALIAS,
            "repo_url": GLOBAL_REPO_URL,
        }

        formatted_repo = {
            "user_alias": global_repo["alias_name"],
            "username": "global",
            "is_global": True,
            "repo_url": global_repo.get("repo_url", ""),
        }

        assert formatted_repo["user_alias"] == GLOBAL_REPO_ALIAS

    def test_merged_repos_list_contains_global_repos(
        self,
        semantic_query_manager,
        mock_backend_registry,
    ):
        """Global repos appear in the merged all_repos list passed to _perform_search."""
        captured_repos = []

        def capture_and_search(*args, **kwargs):
            if len(args) >= 2:
                captured_repos.extend(args[1])
            return []

        with patch.object(
            semantic_query_manager,
            "_perform_search",
            side_effect=capture_and_search,
        ):
            semantic_query_manager.query_user_repositories(
                username="testuser",
                query_text="test",
                search_mode="semantic",
                limit=10,
            )

        assert len(captured_repos) > 0, "Expected global repos to be included in search"
        global_repo_found = any(
            repo.get("user_alias") == GLOBAL_REPO_ALIAS for repo in captured_repos
        )
        assert global_repo_found, (
            f"Expected to find global repo with user_alias='{GLOBAL_REPO_ALIAS}' "
            f"in repos list. Found repos: {captured_repos}"
        )

    def test_filtering_preserves_global_repo_when_alias_matches(
        self,
        semantic_query_manager,
        mock_backend_registry,
    ):
        """Filtering by alias preserves the matching global repo in the list."""
        captured_repos = []

        def capture_and_search(*args, **kwargs):
            if len(args) >= 2:
                captured_repos.extend(args[1])
            return []

        with patch.object(
            semantic_query_manager,
            "_perform_search",
            side_effect=capture_and_search,
        ):
            try:
                semantic_query_manager.query_user_repositories(
                    username="testuser",
                    query_text="test",
                    repository_alias=GLOBAL_REPO_ALIAS,
                    search_mode="semantic",
                    limit=10,
                )
            except SemanticQueryError as e:
                pytest.fail(
                    f"Filtering should preserve global repo when alias matches. "
                    f"Got error: {e}. Captured repos: {captured_repos}"
                )

        assert len(captured_repos) == 1, (
            f"Expected exactly 1 repo after filtering. "
            f"Got {len(captured_repos)}: {captured_repos}"
        )
        assert captured_repos[0]["user_alias"] == GLOBAL_REPO_ALIAS
        assert captured_repos[0].get("is_global") is True


class TestGlobalReposFlow:
    """Flow tests for global repos with mocked backend_registry."""

    def test_complete_global_repo_query_flow(self, temp_dirs):
        """
        Full flow: backend_registry has a global repo, query by alias succeeds.
        """
        activated_repo_manager = MagicMock()
        activated_repo_manager.activated_repos_dir = temp_dirs["activated_repos_dir"]
        activated_repo_manager.list_activated_repositories.return_value = []

        manager = SemanticQueryManager(
            data_dir=temp_dirs["data_dir"],
            activated_repo_manager=activated_repo_manager,
            background_job_manager=MagicMock(),
        )

        registry = _make_backend_registry_mock(
            alias="test-repo-global",
            url="https://example.com/repo.git",
        )
        mock_app = MagicMock()
        mock_app.state.backend_registry = registry

        with patch("code_indexer.server.app.app", mock_app):
            with patch.object(manager, "_perform_search", return_value=[]):
                try:
                    result = manager.query_user_repositories(
                        username="testuser",
                        query_text="test query",
                        repository_alias="test-repo-global",
                        search_mode="semantic",
                        limit=10,
                    )
                    assert result is not None
                except SemanticQueryError as e:
                    pytest.fail(
                        f"Full flow should work for global repos. Got error: {e}"
                    )
