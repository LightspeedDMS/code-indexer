"""
Unit tests for Bug Fix #2: Add Index functionality with individual index types.

Tests the following acceptance criteria:
- Backend supports individual index types: semantic, fts, temporal, scip
- Backend removes semantic_fts combined type

TDD Approach: Tests written FIRST to define expected behavior, then implementation.
"""

import pytest
from unittest.mock import MagicMock


@pytest.fixture
def manager_with_repo(tmp_path):
    """Create a GoldenRepoManager with a test repository configured."""
    from code_indexer.server.repositories.golden_repo_manager import (
        GoldenRepoManager,
        GoldenRepo,
    )

    manager = GoldenRepoManager(data_dir=str(tmp_path))
    manager.background_job_manager = MagicMock()

    repo_path = tmp_path / "golden-repos" / "test-repo"
    repo_path.mkdir(parents=True)
    (repo_path / ".code-indexer").mkdir()

    manager.golden_repos["test-repo"] = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/test/repo.git",
        default_branch="main",
        clone_path=str(repo_path),
        created_at="2024-01-01T00:00:00Z",
    )

    return manager


class TestValidIndexTypesBackend:
    """Test that backend supports new individual index types."""

    @pytest.mark.parametrize("index_type", ["semantic", "fts", "temporal", "scip"])
    def test_valid_index_types_accepted(self, manager_with_repo, index_type):
        """AC: Backend accepts individual index types: semantic, fts, temporal, scip."""
        manager_with_repo.background_job_manager.submit_job.return_value = (
            f"job-{index_type}"
        )

        job_id = manager_with_repo.add_index_to_golden_repo(
            alias="test-repo", index_type=index_type, submitter_username="admin"
        )

        assert job_id == f"job-{index_type}"

    def test_semantic_fts_combined_type_not_valid(self, manager_with_repo):
        """AC: Backend rejects 'semantic_fts' combined type (removed)."""
        with pytest.raises(ValueError) as exc_info:
            manager_with_repo.add_index_to_golden_repo(
                alias="test-repo", index_type="semantic_fts", submitter_username="admin"
            )

        assert "semantic_fts" in str(exc_info.value).lower()
        assert "invalid" in str(exc_info.value).lower()
