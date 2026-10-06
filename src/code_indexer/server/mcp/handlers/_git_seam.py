"""Read git remote state through the configured GitBackend seam (EVO-64678).

The MCP git-write and pull-request handlers resolved a repository's remote URL by
shelling out to ``git remote get-url`` directly. That bypassed the seam, so with
``git_backend='delegated'`` those handlers still read the LOCAL clone's config
while every other git-executing manager went through the configured backend --
two different answers to the same question, and the local one carries whatever
the clone was created with.

This module is the one place the handlers ask for that, so a deployment with a
delegated backend gets a consistent answer.
"""

from __future__ import annotations

from typing import Any, Optional

from code_indexer.server.storage.shared.git_backend import GitBackend, LocalGitBackend


def configured_git_backend(app_module: Any = None) -> GitBackend:
    """The backend selected at startup, or a ``LocalGitBackend``.

    The startup path parks its single selection on ``app.state.git_backend`` so
    every caller shares it. The fallback keeps solo/CLI contexts and unit tests --
    which never run lifespan -- on exactly the behaviour they have today.
    """
    state: Optional[Any] = None
    if app_module is not None:
        state = getattr(getattr(app_module, "app", None), "state", None)
    backend = getattr(state, "git_backend", None) if state is not None else None
    if backend is None:
        return LocalGitBackend()
    return backend  # type: ignore[no-any-return]


def remote_get_url(
    repo_path: str, remote: str = "origin", *, app_module: Any = None
) -> str:
    """``git remote get-url <remote>`` through the configured seam."""
    return configured_git_backend(app_module).remote_get_url(repo_path, remote)
