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

import dataclasses
import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from code_indexer.server.utils.config_manager import (
        CowDaemonConfig,
    )  # pragma: no cover

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


def _findmnt_source(mount_point: str) -> Optional[str]:
    """Run ``findmnt -n -o SOURCE <mount_point>``; return trimmed stdout or None.

    None is returned (never raises) when findmnt is absent, times out, or the
    path is not a mount point at all -- all of these are legitimate "nothing
    to derive" outcomes for the Gap 1 fallback, not errors.
    """
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", mount_point],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    source = result.stdout.strip()
    return source or None


def _proc_mounts_source(
    mount_point: str, mounts_file: str = "/proc/mounts"
) -> Optional[str]:
    """Fallback when findmnt is unavailable: scan *mounts_file* for an exact
    mount_point match and return its source field.

    *mounts_file* is injectable (defaults to /proc/mounts) so tests can point
    at a fixture file instead of the real kernel mount table.
    """
    normalized = mount_point.rstrip("/")
    try:
        with open(mounts_file, "r", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == normalized:
                    return parts[0]
    except OSError:
        return None
    return None


def _derive_daemon_storage_path_from_mount(
    mount_point: str, mounts_file: str = "/proc/mounts"
) -> Optional[str]:
    """Bug #1337 Gap 1: derive cow_daemon.daemon_storage_path from the NFS
    mount source backing *mount_point*, for NFS-CLIENT nodes only.

    cow_daemon.daemon_storage_path is normally auto-detected (Bug #1320
    Part B) from the co-located CoW daemon's own config file at a fixed
    path -- but that file exists ONLY on the CoW daemon HOST. Every
    NFS-client node falls through that whole resolution chain and ends up
    with daemon_storage_path empty, so
    CowDaemonBackend._translate_to_daemon_path hard-raises the first time
    any path resolves under mount_point.

    On an NFS client, ``findmnt -n -o SOURCE <mount_point>`` (falling back
    to /proc/mounts when findmnt is unavailable) reports the export in
    ``<host>:<export_path>`` form -- the path after the host prefix IS the
    daemon's local storage root, since that is the directory the daemon
    exports and the client mounts as mount_point. The host prefix may be a
    plain hostname, an IPv4 address, or a bracketed IPv6 address (e.g.
    ``[fe80::1]:/export``) -- all three forms are supported uniformly: the
    export path is always absolute, and none of hostname / IPv4 / bracketed
    IPv6 can themselves contain '/', so deriving the export path as the
    substring from the FIRST '/' (rather than splitting on the first ':',
    which would land inside the brackets of an IPv6 host) is unambiguous
    for all of them. The presence of ':' anywhere in the source still gates
    whether this is an NFS source at all (see below).

    Returns None (not an error) when mount_point is not an NFS mount at all
    -- e.g. on the daemon host itself, where mount_point resolves to local
    XFS (or a bind mount) with no ':' in the source -- nothing to derive,
    and the existing daemon-host resolution chain remains authoritative.
    """
    if not mount_point:
        return None

    source = _findmnt_source(mount_point) or _proc_mounts_source(
        mount_point, mounts_file=mounts_file
    )
    if source is None or ":" not in source:
        return None

    slash_index = source.find("/")
    if slash_index == -1:
        return None
    return source[slash_index:]


def _resolve_effective_cow_daemon_config(
    cow_cfg: "CowDaemonConfig",
) -> "CowDaemonConfig":
    """Bug #1337 Gap 1: fill in daemon_storage_path at runtime via NFS-mount
    derivation when the existing operator-param/env/co-located-daemon-config
    chain (already applied by the shell installer into config.json) left it
    empty -- true for every NFS-client node.

    A pre-existing non-empty value ALWAYS wins (never overridden); this is a
    last-resort, runtime-only fallback so NFS clients self-configure with no
    operator action. Returns a NEW CowDaemonConfig (via dataclasses.replace)
    rather than mutating *cow_cfg* in place, so the shared server config
    object used elsewhere (logging, health checks) is unaffected.
    """
    if cow_cfg.daemon_storage_path:
        return cow_cfg

    derived = _derive_daemon_storage_path_from_mount(cow_cfg.mount_point)
    if derived is None:
        return cow_cfg

    logger.info(
        "cow_daemon.daemon_storage_path was empty; derived %r from the NFS "
        "mount source of mount_point %r (Bug #1337 Gap 1)",
        derived,
        cow_cfg.mount_point,
    )
    return dataclasses.replace(cow_cfg, daemon_storage_path=derived)


def _under_root(resolved: str, root: str) -> bool:
    """True if *resolved* equals *root* or is a path segment under it."""
    root = root.rstrip("/")
    return bool(root) and (resolved == root or resolved.startswith(root + "/"))


def _check_symlink_placement(
    dir_path: str, cow_cfg: Any, *, attr_name: str, link_name: str
) -> None:
    """Bug #1337: verify *dir_path* is placed so CowDaemonBackend can
    translate it to a daemon-local path. Generalized so the same logic backs
    both the golden-repos check (Bug #1337 original) and the activated-repos
    check (Bug #1337 Gap 2) -- both directories are reflink-clone
    destinations that must resolve under the CoW storage tree.

    Per-user activation calls CowDaemonBackend.create_clone_at_path(), which
    requires the repo bytes to resolve under cow_daemon.mount_point or
    cow_daemon.daemon_storage_path (``cp --reflink`` physically needs both
    source and dest on the daemon's local XFS). *dir_path* must therefore be
    a SYMLINK into that tree, never a plain directory.

    Two failure modes are distinguished, BOTH non-fatal (logged WARNING,
    function returns -- never raises):
    - Dangling symlink (link present, target unresolvable -- e.g. the NFS/
      CoW host is transiently down): the mount may return (see memory
      project_nfs_host_down_hangs_systemd).
    - Plain directory (never a symlink), or a symlink whose realpath is not
      under mount_point or daemon_storage_path (e.g. an NFS-mounted
      dir_path, where os.path.realpath() does not follow NFS mounts and
      returns the mount-point path itself -- a legitimate, correctly
      reflink-capable configuration that this check cannot distinguish from
      a real misconfiguration): this is a provisioning issue that should be
      fixed (run the installer's or auto-updater's symlink provisioning
      step), but per-user activation is the only thing that fails until then
      -- snapshot_manager itself must stay functional, so this is degraded
      to a WARNING rather than disabling snapshot_manager server-wide.

    Parameters
    ----------
    attr_name:
        Config-attribute-style label used in log text (e.g.
        ``"golden_repos_dir"`` or ``"activated_repos_dir"``).
    link_name:
        On-disk directory/symlink name used in remediation commands (e.g.
        ``"golden-repos"`` or ``"activated-repos"``).
    """
    mount_point = getattr(cow_cfg, "mount_point", "") or ""
    daemon_storage_path = getattr(cow_cfg, "daemon_storage_path", "") or ""

    is_link = os.path.islink(dir_path)
    if is_link and not os.path.exists(dir_path):
        logger.warning(
            "Bug #1337: %s (%s) is a dangling symlink -- CoW "
            "storage unavailable; per-user activation will fail until the "
            "mount returns",
            attr_name,
            dir_path,
        )
        return

    resolved = os.path.realpath(dir_path)
    if _under_root(resolved, mount_point) or _under_root(resolved, daemon_storage_path):
        return

    logger.warning(
        "Bug #1337: %s (%s) resolves to '%s', which is not "
        "under cow_daemon.mount_point (%r) or daemon_storage_path (%r). "
        "Per-user activation requires %s to be a symlink into the "
        "CoW storage tree so CowDaemonBackend can translate it to a "
        "daemon-local path; per-user activation will fail until this is "
        "fixed, but snapshot_manager remains functional. Run the "
        "installer's/auto-updater's %s symlink provisioning step, "
        "or manually: mv %s %s.legacy.bug1337 && "
        "ln -s <mount_point_or_daemon_storage_path>/%s %s",
        attr_name,
        dir_path,
        resolved,
        mount_point,
        daemon_storage_path,
        link_name,
        link_name,
        dir_path,
        dir_path,
        link_name,
        dir_path,
    )
    return


def _check_golden_repos_symlink_placement(golden_repos_dir: str, cow_cfg: Any) -> None:
    """Bug #1337: verify golden_repos_dir is placed so CowDaemonBackend can
    translate it to a daemon-local path. See _check_symlink_placement for
    the full rationale (shared with activated-repos, Bug #1337 Gap 2).
    """
    _check_symlink_placement(
        golden_repos_dir,
        cow_cfg,
        attr_name="golden_repos_dir",
        link_name="golden-repos",
    )


def _check_activated_repos_symlink_placement(
    activated_repos_dir: str, cow_cfg: Any
) -> None:
    """Bug #1337 Gap 2: verify activated_repos_dir is placed so
    CowDaemonBackend can translate it to a daemon-local path.

    Per-user activation reflink-clones INTO activated_repos_dir (Bug #1052),
    which must ALSO be a symlink into the CoW storage for the same reason
    golden_repos_dir must be -- see _check_symlink_placement.
    """
    _check_symlink_placement(
        activated_repos_dir,
        cow_cfg,
        attr_name="activated_repos_dir",
        link_name="activated-repos",
    )


def build_snapshot_manager(
    config: Any, versioned_base: str, activated_repos_dir: Optional[str] = None
) -> Any:
    """Build a VersionedSnapshotManager configured from *config*.

    Parameters
    ----------
    config:
        Server config object (ServerConfig). Must have a ``clone_backend``
        field (str: "local", "ontap", or "cow-daemon") and optional
        ``cow_daemon`` / ``ontap`` sub-configs.
    versioned_base:
        Base directory for filesystem CoW snapshots and the LocalCloneBackend.
    activated_repos_dir:
        Optional path to the activated-repos directory. When provided AND
        ``clone_backend == "cow-daemon"``, its placement is validated the
        same way as golden_repos_dir (Bug #1337 Gap 2) -- WARNING only,
        never raises. Defaults to None (skip the check) for backward
        compatibility with existing callers/tests that only pass
        versioned_base.

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
        # Bug #1337 Gap 1: self-configure daemon_storage_path from the NFS
        # mount source when the shell installer's resolution chain (operator
        # flag / env var / co-located daemon config, daemon-HOST only) left
        # it empty -- true for every NFS-client node.
        cow_cfg = _resolve_effective_cow_daemon_config(config.cow_daemon)
        # Fail-fast: validate daemon and NFS before constructing anything
        _check_daemon_health(cow_cfg.daemon_url)
        _check_nfs_mount(cow_cfg.mount_point)
        # Bug #1337: golden_repos_dir (versioned_base) must be a symlink into
        # the CoW storage tree, never a plain directory, so per-user
        # activation's CowDaemonBackend.create_clone_at_path() can translate
        # it to a daemon-local path.
        _check_golden_repos_symlink_placement(versioned_base, cow_cfg)
        # Bug #1337 Gap 2: activated_repos_dir needs the identical placement
        # guarantee (per-user activation reflink-clones INTO it, Bug #1052).
        # Optional param -- only checked when the caller supplies it.
        if activated_repos_dir:
            _check_activated_repos_symlink_placement(activated_repos_dir, cow_cfg)

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
