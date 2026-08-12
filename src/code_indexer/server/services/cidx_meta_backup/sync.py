"""Git backup mirror for mutable cidx-meta.

Bug #1555 (root-cause fix): the remote is a passive BACKUP MIRROR of
cidx-meta-global's local state -- never a peer whose independent history
must be preserved. This is an explicit product decision: the git remote
connection exists purely to exercise git-remote-backup capability for the
staging environment; there is nothing on the remote worth preserving, and
local cidx-meta content is always authoritative.

The previous design (Story #926 / Bug #1539) rebased local commits onto
``origin/{branch}`` before pushing, which treated the remote as a peer.
Since cidx-meta content is machine-generated (descriptions, dep-map YAML),
a remote commit touching the SAME generated file as a local regenerated
commit produced a genuine, structurally UNRESOLVABLE content conflict --
the automatic Claude conflict resolver failed identically on every retry
against that exact commit, and Bug #1539's circuit-breaker (correctly)
quarantined the sync indefinitely. There was no path to self-resolution:
the same conflict recurred on every attempt against an unchanged upstream
target.

``sync()`` no longer rebases at all. It commits local changes, then
publishes local HEAD directly with ``git push --force-with-lease``,
overwriting whatever the remote holds. A diverged remote is therefore
never a stuck state: it self-heals on the very next scheduled sync cycle
with no operator action, because there is no conflict class left to get
stuck on.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env


@dataclass
class SyncResult:
    skipped: bool
    sync_failure: Optional[str]


class CidxMetaBackupSync:
    """Mirror local mutable cidx-meta writes to a remote git repository.

    Local is the sole source of truth (Bug #1555): this pushes TO the
    remote, it never merges FROM it. See the module docstring for the
    full design rationale.
    """

    def __init__(self, cidx_meta_path: str, branch: str) -> None:
        self.cidx_meta_path = cidx_meta_path
        self.branch = branch

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

        # Bug #1555: local is authoritative -- publish it directly rather
        # than rebasing onto (and thereby preserving) the remote's
        # history. --force-with-lease, not --force: the fetch immediately
        # above refreshed this process's origin/{branch} tracking ref, so
        # the lease reflects genuinely current remote state, making this
        # push race-safe against a concurrent writer without
        # reintroducing any merge/rebase step. A lease mismatch (another
        # writer pushed between our fetch and this push) simply defers to
        # the next scheduled cycle, whose fresh fetch updates the lease
        # and self-heals automatically -- consistent with this module's
        # "never gets stuck" guarantee.
        push_result = self._git(
            "push", "--force-with-lease", "origin", self.branch, check=False
        )
        if push_result.returncode != 0:
            sync_failure = f"push failed: {self._stderr_or_stdout(push_result)}"

        return SyncResult(skipped=False, sync_failure=sync_failure)
