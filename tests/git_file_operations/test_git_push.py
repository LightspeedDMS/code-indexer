"""
Integration tests for Git Push Operation.

Story #9 - Git Remote Operations Test Suite (AC1: git_push)

Tests for git_push operation:
- Push local commits to remote successfully
- Push rejection when remote has new commits (non-fast-forward)
- Default remote/branch handling

Uses REAL git operations - NO Python mocks for git commands.
Uses local bare remote set up by local_test_repo fixture - NO network access required.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    GitOperationsService,
)


class TestGitPush:
    """Tests for git_push operation (AC1)."""

    def test_push_local_commits_to_remote(
        self,
        local_test_repo: Path,
        synced_remote_state,
        unique_filename: str,
    ):
        """AC1: Push local commits to remote successfully."""
        # Create and commit a file
        test_file = local_test_repo / unique_filename
        test_file.write_text("content to push\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Test commit for push"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_push(local_test_repo, remote="origin", branch="main")

        assert result["success"] is True
        assert "pushed_commits" in result

    def test_push_default_remote_and_branch(
        self,
        local_test_repo: Path,
        synced_remote_state,
        unique_filename: str,
    ):
        """AC1: Default remote (origin) and branch handling when not specified."""
        # Create and commit a file
        test_file = local_test_repo / unique_filename
        test_file.write_text("default push content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Default remote test"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        # Push without specifying branch (uses current branch)
        result = service.git_push(local_test_repo, remote="origin")

        assert result["success"] is True

    def test_push_nothing_to_push(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC1: Push when there's nothing new to push succeeds (no-op)."""
        service = GitOperationsService()

        # Push when already up to date should succeed
        result = service.git_push(local_test_repo, remote="origin", branch="main")

        assert result["success"] is True
        assert result["pushed_commits"] == 0

    def test_push_multiple_commits(
        self,
        local_test_repo: Path,
        synced_remote_state,
    ):
        """AC1: Push multiple local commits to remote."""
        # Create multiple commits
        for i in range(3):
            test_file = local_test_repo / f"multi_commit_file_{i}.txt"
            test_file.write_text(f"content {i}\n")
            subprocess.run(
                ["git", "add", "."],
                cwd=local_test_repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Multi commit {i}"],
                cwd=local_test_repo,
                check=True,
                capture_output=True,
            )

        service = GitOperationsService()
        result = service.git_push(local_test_repo, remote="origin", branch="main")

        assert result["success"] is True


class TestGitPushErrors:
    """Error case tests for git_push operation (AC1)."""

    def test_push_invalid_remote(
        self,
        local_test_repo: Path,
        synced_remote_state,
        unique_filename: str,
    ):
        """AC1: Push to non-existent remote raises GitCommandError."""
        # Create a commit to push
        test_file = local_test_repo / unique_filename
        test_file.write_text("content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Test commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        with pytest.raises(GitCommandError) as exc_info:
            service.git_push(local_test_repo, remote="nonexistent_remote")

        assert "push" in str(exc_info.value).lower()

    def test_push_rejection_when_remote_has_diverged(
        self,
        local_test_repo: Path,
        synced_remote_state,
        unique_filename: str,
    ):
        """
        AC1: Push rejection when remote has new commits (non-fast-forward).

        Simulates the scenario where another user pushed to the remote
        while we have local commits.
        """
        import shutil

        # Get the path to the bare remote
        temp_dir = local_test_repo.parent
        remote_path = temp_dir / "test-remote.git"

        # Create a second clone to simulate another user
        second_clone = temp_dir / "second-clone"
        # Clean up if it exists from a previous test run
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

        # Make a commit in second clone and push it (simulating another user)
        other_file = second_clone / "other_user_file.txt"
        other_file.write_text("other user's content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=second_clone,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Other user commit"],
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

        # Now make a local commit in our repo (this will diverge from remote)
        local_file = local_test_repo / unique_filename
        local_file.write_text("local content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Local commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Push should fail because remote has diverged (non-fast-forward)
        with pytest.raises(GitCommandError) as exc_info:
            service.git_push(local_test_repo, remote="origin", branch="main")

        # The error should indicate rejection
        error_msg = str(exc_info.value).lower()
        assert "push" in error_msg or exc_info.value.returncode != 0
