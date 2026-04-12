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
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .clone_backend import CloneBackend  # pragma: no cover
    from .ontap_flexclone_client import OntapFlexCloneClient  # pragma: no cover

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
