"""Bidirectional git sync for mutable cidx-meta."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from code_indexer.global_repos.repeated_failure_guard import RepeatedFailureGuard
from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env

from .conflict_resolver import ClaudeConflictResolver

# Bug #1539: production observed every scheduled refresh failing with
# "conflict resolution failed: ..." at a DIFFERENT rebase position each
# time -- a fresh attempt each cycle, never one stuck job -- holding
# /health degraded permanently with no forward progress. This guard
# detects when the failure "shape" repeats identically across consecutive
# attempts and stops retrying instead of doing so forever. RepeatedFailureGuard
# is internally lock-protected (see repeated_failure_guard.py), so sharing
# this single module-level instance across concurrent CidxMetaBackupSync
# instances is safe.
_CONFLICT_FAILURE_GUARD = RepeatedFailureGuard()

# Normalizes away varying numeric details (e.g. "Rebasing (3/7)" vs
# "Rebasing (9/12)") so the SAME underlying condition is recognized as the
# same failure shape regardless of which rebase step it stops at.
_DIGIT_RUN_PATTERN = re.compile(r"\d+")


def _conflict_failure_fingerprint(conflict_files: List[str], detail: str) -> str:
    normalized_detail = _DIGIT_RUN_PATTERN.sub("#", detail)
    return "|".join(sorted(conflict_files)) + "::" + normalized_detail


class StructurallyUnresolvableConflictError(RuntimeError):
    """Raised when the identical conflict-resolution failure shape repeats.

    Signals that retrying cannot fix this condition -- it needs manual/
    operator intervention (e.g. corrupt content in cidx-meta that the
    LLM-based resolver cannot repair, or a rebase that will never converge
    given the current state of both branches). Subclasses RuntimeError so
    any existing ``except RuntimeError`` handling upstream keeps working
    unchanged; only the message and type distinguish "stop retrying" from
    a single transient failure.
    """

    def __init__(self, message: str, *, occurrences: int, fingerprint: str) -> None:
        self.occurrences = occurrences
        self.fingerprint = fingerprint
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
        """Raise for a conflict-resolution failure, escalating on repeats.

        Bug #1539: the first N-1 occurrences of a given failure shape raise
        a plain RuntimeError (the pre-existing, retry-next-cycle behavior,
        preserved byte-for-byte). Once the SAME shape has now failed
        `threshold` times in a row, raise
        StructurallyUnresolvableConflictError instead -- a clear, distinct
        signal that retrying will never help and this needs manual
        intervention, instead of silently piling up identical failed jobs
        forever.
        """
        fingerprint = _conflict_failure_fingerprint(conflict_files, detail)
        occurrences = _CONFLICT_FAILURE_GUARD.record_failure(
            self.cidx_meta_path, fingerprint
        )
        message = "conflict resolution failed: " + detail
        if _CONFLICT_FAILURE_GUARD.is_exhausted(occurrences):
            raise StructurallyUnresolvableConflictError(
                f"{message} -- repeated identical failure {occurrences}x, "
                "stopping retries: this condition cannot be resolved by "
                "retrying and requires manual intervention on cidx-meta at "
                f"{self.cidx_meta_path}",
                occurrences=occurrences,
                fingerprint=fingerprint,
            )
        raise RuntimeError(message)

    def _rebase_onto_remote(self) -> None:
        """Rebase local work onto ``origin/{branch}``, resolving conflicts.

        Extracted from ``sync()`` to keep that method short. Raises
        RuntimeError (or, on repeated identical failures, Bug #1539's
        StructurallyUnresolvableConflictError) on any unrecoverable
        failure; returns normally when the rebase needed no action or
        completed (with or without conflicts) successfully.
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
            # Bug #1539: nothing to sync this cycle -- clear any stale
            # conflict-failure tally so a LATER, genuinely new problem
            # starts counting from zero rather than inheriting a stale
            # count from an unrelated earlier condition.
            _CONFLICT_FAILURE_GUARD.reset(self.cidx_meta_path)
            return SyncResult(skipped=True, sync_failure=None)

        self._rebase_onto_remote()

        # Bug #1539: reaching this point means either no rebase was needed,
        # or a rebase (with or without conflicts) completed successfully --
        # any previously tracked conflict-failure tally for this repo no
        # longer reflects reality and must be cleared.
        _CONFLICT_FAILURE_GUARD.reset(self.cidx_meta_path)

        push_result = self._git("push", "origin", self.branch, check=False)
        if push_result.returncode != 0:
            sync_failure = f"push failed: {self._stderr_or_stdout(push_result)}"

        return SyncResult(skipped=False, sync_failure=sync_failure)
