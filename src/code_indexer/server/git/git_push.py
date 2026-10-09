"""The single git push implementation of the server.

Every push the server performs -- REST and MCP ``git_push``
(GitOperationsService) and the SCIP self-heal PR flow (GitStateManager) --
runs ``push``. Remote and refspec are validated (git_argv_safety) before any
network subprocess; the argv carries a hard option boundary; credentials
reach git only at run time through the environment, never on argv nor
stored in the clone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from code_indexer.server.git.git_subprocess_env import (
    credential_scope,
    remote_url_without_credentials,
)
from code_indexer.server.git.remote_credentials import credentials_for_remote
from code_indexer.server.services.git_argv_safety import (
    GitArgumentValidationError,
    validate_branch_name,
    validate_remote_name,
)
from code_indexer.utils import git_runner
from code_indexer.utils.git_runner import run_git_command


class PushCredentialsNotApplicableError(Exception):
    """The push would not travel over the URL its credential applies to."""


def push(
    repo_path: Path,
    remote: str,
    refspec: Optional[str],
    *,
    set_upstream: bool,
    credentials_url: Optional[str],
    push_url: Optional[str] = None,
    timeout: Optional[float] = None,
) -> "subprocess.CompletedProcess[str]":
    """Push ``refspec`` (a branch name, "src:dst", "+main"; None: git's
    push.default) to the configured remote ``remote``.

    Args:
        set_upstream: push with git's --set-upstream, so each pushed local
            branch tracks its remote counterpart.
        credentials_url: URL whose http(s) userinfo is the credential,
            supplied at run time (None: the server's own git authentication).
        push_url: credential-free URL the push travels over instead of the
            remote's own URL; the remote keeps its name, so tracking refers
            to it. Applied for this run only, never stored.
        timeout: seconds for the push subprocess (None: no limit).

    Returns:
        The completed ``git push``.

    Raises:
        GitArgumentValidationError: remote/refspec fail validation, or
            set_upstream with no refspec on a branch without an upstream.
        PushCredentialsNotApplicableError: ``push_url`` is given but the
            remote has an explicit pushurl elsewhere, which git would use
            as configured (so the credential would not apply).
        subprocess.CalledProcessError: git push failed.
        subprocess.TimeoutExpired: git push exceeded ``timeout``.
    """
    # remote must be a configured remote name; the refspec is ONE argv
    # element, so only the whole element is checked and its '+'/'src:dst'
    # contents reach git unchanged. See git_argv_safety module docstring.
    remote = validate_remote_name(remote, repo_path)
    refspec = validate_branch_name(refspec, param_name="branch")

    # With no refspec, --set-upstream can only re-state the current branch's
    # existing upstream; without one there is nothing to track, so the push
    # is refused before it starts.
    if set_upstream and not refspec:
        on_branch = run_git_command(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=repo_path,
            check=False,
            timeout=git_runner.REMOTE_RESOLVE_TIMEOUT_SECONDS,
        )
        if on_branch.returncode != 0:
            raise GitArgumentValidationError(
                "set_upstream needs a branch: there is no current branch "
                "(detached HEAD); name the branch to push"
            )
        upstream = run_git_command(
            ["git", "rev-parse", "--abbrev-ref", "@{u}"],
            cwd=repo_path,
            check=False,
            timeout=git_runner.REMOTE_RESOLVE_TIMEOUT_SECONDS,
        )
        if upstream.returncode != 0:
            raise GitArgumentValidationError(
                "set_upstream needs a branch: the current branch has no "
                "upstream to track. Name the branch to push, or send "
                "set_upstream=false."
            )

    # The push may travel over push_url while the remote keeps its name;
    # the rewrite exists for this run only.
    run_time_config: List[Tuple[str, str]] = []
    if push_url:
        # The rewrite never carries credentials, whatever the stored URL holds.
        remote_url = remote_url_without_credentials(
            run_git_command(
                ["git", "remote", "get-url", remote],
                cwd=repo_path,
                check=True,
                timeout=git_runner.REMOTE_RESOLVE_TIMEOUT_SECONDS,
            ).stdout.strip()
        )
        if remote_url != push_url:
            run_time_config.append((f"url.{push_url}.pushInsteadOf", remote_url))

    # A credential goes only where it is scoped (credentials_for_remote, the
    # one decision shared with pull and fetch). A credential meant for this
    # remote (push_url) or a remote only partly at its scope is refused;
    # any other remote is pushed without the credential.
    decision = credentials_for_remote(
        repo_path, remote, credentials_url, push=True, run_time_config=run_time_config
    )
    if (
        credential_scope(credentials_url) is not None
        and decision.credentials_url is None
        and (push_url or decision.mixed)
    ):
        raise PushCredentialsNotApplicableError(
            f"Remote '{remote}' does not push to the credential's host "
            "(an explicit pushurl or a URL rewrite redirects it); "
            "cannot apply credentials to it"
        )
    credentials_url = decision.credentials_url

    # Hard option boundary right after the subcommand and its fixed flags.
    cmd = ["git", "push"]
    if set_upstream:
        cmd.append("--set-upstream")
    cmd += ["--end-of-options", remote]
    if refspec:
        cmd.append(refspec)

    return run_git_command(
        cmd,
        cwd=repo_path,
        timeout=timeout,
        check=True,
        credentials_url=credentials_url,
        run_time_config=run_time_config,
    )
