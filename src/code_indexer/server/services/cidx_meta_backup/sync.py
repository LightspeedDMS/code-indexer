"""Bidirectional git sync for mutable cidx-meta."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env

from .conflict_resolver import ClaudeConflictResolver

import logging as _logging

_MAX_MD_DELETE_RATIO = 0.5
_MIN_MD_DELETES_FOR_GATE = 3


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

    def sync(self) -> SyncResult:
        status = self._git("status", "--porcelain")
        local_committed = False
        if status.stdout.strip():
            # Safety gate: block commit if mass-deleting .md files.
            # Git porcelain v1 format is 'XY <path>' where X=index, Y=working-tree.
            # Check both status columns for 'D' to catch all deletion variants
            # (e.g. ' D' = unstaged delete, 'D ' = staged delete, 'DD' = both).
            status_lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
            deleted_md_count = sum(
                1
                for line in status_lines
                if len(line) >= 4
                and "D" in line[:2]
                and line[3:].strip().endswith(".md")
            )
            if deleted_md_count >= _MIN_MD_DELETES_FOR_GATE:
                tracked_result = self._git("ls-files", "*.md", check=False)
                # Runs BEFORE `git add -A`, so deletions are unstaged.
                # `git ls-files` includes deleted-but-tracked files in the count,
                # giving us the correct pre-deletion total.
                total_md = (
                    len(tracked_result.stdout.strip().splitlines())
                    if tracked_result.stdout.strip()
                    else 0
                )
                if total_md > 0 and deleted_md_count / total_md > _MAX_MD_DELETE_RATIO:
                    _logging.getLogger(__name__).error(
                        "CidxMetaBackupSync: BLOCKED commit — mass-delete of %d/%d .md files "
                        "(%.0f%% exceeds %.0f%% threshold)",
                        deleted_md_count,
                        total_md,
                        deleted_md_count / total_md * 100,
                        _MAX_MD_DELETE_RATIO * 100,
                    )
                    # Restore only the deleted .md files, not all unstaged changes.
                    # Using 'git checkout -- .' would revert ALL working-tree modifications
                    # (including legitimate changes to .gitignore, config files, etc.).
                    deleted_md_files = [
                        line[3:].strip()
                        for line in status_lines
                        if len(line) >= 4
                        and "D" in line[:2]
                        and line[3:].strip().endswith(".md")
                    ]
                    for md_file in deleted_md_files:
                        self._git("checkout", "--", md_file, check=False)
                    return SyncResult(
                        skipped=False,
                        sync_failure=(
                            f"mass-delete safety gate blocked commit: "
                            f"{deleted_md_count}/{total_md} .md files would be deleted"
                        ),
                    )

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

        rebase_result = self._git("rebase", f"origin/{self.branch}", check=False)
        if rebase_result.returncode != 0:
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
                    raise RuntimeError(
                        "conflict resolution failed: "
                        + self._stderr_or_stdout(continue_result)
                    )
            else:
                self._git("rebase", "--abort", check=False)
                raise RuntimeError(
                    "conflict resolution failed: "
                    + str(resolver_result.error or "unknown error")
                )

        push_result = self._git("push", "origin", self.branch, check=False)
        if push_result.returncode != 0:
            sync_failure = f"push failed: {self._stderr_or_stdout(push_result)}"

        return SyncResult(skipped=False, sync_failure=sync_failure)
