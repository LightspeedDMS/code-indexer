"""
Clone backend wiring helper for CIDX server startup.

Story #510 AC8 — builds a VersionedSnapshotManager configured with the
appropriate CloneBackend based on server config, then makes it available
for injection into golden-repo lifecycle services.

Fail-fast policy:
- cow-daemon: daemon health check AND NFS mount check MUST pass.
  If either fails, raise RuntimeError immediately — NO fallback.
- ontap: no extra validation (existing ONTAP path is unchanged).
- local: no external validation needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass  # pragma: no cover

logger = logging.getLogger(__name__)


def _check_daemon_health(daemon_url: str) -> None:
    """Verify CoW daemon is reachable via GET /api/v1/health.

    Raises RuntimeError if the daemon does not return HTTP 200.
    """
    import requests  # noqa: PLC0415

    health_url = f"{daemon_url.rstrip('/')}/api/v1/health"
    logger.info("Checking CoW daemon health at %s", health_url)
    try:
        resp = requests.get(health_url, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"CoW daemon not reachable at {daemon_url}. "
            f"Ensure the daemon is running. Error: {exc}"
        ) from exc
    logger.info("CoW daemon health check: OK")


def _check_nfs_mount(mount_point: str) -> None:
    """Verify NFS mount is healthy via NfsMountValidator.

    Raises RuntimeError if the mount point is not healthy.
    """
    from code_indexer.server.storage.shared.nfs_validator import NfsMountValidator  # noqa: PLC0415

    logger.info("Validating NFS mount at %s", mount_point)
    validator = NfsMountValidator(mount_point)
    result = validator.validate()
    if not result["healthy"]:
        raise RuntimeError(
            f"NFS mount is not healthy at {mount_point}: {result.get('error', 'unknown error')}"
        )
    logger.info(
        "NFS mount validation: OK (latency=%.1fms)", result.get("latency_ms", 0)
    )


def build_snapshot_manager(config: Any, versioned_base: str) -> Any:
    """Build a VersionedSnapshotManager configured from *config*.

    Parameters
    ----------
    config:
        Server config object (ServerConfig). Must have a ``clone_backend``
        field (str: "local", "ontap", or "cow-daemon") and optional
        ``cow_daemon`` / ``ontap`` sub-configs.
    versioned_base:
        Base directory for filesystem CoW snapshots and the LocalCloneBackend.

    Returns
    -------
    VersionedSnapshotManager
        Configured with the appropriate CloneBackend injected.

    Raises
    ------
    RuntimeError
        If ``clone_backend == "cow-daemon"`` and the daemon is unreachable or
        the NFS mount is unhealthy.  No fallback is performed.
    """
    from code_indexer.server.storage.shared.clone_backend import CloneBackendFactory  # noqa: PLC0415
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )  # noqa: PLC0415

    backend_type = getattr(config, "clone_backend", "local") or "local"
    logger.info("Building VersionedSnapshotManager with clone_backend=%r", backend_type)

    if backend_type == "cow-daemon":
        cow_cfg = config.cow_daemon
        # Fail-fast: validate daemon and NFS before constructing anything
        _check_daemon_health(cow_cfg.daemon_url)
        _check_nfs_mount(cow_cfg.mount_point)

        backend = CloneBackendFactory.create(
            clone_backend_type="cow-daemon",
            cow_daemon_config=cow_cfg,
        )
        logger.info(
            "VersionedSnapshotManager: using CowDaemonBackend (daemon=%s, mount=%s)",
            cow_cfg.daemon_url,
            cow_cfg.mount_point,
        )

    elif backend_type == "ontap":
        backend = CloneBackendFactory.create(
            clone_backend_type="ontap",
            versioned_base=versioned_base,
            ontap_config=config.ontap,
        )
        logger.info(
            "VersionedSnapshotManager: using OntapCloneBackend (mount=%s)",
            config.ontap.mount_point,
        )

    else:
        # "local" or any unrecognised value — default to local CoW
        backend = CloneBackendFactory.create(
            clone_backend_type="local",
            versioned_base=versioned_base,
        )
        logger.info(
            "VersionedSnapshotManager: using LocalCloneBackend (versioned_base=%s)",
            versioned_base,
        )

    return VersionedSnapshotManager(clone_backend=backend)
