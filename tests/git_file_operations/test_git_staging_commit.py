"""
Integration tests for Git Staging and Commit Operations.

Story #8 - Git Staging and Commit Operations Test Suite

Tests for:
- git_status (AC1): Status reporting with file categorization
- git_stage (AC2): Staging files for commit
- git_unstage (AC3): Removing files from staging area
- git_commit (AC4): Creating commits with message and author attribution

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
# AC1: git_status Operation Tests
# ---------------------------------------------------------------------------


class TestGitStatus:
    """Tests for git_status operation (AC1)."""

    def test_status_clean_repository(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Clean repository shows empty staged, unstaged, untracked lists.
        """
        service = GitOperationsService()
        result = service.git_status(local_test_repo)

        assert result["staged"] == []
        assert result["unstaged"] == []
        assert result["untracked"] == []

    def test_status_with_untracked_file(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC1: Untracked file appears in untracked list only.
        """
        # Create untracked file
        new_file = local_test_repo / unique_filename
        new_file.write_text("untracked content\n")

        service = GitOperationsService()
        result = service.git_status(local_test_repo)

        assert unique_filename in result["untracked"]
        assert unique_filename not in result["staged"]
        assert unique_filename not in result["unstaged"]

    def test_status_with_staged_file(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC1: Staged file appears in staged list only.
        """
        # Create and stage file
        new_file = local_test_repo / unique_filename
        new_file.write_text("staged content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_status(local_test_repo)

        assert unique_filename in result["staged"]
        assert unique_filename not in result["untracked"]

    def test_status_with_unstaged_modification(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC1: Modified tracked file appears in unstaged list.
        """
        # Modify existing README.md
        readme = local_test_repo / "README.md"
        readme.write_text("Modified content\n")

        service = GitOperationsService()
        result = service.git_status(local_test_repo)

        assert "README.md" in result["unstaged"]
        assert "README.md" not in result["staged"]

    def test_status_mixed_changes_categorization(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC1: Mixed changes categorize files correctly into each category.
        """
        # Create untracked file
        untracked_file = f"untracked_{unique_filename}"
        (local_test_repo / untracked_file).write_text("untracked\n")

        # Create and stage a new file
        staged_file = f"staged_{unique_filename}"
        (local_test_repo / staged_file).write_text("staged\n")
        subprocess.run(
            ["git", "add", staged_file],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Modify existing file (unstaged)
        readme = local_test_repo / "README.md"
        readme.write_text("Modified README\n")

        service = GitOperationsService()
        result = service.git_status(local_test_repo)

        # Verify categorization
        assert untracked_file in result["untracked"]
        assert staged_file in result["staged"]
        assert "README.md" in result["unstaged"]

    def test_status_staged_and_modified_file(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC1: File that is both staged and modified appears in both lists.
        """
        # Create, stage, then modify file again
        test_file = local_test_repo / unique_filename
        test_file.write_text("original content\n")

        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Modify after staging
        test_file.write_text("modified after staging\n")

        service = GitOperationsService()
        result = service.git_status(local_test_repo)

        # File should be in both staged and unstaged
        assert unique_filename in result["staged"]
        assert unique_filename in result["unstaged"]


# ---------------------------------------------------------------------------
# AC2: git_stage Operation Tests
# ---------------------------------------------------------------------------


class TestGitStage:
    """Tests for git_stage operation (AC2)."""

    def test_stage_single_file(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC2: Stage single modified file successfully.
        """
        # Create untracked file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content to stage\n")

        service = GitOperationsService()
        result = service.git_stage(local_test_repo, file_paths=[unique_filename])

        assert result["success"] is True
        assert unique_filename in result["staged_files"]

        # Verify via git status
        status = service.git_status(local_test_repo)
        assert unique_filename in status["staged"]
        assert unique_filename not in status["untracked"]

    def test_stage_multiple_files(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Stage multiple files at once.
        """
        # Create multiple files
        files = ["file_a.txt", "file_b.txt", "file_c.txt"]
        for f in files:
            (local_test_repo / f).write_text(f"content for {f}\n")

        service = GitOperationsService()
        result = service.git_stage(local_test_repo, file_paths=files)

        assert result["success"] is True
        assert len(result["staged_files"]) == len(files)
        for f in files:
            assert f in result["staged_files"]

        # Verify all files are staged
        status = service.git_status(local_test_repo)
        for f in files:
            assert f in status["staged"]

    def test_stage_modified_tracked_file(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC2: Stage a modified tracked file.
        """
        # Modify existing file
        readme = local_test_repo / "README.md"
        readme.write_text("Modified content for staging\n")

        service = GitOperationsService()

        # Verify file is unstaged
        status_before = service.git_status(local_test_repo)
        assert "README.md" in status_before["unstaged"]

        # Stage the file
        result = service.git_stage(local_test_repo, file_paths=["README.md"])

        assert result["success"] is True
        assert "README.md" in result["staged_files"]

        # Verify file moves from unstaged to staged
        status_after = service.git_status(local_test_repo)
        assert "README.md" in status_after["staged"]
        assert "README.md" not in status_after["unstaged"]

    def test_stage_subsequent_status_shows_staged(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC2: Subsequent git_status shows files in staged_files.
        """
        # Create file
        new_file = local_test_repo / unique_filename
        new_file.write_text("test content\n")

        service = GitOperationsService()

        # Before staging
        status_before = service.git_status(local_test_repo)
        assert unique_filename in status_before["untracked"]
        assert unique_filename not in status_before["staged"]

        # Stage
        service.git_stage(local_test_repo, file_paths=[unique_filename])

        # After staging
        status_after = service.git_status(local_test_repo)
        assert unique_filename in status_after["staged"]
        assert unique_filename not in status_after["untracked"]

    def test_stage_nested_directory_file(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC2: Stage file in nested directory.
        """
        # Create nested directory and file
        nested_dir = local_test_repo / "src" / "subdir"
        nested_dir.mkdir(parents=True, exist_ok=True)
        nested_file = nested_dir / unique_filename
        nested_file.write_text("nested file content\n")

        file_path = f"src/subdir/{unique_filename}"

        service = GitOperationsService()
        result = service.git_stage(local_test_repo, file_paths=[file_path])

        assert result["success"] is True
        assert file_path in result["staged_files"]

        # Verify
        status = service.git_status(local_test_repo)
        assert file_path in status["staged"]


# ---------------------------------------------------------------------------
# AC3: git_unstage Operation Tests
# ---------------------------------------------------------------------------


class TestGitUnstage:
    """Tests for git_unstage operation (AC3)."""

    def test_unstage_single_file(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC3: Remove file from staging area.
        """
        # Create and stage file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Verify file is staged
        status_before = service.git_status(local_test_repo)
        assert unique_filename in status_before["staged"]

        # Unstage
        result = service.git_unstage(local_test_repo, file_paths=[unique_filename])

        assert result["success"] is True
        assert unique_filename in result["unstaged_files"]

    def test_unstage_file_moves_to_untracked(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC3: Unstaged new file moves to untracked list.
        """
        # Create and stage new file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Unstage
        service.git_unstage(local_test_repo, file_paths=[unique_filename])

        # Verify file is now untracked
        status = service.git_status(local_test_repo)
        assert unique_filename not in status["staged"]
        assert unique_filename in status["untracked"]

    def test_unstage_modified_file_moves_to_unstaged(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC3: Unstaged modified tracked file moves to unstaged list.
        """
        # Modify and stage existing tracked file
        readme = local_test_repo / "README.md"
        readme.write_text("Modified content\n")
        subprocess.run(
            ["git", "add", "README.md"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Verify staged
        status_before = service.git_status(local_test_repo)
        assert "README.md" in status_before["staged"]

        # Unstage
        service.git_unstage(local_test_repo, file_paths=["README.md"])

        # Verify file moves from staged to unstaged
        status_after = service.git_status(local_test_repo)
        assert "README.md" not in status_after["staged"]
        assert "README.md" in status_after["unstaged"]

    def test_unstage_multiple_files(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        AC3: Unstage multiple files at once.
        """
        # Create and stage multiple files
        files = ["unstage_a.txt", "unstage_b.txt"]
        for f in files:
            (local_test_repo / f).write_text(f"content for {f}\n")
        subprocess.run(
            ["git", "add"] + files,
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Verify all staged
        status_before = service.git_status(local_test_repo)
        for f in files:
            assert f in status_before["staged"]

        # Unstage all
        result = service.git_unstage(local_test_repo, file_paths=files)

        assert result["success"] is True
        for f in files:
            assert f in result["unstaged_files"]

        # Verify none staged
        status_after = service.git_status(local_test_repo)
        for f in files:
            assert f not in status_after["staged"]


# ---------------------------------------------------------------------------
# AC4: git_commit Operation Tests
# ---------------------------------------------------------------------------


class TestGitCommit:
    """Tests for git_commit operation (AC4)."""

    def test_commit_with_message_returns_hash(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: Create commit with message, returns commit hash.
        """
        # Create and stage file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content for commit\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        result = service.git_commit(
            local_test_repo,
            message="Test commit message",
            user_email="test@example.com",
            user_name="Test User",
        )

        assert result["success"] is True
        assert "commit_hash" in result
        # Verify commit hash is a valid 40-character hex string
        assert len(result["commit_hash"]) == 40
        assert all(c in "0123456789abcdef" for c in result["commit_hash"])

    def test_commit_staged_files_cleared(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: staged_files is empty after commit.
        """
        # Create and stage file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        # Verify file is staged before commit
        status_before = service.git_status(local_test_repo)
        assert unique_filename in status_before["staged"]

        # Commit
        service.git_commit(
            local_test_repo,
            message="Commit to clear staged",
            user_email="test@example.com",
            user_name="Test User",
        )

        # Verify staged is empty after commit
        status_after = service.git_status(local_test_repo)
        assert status_after["staged"] == []

    def test_commit_custom_author_honored(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: Custom author_name and author_email honored.
        """
        # Create and stage file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        custom_email = "custom.author@example.com"
        custom_name = "Custom Author"

        service = GitOperationsService()
        result = service.git_commit(
            local_test_repo,
            message="Commit with custom author",
            user_email=custom_email,
            user_name=custom_name,
        )

        assert result["success"] is True
        assert result["author"] == custom_email

        # Verify via git log
        log_result = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        )
        author_line = log_result.stdout.strip()
        assert custom_name in author_line
        assert custom_email in author_line

    def test_commit_message_preserved(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: Commit message is preserved in commit.
        """
        # Create and stage file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        test_message = "This is the test commit message"

        service = GitOperationsService()
        result = service.git_commit(
            local_test_repo,
            message=test_message,
            user_email="test@example.com",
            user_name="Test User",
        )

        assert result["success"] is True
        assert result["message"] == test_message

        # Verify message in git log
        log_result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        )
        # The commit message should start with our message (before trailers)
        assert test_message in log_result.stdout

    def test_commit_derives_name_from_email(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        AC4: When user_name not provided, derives from email.
        """
        # Create and stage file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()
        # Use email with simple username (no dots) to comply with validation
        result = service.git_commit(
            local_test_repo,
            message="Commit without explicit name",
            user_email="deriveduser@example.com",
            user_name=None,  # Explicitly not providing name
        )

        assert result["success"] is True

        # Verify name was derived from email
        log_result = subprocess.run(
            ["git", "log", "-1", "--format=%an"],
            cwd=local_test_repo,
            capture_output=True,
            text=True,
        )
        # Should derive "deriveduser" from email (part before @)
        assert "deriveduser" in log_result.stdout


# ---------------------------------------------------------------------------
# Error Case Tests
# ---------------------------------------------------------------------------


class TestGitStageErrors:
    """Error case tests for git_stage operation."""

    def test_stage_nonexistent_file(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        git_stage raises GitCommandError for non-existent file.
        """
        service = GitOperationsService()

        with pytest.raises(GitCommandError) as exc_info:
            service.git_stage(
                local_test_repo,
                file_paths=["this_file_does_not_exist.txt"],
            )

        assert "git add failed" in str(exc_info.value).lower() or exc_info.value.returncode != 0

    def test_stage_code_indexer_files_blocked(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        git_stage raises ValueError when staging .code-indexer files.
        """
        service = GitOperationsService()

        with pytest.raises(ValueError) as exc_info:
            service.git_stage(
                local_test_repo,
                file_paths=[".code-indexer/index/something.json"],
            )

        assert ".code-indexer" in str(exc_info.value).lower()
        assert "never be committed" in str(exc_info.value).lower()


class TestGitUnstageErrors:
    """Error case tests for git_unstage operation."""

    def test_unstage_file_not_staged(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        git_unstage on a file not staged succeeds silently (git reset behavior).

        Note: git reset HEAD on an unstaged file doesn't error - it's idempotent.
        This test documents this expected behavior.
        """
        # Create an untracked file (not staged)
        new_file = local_test_repo / unique_filename
        new_file.write_text("untracked content\n")

        service = GitOperationsService()

        # Git reset HEAD on unstaged file doesn't raise - it's a no-op
        result = service.git_unstage(local_test_repo, file_paths=[unique_filename])

        # Operation succeeds (git reset is idempotent)
        assert result["success"] is True


class TestGitCommitErrors:
    """Error case tests for git_commit operation."""

    def test_commit_empty_email_rejected(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        git_commit raises ValueError when user_email is empty.
        """
        # Create and stage a file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        with pytest.raises(ValueError) as exc_info:
            service.git_commit(
                local_test_repo,
                message="Test commit",
                user_email="",
                user_name="Test User",
            )

        assert "required" in str(exc_info.value).lower()

    def test_commit_invalid_email_rejected(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        git_commit raises ValueError when user_email has invalid format.
        """
        # Create and stage a file
        new_file = local_test_repo / unique_filename
        new_file.write_text("content\n")
        subprocess.run(
            ["git", "add", unique_filename],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        service = GitOperationsService()

        with pytest.raises(ValueError) as exc_info:
            service.git_commit(
                local_test_repo,
                message="Test commit",
                user_email="not-a-valid-email",
                user_name="Test User",
            )

        assert "invalid email format" in str(exc_info.value).lower()

    def test_commit_nothing_to_commit(
        self,
        local_test_repo: Path,
        captured_state,
    ):
        """
        git_commit raises GitCommandError when nothing is staged.
        """
        service = GitOperationsService()

        with pytest.raises(GitCommandError) as exc_info:
            service.git_commit(
                local_test_repo,
                message="Empty commit",
                user_email="test@example.com",
                user_name="Test User",
            )

        assert "git commit failed" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Workflow Integration Tests
# ---------------------------------------------------------------------------


class TestWorkflowIntegration:
    """Integration tests for complete git workflows."""

    def test_full_create_stage_commit_workflow(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        Full workflow: create file -> stage -> commit -> verify clean state.
        """
        service = GitOperationsService()

        # Step 1: Verify clean initial state
        status_initial = service.git_status(local_test_repo)
        assert status_initial["staged"] == []
        assert status_initial["untracked"] == []

        # Step 2: Create a new file
        new_file = local_test_repo / unique_filename
        new_file.write_text("# New file for workflow test\nprint('hello')\n")

        # Step 3: Verify file appears as untracked
        status_untracked = service.git_status(local_test_repo)
        assert unique_filename in status_untracked["untracked"]

        # Step 4: Stage the file
        stage_result = service.git_stage(local_test_repo, file_paths=[unique_filename])
        assert stage_result["success"] is True

        # Step 5: Verify file is staged
        status_staged = service.git_status(local_test_repo)
        assert unique_filename in status_staged["staged"]
        assert unique_filename not in status_staged["untracked"]

        # Step 6: Commit the file
        commit_result = service.git_commit(
            local_test_repo,
            message="Add new file via workflow test",
            user_email="workflow@example.com",
            user_name="Workflow Test",
        )
        assert commit_result["success"] is True
        assert len(commit_result["commit_hash"]) == 40

        # Step 7: Verify clean state after commit
        status_final = service.git_status(local_test_repo)
        assert status_final["staged"] == []
        assert unique_filename not in status_final["untracked"]

    def test_stage_unstage_restage_workflow(
        self,
        local_test_repo: Path,
        captured_state,
        unique_filename: str,
    ):
        """
        Workflow: stage -> unstage -> restage -> verify state transitions.
        """
        service = GitOperationsService()

        # Create a new file
        new_file = local_test_repo / unique_filename
        new_file.write_text("stage/unstage test content\n")

        # Stage the file
        service.git_stage(local_test_repo, file_paths=[unique_filename])
        status_after_stage = service.git_status(local_test_repo)
        assert unique_filename in status_after_stage["staged"]

        # Unstage the file
        service.git_unstage(local_test_repo, file_paths=[unique_filename])
        status_after_unstage = service.git_status(local_test_repo)
        assert unique_filename not in status_after_unstage["staged"]
        assert unique_filename in status_after_unstage["untracked"]

        # Restage the file
        service.git_stage(local_test_repo, file_paths=[unique_filename])
        status_after_restage = service.git_status(local_test_repo)
        assert unique_filename in status_after_restage["staged"]
        assert unique_filename not in status_after_restage["untracked"]
