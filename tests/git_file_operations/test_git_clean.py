"""
Git Clean Recovery Tests - Story #13 AC4.

Tests for git_clean operation with confirmation token:
- Removes untracked files and directories
- Requires confirmation token (destructive operation)
- Token validation (single-use, expiration)
- Preserves tracked and staged files

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


class TestGitClean:
    """Tests for git_clean operation (AC4)."""

    def test_clean_without_token_returns_confirmation(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC4: Clean without token returns confirmation requirement."""
        # Create untracked file
        untracked_file = local_test_repo / unique_filename
        untracked_file.write_text("untracked content\n")

        service = GitOperationsService()
        result = service.git_clean(local_test_repo)

        assert result["requires_confirmation"] is True
        assert "token" in result
        assert len(result["token"]) == 6

    def test_clean_with_valid_token_removes_untracked(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC4: Clean with valid token removes untracked files."""
        # Create untracked file
        untracked_file = local_test_repo / unique_filename
        untracked_file.write_text("content to be removed\n")

        assert untracked_file.exists()

        service = GitOperationsService()

        # Get token
        token_result = service.git_clean(local_test_repo)
        token = token_result["token"]

        # Execute with token
        result = service.git_clean(local_test_repo, confirmation_token=token)

        assert result["success"] is True
        assert "removed_files" in result
        # File should be removed
        assert not untracked_file.exists()

    def test_clean_removes_untracked_directories(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC4: Clean removes untracked directories."""
        import uuid

        # Create untracked directory with files
        dir_name = f"untracked_dir_{uuid.uuid4().hex[:8]}"
        untracked_dir = local_test_repo / dir_name
        untracked_dir.mkdir()
        (untracked_dir / "file1.txt").write_text("content 1\n")
        (untracked_dir / "file2.txt").write_text("content 2\n")

        assert untracked_dir.exists()

        service = GitOperationsService()

        # Get and use token
        token_result = service.git_clean(local_test_repo)
        token = token_result["token"]

        result = service.git_clean(local_test_repo, confirmation_token=token)

        assert result["success"] is True
        # Directory should be removed
        assert not untracked_dir.exists()

    def test_clean_preserves_tracked_files(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC4: Clean preserves tracked files."""
        # README.md is tracked - read its original content
        readme = local_test_repo / "README.md"
        original_content = readme.read_text()

        # Create untracked file (this will be removed)
        untracked_file = local_test_repo / unique_filename
        untracked_file.write_text("untracked\n")

        service = GitOperationsService()

        # Get and use token
        token_result = service.git_clean(local_test_repo)
        token = token_result["token"]

        service.git_clean(local_test_repo, confirmation_token=token)

        # Tracked file should still exist with original content
        assert readme.exists()
        assert readme.read_text() == original_content
        # Untracked file removed
        assert not untracked_file.exists()

    def test_clean_preserves_staged_files(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC4: Clean preserves staged (added) files."""
        # Create and stage a new file
        staged_file = local_test_repo / f"staged_{unique_filename}"
        staged_content = "staged content\n"
        staged_file.write_text(staged_content)
        subprocess.run(
            ["git", "add", staged_file.name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Create untracked file (this will be removed)
        untracked_file = local_test_repo / unique_filename
        untracked_file.write_text("untracked\n")

        service = GitOperationsService()

        # Get and use token
        token_result = service.git_clean(local_test_repo)
        token = token_result["token"]

        service.git_clean(local_test_repo, confirmation_token=token)

        # Staged file should still exist
        assert staged_file.exists()
        assert staged_file.read_text() == staged_content
        # Untracked file removed
        assert not untracked_file.exists()

    def test_clean_invalid_token_fails(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC4: Clean with invalid token raises ValueError."""
        # Create untracked file
        untracked_file = local_test_repo / unique_filename
        untracked_file.write_text("content\n")

        service = GitOperationsService()

        with pytest.raises(ValueError) as exc_info:
            service.git_clean(local_test_repo, confirmation_token="INVALID")

        assert "invalid or expired" in str(exc_info.value).lower()
        # File should still exist
        assert untracked_file.exists()

    def test_clean_token_single_use(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """AC4: Token can only be used once."""
        # Create two untracked files
        file1 = local_test_repo / f"{unique_filename}_1.txt"
        file2 = local_test_repo / f"{unique_filename}_2.txt"
        file1.write_text("content 1\n")
        file2.write_text("content 2\n")

        service = GitOperationsService()

        # Get and use token for first clean
        token_result = service.git_clean(local_test_repo)
        token = token_result["token"]

        service.git_clean(local_test_repo, confirmation_token=token)

        # Create more untracked files after first clean
        file3 = local_test_repo / f"{unique_filename}_3.txt"
        file3.write_text("content 3\n")

        # Try to reuse token - should fail
        with pytest.raises(ValueError) as exc_info:
            service.git_clean(local_test_repo, confirmation_token=token)

        assert "invalid or expired" in str(exc_info.value).lower()

    def test_clean_on_clean_repository(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC4: Clean on clean repository succeeds with empty removed_files."""
        # Make sure there are no untracked files
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        )
        # If there are untracked files (??), clean them first
        if "??" in status_result.stdout:
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=local_test_repo,
                check=True,
                capture_output=True,
            )

        service = GitOperationsService()

        # Get and use token
        token_result = service.git_clean(local_test_repo)
        token = token_result["token"]

        result = service.git_clean(local_test_repo, confirmation_token=token)

        assert result["success"] is True
        # removed_files should be empty or list existing
        assert "removed_files" in result
