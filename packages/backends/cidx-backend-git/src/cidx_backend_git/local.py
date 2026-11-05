"""The local git backend: cidx's internal git operations, run in this process.

The ``local`` mode of the git seam (``cidx_sdk.seams.git.GitBackend`` and
``GitCommandRunner``), registered on ``code_indexer.git_backend`` under the
entry name ``local``; the delegated mode runs the same operations in git-mcp.

This module builds argvs and nothing else. How git is spawned -- the
non-interactive SSH environment, ``safe.directory``, a network operation's
credential (``credentials_url``, through the environment, never on argv),
the owning job's cancellation scope (Bug #2012), retries and the git invoker
routing -- is the core's, handed to :func:`make_git_backend` as a
:class:`~cidx_sdk.seams.git.GitProcessRunner`. Every method takes a timeout
(``None``: no timeout) and, unless documented otherwise, raises
``subprocess.CalledProcessError`` on a non-zero exit.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from cidx_sdk.seams.git import GitProcessRunner, Timeout, redact_url_credentials

_URL_USERINFO_RE_BYTES = re.compile(
    rb"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/@\s]+)@"
)


def _redact_bytes(data: bytes) -> bytes:
    return _URL_USERINFO_RE_BYTES.sub(lambda m: m.group("scheme") + b"***@", data)


#: GitCommandRunner identity fields -> git's commit-identity variables.
_IDENTITY_ENV_NAMES = {
    "author_name": "GIT_AUTHOR_NAME",
    "author_email": "GIT_AUTHOR_EMAIL",
    "committer_name": "GIT_COMMITTER_NAME",
    "committer_email": "GIT_COMMITTER_EMAIL",
}


class LocalGitBackend:
    """Runs git in this process (the ``local`` mode of the git seam).

    Every method builds the ``git`` argv the core's call sites have always run
    and hands it to the injected :class:`GitProcessRunner`, which owns the
    environment (SSH-safe, ``safe.directory``, credentials), cancellation and
    retries; network-touching operations ask it to retry when the caller does.
    """

    def __init__(self, runner: GitProcessRunner) -> None:
        if not isinstance(runner, GitProcessRunner):
            raise TypeError(
                "LocalGitBackend needs a GitProcessRunner (the core's git "
                f"funnel), got {type(runner).__name__}"
            )
        self._runner = runner

    @property
    def runner(self) -> GitProcessRunner:
        """The process runner every git command goes through."""
        return self._runner

    # -- capabilities ---------------------------------------------------------
    @property
    def owns_credentials(self) -> bool:
        """False: local git relies on the credential in the URL / git config, so
        callers must keep any embedded token. See ``GitBackend.owns_credentials``."""
        return False

    # -- single funnel: every git invocation goes through here -----------------
    def _run(
        self,
        cmd: Sequence[str],
        cwd: Path,
        *,
        check: bool = True,
        timeout: Timeout = None,
        retry: bool = False,
        text: bool = True,
        credentials_url: Optional[str] = None,
    ) -> subprocess.CompletedProcess:
        return self._runner.run_git(
            list(cmd),
            cwd=cwd,
            check=check,
            timeout=timeout,
            retry=retry,
            text=text,
            credentials_url=credentials_url,
        )

    def _run_bytes(
        self,
        cmd: Sequence[str],
        cwd: Path,
        *,
        check: bool = True,
        timeout: Timeout = None,
    ) -> subprocess.CompletedProcess:
        """Run git keeping stdout as BYTES; stderr is decoded for messages."""
        result = self._run(cmd, cwd, check=False, timeout=timeout, text=False)
        stderr = bytes(result.stderr or b"").decode("utf-8", "replace")
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                list(cmd),
                output=_redact_bytes(result.stdout),
                stderr=redact_url_credentials(stderr),
            )
        return subprocess.CompletedProcess(
            list(cmd), result.returncode, stdout=result.stdout, stderr=stderr
        )

    @staticmethod
    def _lines(stdout: object) -> List[str]:
        return [line.strip() for line in str(stdout).splitlines() if line.strip()]

    # -- remote-URL operations ------------------------------------------------
    def ls_remote(
        self,
        url: str,
        timeout: Timeout = None,
        credentials_url: Optional[str] = None,
    ) -> bool:
        try:
            # ls-remote needs no repo cwd; run from the current directory.
            self._run(
                ["git", "ls-remote", url],
                Path.cwd(),
                timeout=timeout,
                credentials_url=credentials_url,
            )
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
        credentials_url: Optional[str] = None,
    ) -> None:
        cmd = ["git"]
        if no_fsync:
            cmd += ["-c", "core.fsync=none"]
        cmd += ["clone"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [url, dest_path]
        # The clone target does not exist yet — run from its parent.
        self._run(
            cmd,
            Path(dest_path).parent,
            timeout=timeout,
            retry=retry,
            credentials_url=credentials_url,
        )

    # -- fetch / pull / merge -------------------------------------------------
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
        cmd = ["git"]
        if no_fsync:
            cmd += ["-c", "core.fsync=none"]
        cmd += ["fetch", remote]
        if ref:
            # The same refspec git-mcp's git_fetch builds for its ``ref``.
            cmd.append(f"+{ref}:refs/remotes/{remote}/{ref}")
        self._run(
            cmd,
            Path(repo_path),
            timeout=timeout,
            retry=retry,
            credentials_url=credentials_url,
        )

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
        self._run(
            cmd,
            Path(repo_path),
            timeout=timeout,
            retry=retry,
            credentials_url=credentials_url,
        )

    def merge(self, repo_path: str, ref: str, timeout: Timeout = None) -> None:
        self._run(["git", "merge", ref], Path(repo_path), timeout=timeout)

    # -- the git tools' runner (GitCommandRunner) ------------------------------
    def run(
        self,
        repo_path: str,
        args: Sequence[str],
        timeout: Timeout = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        env = {
            name: identity[field]
            for field, name in _IDENTITY_ENV_NAMES.items()
            if identity and identity.get(field)
        }
        return self._runner.run_git(
            ["git", *args],
            cwd=Path(repo_path),
            check=False,
            timeout=timeout,
            env=env or None,
        )

    def push(
        self,
        repo_path: str,
        remote: str,
        branch: Optional[str] = None,
        timeout: Timeout = None,
    ) -> subprocess.CompletedProcess:
        cmd = ["git", "push", "--end-of-options", remote]
        if branch:
            cmd.append(branch)
        return self._run(cmd, Path(repo_path), timeout=timeout)

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
        # The argv git-mcp's git_for_each_ref runs: options, then ``--`` patterns.
        cmd = ["git", "for-each-ref", f"--format={fmt}"]
        if sort:
            cmd.append(f"--sort={sort}")
        if count:
            cmd.append(f"--count={count}")
        cmd.append("--")
        cmd.extend(patterns)
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


def make_git_backend(runner: GitProcessRunner) -> LocalGitBackend:
    """Entry point ``local`` on ``code_indexer.git_backend``.

    Unlike a delegate's zero-argument factory, the local backend takes the
    core's git process runner: running git here means running it the way the
    core runs every git command.
    """
    return LocalGitBackend(runner)
