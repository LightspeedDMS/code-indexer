"""
Where an activated repository's working copy lives: this pod, or workspace-mcp.

An activated repository is a user's writable copy of a golden repository at
``<activated_repos_dir>/<username>/<user_alias>``. Under the local content
backend (the default) that path is a directory on this pod's disk and
``ActivatedRepoManager`` creates, inspects and removes it exactly as it always
has -- :class:`LocalWorkingCopies` is that code, unchanged.

Under ``content_backend='delegated'`` the pod has no repository volume. The
working copy exists only on workspace-mcp's storage, at the SAME absolute path,
and :class:`DelegatedWorkingCopies` reaches it there:

* lifecycle -- create / exists / remove through workspace-mcp's clone-path
  endpoints (``POST /clone-path`` with ``if_absent``, ``/clone-path/stat``,
  ``/clone-path/delete``), via :class:`DelegatedCloneBackend`;
* git -- the git backend, which must then be git-mcp (addressed by the same
  path, its ``cow_path``);
* content reads and writes -- the content seam at the same path (file tools).

What stays on this pod under delegated: the per-user metadata registry of solo
mode and the copy's index state (``<path>/.code-indexer``), both cidx state.

Fail-loud rule: a delegated server whose clone or git backend cannot reach the
working copy refuses (:class:`WorkingCopyUnavailableError`); it never falls
back to this pod's disk, where a missing directory would read as "no
repository" instead of "cannot tell".
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any, Union

from code_indexer.server.storage.shared.clone_backend import DelegatedCloneBackend
from code_indexer.server.storage.shared.git_backend import LocalGitBackend

logger = logging.getLogger(__name__)

PathLike = Union[str, "os.PathLike[str]"]


class WorkingCopyUnavailableError(RuntimeError):
    """The working copies are delegated but this server cannot reach them."""


class LocalWorkingCopies:
    """Today's behaviour: the working copy is a directory on this pod."""

    is_delegated = False

    def exists(self, path: PathLike) -> bool:
        return os.path.exists(path)

    def is_git_repo(self, path: PathLike) -> bool:
        return os.path.exists(os.path.join(path, ".git"))

    def discard(self, path: PathLike) -> None:
        """Remove a failed or partial copy; a no-op when absent."""
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)


class DelegatedWorkingCopies:
    """The working copy is on workspace-mcp's storage, at the same path."""

    is_delegated = True

    def __init__(self, store: DelegatedCloneBackend) -> None:
        self._store = store

    def exists(self, path: PathLike) -> bool:
        return self._store.stat_working_copy(os.fspath(path)).exists

    def is_git_repo(self, path: PathLike) -> bool:
        return self.exists(os.path.join(os.fspath(path), ".git"))

    def clone(
        self, source_path: str, dest_path: str, *, timeout: Any, cancel_check: Any
    ) -> str:
        """Create the copy; an existing destination raises
        ``CloneDestinationExistsError`` and is left untouched."""
        return self._store.clone_working_copy(
            source_path, dest_path, timeout=timeout, cancel_check=cancel_check
        )

    def remove(self, path: PathLike) -> bool:
        """Remove the copy; False when it was already absent. Failures raise."""
        return self._store.delete_working_copy(os.fspath(path))

    def discard(self, path: PathLike) -> None:
        """Remove a failed or partial copy. Never raises: it runs while another
        error is already propagating, which must not be masked -- a copy that
        could not be removed is logged as a leak instead."""
        try:
            self.remove(path)
        except Exception as exc:  # the original failure is what callers report
            logger.error(
                "RESOURCE LEAK WARNING: could not remove the working copy at "
                "'%s' on workspace-mcp after a failure: %s",
                os.fspath(path),
                exc,
            )


WorkingCopies = Union[LocalWorkingCopies, DelegatedWorkingCopies]

_LOCAL = LocalWorkingCopies()


def working_copies_for(clone_backend: Any, git_backend: Any) -> WorkingCopies:
    """The working-copy store for the running server's configuration.

    Local unless the content backend is delegated. Then BOTH the clone backend
    (lifecycle) and the git backend (every git operation on the copy) must be
    the delegated ones: anything else would run against this pod's disk, where
    the copy does not exist.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    if not is_content_seam_delegated():
        return _LOCAL
    if not isinstance(clone_backend, DelegatedCloneBackend):
        raise WorkingCopyUnavailableError(
            "Activated repositories are not on this server (content_backend="
            "'delegated') and the clone backend is "
            f"{type(clone_backend).__name__}, not workspace-mcp. Set "
            "clone_backend='delegated' (workspace_mcp.endpoint)."
        )
    if git_backend is None or isinstance(git_backend, LocalGitBackend):
        raise WorkingCopyUnavailableError(
            "Activated repositories are not on this server (content_backend="
            "'delegated') but git runs locally. Set git_backend='delegated' "
            "(git-mcp) so git reaches the working copy."
        )
    return DelegatedWorkingCopies(clone_backend)
