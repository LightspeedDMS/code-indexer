"""
Git Merge Abort Recovery Tests - Story #13 AC2.

Tests for git_merge_abort operation:
- Abort in-progress merge with conflicts
- Handle case when no merge is in progress
- Verify clean state after abort

Uses REAL git operations - NO Python mocks for git commands.
All tests are idempotent via captured_state fixture.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    GitOperationsService,
)

# Mark all tests as destructive (they modify repository state)
pytestmark = pytest.mark.destructive


class TestGitMergeAbort:
    """Tests for git_merge_abort operation (AC2)."""

    def test_merge_abort_when_merge_in_progress(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC2: Abort an in-progress merge with conflicts."""
        # Create a branch with conflicting content
        subprocess.run(
            ["git", "checkout", "-b", "conflict-branch"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        conflict_file = local_test_repo / "conflict.txt"
        conflict_file.write_text("Branch content line 1\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Branch change"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Switch to main and create conflicting content
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        conflict_file.write_text("Main content line 1\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Main change"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Attempt merge (will conflict)
        subprocess.run(
            ["git", "merge", "conflict-branch"],
            cwd=local_test_repo,
            capture_output=True,
        )

        # Now abort the merge
        service = GitOperationsService()
        result = service.git_merge_abort(local_test_repo)

        assert result["success"] is True
        assert result["aborted"] is True

        # Verify we're back to clean state
        status = service.git_status(local_test_repo)
        assert status["staged"] == []
        assert status["unstaged"] == []

    def test_merge_abort_when_no_merge_fails(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC2: Abort fails when no merge is in progress."""
        service = GitOperationsService()

        with pytest.raises(GitCommandError) as exc_info:
            service.git_merge_abort(local_test_repo)

        assert "git merge --abort failed" in str(exc_info.value).lower()

    def test_merge_abort_removes_merge_head(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC2: After aborting merge, MERGE_HEAD is removed."""
        # Create and trigger a merge conflict
        subprocess.run(
            ["git", "checkout", "-b", "feature-conflict"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        merge_file = local_test_repo / "merge_test.txt"
        merge_file.write_text("Feature branch version\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Feature commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        subprocess.run(
            ["git", "checkout", "main"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        merge_file.write_text("Main branch version\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Main commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Trigger merge conflict
        subprocess.run(
            ["git", "merge", "feature-conflict"],
            cwd=local_test_repo,
            capture_output=True,
        )

        # Verify MERGE_HEAD exists (merge in progress)
        merge_head = local_test_repo / ".git" / "MERGE_HEAD"
        assert merge_head.exists()

        service = GitOperationsService()
        result = service.git_merge_abort(local_test_repo)
        assert result["success"] is True

        # Verify MERGE_HEAD removed
        assert not merge_head.exists()

    def test_merge_abort_restores_file_content(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC2: After aborting merge, file content is restored."""
        # Create conflicting branches
        subprocess.run(
            ["git", "checkout", "-b", "branch-for-conflict"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        conflict_file = local_test_repo / "content_test.txt"
        conflict_file.write_text("Branch version content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Branch commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        subprocess.run(
            ["git", "checkout", "main"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        conflict_file.write_text("Main version content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Main commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Trigger merge (creates conflict markers in file)
        subprocess.run(
            ["git", "merge", "branch-for-conflict"],
            cwd=local_test_repo,
            capture_output=True,
        )

        # Abort merge
        service = GitOperationsService()
        service.git_merge_abort(local_test_repo)

        # File should be restored to main's version
        assert conflict_file.read_text() == "Main version content\n"
