"""
Integration tests for Git Pull Operation.

Story #9 - Git Remote Operations Test Suite (AC2: git_pull)

Tests for git_pull operation:
- Pull and merge remote commits successfully
- Conflict handling when local and remote diverge

Uses REAL git operations - NO Python mocks for git commands.
Uses local bare remote set up by local_test_repo fixture - NO network access required.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_operations_service import GitOperationsService


class TestGitPull:
    """Tests for git_pull operation (AC2)."""

    def test_pull_remote_commits_successfully(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC2: Pull and merge remote commits successfully."""
        # Get the path to the bare remote
        temp_dir = local_test_repo.parent
        remote_path = temp_dir / "test-remote.git"

        # Create a second clone to simulate another user pushing commits
        second_clone = temp_dir / "second-clone-for-pull"
        if second_clone.exists():
            shutil.rmtree(second_clone)

        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote_path), str(second_clone)],
            check=True,
            capture_output=True,
        )

        # Configure git user in second clone
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

        # Make a commit in second clone and push it
        new_file = second_clone / "remote_new_file.txt"
        new_file.write_text("content from remote\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Remote commit to pull"],
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

        # Now pull in our local repo
        service = GitOperationsService()
        result = service.git_pull(local_test_repo, remote="origin", branch="main")

        assert result["success"] is True
        assert "updated_files" in result
        assert result["conflicts"] == []

        # Verify the file was actually pulled
        pulled_file = local_test_repo / "remote_new_file.txt"
        assert pulled_file.exists()
        assert pulled_file.read_text() == "content from remote\n"

    def test_pull_default_remote_and_branch(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC2: Default remote (origin) and branch handling."""
        service = GitOperationsService()

        # Pull when already up to date should succeed
        result = service.git_pull(local_test_repo, remote="origin")

        assert result["success"] is True

    def test_pull_nothing_to_pull(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC2: Pull when already up to date succeeds with no changes."""
        service = GitOperationsService()

        result = service.git_pull(local_test_repo, remote="origin", branch="main")

        assert result["success"] is True
        assert result["updated_files"] == 0
        assert result["conflicts"] == []

    def test_pull_with_merge_conflict(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC2: Conflict handling when local and remote diverge on same file."""
        # Get the path to the bare remote
        temp_dir = local_test_repo.parent
        remote_path = temp_dir / "test-remote.git"

        # Configure git to use merge (not rebase) for divergent branches
        # This is required in newer git versions to allow merge conflicts
        subprocess.run(
            ["git", "config", "pull.rebase", "false"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Create a second clone
        second_clone = temp_dir / "second-clone-for-conflict"
        if second_clone.exists():
            shutil.rmtree(second_clone)

        subprocess.run(
            ["git", "clone", "--branch", "main", str(remote_path), str(second_clone)],
            check=True,
            capture_output=True,
        )

        # Configure git user in second clone
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

        # Modify README.md in both clones to create a conflict
        # First, in second clone
        readme_second = second_clone / "README.md"
        readme_second.write_text("Remote version of README\nConflicting content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Remote README change"],
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

        # Then, modify same file in local repo
        readme_local = local_test_repo / "README.md"
        readme_local.write_text("Local version of README\nDifferent content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Local README change"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Now pull - should result in conflict
        service = GitOperationsService()
        result = service.git_pull(local_test_repo, remote="origin", branch="main")

        # Pull with conflict returns success=False and populates conflicts list
        assert result["success"] is False
        assert len(result["conflicts"]) > 0
        assert "README.md" in result["conflicts"]


class TestGitPullErrors:
    """Error case tests for git_pull operation (AC2)."""

    def test_pull_invalid_remote(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC2: Pull from non-existent remote fails gracefully."""
        service = GitOperationsService()

        # git_pull returns failure via success flag
        result = service.git_pull(local_test_repo, remote="nonexistent_remote")

        # Should indicate failure
        assert result["success"] is False
