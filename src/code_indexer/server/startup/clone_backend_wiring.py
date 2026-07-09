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
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass  # pragma: no cover

logger = logging.getLogger(__name__)


_MIN_DAEMON_VERSION = "0.2.0"


def _check_daemon_health(daemon_url: str) -> None:
    """Verify CoW daemon is reachable via GET /api/v1/health and meets minimum version.

    Raises RuntimeError if the daemon does not return HTTP 200, or if the reported
    version is absent or older than 0.2.0.
    """
    import requests  # noqa: PLC0415
    from packaging.version import parse as parse_version  # noqa: PLC0415

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

    data = resp.json()
    version = data.get("version")
    if version is None or parse_version(str(version)) < parse_version(
        _MIN_DAEMON_VERSION
    ):
        raise RuntimeError(
            f"CoW Daemon at {daemon_url} is version {version!r}; "
            f"CIDX requires {_MIN_DAEMON_VERSION}+. "
            f"Old daemon silently ignores dest_path and clones to wrong location."
        )
    logger.info("CoW daemon health check: OK (version=%s)", version)


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


def _under_root(resolved: str, root: str) -> bool:
    """True if *resolved* equals *root* or is a path segment under it."""
    root = root.rstrip("/")
    return bool(root) and (resolved == root or resolved.startswith(root + "/"))


def _check_golden_repos_symlink_placement(golden_repos_dir: str, cow_cfg: Any) -> None:
    """Bug #1337: verify golden_repos_dir is placed so CowDaemonBackend can
    translate it to a daemon-local path.

    Per-user activation calls CowDaemonBackend.create_clone_at_path(), which
    requires the golden repo bytes to resolve under cow_daemon.mount_point or
    cow_daemon.daemon_storage_path (``cp --reflink`` physically needs both
    source and dest on the daemon's local XFS). golden_repos_dir must
    therefore be a SYMLINK into that tree, never a plain directory.

    Two failure modes are distinguished, BOTH non-fatal (logged WARNING,
    function returns -- never raises):
    - Dangling symlink (link present, target unresolvable -- e.g. the NFS/
      CoW host is transiently down): the mount may return (see memory
      project_nfs_host_down_hangs_systemd).
    - Plain directory (never a symlink), or a symlink whose realpath is not
      under mount_point or daemon_storage_path (e.g. an NFS-mounted
      golden_repos_dir, where os.path.realpath() does not follow NFS mounts
      and returns the mount-point path itself -- a legitimate, correctly
      reflink-capable configuration that this check cannot distinguish from
      a real misconfiguration): this is a provisioning issue that should be
      fixed (run the installer's or auto-updater's golden-repos symlink
      step), but per-user activation is the only thing that fails until then
      -- snapshot_manager itself must stay functional, so this is degraded
      to a WARNING rather than disabling snapshot_manager server-wide.
    """
    mount_point = getattr(cow_cfg, "mount_point", "") or ""
    daemon_storage_path = getattr(cow_cfg, "daemon_storage_path", "") or ""

    is_link = os.path.islink(golden_repos_dir)
    if is_link and not os.path.exists(golden_repos_dir):
        logger.warning(
            "Bug #1337: golden_repos_dir (%s) is a dangling symlink -- CoW "
            "storage unavailable; per-user activation will fail until the "
            "mount returns",
            golden_repos_dir,
        )
        return

    resolved = os.path.realpath(golden_repos_dir)
    if _under_root(resolved, mount_point) or _under_root(resolved, daemon_storage_path):
        return

    logger.warning(
        "Bug #1337: golden_repos_dir (%s) resolves to '%s', which is not "
        "under cow_daemon.mount_point (%r) or daemon_storage_path (%r). "
        "Per-user activation requires golden-repos to be a symlink into the "
        "CoW storage tree so CowDaemonBackend can translate it to a "
        "daemon-local path; per-user activation will fail until this is "
        "fixed, but snapshot_manager remains functional. Run the "
        "installer's/auto-updater's golden-repos symlink provisioning step, "
        "or manually: mv %s %s.legacy.bug1337 && "
        "ln -s <mount_point_or_daemon_storage_path>/golden-repos %s",
        golden_repos_dir,
        resolved,
        mount_point,
        daemon_storage_path,
        golden_repos_dir,
        golden_repos_dir,
        golden_repos_dir,
    )
    return


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
        If ``clone_backend == "cow-daemon"`` and the daemon is unreachable,
        or the NFS mount is unhealthy. No fallback is performed.

        NOTE (Bug #1337 staging regression fix): golden_repos_dir
        (versioned_base) not resolving under the CoW storage tree no longer
        raises here -- ``os.path.realpath()`` does not follow NFS mounts, so
        a legitimately NFS-mounted golden-repos dir would otherwise trip
        this check and disable snapshot_manager server-wide. That case now
        logs a WARNING (with remediation guidance) and this function
        proceeds, returning a working VersionedSnapshotManager. Per-user
        activation will still fail at translate time until the golden-repos
        symlink migration is done; the warning surfaces that pre-existing
        symptom rather than the check disabling snapshot_manager as a side
        effect.
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
        # Bug #1337: golden_repos_dir (versioned_base) must be a symlink into
        # the CoW storage tree, never a plain directory, so per-user
        # activation's CowDaemonBackend.create_clone_at_path() can translate
        # it to a daemon-local path.
        _check_golden_repos_symlink_placement(versioned_base, cow_cfg)

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

    return VersionedSnapshotManager(
        clone_backend=backend, versioned_base=versioned_base
    )
