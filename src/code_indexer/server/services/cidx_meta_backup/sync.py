"""Bidirectional git sync for mutable cidx-meta."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env

from .conflict_resolver import ClaudeConflictResolver

# Bug #1539: git's own rebase-progress banner ("Rebasing (2/18)") grows on
# every failed attempt, because a failed conflict resolution aborts the
# rebase but leaves the local unpushed auto-commit in place -- the NEXT
# attempt has one more commit to replay, so the position (and total)
# climbs every cycle even though the underlying conflict is identical.
# Only THIS marker is normalized; every other digit in the failure text
# (SHAs, line numbers) is left untouched so genuinely different failures
# are never folded into the same fingerprint.
_REBASE_POSITION_PATTERN = re.compile(r"\(\d+/\d+\)")


def conflict_failure_fingerprint(conflict_files: List[str], detail: str) -> str:
    """Normalize a conflict-resolution failure into a stable "shape" key.

    Used by callers (e.g. RefreshScheduler's Bug #1539 quarantine
    bookkeeping) to recognize when the SAME underlying conflict has failed
    repeatedly across separate, independent ``sync()`` calls -- this
    module itself makes no retry/quarantine decisions; it only exposes a
    fingerprint callers can persist and compare.
    """
    normalized_detail = _REBASE_POSITION_PATTERN.sub("(#/#)", detail)
    return "|".join(sorted(conflict_files)) + "::" + normalized_detail


class ConflictResolutionFailedError(RuntimeError):
    """Raised when a rebase conflict could not be resolved.

    Carries the conflicted file list and raw failure detail so a caller
    can compute ``conflict_failure_fingerprint()`` and track repeated
    occurrences in durable, cross-process storage (Bug #1539) -- this
    class intentionally carries data only; it does not decide retry vs.
    quarantine policy itself. Subclasses RuntimeError so any existing
    ``except RuntimeError`` handling upstream keeps working unchanged.
    """

    def __init__(self, message: str, *, conflict_files: List[str], detail: str) -> None:
        self.conflict_files = conflict_files
        self.detail = detail
        super().__init__(message)


@dataclass
class SyncResult:
    skipped: bool
    sync_failure: Optional[str]


class CidxMetaBackupSync:
    """Sync local mutable cidx-meta writes with a remote git repository."""

    def __init__(
        self,
        cidx_meta_path: str,
        branch: str,
        claude_resolver: Optional[ClaudeConflictResolver],
    ) -> None:
        self.cidx_meta_path = cidx_meta_path
        self.branch = branch
        self.claude_resolver = claude_resolver or ClaudeConflictResolver()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        env = build_non_interactive_git_env()
        env.setdefault("GIT_AUTHOR_NAME", "cidx-meta-backup")
        env.setdefault("GIT_AUTHOR_EMAIL", "cidx-meta-backup@example.invalid")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        # Bug #1500: `git rebase --continue` after a conflict opens an
        # editor to confirm the reapplied commit message (the conflict
        # appends a "# Conflicts:" comment, forcing msg_needs_editing) even
        # in a non-interactive rebase. The systemd job context has no tty
        # and no EDITOR/VISUAL configured, so git aborts with "Terminal is
        # dumb, but EDITOR unset". `true` exits 0 without touching the
        # message file, so git reuses the original commit message
        # non-interactively. setdefault so an operator-configured
        # GIT_EDITOR is never overridden.
        env.setdefault("GIT_EDITOR", "true")
        return subprocess.run(
            ["git", *args],
            cwd=self.cidx_meta_path,
            capture_output=True,
            text=True,
            env=env,
            check=check,
        )

    @staticmethod
    def _stderr_or_stdout(result: subprocess.CompletedProcess) -> str:
        return (result.stderr or result.stdout or "").strip()

    def _rebase_in_progress(self) -> bool:
        """Return True when git has stopped mid-rebase with state on disk.

        A non-zero rebase exit code does NOT always mean a conflict stopped it.
        git may fail before creating any state (dirty working tree, invalid
        upstream, pre-rebase hook, lock contention, etc.).  We only enter the
        conflict-resolution path when git has actually written its rebase-state
        directory, meaning there is a rebase to --continue or --abort.
        """
        git_dir = Path(self.cidx_meta_path) / ".git"
        return (git_dir / "rebase-merge").is_dir() or (
            git_dir / "rebase-apply"
        ).is_dir()

    def _raise_conflict_resolution_failure(
        self, conflict_files: List[str], detail: str
    ) -> None:
        """Raise ConflictResolutionFailedError carrying the failure data.

        Bug #1539: this method used to make an in-process retry/escalate
        decision itself; it no longer does. It only raises a typed
        exception so the caller (RefreshScheduler) can persist a
        cross-process, cross-node consecutive-failure count via
        golden_repo_metadata (the SAME dual-backend store Bug #1506 uses)
        and decide, on a LATER cycle, whether to skip retrying.
        """
        raise ConflictResolutionFailedError(
            "conflict resolution failed: " + detail,
            conflict_files=conflict_files,
            detail=detail,
        )

    def _rebase_onto_remote(self) -> None:
        """Rebase local work onto ``origin/{branch}``, resolving conflicts.

        Extracted from ``sync()`` to keep that method short. Raises
        RuntimeError (or ConflictResolutionFailedError on a conflict that
        could not be resolved) on any unrecoverable failure; returns
        normally when the rebase needed no action or completed (with or
        without conflicts) successfully.
        """
        rebase_result = self._git("rebase", f"origin/{self.branch}", check=False)
        if rebase_result.returncode == 0:
            return

        if not self._rebase_in_progress():
            # Rebase failed before creating any state (pre-rebase hook, dirty
            # working tree, invalid upstream, lock contention, etc.).  There is
            # nothing to --continue or --abort; surface the original failure.
            raise RuntimeError(
                "rebase failed: " + self._stderr_or_stdout(rebase_result)
            )

        conflict_files = self._git(
            "diff", "--name-only", "--diff-filter=U", check=False
        ).stdout.splitlines()
        resolver_result = self.claude_resolver.resolve(
            self.cidx_meta_path, conflict_files, self.branch
        )
        remaining_conflicts = self._git(
            "diff", "--name-only", "--diff-filter=U", check=False
        ).stdout.strip()
        if resolver_result.success and not remaining_conflicts:
            continue_result = self._git("rebase", "--continue", check=False)
            if continue_result.returncode != 0:
                self._git("rebase", "--abort", check=False)
                self._raise_conflict_resolution_failure(
                    conflict_files, self._stderr_or_stdout(continue_result)
                )
            return

        self._git("rebase", "--abort", check=False)
        self._raise_conflict_resolution_failure(
            conflict_files,
            str(resolver_result.error or "unknown error"),
        )

    def sync(self) -> SyncResult:
        status = self._git("status", "--porcelain")
        local_committed = False
        if status.stdout.strip():
            self._git("add", "-A")
            timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            self._git("commit", "-m", f"auto: cidx-meta refresh @ {timestamp}")
            local_committed = True

        sync_failure: Optional[str] = None
        fetch_result = self._git("fetch", "origin", check=False)
        if fetch_result.returncode != 0:
            sync_failure = f"fetch failed: {self._stderr_or_stdout(fetch_result)}"
            return SyncResult(skipped=False, sync_failure=sync_failure)

        head = self._git("rev-parse", "HEAD")
        remote_head = self._git("rev-parse", f"origin/{self.branch}")
        remote_changed = head.stdout.strip() != remote_head.stdout.strip()

        if not local_committed and not remote_changed:
            return SyncResult(skipped=True, sync_failure=None)

        self._rebase_onto_remote()

        push_result = self._git("push", "origin", self.branch, check=False)
        if push_result.returncode != 0:
            sync_failure = f"push failed: {self._stderr_or_stdout(push_result)}"

        return SyncResult(skipped=False, sync_failure=sync_failure)
