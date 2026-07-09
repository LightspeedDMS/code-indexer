"""
Versioned snapshot manager for CIDX golden repositories.

Creates and deletes versioned snapshots of already-indexed base clones using
either ONTAP FlexClone (when an :class:`OntapFlexCloneClient` is supplied) or
filesystem Copy-on-Write via ``cp --reflink=auto`` (standalone / no-ONTAP mode).

Architecture
------------
- FlexClone mode: each snapshot is a zero-cost ONTAP volume clone.  The clone
  is named ``cidx_clone_{alias}_{timestamp}`` and mounted at
  ``{mount_point}/cidx_clone_{alias}_{timestamp}``.
- Filesystem CoW mode: each snapshot is a directory created by
  ``cp --reflink=auto -a {source_path} {versioned_path}``.  The path follows
  the existing convention: ``{versioned_base}/.versioned/{alias}/v_{timestamp}``.

In both modes, :meth:`create_snapshot` returns the filesystem path to the new
snapshot so that callers can swap alias JSON ``target_path`` without knowing
which storage backend was used.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Tuple

from .snapshot_paths import is_versioned_snapshot as _is_versioned_snapshot

if TYPE_CHECKING:
    from .clone_backend import CloneBackend  # pragma: no cover
    from .ontap_flexclone_client import OntapFlexCloneClient  # pragma: no cover

#: Matches a ``v_<unix_ts>`` snapshot leaf name and captures the timestamp.
_V_TIMESTAMP_CAPTURE_RE = re.compile(r"^v_(\d+)$")

logger = logging.getLogger(__name__)

#: Default timeout in seconds for the filesystem CoW ``cp`` command.
_DEFAULT_COW_TIMEOUT = 600


class VersionedSnapshotManager:
    """Manages versioned snapshots using FlexClone, filesystem CoW, or a CloneBackend.

    Parameters
    ----------
    flexclone_client:
        Optional :class:`OntapFlexCloneClient` instance.  When supplied (and
        ``clone_backend`` is ``None``), snapshot creation and deletion use
        ONTAP FlexClone volumes.  When ``None``, falls back to
        ``cp --reflink=auto`` (standalone mode).
    mount_point:
        Filesystem path where ONTAP FlexClone volumes are mounted, e.g.
        ``"/mnt/fsx"``.  Only used in FlexClone mode.
    versioned_base:
        Base directory under which filesystem CoW snapshots are stored in the
        ``.versioned/{alias}/v_{timestamp}`` hierarchy.  Only used in CoW mode.
        Defaults to an empty string — callers must supply a value when operating
        in CoW mode.
    cow_timeout:
        Maximum seconds allowed for the ``cp --reflink=auto`` command before a
        :exc:`subprocess.TimeoutExpired` is raised.  Defaults to 600 (10 min).
    clone_backend:
        Optional :class:`CloneBackend` instance (Story #510 AC7).  When
        supplied, ``create_snapshot`` and ``delete_snapshot`` delegate entirely
        to this backend, bypassing both ``flexclone_client`` and the local CoW
        path.  When ``None``, the existing FlexClone / CoW selection logic is
        used unchanged.
    """

    def __init__(
        self,
        flexclone_client: Optional["OntapFlexCloneClient"] = None,
        mount_point: str = "/mnt/fsx",
        versioned_base: str = "",
        cow_timeout: int = _DEFAULT_COW_TIMEOUT,
        clone_backend: Optional["CloneBackend"] = None,
    ) -> None:
        self._flexclone = flexclone_client
        self._mount_point = mount_point.rstrip("/")
        self._versioned_base = versioned_base
        self._cow_timeout = cow_timeout
        self._clone_backend = clone_backend

    # ------------------------------------------------------------------
    # Mode helpers
    # ------------------------------------------------------------------

    @property
    def uses_flexclone(self) -> bool:
        """``True`` when FlexClone is the active storage backend."""
        return self._flexclone is not None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_snapshot(self, alias: str, source_path: str) -> str:
        """Create a versioned snapshot of *source_path* for *alias*.

        Parameters
        ----------
        alias:
            Repository alias name, e.g. ``"my-repo"``.
        source_path:
            Filesystem path to the already-indexed base clone directory.
            Only used in CoW (filesystem) mode; ignored in FlexClone mode
            because ONTAP clones the parent volume directly.

        Returns
        -------
        str
            Absolute filesystem path to the new snapshot directory/mount.

        Raises
        ------
        subprocess.CalledProcessError
            CoW mode only: ``cp`` exited with a non-zero status.
        subprocess.TimeoutExpired
            CoW mode only: ``cp`` did not complete within :attr:`cow_timeout`.
        RuntimeError
            FlexClone mode only: clone creation did not return a usable path.
        """
        timestamp = int(time.time())

        if self._clone_backend is not None:
            return self._clone_backend.create_clone(
                source_path, alias, f"v_{timestamp}"
            )

        if self._flexclone is not None:
            return self._create_flexclone_snapshot(alias, timestamp)
        return self._create_cow_snapshot(alias, source_path, timestamp)

    def delete_snapshot(self, alias: str, version_path: str) -> bool:
        """Delete a versioned snapshot.

        Parameters
        ----------
        alias:
            Repository alias name.  Used in FlexClone mode to derive the clone
            name from *version_path*.
        version_path:
            Filesystem path to the snapshot that should be deleted.  In CoW
            mode this is a directory that is removed with ``shutil.rmtree``.
            In FlexClone mode the clone name is derived from the path's
            basename (which equals the clone volume name).

        Returns
        -------
        bool
            ``True`` on success.  In FlexClone mode, ``True`` is also returned
            when the volume is already gone (idempotent).

        Raises
        ------
        RuntimeError
            If deletion fails unexpectedly.
        """
        if self._clone_backend is not None:
            return self._clone_backend.delete_clone(version_path)

        if self._flexclone is not None:
            return self._delete_flexclone_snapshot(version_path)
        return self._delete_cow_snapshot(version_path)

    def get_snapshot_path(self, alias: str, timestamp: str) -> str:
        """Compute the expected filesystem path for a snapshot.

        Parameters
        ----------
        alias:
            Repository alias name.
        timestamp:
            Integer timestamp string used to identify the snapshot version,
            e.g. ``"1700000000"``.

        Returns
        -------
        str
            Expected absolute filesystem path for the snapshot.
        """
        if self._flexclone is not None:
            clone_name = f"cidx_clone_{alias}_{timestamp}"
            return f"{self._mount_point}/{clone_name}"
        # CoW / filesystem mode
        return str(Path(self._versioned_base) / ".versioned" / alias / f"v_{timestamp}")

    # ------------------------------------------------------------------
    # Bug #1084 Phase A: canonical predicate facade + discovery API
    # ------------------------------------------------------------------

    def _backend_mount_point(self) -> Optional[str]:
        """Return the active backend's mount point, if it has one.

        Used so :meth:`is_versioned_snapshot` can recognize the legacy
        cow-daemon / flat-ONTAP transition shapes (which require the mount
        point). LocalCloneBackend and CoW-filesystem mode have no mount point —
        only the canonical ``.versioned`` shape applies there.
        """
        backend = self._clone_backend
        if backend is not None:
            mount = getattr(backend, "_mount_point", None)
            if mount:
                return str(mount)
        if self._flexclone is not None:
            return self._mount_point
        return None

    def is_versioned_snapshot(self, path: str) -> bool:
        """Return ``True`` when *path* is a versioned snapshot (Bug #1084).

        Delegates to the single canonical predicate in :mod:`snapshot_paths`,
        supplying the backend mount point so legacy-shape snapshots are still
        recognized during the transition window.
        """
        return _is_versioned_snapshot(path, mount_point=self._backend_mount_point())

    def list_snapshots(self, alias: str) -> List[Tuple[str, int]]:
        """Return ``[(snapshot_path, unix_ts), ...]`` for *alias*, sorted ascending.

        *alias* may be the global alias (``"repo-global"``) or the bare repo
        name; the ``-global`` suffix is stripped to obtain the namespace.

        Backend behaviour:
        - **cow-daemon:** ``list_clones(sanitized_ns)`` then map each ``v_*``
          clone to its CANONICAL ``{mount}/.versioned/{ns}/v_<ts>`` path — the
          same shape ``create_clone`` writes and alias ``target_path`` /
          ``previous_path`` carry. Pre-migration LEGACY snapshots
          (``{mount}/{ns}/v_<ts>``) are intentionally NOT listed; see
          :meth:`_list_cow_daemon_snapshots` for the transition rationale.
        - **local CloneBackend / CoW-filesystem mode:** glob
          ``{versioned_base}/.versioned/{ns}/v_*``.
        - **ONTAP / FlexClone:** returns ``[]`` — ``list_clones`` ignores the
          namespace, so per-alias retention is impossible (spec section 6);
          per-swap deletion is unaffected.
        """
        namespace = alias.removesuffix("-global")
        backend = self._clone_backend

        if backend is not None:
            backend_cls = type(backend).__name__
            if backend_cls == "CowDaemonBackend":
                return self._list_cow_daemon_snapshots(backend, namespace)
            if backend_cls == "LocalCloneBackend":
                return self._list_local_snapshots(namespace)
            # OntapCloneBackend (and any future namespace-blind backend): disabled.
            return []

        # No CloneBackend wired.
        if self._flexclone is not None:
            # FlexClone mode: list_clones ignores namespace — retention disabled.
            return []
        # CoW filesystem mode: glob versioned_base.
        return self._list_local_snapshots(namespace)

    def latest_snapshot(self, alias: str) -> Optional[str]:
        """Return the path of the newest snapshot for *alias*, or ``None``."""
        snaps = self.list_snapshots(alias)
        if not snaps:
            return None
        return snaps[-1][0]

    def _list_local_snapshots(self, namespace: str) -> List[Tuple[str, int]]:
        """Glob ``{versioned_base}/.versioned/{namespace}/v_*`` for snapshots."""
        if not self._versioned_base:
            return []
        ns_dir = Path(self._versioned_base) / ".versioned" / namespace
        if not ns_dir.exists():
            return []
        result: List[Tuple[str, int]] = []
        for entry in ns_dir.iterdir():
            if not entry.is_dir():
                continue
            match = _V_TIMESTAMP_CAPTURE_RE.match(entry.name)
            if match:
                result.append((str(entry), int(match.group(1))))
        result.sort(key=lambda item: item[1])
        return result

    def _list_cow_daemon_snapshots(
        self, backend: "CloneBackend", namespace: str
    ) -> List[Tuple[str, int]]:
        """List cow-daemon snapshots, mapping daemon clones to CANONICAL CIDX paths.

        The daemon namespace is the dots->underscores sanitized form of the repo
        namespace; a single ``list_clones`` call covers every ``v_*`` clone the
        daemon knows about under that namespace.

        Path shape (Bug #1084 review fix)
        ---------------------------------
        Each clone maps to the CANONICAL snapshot path
        ``{mount}/.versioned/{ns}/v_<ts>`` — the SAME shape that
        :meth:`CowDaemonBackend.create_clone` writes and that alias
        ``target_path`` / ``previous_path`` carry. Emitting the canonical shape is
        what makes the two path-consuming discovery clients correct on cow-daemon:

        * Retention (``RefreshScheduler._enforce_retention``) force-protects the
          current ``target_path`` and the rollback ``previous_path`` by STRING
          equality against this list. Canonical-vs-canonical now matches, so the
          ``previous`` snapshot is never scheduled for deletion (AC10).
        * Defect-E restore (``RefreshScheduler._restore_master_from_versioned``)
          uses :meth:`latest_snapshot` as the reverse-clone ``cp`` source; the
          canonical path actually exists on disk under the NFS mount.

        :meth:`CowDaemonBackend.delete_clone` already strips the leading
        ``.versioned`` segment before deriving ``(namespace, name)``, so scheduled
        deletions of these canonical paths still resolve to the correct daemon
        clone identity.

        Transition (pre-migration legacy snapshots are intentionally NOT listed)
        -----------------------------------------------------------------------
        Snapshots created before this convention live at the LEGACY shape
        ``{mount}/{ns}/v_<ts>`` (no ``.versioned`` segment). They are deliberately
        excluded from discovery here: dual-listing both shapes would double-count
        and make ``(ns, name)`` ambiguous. The legacy backlog is reclaimed by two
        other mechanisms, not by this discovery API:

        * the swap-gate predicate's legacy clause cleans the superseded legacy
          ``current`` on the first post-deploy refresh, and
        * the one-time AC12 post-deploy purge clears the historical legacy backlog.

        Retention therefore governs the CANONICAL era only — exactly the snapshots
        this method now returns.
        """
        sanitize = getattr(backend, "_sanitize_identifier", None)
        sanitized_ns = sanitize(namespace) if callable(sanitize) else namespace
        mount = str(getattr(backend, "_mount_point", "")).rstrip("/")

        try:
            clones = backend.list_clones(sanitized_ns)
        except Exception as exc:  # pragma: no cover - network/daemon failure path
            logger.warning(
                "list_snapshots: cow-daemon list_clones failed for ns '%s': %s",
                sanitized_ns,
                exc,
            )
            return []

        result: List[Tuple[str, int]] = []
        for clone in clones:
            name = clone.get("name", "")
            match = _V_TIMESTAMP_CAPTURE_RE.match(name)
            if not match:
                continue
            clone_ns = clone.get("namespace", sanitized_ns)
            # CANONICAL shape — identical to what CowDaemonBackend.create_clone
            # writes and to the alias target_path/previous_path strings.
            snapshot_path = f"{mount}/.versioned/{clone_ns}/{name}"
            result.append((snapshot_path, int(match.group(1))))
        result.sort(key=lambda item: item[1])
        return result

    # ------------------------------------------------------------------
    # FlexClone implementation
    # ------------------------------------------------------------------

    def _create_flexclone_snapshot(self, alias: str, timestamp: int) -> str:
        """Create a FlexClone volume and return its mount path."""
        assert self._flexclone is not None  # guarded by caller

        clone_name = f"cidx_clone_{alias}_{timestamp}"
        junction_path = f"/{clone_name}"

        logger.info(
            "Creating FlexClone snapshot for alias '%s' as '%s'",
            alias,
            clone_name,
        )

        self._flexclone.create_clone(clone_name, junction_path=junction_path)

        mount_path = f"{self._mount_point}/{clone_name}"
        logger.info("FlexClone snapshot ready at '%s'", mount_path)
        return mount_path

    def _delete_flexclone_snapshot(self, version_path: str) -> bool:
        """Delete a FlexClone volume identified by the basename of *version_path*."""
        assert self._flexclone is not None  # guarded by caller

        # The clone name is the final path component, e.g. "cidx_clone_myrepo_1700000000"
        clone_name = Path(version_path).name
        logger.info(
            "Deleting FlexClone snapshot '%s' (path='%s')",
            clone_name,
            version_path,
        )
        return self._flexclone.delete_clone(clone_name)

    # ------------------------------------------------------------------
    # Filesystem CoW implementation
    # ------------------------------------------------------------------

    def _create_cow_snapshot(self, alias: str, source_path: str, timestamp: int) -> str:
        """Create a CoW directory snapshot using ``cp --reflink=auto``."""
        versioned_path = (
            Path(self._versioned_base) / ".versioned" / alias / f"v_{timestamp}"
        )
        versioned_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(
            "Creating CoW snapshot for alias '%s' at '%s' from '%s'",
            alias,
            versioned_path,
            source_path,
        )

        subprocess.run(
            ["cp", "--reflink=auto", "-a", source_path, str(versioned_path)],
            check=True,
            capture_output=True,
            timeout=self._cow_timeout,
        )

        logger.info("CoW snapshot created at '%s'", versioned_path)
        return str(versioned_path)

    def _delete_cow_snapshot(self, version_path: str) -> bool:
        """Remove a CoW snapshot directory tree."""
        import shutil  # noqa: PLC0415

        path = Path(version_path)
        if not path.exists():
            logger.info(
                "CoW snapshot '%s' does not exist — treating as already deleted",
                version_path,
            )
            return True

        logger.info("Deleting CoW snapshot at '%s'", version_path)
        shutil.rmtree(str(path))
        logger.info("CoW snapshot deleted: '%s'", version_path)
        return True
