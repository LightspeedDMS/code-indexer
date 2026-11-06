"""Bootstrap cidx-meta git backup state."""

from __future__ import annotations
from code_indexer.utils.git_runner import run_git_command

import subprocess
from pathlib import Path
from typing import Callable, Optional

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env
from code_indexer.server.services.cidx_meta_content import (
    refuse_backup_of_shared_content,
)

from .branch_detect import detect_default_branch
from .paths import cidx_meta_is_git_repository


class BackupPushFailed(RuntimeError):
    """The bootstrap push failed; *reason* is neutral (no URL, no raw git
    output) and safe to show an operator."""

    def __init__(self, reason: str, output: str) -> None:
        from code_indexer.utils.credential_redaction import redact_command_output

        self.reason = reason
        super().__init__(
            f"backup push failed ({reason}): {redact_command_output(output, None)}"
        )


class CidxMetaBackupBootstrap:
    """Bootstrap a mutable cidx-meta directory into a git-backed remote."""

    # Single source of truth for required .gitignore entries (Bug #1871).
    # Referenced by BOTH the first-bootstrap and already-initialized
    # convergence paths in _write_gitignore() -- never duplicate this
    # literal elsewhere.
    _REQUIRED_GITIGNORE_ENTRIES: tuple = (
        ".code-indexer/",
        ".snapshot-reader-leases/",
    )

    def __init__(self, cancel_check: Optional[Callable[[], bool]] = None) -> None:
        # Bug #2012: the owning refresh job's cancel check; every git call
        # made by bootstrap() (via _git) is then terminated on cancel.
        self._cancel_check = cancel_check

    def _git(
        self, cidx_meta_path: str, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        from .stall_detection import with_ssh_keepalive

        env = with_ssh_keepalive(build_non_interactive_git_env())
        env.setdefault("GIT_AUTHOR_NAME", "cidx-meta-backup")
        env.setdefault("GIT_AUTHOR_EMAIL", "cidx-meta-backup@example.invalid")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        # Bug #1500 (defense-in-depth): every commit this helper currently
        # issues already passes -m, so this is not fixing an active bug
        # here, but it protects any future rebase/merge step from opening
        # an interactive editor in the non-interactive systemd job context
        # -- see CidxMetaBackupSync._git() for the active fix this mirrors.
        env.setdefault("GIT_EDITOR", "true")
        # EVO-64678: this helper runs arbitrary git (including commits) against
        # cidx-meta with argv only known at runtime, so it goes through the seam
        # chokepoint and lets is_mutating_git() classify each call rather than a
        # static scan guessing. No invoker claims cidx-meta today, so behaviour is
        # unchanged; the env below is merged on top of get_git_environment().
        # Bug #2012: the owning job's cancel check terminates the git process.
        return run_git_command(
            ["git", *args],
            cwd=Path(cidx_meta_path),
            cancel_check=self._cancel_check,
            capture_output=True,
            text=True,
            env=env,
            check=check,
        )

    def _write_gitignore(self, cidx_meta_path: str) -> None:
        """Idempotently converge .gitignore to include every required entry
        (Bug #1871). Check-then-apply: preserves any existing lines --
        including operator-added ones -- appending only whichever required
        entries are currently missing, in their declared order, and never
        rewrites the file when it already contains everything required.

        Called from BOTH bootstrap() return paths (first-bootstrap and
        already-initialized) so an already-deployed host self-heals a
        stale .gitignore automatically, with no human editing files by
        hand.
        """
        gitignore_path = Path(cidx_meta_path) / ".gitignore"
        existing_lines = (
            gitignore_path.read_text().splitlines() if gitignore_path.exists() else []
        )
        missing_entries = [
            entry
            for entry in self._REQUIRED_GITIGNORE_ENTRIES
            if entry not in existing_lines
        ]
        if not missing_entries:
            return
        new_content = "\n".join(existing_lines + missing_entries) + "\n"
        gitignore_path.write_text(new_content)

    def _push(self, cidx_meta_path: str, branch: str) -> None:
        """Push to remote. Raises BackupPushFailed on any failure (stall,
        unreachable remote, rejection) -- never force-pushes."""
        from .stall_detection import push_failure_reason

        result = self._git(
            cidx_meta_path, "push", "origin", f"HEAD:{branch}", check=False
        )
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise BackupPushFailed(push_failure_reason(stderr), stderr)

    def bootstrap(self, cidx_meta_path: str, remote_url: str) -> str:
        """Initialize or re-point git backup state for cidx-meta.

        Refused (CidxMetaBackupUnsupportedError) under a delegated content
        backend: this pod's directory is not cidx-meta's content there.
        """
        refuse_backup_of_shared_content(cidx_meta_path)
        branch = (
            detect_default_branch(cidx_meta_path, cancel_check=self._cancel_check)
            or "master"
        )

        if not cidx_meta_is_git_repository(cidx_meta_path):
            self._git(cidx_meta_path, "init")
            self._git(cidx_meta_path, "checkout", "-B", branch)
            self._write_gitignore(cidx_meta_path)
            self._git(cidx_meta_path, "add", "-A")
            self._git(cidx_meta_path, "commit", "-m", "auto: initial cidx-meta state")
            self._git(cidx_meta_path, "remote", "add", "origin", remote_url)
            self._push(cidx_meta_path, branch)
            return "bootstrapped"

        current_remote_result = self._git(
            cidx_meta_path, "remote", "get-url", "origin", check=False
        )
        current_remote = (
            current_remote_result.stdout.strip()
            if current_remote_result.returncode == 0
            else None
        )

        # Bug #1871 self-heal: an already-initialized host can never reach
        # the first-bootstrap call site above again, so convergence must
        # also run here on every invocation.
        self._write_gitignore(cidx_meta_path)

        if current_remote != remote_url:
            self._git(cidx_meta_path, "checkout", "-B", branch)
            if current_remote is None:
                self._git(cidx_meta_path, "remote", "add", "origin", remote_url)
            else:
                self._git(cidx_meta_path, "remote", "set-url", "origin", remote_url)
            self._push(cidx_meta_path, branch)

        return "already_initialized"
