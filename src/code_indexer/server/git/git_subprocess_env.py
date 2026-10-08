"""
Shared environment builder for git subprocess calls.

Ensures SSH never prompts for interactive passwords, preventing server worker
threads from hanging indefinitely when key authentication fails against an
SSH remote (e.g. git@gitlab.com or git@github.com).

Every git clone/fetch/pull/push/ls-remote call that may contact an SSH remote
MUST pass env=build_non_interactive_git_env() to subprocess.run / subprocess.Popen.

See Bug: SSH password prompt hangs server thread.

Env-level protocol hardening here (e.g. GIT_PROTOCOL_FROM_USER=0 or
protocol.file.allow policy), beneath the argv validation in
git_argv_safety.py, was investigated and NOT implemented: this project
legitimately fetches/pulls over a local filesystem transport for real
repository-sync workflows (see CLAUDE.md "Golden Repo and Versioned
Snapshots"; this module is also used by the standalone CLI indexer, which
never contacts a remote at all).

Empirical proof (real throwaway repos, git 2.52): the protocol-policy env
settings that would restrict the local ('file') transport block an
ordinary local-path fetch/pull identically -- there is no env-level
policy that distinguishes a legitimate local-path operation from any
other value over that same transport. Enabling either setting would break
legitimate local-path sync with no corresponding benefit, since the argv
validation in git_argv_safety.py (remote must be one of the repository's
actually-configured remotes, and no remote/branch/revision value may
start with '-') already governs argv construction at a layer below the
transport, with zero risk to legitimate local-path operation.
"""

import logging
import os
import re
import subprocess
from typing import Dict, List, NamedTuple, Optional
from urllib.parse import unquote

from code_indexer.utils.git_remote_url import split_url_authority

logger = logging.getLogger(__name__)

# Repository credentials are supplied to git at run time and never stored in
# a clone's configuration nor placed on a command line. A remote URL given
# with userinfo (https://user:token@host/repo.git) is used by git in its
# credential-free form, and the credential reaches git only through the
# child process environment: a credential helper, scoped to the remote's
# scheme://host[:port] and configured through GIT_CONFIG_COUNT/KEY/VALUE,
# prints the username and password from the two variables below.
_USERNAME_VAR = "CIDX_GIT_REMOTE_USERNAME"
_PASSWORD_VAR = "CIDX_GIT_REMOTE_PASSWORD"
_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get || exit 0; '
    f'printf "username=%s\\n" "${_USERNAME_VAR}"; '
    f'test -z "${_PASSWORD_VAR}" || printf "password=%s\\n" "${_PASSWORD_VAR}"; '
    "}; f"
)
# Only the http(s) transports send URL userinfo to the server as
# credentials; the user part of an ssh URL is the ssh login name.
_HTTP_USERINFO_URL = re.compile(r"^(https?://)([^/?#]*)@([^/?#@]*)(.*)$", re.I | re.S)


class _HttpUserinfo(NamedTuple):
    clean_url: str
    scope: str
    username: str
    password: str


# A decoded credential value may not hold these: each would end a line of
# the git credential protocol.
_FORBIDDEN_CREDENTIAL_CHARS = ("\r", "\n", "\0")


def _split_http_userinfo(url: str) -> Optional[_HttpUserinfo]:
    """The credential-free URL, the credential scope (scheme://host[:port])
    and the percent-decoded username/password of an http(s) URL carrying
    userinfo (everything up to the LAST '@' of the authority); None for any
    other URL.

    Raises:
        ValueError: The decoded username or password contains CR, LF or
            NUL. The message never includes the value.
    """
    match = _HTTP_USERINFO_URL.match(url)
    if match is None:
        return None
    scheme, userinfo, host, rest = match.groups()
    raw_username, _, raw_password = userinfo.partition(":")
    username, password = unquote(raw_username), unquote(raw_password)
    if any(
        char in value
        for value in (username, password)
        for char in _FORBIDDEN_CREDENTIAL_CHARS
    ):
        raise ValueError(
            "Repository URL credentials contain a line break or NUL "
            "character; the URL is refused"
        )
    return _HttpUserinfo(
        clean_url=f"{scheme}{host}{rest}",
        scope=f"{scheme.lower()}{host}",
        username=username,
        password=password,
    )


_SSH_SCHEMES = ("ssh", "git+ssh", "ssh+git")
# An ssh login name kept in a credential-free URL.
_PLAIN_SSH_LOGIN = re.compile(r"[A-Za-z0-9._~-]+")


def remote_url_without_credentials(url: str) -> str:
    """``url`` without credentials -- the form git is given on a command
    line and the form stored in a clone's configuration. Userinfo runs to
    the authority's last '@' (``split_url_authority``). http(s) and other
    schemes lose all of it; an ssh-family URL keeps only a plain login name
    (needed, not secret) and never a password. A URL without userinfo
    (including scp-like and local paths) is returned unchanged.

    Raises:
        ValueError: An http(s) username or password decodes to CR, LF or
            NUL. The message never includes the value.
    """
    split = split_url_authority(url)
    if split is None or split.userinfo is None:
        return url
    scheme = split.scheme.lower()
    login = ""
    if scheme in ("http", "https"):
        username, _, password = split.userinfo.partition(":")
        for value in (unquote(username), unquote(password)):
            if any(char in value for char in _FORBIDDEN_CREDENTIAL_CHARS):
                raise ValueError(
                    "Repository URL credentials contain a line break or NUL "
                    "character; the URL is refused"
                )
    elif scheme in _SSH_SCHEMES:
        # The login is the raw text before the first literal ':', decoded
        # only afterwards, and kept only when it is a plain name.
        name = unquote(split.userinfo.partition(":")[0])
        if _PLAIN_SSH_LOGIN.fullmatch(name):
            login = f"{name}@"
    return f"{split.scheme}://{login}{split.hostport}{split.rest}"


def supply_remote_credentials(
    env: Dict[str, str], credentials_url: Optional[str]
) -> None:
    """Add to ``env`` (in place) the run-time credential configuration for
    ``credentials_url``, the repository URL as registered. A no-op when it
    carries no http(s) userinfo. Entries are appended after any
    GIT_CONFIG_* entries ``env`` already holds; the helper list for the
    remote's scope is reset first so only this credential is offered."""
    parts = _split_http_userinfo(credentials_url) if credentials_url else None
    if parts is None:
        return
    count = int(env.get("GIT_CONFIG_COUNT") or 0)
    key = f"credential.{parts.scope}.helper"
    env[f"GIT_CONFIG_KEY_{count}"] = key
    env[f"GIT_CONFIG_VALUE_{count}"] = ""
    env[f"GIT_CONFIG_KEY_{count + 1}"] = key
    env[f"GIT_CONFIG_VALUE_{count + 1}"] = _CREDENTIAL_HELPER
    env["GIT_CONFIG_COUNT"] = str(count + 2)
    env[_USERNAME_VAR] = parts.username
    env[_PASSWORD_VAR] = parts.password


_LOCAL_GIT_TIMEOUT_SECONDS = 30


def _run_local_git(
    repo_path: str, args: list
) -> Optional["subprocess.CompletedProcess[str]"]:
    """Run a local (no network) git command in ``repo_path``; None, with a
    WARNING, when it times out or cannot be started."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=_LOCAL_GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(
            "git %s did not complete in %s: %s", args[0], repo_path, type(exc).__name__
        )
        return None


class RemoteUrlSanitization(NamedTuple):
    """Outcome of ``ensure_remote_url_without_credentials``: how many stored
    remote URL keys were rewritten, and how many reads/rewrites failed."""

    rewritten: int
    failed: int

    @property
    def ok(self) -> bool:
        return self.failed == 0


# Every stored remote URL and push URL, e.g. remote.origin.url.
_REMOTE_URL_KEYS = r"^remote\..*\.(url|pushurl)$"


def _strip_http_userinfo(url: str) -> Optional[str]:
    """``url`` without its http(s) userinfo, or None when it carries none.
    Pure text surgery (nothing is decoded), so any stored value -- however
    malformed its userinfo -- can be sanitized."""
    match = _HTTP_USERINFO_URL.match(url)
    if match is None:
        return None
    scheme, _userinfo, host, rest = match.groups()
    return f"{scheme}{host}{rest}"


def _stored_remote_urls(repo_path: str) -> Optional[Dict[str, List[str]]]:
    """Every stored ``remote.*.url`` / ``remote.*.pushurl`` value, grouped by
    key in stored order; None (with a WARNING naming no URL) when they
    cannot be read."""
    listed = _run_local_git(repo_path, ["config", "--get-regexp", _REMOTE_URL_KEYS])
    if listed is None:
        return None
    if listed.returncode == 1:  # no such key stored
        return {}
    if listed.returncode != 0:
        logger.warning(
            "Could not read the stored remote URLs of %s (git exit %d)",
            repo_path,
            listed.returncode,
        )
        return None
    values: Dict[str, List[str]] = {}
    for line in listed.stdout.splitlines():
        key, _, value = line.partition(" ")
        values.setdefault(key, []).append(value)
    return values


def ensure_remote_url_without_credentials(repo_path: str) -> RemoteUrlSanitization:
    """Rewrite every stored remote URL and push URL of the clone at
    ``repo_path`` that still carries http(s) userinfo to its credential-free
    form. Repository credentials are supplied to git at run time and never
    stored in a clone's configuration. Idempotent. The old value is never
    placed on a command line, and no log line names a URL. Local git calls
    only -- run it from a worker/job thread, never on the event loop.

    A versioned snapshot (``.versioned/<alias>/v_<ts>``) is NEVER modified:
    snapshots are immutable once published and are not used for network
    operations; the base clone they are taken from is the one rewritten."""
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )

    if is_versioned_snapshot(repo_path):
        return RemoteUrlSanitization(rewritten=0, failed=0)
    stored = _stored_remote_urls(repo_path)
    if stored is None:
        return RemoteUrlSanitization(rewritten=0, failed=1)
    rewritten = failed = 0
    for key, values in stored.items():
        stripped = [_strip_http_userinfo(value) for value in values]
        if all(clean is None for clean in stripped):
            continue
        cleaned = [
            value if clean is None else clean for value, clean in zip(values, stripped)
        ]
        commands = [["config", "--replace-all", key, cleaned[0]]]
        commands += [["config", "--add", key, value] for value in cleaned[1:]]
        for command in commands:
            result = _run_local_git(repo_path, command)
            if result is None or result.returncode != 0:
                logger.warning(
                    "Could not store the credential-free %s for %s (git exit %s)",
                    key,
                    repo_path,
                    "none" if result is None else result.returncode,
                )
                failed += 1
                break
        else:
            rewritten += 1
            logger.info(
                "Stored the credential-free %s for %s; repository credentials "
                "are supplied to git at run time",
                key,
                repo_path,
            )
    return RemoteUrlSanitization(rewritten=rewritten, failed=failed)


def build_non_interactive_git_env(
    credentials_url: Optional[str] = None,
) -> Dict[str, str]:
    """Return a copy of the current environment augmented for non-interactive git SSH.

    The returned dict:
    - Inherits all variables from os.environ (PATH, HOME, SSH_AUTH_SOCK, etc.)
    - Sets GIT_SSH_COMMAND with BatchMode=yes and fail-fast SSH options so that
      SSH exits immediately with an error instead of prompting for a password or
      blocking on a tty when key authentication fails.
    - Sets GIT_TERMINAL_PROMPT=0 to disable git's own HTTP credential prompt.
    - Defaults GIT_EDITOR and GIT_SEQUENCE_EDITOR to a non-interactive no-op
      ("true") via setdefault, so a git operation that must finalize an
      automatic commit message (e.g. `rebase --continue`, or a merge run
      with both stdin and stdout attached to a real tty) never blocks
      waiting on an interactive editor or fails with "Terminal is dumb,
      but EDITOR unset" (Bug #1578). setdefault is used rather than a hard
      overwrite so a caller that has deliberately configured its own editor
      (inherited via os.environ, or merged in by the caller afterward) is
      not silently clobbered.

    Callers receive a fresh dict each time; os.environ is never mutated.

    ``credentials_url``: the repository URL as registered. When it carries
    http(s) userinfo the credential is supplied at run time
    (``supply_remote_credentials``); the command itself must then name the
    remote by ``remote_url_without_credentials(...)`` or by a remote whose
    stored URL is credential-free.
    """
    env: Dict[str, str] = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (
        "ssh"
        " -o BatchMode=yes"
        " -o ConnectTimeout=10"
        " -o StrictHostKeyChecking=accept-new"
        " -o PasswordAuthentication=no"
        " -o KbdInteractiveAuthentication=no"
        " -o PubkeyAuthentication=yes"
    )
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_EDITOR", "true")
    env.setdefault("GIT_SEQUENCE_EDITOR", "true")
    supply_remote_credentials(env, credentials_url)
    return env
