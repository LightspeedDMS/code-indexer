"""
Integration tests for Git Fetch Operation.

Story #9 - Git Remote Operations Test Suite (AC3: git_fetch)

Tests for git_fetch operation:
- Fetch refs from remote without merging
- Update remote tracking refs

Uses REAL git operations - NO Python mocks for git commands.
Uses local bare remote set up by local_test_repo fixture - NO network access required.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    GitOperationsService,
)


class TestGitFetch:
    """Tests for git_fetch operation (AC3)."""

    def test_fetch_refs_from_remote(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC3: Fetch refs from remote successfully."""
        service = GitOperationsService()
        result = service.git_fetch(local_test_repo, remote="origin")

        assert result["success"] is True
        assert "fetched_refs" in result
        assert isinstance(result["fetched_refs"], list)

    def test_fetch_updates_remote_tracking_refs(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC3: Fetch updates remote tracking refs."""
        # Get the path to the bare remote
        temp_dir = local_test_repo.parent
        remote_path = temp_dir / "test-remote.git"

        # Create a second clone and push a new commit
        second_clone = temp_dir / "second-clone-for-fetch"
        if second_clone.exists():
            shutil.rmtree(second_clone)

        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote_path), str(second_clone)],
            check=True,
            capture_output=True,
        )

        # Configure git user
        subprocess.run(
            ["git", "config", "user.email", "other@example.com"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Other User"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )

        # Create and push new commit
        fetch_test_file = second_clone / "fetch_test_file.txt"
        fetch_test_file.write_text("content for fetch test\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit for fetch test"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )

        # Get local HEAD before fetch
        local_head_before = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Fetch should update origin/main without changing local HEAD
        service = GitOperationsService()
        result = service.git_fetch(local_test_repo, remote="origin")

        assert result["success"] is True

        # Local HEAD should be unchanged (fetch doesn't merge)
        local_head_after = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert local_head_after == local_head_before

        # But origin/main should be updated
        origin_main = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # origin/main should have new commit (different from local HEAD)
        assert origin_main != local_head_before

    def test_fetch_does_not_modify_working_directory(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC3: Fetch does not modify local working directory or branch."""
        # Create a local modification (but don't commit)
        local_file = local_test_repo / "local_uncommitted.txt"
        local_file.write_text("uncommitted local content\n")

        service = GitOperationsService()
        result = service.git_fetch(local_test_repo, remote="origin")

        assert result["success"] is True

        # Local uncommitted file should still exist
        assert local_file.exists()
        assert local_file.read_text() == "uncommitted local content\n"

    def test_fetch_default_remote(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC3: Fetch uses default remote (origin) when not specified."""
        service = GitOperationsService()

        # The service method has default remote="origin"
        result = service.git_fetch(local_test_repo)

        assert result["success"] is True


class TestGitFetchErrors:
    """Error case tests for git_fetch operation (AC3)."""

    def test_fetch_invalid_remote(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC3: Fetch from non-existent remote raises GitCommandError."""
        service = GitOperationsService()

        with pytest.raises(GitCommandError) as exc_info:
            service.git_fetch(local_test_repo, remote="nonexistent_remote")

        assert "fetch" in str(exc_info.value).lower() or exc_info.value.returncode != 0
