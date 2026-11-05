"""The git seam: cidx's internal git operations, and its git tools.

Entry-point group ``code_indexer.git_backend``. ``local`` runs git in this
process (``LocalGitBackend``); ``delegated`` runs it in git-mcp (the
``cidx_git_backend`` plugin). :class:`GitCommandRunner` is the extension a
backend implements to also carry the user-facing git TOOLS.

Moved from ``code_indexer.server.storage.shared.git_backend``, which
re-exports both protocols.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import (
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

Timeout = Optional[float]

#: The configured values ``git_backend`` may take.
VALID_GIT_BACKENDS = ("local", "delegated")

#: What git prints when HEAD is not a branch: ``fatal: ref HEAD is not a
#: symbolic ref``. Part of the contract: ``symbolic_ref`` on a detached HEAD
#: raises ``CalledProcessError`` whose ``stderr`` carries this text, through
#: every backend (git-mcp surfaces git's stderr verbatim), so callers tell a
#: detached HEAD from a failure.
DETACHED_HEAD_MARKER = "is not a symbolic ref"


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
    def ls_remote(
        self,
        url: str,
        timeout: Timeout = None,
        credentials_url: Optional[str] = None,
    ) -> bool:
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
        credentials_url: Optional[str] = None,
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
        credentials_url: Optional[str] = None,
        ref: Optional[str] = None,
    ) -> None:
        """``git [-c core.fsync=none] fetch <remote>`` (``retry`` opt-in).

        ``ref`` (a branch name) fetches ONLY that branch, into
        ``refs/remotes/<remote>/<ref>`` (git-mcp ``git_fetch`` ``ref``), never
        the clone's configured refspec -- which, on a single-branch clone,
        names a branch upstream may have deleted."""
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
        credentials_url: Optional[str] = None,
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
        """``git for-each-ref --format=<fmt> [--sort=S] [--count=N] -- <patterns>``.

        Every matching ref with its fields in ONE call (e.g. all branch tips).
        Patterns are ref prefixes/globs, never options (they follow ``--``).
        """
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


@runtime_checkable
class GitCommandRunner(Protocol):
    """A git backend that also runs cidx's git TOOLS (``GitOperationsService``).

    Separate from :class:`GitBackend` on purpose: GitBackend is checked with
    ``isinstance`` when the server parks it, and a plugin built before these
    methods existed must keep satisfying it. A caller that needs to run the
    git tools through the seam checks THIS protocol and fails loud without it.
    """

    def run(
        self,
        repo_path: str,
        args: Sequence[str],
        timeout: Timeout = None,
        identity: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        """``git <args>`` in the repository -> CompletedProcess with TEXT
        stdout/stderr; a non-zero exit is returned, never raised. Local
        subcommands only: network operations use push/pull/fetch. *identity*
        (author_name/author_email/committer_name/committer_email) attributes a
        commit through git's GIT_AUTHOR_* / GIT_COMMITTER_* variables."""
        ...  # pragma: no cover

    def push(
        self,
        repo_path: str,
        remote: str,
        branch: Optional[str] = None,
        timeout: Timeout = None,
    ) -> subprocess.CompletedProcess:
        """``git push <remote> [<branch>]`` with the backend's own credentials;
        raises ``CalledProcessError`` on failure."""
        ...  # pragma: no cover


@runtime_checkable
class GitProcessRunner(Protocol):
    """Runs one git argv on this machine: what the LOCAL git backend is given.

    The local backend (``cidx-backend-git``) builds argvs; it does not decide
    HOW git is spawned. The core owns that -- the non-interactive SSH
    environment, ``safe.directory``, a network operation's credential (from
    ``credentials_url``, through the environment, never on argv), the owning
    job's cancellation scope, retries of transient failures, and the git
    invoker routing -- and passes its runner to the backend's factory.

    The arguments mirror the core's ``run_git_command``: ``check`` raises
    ``CalledProcessError`` on a non-zero exit; ``text=False`` keeps both streams
    as bytes; ``retry`` retries transient failures; ``env`` adds variables (a
    commit identity) on top of the runner's own environment.
    """

    def run_git(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        check: bool = True,
        timeout: Timeout = None,
        retry: bool = False,
        text: bool = True,
        credentials_url: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        """Run *cmd* (``["git", ...]``) in *cwd*."""
        ...  # pragma: no cover


# A URL of the form <scheme>://<userinfo>@<host>... -- the userinfo half is where
# a token lands when a repo URL carries one (https://oauth2:<token>@host/...).
# The scp-like ``git@host:path`` form has no scheme and no secret, so it is left
# alone. The userinfo runs to the LAST '@' before the host and never crosses
# '/', '?', '#' or whitespace, so a password holding '@' is masked whole.
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<userinfo>[^/?#\s]+)@"
)


def redact_url_credentials(text: object) -> str:
    """Replace the userinfo of any ``scheme://user:secret@host`` URL with ``***``.

    git echoes a credentialed remote URL back on failure, and a failure's
    message travels into job errors, API responses and logs, so every stream
    of a failed git command is passed through this before it is reported.

    Accepts anything an f-string accepts: captured streams are not reliably
    ``str`` (bytes without ``text=True``, None when not captured, test
    doubles), and raising here would turn building a diagnostic into a crash
    on exactly the error path. Rendering matches f-string output.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    elif not isinstance(text, str):
        text = f"{text}"
    return _URL_USERINFO_RE.sub(lambda m: f"{m.group('scheme')}***@", text)
