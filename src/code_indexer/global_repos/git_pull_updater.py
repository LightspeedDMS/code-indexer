"""
Git Pull Updater - update strategy for git-based repositories.

Implements UpdateStrategy interface using git pull for updates
and git diff-index for change detection.
"""

import logging
import os
import subprocess
from collections.abc import Sequence as _Sequence
from pathlib import Path
from typing import Any, Optional, Union

from .git_error_classifier import GitFetchError
from code_indexer.global_repos.orphaned_repo_error import OrphanedRepoError
from code_indexer.server.storage.shared.git_backend import (
    GitBackend,
    LocalGitBackend,
    head_branch,
)
from code_indexer.utils.subprocess_diagnostics import (
    DEFAULT_DIAGNOSTIC_MAX_CHARS,
    format_called_process_error_diagnostic as _format_called_process_diagnostic,
)
from .update_strategy import UpdateStrategy
from code_indexer.utils.subprocess_diagnostics import redact_url_credentials


logger = logging.getLogger(__name__)


def _stream_to_str(value: Optional[Union[str, bytes]]) -> str:
    """Bug #1832 follow-up: normalize a subprocess stream (str, bytes, or
    None) to a plain str WITHOUT length-capping -- used where the RAW value
    must be preserved (GitFetchError.stderr/.stdout), matching the
    non-timeout GitFetchError raise site's established uncapped convention.
    Distinct from the capped formatting `format_completed_process_diagnostic`
    (imported above) and `_format_timeout_diagnostic` (below) apply at
    DISPLAY/LOG time -- this is for the raw VALUE stored on the exception.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _format_timeout_diagnostic(e: subprocess.TimeoutExpired) -> str:
    """Bug #1830 AC2: build a diagnostic identifying WHICH command timed
    out, after how long, and any partial output captured before the
    process was killed -- preserving TimeoutExpired.cmd/.timeout/.stdout/
    .stderr instead of discarding them into one generic message.

    TimeoutExpired has no formatter in the shared
    code_indexer.utils.subprocess_diagnostics module (Bug #1832 AC3) -- that
    module covers the two non-timeout subprocess-failure shapes (a raised
    CalledProcessError, and a non-raising CompletedProcess inspected via
    `.returncode != 0`). A process killed by a timeout never completes at
    all -- it has no exit code -- which is a third, distinct shape that is
    Bug #1830's own concern, not #1832's, so this stays local. It reuses
    DEFAULT_DIAGNOSTIC_MAX_CHARS so both cap to the same length.
    """
    cmd = e.cmd
    cmd_display = redact_url_credentials(
        " ".join(str(part) for part in cmd)
        if isinstance(cmd, _Sequence) and not isinstance(cmd, (str, bytes))
        else str(cmd)
    )
    # Redact BEFORE truncating: capping first can sever a credentialed URL
    # before the `@` the pattern anchors on, leaving a live token prefix.
    stdout_text = redact_url_credentials(_stream_to_str(e.stdout))[
        :DEFAULT_DIAGNOSTIC_MAX_CHARS
    ]
    stderr_text = redact_url_credentials(_stream_to_str(e.stderr))[
        :DEFAULT_DIAGNOSTIC_MAX_CHARS
    ]
    return (
        f"command='{cmd_display}' timeout={e.timeout}s "
        f"stdout={stdout_text!r} stderr={stderr_text!r}"
    )


# Known cidx artifacts that may be created in a repo by `cidx init` and must
# never block `git pull` or `git reset --hard` (Bug #1013).
_CIDX_UNTRACKED_ARTIFACTS = frozenset(
    [
        ".code-indexer-override.yaml",
        "language-mappings.yaml",
    ]
)


def _delegated_content_writer() -> Optional[Any]:
    """The content writer that owns the working tree, or None when it is HERE.

    Under ``content_backend='delegated'`` git runs in git-mcp and the working
    tree lives with workspace-mcp; this pod's ``repo_path`` holds index state
    only, so an untracked file git names is removed THERE, through the content
    writer. None outside a delegated server (local backend, CLI, tests): the
    local disk is the working tree, exactly as before. A delegated server with
    no writer parked raises -- unlinking this pod's disk instead would remove
    nothing git can see (or worse, cidx's own state of the same name).
    """
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    if not is_content_seam_delegated():
        return None
    from code_indexer.server.startup.content_writer_wiring import (
        server_content_writer,
    )

    writer = server_content_writer()
    if writer is None:
        raise RuntimeError(
            "content_backend='delegated' but no content writer is configured: "
            "cannot remove untracked files from the working tree, which is not "
            "on this pod"
        )
    return writer


def _remove_untracked_file(
    repo_path: Path, filename: str, writer: Optional[Any]
) -> bool:
    """Remove one untracked *filename* from the working tree; True if removed.

    Confined to *repo_path*: a name escaping it is skipped with a warning. With
    a *writer* (delegated) the working tree is workspace-mcp's: confinement is
    lexical (this pod cannot resolve that tree's symlinks; the writer confines
    on its side) and the delete goes through the writer, whose failure raises.
    Without one, today's resolve() confinement and local unlink.
    """
    artifact = repo_path / filename
    if writer is not None:
        normalized = Path(os.path.normpath(artifact))
        if not normalized.is_relative_to(repo_path) or normalized == repo_path:
            logger.warning(f"Skipping suspicious path outside repo: {filename}")
            return False
        return bool(writer.delete(str(normalized)))
    if not artifact.resolve().is_relative_to(repo_path.resolve()):
        logger.warning(f"Skipping suspicious path outside repo: {filename}")
        return False
    if artifact.exists():
        artifact.unlink()
        return True
    return False


def _remove_conflicting_untracked(repo_path: Path, filenames: list[str]) -> None:
    """Remove the untracked files git says would be overwritten (Bug #1013)."""
    writer = _delegated_content_writer()
    for filename in filenames:
        if _remove_untracked_file(repo_path, filename, writer):
            logger.info(f"Removed conflicting untracked file: {repo_path / filename}")


def _remove_cidx_untracked_artifacts(repo_path: Path, status_lines: list[str]) -> None:
    """
    Remove known cidx artifacts that appear as untracked (??) in git status output.

    Only removes files whose names are in _CIDX_UNTRACKED_ARTIFACTS and that
    are reported as untracked by git status --porcelain.  Non-cidx untracked
    files are left untouched. Under a delegated content backend the status is
    git-mcp's and the removal goes through the content writer
    (``_delegated_content_writer``), never this pod's disk.

    Args:
        repo_path: Absolute path to the git repository root.
        status_lines: Lines from `git status --porcelain` stdout.
    """
    untracked_names = set()
    for line in status_lines:
        if line.startswith("?? "):
            filename = line[3:].strip()
            untracked_names.add(filename)

    present = [name for name in _CIDX_UNTRACKED_ARTIFACTS if name in untracked_names]
    if not present:
        return
    writer = _delegated_content_writer()
    for name in present:
        if _remove_untracked_file(repo_path, name, writer):
            logger.info(
                f"Removed cidx untracked artifact before git operation: "
                f"{repo_path / name}"
            )


def _working_tree_is_elsewhere() -> bool:
    """True on a delegated server: the clone is git-mcp's, not on this pod.

    There ``repo_path`` holds index state only, so its absence here says
    nothing about the repository; whether the clone exists is git-mcp's answer,
    and a missing one fails the fetch loudly (GitFetchError) instead of being
    skipped as an orphan.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    return is_content_seam_delegated()


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

    def __init__(self, repo_path: str, git_backend: Optional[GitBackend] = None):
        """
        Initialize git pull updater.

        Args:
            repo_path: Path to git repository
            git_backend: EVO-64678 git seam. Defaults to LocalGitBackend (git run
                locally via utils.git_runner), which reproduces today's behavior;
                server wiring can inject the configured backend instead.
        """
        self.repo_path = Path(repo_path)
        self._git_backend: GitBackend = (
            git_backend if git_backend is not None else LocalGitBackend()
        )

        if not _working_tree_is_elsewhere() and not self.repo_path.exists():
            # Bug #1338: this is the ORPHAN case (registry row present, clone
            # missing on disk) -- raise the typed OrphanedRepoError, not a
            # plain ValueError, so the refresh-scheduler skip site can catch
            # it by TYPE instead of matching this message's text.
            raise OrphanedRepoError(f"Repository path does not exist: {repo_path}")

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
            # First, fetch latest refs from remote.
            # EVO-64678: routed through the git seam (default LocalGitBackend ==
            # today's `git fetch origin`, with the same non-interactive SSH env).
            # The seam raises CalledProcessError on a non-zero exit, so it is
            # caught INLINE here to rebuild the identical classification +
            # GitFetchError shape the returncode check used to produce.
            try:
                self._git_backend.fetch(str(self.repo_path), timeout=30)

            except subprocess.TimeoutExpired as e:
                # Bug #1830 AC1/AC4: a fetch that TIMES OUT must be classified
                # exactly like a fetch that fails with a non-zero exit code --
                # via the SAME classify_fetch_error() Story #295 uses -- so it
                # participates in the repeated-transient-failure -> re-clone
                # escalation. Previously this fell into the generic
                # `except subprocess.TimeoutExpired` below and escaped
                # classification entirely.
                from .git_error_classifier import classify_fetch_error

                # Bug #1832 follow-up: GitFetchError.stderr/.stdout keep the
                # RAW, uncapped stream text (matching the non-timeout raise
                # site below) -- only the logged/raised MESSAGE is capped via
                # _format_timeout_diagnostic(). An earlier version of this
                # fix stored the CAPPED text on the exception itself, which
                # would have silently truncated data a downstream consumer
                # (refresh_scheduler.py) reads directly.
                stderr_raw = _stream_to_str(e.stderr)
                stdout_raw = _stream_to_str(e.stdout)
                diagnostic = _format_timeout_diagnostic(e)
                category = classify_fetch_error(stderr_raw)
                logger.warning(
                    f"Git fetch timed out for {self.repo_path} "
                    f"(category={category}): {diagnostic}"
                )
                raise GitFetchError(
                    f"Git fetch timed out for {self.repo_path}: {diagnostic}",
                    category=category,
                    stderr=stderr_raw,
                    stdout=stdout_raw,
                    # No exit code: the process was killed by the timeout,
                    # it never completed with a (non-)zero return code.
                    returncode=None,
                    cmd=e.cmd,
                )

            except subprocess.CalledProcessError as fetch_exc:
                # Story #295: Raise instead of silently returning False so the
                # refresh scheduler can classify the error and trigger re-clone
                # for corruption or after repeated transient failures.
                from .git_error_classifier import classify_fetch_error

                # Bug #1832: classification keeps the raw, uncapped stderr; only the
                # logged/raised MESSAGE is upgraded to the full diagnostic. EVO-64678:
                # sourced from the seam's CalledProcessError rather than a CompletedProcess.
                category = classify_fetch_error(fetch_exc.stderr)
                diagnostic = _format_called_process_diagnostic(fetch_exc)
                logger.warning(
                    f"Git fetch failed for {self.repo_path} "
                    f"(category={category}): {diagnostic}"
                )
                raise GitFetchError(
                    f"Git fetch failed for {self.repo_path}: {diagnostic}",
                    category=category,
                    stderr=redact_url_credentials(fetch_exc.stderr),
                    stdout=redact_url_credentials(fetch_exc.stdout),
                    returncode=fetch_exc.returncode,
                    cmd=getattr(fetch_exc, "cmd", None),
                )

            # Detect detached HEAD (e.g. repo pinned to a tag or specific commit).
            # A detached/pinned ref is immutable for refresh purposes -- skip the
            # upstream comparison entirely and return False gracefully.
            # Through the git seam: run on this pod, these two commands fail on
            # a pod that does not hold the repository, and the failure used to
            # read as "detached" -- refresh then never pulled. head_branch()
            # reports ONLY a real detached HEAD as None and raises otherwise.
            if head_branch(self._git_backend, str(self.repo_path), timeout=10) is None:
                logger.info(
                    f"Skipping remote-change check for {self.repo_path}: "
                    "HEAD is detached (pinned tag or commit). Nothing to pull."
                )
                return False

            # Commits on the remote not in local: HEAD..@{upstream}.
            try:
                behind = self._git_backend.rev_list_count(
                    str(self.repo_path), "HEAD..@{upstream}", timeout=30
                )
            except subprocess.CalledProcessError as e:
                # Bug #1832: name exit code + both streams, not stderr alone.
                raise RuntimeError(
                    f"Git upstream comparison (rev-list HEAD..@{{upstream}}) failed for {self.repo_path}: "
                    f"{_format_called_process_diagnostic(e)}"
                ) from e

            if behind:
                logger.info(
                    f"Remote changes detected for {self.repo_path}: "
                    f"{behind} commit(s) to pull"
                )

            return behind > 0

        except GitFetchError:
            raise
        except RuntimeError:
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
            # EVO-64678: via the git seam. rev_parse() emits the same
            # `git rev-parse --abbrev-ref HEAD`, already strips stdout, and
            # raises CalledProcessError on a non-zero exit — caught below so the
            # "(returncode=N)" warning keeps its exact shape.
            branch: str = self._git_backend.rev_parse(
                str(self.repo_path), "--abbrev-ref", "HEAD", timeout=30
            )
            if branch:
                return branch
            logger.warning(
                f"git rev-parse failed for {self.repo_path} "
                f"(returncode=0), falling back to 'main'"
            )
        except subprocess.CalledProcessError as e:
            logger.warning(
                f"git rev-parse failed for {self.repo_path} "
                f"(returncode={e.returncode}), falling back to 'main'"
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
        # EVO-64678: fetch + reset routed through the git seam. Both raise
        # CalledProcessError on a non-zero exit, so each is caught INLINE to
        # rebuild the identical RuntimeError message the returncode checks
        # produced. The seam methods return None, so the git stdout that used to
        # be appended to the success INFO logs is no longer available.
        try:
            self._git_backend.fetch(str(self.repo_path), timeout=30)
        except subprocess.CalledProcessError as fetch_exc:
            raise RuntimeError(
                f"Git fetch failed for {self.repo_path} during reset to "
                f"origin/{branch}: {_format_called_process_diagnostic(fetch_exc)}"
            )

        try:
            self._git_backend.reset(
                str(self.repo_path), "hard", f"origin/{branch}", timeout=30
            )
        except subprocess.CalledProcessError as reset_exc:
            reset_stderr = reset_exc.stderr
            # Bug #1013: Untracked files would be overwritten — remove and retry once
            if "untracked working tree files would be overwritten" in reset_stderr:
                conflicting = _parse_untracked_overwrite_filenames(reset_stderr)
                logger.warning(
                    f"Git reset blocked by untracked files in {self.repo_path}: "
                    f"{conflicting}. Removing and retrying."
                )
                _remove_conflicting_untracked(self.repo_path, conflicting)
                try:
                    self._git_backend.reset(
                        str(self.repo_path), "hard", f"origin/{branch}", timeout=30
                    )
                except subprocess.CalledProcessError as retry_exc:
                    raise RuntimeError(
                        f"Git reset --hard origin/{branch} failed after untracked-file "
                        f"cleanup for {self.repo_path}: {_format_called_process_diagnostic(retry_exc)}"
                    )
                logger.info(
                    f"Successfully reset {self.repo_path} to origin/{branch} after cleanup"
                )
                return
            # Bug #1832: name exit code + both streams, not stderr alone.
            raise RuntimeError(
                f"Git reset --hard origin/{branch} failed for {self.repo_path}: "
                f"{_format_called_process_diagnostic(reset_exc)}"
            )

        logger.info(f"Successfully reset {self.repo_path} to origin/{branch}")

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
            # Story #726: Defense in depth - check for local modifications.
            # EVO-64678: via the git seam. status_porcelain() returns raw stdout
            # and raises CalledProcessError on a non-zero exit; the raw call used
            # `check=False` and simply skipped the modified-file handling when
            # git failed, so the inline catch reproduces that tolerance exactly.
            try:
                status_stdout = self._git_backend.status_porcelain(
                    str(self.repo_path), timeout=10
                )
            except subprocess.CalledProcessError:
                status_stdout = ""

            modified_files = []
            if status_stdout.strip():
                # Filter out untracked files (??) - only warn/reset for tracked modifications
                all_files = status_stdout.strip().splitlines()
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

                # Reset local modifications to allow clean pull.
                # EVO-64678: via the git seam. reset() raises
                # CalledProcessError on a non-zero exit; caught inline so the
                # failure stays NON-fatal ("Proceeding with pull anyway"),
                # exactly as the returncode check did.
                try:
                    self._git_backend.reset(
                        str(self.repo_path), "hard", "HEAD", timeout=30
                    )
                except subprocess.CalledProcessError as reset_exc:
                    logger.warning(
                        f"Git reset failed for {self.repo_path}: {_format_called_process_diagnostic(reset_exc)}. "
                        "Proceeding with pull anyway."
                    )
                else:
                    logger.info("Git reset successful")

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

            # EVO-64678: routed through the git seam (default LocalGitBackend ==
            # today's plain `git pull` with the non-interactive SSH env). The
            # seam raises CalledProcessError on a non-zero exit, so the whole
            # former `if result.returncode != 0:` body moves into the inline
            # handler below with its stderr-driven branches unchanged. pull()
            # returns None, so git's stdout is no longer appended to the success
            # INFO logs.
            try:
                self._git_backend.pull(str(self.repo_path), timeout=120)
            except subprocess.CalledProcessError as pull_exc:
                stderr = pull_exc.stderr
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
                    _remove_conflicting_untracked(self.repo_path, conflicting)
                    try:
                        self._git_backend.pull(str(self.repo_path), timeout=120)
                    except subprocess.CalledProcessError as retry_exc:
                        raise RuntimeError(
                            f"Git pull failed after untracked-file cleanup for "
                            f"{self.repo_path}: {redact_url_credentials(retry_exc.stderr)}"
                        )
                    logger.info(
                        "Git pull retry successful after removing untracked files"
                    )
                    return

                # AC2: Non-divergence errors are not intercepted
                # Bug #1832: name exit code + both streams, not stderr alone.
                raise RuntimeError(
                    f"Git pull failed for {self.repo_path}: "
                    f"{_format_called_process_diagnostic(pull_exc)}"
                )

            logger.info("Git pull successful")

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
