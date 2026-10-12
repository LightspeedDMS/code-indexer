"""
Index store wiring for CIDX server startup.

Builds the ``IndexStore`` refresh publishes through, selected by the
``index_store_backend`` server config field. Same fail-fast policy as the git,
content and ripgrep wiring:

- ``local``     : the index is read where it is built (today). The DEFAULT, so
                  an unset or absent config behaves exactly as cidx does now.
- ``delegated`` : each refresh publishes the built index to the artifact
                  service and reads back a checked-out, verified version. No
                  silent fallback: an unusable plugin raises at startup.
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


def build_index_store(config: Any, *, artifacts: Optional[Any] = None) -> IndexStore:
    """Build the IndexStore configured by *config* (read via ``getattr``)."""
    backend_type = getattr(config, "index_store_backend", "local") or "local"
    if backend_type not in VALID_INDEX_STORE_BACKENDS:
        raise RuntimeError(
            f"Unknown index_store_backend {backend_type!r}. "
            f"Valid values are {VALID_INDEX_STORE_BACKENDS}."
        )
    if backend_type == "delegated":
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
