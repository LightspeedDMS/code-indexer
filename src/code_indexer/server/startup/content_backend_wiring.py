"""
Content backend wiring helper for CIDX server startup.

The content sibling of ``git_backend_wiring``. Builds the ``ContentBackend``
that repository reads run through, selected by the ``content_backend`` server
config field.

Fail-fast policy (mirrors the git and clone wiring):
- ``local``     : reads this container's filesystem. This is the DEFAULT, so an
                  unset or absent config behaves exactly as cidx does today.
- ``delegated`` : bytes come from the service that owns filesystem access, and
                  cidx reads no repository files itself. There is deliberately
                  **no silent fallback to local**.
- anything else : raises immediately, so a typo'd config cannot degrade into a
                  surprising default.

Why the no-fallback rule is stricter here than elsewhere: a pod configured for
delegation may not mount the repositories at all. Falling back to the local
filesystem would not raise -- ``os.walk`` over a missing tree yields nothing --
so the run would report an EMPTY repository and index zero files while looking
entirely successful. An error is the only safe failure.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from code_indexer.server.storage.shared.content_backend import (
    ContentBackend,
    ContentBackendFactory,
)

logger = logging.getLogger(__name__)

VALID_CONTENT_BACKENDS = ("local", "delegated")


def build_content_backend(
    config: Any, *, delegated_backend: Optional[ContentBackend] = None
) -> ContentBackend:
    """Build the ContentBackend configured by *config*.

    Parameters
    ----------
    config:
        Server config object (ServerConfig). Read via ``getattr`` so callers
        holding a partial/bootstrap config still get the ``local`` default
        rather than an AttributeError.
    delegated_backend:
        The plugin-supplied backend used when ``content_backend='delegated'``.
        Resolved from the ``code_indexer.content_backend`` entry-point seam.
        Selecting ``delegated`` without one raises.

    Returns
    -------
    ContentBackend
        ``LocalContentBackend`` by default; the injected backend when delegated.

    Raises
    ------
    RuntimeError
        If ``content_backend`` is unrecognised, or ``delegated`` was selected
        with no backend available.
    """
    backend_type = getattr(config, "content_backend", "local") or "local"

    if backend_type not in VALID_CONTENT_BACKENDS:
        raise RuntimeError(
            f"Unknown content_backend {backend_type!r}. "
            f"Valid values are {VALID_CONTENT_BACKENDS}."
        )

    if backend_type == "delegated":
        if delegated_backend is None:
            raise RuntimeError(
                "content_backend='delegated' selected but no delegated backend "
                "is available. Install a plugin registering a factory on the "
                "code_indexer.content_backend entry point, or set "
                "CIDX_CONTENT_BACKEND='package.module:factory'. Refusing to "
                "fall back to the local filesystem: a pod built for delegation "
                "may not mount the repositories at all, and the fallback would "
                "report an empty repository instead of failing."
            )
        backend = ContentBackendFactory.create(
            "delegated", delegated_backend=delegated_backend
        )
        logger.info("ContentBackend: using delegated backend")
        return backend

    backend = ContentBackendFactory.create("local")
    logger.info("ContentBackend: using LocalContentBackend (this container's disk)")
    return backend


def build_configured_content_backend(config: Any) -> ContentBackend:
    """Resolve the delegated plugin (if any) and build the configured backend.

    The single call the startup path uses, so every consumer shares one
    selection. With ``content_backend`` unset/``local`` (the default) the plugin
    is never imported or instantiated and a plain ``LocalContentBackend`` is
    returned -- byte-for-byte the behaviour cidx has today.
    """
    # Imported lazily so the local-default path never imports the plugin seam
    # (and so a plugin import error only surfaces when delegation is requested).
    from code_indexer.server.services.content_backend_plugin import (
        resolve_delegated_content_backend,
    )

    backend_type = getattr(config, "content_backend", "local") or "local"
    delegated = (
        resolve_delegated_content_backend() if backend_type == "delegated" else None
    )
    return build_content_backend(config, delegated_backend=delegated)
