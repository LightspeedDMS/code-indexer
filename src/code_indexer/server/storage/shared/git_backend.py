"""
Git backend abstraction for cidx's internal git operations.

EVO-64616 Phase 2 (EVO-64678) — the git sibling of ``CloneBackend``
(``storage/shared/clone_backend.py``). cidx's job is semantic search; it should
do **no git work itself**. Today git runs as scattered raw
``subprocess.run(["git", ...])`` across the golden-repo and activated-repo
managers. This module introduces a single seam so those call sites route through
a pluggable backend:

- ``LocalGitBackend``  — runs git via the existing ``utils.git_runner`` helper
  (SSH-safe env + ``safe.directory`` handling). This is the DEFAULT and reproduces
  today's behavior exactly.
- ``GitMcpBackend``    — (Phase 3, shipped as the ``cidx_git_backend`` plugin)
  delegates each operation to the git-mcp spoke. Not defined here.

The Protocol uses structural subtyping (``typing.Protocol``), NOT inheritance,
mirroring ``CloneBackend``. Selection is config-driven in
``startup/git_backend_wiring.py``; default ``local`` = zero regression.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import List, Optional

from code_indexer.utils.git_runner import (
    run_git_command,
    run_git_command_with_retry,
)

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore  # pragma: no cover

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol definition
# ---------------------------------------------------------------------------


@runtime_checkable
class GitBackend(Protocol):
    """Structural interface for cidx's internal git operations.

    Every method operates on a repo identified by an absolute ``repo_path``
    (except ``clone``/``ls_remote`` which act on a URL). Implementations MUST
    preserve the exception contract of the local git CLI: a failing command
    raises ``subprocess.CalledProcessError`` unless the method explicitly
    documents otherwise.
    """

    # -- remote-URL operations ------------------------------------------------
    def ls_remote(self, url: str) -> bool:
        """Return True if ``git ls-remote <url>`` succeeds (URL reachable)."""
        ...  # pragma: no cover

    def clone(
        self,
        url: str,
        dest_path: str,
        branch: Optional[str] = None,
        no_fsync: bool = False,
    ) -> None:
        """``git [-c core.fsync=none] clone [--branch B] <url> <dest_path>``."""
        ...  # pragma: no cover

    # -- fetch / pull / merge -------------------------------------------------
    def fetch(
        self, repo_path: str, remote: str = "origin", no_fsync: bool = False
    ) -> None:
        """``git [-c core.fsync=none] fetch <remote>``."""
        ...  # pragma: no cover

    def pull(
        self,
        repo_path: str,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        mode: str = "default",
        no_fsync: bool = False,
    ) -> None:
        """``git pull [<remote> <branch>]`` with mode = default|ff-only|no-ff|rebase."""
        ...  # pragma: no cover

    def merge(self, repo_path: str, ref: str) -> None:
        """``git merge <ref>``."""
        ...  # pragma: no cover

    # -- checkout / branch ----------------------------------------------------
    def checkout(
        self,
        repo_path: str,
        ref: str,
        create: bool = False,
        force_new: bool = False,
        force: bool = False,
        start_point: Optional[str] = None,
    ) -> None:
        """``git checkout [-f] [-b|-B] <ref> [<start_point>]``."""
        ...  # pragma: no cover

    def current_branch(self, repo_path: str) -> str:
        """Short current branch name (``symbolic-ref --short HEAD``)."""
        ...  # pragma: no cover

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
    ) -> List[str]:
        """``git branch [-a|-r] [--format=<fmt>]`` -> list of lines."""
        ...  # pragma: no cover

    # -- inspection -----------------------------------------------------------
    def rev_parse(self, repo_path: str, *args: str) -> str:
        """``git rev-parse <args...>`` -> stripped stdout."""
        ...  # pragma: no cover

    def rev_list_count(self, repo_path: str, range_expr: str) -> int:
        """``git rev-list --count <range_expr>`` -> int."""
        ...  # pragma: no cover

    def symbolic_ref(self, repo_path: str, short: bool = True) -> str:
        """``git symbolic-ref [--short] HEAD``."""
        ...  # pragma: no cover

    def status_porcelain(self, repo_path: str) -> str:
        """``git status --porcelain`` -> raw stdout."""
        ...  # pragma: no cover

    def diff_name_only(self, repo_path: str, spec: str) -> List[str]:
        """``git diff --name-only <spec>`` -> paths (spec may be ``A..B``)."""
        ...  # pragma: no cover

    def ls_files(self, repo_path: str) -> List[str]:
        """``git ls-files`` -> tracked paths."""
        ...  # pragma: no cover

    def show_ref(self, repo_path: str, *args: str) -> subprocess.CompletedProcess:
        """``git show-ref <args...>`` (check=False; caller inspects returncode)."""
        ...  # pragma: no cover

    # -- mutation -------------------------------------------------------------
    def reset(self, repo_path: str, mode: str, target: str) -> None:
        """``git reset --<mode> <target>``."""
        ...  # pragma: no cover

    def restore(self, repo_path: str, pathspec: str = ".") -> None:
        """``git restore <pathspec>``."""
        ...  # pragma: no cover

    def update_index_refresh(self, repo_path: str) -> None:
        """``git update-index --refresh`` (check=False; non-zero is non-fatal)."""
        ...  # pragma: no cover

    def config_set(self, repo_path: str, key: str, value: str) -> None:
        """``git config <key> <value>`` (local, persistent)."""
        ...  # pragma: no cover

    # -- remotes --------------------------------------------------------------
    def remote_list(self, repo_path: str, verbose: bool = False) -> str:
        """``git remote [-v]`` -> raw stdout."""
        ...  # pragma: no cover

    def remote_get_url(self, repo_path: str, name: str) -> str:
        """``git remote get-url <name>`` -> stripped stdout."""
        ...  # pragma: no cover

    def remote_add(self, repo_path: str, name: str, url: str) -> None:
        """``git remote add <name> <url>``."""
        ...  # pragma: no cover

    def remote_set_url(self, repo_path: str, name: str, url: str) -> None:
        """``git remote set-url <name> <url>``."""
        ...  # pragma: no cover

    def remote_rename(self, repo_path: str, old: str, new: str) -> None:
        """``git remote rename <old> <new>``."""
        ...  # pragma: no cover

    def remote_remove(self, repo_path: str, name: str) -> None:
        """``git remote remove <name>``."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# LocalGitBackend — current behavior, via utils.git_runner
# ---------------------------------------------------------------------------


class LocalGitBackend:
    """Runs git locally through ``utils.git_runner`` (the default backend).

    Every method reproduces the exact ``git`` invocation the call sites use
    today, delegating env handling (SSH-safe, ``safe.directory``) to
    ``run_git_command``. Network-touching operations (clone/fetch/pull) use the
    retrying variant, matching current behavior.
    """

    @staticmethod
    def _p(repo_path: str) -> Path:
        return Path(repo_path)

    def ls_remote(self, url: str) -> bool:
        try:
            # ls-remote needs no repo cwd; run from the current directory.
            run_git_command(["git", "ls-remote", url], cwd=Path.cwd(), check=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def clone(
        self,
        url: str,
        dest_path: str,
        branch: Optional[str] = None,
        no_fsync: bool = False,
    ) -> None:
        cmd = ["git"]
        if no_fsync:
            cmd += ["-c", "core.fsync=none"]
        cmd += ["clone"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [url, dest_path]
        # The clone target does not exist yet — run from its parent.
        run_git_command_with_retry(cmd, cwd=Path(dest_path).parent)

    def fetch(
        self, repo_path: str, remote: str = "origin", no_fsync: bool = False
    ) -> None:
        cmd = ["git"]
        if no_fsync:
            cmd += ["-c", "core.fsync=none"]
        cmd += ["fetch", remote]
        run_git_command_with_retry(cmd, cwd=self._p(repo_path))

    def pull(
        self,
        repo_path: str,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        mode: str = "default",
        no_fsync: bool = False,
    ) -> None:
        cmd = ["git"]
        if no_fsync:
            cmd += ["-c", "core.fsync=none"]
        cmd += ["pull"]
        mode_flags = {"ff-only": "--ff-only", "no-ff": "--no-ff", "rebase": "--rebase"}
        if mode in mode_flags:
            cmd.append(mode_flags[mode])
        if remote:
            cmd.append(remote)
        if branch:
            cmd.append(branch)
        run_git_command_with_retry(cmd, cwd=self._p(repo_path))

    def merge(self, repo_path: str, ref: str) -> None:
        run_git_command(["git", "merge", ref], cwd=self._p(repo_path))

    def checkout(
        self,
        repo_path: str,
        ref: str,
        create: bool = False,
        force_new: bool = False,
        force: bool = False,
        start_point: Optional[str] = None,
    ) -> None:
        cmd = ["git", "checkout"]
        if force:
            cmd.append("-f")
        if force_new:
            cmd.append("-B")
        elif create:
            cmd.append("-b")
        cmd.append(ref)
        if start_point:
            cmd.append(start_point)
        run_git_command(cmd, cwd=self._p(repo_path))

    def current_branch(self, repo_path: str) -> str:
        result = run_git_command(
            ["git", "symbolic-ref", "--short", "HEAD"], cwd=self._p(repo_path)
        )
        return str(result.stdout).strip()

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
    ) -> List[str]:
        cmd = ["git", "branch"]
        if all_refs:
            cmd.append("-a")
        elif remote:
            cmd.append("-r")
        if fmt is not None:
            cmd.append(f"--format={fmt}")
        result = run_git_command(cmd, cwd=self._p(repo_path))
        return [
            line.strip() for line in str(result.stdout).splitlines() if line.strip()
        ]

    def rev_parse(self, repo_path: str, *args: str) -> str:
        result = run_git_command(["git", "rev-parse", *args], cwd=self._p(repo_path))
        return str(result.stdout).strip()

    def rev_list_count(self, repo_path: str, range_expr: str) -> int:
        result = run_git_command(
            ["git", "rev-list", "--count", range_expr], cwd=self._p(repo_path)
        )
        return int(str(result.stdout).strip() or "0")

    def symbolic_ref(self, repo_path: str, short: bool = True) -> str:
        cmd = ["git", "symbolic-ref"]
        if short:
            cmd.append("--short")
        cmd.append("HEAD")
        result = run_git_command(cmd, cwd=self._p(repo_path))
        return str(result.stdout).strip()

    def status_porcelain(self, repo_path: str) -> str:
        result = run_git_command(
            ["git", "status", "--porcelain"], cwd=self._p(repo_path)
        )
        return str(result.stdout)

    def diff_name_only(self, repo_path: str, spec: str) -> List[str]:
        result = run_git_command(
            ["git", "diff", "--name-only", spec], cwd=self._p(repo_path)
        )
        return [
            line.strip() for line in str(result.stdout).splitlines() if line.strip()
        ]

    def ls_files(self, repo_path: str) -> List[str]:
        result = run_git_command(["git", "ls-files"], cwd=self._p(repo_path))
        return [line for line in str(result.stdout).splitlines() if line]

    def show_ref(self, repo_path: str, *args: str) -> subprocess.CompletedProcess:
        result: subprocess.CompletedProcess = run_git_command(
            ["git", "show-ref", *args], cwd=self._p(repo_path), check=False
        )
        return result

    def reset(self, repo_path: str, mode: str, target: str) -> None:
        run_git_command(["git", "reset", f"--{mode}", target], cwd=self._p(repo_path))

    def restore(self, repo_path: str, pathspec: str = ".") -> None:
        run_git_command(["git", "restore", pathspec], cwd=self._p(repo_path))

    def update_index_refresh(self, repo_path: str) -> None:
        # --refresh returns non-zero when the index is merely out of date; non-fatal.
        run_git_command(
            ["git", "update-index", "--refresh"], cwd=self._p(repo_path), check=False
        )

    def config_set(self, repo_path: str, key: str, value: str) -> None:
        run_git_command(["git", "config", key, value], cwd=self._p(repo_path))

    def remote_list(self, repo_path: str, verbose: bool = False) -> str:
        cmd = ["git", "remote"]
        if verbose:
            cmd.append("-v")
        return str(run_git_command(cmd, cwd=self._p(repo_path)).stdout)

    def remote_get_url(self, repo_path: str, name: str) -> str:
        result = run_git_command(
            ["git", "remote", "get-url", name], cwd=self._p(repo_path)
        )
        return str(result.stdout).strip()

    def remote_add(self, repo_path: str, name: str, url: str) -> None:
        run_git_command(["git", "remote", "add", name, url], cwd=self._p(repo_path))

    def remote_set_url(self, repo_path: str, name: str, url: str) -> None:
        run_git_command(["git", "remote", "set-url", name, url], cwd=self._p(repo_path))

    def remote_rename(self, repo_path: str, old: str, new: str) -> None:
        run_git_command(["git", "remote", "rename", old, new], cwd=self._p(repo_path))

    def remote_remove(self, repo_path: str, name: str) -> None:
        run_git_command(["git", "remote", "remove", name], cwd=self._p(repo_path))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

VALID_GIT_BACKENDS = ("local", "delegated")


class GitBackendFactory:
    """Create a GitBackend from a backend name.

    Mirrors ``CloneBackendFactory``. ``local`` (the default) -> ``LocalGitBackend``.
    ``delegated`` is supplied by the entry-point plugin seam
    (``services.git_backend_plugin``) in Phase 3; it raises here unless a
    plugin-provided backend is injected, so the core never silently no-ops.
    """

    @staticmethod
    def create(
        backend: str = "local", *, delegated_backend: Optional[GitBackend] = None
    ) -> GitBackend:
        if backend == "local":
            return LocalGitBackend()
        if backend == "delegated":
            if delegated_backend is None:
                raise RuntimeError(
                    "git_backend='delegated' selected but no delegated backend was "
                    "provided. The cidx_git_backend plugin (EVO-64678 Phase 3) must "
                    "supply a GitMcpBackend via the code_indexer.git_backend seam."
                )
            return delegated_backend
        raise ValueError(
            f"Unknown git_backend {backend!r}; valid values are {VALID_GIT_BACKENDS}."
        )
