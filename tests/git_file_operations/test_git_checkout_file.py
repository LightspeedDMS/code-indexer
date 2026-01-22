"""
Git Checkout File Recovery Tests - Story #13 AC1.

Tests for git_checkout_file operation:
- Restore modified files to HEAD version
- Restore deleted files from HEAD
- Handle non-existent files appropriately
- Support nested directory structures

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


class TestGitCheckoutFile:
    """Tests for git_checkout_file operation (AC1)."""

    def test_checkout_restores_modified_file(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC1: Restore a modified file to HEAD version."""
        readme = local_test_repo / "README.md"
        original_content = readme.read_text()
        readme.write_text("MODIFIED CONTENT - should be restored")

        service = GitOperationsService()
        result = service.git_checkout_file(local_test_repo, "README.md")

        assert result["success"] is True
        assert result["restored_file"] == "README.md"
        assert readme.read_text() == original_content

    def test_checkout_restores_deleted_file(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC1: Restore a deleted tracked file from HEAD."""
        readme = local_test_repo / "README.md"
        original_content = readme.read_text()
        readme.unlink()

        assert not readme.exists()

        service = GitOperationsService()
        result = service.git_checkout_file(local_test_repo, "README.md")

        assert result["success"] is True
        assert result["restored_file"] == "README.md"
        assert readme.exists()
        assert readme.read_text() == original_content

    def test_checkout_restores_staged_file(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC1: Restore a file that was modified and staged."""
        readme = local_test_repo / "README.md"
        original_content = readme.read_text()
        readme.write_text("STAGED MODIFICATION")

        subprocess.run(
            ["git", "add", "README.md"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_checkout_file(local_test_repo, "README.md")

        assert result["success"] is True
        assert readme.read_text() == original_content

    def test_checkout_file_not_in_head_fails(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC1: Checkout fails for file that doesn't exist in HEAD."""
        service = GitOperationsService()

        with pytest.raises(GitCommandError) as exc_info:
            service.git_checkout_file(local_test_repo, "nonexistent_file.txt")

        assert "git checkout file failed" in str(exc_info.value).lower()

    def test_checkout_nested_directory_file(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC1: Restore file in nested directory structure."""
        nested_dir = local_test_repo / "src" / "utils"
        nested_dir.mkdir(parents=True)
        nested_file = nested_dir / "helper.py"
        nested_file.write_text("# Helper module\ndef helper(): pass\n")

        subprocess.run(
            ["git", "add", "src/utils/helper.py"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add helper module"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        original_content = nested_file.read_text()
        nested_file.write_text("# MODIFIED\ndef modified(): pass\n")

        service = GitOperationsService()
        result = service.git_checkout_file(local_test_repo, "src/utils/helper.py")

        assert result["success"] is True
        assert result["restored_file"] == "src/utils/helper.py"
        assert nested_file.read_text() == original_content

    def test_checkout_restores_file_with_multiple_modifications(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """AC1: Restore file after multiple modifications."""
        readme = local_test_repo / "README.md"
        original_content = readme.read_text()

        readme.write_text("First modification\n")
        readme.write_text("Second modification\n")
        readme.write_text("Third modification\n")

        service = GitOperationsService()
        result = service.git_checkout_file(local_test_repo, "README.md")

        assert result["success"] is True
        assert readme.read_text() == original_content
