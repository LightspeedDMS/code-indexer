"""
Git Reset Recovery Tests - Story #13 AC3.

Tests for git_reset operation with confirmation token:
- Soft reset (keeps changes staged) - no token required
- Mixed reset (unstages changes) - no token required
- Hard reset (discards changes) - requires confirmation token
- Token validation (single-use, expiration)

Uses REAL git operations - NO Python mocks for git commands.
All tests are idempotent via captured_state fixture.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_operations_service import (
    GitOperationsService,
)

# Mark all tests as destructive (they modify repository state)
pytestmark = pytest.mark.destructive


class TestGitResetSoft:
    """Tests for git_reset soft mode (AC3)."""

    def test_reset_soft_without_token(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Soft reset works without confirmation token."""
        new_file = local_test_repo / unique_filename
        new_file.write_text("content for soft reset test\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit to reset from"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_reset(local_test_repo, mode="soft", commit_hash="HEAD~1")

        assert result["success"] is True
        assert result["reset_mode"] == "soft"
        assert result["target_commit"] == "HEAD~1"

        # Soft reset keeps changes staged
        status = service.git_status(local_test_repo)
        assert unique_filename in status["staged"]

    def test_reset_soft_preserves_file_content(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Soft reset preserves file content."""
        new_file = local_test_repo / unique_filename
        file_content = "unique content for preservation test\n"
        new_file.write_text(file_content)
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit with content"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        service.git_reset(local_test_repo, mode="soft", commit_hash="HEAD~1")

        # File content preserved
        assert new_file.exists()
        assert new_file.read_text() == file_content


class TestGitResetMixed:
    """Tests for git_reset mixed mode (AC3)."""

    def test_reset_mixed_without_token(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Mixed reset works without confirmation token."""
        new_file = local_test_repo / unique_filename
        new_file.write_text("content for mixed reset test\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit to reset from"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_reset(local_test_repo, mode="mixed", commit_hash="HEAD~1")

        assert result["success"] is True
        assert result["reset_mode"] == "mixed"

        # Mixed reset unstages (new files become untracked)
        status = service.git_status(local_test_repo)
        assert unique_filename not in status["staged"]
        assert unique_filename in status["untracked"]

    def test_reset_mixed_default_to_head(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Mixed reset without commit_hash defaults to HEAD."""
        new_file = local_test_repo / unique_filename
        new_file.write_text("staged content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_reset(local_test_repo, mode="mixed")

        assert result["success"] is True
        assert result["target_commit"] == "HEAD"

        # File is unstaged
        status = service.git_status(local_test_repo)
        assert unique_filename not in status["staged"]
        assert unique_filename in status["untracked"]


class TestGitResetHard:
    """Tests for git_reset hard mode with confirmation token (AC3)."""

    def test_reset_hard_without_token_returns_confirmation(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Hard reset without token returns confirmation requirement."""
        new_file = local_test_repo / unique_filename
        new_file.write_text("content for hard reset\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit to reset"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_reset(local_test_repo, mode="hard", commit_hash="HEAD~1")

        assert result["requires_confirmation"] is True
        assert "token" in result
        assert len(result["token"]) == 6

    def test_reset_hard_with_valid_token_executes(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Hard reset with valid token executes."""
        new_file = local_test_repo / unique_filename
        new_file.write_text("content to be lost on hard reset\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit to reset"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Get token
        token_result = service.git_reset(
            local_test_repo, mode="hard", commit_hash="HEAD~1"
        )
        token = token_result["token"]

        # Execute with token
        result = service.git_reset(
            local_test_repo,
            mode="hard",
            commit_hash="HEAD~1",
            confirmation_token=token,
        )

        assert result["success"] is True
        assert result["reset_mode"] == "hard"
        assert result["target_commit"] == "HEAD~1"

        # Hard reset removes the file
        assert not new_file.exists()

    def test_reset_hard_invalid_token_fails(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Hard reset with invalid token raises ValueError."""
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        with pytest.raises(ValueError) as exc_info:
            service.git_reset(
                local_test_repo,
                mode="hard",
                commit_hash="HEAD~1",
                confirmation_token="INVALID",
            )

        assert "invalid or expired" in str(exc_info.value).lower()

    def test_reset_token_single_use(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Token can only be used once."""
        # Create two commits
        for i in range(2):
            filename = f"{unique_filename}_{i}.txt"
            new_file = local_test_repo / filename
            new_file.write_text(f"content {i}\n")
            subprocess.run(
                ["git", "add", "."],
                cwd=local_test_repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"Commit {i}"],
                cwd=local_test_repo,
                check=True,
                capture_output=True,
            )

        service = GitOperationsService()

        # Get and use token
        token_result = service.git_reset(
            local_test_repo, mode="hard", commit_hash="HEAD~1"
        )
        token = token_result["token"]

        service.git_reset(
            local_test_repo,
            mode="hard",
            commit_hash="HEAD~1",
            confirmation_token=token,
        )

        # Try to reuse token - should fail
        with pytest.raises(ValueError) as exc_info:
            service.git_reset(
                local_test_repo,
                mode="hard",
                commit_hash="HEAD~1",
                confirmation_token=token,
            )

        assert "invalid or expired" in str(exc_info.value).lower()


class TestGitResetSpecificCommit:
    """Tests for git_reset to specific commit hash (AC3)."""

    def test_reset_to_specific_commit_hash(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC3: Reset to specific commit hash works."""
        # Get initial HEAD
        initial_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Create a commit
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", "."],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "New commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Soft reset to specific commit hash
        result = service.git_reset(
            local_test_repo, mode="soft", commit_hash=initial_head
        )

        assert result["success"] is True
        assert result["target_commit"] == initial_head

        # Verify HEAD is at original commit
        current_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert current_head == initial_head
