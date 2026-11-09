"""
Git backend abstraction for cidx's internal git operations.

issue-64616 Phase 2 (issue-64678) — the git sibling of ``CloneBackend``
(``storage/shared/clone_backend.py``). cidx's job is semantic search; it should
do **no git work itself**. Today git runs as scattered raw
``subprocess.run(["git", ...])`` across the golden-repo and activated-repo
managers. This module introduces a single seam so those call sites route through
a pluggable backend:

- ``LocalGitBackend``  — runs git in-process. Lives in its own distribution
  (``cidx-backend-git``, entry ``local``), reached through the seam resolver
  (:func:`local_git_backend`), and runs every command through the core's
  ``utils.git_runner`` (:class:`CoreGitProcessRunner`: SSH-safe env +
  ``safe.directory`` handling). This is the DEFAULT.
- ``GitMcpBackend``    — (Phase 3, shipped as the ``cidx_git_backend`` plugin)
  delegates each operation to the the git service spoke. Not defined here.

The Protocol uses structural subtyping (``typing.Protocol``), NOT inheritance,
mirroring ``CloneBackend``. Selection is config-driven in
``startup/git_backend_wiring.py``; default ``local`` = zero regression.

**Timeouts are part of the contract.** Every call site today passes a
config-driven timeout (``git_clone_timeout``, ``git_pull_timeout``, ...), so
every method accepts one and a refactor can preserve it exactly. ``None`` means
"no timeout", matching ``subprocess.run``.

**Network operations take ``credentials_url``** (upstream v12.89.0): the
repository URL as registered. A backend that runs git itself supplies its
credential through the environment, never on argv nor stored in the clone; a
backend that ``owns_credentials`` ignores it.

**Cancellation is scoped, not a parameter** (Bug #2012): inside
``utils.git_runner.git_cancel_scope(check)`` every git command the local
backend runs is terminated when the owning job is cancelled
(``SubprocessCancelledError``). A delegated backend's request is bounded by
its timeout instead; callers check cancellation between operations.

**Failures raise.** Methods run with ``check=True`` unless documented otherwise,
so a non-zero exit raises ``subprocess.CalledProcessError`` — whose ``stderr``
callers already use to build their own errors (e.g. ``GitOperationError``).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

# The Protocols live in the SDK (``cidx_sdk.seams.git``); re-exported so
# existing imports and isinstance checks see the same classes.
from cidx_sdk.seams.git import (
    DETACHED_HEAD_MARKER as DETACHED_HEAD_MARKER,
    VALID_GIT_BACKENDS as VALID_GIT_BACKENDS,
    GitBackend as GitBackend,
    GitCommandRunner as GitCommandRunner,
    GitProcessRunner as GitProcessRunner,
    Timeout as Timeout,
)
from cidx_sdk.seams.selection import LOCAL_MODE

# The local implementation lives in its own distribution
# (packages/backends/cidx-backend-git) and is selected by the seam resolver
# under the entry name ``local``; re-exported for existing imports and
# ``isinstance`` checks. Never construct it here: :func:`local_git_backend`.
from cidx_backend_git.local import LocalGitBackend as LocalGitBackend
from code_indexer.utils.git_runner import (
    run_git_command,
    run_git_command_with_retry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The core's git process runner: what the local backend runs git through
# ---------------------------------------------------------------------------


class CoreGitProcessRunner:
    """The core's git funnel, as the local backend's ``GitProcessRunner``.

    ``run_git_command`` (``utils.git_runner``) owns the SSH-safe environment,
    ``safe.directory``, the credential of a network operation, the job's
    cancellation scope and the git invoker routing; ``retry`` selects its
    retrying variant. Both are looked up on THIS module at call time, so a
    test that patches ``git_backend.run_git_command`` reaches the local
    backend, as it did when the backend lived here.
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
        runner = run_git_command_with_retry if retry else run_git_command
        # Optional arguments are forwarded only when set, so a runner patched
        # by a test with the historical (cmd, cwd, check, timeout) shape keeps
        # working.
        kwargs: Dict[str, Any] = {"cwd": cwd, "check": check, "timeout": timeout}
        if not text:
            kwargs["text"] = False
        if credentials_url is not None:
            kwargs["credentials_url"] = credentials_url
        if env:
            kwargs["env"] = dict(env)
        result: subprocess.CompletedProcess = runner(list(cmd), **kwargs)
        return result


def local_git_backend() -> LocalGitBackend:
    """A new instance of the git backend installed for the ``local`` mode.

    Resolved through the git seam's resolver (``cidx-backend-git``) and given
    the core's :class:`CoreGitProcessRunner`. With nothing installed for the
    mode this raises :class:`cidx_sdk.errors.SeamUnavailableError`, naming the
    seam: the kernel holds no git implementation of its own to fall back to.
    """
    from code_indexer.server.services.git_backend_plugin import resolve_git_backend

    backend = resolve_git_backend(LOCAL_MODE, CoreGitProcessRunner())
    if not isinstance(backend, LocalGitBackend):
        raise TypeError(
            "the git seam's local provider is not a LocalGitBackend: "
            f"{type(backend).__name__}"
        )
    return backend


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def head_branch(
    backend: "GitBackend", repo_path: str, timeout: Timeout = None
) -> Optional[str]:
    """The checked-out branch, or ``None`` when HEAD is detached.

    A detached HEAD is a normal state (a repository pinned to a tag or commit)
    and is reported as ``None``. ANY other failure -- a transport error to
    the git service, a path that is not a repository -- raises: reading it as
    "detached" would make refresh silently skip the pull, which is exactly how
    a pod without the repository on its disk used to fail.
    """
    try:
        return backend.symbolic_ref(repo_path, short=True, timeout=timeout) or None
    except subprocess.CalledProcessError as exc:
        if DETACHED_HEAD_MARKER in str(exc.stderr or ""):
            return None
        raise


class GitBackendFactory:
    """Create a GitBackend from a backend name.

    Mirrors ``CloneBackendFactory``. ``local`` (the default) is the module
    installed for the git seam's ``local`` mode (``cidx-backend-git``), through
    the seam resolver; with none installed it raises, naming the seam.
    ``delegated`` is supplied by the entry-point plugin seam
    (``services.git_backend_plugin``) in Phase 3; it raises here unless a
    plugin-provided backend is injected, so the core never silently no-ops.
    """

    @staticmethod
    def create(
        backend: str = "local", *, delegated_backend: Optional[GitBackend] = None
    ) -> GitBackend:
        if backend == "local":
            return local_git_backend()
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
