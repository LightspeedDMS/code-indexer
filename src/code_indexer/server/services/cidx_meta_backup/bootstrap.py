"""Bootstrap cidx-meta git backup state."""

from __future__ import annotations

import subprocess
from pathlib import Path

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env

from .branch_detect import detect_default_branch


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

    def _git(
        self, cidx_meta_path: str, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        env = build_non_interactive_git_env()
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
        return subprocess.run(
            ["git", *args],
            cwd=cidx_meta_path,
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
        """Push to remote. Raises RuntimeError on rejection -- never force-pushes."""
        result = self._git(
            cidx_meta_path, "push", "origin", f"HEAD:{branch}", check=False
        )
        if result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"push rejected by remote: {stderr}")

    def bootstrap(self, cidx_meta_path: str, remote_url: str) -> str:
        """Initialize or re-point git backup state for cidx-meta."""
        git_dir = Path(cidx_meta_path) / ".git"
        branch = detect_default_branch(cidx_meta_path) or "master"

        if not git_dir.exists():
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
