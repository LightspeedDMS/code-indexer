"""
Search backend wiring helper for CIDX server startup.

Builds the ``SearchBackend`` that FTS and semantic queries run through, selected
by the ``search_backend`` server config field.

Unlike the content and ripgrep seams, the search seam has no ``LocalSearchBackend``
class -- "local" means "use the existing code paths that read Tantivy/HNSW index
files directly". The wiring returns ``None`` for local and the caller keeps its
existing behaviour. ``delegated`` returns the plugin-resolved backend, which
forwards queries to a remote service.

Fail-fast policy (mirrors the git, content and ripgrep wiring):
- ``local``     : returns None; existing code paths read local index files.
                  The DEFAULT, so an unset or absent config behaves exactly as
                  cidx does today.
- ``delegated`` : queries are forwarded to the service that owns the indexes.
                  There is deliberately **no silent fallback to local**.
- anything else : raises immediately, so a typo'd config cannot degrade into a
                  surprising default.

A stateless EKS pod configured for delegation has no Tantivy/HNSW indexes at
all, so a silent fallback to local would surface as "index not available" errors
on every query -- correct error text, wrong root cause, and invisible to the
operator who misconfigured the seam.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

# The seam Protocol lives in the SDK; re-exported for existing importers.
from cidx_sdk.seams.search import SearchBackend as SearchBackend

logger = logging.getLogger(__name__)

VALID_SEARCH_BACKENDS = ("local", "delegated")

# Temporal queries (time_range, time_range_all, at_commit, diff_type, author,
# chunk_type) have no delegated route: the SearchBackend protocol carries none
# of those parameters. Under ``delegated`` every temporal entry point refuses
# with this message instead of reading local temporal indexes, which a pod
# built for delegation does not have (the silent "index not available"
# fallback the module docstring forbids).
TEMPORAL_NOT_DELEGATED_ERROR_CODE = "TEMPORAL_SEARCH_NOT_DELEGATED"
TEMPORAL_NOT_DELEGATED_MESSAGE = (
    "Temporal search is not available on this server: search_backend is "
    "'delegated' and the delegated search backend does not carry temporal "
    "queries (time_range, time_range_all, at_commit, diff_type, author, "
    "chunk_type). Refusing to read local temporal indexes."
)


def build_search_backend(
    config: Any, *, delegated_backend: Optional[SearchBackend] = None
) -> Optional[SearchBackend]:
    """Build the SearchBackend configured by *config*, or None for local.

    Parameters
    ----------
    config:
        Server config object (ServerConfig). Read via ``getattr`` so callers
        holding a partial/bootstrap config still get the ``local`` default
        rather than an AttributeError.
    delegated_backend:
        The plugin-supplied backend used when ``search_backend='delegated'``.
        Resolved from the ``code_indexer.search_backend`` entry-point seam.
        Selecting ``delegated`` without one raises.

    Returns
    -------
    Optional[SearchBackend]
        None for local (existing code paths run); the injected backend when
        delegated.

    Raises
    ------
    RuntimeError
        If ``search_backend`` is unrecognised, or ``delegated`` was selected
        with no backend available.
    """
    backend_type = getattr(config, "search_backend", "local") or "local"

    if backend_type not in VALID_SEARCH_BACKENDS:
        raise RuntimeError(
            f"Unknown search_backend {backend_type!r}. "
            f"Valid values are {VALID_SEARCH_BACKENDS}."
        )

    if backend_type == "delegated":
        if delegated_backend is None:
            raise RuntimeError(
                "search_backend='delegated' selected but no delegated backend "
                "is available. Install a plugin registering a factory on the "
                "code_indexer.search_backend entry point, or set "
                "CIDX_SEARCH_BACKEND='package.module:factory'. Refusing to "
                "fall back to local indexes: a stateless pod built for "
                "delegation has no Tantivy/HNSW index files at all."
            )
        logger.info("SearchBackend: using delegated backend")
        return delegated_backend

    logger.info("SearchBackend: local (existing index-file code paths)")
    return None


def build_configured_search_backend(config: Any) -> Optional[SearchBackend]:
    """Resolve the delegated plugin (if any) and build the configured backend.

    The single call the startup path uses, so every consumer shares one
    selection. With ``search_backend`` unset/``local`` (the default) the plugin
    is never imported or instantiated and None is returned -- byte-for-byte the
    behaviour cidx has today.
    """
    # Imported lazily so the local-default path never imports the plugin seam
    # (and so a plugin import error only surfaces when delegation is requested).
    from code_indexer.server.services.search_backend_plugin import (
        resolve_delegated_search_backend,
    )

    backend_type = getattr(config, "search_backend", "local") or "local"
    delegated = (
        resolve_delegated_search_backend() if backend_type == "delegated" else None
    )
    return build_search_backend(config, delegated_backend=delegated)


def active_search_backend() -> Optional[SearchBackend]:
    """The running server's delegated search backend, or None for local.

    Reads ``app.state.search_backend`` (parked by the lifespan) without
    constructing the lazy ``code_indexer.server.app`` singleton: no app yet
    means no lifespan ran, so nothing was delegated. None under ``local``,
    the CLI and tests that build no app.
    """
    from code_indexer.server.mcp.handlers._utils import _lazy_module_attr_or_none

    app = _lazy_module_attr_or_none("app")
    state = getattr(app, "state", None)
    return getattr(state, "search_backend", None)
