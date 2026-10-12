"""Control-plane metadata: golden-repo alias pointers and write-mode markers.

Content seam step 5a (docs/content-seam-remaining-consumers.md). Aliases
(``golden-repos/aliases/<name>.json``) and write-mode markers
(``golden-repos/.write_mode/<name>.json``) are cidx's own control-plane state.
They used to be files on the repository volume that phase 6 unmounts; they now
live in the server's registry (SQLite solo, PostgreSQL cluster).

Every reader and writer goes through ONE component, :class:`ControlPlaneMetadata`,
reached via :func:`metadata_for` -- ``AliasManager`` and ``WriteModeMarkers``
are thin, kind-specific views over it. That component decides where a read
comes from and where a write goes:

* No registry installed (the CLI, unit tests, a process that never ran
  ``service_init``): the files, exactly as before.
* ``metadata_read_source=registry`` (default): reads come from the registry;
  writes go to the registry, and -- while ``metadata_file_dual_write`` is on --
  to the old files too, so a rollback can read them.
* ``metadata_read_source=files`` (rollback): reads come from the files; a write
  goes to the file FIRST and the registry second, synchronously, so a
  rolled-back server reads its own writes.

Migration: :meth:`ControlPlaneMetadata.import_files` copies the existing files
into the registry, idempotently, never letting an older file clobber a newer
registry value, and never bringing back a key deleted after the file was
written (the delete's tombstone). Until this process's import has finished
WITHOUT a failure, a registry miss falls back to the file, so a first boot on
the new image serves every alias it served before.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from code_indexer.server.storage.protocols.control_plane_metadata_backend import (
    KIND_ALIAS,
    KIND_WRITE_MODE,
    METADATA_TABLES,
    ControlPlaneMetadataBackend,
    format_updated_at,
)
from code_indexer.utils.file_locking import nfs_safe_fsync

logger = logging.getLogger(__name__)

__all__ = [
    "KIND_ALIAS",
    "KIND_WRITE_MODE",
    "ControlPlaneMetadata",
    "MetadataFiles",
    "MetadataSettings",
    "install_control_plane_metadata",
    "installed_control_plane_metadata",
    "metadata_for",
    "pointer_from_document",
    "uninstall_control_plane_metadata",
]

#: Directory (under golden-repos) that held each kind's files.
METADATA_FILE_DIRS: Dict[str, str] = {
    KIND_ALIAS: "aliases",
    KIND_WRITE_MODE: ".write_mode",
}

READ_SOURCE_REGISTRY = "registry"
READ_SOURCE_FILES = "files"

#: The timestamp an undated document imports with: older than any real write,
#: so it fills an empty registry slot and never replaces a stored value.
_UNDATED = "0000-00-00T00:00:00.000000Z"

#: Document fields that date each kind, newest wins.
_TIMESTAMP_FIELDS: Dict[str, Tuple[str, ...]] = {
    KIND_ALIAS: ("created_at", "last_refresh", "swapped_at"),
    KIND_WRITE_MODE: ("entered_at",),
}

#: Seconds to wait before each retry of an import that had failures. The
#: read-through to the files stays on until an attempt has none.
IMPORT_RETRY_DELAYS_SECONDS: Tuple[float, ...] = (5.0, 30.0, 120.0)

PathLike = Union[str, "os.PathLike[str]"]


@dataclass(frozen=True)
class MetadataSettings:
    """The two runtime switches this component reads on every call."""

    read_source: str = READ_SOURCE_REGISTRY
    file_dual_write: bool = True


SettingsProvider = Callable[[], MetadataSettings]


def _default_settings() -> MetadataSettings:
    return MetadataSettings()


def pointer_from_document(
    document: Optional[Dict[str, Any]],
) -> Optional[Tuple[str, Optional[str]]]:
    """``(target_path, previous_path)`` from one alias document, both read
    from the SAME dict. None for a missing document or one with no usable
    target_path (Bug #1567 Gap 1: never two separate reads)."""
    if not isinstance(document, dict):
        return None
    target_path = document.get("target_path")
    if not isinstance(target_path, str) or not target_path:
        return None
    previous_path = document.get("previous_path")
    if not isinstance(previous_path, str) or not previous_path:
        previous_path = None
    return target_path, previous_path


def document_timestamp(kind: str, document: Dict[str, Any]) -> str:
    """The newest date a document carries, in the registry's updated_at
    format; ``_UNDATED`` when it carries none that parses."""
    newest: Optional[str] = None
    for field_name in _TIMESTAMP_FIELDS.get(kind, ()):
        raw = document.get(field_name)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        stamp = format_updated_at(parsed)
        if newest is None or stamp > newest:
            newest = stamp
    return newest if newest is not None else _UNDATED


def _normalized(path: PathLike) -> str:
    """A comparable spelling of *path* with no filesystem access."""
    return os.path.normpath(os.path.abspath(os.fspath(path)))


class MetadataFiles:
    """The file representation: one JSON document per key under golden-repos.

    The ONE implementation of reading, listing, writing and deleting these
    files -- the files-only mode, the rollback read source, dual-write and the
    startup import all use it.
    """

    def __init__(self, golden_repos_dir: PathLike) -> None:
        self.golden_repos_dir = Path(golden_repos_dir)

    def dir_for(self, kind: str) -> Path:
        return self.golden_repos_dir / METADATA_FILE_DIRS[kind]

    def path_for(self, kind: str, name: str) -> Path:
        return self.dir_for(kind) / f"{name}.json"

    @staticmethod
    def encode(document: Dict[str, Any]) -> bytes:
        """The exact bytes a document is written as."""
        return json.dumps(document, indent=2).encode("utf-8")

    @staticmethod
    def _decode_file(path: Path) -> Optional[Dict[str, Any]]:
        try:
            with open(path, "r") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            if not isinstance(exc, FileNotFoundError):
                logger.warning("Unreadable metadata file %s: %s", path, exc)
            return None
        return data if isinstance(data, dict) else None

    def read(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        """The document for *name*, or None if missing or unreadable."""
        return self._decode_file(self.path_for(kind, name))

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        """Every readable document whose name starts with *prefix*.

        A missing directory lists as empty; an unreadable file is simply
        absent. Raises OSError only if the directory itself cannot be listed.
        """
        documents: Dict[str, Dict[str, Any]] = {}
        for path in sorted(self.dir_for(kind).glob(f"{prefix}*.json")):
            document = self._decode_file(path)
            if document is not None:
                documents[path.stem] = document
        return documents

    def names(self, kind: str) -> List[str]:
        """Every key with a file, readable or not (a corrupt file is still a
        key someone has to clean up)."""
        return sorted(path.stem for path in self.dir_for(kind).glob("*.json"))

    def write(self, kind: str, name: str, document: Dict[str, Any]) -> None:
        """Durably replace the file: temp file, fsync, rename, fsync the dir."""
        self.write_bytes(self.path_for(kind, name), self.encode(document))

    @staticmethod
    def write_bytes(path: Path, data: bytes) -> None:
        """Atomic durable write of *data* to *path* (Story #1457 AC10)."""
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(directory), prefix=f".{path.stem}_", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                nfs_safe_fsync(handle.fileno())
            os.replace(tmp_path, str(path))
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            nfs_safe_fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def delete(self, kind: str, name: str) -> None:
        """Remove the file; a missing file is not an error."""
        self.path_for(kind, name).unlink(missing_ok=True)


class ControlPlaneMetadata:
    """The single reader/writer of alias pointers and write-mode markers.

    Args:
        golden_repos_dir: The golden-repos root the files live under.
        backend: Registry storage; None means files only.
        settings: Zero-argument callable returning the current
            :class:`MetadataSettings` (read on every call, so a Config-screen
            change applies without a restart).
        cluster: True when *backend* is shared by several nodes (PostgreSQL).
    """

    def __init__(
        self,
        golden_repos_dir: PathLike,
        *,
        backend: Optional[ControlPlaneMetadataBackend] = None,
        settings: SettingsProvider = _default_settings,
        cluster: bool = False,
    ) -> None:
        self.files = MetadataFiles(golden_repos_dir)
        self.backend = backend
        self.cluster = cluster
        self._settings = settings
        self._import_complete = threading.Event()

    @property
    def golden_repos_dir(self) -> Path:
        return self.files.golden_repos_dir

    @property
    def is_registry_backed(self) -> bool:
        return self.backend is not None

    def current_settings(self) -> MetadataSettings:
        try:
            return self._settings()
        except Exception as exc:
            logger.warning(
                "Control-plane metadata settings unreadable (%s); using defaults "
                "(read_source=registry, dual_write=on)",
                exc,
            )
            return MetadataSettings()

    def _reads_files(self) -> bool:
        return (
            self.backend is None
            or self.current_settings().read_source == READ_SOURCE_FILES
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        if self.backend is None or self._reads_files():
            return self.files.read(kind, name)
        document = self.backend.get(kind, name)
        if document is None and not self._import_complete.is_set():
            return self.files.read(kind, name)
        return document

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        if self.backend is None or self._reads_files():
            return self.files.list_all(kind, prefix)
        documents = self.backend.list_all(kind, prefix)
        if self._import_complete.is_set():
            return documents
        merged = self.files.list_all(kind, prefix)
        merged.update(documents)
        return merged

    def names(self, kind: str) -> List[str]:
        """Every key of *kind* -- including, in files mode, keys whose file is
        unreadable, so cleanup can still remove them."""
        if self.backend is None or self._reads_files():
            return self.files.names(kind)
        return sorted(self.list_all(kind))

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def put(self, kind: str, name: str, document: Dict[str, Any]) -> None:
        if self.backend is None:
            self.files.write(kind, name, document)
            return
        settings = self.current_settings()
        if settings.read_source == READ_SOURCE_FILES:
            self.files.write(kind, name, document)
            self.backend.put(kind, name, document)
            return
        self.backend.put(kind, name, document)
        if settings.file_dual_write:
            self._dual_write(kind, name)

    def delete(self, kind: str, name: str) -> None:
        if self.backend is None:
            self.files.delete(kind, name)
            return
        settings = self.current_settings()
        if settings.read_source == READ_SOURCE_FILES:
            self.files.delete(kind, name)
            self.backend.delete(kind, name)
            return
        self.backend.delete(kind, name)
        if settings.file_dual_write:
            self._dual_write(kind, name)

    def _dual_write(self, kind: str, name: str) -> None:
        """Bring the file for *name* in line with the registry's CURRENT
        value -- write it, or delete it when the key is gone.

        Best-effort: the registry change has already committed and is what
        every reader sees, so a failed file write is logged, never raised.
        """
        assert self.backend is not None
        try:
            current = self.backend.get(kind, name)
            if current is None:
                self.files.delete(kind, name)
            else:
                self.files.write(kind, name, current)
        except Exception as exc:
            logger.warning(
                "Control-plane metadata: dual-write of %s %r to %s failed "
                "(registry is current; the rollback file is stale): %s",
                kind,
                name,
                self.files.path_for(kind, name),
                exc,
            )

    # ------------------------------------------------------------------
    # Startup import
    # ------------------------------------------------------------------

    def import_files(self) -> Dict[str, int]:
        """Copy every existing file into the registry, idempotently.

        A file is stored only when its own newest timestamp is strictly newer
        than the stored row's updated_at and than the key's delete tombstone
        -- so an older file never clobbers a newer registry value, a stale
        file never resurrects a deleted key, and a re-run changes nothing.
        Never deletes: a registry row with no file is kept. Only an import
        with no failures marks this process's import complete and ends the
        read-through fallback to the files.
        """
        report = {"imported": 0, "skipped": 0, "failed": 0}
        if self.backend is None:
            self._import_complete.set()
            return report
        for kind in METADATA_TABLES:
            try:
                documents = self.files.list_all(kind)
            except OSError as exc:
                logger.warning(
                    "Control-plane metadata import: cannot list %s: %s",
                    self.files.dir_for(kind),
                    exc,
                )
                report["failed"] += 1
                continue
            for name, document in documents.items():
                try:
                    stored = self.backend.import_if_newer(
                        kind, name, document, document_timestamp(kind, document)
                    )
                except Exception as exc:
                    logger.warning(
                        "Control-plane metadata import: %s %r failed: %s",
                        kind,
                        name,
                        exc,
                    )
                    report["failed"] += 1
                    continue
                report["imported" if stored else "skipped"] += 1
        if report["failed"] == 0:
            self._import_complete.set()
        logger.info(
            "Control-plane metadata import finished: imported=%d skipped=%d failed=%d",
            report["imported"],
            report["skipped"],
            report["failed"],
        )
        return report

    @property
    def import_complete(self) -> bool:
        """True once an import ran with no failures (read-through is off)."""
        return self._import_complete.is_set()

    def start_import(
        self, retry_delays: Sequence[float] = IMPORT_RETRY_DELAYS_SECONDS
    ) -> threading.Thread:
        """Run :meth:`import_files` on a daemon thread, off the startup path.

        Reads fall back to the files until an attempt finishes with no
        failures, so nothing waits on it. An attempt that had failures is
        retried after each of *retry_delays*; if every attempt fails, the
        fallback simply stays on for this process.
        """

        def _run() -> None:
            for delay in (None, *retry_delays):
                if delay is not None:
                    time.sleep(delay)
                try:
                    self.import_files()
                except Exception:
                    logger.exception("Control-plane metadata import failed")
                if self._import_complete.is_set():
                    return
            logger.error(
                "Control-plane metadata import still has failures after %d "
                "attempts; registry misses keep reading through to the files "
                "until the next restart",
                len(retry_delays) + 1,
            )

        thread = threading.Thread(
            target=_run, name="control-plane-metadata-import", daemon=True
        )
        thread.start()
        return thread


# ----------------------------------------------------------------------
# Process wiring
# ----------------------------------------------------------------------

_installed: Optional[ControlPlaneMetadata] = None
_installed_lock = threading.Lock()


def install_control_plane_metadata(metadata: ControlPlaneMetadata) -> None:
    """Make *metadata* the component every AliasManager / WriteModeMarkers for
    its golden-repos directory uses (called once by service_init)."""
    global _installed
    with _installed_lock:
        _installed = metadata


def uninstall_control_plane_metadata() -> None:
    """Remove the installed component (tests, shutdown)."""
    global _installed
    with _installed_lock:
        _installed = None


def installed_control_plane_metadata() -> Optional[ControlPlaneMetadata]:
    return _installed


def metadata_for(golden_repos_dir: PathLike) -> ControlPlaneMetadata:
    """The component for *golden_repos_dir*: the installed, registry-backed one
    when it serves that directory, otherwise a files-only one."""
    installed = _installed
    if installed is not None and _normalized(installed.golden_repos_dir) == (
        _normalized(golden_repos_dir)
    ):
        return installed
    return ControlPlaneMetadata(golden_repos_dir)
