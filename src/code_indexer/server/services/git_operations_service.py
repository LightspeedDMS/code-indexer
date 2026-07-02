"""
GitOperationsService: Comprehensive git operations service.

Provides 17 git operations across 5 feature groups:
- F2: Status/Inspection (git_status, git_diff, git_log)
- F3: Staging/Commit (git_stage, git_unstage, git_commit)
- F4: Remote Operations (git_push, git_pull, git_fetch)
- F5: Recovery (git_reset, git_clean, git_merge_abort, git_checkout_file)
- F6: Branch Management (git_branch_list, git_branch_create, git_branch_switch, git_branch_delete)

Implements confirmation token system for destructive operations with:
- 6-character alphanumeric tokens
- 5-minute expiration
- Single-use validation
- In-memory storage
"""

from code_indexer.server.middleware.correlation import get_correlation_id

import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from cachetools import TTLCache

from code_indexer.server.utils.config_manager import ServerConfigManager
from code_indexer.utils.git_runner import run_git_command
from code_indexer.server.logging_utils import format_error_log

# Module logger
logger = logging.getLogger(__name__)

# Token expiry constant (not configurable - internal security setting)
TOKEN_EXPIRY = 300  # 5 minutes for confirmation tokens


class GitCommandError(Exception):
    """Exception raised when a git command fails."""

    def __init__(
        self,
        message: str,
        stderr: str = "",
        returncode: int = 1,
        command: Optional[List[str]] = None,
        cwd: Optional[Path] = None,
    ):
        """
        Initialize GitCommandError.

        Args:
            message: Error message
            stderr: Standard error output from git command
            returncode: Git command return code
            command: The git command that failed
            cwd: Working directory where command was executed
        """
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode
        self.command = command or []
        self.cwd = cwd

    def __str__(self) -> str:
        """Return detailed error message with full context."""
        parts = [super().__str__()]

        if self.command:
            parts.append(f"Command: {' '.join(self.command)}")

        if self.cwd:
            parts.append(f"Working directory: {self.cwd}")

        if self.returncode:
            parts.append(f"Return code: {self.returncode}")

        if self.stderr:
            parts.append(f"stderr: {self.stderr}")

        return " | ".join(parts)


class GitOperationsService:
    """Service for executing git operations with subprocess."""

    def __init__(self, config_manager: Optional[ServerConfigManager] = None):
        """
        Initialize GitOperationsService with configuration.

        Args:
            config_manager: ServerConfigManager instance for loading git service config.
                          If None, creates a new ServerConfigManager internally.
        """
        # Thread-safe TTLCache for automatic token expiration (Issue #1, #2)
        # maxsize=10000: Reasonable limit for concurrent users
        # ttl=TOKEN_EXPIRY: Automatic cleanup after 5 minutes
        # timer=time.time: Use time.time() for testability (allows mocking)
        self._tokens: TTLCache = TTLCache(
            maxsize=10000, ttl=TOKEN_EXPIRY, timer=time.time
        )
        self._tokens_lock = threading.RLock()

        # Use config service for runtime config (for REST router compatibility)
        if config_manager is None:
            from code_indexer.server.services.config_service import get_config_service

            config_manager = get_config_service()

        self.config_manager = config_manager
        config = config_manager.get_config()

        # Bug #83 Phase 1: Load timeout configuration from config_manager
        # Handle case where no config file exists (e.g., CI/CD parity tests)
        if config is not None:
            self._git_timeouts = config.git_timeouts_config
            self._api_limits = config.api_limits_config
        else:
            # Use defaults when no config file exists
            from ..utils.config_manager import GitTimeoutsConfig, ApiLimitsConfig

            self._git_timeouts = GitTimeoutsConfig()
            self._api_limits = ApiLimitsConfig()

        # Import ActivatedRepoManager for resolving repo aliases to paths
        # (import here to avoid circular imports)
        import os
        from ..repositories.activated_repo_manager import ActivatedRepoManager

        _server_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
        self.activated_repo_manager = ActivatedRepoManager(
            data_dir=os.path.join(_server_dir, "data") if _server_dir else None
        )

    # REST API Wrapper Methods (resolve repo_alias to repo_path)

    def _trigger_migration_if_needed(
        self, repo_path: str, username: str, repo_alias: str
    ) -> None:
        """
        Trigger legacy remote migration if needed (Story #636).

        Checks if the activated repo uses legacy single-remote setup and
        automatically migrates to dual remote setup (origin=GitHub, golden=local).

        Args:
            repo_path: Path to activated repository
            username: Username
            repo_alias: Repository alias

        Note:
            This method silently succeeds if migration is not needed or already done.
            Logs warnings if golden repo metadata is not available.
        """
        try:
            # Get activated repo metadata to find golden repo alias
            from pathlib import Path as PathLib

            user_dir = PathLib(repo_path).parent
            metadata_file = user_dir / f"{repo_alias}_metadata.json"

            if not metadata_file.exists():
                logger.warning(
                    format_error_log(
                        "CACHE-GENERAL-016",
                        f"Cannot trigger migration: metadata file not found for {username}/{repo_alias}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return

            with open(metadata_file, "r") as f:
                repo_data = json.load(f)

            golden_repo_alias = repo_data.get("golden_repo_alias")
            if not golden_repo_alias:
                logger.warning(
                    format_error_log(
                        "CACHE-GENERAL-017",
                        f"Cannot trigger migration: golden_repo_alias not found in metadata for {username}/{repo_alias}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return

            # Get golden repo path from golden repo manager
            golden_repo = (
                self.activated_repo_manager.golden_repo_manager.get_golden_repo(
                    golden_repo_alias
                )
            )
            if golden_repo is None:
                logger.warning(
                    format_error_log(
                        "CACHE-GENERAL-018",
                        f"Cannot trigger migration: golden repo '{golden_repo_alias}' not found for {username}/{repo_alias}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return

            # Use canonical path resolution to handle versioned repos (Bug #3, #4 fix)
            golden_repo_path = (
                self.activated_repo_manager.golden_repo_manager.get_actual_repo_path(
                    golden_repo_alias
                )
            )

            # Trigger migration via ActivatedRepoManager
            migrated = self.activated_repo_manager._detect_and_migrate_legacy_remotes(
                repo_path, golden_repo_path
            )

            if migrated:
                logger.info(
                    f"Automatically migrated legacy remotes for {username}/{repo_alias}",
                    extra={"correlation_id": get_correlation_id()},
                )

        except Exception as e:
            # Don't fail the git operation if migration check fails
            logger.warning(
                format_error_log(
                    "CACHE-GENERAL-019",
                    f"Failed to check/trigger migration for {username}/{repo_alias}: {str(e)}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )

    def get_status(self, repo_alias: str, username: str) -> Dict[str, Any]:
        """
        Get git status for an activated repository (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup

        Returns:
            Git status dictionary with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git status fails
        """

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_status(Path(repo_path))
        result["success"] = True
        return result

    def get_diff(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Get git diff for an activated repository (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments passed to git_diff
                     (file_paths, context_lines, from_revision, to_revision, path, stat_only, etc.)

        Returns:
            Git diff dictionary with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git diff fails
        """

        # Extract file_paths if present
        file_paths = kwargs.pop("file_paths", None)

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_diff(Path(repo_path), file_paths=file_paths, **kwargs)
        result["success"] = True
        return result

    def get_log(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Get git log for an activated repository (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments passed to git_log
                     (limit, since, until, path, author, branch, etc.)
                     Note: 'since' parameter is mapped to 'since_date' internally

        Returns:
            Git log dictionary with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git log fails
        """

        # Map REST parameter name to service method name
        if "since" in kwargs:
            kwargs["since_date"] = kwargs.pop("since")

        # Extract limit with default
        limit = kwargs.pop("limit", 10)

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_log(Path(repo_path), limit=limit, **kwargs)
        result["success"] = True
        return result

    # F3: Staging/Commit Wrapper Methods

    def stage_files(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Stage files for commit (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (file_paths)

        Returns:
            Git stage result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git add fails
        """

        file_paths = kwargs.get("file_paths", [])
        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_stage(Path(repo_path), file_paths=file_paths)
        result["success"] = True
        return result

    def unstage_files(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Unstage files (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (file_paths)

        Returns:
            Git unstage result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git reset fails
        """

        file_paths = kwargs.get("file_paths", [])
        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_unstage(Path(repo_path), file_paths=file_paths)
        result["success"] = True
        return result

    def create_commit(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Create a git commit (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (message, user_email, user_name)

        Returns:
            Git commit result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git commit fails
            ValueError: If user_email or user_name fail validation
        """

        message = kwargs.get("message", "")
        # Support both author_email (from REST API) and user_email (legacy MCP)
        user_email = kwargs.get("author_email") or kwargs.get("user_email") or None
        # Support both author_name (from REST API) and user_name (legacy MCP)
        user_name = kwargs.get("author_name") or kwargs.get("user_name") or None

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )

        # If email/name not provided, use git config as fallback
        if not user_email or not user_name:
            try:
                if not user_email:
                    user_email = subprocess.check_output(
                        ["git", "config", "user.email"], cwd=repo_path, text=True
                    ).strip()
                    logger.debug(
                        f"Using git config user.email: {user_email}",
                        extra={"correlation_id": get_correlation_id()},
                    )
                if not user_name:
                    user_name = subprocess.check_output(
                        ["git", "config", "user.name"], cwd=repo_path, text=True
                    ).strip()
                    logger.debug(
                        f"Using git config user.name: {user_name}",
                        extra={"correlation_id": get_correlation_id()},
                    )
            except subprocess.CalledProcessError as e:
                logger.debug(
                    f"Git config user.email/user.name not found: {e}. "
                    "Will use provided values (may fail validation if empty).",
                    extra={"correlation_id": get_correlation_id()},
                )

        result = self.git_commit(
            Path(repo_path),
            message=message,
            user_email=user_email or "",
            user_name=user_name,
        )
        result["success"] = True
        return result

    # F4: Remote Operations Wrapper Methods

    def push_to_remote(
        self, repo_alias: str, username: str, **kwargs
    ) -> Dict[str, Any]:
        """
        Push commits to remote repository (REST API wrapper).

        Uses the server's configured SSH key for authentication (not user
        PAT credentials). This is the code path invoked from the REST API.
        The MCP git_push handler uses git_push_with_pat() for PAT-based push.

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (remote, branch)

        Returns:
            Git push result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git push fails
        """

        remote = kwargs.get("remote", "origin")
        branch = kwargs.get("branch")

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )

        # Story #636: Trigger migration before push if needed
        self._trigger_migration_if_needed(repo_path, username, repo_alias)

        result = self.git_push(Path(repo_path), remote=remote, branch=branch)
        result["success"] = True
        return result

    def pull_from_remote(
        self, repo_alias: str, username: str, **kwargs
    ) -> Dict[str, Any]:
        """
        Pull updates from remote repository (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (remote, branch)

        Returns:
            Git pull result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git pull fails
        """

        remote = kwargs.get("remote", "origin")
        branch = kwargs.get("branch")

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )

        # Story #636: Trigger migration before pull if needed
        self._trigger_migration_if_needed(repo_path, username, repo_alias)

        result = self.git_pull(Path(repo_path), remote=remote, branch=branch)
        result["success"] = True
        return result

    def fetch_from_remote(
        self, repo_alias: str, username: str, **kwargs
    ) -> Dict[str, Any]:
        """
        Fetch updates from remote repository (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (remote)

        Returns:
            Git fetch result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git fetch fails
        """

        remote = kwargs.get("remote", "origin")

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )

        # Story #636: Trigger migration before fetch if needed
        self._trigger_migration_if_needed(repo_path, username, repo_alias)

        result = self.git_fetch(Path(repo_path), remote=remote)
        result["success"] = True
        return result

    # F5: Recovery Operations Wrapper Methods

    def reset_repository(
        self, repo_alias: str, username: str, **kwargs
    ) -> Dict[str, Any]:
        """
        Reset repository to a specific commit (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (mode, commit_hash, confirmation_token)

        Returns:
            Git reset result with success field OR requires_confirmation/token

        Raises:
            FileNotFoundError: If repository not found
            ValueError: If hard reset attempted without valid token
            GitCommandError: If git reset fails
        """

        mode = kwargs.get("mode", "mixed")
        commit_hash = kwargs.get("commit_hash")
        confirmation_token = kwargs.get("confirmation_token")

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_reset(
            Path(repo_path),
            mode=mode,
            commit_hash=commit_hash,
            confirmation_token=confirmation_token,
        )
        # Note: result already contains success field from git_reset
        # or requires_confirmation/token for hard reset
        return result

    def clean_repository(
        self, repo_alias: str, username: str, **kwargs
    ) -> Dict[str, Any]:
        """
        Remove untracked files and directories (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (confirmation_token)

        Returns:
            Git clean result with success field OR requires_confirmation/token

        Raises:
            FileNotFoundError: If repository not found
            ValueError: If attempted without valid token
            GitCommandError: If git clean fails
        """

        confirmation_token = kwargs.get("confirmation_token")

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_clean(Path(repo_path), confirmation_token=confirmation_token)
        # Note: result already contains success field from git_clean
        # or requires_confirmation/token
        return result

    def abort_merge(self, repo_alias: str, username: str) -> Dict[str, Any]:
        """
        Abort an in-progress merge (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup

        Returns:
            Git merge abort result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git merge --abort fails
        """

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_merge_abort(Path(repo_path))
        result["success"] = True
        return result

    def checkout_file(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Restore file(s) to HEAD state (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (file_paths)

        Returns:
            Git checkout result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git checkout fails
        """

        file_paths = kwargs.get("file_paths", [])

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )

        # git_checkout_file expects a single file_path string, not a list
        # For REST API compatibility, we accept file_paths list and process first file
        # TODO: Enhance git_checkout_file to support multiple files
        file_path = file_paths[0] if file_paths else ""

        result = self.git_checkout_file(Path(repo_path), file_path=file_path)
        result["success"] = True
        return result

    # F6: Branch Management Wrapper Methods

    def list_branches(self, repo_alias: str, username: str) -> Dict[str, Any]:
        """
        List all branches (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup

        Returns:
            Git branch list with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git branch fails
        """

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_branch_list(Path(repo_path))
        result["success"] = True
        return result

    def create_branch(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Create a new branch (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (branch_name)

        Returns:
            Git branch create result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git branch fails
        """

        branch_name = kwargs.get("branch_name", "")

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_branch_create(Path(repo_path), branch_name=branch_name)
        result["success"] = True
        return result

    def switch_branch(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Switch to a different branch (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (branch_name)

        Returns:
            Git branch switch result with success field

        Raises:
            FileNotFoundError: If repository not found
            GitCommandError: If git checkout/switch fails
        """

        branch_name = kwargs.get("branch_name", "")

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_branch_switch(Path(repo_path), branch_name=branch_name)
        result["success"] = True
        return result

    def delete_branch(self, repo_alias: str, username: str, **kwargs) -> Dict[str, Any]:
        """
        Delete a branch (REST API wrapper).

        Args:
            repo_alias: User's repository alias
            username: Username for repository lookup
            **kwargs: Additional arguments (branch_name, confirmation_token)

        Returns:
            Git branch delete result with success field OR requires_confirmation/token

        Raises:
            FileNotFoundError: If repository not found
            ValueError: If attempted without valid token
            GitCommandError: If git branch delete fails
        """

        branch_name = kwargs.get("branch_name", "")
        confirmation_token = kwargs.get("confirmation_token")

        repo_path = self.activated_repo_manager.get_activated_repo_path(
            username=username, user_alias=repo_alias
        )
        result = self.git_branch_delete(
            Path(repo_path),
            branch_name=branch_name,
            confirmation_token=confirmation_token,
        )
        # Note: result already contains success field from git_branch_delete
        # or requires_confirmation/token
        return result

    # F2: Status/Inspection Operations

    def git_status(self, repo_path: Path) -> Dict[str, Any]:
        """
        Get git repository status.

        Args:
            repo_path: Path to git repository

        Returns:
            Dict with staged, unstaged, and untracked file lists

        Raises:
            GitCommandError: If git status fails
        """
        try:
            cmd = ["git", "status", "--porcelain=v1"]
            result = run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            # Parse porcelain v1 format: XY PATH
            # X = staged status, Y = unstaged status
            staged = []
            unstaged = []
            untracked = []

            for line in result.stdout.splitlines():
                if not line:
                    continue

                status_code = line[:2]
                file_path = line[3:]

                # Staged files (first character)
                if status_code[0] in "MADRC":
                    staged.append(file_path)

                # Unstaged files (second character)
                if status_code[1] in "MADRC":
                    unstaged.append(file_path)

                # Untracked files
                if status_code == "??":
                    untracked.append(file_path)

            return {"staged": staged, "unstaged": unstaged, "untracked": untracked}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git status failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
                command=cmd,
                cwd=repo_path,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git status timed out after {e.timeout}s",
                stderr="",
                command=cmd,
                cwd=repo_path,
            )

    def git_diff(
        self,
        repo_path: Path,
        file_paths: Optional[List[str]] = None,
        context_lines: Optional[int] = None,
        from_revision: Optional[str] = None,
        to_revision: Optional[str] = None,
        path: Optional[str] = None,
        stat_only: Optional[bool] = None,
        offset: int = 0,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Get git diff output with pagination support.

        Args:
            repo_path: Path to git repository
            file_paths: Optional list of specific files to diff
            context_lines: Number of context lines to show (uses -U flag)
            from_revision: Starting revision for diff
            to_revision: Ending revision for diff (requires from_revision)
            path: Specific path to limit diff to
            stat_only: Show only file statistics (--stat flag)
            offset: Number of lines to skip (default: 0)
            limit: Maximum lines to return (default: 500, max: 5000)

        Returns:
            Dict with diff_text, files_changed, and pagination metadata

        Raises:
            GitCommandError: If git diff fails
        """
        try:
            # Story #686: Validate and apply pagination defaults
            if offset < 0:
                offset = 0
            effective_limit = (
                self._api_limits.default_diff_lines if limit is None else limit
            )
            effective_limit = min(effective_limit, self._api_limits.max_diff_lines)

            cmd = ["git", "diff"]

            # Add context lines flag
            if context_lines is not None:
                cmd.append(f"-U{context_lines}")

            # Add stat flag
            if stat_only:
                cmd.append("--stat")

            # Add revision range or single revision
            if from_revision and to_revision:
                cmd.append(f"{from_revision}..{to_revision}")
            elif from_revision:
                cmd.append(from_revision)

            # Add path filter with -- separator
            if path:
                cmd.append("--")
                cmd.append(path)
            elif file_paths:
                # Legacy file_paths parameter (kept for backward compatibility)
                cmd.extend(file_paths)

            result = run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            full_diff_text = result.stdout
            all_lines = full_diff_text.splitlines(keepends=True)
            total_lines = len(all_lines)

            # Story #686: Apply pagination (offset and limit)
            end_index = offset + effective_limit
            selected_lines = all_lines[offset:end_index]
            diff_text = "".join(selected_lines)

            # Calculate pagination metadata
            lines_returned = len(selected_lines)
            has_more = (offset + lines_returned) < total_lines
            next_offset = (offset + lines_returned) if has_more else None

            # Count changed files. Unified-diff format has "diff --git" markers;
            # --stat output (stat_only=True) has no such markers but emits a
            # summary line like "N files changed, ...". Fall back to parsing
            # that summary when the marker count is zero and output is non-empty.
            files_changed = full_diff_text.count("diff --git")
            if files_changed == 0 and full_diff_text:
                stat_summary = re.search(r"(\d+) files? changed", full_diff_text)
                if stat_summary:
                    files_changed = int(stat_summary.group(1))

            return {
                "diff_text": diff_text,
                "files_changed": files_changed,
                # Story #686: Pagination metadata
                "lines_returned": lines_returned,
                "total_lines": total_lines,
                "has_more": has_more,
                "next_offset": next_offset,
                "offset": offset,
                "limit": limit,
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git diff failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
                command=["git", "diff"],
                cwd=repo_path,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git diff timed out after {e.timeout}s",
                stderr="",
                command=["git", "diff"],
                cwd=repo_path,
            )

    def git_log(
        self,
        repo_path: Path,
        limit: int = 50,
        offset: int = 0,
        since_date: Optional[str] = None,
        until: Optional[str] = None,
        author: Optional[str] = None,
        branch: Optional[str] = None,
        path: Optional[str] = None,
        aggregation_mode: Optional[str] = None,
        response_format: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get git commit history with pagination support.

        Args:
            repo_path: Path to git repository
            limit: Maximum number of commits to return (default: 50, max: 500)
            offset: Number of commits to skip (default: 0)
            since_date: Optional date filter for commits after this date (e.g., "2025-01-10")
            until: Optional date filter for commits before this date
            author: Optional author filter
            branch: Optional branch to get log from
            path: Optional path filter to show only commits affecting this path
            aggregation_mode: Optional MCP aggregation mode (affects response formatting)
            response_format: Optional MCP response format (affects response structure)

        Returns:
            Dict with commits list and pagination metadata

        Raises:
            GitCommandError: If git log fails
        """
        try:
            # Story #686: Validate offset
            if offset < 0:
                offset = 0

            # Story #686: Cap limit at configured max_log_commits
            effective_limit = min(limit, self._api_limits.max_log_commits)

            # Get total commit count (for pagination metadata)
            # Use branch or HEAD as the revision specifier
            rev_spec = branch if branch else "HEAD"
            count_cmd = ["git", "rev-list", "--count", rev_spec]

            # Add filters after the revision specifier
            if since_date:
                count_cmd.extend(["--since", since_date])
            if until:
                count_cmd.extend(["--until", until])
            if author:
                count_cmd.extend(["--author", author])
            if path:
                count_cmd.append("--")
                count_cmd.append(path)

            count_result = run_git_command(
                count_cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )
            total_commits = int(count_result.stdout.strip())

            # Build log command.
            # ASCII Unit Separator (\x1f) is not valid in git refs or commit
            # messages, so it is safe as an inter-field delimiter. Using JSON
            # as the format string (former behavior) silently dropped commits
            # whose subject contained `"` or `\` because json.loads raised
            # JSONDecodeError per-line and the except branch swallowed it.
            # Fields order: hash, author, date, message.  Bug #825 fix.
            format_str = "%H\x1f%an\x1f%ai\x1f%s"
            cmd = ["git", "log", f"--format={format_str}", f"-n{effective_limit}"]

            # Story #686: Add offset via --skip
            if offset > 0:
                cmd.append(f"--skip={offset}")

            # Add date filters
            if since_date:
                cmd.append(f"--since={since_date}")
            if until:
                cmd.append(f"--until={until}")
            if author:
                cmd.append(f"--author={author}")
            if branch:
                cmd.append(branch)
            if path:
                cmd.append("--")
                cmd.append(path)

            result = run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            commits = []
            for line in result.stdout.splitlines():
                # Skip blank lines only. Do NOT strip the raw payload —
                # leading/trailing whitespace inside a commit subject must
                # be preserved.
                if not line.strip():
                    continue
                # maxsplit=3 preserves any stray \x1f inside the subject
                # (git won't emit one there, but stay strict).
                parts = line.split("\x1f", 3)
                if len(parts) != 4:
                    # Malformed output shouldn't happen with this format
                    # string; surface it at WARNING instead of silent drop.
                    logger.warning(
                        "git log produced malformed line for %s (expected 4 "
                        "fields separated by \\x1f, got %d): %r",
                        repo_path,
                        len(parts),
                        line,
                    )
                    continue
                commit_hash, commit_author, commit_date, commit_message = parts
                commits.append(
                    {
                        "commit_hash": commit_hash,
                        "author": commit_author,
                        "date": commit_date,
                        "message": commit_message,
                    }
                )

            # Story #686: Calculate pagination metadata
            commits_returned = len(commits)
            has_more = (offset + commits_returned) < total_commits
            next_offset = (offset + commits_returned) if has_more else None

            return {
                "commits": commits,
                "commits_returned": commits_returned,
                "total_commits": total_commits,
                "has_more": has_more,
                "next_offset": next_offset,
                "offset": offset,
                "limit": limit,
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git log failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
                command=["git", "log"],
                cwd=repo_path,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git log timed out after {e.timeout}s",
                stderr="",
                command=["git", "log"],
                cwd=repo_path,
            )

    # F3: Staging/Commit Operations

    def git_stage(self, repo_path: Path, file_paths: List[str]) -> Dict[str, Any]:
        """
        Stage files for commit.

        Args:
            repo_path: Path to git repository
            file_paths: List of file paths to stage

        Returns:
            Dict with success flag and staged_files list

        Raises:
            ValueError: If .code-indexer files are in file_paths
            GitCommandError: If git add fails
        """
        try:
            # Validate no .code-indexer files before staging
            validated_paths = self._validate_no_code_indexer_files(file_paths)

            cmd = ["git", "add"] + validated_paths

            run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {"success": True, "staged_files": validated_paths}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git add failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git add timed out after {e.timeout}s", stderr="")

    def git_unstage(self, repo_path: Path, file_paths: List[str]) -> Dict[str, Any]:
        """
        Unstage files.

        Args:
            repo_path: Path to git repository
            file_paths: List of file paths to unstage

        Returns:
            Dict with success flag and unstaged_files list

        Raises:
            GitCommandError: If git reset fails
        """
        try:
            cmd = ["git", "reset", "HEAD"] + file_paths

            run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {"success": True, "unstaged_files": file_paths}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git reset HEAD failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git reset timed out after {e.timeout}s", stderr="")

    def git_commit(
        self,
        repo_path: Path,
        message: str,
        user_email: str,
        user_name: Optional[str] = None,
        committer_email: Optional[str] = None,
        committer_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a git commit with dual attribution (Story #641, Story #402).

        Uses dual attribution model:
        - Git Author: user_email parameter (co_author_email - actual Claude.ai user)
        - Git Committer: committer_email/committer_name if provided (from PAT credential,
          Story #402), otherwise falls back to author identity (user_email/user_name).
          This ensures forges like GitLab that verify committer email accept the commit.
        - Commit message: Injects AUTHOR prefix for audit trail

        Args:
            repo_path: Path to git repository
            message: Commit message (user's actual message)
            user_email: MANDATORY co_author_email (actual Claude.ai user) - becomes Git author
            user_name: Optional user name (derived from email if not provided)
            committer_email: Optional committer email from PAT credential. Falls back to
                user_email when None or empty.
            committer_name: Optional committer name from PAT credential. Falls back to
                user_name when None.

        Returns:
            Dict with success, commit_hash, message, author, and committer

        Raises:
            GitCommandError: If git commit fails
            ValueError: If user_email is missing, empty, has invalid format, or .code-indexer files are staged
        """
        try:
            # Story #641 AC #3: Validate co_author_email parameter is MANDATORY
            # All parameter validation must happen before any I/O operations
            if user_email is None or user_email == "":
                raise ValueError(
                    "co_author_email parameter is required and cannot be None or empty"
                )

            # Story #641 AC #4: Validate user_email format (RFC 5322 basic format)
            email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            if not re.match(email_pattern, user_email):
                raise ValueError(
                    f"Invalid email format for co_author_email: {user_email}"
                )

            # Derive author name from email if not provided
            if not user_name:
                user_name = user_email.split("@")[0]

            # Validate user_name (alphanumeric + space, hyphen, underscore only)
            name_pattern = r"^[a-zA-Z0-9 _-]+$"
            if not re.match(name_pattern, user_name):
                raise ValueError(f"Invalid user name format: {user_name}")

            # Check for staged .code-indexer files before committing
            staged_result = run_git_command(
                ["git", "diff", "--cached", "--name-only"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )
            staged_files = [
                f.strip() for f in staged_result.stdout.splitlines() if f.strip()
            ]

            # Validate no .code-indexer files are staged
            forbidden_patterns = [".code-indexer/", ".code-indexer-override.yaml"]
            forbidden_staged = [
                f
                for f in staged_files
                if any(pattern in f for pattern in forbidden_patterns)
            ]

            if forbidden_staged:
                raise ValueError(
                    f"Commit blocked: .code-indexer files are staged. "
                    f"These files should never be committed. Blocked files: {forbidden_staged}"
                )

            # Sanitize user message to prevent trailer injection
            # Remove any lines that look like our trailers to prevent forgery
            sanitized_lines = []
            for line in message.split("\n"):
                # Strip lines that start with our reserved trailer keys
                if not line.startswith("Actual-Author:") and not line.startswith(
                    "Committed-Via:"
                ):
                    sanitized_lines.append(line)
            sanitized_message = "\n".join(sanitized_lines)

            # Use Git trailers format (injection-safe)
            # Git trailers: https://git-scm.com/docs/git-interpret-trailers
            # Format: Key: value (no prefix ambiguity, structured metadata)
            attributed_message = f"{sanitized_message}\n\nActual-Author: {user_email}\nCommitted-Via: CIDX API"

            # Story #641 AC #5: Set Git Author via environment variables
            # Story #402: Set Git Committer from PAT credential identity (committer_email/
            # committer_name), falling back to author identity when not provided.
            # This ensures forges like GitLab that verify committer email accept the commit.
            env = os.environ.copy()
            env["GIT_AUTHOR_NAME"] = user_name
            env["GIT_AUTHOR_EMAIL"] = user_email
            env["GIT_COMMITTER_EMAIL"] = (
                committer_email if committer_email else user_email
            )
            env["GIT_COMMITTER_NAME"] = committer_name if committer_name else user_name

            cmd = ["git", "commit", "-m", attributed_message]

            run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
                env=env,
            )

            # Get full commit hash using git rev-parse HEAD
            # (git commit output only shows short hash)
            hash_result = run_git_command(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )
            commit_hash = hash_result.stdout.strip()

            # Get actual committer email from the commit
            committer_result = run_git_command(
                ["git", "show", "-s", "--format=%ce", "HEAD"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )
            actual_committer = committer_result.stdout.strip()

            return {
                "success": True,
                "commit_hash": commit_hash,
                "message": message,
                "author": user_email,
                "committer": actual_committer,
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git commit failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git commit timed out after {e.timeout}s", stderr="")

    # F4: Remote Operations

    def _count_pushed_commits(
        self, result: subprocess.CompletedProcess, repo_path: Path
    ) -> int:
        """Count commits from git push result by parsing stderr ref updates.

        Bug #569: git push writes ref-update info (e.g., 'abc1234..def5678')
        to stderr, not stdout. This method checks stderr and uses
        git rev-list --count for accurate commit counting. Falls back to 1
        with a warning when rev-list fails, since the push already succeeded
        and at least 1 commit was pushed.
        """
        stderr_text = result.stderr or ""
        match = re.search(r"([0-9a-f]{7,40})\.\.\.?([0-9a-f]{7,40})", stderr_text)
        if match:
            try:
                count_result = run_git_command(
                    [
                        "git",
                        "rev-list",
                        "--count",
                        f"{match.group(1)}..{match.group(2)}",
                    ],
                    cwd=repo_path,
                    check=True,
                )
                return int(count_result.stdout.strip())
            except (subprocess.CalledProcessError, ValueError) as e:
                logger.warning(
                    "Failed to count pushed commits via rev-list: %s. "
                    "Falling back to 1 (push succeeded).",
                    e,
                )
                return 1
        return 0

    def git_push(
        self, repo_path: Path, remote: str = "origin", branch: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Push commits to remote repository.

        Args:
            repo_path: Path to git repository
            remote: Remote name (default: "origin")
            branch: Optional branch name

        Returns:
            Dict with success flag and pushed_commits count

        Raises:
            GitCommandError: If git push fails
        """
        try:
            cmd = ["git", "push", remote]
            if branch:
                cmd.append(branch)

            result = run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_remote_timeout,
                check=True,
            )

            pushed_commits = self._count_pushed_commits(result, repo_path)

            return {"success": True, "pushed_commits": pushed_commits}

        except subprocess.CalledProcessError as e:
            stderr = getattr(e, "stderr", "")

            if "Authentication" in stderr or "Permission denied" in stderr:
                raise GitCommandError(
                    f"git push authentication failed: {stderr}",
                    stderr=stderr,
                    returncode=e.returncode,
                )
            elif "Could not resolve host" in stderr or "Network" in stderr:
                raise GitCommandError(
                    f"git push network error: {stderr}",
                    stderr=stderr,
                    returncode=e.returncode,
                )
            else:
                raise GitCommandError(
                    f"git push failed: {e}", stderr=stderr, returncode=e.returncode
                )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git push timed out after {e.timeout}s", stderr="")

    def git_push_with_pat(
        self,
        repo_path: Path,
        remote: str,
        branch: Optional[str],
        credential: Dict[str, Any],
        remote_url: Optional[str] = None,
        set_upstream: bool = True,
    ) -> Dict[str, Any]:
        """Push commits using PAT authentication via GIT_ASKPASS.

        Story #387: PAT-Authenticated Git Push with User Attribution.
        Story #445: Fix branches with no upstream tracking.

        Uses GIT_ASKPASS to provide the PAT, converts SSH remotes to HTTPS,
        and sets GIT_AUTHOR/COMMITTER env vars from stored identity.

        Args:
            repo_path: Path to git repository
            remote: Remote name (e.g., "origin")
            branch: Optional branch name; auto-detected from HEAD when None
            credential: Dict with token, git_user_name, git_user_email
            remote_url: Pre-resolved remote URL (avoids redundant subprocess call)
            set_upstream: When True (default), sets upstream tracking after push

        Returns:
            Dict with success flag and push details

        Raises:
            GitCommandError: If git push fails
        """
        from code_indexer.server.services.git_credential_helper import (
            GitCredentialHelper,
        )

        helper = GitCredentialHelper()

        # Use provided URL or fetch it
        if remote_url is None:
            try:
                url_result = run_git_command(
                    ["git", "remote", "get-url", remote],
                    cwd=repo_path,
                    check=True,
                )
                remote_url = url_result.stdout.strip()
            except subprocess.CalledProcessError as e:
                raise GitCommandError(
                    f"Failed to get remote URL for '{remote}': {e}",
                    stderr=getattr(e, "stderr", ""),
                    returncode=e.returncode,
                )

        # Convert SSH to HTTPS for PAT-based auth
        https_url = helper.convert_ssh_to_https(remote_url)

        # Create askpass script
        askpass_path = helper.create_askpass_script(credential["token"])
        try:
            # Build environment with PAT auth and user identity
            env = os.environ.copy()
            env["GIT_ASKPASS"] = str(askpass_path)
            env["GIT_TERMINAL_PROMPT"] = "0"

            # Set author/committer from stored identity
            if credential.get("git_user_name"):
                env["GIT_AUTHOR_NAME"] = credential["git_user_name"]
                env["GIT_COMMITTER_NAME"] = credential["git_user_name"]
            if credential.get("git_user_email"):
                env["GIT_AUTHOR_EMAIL"] = credential["git_user_email"]
                env["GIT_COMMITTER_EMAIL"] = credential["git_user_email"]

            # Auto-detect branch when not provided (Story #445)
            if not branch:
                rev_result = run_git_command(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=repo_path,
                    check=True,
                )
                branch = rev_result.stdout.strip()
                # Guard against detached HEAD (rev-parse returns "HEAD" literally)
                if not branch or branch == "HEAD":
                    raise GitCommandError(
                        "Cannot push: repository is in detached HEAD state. "
                        "Provide an explicit branch name.",
                        stderr="",
                        returncode=1,
                    )

            # Push using HTTPS URL with explicit refspec (Story #445)
            # HEAD:refs/heads/<branch> works even when no upstream tracking exists
            cmd = ["git", "push", https_url, f"HEAD:refs/heads/{branch}"]

            result = run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_remote_timeout,
                check=True,
                env=env,
            )

            # Set upstream tracking after successful push (Story #445)
            upstream_warning = None
            if set_upstream:
                try:
                    run_git_command(
                        [
                            "git",
                            "branch",
                            f"--set-upstream-to={remote}/{branch}",
                            branch,
                        ],
                        cwd=repo_path,
                        check=True,
                    )
                except subprocess.CalledProcessError as upstream_err:
                    logger.warning(
                        "Failed to set upstream tracking for %s/%s: %s",
                        remote,
                        branch,
                        upstream_err,
                    )
                    upstream_warning = (
                        f"Push succeeded but upstream tracking not set: {upstream_err}"
                    )

            pushed_commits = self._count_pushed_commits(result, repo_path)

            response: Dict[str, Any] = {
                "success": True,
                "pushed_commits": pushed_commits,
            }
            if upstream_warning:
                response["warning"] = upstream_warning
            return response

        except subprocess.CalledProcessError as e:
            stderr = getattr(e, "stderr", "")
            raise GitCommandError(
                f"git push failed: {stderr or e}",
                stderr=stderr,
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git push timed out after {e.timeout}s", stderr="")
        finally:
            helper.cleanup_askpass_script(askpass_path)

    def git_pull(
        self, repo_path: Path, remote: str = "origin", branch: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Pull updates from remote repository.

        Args:
            repo_path: Path to git repository
            remote: Remote name (default: "origin")
            branch: Optional branch name

        Returns:
            Dict with success, updated_files count, and conflicts list

        Raises:
            GitCommandError: If git pull fails
        """
        try:
            cmd = ["git", "pull", remote]
            if branch:
                cmd.append(branch)

            result = run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_remote_timeout,
                check=False,
            )

            conflicts = []
            if result.returncode != 0 or "CONFLICT" in result.stdout:
                for line in result.stdout.splitlines():
                    if "CONFLICT" in line:
                        match = re.search(r"Merge conflict in (.+)", line)
                        if match:
                            conflicts.append(match.group(1))

            updated_files = 0
            if "file changed" in result.stdout or "files changed" in result.stdout:
                match = re.search(r"(\d+) files? changed", result.stdout)
                if match:
                    updated_files = int(match.group(1))

            return {
                "success": result.returncode == 0 and not conflicts,
                "updated_files": updated_files,
                "conflicts": conflicts,
            }

        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git pull timed out after {e.timeout}s", stderr="")

    def git_fetch(self, repo_path: Path, remote: str = "origin") -> Dict[str, Any]:
        """
        Fetch updates from remote repository.

        Args:
            repo_path: Path to git repository
            remote: Remote name (default: "origin")

        Returns:
            Dict with success flag and fetched_refs list

        Raises:
            GitCommandError: If git fetch fails
        """
        try:
            result = run_git_command(
                ["git", "fetch", remote],
                cwd=repo_path,
                timeout=self._git_timeouts.git_remote_timeout,
                check=True,
            )

            fetched_refs = []
            for line in result.stdout.splitlines():
                if " -> " in line or "FETCH_HEAD" in line:
                    fetched_refs.append(line.strip())

            return {"success": True, "fetched_refs": fetched_refs}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git fetch failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git fetch timed out after {e.timeout}s", stderr="")

    # F5: Recovery Operations

    def git_reset(
        self,
        repo_path: Path,
        mode: str,
        commit_hash: Optional[str] = None,
        confirmation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Reset repository to a specific commit.

        Args:
            repo_path: Path to git repository
            mode: Reset mode ("soft", "mixed", "hard")
            commit_hash: Optional commit hash (default: HEAD)
            confirmation_token: Required for hard reset

        Returns:
            Dict with success/reset_mode/target_commit OR requires_confirmation/token

        Raises:
            ValueError: If hard reset attempted without valid token
            GitCommandError: If git reset fails
        """
        if mode == "hard":
            if not confirmation_token:
                token = self._generate_confirmation_token("git_reset_hard")
                return {"requires_confirmation": True, "token": token}

            if not self._validate_confirmation_token(
                "git_reset_hard", confirmation_token
            ):
                raise ValueError("Invalid or expired confirmation token")

        try:
            target = commit_hash or "HEAD"
            cmd = ["git", "reset", f"--{mode}", target]

            run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {"success": True, "reset_mode": mode, "target_commit": target}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git reset failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git reset timed out after {e.timeout}s", stderr="")

    def git_clean(
        self, repo_path: Path, confirmation_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Remove untracked files and directories.

        Args:
            repo_path: Path to git repository
            confirmation_token: Required for this destructive operation

        Returns:
            Dict with success/removed_files OR requires_confirmation/token

        Raises:
            ValueError: If attempted without valid token
            GitCommandError: If git clean fails
        """
        if not confirmation_token:
            token = self._generate_confirmation_token("git_clean")
            return {"requires_confirmation": True, "token": token}

        if not self._validate_confirmation_token("git_clean", confirmation_token):
            raise ValueError("Invalid or expired confirmation token")

        try:
            result = run_git_command(
                ["git", "clean", "-fd"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            removed_files = []
            for line in result.stdout.splitlines():
                if line.startswith("Removing "):
                    file_path = line.replace("Removing ", "").strip()
                    removed_files.append(file_path)

            return {"success": True, "removed_files": removed_files}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git clean failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git clean timed out after {e.timeout}s", stderr="")

    def git_merge_abort(self, repo_path: Path) -> Dict[str, Any]:
        """
        Abort an in-progress merge.

        Args:
            repo_path: Path to git repository

        Returns:
            Dict with success flag and aborted flag

        Raises:
            GitCommandError: If git merge --abort fails
        """
        try:
            run_git_command(
                ["git", "merge", "--abort"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {"success": True, "aborted": True}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git merge --abort failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git merge --abort timed out after {e.timeout}s", stderr=""
            )

    def merge_branch(self, repo_path: Path, source_branch: str) -> Dict[str, Any]:
        """Merge source_branch into current branch with conflict detection.

        Args:
            repo_path: Path to git repository
            source_branch: Branch name to merge into current branch

        Returns:
            Dict with:
              - success: bool
              - merge_summary: str (git merge stdout)
              - conflicts: list of conflict dicts (empty if clean merge)

        Raises:
            GitCommandError: If merge fails for a reason other than conflicts
        """
        try:
            result = run_git_command(
                ["git", "merge", source_branch],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=False,  # Don't raise on conflicts (non-zero exit)
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(f"git merge timed out after {e.timeout}s", stderr="")

        if result.returncode == 0:
            return {
                "success": True,
                "merge_summary": result.stdout.strip(),
                "conflicts": [],
            }

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        if "CONFLICT" in stdout:
            conflicts = self._parse_conflicts(stdout, repo_path)
            return {
                "success": False,
                "merge_summary": stdout.strip(),
                "conflicts": conflicts,
            }

        # Other failure (invalid branch, already in progress, etc.)
        error_msg = stderr.strip() or stdout.strip()
        raise GitCommandError(
            f"git merge failed: {error_msg}",
            stderr=stderr,
            returncode=result.returncode,
        )

    def _parse_conflicts(
        self, merge_output: str, repo_path: Path
    ) -> List[Dict[str, Any]]:
        """Parse conflict information from git merge output and git status.

        Two-phase detection:
        1. Parse CONFLICT lines from merge stdout for type info
        2. Augment with git status --porcelain for definitive file list with
           status codes

        Args:
            merge_output: stdout from the failed git merge command
            repo_path: Path to the repository for git status call

        Returns:
            List of conflict dicts with file, status, conflict_type, is_binary
        """
        # Phase 1: Parse CONFLICT lines for human-readable type info
        conflict_types: Dict[str, str] = {}  # file_path -> conflict_type
        for line in merge_output.split("\n"):
            if "CONFLICT" not in line:
                continue
            type_match = re.search(r"CONFLICT \(([^)]+)\)", line)
            conflict_type = type_match.group(1) if type_match else "unknown"

            # Try "Merge conflict in <path>" first
            path_match = re.search(r"Merge conflict in (.+)$", line)
            if path_match:
                file_path = path_match.group(1).strip()
                conflict_types[file_path] = conflict_type
            else:
                # Try "CONFLICT (...): <path> deleted in..." or similar
                path_match = re.search(
                    r"CONFLICT \([^)]+\): (.+?)(?:\s+deleted|\s+renamed|\s+added)",
                    line,
                )
                if path_match:
                    file_path = path_match.group(1).strip()
                    conflict_types[file_path] = conflict_type

        # Phase 2: git status --porcelain for definitive conflict list
        conflicts: List[Dict[str, Any]] = []
        try:
            status_result = run_git_command(
                ["git", "status", "--porcelain"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )
            for line in status_result.stdout.split("\n"):
                if len(line) < 4:
                    continue
                status_code = line[:2]
                file_path = line[3:]
                # Unmerged status codes
                if status_code in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                    is_binary = self._check_if_binary_conflict(repo_path, file_path)
                    conflict_entry: Dict[str, Any] = {
                        "file": file_path,
                        "status": status_code,
                        "conflict_type": conflict_types.get(file_path, "unknown"),
                        "is_binary": is_binary,
                    }
                    conflicts.append(conflict_entry)
        except (subprocess.CalledProcessError, GitCommandError) as e:
            logger.warning(
                f"git status --porcelain failed after merge conflict, falling back to CONFLICT line parsing: {e}"
            )
            # If status fails, fall back to CONFLICT line parsing only
            for file_path, conflict_type in conflict_types.items():
                conflicts.append(
                    {
                        "file": file_path,
                        "status": "UU",  # Assume both modified
                        "conflict_type": conflict_type,
                        "is_binary": False,
                    }
                )

        return conflicts

    def _check_if_binary_conflict(self, repo_path: Path, file_path: str) -> bool:
        """Check if a conflicted file is binary (has no conflict markers).

        A binary file will not contain the '<<<<<<< ' conflict marker text.
        If the file cannot be read as UTF-8 text it is also treated as binary.

        Args:
            repo_path: Path to the repository root
            file_path: Relative path to the conflicted file

        Returns:
            True if the file is binary or unreadable, False if it contains
            text conflict markers
        """
        try:
            full_path = repo_path / file_path
            content = full_path.read_text(encoding="utf-8", errors="strict")
            return "<<<<<<< " not in content
        except (UnicodeDecodeError, OSError):
            return True  # Cannot read as text — treat as binary

    def _safe_repo_file_path(self, repo_path: Path, file_path: str) -> Path:
        """Resolve file_path relative to repo_path, ensuring it stays within the repo."""
        repo_resolved = repo_path.resolve()
        resolved = (repo_path / file_path).resolve()
        if not resolved.is_relative_to(repo_resolved):
            raise ValueError(f"File path escapes repository boundary: {file_path}")
        return resolved

    def git_conflict_status(self, repo_path: Path) -> Dict[str, Any]:
        """Get detailed conflict status including conflict marker regions.

        Returns:
            Dict with:
              - in_merge: bool (whether merge is in progress)
              - conflicted_files: list of file dicts with regions
              - total_conflicts: int
        """
        # Check if merge is in progress
        merge_head = repo_path / ".git" / "MERGE_HEAD"
        in_merge = merge_head.exists()

        # Get conflicted files from git status
        result = run_git_command(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            timeout=self._git_timeouts.git_local_timeout,
            check=True,
        )

        conflicted_files = []
        for line in result.stdout.split("\n"):
            if len(line) < 4:
                continue
            status_code = line[:2]
            file_path = line[3:]
            if status_code not in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                continue

            if status_code == "DD":
                conflicted_files.append(
                    {
                        "file": file_path,
                        "status": "both_deleted",
                        "regions": [],
                        "is_binary": False,
                    }
                )
                continue

            regions = self.parse_conflict_markers(repo_path, file_path)
            conflicted_files.append(
                {
                    "file": file_path,
                    "status": status_code,
                    "regions": regions,
                    "is_binary": len(regions) == 0,
                }
            )

        return {
            "in_merge": in_merge,
            "conflicted_files": conflicted_files,
            "total_conflicts": len(conflicted_files),
        }

    def parse_conflict_markers(
        self, repo_path: Path, file_path: str
    ) -> List[Dict[str, Any]]:
        """Parse conflict markers from a file, extracting ours/theirs regions.

        Returns list of region dicts with:
          - start_line: int (1-based)
          - end_line: int (1-based)
          - ours_label: str (e.g., "HEAD")
          - theirs_label: str (e.g., "feature-branch")
          - ours_content: str
          - theirs_content: str
        """
        try:
            full_path = self._safe_repo_file_path(repo_path, file_path)
            content = full_path.read_text(encoding="utf-8", errors="strict")
            lines = content.split("\n")
        except (UnicodeDecodeError, OSError):
            return []

        regions = []
        current_region = None
        section = None  # "ours" or "theirs"
        ours_lines: List[str] = []
        theirs_lines: List[str] = []

        for i, line in enumerate(lines):
            if line.startswith("<<<<<<< "):
                current_region = {
                    "start_line": i + 1,
                    "ours_label": line[8:],
                }
                section = "ours"
                ours_lines = []
                theirs_lines = []
            elif line.startswith("=======") and current_region is not None:
                section = "theirs"
            elif line.startswith(">>>>>>> ") and current_region is not None:
                current_region["end_line"] = i + 1
                current_region["theirs_label"] = line[8:]
                current_region["ours_content"] = "\n".join(ours_lines)
                current_region["theirs_content"] = "\n".join(theirs_lines)
                regions.append(current_region)
                current_region = None
                section = None
            elif current_region is not None:
                if section == "ours":
                    ours_lines.append(line)
                else:
                    theirs_lines.append(line)

        return regions

    def git_mark_resolved(self, repo_path: Path, file_path: str) -> Dict[str, Any]:
        """Mark a conflicted file as resolved by staging it.

        Validates that conflict markers are removed before staging.

        Returns:
            Dict with success, file, remaining_conflicts, all_resolved, message

        Raises:
            GitCommandError: If git add fails
            ValueError: If file is not conflicted or still has markers
        """
        # Verify file is currently conflicted
        status_result = run_git_command(
            ["git", "status", "--porcelain", "--", file_path],
            cwd=repo_path,
            timeout=self._git_timeouts.git_local_timeout,
            check=True,
        )

        is_conflicted = False
        for line in status_result.stdout.split("\n"):
            if len(line) < 4:
                continue
            status_code = line[:2]
            if status_code in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                is_conflicted = True
                break

        if not is_conflicted:
            raise ValueError(f"File '{file_path}' is not in a conflicted state.")

        # Check that conflict markers are removed (for text files)
        full_path = self._safe_repo_file_path(repo_path, file_path)
        if full_path.exists():
            try:
                content = full_path.read_text(encoding="utf-8", errors="strict")
                if "<<<<<<< " in content:
                    raise ValueError(
                        "File still contains conflict markers. "
                        "Edit the file to resolve conflicts before marking as resolved."
                    )
            except UnicodeDecodeError:
                pass  # Binary file — no markers to check

        # Stage the resolved file
        run_git_command(
            ["git", "add", "--", file_path],
            cwd=repo_path,
            timeout=self._git_timeouts.git_local_timeout,
            check=True,
        )

        # Count remaining conflicts
        remaining_result = run_git_command(
            ["git", "status", "--porcelain"],
            cwd=repo_path,
            timeout=self._git_timeouts.git_local_timeout,
            check=True,
        )

        remaining_conflicts = 0
        for line in remaining_result.stdout.split("\n"):
            if len(line) < 4:
                continue
            status_code = line[:2]
            if status_code in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
                remaining_conflicts += 1

        all_resolved = remaining_conflicts == 0
        message = (
            "All conflicts resolved. Run git_commit to complete the merge."
            if all_resolved
            else f"{remaining_conflicts} conflict(s) remaining."
        )

        return {
            "success": True,
            "file": file_path,
            "remaining_conflicts": remaining_conflicts,
            "all_resolved": all_resolved,
            "message": message,
        }

    def git_checkout_file(self, repo_path: Path, file_path: str) -> Dict[str, Any]:
        """
        Restore a file to its HEAD state.

        Args:
            repo_path: Path to git repository
            file_path: File to restore

        Returns:
            Dict with success flag and restored_file path

        Raises:
            GitCommandError: If git checkout fails
        """
        try:
            run_git_command(
                ["git", "checkout", "HEAD", "--", file_path],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {"success": True, "restored_file": file_path}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git checkout file failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git checkout timed out after {e.timeout}s", stderr=""
            )

    # F6: Branch Management Operations

    def git_branch_list(self, repo_path: Path) -> Dict[str, Any]:
        """
        List all branches (local and remote).

        Args:
            repo_path: Path to git repository

        Returns:
            Dict with current branch, local branches, and remote branches

        Raises:
            GitCommandError: If git branch fails
        """
        try:
            result = run_git_command(
                ["git", "branch", "-a"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            current_branch = ""
            local_branches = []
            remote_branches = []

            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue

                if line.startswith("* "):
                    current_branch = line[2:].strip()
                    local_branches.append(current_branch)
                elif line.startswith("remotes/"):
                    remote_branch = line.replace("remotes/", "").strip()
                    remote_branches.append(remote_branch)
                else:
                    local_branches.append(line)

            return {
                "current": current_branch,
                "local": local_branches,
                "remote": remote_branches,
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git branch list failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git branch list timed out after {e.timeout}s", stderr=""
            )

    def git_branch_create(self, repo_path: Path, branch_name: str) -> Dict[str, Any]:
        """
        Create a new branch.

        Args:
            repo_path: Path to git repository
            branch_name: Name for new branch

        Returns:
            Dict with success flag and created_branch name

        Raises:
            GitCommandError: If git branch fails
        """
        try:
            run_git_command(
                ["git", "branch", branch_name],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {"success": True, "created_branch": branch_name}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git branch create failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git branch create timed out after {e.timeout}s", stderr=""
            )

    def git_branch_switch(self, repo_path: Path, branch_name: str) -> Dict[str, Any]:
        """
        Switch to a different branch.

        Args:
            repo_path: Path to git repository
            branch_name: Branch to switch to

        Returns:
            Dict with success, current_branch, and previous_branch

        Raises:
            GitCommandError: If git checkout/switch fails
        """
        try:
            # Get current branch first
            current_result = run_git_command(
                ["git", "branch", "--show-current"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )
            previous_branch = current_result.stdout.strip()

            # Switch branch
            run_git_command(
                ["git", "checkout", branch_name],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {
                "success": True,
                "current_branch": branch_name,
                "previous_branch": previous_branch,
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git branch switch failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git branch switch timed out after {e.timeout}s", stderr=""
            )

    def git_branch_delete(
        self,
        repo_path: Path,
        branch_name: str,
        confirmation_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Delete a branch.

        Args:
            repo_path: Path to git repository
            branch_name: Branch to delete
            confirmation_token: Required for this destructive operation

        Returns:
            Dict with success/deleted_branch OR requires_confirmation/token

        Raises:
            ValueError: If attempted without valid token
            GitCommandError: If git branch delete fails
        """
        if not confirmation_token:
            token = self._generate_confirmation_token("git_branch_delete")
            return {"requires_confirmation": True, "token": token}

        if not self._validate_confirmation_token(
            "git_branch_delete", confirmation_token
        ):
            raise ValueError("Invalid or expired confirmation token")

        try:
            run_git_command(
                ["git", "branch", "-d", branch_name],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {"success": True, "deleted_branch": branch_name}

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git branch delete failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git branch delete timed out after {e.timeout}s", stderr=""
            )

    # .code-indexer Protection System

    def _validate_no_code_indexer_files(self, file_paths: List[str]) -> List[str]:
        """
        Filter out and warn about .code-indexer files.

        Args:
            file_paths: List of file paths to validate

        Returns:
            Filtered list without .code-indexer files

        Raises:
            ValueError: If .code-indexer files are detected
        """
        forbidden_patterns = [".code-indexer/", ".code-indexer-override.yaml"]
        forbidden_files = [
            f for f in file_paths if any(pattern in f for pattern in forbidden_patterns)
        ]

        if forbidden_files:
            raise ValueError(
                f"Cannot stage .code-indexer files - these are local index files and should never be committed. "
                f"Blocked files: {forbidden_files}"
            )

        return file_paths

    # Confirmation Token System

    def _generate_confirmation_token(self, operation: str) -> str:
        """
        Generate a 6-character confirmation token (thread-safe).

        Args:
            operation: Operation name for token validation

        Returns:
            6-character alphanumeric token
        """
        # Generate 6-character token using uppercase letters and digits
        # Excluding ambiguous characters: 0, O, I, 1
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        token = "".join(secrets.choice(chars) for _ in range(6))

        # Thread-safe token storage (TTLCache handles expiration automatically)
        with self._tokens_lock:
            # Store only operation name - TTLCache handles expiry via its TTL parameter
            self._tokens[token] = operation

        return token

    def _validate_confirmation_token(self, operation: str, token: str) -> bool:
        """
        Validate a confirmation token (thread-safe, single-use).

        Args:
            operation: Expected operation name
            token: Token to validate

        Returns:
            True if token is valid and not expired, False otherwise
        """
        # Thread-safe token validation
        with self._tokens_lock:
            # TTLCache automatically removes expired entries on access
            if token not in self._tokens:
                return False

            stored_operation = self._tokens[token]

            # Check operation match
            if stored_operation != operation:
                return False

            # Token is valid - consume it (single-use)
            del self._tokens[token]
            return True

    # -------------------------------------------------------------------------
    # Story #453: Git Stash Operations
    # -------------------------------------------------------------------------

    def git_stash_push(
        self,
        repo_path: Path,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Stash current working directory changes.

        Runs 'git stash push [-m "message"]'.

        Args:
            repo_path: Path to git repository
            message: Optional stash message

        Returns:
            Dict with success, stash_ref ('stash@{0}'), and message

        Raises:
            GitCommandError: If git stash push fails
        """
        try:
            cmd = ["git", "stash", "push"]
            if message:
                cmd.extend(["-m", message])

            result = run_git_command(
                cmd,
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {
                "success": True,
                "stash_ref": "stash@{0}",
                "message": result.stdout.strip(),
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git stash push failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git stash push timed out after {e.timeout}s", stderr=""
            )

    def git_stash_pop(
        self,
        repo_path: Path,
        index: int = 0,
    ) -> Dict[str, Any]:
        """Apply and remove a stash entry.

        Runs 'git stash pop stash@{N}'.

        Args:
            repo_path: Path to git repository
            index: Stash index to pop (default 0)

        Returns:
            Dict with success and message

        Raises:
            GitCommandError: If git stash pop fails
        """
        try:
            result = run_git_command(
                ["git", "stash", "pop", f"stash@{{{index}}}"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {
                "success": True,
                "message": result.stdout.strip(),
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git stash pop failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git stash pop timed out after {e.timeout}s", stderr=""
            )

    def git_stash_apply(
        self,
        repo_path: Path,
        index: int = 0,
    ) -> Dict[str, Any]:
        """Apply a stash entry without removing it.

        Runs 'git stash apply stash@{N}'.

        Args:
            repo_path: Path to git repository
            index: Stash index to apply (default 0)

        Returns:
            Dict with success and message

        Raises:
            GitCommandError: If git stash apply fails
        """
        try:
            result = run_git_command(
                ["git", "stash", "apply", f"stash@{{{index}}}"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {
                "success": True,
                "message": result.stdout.strip(),
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git stash apply failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git stash apply timed out after {e.timeout}s", stderr=""
            )

    def git_stash_list(
        self,
        repo_path: Path,
    ) -> Dict[str, Any]:
        """List all stash entries.

        Runs 'git stash list --format=%gd|||%gs|||%ci' and parses output.

        Args:
            repo_path: Path to git repository

        Returns:
            Dict with success and stashes list. Each entry has:
            index (int), message (str), created_at (str)

        Raises:
            GitCommandError: If git stash list fails
        """
        try:
            result = run_git_command(
                ["git", "stash", "list", "--format=%gd|||%gs|||%ci"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            stashes = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|||")
                if len(parts) >= 2:
                    ref = parts[0].strip()  # e.g. stash@{0}
                    msg = parts[1].strip()  # e.g. WIP on main: abc123 message
                    created_at = parts[2].strip() if len(parts) >= 3 else ""
                    # Extract numeric index from stash@{N}
                    idx = 0
                    if ref.startswith("stash@{") and ref.endswith("}"):
                        try:
                            idx = int(ref[7:-1])
                        except ValueError:
                            pass
                    stashes.append(
                        {
                            "index": idx,
                            "message": msg,
                            "created_at": created_at,
                        }
                    )

            return {
                "success": True,
                "stashes": stashes,
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git stash list failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git stash list timed out after {e.timeout}s", stderr=""
            )

    def git_stash_drop(
        self,
        repo_path: Path,
        index: int = 0,
    ) -> Dict[str, Any]:
        """Remove a stash entry without applying it.

        Runs 'git stash drop stash@{N}'.

        Args:
            repo_path: Path to git repository
            index: Stash index to drop (default 0)

        Returns:
            Dict with success and message

        Raises:
            GitCommandError: If git stash drop fails
        """
        try:
            result = run_git_command(
                ["git", "stash", "drop", f"stash@{{{index}}}"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )

            return {
                "success": True,
                "message": result.stdout.strip(),
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git stash drop failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git stash drop timed out after {e.timeout}s", stderr=""
            )

    # -------------------------------------------------------------------------
    # Story #454: Git Amend Operation
    # -------------------------------------------------------------------------

    def git_amend(
        self,
        repo_path: Path,
        message: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Amend the most recent git commit.

        With message: runs 'git commit --amend -m "msg"'.
        Without message: runs 'git commit --amend --no-edit'.

        Args:
            repo_path: Path to git repository
            message: New commit message. If None, uses --no-edit to keep current message.
            env: Optional environment variables dict (e.g. GIT_AUTHOR_NAME/EMAIL,
                 GIT_COMMITTER_NAME/EMAIL). If None, inherits current environment.

        Returns:
            Dict with success, commit_hash (new HEAD hash), and message

        Raises:
            GitCommandError: If git commit --amend fails
        """
        try:
            cmd = ["git", "commit", "--amend"]
            if message is not None:
                cmd.extend(["-m", message])
            else:
                cmd.append("--no-edit")

            run_kwargs: Dict[str, Any] = {
                "cwd": repo_path,
                "timeout": self._git_timeouts.git_local_timeout,
                "check": True,
            }
            if env is not None:
                run_kwargs["env"] = env

            run_git_command(cmd, **run_kwargs)

            # Get the new commit hash via rev-parse HEAD
            hash_result = run_git_command(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                timeout=self._git_timeouts.git_local_timeout,
                check=True,
            )
            commit_hash = hash_result.stdout.strip()

            return {
                "success": True,
                "commit_hash": commit_hash,
                "message": f"Amended commit {commit_hash[:8]}",
            }

        except subprocess.CalledProcessError as e:
            raise GitCommandError(
                f"git commit --amend failed: {e}",
                stderr=getattr(e, "stderr", ""),
                returncode=e.returncode,
            )
        except subprocess.TimeoutExpired as e:
            raise GitCommandError(
                f"git commit --amend timed out after {e.timeout}s", stderr=""
            )


# Global service instance (lazy initialization to avoid circular imports)
_git_operations_service_instance = None


def _get_git_operations_service():
    """Get or create the global GitOperationsService instance."""
    global _git_operations_service_instance
    if _git_operations_service_instance is None:
        _git_operations_service_instance = GitOperationsService()
    return _git_operations_service_instance


# Global service instance for easy import
git_operations_service = _get_git_operations_service()
