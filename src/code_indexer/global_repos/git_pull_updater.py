"""
Git Pull Updater - update strategy for git-based repositories.

Implements UpdateStrategy interface using git pull for updates
and git diff-index for change detection.
"""

import logging
import subprocess
from pathlib import Path

from .git_error_classifier import GitFetchError
from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env
from .update_strategy import UpdateStrategy


logger = logging.getLogger(__name__)

# Known cidx artifacts that may be created in a repo by `cidx init` and must
# never block `git pull` or `git reset --hard` (Bug #1013).
_CIDX_UNTRACKED_ARTIFACTS = frozenset(
    [
        ".code-indexer-override.yaml",
        "language-mappings.yaml",
    ]
)


def _remove_cidx_untracked_artifacts(repo_path: Path, status_lines: list[str]) -> None:
    """
    Remove known cidx artifacts that appear as untracked (??) in git status output.

    Only removes files whose names are in _CIDX_UNTRACKED_ARTIFACTS and that
    are reported as untracked by git status --porcelain.  Non-cidx untracked
    files are left untouched.

    Args:
        repo_path: Absolute path to the git repository root.
        status_lines: Lines from `git status --porcelain` stdout.
    """
    untracked_names = set()
    for line in status_lines:
        if line.startswith("?? "):
            filename = line[3:].strip()
            untracked_names.add(filename)

    for name in _CIDX_UNTRACKED_ARTIFACTS:
        if name in untracked_names:
            artifact = repo_path / name
            if artifact.exists():
                artifact.unlink()
                logger.info(
                    f"Removed cidx untracked artifact before git operation: {artifact}"
                )


def _parse_untracked_overwrite_filenames(stderr: str) -> list[str]:
    """
    Parse filenames from a git error of the form:
        error: The following untracked working tree files would be overwritten ...
            file1.yaml
            file2.yaml
        Please move or remove them ...

    Returns list of stripped filenames found between the error header and the
    'Please move or remove' line.
    """
    filenames = []
    in_list = False
    for line in stderr.splitlines():
        if "untracked working tree files would be overwritten" in line:
            in_list = True
            continue
        if in_list:
            stripped = line.strip()
            if stripped.startswith("Please") or stripped.startswith("Aborting"):
                break
            if stripped:
                filenames.append(stripped)
    return filenames


class GitPullUpdater(UpdateStrategy):
    """
    Update strategy for git repositories using git pull.

    Uses git diff-index for change detection and git pull for updates.
    """

    def __init__(self, repo_path: str):
        """
        Initialize git pull updater.

        Args:
            repo_path: Path to git repository
        """
        self.repo_path = Path(repo_path)

        if not self.repo_path.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")

    def has_changes(self) -> bool:
        """
        Check if repository has remote changes using git fetch and log.

        Fetches latest refs from remote and checks if there are commits
        on the remote branch that are not in the local branch.

        Returns:
            True if remote changes detected, False if up-to-date

        Raises:
            RuntimeError: If git command fails
        """
        try:
            # First, fetch latest refs from remote
            fetch_result = subprocess.run(
                ["git", "fetch", "origin"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
                env=build_non_interactive_git_env(),
            )

            if fetch_result.returncode != 0:
                # Story #295: Raise instead of silently returning False so the
                # refresh scheduler can classify the error and trigger re-clone
                # for corruption or after repeated transient failures.
                from .git_error_classifier import classify_fetch_error

                category = classify_fetch_error(fetch_result.stderr)
                logger.warning(
                    f"Git fetch failed for {self.repo_path} "
                    f"(category={category}): {fetch_result.stderr}"
                )
                raise GitFetchError(
                    f"Git fetch failed for {self.repo_path}",
                    category=category,
                    stderr=fetch_result.stderr,
                )

            # Check for commits on remote not in local using HEAD..@{upstream}
            log_result = subprocess.run(
                ["git", "log", "HEAD..@{upstream}", "--oneline"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )

            if log_result.returncode != 0:
                raise RuntimeError(
                    f"Git log command failed for {self.repo_path}: {log_result.stderr}"
                )

            # If there's any output, there are remote commits to pull
            has_remote_changes = bool(log_result.stdout.strip())

            if has_remote_changes:
                logger.info(
                    f"Remote changes detected for {self.repo_path}: "
                    f"{len(log_result.stdout.strip().splitlines())} commit(s) to pull"
                )

            return has_remote_changes

        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Git command timed out for {self.repo_path}")
        except GitFetchError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to check for remote changes: {e}")

    def _detect_branch(self) -> str:
        """
        Detect the current branch name via git rev-parse --abbrev-ref HEAD.

        Falls back to "main" when the command fails or times out (AC6).

        Returns:
            Current branch name, or "main" as fallback
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            logger.warning(
                f"git rev-parse failed for {self.repo_path} "
                f"(returncode={result.returncode}), falling back to 'main'"
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                f"git rev-parse timed out for {self.repo_path}, falling back to 'main'"
            )
        except Exception as e:
            logger.warning(
                f"git rev-parse raised {type(e).__name__} for {self.repo_path}, "
                "falling back to 'main'"
            )
        return "main"

    def _fetch_and_reset(self, branch: str) -> None:
        """
        Run git fetch origin followed by git reset --hard origin/{branch}.

        Used by both auto-recovery (AC1) and force_reset path (AC3).

        Args:
            branch: Branch name to reset to (e.g. "main", "master")

        Raises:
            RuntimeError: If fetch or reset fails
        """
        fetch_result = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            timeout=30,
            env=build_non_interactive_git_env(),
        )
        if fetch_result.returncode != 0:
            raise RuntimeError(
                f"Git fetch failed for {self.repo_path} during reset to "
                f"origin/{branch}: {fetch_result.stderr}"
            )

        reset_result = subprocess.run(
            ["git", "reset", "--hard", f"origin/{branch}"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if reset_result.returncode != 0:
            reset_stderr = reset_result.stderr
            # Bug #1013: Untracked files would be overwritten — remove and retry once
            if "untracked working tree files would be overwritten" in reset_stderr:
                conflicting = _parse_untracked_overwrite_filenames(reset_stderr)
                logger.warning(
                    f"Git reset blocked by untracked files in {self.repo_path}: "
                    f"{conflicting}. Removing and retrying."
                )
                for filename in conflicting:
                    artifact = self.repo_path / filename
                    if not artifact.resolve().is_relative_to(self.repo_path.resolve()):
                        logger.warning(
                            f"Skipping suspicious path outside repo: {filename}"
                        )
                        continue
                    if artifact.exists():
                        artifact.unlink()
                        logger.info(f"Removed conflicting untracked file: {artifact}")
                retry_reset = subprocess.run(
                    ["git", "reset", "--hard", f"origin/{branch}"],
                    cwd=str(self.repo_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if retry_reset.returncode != 0:
                    raise RuntimeError(
                        f"Git reset --hard origin/{branch} failed after untracked-file "
                        f"cleanup for {self.repo_path}: {retry_reset.stderr}"
                    )
                logger.info(
                    f"Successfully reset {self.repo_path} to origin/{branch} after cleanup: "
                    f"{retry_reset.stdout.strip()}"
                )
                return
            raise RuntimeError(
                f"Git reset --hard origin/{branch} failed for {self.repo_path}: "
                f"{reset_stderr}"
            )

        logger.info(
            f"Successfully reset {self.repo_path} to origin/{branch}: "
            f"{reset_result.stdout.strip()}"
        )

    def update(self, force_reset: bool = False) -> None:
        """
        Update repository using git pull (or force reset if requested).

        Story #726 Defense in Depth:
        Before pulling, checks for local modifications and resets them if found.
        This handles cases where previous CIDX versions modified .gitignore or
        where external processes modified tracked files.

        Story #272 Divergent Branch Auto-Recovery:
        When git pull fails with divergent branch errors, automatically recovers
        by running git fetch + git reset --hard origin/{branch}.

        Story #272 Force Reset:
        When force_reset=True, skips git pull entirely and runs
        git fetch + git reset --hard origin/{branch} unconditionally.

        Args:
            force_reset: When True, skip git pull and force-reset to remote branch.
                         Used by manual "Force Re-sync" UI action (AC3/AC4).

        Raises:
            RuntimeError: If git operation fails
        """
        try:
            # Story #726: Defense in depth - check for local modifications
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )

            modified_files = []
            if status_result.returncode == 0 and status_result.stdout.strip():
                # Filter out untracked files (??) - only warn/reset for tracked modifications
                all_files = status_result.stdout.strip().splitlines()
                modified_files = [f for f in all_files if not f.startswith("??")]
                # Bug #1013: Remove known cidx untracked artifacts before pull
                _remove_cidx_untracked_artifacts(self.repo_path, all_files)

            if modified_files:
                # Local modifications detected - log warning and reset
                logger.warning(
                    f"Local modifications detected in {self.repo_path}, "
                    f"resetting to HEAD before pull. "
                    f"Modified files: {' '.join(modified_files)}"
                )

                # Reset local modifications to allow clean pull
                reset_result = subprocess.run(
                    ["git", "reset", "--hard", "HEAD"],
                    cwd=str(self.repo_path),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if reset_result.returncode != 0:
                    logger.warning(
                        f"Git reset failed for {self.repo_path}: {reset_result.stderr}. "
                        "Proceeding with pull anyway."
                    )
                else:
                    logger.info(f"Git reset successful: {reset_result.stdout.strip()}")

            if force_reset:
                # AC3/AC4: Force reset path — skip git pull, go straight to
                # fetch + reset --hard origin/{branch}
                branch = self._detect_branch()
                logger.info(
                    f"Force reset requested for {self.repo_path}, "
                    f"resetting to origin/{branch}"
                )
                self._fetch_and_reset(branch)
                return

            logger.info(f"Executing git pull for {self.repo_path}")

            result = subprocess.run(
                ["git", "pull"],
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=120,
                env=build_non_interactive_git_env(),
            )

            if result.returncode != 0:
                stderr = result.stderr
                # AC1: Divergent branch detection — intercept and auto-recover
                if (
                    "divergent branches" in stderr
                    or "Need to specify how to reconcile" in stderr
                ):
                    logger.warning(
                        f"Divergent branch detected for {self.repo_path}, "
                        "attempting auto-recovery via fetch + reset --hard"
                    )
                    branch = self._detect_branch()
                    self._fetch_and_reset(branch)
                    logger.info(
                        f"Auto-recovery successful for {self.repo_path} on branch '{branch}'"
                    )
                    return

                # Bug #1013: Untracked files would be overwritten — remove and retry once
                if "untracked working tree files would be overwritten" in stderr:
                    conflicting = _parse_untracked_overwrite_filenames(stderr)
                    logger.warning(
                        f"Git pull blocked by untracked files in {self.repo_path}: "
                        f"{conflicting}. Removing and retrying."
                    )
                    for filename in conflicting:
                        artifact = self.repo_path / filename
                        if not artifact.resolve().is_relative_to(
                            self.repo_path.resolve()
                        ):
                            logger.warning(
                                f"Skipping suspicious path outside repo: {filename}"
                            )
                            continue
                        if artifact.exists():
                            artifact.unlink()
                            logger.info(
                                f"Removed conflicting untracked file: {artifact}"
                            )
                    retry = subprocess.run(
                        ["git", "pull"],
                        cwd=str(self.repo_path),
                        capture_output=True,
                        text=True,
                        timeout=120,
                        env=build_non_interactive_git_env(),
                    )
                    if retry.returncode == 0:
                        logger.info(
                            f"Git pull retry successful after removing untracked files: "
                            f"{retry.stdout.strip()}"
                        )
                        return
                    raise RuntimeError(
                        f"Git pull failed after untracked-file cleanup for "
                        f"{self.repo_path}: {retry.stderr}"
                    )

                # AC2: Non-divergence errors are not intercepted
                raise RuntimeError(f"Git pull failed for {self.repo_path}: {stderr}")

            logger.info(f"Git pull successful: {result.stdout.strip()}")

        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Git pull timed out for {self.repo_path}")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Git pull operation failed: {e}")

    def get_source_path(self) -> str:
        """
        Get the source repository path.

        Returns:
            Absolute path to git repository
        """
        return str(self.repo_path)
