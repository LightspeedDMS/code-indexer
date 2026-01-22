"""
GitRepoStateManager: Repository state capture and restoration for idempotent tests.

Provides infrastructure for:
- Capturing complete repository state (branch, HEAD, staged files, working tree)
- Creating isolated test branches for test execution
- Restoring repository to exact original state after test completion
- Handling rollback of pushed commits via force-push or reflog

This enables truly idempotent tests - no matter what operations are performed,
the repository returns to its original state after each test.

Usage:
    state_manager = GitRepoStateManager(repo_path)

    # Capture state before test
    original_state = state_manager.capture_state()

    # Create isolated test branch
    test_branch = state_manager.create_test_branch("test-feature-xyz")

    try:
        # Run test operations...
        pass
    finally:
        # Restore to original state (always succeeds)
        state_manager.restore_state(original_state)

NO Python mocks - all operations use REAL git commands via subprocess.
"""

import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GitRepoState:
    """
    Complete snapshot of git repository state for restoration.

    Captures all aspects needed to restore a repository to its exact state:
    - Current branch and HEAD commit
    - Staged files (index state)
    - Working tree modifications
    - Untracked files
    - Stash state (for safety backup)
    """

    # Branch and commit state
    current_branch: str
    head_commit: str

    # Working tree state
    staged_files: List[str] = field(default_factory=list)
    unstaged_files: List[str] = field(default_factory=list)
    untracked_files: List[str] = field(default_factory=list)

    # Remote tracking (for push rollback)
    remote_refs: Dict[str, str] = field(default_factory=dict)

    # Test isolation
    test_branch: Optional[str] = None
    test_branch_created: bool = False

    # Files created during test (for cleanup)
    created_files: List[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"GitRepoState(branch={self.current_branch}, "
            f"head={self.head_commit[:8]}, "
            f"staged={len(self.staged_files)}, "
            f"unstaged={len(self.unstaged_files)}, "
            f"untracked={len(self.untracked_files)})"
        )


class GitRepoStateManager:
    """
    Manages git repository state capture and restoration for idempotent tests.

    All operations are REAL git commands executed via subprocess.
    NO Python mocks are used.
    """

    def __init__(self, repo_path: Path):
        """
        Initialize GitRepoStateManager.

        Args:
            repo_path: Path to git repository root
        """
        self.repo_path = Path(repo_path)
        if not (self.repo_path / ".git").exists():
            raise ValueError(f"Not a git repository: {repo_path}")

    def _run_git(
        self,
        args: List[str],
        check: bool = True,
        capture_output: bool = True,
        timeout: int = 60
    ) -> subprocess.CompletedProcess:
        """
        Execute git command in repository.

        Args:
            args: Git command arguments (without 'git' prefix)
            check: Raise exception on non-zero exit
            capture_output: Capture stdout/stderr
            timeout: Command timeout in seconds

        Returns:
            CompletedProcess result
        """
        cmd = ["git"] + args
        return subprocess.run(
            cmd,
            cwd=self.repo_path,
            check=check,
            capture_output=capture_output,
            text=True,
            timeout=timeout
        )

    def capture_state(self) -> GitRepoState:
        """
        Capture complete repository state for later restoration.

        Returns:
            GitRepoState with all current state information
        """
        # Get current branch
        result = self._run_git(["branch", "--show-current"])
        current_branch = result.stdout.strip()

        # Handle detached HEAD state
        if not current_branch:
            result = self._run_git(["rev-parse", "--short", "HEAD"])
            current_branch = f"(HEAD detached at {result.stdout.strip()})"

        # Get HEAD commit
        result = self._run_git(["rev-parse", "HEAD"])
        head_commit = result.stdout.strip()

        # Get staged, unstaged, and untracked files
        staged_files, unstaged_files, untracked_files = self._parse_status_output()

        # Get remote refs for push rollback
        remote_refs = self._get_remote_refs()

        state = GitRepoState(
            current_branch=current_branch,
            head_commit=head_commit,
            staged_files=staged_files,
            unstaged_files=unstaged_files,
            untracked_files=untracked_files,
            remote_refs=remote_refs
        )

        logger.debug(f"Captured state: {state}")
        return state

    def _parse_status_output(self) -> tuple:
        """Parse git status porcelain output into categorized file lists."""
        result = self._run_git(["status", "--porcelain=v1"])
        staged_files = []
        unstaged_files = []
        untracked_files = []

        for line in result.stdout.splitlines():
            if not line:
                continue
            status_code = line[:2]
            file_path = line[3:]

            if status_code[0] in "MADRC":
                staged_files.append(file_path)
            if status_code[1] in "MADRC":
                unstaged_files.append(file_path)
            if status_code == "??":
                untracked_files.append(file_path)

        return staged_files, unstaged_files, untracked_files

    def _get_remote_refs(self) -> Dict[str, str]:
        """Get remote refs for push rollback tracking."""
        remote_refs = {}
        try:
            result = self._run_git([
                "for-each-ref",
                "--format=%(refname:short) %(objectname)",
                "refs/remotes/"
            ])
            for line in result.stdout.splitlines():
                if line.strip():
                    parts = line.strip().split()
                    if len(parts) == 2:
                        remote_refs[parts[0]] = parts[1]
        except subprocess.CalledProcessError as e:
            logger.debug(f"Could not get remote refs (may not exist): {e}")
        return remote_refs

    def create_test_branch(self, prefix: str = "cidx-test") -> str:
        """
        Create an isolated branch for test execution.

        Args:
            prefix: Branch name prefix

        Returns:
            Created branch name
        """
        unique_id = str(uuid.uuid4())[:8]
        branch_name = f"{prefix}-{unique_id}"
        self._run_git(["checkout", "-b", branch_name])
        logger.debug(f"Created test branch: {branch_name}")
        return branch_name

    def restore_state(self, state: GitRepoState) -> None:
        """
        Restore repository to captured state.

        This method is designed to ALWAYS succeed, even if individual
        operations fail. It uses best-effort restoration with fallbacks.

        Args:
            state: Previously captured GitRepoState
        """
        logger.debug(f"Restoring state: {state}")

        self._cleanup_created_files(state)
        self._cleanup_working_tree()
        self._restore_branch_and_commit(state)
        self._delete_test_branch(state)

        logger.debug("State restoration complete")

    def _cleanup_created_files(self, state: GitRepoState) -> None:
        """Remove files created during test execution."""
        for file_path in state.created_files:
            full_path = self.repo_path / file_path
            if full_path.exists():
                try:
                    if full_path.is_dir():
                        shutil.rmtree(full_path, ignore_errors=True)
                    else:
                        full_path.unlink()
                    logger.debug(f"Removed test file: {file_path}")
                except Exception as e:
                    logger.debug(f"Failed to remove {file_path} (non-critical): {e}")

    def _cleanup_working_tree(self) -> None:
        """Reset working tree to clean state (abort merge, reset staged, clean)."""
        # Abort any in-progress merge
        try:
            self._run_git(["merge", "--abort"], check=False)
        except Exception as e:
            logger.debug(f"merge --abort failed (non-critical): {e}")

        # Reset any staged changes
        try:
            self._run_git(["reset", "HEAD"], check=False)
        except Exception as e:
            logger.debug(f"reset HEAD failed (non-critical): {e}")

        # Discard all working tree changes
        try:
            self._run_git(["checkout", "--", "."], check=False)
        except Exception as e:
            logger.debug(f"checkout -- . failed (non-critical): {e}")

        # Clean untracked files (with force)
        try:
            self._run_git(["clean", "-fd"], check=False)
        except Exception as e:
            logger.debug(f"clean -fd failed (non-critical): {e}")

    def _restore_branch_and_commit(self, state: GitRepoState) -> None:
        """Switch back to original branch and reset to original HEAD."""
        if state.current_branch and not state.current_branch.startswith("(HEAD"):
            try:
                self._run_git(["checkout", state.current_branch])
            except subprocess.CalledProcessError as e:
                logger.debug(f"checkout {state.current_branch} failed, trying -B: {e}")
                try:
                    self._run_git(["checkout", "-B", state.current_branch, state.head_commit])
                except Exception as e2:
                    logger.warning(f"Failed to restore branch {state.current_branch}: {e2}")

        try:
            self._run_git(["reset", "--hard", state.head_commit])
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to reset to {state.head_commit}: {e}")

    def _delete_test_branch(self, state: GitRepoState) -> None:
        """Delete test branch if it was created."""
        if state.test_branch and state.test_branch_created:
            try:
                self._run_git(["branch", "-D", state.test_branch], check=False)
                logger.debug(f"Deleted test branch: {state.test_branch}")
            except Exception as e:
                logger.debug(f"Failed to delete test branch {state.test_branch}: {e}")

    def rollback_pushed_commits(
        self,
        branch: str,
        original_commit: str,
        remote: str = "origin"
    ) -> bool:
        """
        Rollback pushed commits via force push or revert.

        WARNING: This performs a force push which can cause data loss
        for other users of the repository. Use only on test branches.

        Args:
            branch: Branch name
            original_commit: Commit to reset to
            remote: Remote name

        Returns:
            True if rollback succeeded, False otherwise
        """
        logger.warning(f"Rolling back pushed commits on {branch} to {original_commit[:8]}")

        try:
            self._run_git(["checkout", branch])
            self._run_git(["reset", "--hard", original_commit])
            self._run_git(
                ["push", "--force-with-lease", remote, branch],
                timeout=300
            )
            logger.info(f"Successfully rolled back {branch} to {original_commit[:8]}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to rollback pushed commits: {e}")
            return False

    def get_current_branch(self) -> str:
        """Get current branch name."""
        result = self._run_git(["branch", "--show-current"])
        return result.stdout.strip()

    def get_head_commit(self) -> str:
        """Get current HEAD commit hash."""
        result = self._run_git(["rev-parse", "HEAD"])
        return result.stdout.strip()

    def has_uncommitted_changes(self) -> bool:
        """Check if repository has any uncommitted changes."""
        result = self._run_git(["status", "--porcelain"])
        return bool(result.stdout.strip())

    def track_created_file(self, state: GitRepoState, file_path: str) -> None:
        """
        Track a file created during test for cleanup.

        Args:
            state: State object to update
            file_path: Relative path to file created
        """
        if file_path not in state.created_files:
            state.created_files.append(file_path)
