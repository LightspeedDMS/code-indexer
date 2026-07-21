"""
Git backend wiring helper for CIDX server startup.

EVO-64678 (EVO-64616 Phase 2) — the git sibling of ``clone_backend_wiring``.
Builds the ``GitBackend`` that cidx's internal git operations run through,
selected by the ``git_backend`` server config field.

Fail-fast policy (mirrors the clone wiring):
- ``local``     : no external validation needed — git runs in-process via
                  ``utils.git_runner``. This is the DEFAULT, so an unset or
                  absent config behaves exactly as cidx does today.
- ``delegated`` : git is executed by the git-mcp spoke. cidx itself must then
                  perform NO git work, so there is deliberately **no silent
                  fallback to local** — an unavailable delegated backend raises,
                  the same way ``clone_backend='delegated'`` refuses to quietly
                  fall back to a cidx-side copy (EVO-64615).
- anything else : raises immediately, so a typo'd config can't degrade into a
                  surprising default.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from code_indexer.server.storage.shared.git_backend import (
    VALID_GIT_BACKENDS,
    GitBackend,
    GitBackendFactory,
)

logger = logging.getLogger(__name__)


def build_git_backend(
    config: Any, *, delegated_backend: Optional[GitBackend] = None
) -> GitBackend:
    """Build the GitBackend configured by *config*.

    Parameters
    ----------
    config:
        Server config object (ServerConfig). Read via ``getattr`` so callers
        holding a partial/bootstrap config still get the ``local`` default
        rather than an AttributeError.
    delegated_backend:
        The plugin-supplied backend used when ``git_backend='delegated'``.
        Phase 3 resolves this from the ``code_indexer.git_backend`` entry-point
        seam (the ``cidx_git_backend`` plugin, which talks to git-mcp). Until
        that ships, selecting ``delegated`` without injecting one raises.

    Returns
    -------
    GitBackend
        ``LocalGitBackend`` by default; the injected backend when delegated.

    Raises
    ------
    RuntimeError
        If ``git_backend`` is unrecognised, or ``delegated`` was selected with
        no backend available.
    """
    backend_type = getattr(config, "git_backend", "local") or "local"

    if backend_type not in VALID_GIT_BACKENDS:
        raise RuntimeError(
            f"Unknown git_backend {backend_type!r}. "
            f"Valid values are {VALID_GIT_BACKENDS}."
        )

    if backend_type == "delegated":
        if delegated_backend is None:
            raise RuntimeError(
                "git_backend='delegated' selected but no delegated backend is "
                "available. Install the cidx_git_backend plugin (EVO-64678 "
                "Phase 3), which registers a git-mcp backend on the "
                "code_indexer.git_backend entry point. Refusing to fall back to "
                "local git: delegation exists so cidx performs no git work."
            )
        backend = GitBackendFactory.create(
            "delegated", delegated_backend=delegated_backend
        )
        logger.info("GitBackend: using delegated backend (git-mcp)")
        return backend

    backend = GitBackendFactory.create("local")
    logger.info("GitBackend: using LocalGitBackend (in-process git)")
    return backend
