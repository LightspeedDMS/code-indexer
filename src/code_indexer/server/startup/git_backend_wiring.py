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


def build_configured_git_backend(config: Any) -> GitBackend:
    """Resolve the delegated plugin (if any) and build the configured backend.

    The Phase-3 completion of ``build_git_backend``: it fills the
    ``delegated_backend`` slot from the ``code_indexer.git_backend`` entry-point
    seam (the ``cidx_git_backend`` plugin / ``CIDX_GIT_BACKEND`` env var) via
    ``git_backend_plugin.resolve_delegated_git_backend``, then delegates to
    ``build_git_backend``. This is the single call the startup path uses so
    every git-executing manager shares one selection.

    With ``git_backend`` unset/``local`` (the default) the plugin is never
    instantiated and a plain ``LocalGitBackend`` is returned — byte-for-byte the
    behaviour cidx has today. With ``git_backend='delegated'`` a missing/broken
    plugin fails LOUD (never a silent local fallback) — see ``build_git_backend``
    and ``git_backend_plugin``.
    """
    return build_named_git_backend(getattr(config, "git_backend", "local") or "local")


def build_named_git_backend(backend_type: str) -> GitBackend:
    """Resolve the delegated plugin (if *backend_type* asks for it) and build.

    The by-name twin of :func:`build_configured_git_backend`, for where only the
    selection travels -- the ``cidx index`` child, which receives it as
    ``--git-backend`` from the server that spawned it. The plugin reads its
    endpoint from the environment the child inherits.
    """
    from types import SimpleNamespace

    # Imported lazily so the local-default path never imports the plugin seam
    # (and so a plugin import error only surfaces when delegation is requested).
    from code_indexer.server.services.git_backend_plugin import (
        resolve_delegated_git_backend,
    )

    delegated = resolve_delegated_git_backend() if backend_type == "delegated" else None
    return build_git_backend(
        SimpleNamespace(git_backend=backend_type), delegated_backend=delegated
    )


def configured_git_backend_name() -> str:
    """The running server's ``git_backend`` selection, ``local`` when unset."""
    from code_indexer.server.services.config_service import get_config_service

    config = get_config_service().get_config()
    return getattr(config, "git_backend", "local") or "local"
