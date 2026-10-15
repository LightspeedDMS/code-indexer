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


def server_index_store() -> Optional[IndexStore]:
    """The store the running server built at startup (``app.state.index_store``).

    Returns ``None`` outside a server process (CLI, tests) or before startup
    has built one.  Raises ``RuntimeError`` if the parked value is not an
    ``IndexStore``.
    """
    from code_indexer.server.utils.registry_factory import (
        _running_server_app_state,
    )

    state = _running_server_app_state()
    if state is None:
        return None
    store = getattr(state, "index_store", None)
    if store is None:
        return None
    if not hasattr(store, "is_local"):
        raise RuntimeError(
            f"app.state.index_store is not an IndexStore: {type(store)}"
        )
    return store


def start_checkout_sweeper(app: Any, resource_config: Any) -> Optional[Any]:
    """Start this pod's sweep of leftover IndexStore checkouts
    (``server/services/checkout_sweep.py``) and park it on
    ``app.state.checkout_sweeper``. Every pod runs one (the state is
    pod-local). None -- and no thread -- under the local store."""
    app.state.checkout_sweeper = None
    if server_index_store().is_local:
        return None
    from code_indexer.server.services.checkout_sweep import CheckoutSweeper

    manager = app.state.activated_repo_manager
    sweeper = CheckoutSweeper(
        activated_repo_manager=manager,
        activated_repos_dir=str(manager.activated_repos_dir),
        golden_repos_dir=getattr(app.state, "golden_repos_dir", None),
        query_tracker=getattr(app.state, "query_tracker", None),
        grace_seconds=resource_config.index_checkout_sweep_grace_seconds,
    )
    sweeper.start(resource_config.index_checkout_sweep_interval_seconds)
    app.state.checkout_sweeper = sweeper
    logger.info("IndexStore checkout sweep started on this pod")
    return sweeper


def stop_checkout_sweeper(app: Any) -> None:
    sweeper = getattr(app.state, "checkout_sweeper", None)
    if sweeper is not None:
        sweeper.stop()
