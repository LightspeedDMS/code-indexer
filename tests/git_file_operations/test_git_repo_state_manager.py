"""
Unit tests for GitRepoStateManager.

Tests the core infrastructure for idempotent git operation tests:
- State capture (branch, HEAD, staged/unstaged/untracked files)
- State restoration (rollback to original state)
- Test branch creation and cleanup
- File tracking for cleanup

All tests use the local_test_repo fixture (no SSH/network required).
"""

import subprocess
from pathlib import Path

import pytest

from .git_repo_state_manager import GitRepoState, GitRepoStateManager


class TestGitRepoStateManagerInit:
    """Tests for GitRepoStateManager initialization."""

    def test_init_with_valid_repo(self, local_test_repo: Path):
        """GitRepoStateManager initializes with valid git repository."""
        manager = GitRepoStateManager(local_test_repo)
        assert manager.repo_path == local_test_repo

    def test_init_with_invalid_path_raises(self, tmp_path: Path):
        """GitRepoStateManager raises ValueError for non-git directory."""
        with pytest.raises(ValueError, match="Not a git repository"):
            GitRepoStateManager(tmp_path)

    def test_init_with_nonexistent_path_raises(self):
        """GitRepoStateManager raises ValueError for nonexistent path."""
        with pytest.raises(ValueError, match="Not a git repository"):
            GitRepoStateManager(Path("/nonexistent/path"))


class TestCaptureState:
    """Tests for state capture functionality."""

    def test_capture_state_returns_git_repo_state(self, state_manager: GitRepoStateManager):
        """capture_state returns GitRepoState object."""
        state = state_manager.capture_state()
        assert isinstance(state, GitRepoState)

    def test_capture_state_gets_current_branch(self, state_manager: GitRepoStateManager):
        """capture_state correctly identifies current branch."""
        state = state_manager.capture_state()
        assert state.current_branch == "main"

    def test_capture_state_gets_head_commit(self, state_manager: GitRepoStateManager):
        """capture_state captures HEAD commit hash."""
        state = state_manager.capture_state()
        # HEAD commit should be 40-character hex string
        assert len(state.head_commit) == 40
        assert all(c in "0123456789abcdef" for c in state.head_commit)

    def test_capture_state_detects_staged_files(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """capture_state detects staged files."""
        # Create and stage a file
        test_file = local_test_repo / "staged_test.txt"
        test_file.write_text("staged content")
        subprocess.run(["git", "add", "staged_test.txt"], cwd=local_test_repo, check=True)

        state = state_manager.capture_state()
        assert "staged_test.txt" in state.staged_files

        # Cleanup
        subprocess.run(["git", "reset", "HEAD"], cwd=local_test_repo, check=False)
        test_file.unlink()

    def test_capture_state_detects_unstaged_modifications(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """capture_state detects unstaged modifications to tracked files."""
        # Modify existing tracked file
        readme = local_test_repo / "README.md"
        original_content = readme.read_text()
        readme.write_text(original_content + "\nModified for test")

        state = state_manager.capture_state()
        assert "README.md" in state.unstaged_files

        # Cleanup
        readme.write_text(original_content)

    def test_capture_state_detects_untracked_files(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """capture_state detects untracked files."""
        # Create untracked file
        test_file = local_test_repo / "untracked_test.txt"
        test_file.write_text("untracked content")

        state = state_manager.capture_state()
        assert "untracked_test.txt" in state.untracked_files

        # Cleanup
        test_file.unlink()

    def test_capture_state_clean_repo_has_empty_lists(
        self, state_manager: GitRepoStateManager
    ):
        """capture_state returns empty lists for clean repository."""
        state = state_manager.capture_state()
        assert state.staged_files == []
        assert state.unstaged_files == []
        assert state.untracked_files == []


class TestRestoreState:
    """Tests for state restoration functionality."""

    def test_restore_state_resets_to_original_branch(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """restore_state returns to original branch."""
        # Capture original state
        original_state = state_manager.capture_state()
        assert original_state.current_branch == "main"

        # Create and switch to new branch
        subprocess.run(
            ["git", "checkout", "-b", "test-branch"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Verify we're on new branch
        assert state_manager.get_current_branch() == "test-branch"

        # Restore state
        state_manager.restore_state(original_state)

        # Verify we're back on main
        assert state_manager.get_current_branch() == "main"

    def test_restore_state_resets_to_original_commit(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """restore_state resets HEAD to original commit."""
        # Capture original state
        original_state = state_manager.capture_state()
        original_commit = original_state.head_commit

        # Create a new commit
        test_file = local_test_repo / "new_commit_test.txt"
        test_file.write_text("new commit content")
        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Test commit"],
            cwd=local_test_repo,
            check=True,
            capture_output=True,
        )

        # Verify HEAD changed
        assert state_manager.get_head_commit() != original_commit

        # Restore state
        state_manager.restore_state(original_state)

        # Verify HEAD is back to original
        assert state_manager.get_head_commit() == original_commit

    def test_restore_state_cleans_staged_files(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """restore_state unstages any staged files."""
        # Capture clean state
        original_state = state_manager.capture_state()

        # Create and stage a file
        test_file = local_test_repo / "staged_cleanup_test.txt"
        test_file.write_text("staged content")
        subprocess.run(["git", "add", "staged_cleanup_test.txt"], cwd=local_test_repo, check=True)

        # Verify file is staged
        assert state_manager.has_uncommitted_changes()

        # Restore state
        state_manager.restore_state(original_state)

        # Verify no uncommitted changes (file was cleaned)
        assert not state_manager.has_uncommitted_changes()

    def test_restore_state_cleans_untracked_files(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """restore_state removes untracked files."""
        # Capture clean state
        original_state = state_manager.capture_state()

        # Create untracked file
        test_file = local_test_repo / "untracked_cleanup_test.txt"
        test_file.write_text("untracked content")
        assert test_file.exists()

        # Restore state
        state_manager.restore_state(original_state)

        # Verify file was removed
        assert not test_file.exists()

    def test_restore_state_reverts_working_tree_changes(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """restore_state reverts modifications to tracked files."""
        # Capture clean state
        original_state = state_manager.capture_state()

        # Modify tracked file
        readme = local_test_repo / "README.md"
        original_content = readme.read_text()
        readme.write_text("MODIFIED CONTENT")

        # Verify modification
        assert readme.read_text() == "MODIFIED CONTENT"

        # Restore state
        state_manager.restore_state(original_state)

        # Verify file restored
        assert readme.read_text() == original_content

    def test_restore_state_removes_tracked_created_files(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """restore_state removes files tracked via track_created_file."""
        # Capture state
        state = state_manager.capture_state()

        # Create file and track it
        test_file = local_test_repo / "tracked_file.txt"
        test_file.write_text("tracked content")
        state_manager.track_created_file(state, "tracked_file.txt")

        # Verify file exists
        assert test_file.exists()

        # Restore state
        state_manager.restore_state(state)

        # Verify file was removed
        assert not test_file.exists()


class TestCreateTestBranch:
    """Tests for test branch creation."""

    def test_create_test_branch_creates_unique_branch(
        self, state_manager: GitRepoStateManager
    ):
        """create_test_branch creates branch with unique name."""
        branch1 = state_manager.create_test_branch("test")
        branch2 = state_manager.create_test_branch("test")

        assert branch1 != branch2
        assert branch1.startswith("test-")
        assert branch2.startswith("test-")

        # Cleanup - switch back to main before deleting branches
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=state_manager.repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch1, branch2],
            cwd=state_manager.repo_path,
            check=False,
            capture_output=True,
        )

    def test_create_test_branch_switches_to_new_branch(
        self, state_manager: GitRepoStateManager
    ):
        """create_test_branch checks out the new branch."""
        original_branch = state_manager.get_current_branch()
        new_branch = state_manager.create_test_branch("switch-test")

        assert state_manager.get_current_branch() == new_branch
        assert state_manager.get_current_branch() != original_branch

        # Cleanup
        subprocess.run(
            ["git", "checkout", original_branch],
            cwd=state_manager.repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", new_branch],
            cwd=state_manager.repo_path,
            check=False,
            capture_output=True,
        )


class TestHelperMethods:
    """Tests for helper methods."""

    def test_get_current_branch(self, state_manager: GitRepoStateManager):
        """get_current_branch returns current branch name."""
        assert state_manager.get_current_branch() == "main"

    def test_get_head_commit(self, state_manager: GitRepoStateManager):
        """get_head_commit returns 40-character commit hash."""
        commit = state_manager.get_head_commit()
        assert len(commit) == 40
        assert all(c in "0123456789abcdef" for c in commit)

    def test_has_uncommitted_changes_false_for_clean_repo(
        self, state_manager: GitRepoStateManager
    ):
        """has_uncommitted_changes returns False for clean repo."""
        assert not state_manager.has_uncommitted_changes()

    def test_has_uncommitted_changes_true_for_dirty_repo(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """has_uncommitted_changes returns True for dirty repo."""
        # Create untracked file
        test_file = local_test_repo / "dirty_test.txt"
        test_file.write_text("dirty content")

        assert state_manager.has_uncommitted_changes()

        # Cleanup
        test_file.unlink()

    def test_track_created_file_adds_to_list(self, state_manager: GitRepoStateManager):
        """track_created_file adds file to state's created_files list."""
        state = state_manager.capture_state()
        assert state.created_files == []

        state_manager.track_created_file(state, "file1.txt")
        assert "file1.txt" in state.created_files

        state_manager.track_created_file(state, "file2.txt")
        assert "file2.txt" in state.created_files

    def test_track_created_file_no_duplicates(self, state_manager: GitRepoStateManager):
        """track_created_file does not add duplicate entries."""
        state = state_manager.capture_state()

        state_manager.track_created_file(state, "file.txt")
        state_manager.track_created_file(state, "file.txt")

        assert state.created_files.count("file.txt") == 1


class TestIdempotency:
    """Tests verifying idempotent test behavior."""

    def test_captured_state_fixture_provides_automatic_restoration(
        self, captured_state: GitRepoState, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """
        captured_state fixture automatically restores state after test.

        This test modifies the repository and relies on the fixture
        to restore state. If the next test fails, this fixture is broken.
        """
        # Make various modifications
        test_file = local_test_repo / "idempotency_test.txt"
        test_file.write_text("test content")

        readme = local_test_repo / "README.md"
        readme.write_text("MODIFIED")

        subprocess.run(["git", "add", "."], cwd=local_test_repo, check=True)

        # State should be dirty
        assert state_manager.has_uncommitted_changes()

        # Fixture will restore state after this test

    def test_previous_test_did_not_affect_state(
        self, state_manager: GitRepoStateManager, local_test_repo: Path
    ):
        """
        Verify previous test's modifications were rolled back.

        This test runs after test_captured_state_fixture_provides_automatic_restoration
        and verifies the repository is clean.
        """
        # Repository should be clean
        assert not state_manager.has_uncommitted_changes()

        # Specific file from previous test should not exist
        test_file = local_test_repo / "idempotency_test.txt"
        assert not test_file.exists()

        # README should have original content
        readme = local_test_repo / "README.md"
        assert "MODIFIED" not in readme.read_text()


class TestGitRepoStateDataclass:
    """Tests for GitRepoState dataclass."""

    def test_git_repo_state_repr(self):
        """GitRepoState has readable repr."""
        state = GitRepoState(
            current_branch="main",
            head_commit="abc123def456789012345678901234567890abcd",
            staged_files=["file1.txt"],
            unstaged_files=["file2.txt", "file3.txt"],
            untracked_files=[],
        )

        repr_str = repr(state)
        assert "main" in repr_str
        assert "abc123de" in repr_str  # First 8 chars
        assert "staged=1" in repr_str
        assert "unstaged=2" in repr_str
        assert "untracked=0" in repr_str

    def test_git_repo_state_defaults(self):
        """GitRepoState has sensible defaults for optional fields."""
        state = GitRepoState(
            current_branch="main",
            head_commit="abc123def456789012345678901234567890abcd",
        )

        assert state.staged_files == []
        assert state.unstaged_files == []
        assert state.untracked_files == []
        assert state.remote_refs == {}
        assert state.test_branch is None
        assert state.test_branch_created is False
        assert state.created_files == []
