"""Bootstrap cidx-meta git backup state."""

from __future__ import annotations

import subprocess
from pathlib import Path

from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env

from .branch_detect import detect_default_branch


class CidxMetaBackupBootstrap:
    """Bootstrap a mutable cidx-meta directory into a git-backed remote."""

    def _git(
        self, cidx_meta_path: str, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        env = build_non_interactive_git_env()
        env.setdefault("GIT_AUTHOR_NAME", "cidx-meta-backup")
        env.setdefault("GIT_AUTHOR_EMAIL", "cidx-meta-backup@example.invalid")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        return subprocess.run(
            ["git", *args],
            cwd=cidx_meta_path,
            capture_output=True,
            text=True,
            env=env,
            check=check,
        )

    def _write_gitignore(self, cidx_meta_path: str) -> None:
        gitignore_path = Path(cidx_meta_path) / ".gitignore"
        content = ".code-indexer/\n"
        if not gitignore_path.exists() or gitignore_path.read_text() != content:
            gitignore_path.write_text(content)

    def _push_with_fallback(self, cidx_meta_path: str, branch: str) -> None:
        """Try plain push first; fall back to --force if rejected; raise if both fail."""
        plain = self._git(
            cidx_meta_path, "push", "origin", f"HEAD:{branch}", check=False
        )
        if plain.returncode == 0:
            return
        force = self._git(
            cidx_meta_path, "push", "--force", "origin", f"HEAD:{branch}", check=False
        )
        if force.returncode == 0:
            return
        stderr = (force.stderr or force.stdout or "").strip()
        raise RuntimeError(f"push failed (plain and --force both rejected): {stderr}")

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
            self._push_with_fallback(cidx_meta_path, branch)
            return "bootstrapped"

        current_remote_result = self._git(
            cidx_meta_path, "remote", "get-url", "origin", check=False
        )
        current_remote = (
            current_remote_result.stdout.strip()
            if current_remote_result.returncode == 0
            else None
        )

        if current_remote != remote_url:
            self._git(cidx_meta_path, "checkout", "-B", branch)
            if current_remote is None:
                self._git(cidx_meta_path, "remote", "add", "origin", remote_url)
            else:
                self._git(cidx_meta_path, "remote", "set-url", "origin", remote_url)
            self._push_with_fallback(cidx_meta_path, branch)

        return "already_initialized"
