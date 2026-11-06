"""Startup check: under all-delegated backends nothing cidx needs is on the
golden-repos volume (EVO-64616).

With every repository seam delegated -- content, git, clone, ripgrep, and the
seam selection of every enabled feature (X-Ray's ``xray_backend``, SCIP's
``scip_backend``) -- and
``index_store_backend: delegated``, the cidx pod reads repositories through
workspace-mcp / git-mcp / sandbox-mcp and serves indexes it checks out of the
IndexStore. ``<data>/golden-repos`` on the pod then holds this pod's own index
state only, and the shared golden-repos volume can come off the StatefulSet.

This module is what makes that checkable at startup:

* :func:`golden_mount_free` -- the predicate: all those backends delegated.
* :func:`pod_state_dir` -- where cidx's own server state that used to sit under
  golden-repos goes instead (the payload cache database, the snapshot-reader
  leases): the pod's data directory, beside golden-repos, never inside it.
* :func:`check_golden_mount_independence` -- REFUSES startup when a path cidx
  writes its own state to is still under golden-repos, and WARNS when the
  golden-repos directory is still a network mount (nothing needs it: it can be
  removed).

Under any other configuration every function here is inert and the local
default is unchanged.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

#: Server config fields that must all be ``delegated`` for golden-repos to be
#: pod-local index state only.
MOUNT_FREE_BACKEND_FIELDS = (
    "content_backend",
    "git_backend",
    "clone_backend",
    "ripgrep_backend",
    "index_store_backend",
)

#: Suffix of a feature's bootstrap setting that selects where it reads
#: repositories (X-Ray's ``xray_backend``, SCIP's ``scip_backend``).
FEATURE_BACKEND_SUFFIX = "_backend"

#: The one feature seam selection that reads repositories on this pod. Every
#: other value is that seam's delegated spelling (X-Ray ``delegated``, SCIP
#: ``delegated-plugin``); an unknown value refuses at the feature's startup.
IN_POD_FEATURE_BACKEND = "local"

#: Filesystem types that mean "a shared network volume is mounted here".
NETWORK_FSTYPES = frozenset({"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"})

#: Name of the pod-local state directory under the server's data directory.
POD_STATE_DIRNAME = ".pod-state"

PathLike = Union[str, "os.PathLike[str]"]


class GoldenMountDependencyError(RuntimeError):
    """Startup refused: cidx state would be written under golden-repos."""


def feature_backend_selections(config: Any) -> List[Tuple[str, Any]]:
    """``(feature.key, value)`` for every ``*_backend`` bootstrap setting of
    an enabled feature (``feature_bootstrap_settings``). A feature that
    leaves its selection unset is not listed: its own startup refuses work
    this pod cannot do (X-Ray refuses in-pod execution without the files)."""
    enabled = getattr(config, "enabled_features", None) or []
    by_feature = getattr(config, "feature_bootstrap_settings", None) or {}
    selections: List[Tuple[str, Any]] = []
    for feature in enabled:
        settings = by_feature.get(feature) if isinstance(by_feature, Mapping) else None
        if not isinstance(settings, Mapping):
            continue
        for key, value in settings.items():
            if str(key).endswith(FEATURE_BACKEND_SUFFIX):
                selections.append((f"{feature}.{key}", value))
    return selections


def golden_mount_free(config: Any) -> bool:
    """True when every repository seam, every enabled feature's seam
    selection and the IndexStore are delegated."""
    return all(
        (getattr(config, field, "local") or "local") == "delegated"
        for field in MOUNT_FREE_BACKEND_FIELDS
    ) and all(
        (value or IN_POD_FEATURE_BACKEND) != IN_POD_FEATURE_BACKEND
        for _name, value in feature_backend_selections(config)
    )


def pod_state_dir(server_data_dir: PathLike) -> Path:
    """This pod's own state directory: ``<server_data_dir>/data/.pod-state``.

    A sibling of ``golden-repos`` (``<server_data_dir>/data/golden-repos``),
    never inside it, so it is on the pod's own volume whether or not the
    golden-repos volume is mounted.
    """
    return Path(server_data_dir) / "data" / POD_STATE_DIRNAME


def payload_cache_db_path(
    config: Any, golden_repos_dir: PathLike, server_data_dir: PathLike
) -> Path:
    """The PayloadCache SQLite file.

    Local default: ``<golden_repos_dir>/.cache/payload_cache.db``, unchanged.
    Mount-free: in :func:`pod_state_dir`. Only the backend-less SQLite path
    opens it; a server with a storage backend registry keeps the entries in
    the registry database, which every pod shares, so a handle cached on one
    pod is readable on another either way.
    """
    if golden_mount_free(config):
        return pod_state_dir(server_data_dir) / "payload_cache.db"
    return Path(golden_repos_dir) / ".cache" / "payload_cache.db"


def snapshot_lease_directory(server_data_dir: PathLike) -> Path:
    """The pod-local snapshot-reader lease directory (mount-free only).

    Under a delegated IndexStore every snapshot a reader holds is a checkout on
    this pod's own disk, so the leases protecting it need be visible to this
    pod only.
    """
    return pod_state_dir(server_data_dir) / "snapshot-reader-leases"


def _is_under(path: PathLike, root: PathLike) -> bool:
    candidate = Path(os.path.abspath(os.fspath(path)))
    base = Path(os.path.abspath(os.fspath(root)))
    return candidate == base or base in candidate.parents


def _disk_partitions() -> Sequence[Any]:
    import psutil

    return list(psutil.disk_partitions(all=True))


def network_mount_of(
    path: PathLike, partitions: Optional[Iterable[Any]] = None
) -> Optional[Tuple[str, str]]:
    """``(mountpoint, fstype)`` of the most specific network mount holding
    *path*, or None. *partitions* defaults to ``psutil.disk_partitions``."""
    parts = _disk_partitions() if partitions is None else partitions
    best: Optional[Tuple[str, str]] = None
    for part in parts:
        if part.fstype not in NETWORK_FSTYPES:
            continue
        if not _is_under(path, part.mountpoint):
            continue
        if best is None or len(part.mountpoint) > len(best[0]):
            best = (part.mountpoint, part.fstype)
    return best


def check_golden_mount_independence(
    config: Any,
    golden_repos_dir: PathLike,
    state_paths: Mapping[str, PathLike],
    *,
    partitions: Optional[Iterable[Any]] = None,
) -> List[str]:
    """Verify, under all-delegated backends, that cidx needs nothing on the
    golden-repos volume. Returns the warnings it logged ([] when inert).

    Raises :class:`GoldenMountDependencyError` when any of *state_paths*
    (name -> path cidx writes its own state to) is under *golden_repos_dir*.
    A mount still present is a warning: the server runs correctly, but the
    mount is not yet removed. (A feature that still builds on this pod -- SCIP
    under ``scip_backend=local`` -- makes the configuration not mount-free,
    and this check inert.)
    """
    if not golden_mount_free(config):
        return []
    misplaced = sorted(
        f"{name}={os.fspath(path)}"
        for name, path in state_paths.items()
        if _is_under(path, golden_repos_dir)
    )
    if misplaced:
        raise GoldenMountDependencyError(
            "All repository backends and the IndexStore are delegated, so "
            f"{os.fspath(golden_repos_dir)} must hold this pod's index state "
            "only, but cidx would write its own state there: "
            + ", ".join(misplaced)
            + ". Move it to the pod's data directory."
        )
    warnings: List[str] = []
    try:
        mount = network_mount_of(golden_repos_dir, partitions)
    except Exception as exc:  # psutil missing or failing: say so, never guess
        warnings.append(
            f"could not tell whether {os.fspath(golden_repos_dir)} is a network "
            f"mount: {type(exc).__name__}: {exc}"
        )
        mount = None
    if mount is not None:
        warnings.append(
            f"{os.fspath(golden_repos_dir)} is still a network mount "
            f"({mount[1]} at {mount[0]}); with all repository backends and the "
            "IndexStore delegated cidx needs nothing on it, and it can be "
            "removed from the cidx pods."
        )
    for warning in warnings:
        logger.warning("Golden-repos mount check: %s", warning)
    if not warnings:
        logger.info(
            "Golden-repos mount check: all repository backends and the IndexStore "
            "are delegated; %s holds this pod's index state only",
            os.fspath(golden_repos_dir),
        )
    return warnings


def golden_volume_monitoring_skip_reason(config: Any) -> Optional[str]:
    """Why startup does not monitor the golden-repos volume, or None.

    Mount-free, nothing cidx needs is on it (checked at startup by
    :func:`check_golden_mount_independence`), so its health is not this
    server's to report -- and on a pod without the mount there is none.
    """
    if not golden_mount_free(config):
        return None
    return "all repository backends and the IndexStore are delegated"


def legacy_lease_sweep_skip_reason(config: Any) -> Optional[str]:
    """Why startup does not sweep the legacy lease directory, or None.

    Mount-free, reader leases are pod-local; the legacy directory is inside
    cidx-meta on the golden-repos volume and is never read.
    """
    if not golden_mount_free(config):
        return None
    return (
        "all repository backends and the IndexStore are delegated, so reader "
        "leases are pod-local"
    )


def versioned_snapshot_sweep_skip_reason(index_store: Any) -> Optional[str]:
    """Why startup does not run the `.versioned` orphan sweep, or None.

    Under a delegated IndexStore every snapshot on a pod is a checkout of a
    published version, pruned per pod by the leftover sweep and the
    served-index cache; a leader-only sweep would judge one pod's disk for the
    whole fleet (and, with the volume still mounted, the other pods').
    """
    if index_store.is_local:
        return None
    return (
        "index_store_backend='delegated' -- this pod's snapshots are IndexStore "
        "checkouts, pruned by the checkout sweep and the served-index cache"
    )
