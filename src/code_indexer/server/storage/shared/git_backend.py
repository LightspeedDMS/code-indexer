"""
Git backend abstraction for cidx's internal git operations.

EVO-64616 Phase 2 (EVO-64678) — the git sibling of ``CloneBackend``
(``storage/shared/clone_backend.py``). cidx's job is semantic search; it should
do **no git work itself**. Today git runs as scattered raw
``subprocess.run(["git", ...])`` across the golden-repo and activated-repo
managers. This module introduces a single seam so those call sites route through
a pluggable backend:

- ``LocalGitBackend``  — runs git via the existing ``utils.git_runner`` helper
  (SSH-safe env + ``safe.directory`` handling). This is the DEFAULT and
  reproduces today's behavior.
- ``GitMcpBackend``    — (Phase 3, shipped as the ``cidx_git_backend`` plugin)
  delegates each operation to the git-mcp spoke. Not defined here.

The Protocol uses structural subtyping (``typing.Protocol``), NOT inheritance,
mirroring ``CloneBackend``. Selection is config-driven in
``startup/git_backend_wiring.py``; default ``local`` = zero regression.

**Timeouts are part of the contract.** Every call site today passes a
config-driven timeout (``git_clone_timeout``, ``git_pull_timeout``, ...), so
every method accepts one and a refactor can preserve it exactly. ``None`` means
"no timeout", matching ``subprocess.run``.

**Failures raise.** Methods run with ``check=True`` unless documented otherwise,
so a non-zero exit raises ``subprocess.CalledProcessError`` — whose ``stderr``
callers already use to build their own errors (e.g. ``GitOperationError``).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from code_indexer.utils.git_runner import (
    run_git_command,
    run_git_command_with_retry,
)
from code_indexer.utils.subprocess_diagnostics import redact_url_credentials

logger = logging.getLogger(__name__)

Timeout = Optional[float]


# ---------------------------------------------------------------------------
# Protocol definition
# ---------------------------------------------------------------------------


@runtime_checkable
class GitBackend(Protocol):
    """Structural interface for cidx's internal git operations."""

    # -- capabilities ---------------------------------------------------------
    @property
    def owns_credentials(self) -> bool:
        """True when this backend supplies its OWN git credentials for network
        operations (so callers must NOT embed a token in the repo URL, and any
        credential-bearing URL should be stripped before it is persisted).

        ``LocalGitBackend`` returns False — it runs git in-process and relies on
        the credential carried in the URL / ambient git config. The delegated
        git-mcp backend returns True — git-mcp holds the token (EVO-64616), so the
        PAT lives only there and never in cidx's stored golden-repo state.
        """
        ...  # pragma: no cover

    # -- remote-URL operations ------------------------------------------------
    def ls_remote(self, url: str, timeout: Timeout = None) -> bool:
        """``git ls-remote <url>`` -> True when the remote is reachable."""
        ...  # pragma: no cover

    def clone(
        self,
        url: str,
        dest_path: str,
        branch: Optional[str] = None,
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
    ) -> None:
        """``git [-c core.fsync=none] clone [--branch B] <url> <dest_path>``.

        ``retry`` is opt-in: today's call sites use a plain single-shot run, and
        a silent retry would change failure behavior (a retried clone hits
        "destination path already exists" and reports a different error).
        """
        ...  # pragma: no cover

    # -- fetch / pull / merge -------------------------------------------------
    def fetch(
        self,
        repo_path: str,
        remote: str = "origin",
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
    ) -> None:
        """``git [-c core.fsync=none] fetch <remote>`` (``retry`` opt-in)."""
        ...  # pragma: no cover

    def pull(
        self,
        repo_path: str,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        mode: str = "default",
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
    ) -> None:
        """``git pull [<remote> <branch>]``; mode = default|ff-only|no-ff|rebase."""
        ...  # pragma: no cover

    def merge(self, repo_path: str, ref: str, timeout: Timeout = None) -> None:
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
        timeout: Timeout = None,
    ) -> None:
        """``git checkout [-f] [-b|-B] <ref> [<start_point>]``."""
        ...  # pragma: no cover

    def current_branch(self, repo_path: str, timeout: Timeout = None) -> str:
        """Short current branch name (``symbolic-ref --short HEAD``)."""
        ...  # pragma: no cover

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
        timeout: Timeout = None,
    ) -> List[str]:
        """``git branch [-a|-r] [--format=<fmt>]`` -> non-empty stripped lines."""
        ...  # pragma: no cover

    # -- inspection -----------------------------------------------------------
    def rev_parse(self, repo_path: str, *args: str, timeout: Timeout = None) -> str:
        """``git rev-parse <args...>`` -> stripped stdout."""
        ...  # pragma: no cover

    def rev_list_count(
        self, repo_path: str, range_expr: str, timeout: Timeout = None
    ) -> int:
        """``git rev-list --count <range_expr>`` -> int."""
        ...  # pragma: no cover

    def symbolic_ref(
        self, repo_path: str, short: bool = True, timeout: Timeout = None
    ) -> str:
        """``git symbolic-ref [--short] HEAD``."""
        ...  # pragma: no cover

    def status_porcelain(self, repo_path: str, timeout: Timeout = None) -> str:
        """``git status --porcelain`` -> raw stdout."""
        ...  # pragma: no cover

    def diff_name_only(
        self, repo_path: str, spec: str, timeout: Timeout = None
    ) -> List[str]:
        """``git diff --name-only <spec>`` -> paths (spec may be ``A..B``)."""
        ...  # pragma: no cover

    def ls_files(self, repo_path: str, timeout: Timeout = None) -> List[str]:
        """``git ls-files`` -> tracked paths."""
        ...  # pragma: no cover

    def show_ref(
        self, repo_path: str, *args: str, timeout: Timeout = None
    ) -> subprocess.CompletedProcess:
        """``git show-ref <args...>`` — ``check=False``; caller reads returncode."""
        ...  # pragma: no cover

    # -- history (temporal indexing and queries) ------------------------------
    # Output is git's raw stdout BYTES, so the consumer decodes it exactly as a
    # local ``subprocess.run(text=True)`` would. Each call is one page or one
    # batch -- never one call per commit or per file.
    def log_raw(
        self,
        repo_path: str,
        fmt: str,
        revs: Sequence[str] = (),
        *,
        all_refs: bool = False,
        since: Optional[str] = None,
        skip: Optional[int] = None,
        max_count: Optional[int] = None,
        timeout: Timeout = None,
    ) -> bytes:
        """``git log --format=<fmt> [--all] [--since S] [--skip=N] [-n M] [<revs>]``."""
        ...  # pragma: no cover

    def diff_commits(
        self,
        repo_path: str,
        pairs: Sequence[Tuple[str, str]],
        context_lines: int,
        timeout: Timeout = None,
    ) -> List[bytes]:
        """``git diff -M -U<n> --full-index <base> <commit>`` for each pair.

        Raises ``CalledProcessError`` for the first pair git fails on.
        """
        ...  # pragma: no cover

    def show_objects(
        self,
        repo_path: str,
        specs: Sequence[Tuple[str, str]],
        timeout: Timeout = None,
    ) -> List[subprocess.CompletedProcess]:
        """``git show <rev>:<path>`` for each ``(rev, path)`` -- ``check=False``.

        Each result carries git's returncode, raw stdout bytes and stderr text.
        """
        ...  # pragma: no cover

    # -- mutation -------------------------------------------------------------
    def reset(
        self, repo_path: str, mode: str, target: str, timeout: Timeout = None
    ) -> None:
        """``git reset --<mode> <target>``."""
        ...  # pragma: no cover

    def restore(
        self, repo_path: str, pathspec: str = ".", timeout: Timeout = None
    ) -> None:
        """``git restore <pathspec>``."""
        ...  # pragma: no cover

    def update_index_refresh(self, repo_path: str, timeout: Timeout = None) -> None:
        """``git update-index --refresh`` — ``check=False`` (non-zero is normal)."""
        ...  # pragma: no cover

    def config_set(
        self, repo_path: str, key: str, value: str, timeout: Timeout = None
    ) -> None:
        """``git config <key> <value>`` (local, persistent)."""
        ...  # pragma: no cover

    # -- remotes --------------------------------------------------------------
    def remote_list(
        self, repo_path: str, verbose: bool = False, timeout: Timeout = None
    ) -> str:
        """``git remote [-v]`` -> raw stdout."""
        ...  # pragma: no cover

    def remote_get_url(self, repo_path: str, name: str, timeout: Timeout = None) -> str:
        """``git remote get-url <name>`` -> stripped stdout."""
        ...  # pragma: no cover

    def remote_add(
        self, repo_path: str, name: str, url: str, timeout: Timeout = None
    ) -> None:
        """``git remote add <name> <url>``."""
        ...  # pragma: no cover

    def remote_set_url(
        self, repo_path: str, name: str, url: str, timeout: Timeout = None
    ) -> None:
        """``git remote set-url <name> <url>``."""
        ...  # pragma: no cover

    def remote_rename(
        self, repo_path: str, old: str, new: str, timeout: Timeout = None
    ) -> None:
        """``git remote rename <old> <new>``."""
        ...  # pragma: no cover

    def remote_remove(self, repo_path: str, name: str, timeout: Timeout = None) -> None:
        """``git remote remove <name>``."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# LocalGitBackend — current behavior, via utils.git_runner
# ---------------------------------------------------------------------------


class LocalGitBackend:
    """Runs git locally through ``utils.git_runner`` (the default backend).

    Every method reproduces the ``git`` invocation the call sites use today.
    Env handling (SSH-safe + ``safe.directory``) is delegated to
    ``run_git_command``; network-touching operations use the retrying variant,
    matching current behavior.
    """

    # -- capabilities ---------------------------------------------------------
    @property
    def owns_credentials(self) -> bool:
        """False: local git relies on the credential in the URL / git config, so
        callers must keep any embedded token. See ``GitBackend.owns_credentials``."""
        return False

    # -- single funnel: every git invocation goes through here -----------------
    @staticmethod
    def _run(
        cmd: Sequence[str],
        cwd: Path,
        *,
        check: bool = True,
        timeout: Timeout = None,
        retry: bool = False,
        text: bool = True,
    ) -> subprocess.CompletedProcess:
        runner = run_git_command_with_retry if retry else run_git_command
        if text:
            result: subprocess.CompletedProcess = runner(
                list(cmd), cwd=cwd, check=check, timeout=timeout
            )
        else:
            result = runner(
                list(cmd), cwd=cwd, check=check, timeout=timeout, text=False
            )
        return result

    @classmethod
    def _run_bytes(
        cls,
        cmd: Sequence[str],
        cwd: Path,
        *,
        check: bool = True,
        timeout: Timeout = None,
    ) -> subprocess.CompletedProcess:
        """Run git keeping stdout as BYTES; stderr is decoded for messages."""
        result = cls._run(cmd, cwd, check=False, timeout=timeout, text=False)
        stderr = bytes(result.stderr or b"").decode("utf-8", "replace")
        if check and result.returncode != 0:
            # The error travels into messages: git echoes a credentialed URL
            # back on failure, so both streams are redacted here. stdout
            # round-trips through ``surrogateescape`` so every byte other than
            # a redacted credential comes back exactly as git wrote it.
            raise subprocess.CalledProcessError(
                result.returncode,
                list(cmd),
                output=redact_url_credentials(
                    bytes(result.stdout or b"").decode("utf-8", "surrogateescape")
                ).encode("utf-8", "surrogateescape"),
                stderr=redact_url_credentials(stderr),
            )
        return subprocess.CompletedProcess(
            list(cmd), result.returncode, stdout=result.stdout, stderr=stderr
        )

    @staticmethod
    def _lines(stdout: object) -> List[str]:
        return [line.strip() for line in str(stdout).splitlines() if line.strip()]

    # -- remote-URL operations ------------------------------------------------
    def ls_remote(self, url: str, timeout: Timeout = None) -> bool:
        try:
            # ls-remote needs no repo cwd; run from the current directory.
            self._run(["git", "ls-remote", url], Path.cwd(), timeout=timeout)
            return True
        except subprocess.CalledProcessError:
            return False

    def clone(
        self,
        url: str,
        dest_path: str,
        branch: Optional[str] = None,
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
    ) -> None:
        cmd = ["git"]
        if no_fsync:
            cmd += ["-c", "core.fsync=none"]
        cmd += ["clone"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [url, dest_path]
        # The clone target does not exist yet — run from its parent.
        self._run(cmd, Path(dest_path).parent, timeout=timeout, retry=retry)

    # -- fetch / pull / merge -------------------------------------------------
    def fetch(
        self,
        repo_path: str,
        remote: str = "origin",
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
    ) -> None:
        cmd = ["git"]
        if no_fsync:
            cmd += ["-c", "core.fsync=none"]
        cmd += ["fetch", remote]
        self._run(cmd, Path(repo_path), timeout=timeout, retry=retry)

    def pull(
        self,
        repo_path: str,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        mode: str = "default",
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
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
        self._run(cmd, Path(repo_path), timeout=timeout, retry=retry)

    def merge(self, repo_path: str, ref: str, timeout: Timeout = None) -> None:
        self._run(["git", "merge", ref], Path(repo_path), timeout=timeout)

    # -- checkout / branch ----------------------------------------------------
    def checkout(
        self,
        repo_path: str,
        ref: str,
        create: bool = False,
        force_new: bool = False,
        force: bool = False,
        start_point: Optional[str] = None,
        timeout: Timeout = None,
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
        self._run(cmd, Path(repo_path), timeout=timeout)

    def current_branch(self, repo_path: str, timeout: Timeout = None) -> str:
        result = self._run(
            ["git", "symbolic-ref", "--short", "HEAD"], Path(repo_path), timeout=timeout
        )
        return str(result.stdout).strip()

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
        timeout: Timeout = None,
    ) -> List[str]:
        cmd = ["git", "branch"]
        if all_refs:
            cmd.append("-a")
        elif remote:
            cmd.append("-r")
        if fmt is not None:
            cmd.append(f"--format={fmt}")
        return self._lines(self._run(cmd, Path(repo_path), timeout=timeout).stdout)

    # -- inspection -----------------------------------------------------------
    def rev_parse(self, repo_path: str, *args: str, timeout: Timeout = None) -> str:
        result = self._run(
            ["git", "rev-parse", *args], Path(repo_path), timeout=timeout
        )
        return str(result.stdout).strip()

    def rev_list_count(
        self, repo_path: str, range_expr: str, timeout: Timeout = None
    ) -> int:
        result = self._run(
            ["git", "rev-list", "--count", range_expr], Path(repo_path), timeout=timeout
        )
        return int(str(result.stdout).strip() or "0")

    def symbolic_ref(
        self, repo_path: str, short: bool = True, timeout: Timeout = None
    ) -> str:
        cmd = ["git", "symbolic-ref"]
        if short:
            cmd.append("--short")
        cmd.append("HEAD")
        return str(self._run(cmd, Path(repo_path), timeout=timeout).stdout).strip()

    def status_porcelain(self, repo_path: str, timeout: Timeout = None) -> str:
        result = self._run(
            ["git", "status", "--porcelain"], Path(repo_path), timeout=timeout
        )
        return str(result.stdout)

    def diff_name_only(
        self, repo_path: str, spec: str, timeout: Timeout = None
    ) -> List[str]:
        result = self._run(
            ["git", "diff", "--name-only", spec], Path(repo_path), timeout=timeout
        )
        return self._lines(result.stdout)

    def ls_files(self, repo_path: str, timeout: Timeout = None) -> List[str]:
        result = self._run(["git", "ls-files"], Path(repo_path), timeout=timeout)
        return [line for line in str(result.stdout).splitlines() if line]

    def show_ref(
        self, repo_path: str, *args: str, timeout: Timeout = None
    ) -> subprocess.CompletedProcess:
        return self._run(
            ["git", "show-ref", *args], Path(repo_path), check=False, timeout=timeout
        )

    # -- history (temporal indexing and queries) ------------------------------
    def log_raw(
        self,
        repo_path: str,
        fmt: str,
        revs: Sequence[str] = (),
        *,
        all_refs: bool = False,
        since: Optional[str] = None,
        skip: Optional[int] = None,
        max_count: Optional[int] = None,
        timeout: Timeout = None,
    ) -> bytes:
        # The flags in the order TemporalIndexer._get_commit_history passes them.
        cmd = ["git", "log", f"--format={fmt}"]
        if all_refs:
            cmd.append("--all")
        if since:
            cmd.extend(["--since", since])
        if skip:
            cmd.append(f"--skip={skip}")
        if max_count:
            cmd.extend(["-n", str(max_count)])
        cmd.extend(revs)
        return bytes(self._run_bytes(cmd, Path(repo_path), timeout=timeout).stdout)

    def diff_commits(
        self,
        repo_path: str,
        pairs: Sequence[Tuple[str, str]],
        context_lines: int,
        timeout: Timeout = None,
    ) -> List[bytes]:
        # The argv commit_aggregator.get_file_changes runs for one commit.
        return [
            bytes(
                self._run_bytes(
                    [
                        "git",
                        "diff",
                        "-M",
                        f"-U{context_lines}",
                        "--full-index",
                        base,
                        commit,
                    ],
                    Path(repo_path),
                    timeout=timeout,
                ).stdout
            )
            for base, commit in pairs
        ]

    def show_objects(
        self,
        repo_path: str,
        specs: Sequence[Tuple[str, str]],
        timeout: Timeout = None,
    ) -> List[subprocess.CompletedProcess]:
        # The argv TemporalSearchService._reconstruct_temporal_content runs.
        return [
            self._run_bytes(
                ["git", "show", f"{rev}:{path}"],
                Path(repo_path),
                check=False,
                timeout=timeout,
            )
            for rev, path in specs
        ]

    # -- mutation -------------------------------------------------------------
    def reset(
        self, repo_path: str, mode: str, target: str, timeout: Timeout = None
    ) -> None:
        self._run(
            ["git", "reset", f"--{mode}", target], Path(repo_path), timeout=timeout
        )

    def restore(
        self, repo_path: str, pathspec: str = ".", timeout: Timeout = None
    ) -> None:
        self._run(["git", "restore", pathspec], Path(repo_path), timeout=timeout)

    def update_index_refresh(self, repo_path: str, timeout: Timeout = None) -> None:
        # --refresh returns non-zero when the index is merely out of date; non-fatal.
        self._run(
            ["git", "update-index", "--refresh"],
            Path(repo_path),
            check=False,
            timeout=timeout,
        )

    def config_set(
        self, repo_path: str, key: str, value: str, timeout: Timeout = None
    ) -> None:
        self._run(["git", "config", key, value], Path(repo_path), timeout=timeout)

    # -- remotes --------------------------------------------------------------
    def remote_list(
        self, repo_path: str, verbose: bool = False, timeout: Timeout = None
    ) -> str:
        cmd = ["git", "remote"]
        if verbose:
            cmd.append("-v")
        return str(self._run(cmd, Path(repo_path), timeout=timeout).stdout)

    def remote_get_url(self, repo_path: str, name: str, timeout: Timeout = None) -> str:
        result = self._run(
            ["git", "remote", "get-url", name], Path(repo_path), timeout=timeout
        )
        return str(result.stdout).strip()

    def remote_add(
        self, repo_path: str, name: str, url: str, timeout: Timeout = None
    ) -> None:
        self._run(["git", "remote", "add", name, url], Path(repo_path), timeout=timeout)

    def remote_set_url(
        self, repo_path: str, name: str, url: str, timeout: Timeout = None
    ) -> None:
        self._run(
            ["git", "remote", "set-url", name, url], Path(repo_path), timeout=timeout
        )

    def remote_rename(
        self, repo_path: str, old: str, new: str, timeout: Timeout = None
    ) -> None:
        self._run(
            ["git", "remote", "rename", old, new], Path(repo_path), timeout=timeout
        )

    def remote_remove(self, repo_path: str, name: str, timeout: Timeout = None) -> None:
        self._run(["git", "remote", "remove", name], Path(repo_path), timeout=timeout)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

#: What git prints when HEAD is not a branch: ``fatal: ref HEAD is not a
#: symbolic ref``. git-mcp surfaces git's stderr verbatim, so the same text
#: identifies a detached HEAD through either backend.
DETACHED_HEAD_MARKER = "is not a symbolic ref"


def head_branch(
    backend: "GitBackend", repo_path: str, timeout: Timeout = None
) -> Optional[str]:
    """The checked-out branch, or ``None`` when HEAD is detached.

    A detached HEAD is a normal state (a repository pinned to a tag or commit)
    and is reported as ``None``. ANY other failure -- a transport error to
    git-mcp, a path that is not a repository -- raises: reading it as
    "detached" would make refresh silently skip the pull, which is exactly how
    a pod without the repository on its disk used to fail.
    """
    try:
        return backend.symbolic_ref(repo_path, short=True, timeout=timeout) or None
    except subprocess.CalledProcessError as exc:
        if DETACHED_HEAD_MARKER in str(exc.stderr or ""):
            return None
        raise


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
                    "provided. Install a package exposing a GitBackend factory on the "
                    "'code_indexer.git_backend' entry-point group, or pass one "
                    "explicitly to the factory."
                )
            return delegated_backend
        raise ValueError(
            f"Unknown git_backend {backend!r}; valid values are {VALID_GIT_BACKENDS}."
        )
