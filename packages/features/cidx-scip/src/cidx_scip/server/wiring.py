"""``on_startup`` / ``on_shutdown`` of the ``scip`` feature.

What the core's lifespan and service init used to wire for SCIP, now done by
the feature when the core starts it (once per process, off the event loop):

=================================  ===========================================
Startup step                       Was (core)
=================================  ===========================================
``scip_backend`` validation        ``lifespan.py`` +
(a removed or unknown value        ``startup/scip_backend_wiring.py``
refuses startup)                   (``check_scip_backend_at_startup``)
dependency-installation audit      ``service_init.py`` + ``database_init.py``
repository                         + ``storage/factory.py`` (``scip_audit``)
self-healing workspace cleanup     ``service_init.py`` / ``app_wiring.py``
service                            (``workspace_cleanup_service``)
delegated build queue stop         (the queue's worker thread is a daemon;
                                   now stopped on shutdown)
=================================  ===========================================

The periodic backfill is a recurring feature job
(``feature.background_jobs()``), not a lifespan task. Everything installed here
is process wiring of this node; builds are tracked in the shared job tracker.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from cidx_sdk import FeatureContext
from cidx_sdk.capabilities import STORAGE
from cidx_sdk.capability_types import STORAGE_POSTGRES, StorageCapability

from cidx_scip._platform import server_platform
from cidx_scip.server import runtime

logger = logging.getLogger(__name__)


def _audit_repository(ctx: FeatureContext) -> Any:
    """The audit repository over the ``storage`` capability: the PostgreSQL
    backend in a cluster, the SQLite one solo (migration 1 made the table)."""
    from cidx_scip.server.audit import SCIPAuditRepository
    from cidx_scip.server.audit_postgres import SCIPAuditPostgresBackend
    from cidx_scip.server.audit_sqlite import SCIPAuditSqliteBackend

    storage = cast(StorageCapability, ctx.capability(STORAGE))
    backend: Any
    if storage.mode == STORAGE_POSTGRES:
        backend = SCIPAuditPostgresBackend(storage)
    else:
        backend = SCIPAuditSqliteBackend(storage)
    return SCIPAuditRepository(storage_backend=backend)


def start(ctx: FeatureContext) -> None:
    """Validate ``scip_backend`` and install the feature's process wiring.

    Raises:
        UnsupportedScipBackendError: ``scip_backend`` is a removed or unknown
            value (the core refuses startup). A missing or broken
            ``delegated-plugin`` plugin is logged at ERROR instead, and per-job
            builds fail soft (unchanged from the core's own check).
    """
    from cidx_scip.server.backend_wiring import check_scip_backend_at_startup
    from cidx_scip.server.workspace_cleanup import WorkspaceCleanupService

    runtime.CONTEXT.install(ctx)
    try:
        check_scip_backend_at_startup(runtime.backend_config)
        runtime.AUDIT_REPOSITORY.install(_audit_repository(ctx))
        runtime.WORKSPACE_CLEANUP.install(
            WorkspaceCleanupService(
                job_manager=server_platform().app_attr("background_job_manager"),
                # Standard temp directory for SCIP self-healing workspaces.
                workspace_root="/tmp",
            )
        )
    except BaseException:
        stop()
        raise


def stop() -> None:
    """Stop the delegated build queue's worker and clear the wiring."""
    from cidx_scip.server import resolution_queue

    try:
        resolution_queue.shutdown_delegated_scip_build_queue()
    except Exception as exc:  # shutdown never raises
        logger.warning("SCIP delegated build queue did not stop: %s", exc)
    runtime.WORKSPACE_CLEANUP.uninstall()
    runtime.AUDIT_REPOSITORY.uninstall()
    runtime.CONTEXT.uninstall()
