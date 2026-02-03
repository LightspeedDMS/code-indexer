"""
Integration tests for Git Branch Management Operations.

Story #10 - Git Branch Operations Test Suite (list, create, switch, delete)

Tests for:
- git_branch_list (AC1): List all branches with current branch marking
- git_branch_create (AC2): Create new branches from HEAD or start_point
- git_branch_switch (AC3): Switch between branches with state validation
- git_branch_delete (AC4): Delete branches with confirmation token flow

Uses REAL git operations - NO Python mocks for git commands.
"""

import subprocess
from pathlib import Path

import pytest

from code_indexer.server.services.git_operations_service import (
    GitCommandError,
    GitOperationsService,
)


# ---------------------------------------------------------------------------
# AC1: git_branch_list Operation Tests
# ---------------------------------------------------------------------------


class TestGitBranchList:
    """Tests for git_branch_list operation (AC1)."""

    def test_list_shows_current_branch(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: List includes current branch marked correctly.
        """
        service = GitOperationsService()
        result = service.git_branch_list(local_test_repo)

        # Should have 'main' as current
        assert result["current"] == "main"
        assert "main" in result["local"]

    def test_list_local_branches(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC1: List includes all local branches.
        """
        # Create additional branch using git
        subprocess.run(
            ["git", "branch", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_branch_list(local_test_repo)

        # Should include both main and the new branch
        assert "main" in result["local"]
        assert unique_branch_name in result["local"]
        # Current should still be main
        assert result["current"] == "main"

    def test_list_remote_branches(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: List includes remote tracking branches.
        """
        service = GitOperationsService()
        result = service.git_branch_list(local_test_repo)

        # Should have remote branches (origin/main exists after push)
        assert "remote" in result
        # Remote branches should contain origin/main
        assert any("origin/main" in branch for branch in result["remote"])

    def test_list_current_branch_after_switch(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC1: Current branch is correctly marked after switching branches.
        """
        # Create and switch to new branch
        subprocess.run(
            ["git", "checkout", "-b", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_branch_list(local_test_repo)

        # Current should now be the new branch
        assert result["current"] == unique_branch_name
        assert unique_branch_name in result["local"]
        assert "main" in result["local"]

    def test_list_returns_proper_structure(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: List returns proper structure with current, local, and remote keys.
        """
        service = GitOperationsService()
        result = service.git_branch_list(local_test_repo)

        # Structure should include current, local, and remote keys
        assert "current" in result
        assert "local" in result
        assert "remote" in result
        assert isinstance(result["local"], list)
        assert isinstance(result["remote"], list)


# ---------------------------------------------------------------------------
# AC2: git_branch_create Operation Tests
# ---------------------------------------------------------------------------


class TestGitBranchCreate:
    """Tests for git_branch_create operation (AC2)."""

    def test_create_branch_from_head(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC2: Create new branch from current HEAD.
        """
        service = GitOperationsService()
        result = service.git_branch_create(
            local_test_repo, branch_name=unique_branch_name
        )

        assert result["success"] is True
        assert result["created_branch"] == unique_branch_name

        # Verify branch exists via git_branch_list
        branches = service.git_branch_list(local_test_repo)
        assert unique_branch_name in branches["local"]

    def test_create_branch_does_not_switch(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC2: Creating a branch does not switch to it.
        """
        service = GitOperationsService()

        # Verify we're on main
        branches_before = service.git_branch_list(local_test_repo)
        assert branches_before["current"] == "main"

        # Create new branch
        result = service.git_branch_create(
            local_test_repo, branch_name=unique_branch_name
        )
        assert result["success"] is True

        # Verify we're still on main
        branches_after = service.git_branch_list(local_test_repo)
        assert branches_after["current"] == "main"
        assert unique_branch_name in branches_after["local"]

    def test_create_branch_with_slash_separator(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Create branch with slashes (feature/name style).
        """
        import uuid

        branch_name = f"feature/test-{uuid.uuid4().hex[:6]}"

        service = GitOperationsService()
        result = service.git_branch_create(local_test_repo, branch_name=branch_name)

        assert result["success"] is True
        assert result["created_branch"] == branch_name

        # Verify branch exists
        branches = service.git_branch_list(local_test_repo)
        assert branch_name in branches["local"]


class TestGitBranchCreateErrors:
    """Error case tests for git_branch_create operation."""

    def test_create_existing_branch_fails(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC2: Error when branch already exists.
        """
        # First create the branch
        subprocess.run(
            ["git", "branch", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Try to create the same branch again
        with pytest.raises(GitCommandError) as exc_info:
            service.git_branch_create(local_test_repo, branch_name=unique_branch_name)

        assert "git branch create failed" in str(exc_info.value).lower()

    def test_create_branch_invalid_name_fails(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Error when branch name is invalid (starts with dash).
        """
        service = GitOperationsService()

        # Invalid branch name
        with pytest.raises(GitCommandError):
            service.git_branch_create(local_test_repo, branch_name="-invalid-name")


# ---------------------------------------------------------------------------
# AC3: git_branch_switch Operation Tests
# ---------------------------------------------------------------------------


class TestGitBranchSwitch:
    """Tests for git_branch_switch operation (AC3)."""

    def test_switch_to_existing_branch(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC3: Switch to existing branch successfully.
        """
        # Create branch first
        subprocess.run(
            ["git", "branch", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_branch_switch(
            local_test_repo, branch_name=unique_branch_name
        )

        assert result["success"] is True
        assert result["current_branch"] == unique_branch_name
        assert result["previous_branch"] == "main"

    def test_switch_back_to_main(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC3: Switch back to main branch from feature branch.
        """
        # Create and switch to new branch
        subprocess.run(
            ["git", "checkout", "-b", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_branch_switch(local_test_repo, branch_name="main")

        assert result["success"] is True
        assert result["current_branch"] == "main"
        assert result["previous_branch"] == unique_branch_name

    def test_switch_returns_previous_branch(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC3: Switch returns previous_branch in result.
        """
        # Create branch
        subprocess.run(
            ["git", "branch", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_branch_switch(
            local_test_repo, branch_name=unique_branch_name
        )
        assert result["previous_branch"] == "main"
        assert result["current_branch"] == unique_branch_name


class TestGitBranchSwitchErrors:
    """Error case tests for git_branch_switch operation."""

    def test_switch_nonexistent_branch_fails(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC3: Error when branch doesn't exist.
        """
        service = GitOperationsService()

        with pytest.raises(GitCommandError) as exc_info:
            service.git_branch_switch(
                local_test_repo,
                branch_name="nonexistent-branch-xyz",
            )

        assert "git branch switch failed" in str(exc_info.value).lower()

    def test_switch_with_conflicting_uncommitted_changes_fails(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
        unique_filename: str,
    ):
        """
        AC3: Error when uncommitted changes would be overwritten.
        """
        # Create a branch with a committed file
        subprocess.run(
            ["git", "checkout", "-b", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        new_file = local_test_repo / unique_filename
        new_file.write_text("feature branch content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add file on feature branch"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Switch back to main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Create conflicting uncommitted file on main
        new_file.write_text("main branch different content\n")

        service = GitOperationsService()

        with pytest.raises(GitCommandError) as exc_info:
            service.git_branch_switch(local_test_repo, branch_name=unique_branch_name)

        assert "git branch switch failed" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# AC4: git_branch_delete with Confirmation Token Tests
# ---------------------------------------------------------------------------


class TestGitBranchDelete:
    """Tests for git_branch_delete operation (AC4)."""

    def test_delete_requires_confirmation_token(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC4: First call without token returns confirmation token.
        """
        # Create branch to delete
        subprocess.run(
            ["git", "branch", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_branch_delete(
            local_test_repo, branch_name=unique_branch_name
        )

        assert result["requires_confirmation"] is True
        assert "token" in result
        assert len(result["token"]) == 6

    def test_delete_with_valid_token(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC4: Second call with valid token deletes the branch.
        """
        # Create branch (merged by default as it shares same commit)
        subprocess.run(
            ["git", "branch", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # First call - get token
        token_result = service.git_branch_delete(
            local_test_repo, branch_name=unique_branch_name
        )
        token = token_result["token"]

        # Second call - use token to delete
        delete_result = service.git_branch_delete(
            local_test_repo,
            branch_name=unique_branch_name,
            confirmation_token=token,
        )

        assert delete_result["success"] is True
        assert delete_result["deleted_branch"] == unique_branch_name

        # Verify branch no longer exists
        branches = service.git_branch_list(local_test_repo)
        assert unique_branch_name not in branches["local"]

    def test_delete_token_single_use(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
    ):
        """
        AC4: Token can only be used once.
        """
        # Create two branches
        branch_1 = f"{unique_branch_name}-1"
        branch_2 = f"{unique_branch_name}-2"
        for b in [branch_1, branch_2]:
            subprocess.run(
                ["git", "branch", b],
                cwd=local_test_repo,
                check=True,
                capture_output=True,
            )

        service = GitOperationsService()

        # Get and use token for first branch
        token_result = service.git_branch_delete(local_test_repo, branch_name=branch_1)
        token = token_result["token"]
        service.git_branch_delete(
            local_test_repo, branch_name=branch_1, confirmation_token=token
        )

        # Try to use same token again - should fail
        with pytest.raises(ValueError) as exc_info:
            service.git_branch_delete(
                local_test_repo, branch_name=branch_2, confirmation_token=token
            )

        assert "invalid or expired" in str(exc_info.value).lower()


class TestGitBranchDeleteErrors:
    """Error case tests for git_branch_delete operation (AC4)."""

    def test_delete_current_branch_fails(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC4: Error when trying to delete the current branch.
        """
        service = GitOperationsService()

        # Verify we're on main
        branches = service.git_branch_list(local_test_repo)
        assert branches["current"] == "main"

        # Get token first
        token_result = service.git_branch_delete(local_test_repo, branch_name="main")
        token = token_result["token"]

        # Try to delete current branch - should fail
        with pytest.raises(GitCommandError) as exc_info:
            service.git_branch_delete(
                local_test_repo,
                branch_name="main",
                confirmation_token=token,
            )

        assert "git branch delete failed" in str(exc_info.value).lower()

    def test_delete_unmerged_branch_fails(
        self,
        local_test_repo: Path,
        captured_state,
        unique_branch_name: str,
        unique_filename: str,
    ):
        """
        AC4: Error when deleting unmerged branch with soft delete (-d).
        """
        # Create branch with unique commit (not merged to main)
        subprocess.run(
            ["git", "checkout", "-b", unique_branch_name],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Create and commit a file on this branch
        new_file = local_test_repo / unique_filename
        new_file.write_text("unmerged content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Unmerged commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Switch back to main
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Get token first
        token_result = service.git_branch_delete(
            local_test_repo,
            branch_name=unique_branch_name,
        )
        token = token_result["token"]

        # Try to delete unmerged branch - should fail with -d
        with pytest.raises(GitCommandError) as exc_info:
            service.git_branch_delete(
                local_test_repo,
                branch_name=unique_branch_name,
                confirmation_token=token,
            )

        # Git error message mentions "not fully merged"
        error_msg = str(exc_info.value).lower()
        assert "git branch delete failed" in error_msg
