"""An in-process fake of git-mcp, for the git invoker and git backend seams.

git-mcp runs git where the repositories are, with its own credentials. The
fake runs real ``git`` in the directory a request names (mapped through
``roots`` when cidx and the service address a repository differently: a
request path equal to, or under, a root is rewritten onto the service's
directory), records every request, and answers with a ``CompletedProcess``
whose non-zero exit is reported, never raised -- the invoker contract.

Two delegate-shaped clients: :class:`FakeGitMcpInvoker` (the git invoker) and
:class:`FakeGitMcpGitBackend` (the git backend, as the ``cidx_git_backend``
plugin presents git-mcp: it owns its credentials and raises
``CalledProcessError`` with git's stderr). The backend builds its argvs
independently of the local backend, so passing the git suite against it says
something about the suite.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cidx_sdk.entry_points import (
    GIT_BACKEND_ENTRY_POINT_GROUP,
    GIT_INVOKER_ENTRY_POINT_GROUP,
)
from cidx_sdk.seams.git import Timeout, redact_url_credentials
from cidx_sdk.seams.resolver import add_entry_points, remove_entry_points
from cidx_sdk.seams.selection import DELEGATED_MODE

#: GitCommandRunner identity fields -> git's commit-identity variables.
IDENTITY_ENV_NAMES = {
    "author_name": "GIT_AUTHOR_NAME",
    "author_email": "GIT_AUTHOR_EMAIL",
    "committer_name": "GIT_COMMITTER_NAME",
    "committer_email": "GIT_COMMITTER_EMAIL",
}


@dataclass
class FakeGitMcp:
    """The service. ``roots``: cidx path -> directory the service runs git in."""

    roots: Dict[str, Path] = field(default_factory=dict)
    requests: List[Tuple[Tuple[str, ...], str]] = field(default_factory=list)

    def _cwd(self, cwd: Any) -> Path:
        return Path(self.map_path(os.fspath(cwd)))

    def map_path(self, path: str) -> str:
        """*path* as the service holds it: a path at or under a root is
        rewritten onto the root's directory; anything else is taken as-is."""
        for cidx_root, directory in self.roots.items():
            root = cidx_root.rstrip("/")
            if path == root:
                return str(directory)
            if path.startswith(root + "/"):
                return str(directory / path[len(root) + 1 :])
        return path

    def git_exec(
        self,
        cmd: Sequence[str],
        cwd: Any,
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> "subprocess.CompletedProcess[str]":
        """Run *cmd* in *cwd* (mapped). *env* overrides the service's own
        commit identity, as git-mcp's ``git_exec`` takes one per request."""
        argv = tuple(cmd)
        self.requests.append((argv, os.fspath(cwd)))
        if not argv or argv[0] != "git":
            return subprocess.CompletedProcess(list(argv), 2, "", "not a git command")
        full_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Example",
            "GIT_AUTHOR_EMAIL": "example@example.com",
            "GIT_COMMITTER_NAME": "Example",
            "GIT_COMMITTER_EMAIL": "example@example.com",
            **(env or {}),
        }
        return subprocess.run(
            list(argv),
            cwd=self._cwd(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=full_env,
        )


class FakeGitMcpInvoker:
    """A git invoker that executes through :class:`FakeGitMcp`."""

    def __init__(self, service: FakeGitMcp) -> None:
        self.service = service

    def run(
        self, cmd: Sequence[str], cwd: Any, *, timeout: Optional[float] = None
    ) -> "subprocess.CompletedProcess[str]":
        return self.service.git_exec(cmd, cwd, timeout)


class FakeGitMcpGitBackend:
    """A git backend that runs every operation in :class:`FakeGitMcp`.

    Paths (repositories, clone destinations) are mapped onto the service's
    directories; a failure raises ``CalledProcessError`` carrying git's stderr
    (credentials in it redacted), as the delegate plugin does. It owns its
    credentials: ``credentials_url`` is ignored, as git-mcp ignores it.
    """

    def __init__(self, service: FakeGitMcp) -> None:
        self.service = service

    @property
    def owns_credentials(self) -> bool:
        return True

    # -- transport -------------------------------------------------------------

    def _exec(
        self,
        cwd: str,
        args: Sequence[str],
        *,
        check: bool = True,
        timeout: Timeout = None,
        text: bool = True,
    ) -> "subprocess.CompletedProcess[Any]":
        done = self.service.git_exec(["git", *args], cwd, timeout)
        if check and done.returncode != 0:
            raise subprocess.CalledProcessError(
                done.returncode,
                ["git", *args],
                output=redact_url_credentials(done.stdout),
                stderr=redact_url_credentials(done.stderr),
            )
        if text:
            return done
        return subprocess.CompletedProcess(
            done.args, done.returncode, done.stdout.encode("utf-8"), done.stderr
        )

    def _bytes(self, cwd: str, args: Sequence[str], timeout: Timeout) -> bytes:
        return bytes(self._exec(cwd, args, timeout=timeout, text=False).stdout)

    def _text(self, cwd: str, args: Sequence[str], timeout: Timeout) -> str:
        return str(self._exec(cwd, args, timeout=timeout).stdout)

    @staticmethod
    def _lines(text: str) -> List[str]:
        return [line.strip() for line in text.splitlines() if line.strip()]

    # -- remote-URL operations -------------------------------------------------

    def ls_remote(
        self, url: str, timeout: Timeout = None, credentials_url: Optional[str] = None
    ) -> bool:
        done = self._exec(os.getcwd(), ["ls-remote", url], check=False)
        return done.returncode == 0

    def clone(
        self,
        url: str,
        dest_path: str,
        branch: Optional[str] = None,
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
        credentials_url: Optional[str] = None,
    ) -> None:
        target = self.service.map_path(dest_path)
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", *(["--branch", branch] if branch else []), url, target]
        self._exec(str(Path(target).parent), args, timeout=timeout)

    # -- fetch / pull / merge --------------------------------------------------

    def fetch(
        self,
        repo_path: str,
        remote: str = "origin",
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
        credentials_url: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> None:
        refspec = [f"+{ref}:refs/remotes/{remote}/{ref}"] if ref else []
        self._exec(repo_path, ["fetch", remote, *refspec], timeout=timeout)

    def pull(
        self,
        repo_path: str,
        remote: Optional[str] = None,
        branch: Optional[str] = None,
        mode: str = "default",
        no_fsync: bool = False,
        timeout: Timeout = None,
        retry: bool = False,
        credentials_url: Optional[str] = None,
    ) -> None:
        flags = {"ff-only": "--ff-only", "no-ff": "--no-ff", "rebase": "--rebase"}
        args = ["pull", *([flags[mode]] if mode in flags else [])]
        args += [part for part in (remote, branch) if part]
        self._exec(repo_path, args, timeout=timeout)

    def merge(self, repo_path: str, ref: str, timeout: Timeout = None) -> None:
        self._exec(repo_path, ["merge", ref], timeout=timeout)

    # -- the git tools (GitCommandRunner) --------------------------------------

    def run(
        self,
        repo_path: str,
        args: Sequence[str],
        timeout: Timeout = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> "subprocess.CompletedProcess[Any]":
        env = {
            name: identity[field]
            for field, name in IDENTITY_ENV_NAMES.items()
            if identity and identity.get(field)
        }
        return self.service.git_exec(["git", *args], repo_path, timeout, env=env)

    def push(
        self,
        repo_path: str,
        remote: str,
        branch: Optional[str] = None,
        timeout: Timeout = None,
    ) -> "subprocess.CompletedProcess[Any]":
        args = ["push", remote, *([branch] if branch else [])]
        return self._exec(repo_path, args, timeout=timeout)

    # -- checkout / branch -----------------------------------------------------

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
        args = ["checkout", *(["-f"] if force else [])]
        if force_new:
            args.append("-B")
        elif create:
            args.append("-b")
        args += [ref, *([start_point] if start_point else [])]
        self._exec(repo_path, args, timeout=timeout)

    def current_branch(self, repo_path: str, timeout: Timeout = None) -> str:
        return self.symbolic_ref(repo_path, short=True, timeout=timeout)

    def list_branches(
        self,
        repo_path: str,
        remote: bool = False,
        all_refs: bool = False,
        fmt: Optional[str] = None,
        timeout: Timeout = None,
    ) -> List[str]:
        args = ["branch", *(["-a"] if all_refs else ["-r"] if remote else [])]
        if fmt is not None:
            args.append(f"--format={fmt}")
        return self._lines(self._text(repo_path, args, timeout))

    # -- inspection ------------------------------------------------------------

    def rev_parse(self, repo_path: str, *args: str, timeout: Timeout = None) -> str:
        return self._text(repo_path, ["rev-parse", *args], timeout).strip()

    def rev_list_count(
        self, repo_path: str, range_expr: str, timeout: Timeout = None
    ) -> int:
        out = self._text(repo_path, ["rev-list", "--count", range_expr], timeout)
        return int(out.strip() or "0")

    def symbolic_ref(
        self, repo_path: str, short: bool = True, timeout: Timeout = None
    ) -> str:
        args = ["symbolic-ref", *(["--short"] if short else []), "HEAD"]
        return self._text(repo_path, args, timeout).strip()

    def status_porcelain(self, repo_path: str, timeout: Timeout = None) -> str:
        return self._text(repo_path, ["status", "--porcelain"], timeout)

    def diff_name_only(
        self, repo_path: str, spec: str, timeout: Timeout = None
    ) -> List[str]:
        return self._lines(
            self._text(repo_path, ["diff", "--name-only", spec], timeout)
        )

    def ls_files(self, repo_path: str, timeout: Timeout = None) -> List[str]:
        out = self._text(repo_path, ["ls-files"], timeout)
        return [line for line in out.splitlines() if line]

    def show_ref(
        self, repo_path: str, *args: str, timeout: Timeout = None
    ) -> "subprocess.CompletedProcess[Any]":
        return self._exec(repo_path, ["show-ref", *args], check=False, timeout=timeout)

    # -- history ---------------------------------------------------------------

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
        args = ["log", f"--format={fmt}", *(["--all"] if all_refs else [])]
        args += ["--since", since] if since else []
        args += [f"--skip={skip}"] if skip else []
        args += ["-n", str(max_count)] if max_count else []
        return self._bytes(repo_path, [*args, *revs], timeout)

    def for_each_ref(
        self,
        repo_path: str,
        fmt: str,
        patterns: Sequence[str] = (),
        *,
        sort: Optional[str] = None,
        count: Optional[int] = None,
        timeout: Timeout = None,
    ) -> bytes:
        args = ["for-each-ref", f"--format={fmt}"]
        args += [f"--sort={sort}"] if sort else []
        args += [f"--count={count}"] if count else []
        return self._bytes(repo_path, [*args, "--", *patterns], timeout)

    def diff_commits(
        self,
        repo_path: str,
        pairs: Sequence[Tuple[str, str]],
        context_lines: int,
        timeout: Timeout = None,
    ) -> List[bytes]:
        return [
            self._bytes(
                repo_path,
                ["diff", "-M", f"-U{context_lines}", "--full-index", base, commit],
                timeout,
            )
            for base, commit in pairs
        ]

    def show_objects(
        self,
        repo_path: str,
        specs: Sequence[Tuple[str, str]],
        timeout: Timeout = None,
    ) -> List["subprocess.CompletedProcess[Any]"]:
        return [
            self._exec(
                repo_path,
                ["show", f"{rev}:{path}"],
                check=False,
                timeout=timeout,
                text=False,
            )
            for rev, path in specs
        ]

    # -- mutation --------------------------------------------------------------

    def reset(
        self, repo_path: str, mode: str, target: str, timeout: Timeout = None
    ) -> None:
        self._exec(repo_path, ["reset", f"--{mode}", target], timeout=timeout)

    def restore(
        self, repo_path: str, pathspec: str = ".", timeout: Timeout = None
    ) -> None:
        self._exec(repo_path, ["restore", pathspec], timeout=timeout)

    def update_index_refresh(self, repo_path: str, timeout: Timeout = None) -> None:
        self._exec(repo_path, ["update-index", "--refresh"], check=False)

    def config_set(
        self, repo_path: str, key: str, value: str, timeout: Timeout = None
    ) -> None:
        self._exec(repo_path, ["config", key, value], timeout=timeout)

    # -- remotes ---------------------------------------------------------------

    def remote_list(
        self, repo_path: str, verbose: bool = False, timeout: Timeout = None
    ) -> str:
        return self._text(repo_path, ["remote", *(["-v"] if verbose else [])], timeout)

    def remote_get_url(self, repo_path: str, name: str, timeout: Timeout = None) -> str:
        return self._text(repo_path, ["remote", "get-url", name], timeout).strip()

    def remote_add(
        self, repo_path: str, name: str, url: str, timeout: Timeout = None
    ) -> None:
        self._exec(repo_path, ["remote", "add", name, url], timeout=timeout)

    def remote_set_url(
        self, repo_path: str, name: str, url: str, timeout: Timeout = None
    ) -> None:
        self._exec(repo_path, ["remote", "set-url", name, url], timeout=timeout)

    def remote_rename(
        self, repo_path: str, old: str, new: str, timeout: Timeout = None
    ) -> None:
        self._exec(repo_path, ["remote", "rename", old, new], timeout=timeout)

    def remote_remove(self, repo_path: str, name: str, timeout: Timeout = None) -> None:
        self._exec(repo_path, ["remote", "remove", name], timeout=timeout)


@dataclass(frozen=True)
class _Registration:
    name: str
    factory: Any

    def load(self) -> Any:
        return self.factory


def register_as_delegate(service: FakeGitMcp) -> Dict[str, List[str]]:
    """Register the fake as the ``delegated`` git invoker and git backend,
    exactly as an installed delegate distribution would be found. Returns
    what to pass to :func:`unregister_delegate`."""
    factories = {
        GIT_INVOKER_ENTRY_POINT_GROUP: lambda: FakeGitMcpInvoker(service),
        GIT_BACKEND_ENTRY_POINT_GROUP: lambda: FakeGitMcpGitBackend(service),
    }
    for group, factory in factories.items():
        add_entry_points(group, [_Registration(DELEGATED_MODE, factory)])
    return {group: [DELEGATED_MODE] for group in factories}


def unregister_delegate(registered: Dict[str, List[str]]) -> None:
    """Withdraw what :func:`register_as_delegate` added."""
    for group, names in registered.items():
        remove_entry_points(group, names)
