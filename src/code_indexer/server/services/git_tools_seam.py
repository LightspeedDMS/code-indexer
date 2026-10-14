"""
cidx's git TOOLS on a working copy that is not on this pod.

``GitOperationsService`` (git_status, git_diff, git_log, git_stage, git_commit,
git_push, git_branch_*, git_stash_*, conflicts, ...) runs git in a user's
activated working copy and parses git's own output. Under
``content_backend='delegated'`` that copy exists only on the shared volume, so
the git runs in git-mcp, addressed by the copy's path (``cow_path``):

* local subcommands -> ``GitCommandRunner.run`` (git-mcp ``git_exec``): git's
  raw stdout, exit code and stderr, so the service's parsing is unchanged;
  commit attribution (GIT_AUTHOR_* / GIT_COMMITTER_*) travels as an identity;
* ``fetch`` / ``pull`` / ``push`` -> the backend's structured network
  operations, which carry git-mcp's credentials (cidx holds none);
* working-tree file reads (conflict markers) -> the content seam.

Under the local default :func:`delegated_git_tools` is None and the service
runs exactly the code it always has. A delegated server whose git backend
cannot run the tools refuses (:class:`GitToolsUnavailableError`) -- it never
runs git on this pod, where the copy does not exist.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from code_indexer.server.storage.shared.git_backend import GitCommandRunner

#: git's commit-identity variables -> git-mcp ``git_exec`` identity fields.
_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "author_name",
    "GIT_AUTHOR_EMAIL": "author_email",
    "GIT_COMMITTER_NAME": "committer_name",
    "GIT_COMMITTER_EMAIL": "committer_email",
}

#: The network subcommands, which run as the backend's structured operations.
_NETWORK = ("fetch", "pull", "push")
_END_OF_OPTIONS = "--end-of-options"

PathLike = Union[str, Path]


class GitToolsUnavailableError(RuntimeError):
    """The repository is not on this pod and the git seam cannot run the tools."""


class DelegatedGitTools:
    """The git tools' git, through the git backend (git-mcp) and the content seam."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    # -- git ----------------------------------------------------------------

    def run(
        self,
        cmd: Sequence[str],
        cwd: PathLike,
        *,
        check: bool = True,
        timeout: Optional[float] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "subprocess.CompletedProcess[str]":
        """``run_git_command`` semantics: CompletedProcess, CalledProcessError
        when *check* and git failed."""
        if not cmd or cmd[0] != "git" or len(cmd) < 2:
            raise ValueError("Command must start with 'git' and name a subcommand")
        if cmd[1] in _NETWORK:
            return self._network(list(cmd), str(cwd), check=check, timeout=timeout)
        result: "subprocess.CompletedProcess[str]" = self._backend.run(
            str(cwd), list(cmd[1:]), timeout=timeout, identity=_identity(env)
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, list(cmd), output=result.stdout, stderr=result.stderr
            )
        return result

    def _network(
        self, cmd: List[str], repo: str, *, check: bool, timeout: Optional[float]
    ) -> "subprocess.CompletedProcess[str]":
        verb = cmd[1]
        set_upstream, positionals = _network_operands(verb, cmd[2:])
        remote = positionals[0] if positionals else "origin"
        branch = positionals[1] if len(positionals) > 1 else None
        stdout = ""
        try:
            if verb == "push":
                pushed: "subprocess.CompletedProcess[str]" = self._backend.push(
                    repo, remote, branch, timeout=timeout, set_upstream=set_upstream
                )
                stdout = pushed.stdout or ""
            elif verb == "pull":
                before = self._head(repo, timeout)
                self._backend.pull(repo, remote=remote, branch=branch, timeout=timeout)
                try:
                    stdout = self._changed_summary(repo, before, timeout)
                except subprocess.CalledProcessError:
                    pass
            else:
                self._backend.fetch(repo, remote, timeout=timeout)
        except subprocess.CalledProcessError as e:
            if check:
                raise
            stderr = str(e.stderr or "")
            # git's conflicts are on stdout; a backend that only had them as
            # error text still reports them where the callers read them.
            out = str(e.output or "") or _conflict_lines(stderr)
            return subprocess.CompletedProcess(cmd, e.returncode or 1, out, stderr)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    def _head(self, repo: str, timeout: Optional[float]) -> Optional[str]:
        """HEAD's commit, or None (an unborn branch has none)."""
        head = self._backend.run(
            repo, ["rev-parse", "-q", "--verify", "HEAD"], timeout=timeout
        )
        if head.returncode != 0:
            return None
        return head.stdout.strip() or None

    def _changed_summary(
        self, repo: str, before: Optional[str], timeout: Optional[float]
    ) -> str:
        """What a local pull prints about the files it changed (git's
        ``N files changed`` diffstat summary), read from the copy: git-mcp's
        ``git_pull`` reports a status, not git's output."""
        if before is None:
            return ""
        stat = self._backend.run(
            repo, ["diff", "--shortstat", before, "HEAD"], timeout=timeout
        )
        return stat.stdout if stat.returncode == 0 else ""

    def merge_in_progress(self, repo_path: PathLike) -> bool:
        """Whether a merge is in progress (``.git/MERGE_HEAD`` exists)."""
        result = self.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
            repo_path,
            check=False,
        )
        return result.returncode == 0

    # -- working-tree files -------------------------------------------------

    @staticmethod
    def read_worktree_text(repo_path: PathLike, file_path: str) -> Optional[str]:
        """A working-tree file's text through the content seam; None if absent.

        Confinement is lexical (no local tree to resolve against): a path that
        leaves the repository raises ValueError, as the local resolver does.
        Undecodable bytes raise UnicodeDecodeError (callers treat as binary);
        an unreadable file raises OSError.
        """
        from code_indexer.server.services.file_service import (
            content_seam_source,
            fetch_one,
        )
        from code_indexer.server.storage.shared.content_backend import (
            UNREADABLE_FETCH_ERRORS,
        )
        from code_indexer.utils.path_confinement import confine_relative_path

        try:
            rel = confine_relative_path(file_path)
        except PermissionError as e:
            raise ValueError(
                f"File path escapes repository boundary: {file_path}"
            ) from e
        if not rel:
            raise ValueError(f"File path escapes repository boundary: {file_path}")
        source = content_seam_source(str(repo_path))
        if source is None:
            raise GitToolsUnavailableError("content backend is not delegated")
        fetched = fetch_one(source, rel)
        if fetched.error is None:
            return bytes(fetched.data).decode("utf-8", errors="strict")
        if fetched.error in UNREADABLE_FETCH_ERRORS:
            raise OSError(f"Cannot read '{file_path}': {fetched.error}")
        return None


_SET_UPSTREAM = ("-u", "--set-upstream")


def _network_operands(verb: str, args: Sequence[str]) -> Tuple[bool, List[str]]:
    """(set_upstream, positionals) of a network command's arguments.

    The structured operations take a remote and a branch; the one option that
    crosses is push's ``--set-upstream`` (before ``--end-of-options``). Any
    other option is refused rather than silently dropped."""
    set_upstream = False
    positionals: List[str] = []
    options_ended = False
    for arg in args:
        if not options_ended and arg == _END_OF_OPTIONS:
            options_ended = True
        elif not options_ended and verb == "push" and arg in _SET_UPSTREAM:
            set_upstream = True
        elif arg.startswith("-"):
            raise GitToolsUnavailableError(
                f"git {verb} with options cannot run through the git seam: {list(args)}"
            )
        else:
            positionals.append(arg)
    return set_upstream, positionals


def _conflict_lines(text: str) -> str:
    """git's ``CONFLICT ...`` lines in *text* (empty when there are none)."""
    lines = [line for line in text.splitlines() if line.startswith("CONFLICT")]
    return "".join(line + "\n" for line in lines)


def _identity(env: Optional[Mapping[str, str]]) -> Optional[Dict[str, str]]:
    """The commit identity a caller's env carries. Any OTHER git variable the
    caller SET cannot cross the seam, so it is refused rather than silently
    dropped; one merely inherited from os.environ is this process's own
    environment (git-mcp has its own) and is not the caller's request."""
    if not env:
        return None
    identity = {
        field: env[name] for name, field in _IDENTITY_ENV.items() if env.get(name)
    }
    unsupported = sorted(
        k
        for k in env
        if k.startswith("GIT_")
        and k not in _IDENTITY_ENV
        and env.get(k)
        and env.get(k) != os.environ.get(k)
    )
    if unsupported:
        raise GitToolsUnavailableError(
            f"git environment {unsupported} cannot run through the git seam"
        )
    return identity or None


def delegated_git_tools() -> Optional[DelegatedGitTools]:
    """The delegated git tools, or None to run git on this pod (the default).

    None unless the running server's content backend is delegated. Then the
    git backend must be the delegated one AND run the tools; otherwise this
    refuses -- the repository is not on this pod.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )
    from code_indexer.server.startup.git_backend_wiring import (
        server_delegated_git_backend,
    )

    if not is_content_seam_delegated():
        return None
    backend = server_delegated_git_backend()
    if backend is None or not isinstance(backend, GitCommandRunner):
        raise GitToolsUnavailableError(
            "Repositories are not on this server (content_backend='delegated') "
            "and the git backend cannot run cidx's git tools: set "
            "git_backend='delegated' with a git-mcp plugin that provides run/push "
            "(git-mcp git_exec)."
        )
    return DelegatedGitTools(backend)
