"""
Index store wiring for CIDX server startup.

Builds the ``IndexStore`` refresh publishes through, selected by the
``index_store_backend`` server config field. Same fail-fast policy as the git,
content and ripgrep wiring:

- ``local``     : the index is read where it is built (today). The DEFAULT, so
                  an unset or absent config behaves exactly as cidx does now.
- ``delegated`` : each refresh publishes the built index to the artifact
                  service and reads back a checked-out, verified version. No
                  silent fallback: an unusable plugin raises at startup. A
                  served snapshot then holds index state only (no working
                  tree), so repository bytes must come through the content
                  and ripgrep seams (``SEAMS_REQUIRED_BY_DELEGATED``), which
                  map a snapshot path to the base clone; with either one
                  local, file/regex/X-Ray reads of a snapshot would come back
                  silently empty, so startup refuses that combination.
- anything else : raises immediately.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from code_indexer.utils.index_store import (
    DelegatedIndexStore,
    IndexStore,
    LocalIndexStore,
)

logger = logging.getLogger(__name__)

VALID_INDEX_STORE_BACKENDS = ("local", "delegated")

#: Server config fields that must also be ``delegated`` with a delegated store.
SEAMS_REQUIRED_BY_DELEGATED = ("content_backend", "ripgrep_backend")


def build_index_store(config: Any, *, artifacts: Optional[Any] = None) -> IndexStore:
    """Build the IndexStore configured by *config* (read via ``getattr``)."""
    backend_type = getattr(config, "index_store_backend", "local") or "local"
    if backend_type not in VALID_INDEX_STORE_BACKENDS:
        raise RuntimeError(
            f"Unknown index_store_backend {backend_type!r}. "
            f"Valid values are {VALID_INDEX_STORE_BACKENDS}."
        )
    if backend_type == "delegated":
        local_seams = [
            field
            for field in SEAMS_REQUIRED_BY_DELEGATED
            if (getattr(config, field, "local") or "local") != "delegated"
        ]
        if local_seams:
            raise RuntimeError(
                "index_store_backend='delegated' requires "
                + " and ".join(f"{field}='delegated'" for field in local_seams)
                + ": a served snapshot holds index state only, so reading "
                "repository files from it locally would return nothing."
            )
        if artifacts is None:
            raise RuntimeError(
                "index_store_backend='delegated' selected but no index-artifact "
                "client is available. Install a plugin registering a factory on "
                "the code_indexer.index_artifacts entry point, or set "
                "CIDX_INDEX_ARTIFACTS='package.module:factory'. Refusing to fall "
                "back to the local store: built indexes would never be published."
            )
        logger.info("IndexStore: using delegated store (%s)", type(artifacts).__name__)
        return DelegatedIndexStore(artifacts)
    logger.info("IndexStore: using LocalIndexStore (index read where built)")
    return LocalIndexStore()


def build_configured_index_store(config: Any) -> IndexStore:
    """Resolve the artifact plugin (only when delegated) and build the store."""
    from code_indexer.server.services.index_artifacts_plugin import (
        resolve_index_artifacts,
    )

    backend_type = getattr(config, "index_store_backend", "local") or "local"
    artifacts = resolve_index_artifacts() if backend_type == "delegated" else None
    return build_index_store(config, artifacts=artifacts)


def server_index_store() -> IndexStore:
    """The store the running server built at startup (``app.state.index_store``).

    ``LocalIndexStore`` outside a server process (CLI, tests) or before startup
    built one: indexes are then read where they were built, as before. Read
    without importing the app, so a CLI process never constructs a store.
    """
    from code_indexer.server.utils.registry_factory import (
        _running_server_app_state,
    )

    store = getattr(_running_server_app_state(), "index_store", None)
    return store if store is not None else LocalIndexStore()
