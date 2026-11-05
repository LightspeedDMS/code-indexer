"""
Clone backend wiring helper for CIDX server startup.

Story #510 AC8 — builds a VersionedSnapshotManager configured with the
appropriate CloneBackend based on server config, then makes it available
for injection into golden-repo lifecycle services.

The backend for the configured mode is a provider on the clone seam's
entry-point group, reached through the seam resolver
(``server/services/clone_backend_plugin.py``); this module validates the
deployment for that mode first and hands the provider its settings.

Fail-fast policy:
- cow-daemon: daemon health check AND NFS mount check MUST pass.
  If either fails, raise RuntimeError immediately — NO fallback.
- delegated: workspace-mcp must be configured and answer its health check.
- ontap: no extra validation (existing ONTAP path is unchanged).
- local: no external validation needed.
- anything else: RuntimeError (it used to become ``local`` silently).
- no module installed for the mode: ``SeamUnavailableError`` naming the seam.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from code_indexer.server.utils.config_manager import (
        CowDaemonConfig,
    )  # pragma: no cover

logger = logging.getLogger(__name__)


_MIN_DAEMON_VERSION = "0.2.0"


#: How long startup waits for workspace-mcp to answer its health check before
#: giving up on the delegated clone backend (env override, seconds; 0: once).
WORKSPACE_MCP_STARTUP_WAIT_ENV = "CIDX_WORKSPACE_MCP_STARTUP_WAIT_SECONDS"
DEFAULT_WORKSPACE_MCP_STARTUP_WAIT_SECONDS = 90.0
_WORKSPACE_MCP_HEALTH_RETRY_DELAY_SECONDS = 5.0


def _workspace_mcp_startup_wait() -> float:
    raw = os.environ.get(WORKSPACE_MCP_STARTUP_WAIT_ENV)
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        value = None
    if value is None or value < 0:
        return DEFAULT_WORKSPACE_MCP_STARTUP_WAIT_SECONDS
    return value


def _http_status(exc: BaseException, requests_module: Any) -> Optional[int]:
    """The HTTP status of a ``raise_for_status`` failure, else None."""
    if not isinstance(exc, requests_module.exceptions.HTTPError):
        return None
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _is_transient_health_failure(exc: BaseException, requests_module: Any) -> bool:
    """Worth retrying while workspace-mcp starts: it could not be reached
    (connection refused, DNS, timeout) or answered 5xx (starting, or its
    gateway is). A 4xx or any other error is an answer, not a delay."""
    status = _http_status(exc, requests_module)
    if status is not None:
        return status >= 500
    exceptions = requests_module.exceptions
    return isinstance(exc, (exceptions.ConnectionError, exceptions.Timeout))


def _check_workspace_mcp_health(
    endpoint: str,
    api_key: str,
    *,
    wait_seconds: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Verify workspace-mcp is reachable before wiring the delegated backend.

    EVO-64616: the ``delegated`` clone backend forwards every activated-repo
    CoW clone to workspace-mcp. If workspace-mcp is unreachable at startup we
    fail fast — there is NO silent fallback to a local cidx-side copy (that
    would reintroduce the very filesystem work delegation exists to remove,
    EVO-64615). GETs ``{endpoint}/clone-path/health``.

    Only a transient failure (connection/timeout/5xx,
    :func:`_is_transient_health_failure`) is retried, for up to *wait_seconds* (default
    ``CIDX_WORKSPACE_MCP_STARTUP_WAIT_SECONDS``, 90): the snapshot manager is
    built ONCE, so a pod that starts while workspace-mcp is restarting (a
    rollout of both) stayed without a clone backend until its own restart --
    every rename, activation and orphan prune on it refused, for hours, while
    its siblings worked (EVO-64616, tools-test claude-code-indexer-1).
    """
    import requests

    health_url = f"{endpoint.rstrip('/')}/clone-path/health"
    logger.info("Checking workspace-mcp health at %s", health_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    budget = _workspace_mcp_startup_wait() if wait_seconds is None else wait_seconds
    deadline = clock() + budget
    while True:
        try:
            resp = requests.get(health_url, headers=headers, timeout=10)
            resp.raise_for_status()
            break
        except Exception as exc:
            status = _http_status(exc, requests)
            if status is not None and status < 500:
                # Reachable, and refusing: a wrong api_key (401/403) or
                # endpoint path (404) will not fix itself -- fail now, and
                # say so instead of "not reachable" after the whole wait.
                raise RuntimeError(
                    f"workspace-mcp at {endpoint} answered its health check "
                    f"with HTTP {status}: check workspace_mcp.api_key and "
                    f"workspace_mcp.endpoint. Error: {exc}"
                ) from exc
            if not _is_transient_health_failure(exc, requests):
                raise RuntimeError(
                    f"workspace-mcp not reachable at {endpoint} for the delegated "
                    f"clone backend. Ensure workspace-mcp is running and "
                    f"CIDX_CLONE_BACKEND / workspace_mcp.endpoint is correct. "
                    f"Error: {exc}"
                ) from exc
            if clock() + _WORKSPACE_MCP_HEALTH_RETRY_DELAY_SECONDS > deadline:
                raise RuntimeError(
                    f"workspace-mcp not reachable at {endpoint} for the delegated "
                    f"clone backend (waited {budget:.0f}s). Ensure workspace-mcp "
                    f"is running and CIDX_CLONE_BACKEND / workspace_mcp.endpoint "
                    f"is correct. Error: {exc}"
                ) from exc
            logger.warning(
                "workspace-mcp health check failed (%s); retrying in %.0fs",
                type(exc).__name__,
                _WORKSPACE_MCP_HEALTH_RETRY_DELAY_SECONDS,
            )
            sleep(_WORKSPACE_MCP_HEALTH_RETRY_DELAY_SECONDS)
    logger.info("workspace-mcp health check: OK")


def _check_daemon_health(daemon_url: str) -> None:
    """Verify CoW daemon is reachable via GET /api/v1/health and meets minimum version.

    Raises RuntimeError if the daemon does not return HTTP 200, or if the reported
    version is absent or older than 0.2.0.
    """
    import requests
    from packaging.version import parse as parse_version

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
    from code_indexer.server.storage.shared.nfs_validator import NfsMountValidator

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
        field (str: "local", "ontap", "cow-daemon" or "delegated") and
        optional ``cow_daemon`` / ``ontap`` / ``workspace_mcp`` sub-configs.
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
        If ``clone_backend`` is not a known mode, or is ``"cow-daemon"`` and
        the daemon is unreachable or the NFS mount is unhealthy, or is
        ``"delegated"`` and workspace-mcp is unconfigured or unreachable. No
        fallback is performed.
    SeamUnavailableError
        If no installed module (or more than one) provides the mode.

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
    from cidx_sdk.seams.clone import VALID_CLONE_BACKENDS, CloneBackendSettings

    from code_indexer.server.services.clone_backend_plugin import (
        resolve_clone_backend,
    )
    from code_indexer.server.storage.shared.clone_backend import core_clone_services
    from code_indexer.server.storage.shared.snapshot_manager import (
        VersionedSnapshotManager,
    )

    backend_type = getattr(config, "clone_backend", "local") or "local"
    logger.info("Building VersionedSnapshotManager with clone_backend=%r", backend_type)
    if backend_type not in VALID_CLONE_BACKENDS:
        # Fail loud: an unrecognised value used to become "local" silently,
        # cloning on this pod's disk when the operator asked for something
        # else (a typo of "cow-daemon" or "delegated").
        raise RuntimeError(
            f"Unknown clone_backend {backend_type!r}. "
            f"Valid values are {VALID_CLONE_BACKENDS}."
        )

    # What the configured mode's provider receives. Each provider is reached
    # through the seam resolver by the mode's entry name
    # (code_indexer.clone_backend); none installed for the mode refuses,
    # naming the seam -- there is no built-in fallback.
    settings = CloneBackendSettings(
        services=core_clone_services(), versioned_base=versioned_base
    )

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
        settings = dataclasses.replace(settings, cow_daemon=cow_cfg)
        detail = f"daemon={cow_cfg.daemon_url}, mount={cow_cfg.mount_point}"

    elif backend_type == "delegated":
        # EVO-64616: delegate the activated-repo CoW clone to workspace-mcp so
        # cidx performs NO filesystem clone itself (EVO-64615). Fail-fast on an
        # unreachable workspace-mcp -- NO silent local fallback. cidx does not
        # translate paths here (unlike cow-daemon); workspace-mcp owns reflink
        # placement, so the golden/activated symlink-placement checks do not
        # apply to this backend.
        wsm_cfg = config.workspace_mcp
        if wsm_cfg is None or not wsm_cfg.endpoint:
            raise RuntimeError(
                "clone_backend='delegated' requires workspace_mcp.endpoint to be "
                "configured (the workspace-mcp base URL). None was provided."
            )
        _check_workspace_mcp_health(wsm_cfg.endpoint, wsm_cfg.api_key)
        settings = dataclasses.replace(settings, endpoint=wsm_cfg)
        detail = f"workspace-mcp={wsm_cfg.endpoint}"

    elif backend_type == "ontap":
        settings = dataclasses.replace(settings, ontap=config.ontap)
        detail = f"mount={config.ontap.mount_point}"

    else:
        detail = f"versioned_base={versioned_base}"

    backend = resolve_clone_backend(backend_type, settings)
    logger.info(
        "VersionedSnapshotManager: using %s for clone_backend=%r (%s)",
        type(backend).__name__,
        backend_type,
        detail,
    )

    return VersionedSnapshotManager(
        clone_backend=backend, versioned_base=versioned_base
    )
