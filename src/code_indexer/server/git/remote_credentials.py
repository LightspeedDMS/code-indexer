"""Which credential a git network command on a named remote may carry.

A credential applies only where it is scoped. git itself resolves where the
command really goes (url/pushurl, insteadOf/pushInsteadOf and any run-time
rewrite), and the credential -- with its protocol restriction -- is
supplied only when every destination is the credential's
``scheme://host[:port]``. Any other remote runs with no credential at all.
Push, pull and fetch all decide through ``credentials_for_remote``.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional, Sequence, Tuple

from code_indexer.server.git.git_subprocess_env import credential_scope
from code_indexer.utils import git_runner
from code_indexer.utils.git_runner import run_git_command


class RemoteCredentials(NamedTuple):
    """The credential a command on the remote may carry (None: none), and
    whether some -- but not all -- of the remote's destinations are the
    credential's scope."""

    credentials_url: Optional[str]
    mixed: bool


def credentials_for_remote(
    repo_path: Path,
    remote: str,
    credentials_url: Optional[str],
    *,
    push: bool,
    run_time_config: Sequence[Tuple[str, str]] = (),
) -> RemoteCredentials:
    """The credential a git network command on the configured ``remote``
    may carry: ``credentials_url`` when every destination git resolves for
    it (``push``: all push URLs; otherwise the URL fetch uses) is that
    credential's scope, else None. Local git only, bounded by
    git_runner.REMOTE_RESOLVE_TIMEOUT_SECONDS: a remote git cannot resolve
    in time fails (subprocess.TimeoutExpired), never sending the
    credential."""
    scope = credential_scope(credentials_url)
    if scope is None:
        return RemoteCredentials(None, False)
    command = ["git", "remote", "get-url"]
    command += ["--push", "--all", remote] if push else [remote]
    destinations = run_git_command(
        command,
        cwd=repo_path,
        check=True,
        timeout=git_runner.REMOTE_RESOLVE_TIMEOUT_SECONDS,
        run_time_config=run_time_config,
    ).stdout.splitlines()
    scopes = [credential_scope(url.strip()) for url in destinations]
    if scopes and all(found == scope for found in scopes):
        return RemoteCredentials(credentials_url, False)
    return RemoteCredentials(None, scope in scopes)
