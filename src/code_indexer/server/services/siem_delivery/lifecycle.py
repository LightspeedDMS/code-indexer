"""Construction and failure recording of the SIEM delivery scheduler.

The lifespan constructs the scheduler after the audit service is bound and
the fault-injection gate is wired, AWAITS the offloaded registration
barrier before serving, then starts the loop.  A failure degrades the
process (visible on /health, the stats endpoint and an ERROR log); it never
kills boot.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

import anyio.to_thread

from code_indexer.server.services.siem_delivery import capture, telemetry

logger = logging.getLogger(__name__)


def construct_scheduler(
    app: Any, backend_registry: Any, background_job_manager: Any
) -> Any:
    from code_indexer.server.services.audit_capture import audit_node_id
    from code_indexer.server.services.config_service import get_config_service
    from code_indexer.server.services.siem_delivery.scheduler import (
        SiemDeliveryScheduler,
    )

    if (
        backend_registry is None
        or getattr(backend_registry, "siem_delivery", None) is None
    ):
        raise RuntimeError("backend_registry.siem_delivery is not available")
    factory = getattr(app.state, "http_client_factory", None)
    if factory is None:
        raise RuntimeError("http_client_factory is not available")
    harness_active = getattr(app.state, "fault_injection_service", None) is not None
    config_service = get_config_service()
    return SiemDeliveryScheduler(
        db=backend_registry.siem_delivery,
        config_service=config_service,
        background_job_manager=background_job_manager,
        http_client_factory=factory,
        harness_active=harness_active,
        node_id=audit_node_id(),
        credential_store=_credential_store(
            backend_registry.siem_delivery, config_service
        ),
    )


# Domain separation: the SIEM credential key never equals another feature's
# key derived from the same cluster secret.
_SIEM_CLUSTER_KEY_SALT = hashlib.sha256(b"cidx-siem-credential-cluster-salt").digest()


def _credential_store(db: Any, config_service: Any) -> Any:
    """The encrypted credential store.

    Cluster (PostgreSQL): the key derives from the SHARED JWT secret row in
    ``cluster_secrets`` (the helper LLM lease state uses), so every node
    decrypts the one stored row.  Solo (SQLite): the node-local
    ``.encryption_key_salt`` derivation of the CI-token and git-credential
    managers."""
    from pathlib import Path

    from code_indexer.server.services.encryption_key_salt import (
        ensure_encryption_key_salt,
    )
    from code_indexer.server.services.siem_delivery.credential import (
        SiemCredentialStore,
    )
    from code_indexer.server.services.token_encryption import derive_encryption_key

    # Derived LAZILY on first use (a scheduler or worker thread): this
    # constructor runs on the async startup path, where no DB query or key
    # file read may happen.
    if db.dialect.name == "postgres":
        from code_indexer.server.config.llm_lease_state import (
            derive_cluster_encryption_key,
        )

        return SiemCredentialStore.lazy(
            db, lambda: derive_cluster_encryption_key(db.pool, _SIEM_CLUSTER_KEY_SALT)
        )

    def _solo_key() -> bytes:
        server_dir = Path(config_service.config_manager.server_dir)
        ensure_encryption_key_salt(server_dir, config_service.get_config().storage_mode)
        return derive_encryption_key(
            server_dir_for_salt=server_dir, cluster_secret=None
        )

    return SiemCredentialStore.lazy(db, _solo_key)


def record_startup_failure(app: Any, error: BaseException) -> None:
    app.state.siem_delivery_scheduler = None
    app.state.siem_delivery_startup_error = type(error).__name__
    logger.error(
        "SIEM delivery failed to start in this process (%s): capture is "
        "INACTIVE here; see GET /api/admin/siem-delivery/stats",
        type(error).__name__,
        exc_info=True,
    )


def _gauge_snapshot(scheduler: Any) -> Dict[str, float]:
    from code_indexer.server.services.siem_delivery.stats import persisted_snapshot

    inputs = scheduler.health_inputs()
    state = inputs["state"]
    snap = persisted_snapshot(state)
    view = inputs["view"]
    return {
        "pending": float(snap.get("pending") or 0),
        "quarantined": float(snap.get("quarantined") or 0),
        "oldest_pending_age_seconds": float(
            snap.get("oldest_pending_age_seconds") or 0
        ),
        "backlog_bytes_estimate": float(snap.get("backlog_bytes_estimate") or 0),
        "projected_hours_to_disk_full": float(
            snap.get("projected_hours_to_disk_full") or 0
        ),
        "halted": 1.0 if state.get("halted_class") else 0.0,
        "capture_active": 1.0 if capture.capture_state().active else 0.0,
        "unrecoverable": float(state.get("unrecoverable_total") or 0),
        "capture_after_boundary": float(state.get("capture_after_boundary_total") or 0),
        "capture_after_boundary_late": float(
            state.get("capture_after_boundary_late_total") or 0
        ),
        "enabled": 1.0 if view is not None and view.section.enabled else 0.0,
    }


def start_after_registration(app: Any, scheduler: Any) -> None:
    """Called once ``register_process`` has completed: start the loop only."""
    scheduler.start()
    telemetry.register_gauges(lambda: _gauge_snapshot(scheduler))
    app.state.siem_delivery_scheduler = scheduler
    app.state.siem_delivery_startup_error = None
    logger.info("SIEM delivery scheduler started (process %s)", scheduler.process_id)


async def start_scheduler(
    app: Any, backend_registry: Any, background_job_manager: Any
) -> Optional[Any]:
    """Construct, register (offloaded, AWAITED: the process is registered
    before it serves any request, with no DB I/O on the event loop), start.

    Returns the started scheduler, or None after recording the failure.
    """
    try:
        scheduler = construct_scheduler(app, backend_registry, background_job_manager)
        await anyio.to_thread.run_sync(scheduler.register_process)
        start_after_registration(app, scheduler)
    except Exception as exc:  # degrade, never kill boot
        record_startup_failure(app, exc)
        return None
    return scheduler
