"""Bidirectional git sync for mutable cidx-meta."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env

from .conflict_resolver import ClaudeConflictResolver


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
