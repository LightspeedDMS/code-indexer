"""
Generic query service that filters results based on current branch context.
"""

import os
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional, Sequence
import logging

from code_indexer.config import Config
from code_indexer.services.file_identifier import FileIdentifier
from code_indexer.utils.git_runner import run_git_command

logger = logging.getLogger(__name__)


class BranchContextUnavailableError(RuntimeError):
    """The git backend could not be reached for the branch filter: an error,
    never an unfiltered "unknown branch" result."""


_UNKNOWN_CONTEXT: Dict[str, Any] = {"branch": "unknown", "commit": "unknown"}


class GenericQueryService:
    """Service for performing branch-aware queries on indexed content."""

    def __init__(
        self,
        project_dir: Path,
        config: Optional[Config] = None,
        git_backend: Optional[Any] = None,
    ):
        """Initialize the query service.

        Args:
            project_dir: The project directory path
            config: Optional configuration object
            git_backend: the git seam (git-mcp ``git_exec``) for a repository
                this machine does not hold -- the server's per-member query of
                a composite on a pod whose member directory holds index state
                only. Every git question of the branch filter is then asked of
                it; None keeps local git.
        """
        self._git_backend = git_backend
        if git_backend is not None:
            # String arithmetic only: the directory is index state here, the
            # repository is at the same path with git-mcp.
            self.project_dir = Path(os.path.abspath(project_dir))
            self.config = config
            self.file_identifier = FileIdentifier(
                project_dir, config, git_backend=git_backend
            )
            return
        # Bug #1582 hardening (Codex dual-review follow-up): resolve to an
        # absolute path. `_is_result_current_branch()` passes
        # `self.project_dir` as the `project_root` argument to the shared
        # `_normalize_stored_path_for_visibility()` helper, which computes
        # `Path(absolute_stored_path).relative_to(project_root)` -- if
        # `project_root` itself were relative, that call raises
        # `ValueError` for an absolute stored path (an absolute path can
        # never be "under" a relative root), silently returning the path
        # unchanged and defeating the #1582 fix for that input shape.
        # Every current call site already passes an absolute
        # `config.codebase_dir` in practice, so this is a no-op there.
        self.project_dir = Path(project_dir).resolve()
        self.config = config
        self.file_identifier = FileIdentifier(project_dir, config)

    def filter_results_by_branch(
        self, search_results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Filter search results to only include files from the current branch.

        Args:
            search_results: List of search results from vector database

        Returns:
            Filtered list containing only current branch results
        """
        if not self.file_identifier.git_available:
            # No git, return all results
            return search_results

        current_branch_context = self._get_current_branch_context()
        filtered_results = []

        for result in search_results:
            if self._is_result_current_branch(result, current_branch_context):
                filtered_results.append(result)
        logger.info(
            f"Filtered {len(search_results)} to {len(filtered_results)} results for current branch"
        )
        return filtered_results

    def _get_current_branch_context(self) -> Dict[str, Any]:
        """Get current branch context for filtering.

        Returns:
            Dictionary with current branch information
        """
        if self._git_backend is not None:
            return self._branch_context_through_backend()
        try:
            # First check if this is actually a git repository using the proper git runner
            check_result = run_git_command(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.project_dir,
                check=False,
                capture_output=True,
                text=True,
            )

            # If not a git repository, return empty context silently
            if check_result.returncode != 0:
                return {"branch": "unknown", "commit": "unknown", "files": set()}

            # Get current branch using git runner
            result = run_git_command(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            current_branch = result.stdout.strip()

            # Get current commit hash using git runner
            result = run_git_command(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            current_commit = result.stdout.strip()

            # Get list of files in current branch using git runner
            result = run_git_command(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=self.project_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            current_files = (
                set(result.stdout.strip().split("\n"))
                if result.stdout.strip()
                else set()
            )

            return {
                "branch": current_branch,
                "commit": current_commit,
                "files": current_files,
            }

        except subprocess.CalledProcessError as e:
            # Git command failed - provide more helpful error message
            if "dubious ownership" in str(e.stderr):
                logger.warning(
                    f"Git repository ownership issue detected at {self.project_dir}. "
                    "This typically occurs when running as a different user than the repository owner. "
                    "Consider adding the directory to Git's safe directories with: "
                    f"git config --global --add safe.directory {self.project_dir}"
                )
            else:
                # Not a git repository or no commits yet - this is expected in test environments
                logger.debug(f"Git command failed: {e}")
            return {"branch": "unknown", "commit": "unknown", "files": set()}
        except Exception as e:
            # Only log unexpected errors
            logger.debug(f"Unexpected error getting branch context: {e}")
            return {"branch": "unknown", "commit": "unknown", "files": set()}

    def _backend_git(self, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        """``git <args>`` through the git backend: git's exit code returned as a
        local ``check=False`` run returns it. A failure to reach git at all
        raises :class:`BranchContextUnavailableError`."""
        run = getattr(self._git_backend, "run", None)
        if run is None:
            raise BranchContextUnavailableError(
                f"the git backend {type(self._git_backend).__name__} cannot run "
                "git (no git-mcp git_exec) for the branch filter"
            )
        try:
            result: "subprocess.CompletedProcess[str]" = run(
                str(self.project_dir), list(args)
            )
        except Exception as exc:
            raise BranchContextUnavailableError(
                f"git-mcp could not run git {args[0]} for the branch filter of "
                f"{self.project_dir}: {type(exc).__name__}"
            ) from exc
        return result

    def _branch_context_through_backend(self) -> Dict[str, Any]:
        """``_get_current_branch_context`` through the git backend. Not a
        repository, or no commit yet: the same "unknown" context as local."""
        if self._backend_git(["rev-parse", "--is-inside-work-tree"]).returncode != 0:
            return {**_UNKNOWN_CONTEXT, "files": set()}
        outputs = []
        for args in (
            ["rev-parse", "--abbrev-ref", "HEAD"],
            ["rev-parse", "HEAD"],
            ["ls-tree", "-r", "--name-only", "HEAD"],
        ):
            result = self._backend_git(args)
            if result.returncode != 0:
                return {**_UNKNOWN_CONTEXT, "files": set()}
            outputs.append(str(result.stdout).strip())
        branch, commit, listing = outputs
        return {
            "branch": branch,
            "commit": commit,
            "files": set(listing.split("\n")) if listing else set(),
        }

    def _is_result_current_branch(
        self, result: Dict[str, Any], branch_context: Dict[str, Any]
    ) -> bool:
        """Check if a search result belongs to the current branch.

        Args:
            result: Search result with metadata
            branch_context: Current branch context

        Returns:
            True if result is from current branch
        """
        try:
            # Bug #1582: function-local import mirrors the exact pattern
            # ``HighThroughputProcessor._normalize_stored_path`` already
            # uses to reuse this SAME shared helper (Bug #1575 Part A),
            # rather than a module-level cross-package import.
            from code_indexer.storage.hnsw_index_manager import (
                _normalize_stored_path_for_visibility,
            )

            # Extract metadata from result
            metadata = result.get("payload", {}) if "payload" in result else result

            # If no git metadata, include it (filesystem-based result)
            if not metadata.get("git_available", False):
                return True

            # Check if file exists in current branch.
            #
            # Bug #1582: ``branch_context["files"]`` is always repo-relative
            # (sourced from ``git ls-tree``), but a collection's stored
            # ``payload.path`` may be absolute -- a raw membership check
            # then never matches, silently dropping an otherwise-current
            # result. Reuse the SAME shared normalization helper Bug #1575
            # Part A introduced for the two sibling path-comparison sites
            # (``high_throughput_processor.py``'s branch-hide matching and
            # ``hnsw_index_manager.py``'s ``visible_files`` filter) rather
            # than writing a third, potentially-divergent reimplementation.
            file_path = metadata.get("path", "")
            normalized_file_path = _normalize_stored_path_for_visibility(
                file_path, self.project_dir
            )
            if normalized_file_path in branch_context["files"]:
                return True

            # For git-based results, also check if the commit is reachable
            result_commit = metadata.get("git_commit_hash", "")
            if result_commit and self._is_commit_reachable(result_commit):
                return True

            return False

        except BranchContextUnavailableError:
            raise
        except Exception as e:
            logger.warning(f"Error checking result branch compatibility: {e}")
            # Default to including the result if we can't determine
            return True

    def _is_commit_reachable(self, commit_hash: str) -> bool:
        """Check if a commit is reachable from current HEAD.

        Args:
            commit_hash: Git commit hash to check

        Returns:
            True if commit is reachable from current HEAD
        """
        if self._git_backend is not None:
            # Exit 0: an ancestor. A failure to reach git raises (loud).
            return (
                self._backend_git(
                    ["merge-base", "--is-ancestor", commit_hash, "HEAD"]
                ).returncode
                == 0
            )
        try:
            result = run_git_command(
                ["git", "merge-base", "--is-ancestor", commit_hash, "HEAD"],
                cwd=self.project_dir,
                capture_output=True,
                check=False,
            )

            # Return code 0 means commit is ancestor (reachable)
            return bool(result.returncode == 0)

        except Exception as e:
            logger.warning(f"Error checking commit reachability: {e}")
            return True  # Default to allowing if we can't check

    def enhance_query_metadata(self, query: str) -> Dict[str, Any]:
        """Enhance query with current branch metadata.

        Args:
            query: The search query

        Returns:
            Dictionary with enhanced query metadata
        """
        query_metadata = {
            "query": query,
            "project_id": self.file_identifier.get_project_id(),
            "git_available": self.file_identifier.git_available,
        }

        if self.file_identifier.git_available:
            branch_context = self._get_current_branch_context()
            query_metadata.update(
                {
                    "branch": branch_context["branch"],
                    "commit": branch_context["commit"],
                    "file_count": len(branch_context["files"]),
                }
            )

        return query_metadata

    def get_current_branch_context(self) -> Dict[str, Any]:
        """Get current branch context including git status.

        Returns:
            Dictionary with git status and branch information
        """
        branch_context = self._get_current_branch_context()

        # Add additional context that CLI expects
        result = {
            "git_available": self.file_identifier.git_available,
            "project_id": self.file_identifier.get_project_id(),
            "current_branch": branch_context.get("branch", "unknown"),
            "current_commit": branch_context.get("commit", "unknown"),
            "file_count": len(branch_context.get("files", set())),
        }

        return result

    def filter_results_by_current_branch(
        self, search_results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Filter search results to only include files from the current branch.

        This is an alias for filter_results_by_branch for CLI compatibility.

        Args:
            search_results: List of search results from vector database

        Returns:
            Filtered list containing only current branch results
        """
        return self.filter_results_by_branch(search_results)
