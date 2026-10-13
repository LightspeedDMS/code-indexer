"""
cidx-meta's files on a pod without the golden-repos volume.

cidx-meta (``<golden_repos_dir>/cidx-meta``) is the non-git repository cidx
writes itself: repository descriptions, memories and the rest. Under the local
content backend (the default) it is a directory on this pod's disk -- the
shared volume -- and every writer and reader keeps using that disk exactly as
before: :func:`shared_cidx_meta` returns None and the caller runs today's code.

Under a delegated content backend the pod does not mount the volume. Its
``<golden_repos_dir>/cidx-meta`` is a pod-local directory holding index state
only, and the shared content lives with workspace-mcp at the SAME absolute
path. :class:`SharedCidxMeta` is that content: reads and listings through the
content backend (``/content/manifest`` and ``/content/fetch``), writes and
deletes through the content writer (``POST /content/write``), at
``<cidx-meta root>/<rel>``.

Failure rule: a backend or writer failure RAISES. A file that is absent reads
as :class:`FileNotFoundError`; anything else that keeps the bytes from being
had -- a transport error, an unreadable file, a missing writer -- is a
:class:`CidxMetaUnavailableError`. An unreachable workspace-mcp is never
"cidx-meta is empty".

Compare-and-swap uses git's blob sha of the bytes, as the writer does:
``expected=None`` is "must not exist", a sha is "must still be exactly this",
and :data:`UNCONDITIONAL` sends no precondition. A failed precondition is a
:class:`CidxMetaConflictError`, so a writer on another pod between a read and
a write is a conflict, never a lost update.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple, Union

from code_indexer.server.services.constants import CIDX_META_REPO
from code_indexer.server.services.content_writer_plugin import ECONFLICT
from code_indexer.server.storage.shared.content_backend import (
    UNREADABLE_FETCH_ERRORS,
    ContentBackend,
    ContentEntry,
)
from code_indexer.utils.blob_hash import git_blob_hash_bytes
from code_indexer.utils.index_store import INDEX_DIRNAME
from code_indexer.utils.path_confinement import confine_relative_path

#: ``expected`` meaning "no precondition": the kwarg is not sent at all.
UNCONDITIONAL: Any = object()

Expected = Union[None, str, object]
PathLike = Union[str, Path]


class CidxMetaUnavailableError(RuntimeError):
    """cidx-meta's shared content could not be read, listed or written."""


class CidxMetaConflictError(RuntimeError):
    """A compare-and-swap precondition failed: another writer got there first."""


@dataclass(frozen=True)
class SharedCidxMeta:
    """cidx-meta's content as workspace-mcp holds it, at ``root``."""

    root: str
    backend: ContentBackend
    writer: Any

    # ── paths ───────────────────────────────────────────────────────────

    def rel(self, path: PathLike) -> str:
        """*path* (absolute under ``root``, or already relative) as the
        confined repo-relative POSIX path the seam speaks.

        Raises PermissionError for a path outside cidx-meta.
        """
        candidate = PurePosixPath(str(path))
        if candidate.is_absolute():
            try:
                candidate = candidate.relative_to(PurePosixPath(self.root))
            except ValueError:
                raise PermissionError(f"{path} is not inside cidx-meta") from None
        rel = confine_relative_path(str(candidate))
        if not rel:
            raise PermissionError(f"{path} is not a file inside cidx-meta")
        return rel

    def absolute(self, path: PathLike) -> str:
        """The absolute path the writer takes for *path*."""
        return str(PurePosixPath(self.root) / self.rel(path))

    # ── reads ───────────────────────────────────────────────────────────

    def read_bytes(self, path: PathLike) -> bytes:
        """The file's bytes; FileNotFoundError when absent."""
        data, _sha = self._fetch(path)
        return data

    def read_text(
        self, path: PathLike, encoding: str = "utf-8", errors: str = "strict"
    ) -> str:
        """The file's text; FileNotFoundError when absent."""
        return self.read_bytes(path).decode(encoding, errors)

    def blob_sha(self, path: PathLike) -> Optional[str]:
        """The git blob sha of the file's current bytes, None when absent."""
        try:
            _data, sha = self._fetch(path)
        except FileNotFoundError:
            return None
        return sha

    def exists(self, path: PathLike) -> bool:
        """Whether the file exists in the shared content."""
        return self.blob_sha(path) is not None

    def read_with_sha(self, path: PathLike) -> Tuple[bytes, str]:
        """The bytes and their blob sha (the CAS token for a later write)."""
        return self._fetch(path)

    def _fetch(self, path: PathLike) -> Tuple[bytes, str]:
        rel = self.rel(path)
        try:
            fetched = next(iter(self.backend.fetch(self.root, [rel])), None)
        except Exception as exc:
            raise CidxMetaUnavailableError(
                f"cidx-meta: reading {rel} from the content backend failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if fetched is None:
            raise CidxMetaUnavailableError(
                f"cidx-meta: the content backend returned nothing for {rel}"
            )
        if fetched.error is None:
            return fetched.data, fetched.blob_sha or git_blob_hash_bytes(fetched.data)
        if fetched.error in UNREADABLE_FETCH_ERRORS:
            raise CidxMetaUnavailableError(
                f"cidx-meta: {rel} could not be read: {fetched.error}"
            )
        raise FileNotFoundError(f"cidx-meta file not found: {rel}")

    def entries(self, prune_dirs: Tuple[str, ...] = ()) -> Tuple[ContentEntry, ...]:
        """Every file of the shared cidx-meta, all pages, read NOW (never cached).

        Raises CidxMetaUnavailableError when the backend cannot list it, so an
        unreachable backend is never an empty cidx-meta.
        """
        entries: List[ContentEntry] = []
        cursor: Optional[str] = None
        try:
            while True:
                page = self.backend.manifest(
                    self.root, cursor=cursor, prune_dirs=list(prune_dirs)
                )
                entries.extend(page.entries)
                if page.next_cursor is None:
                    return tuple(entries)
                if page.next_cursor == cursor:
                    raise RuntimeError("manifest cursor did not advance")
                cursor = page.next_cursor
        except Exception as exc:
            raise CidxMetaUnavailableError(
                f"cidx-meta: listing the shared content failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def list_names(self, directory: PathLike = "", suffix: str = "") -> List[str]:
        """Names of the files DIRECTLY in *directory* ending in *suffix*, sorted
        (the shared-content twin of ``directory.glob("*" + suffix)``)."""
        prefix = "" if str(directory) in ("", ".") else self.rel(directory) + "/"
        names = []
        for entry in self.entries():
            if entry.is_symlink or not entry.path.startswith(prefix):
                continue
            name = entry.path[len(prefix) :]
            if "/" not in name and name.endswith(suffix):
                names.append(name)
        return sorted(names)

    # ── writes ──────────────────────────────────────────────────────────

    def put(
        self, path: PathLike, data: bytes, *, expected: Expected = UNCONDITIONAL
    ) -> None:
        """Write *data* atomically (the writer creates parent directories)."""
        target = self.absolute(path)
        try:
            self.writer.put(target, data, **_cas(expected))
        except Exception as exc:
            raise _writer_error(exc, target) from exc

    def put_text(
        self, path: PathLike, text: str, *, expected: Expected = UNCONDITIONAL
    ) -> None:
        """Write *text* as UTF-8."""
        self.put(path, text.encode("utf-8"), expected=expected)

    def delete(self, path: PathLike, *, expected: Expected = UNCONDITIONAL) -> bool:
        """Delete the file; False when it was already absent."""
        target = self.absolute(path)
        try:
            return bool(self.writer.delete(target, **_cas(expected)))
        except Exception as exc:
            raise _writer_error(exc, target) from exc

    def rename(self, old: PathLike, new: PathLike, *, overwrite: bool) -> bool:
        """Move *old* to *new*: write the new name (only if absent unless
        *overwrite*), then delete the old one only if it still holds the
        bytes just copied. False when *old* is absent, or *new* exists and
        *overwrite* is off."""
        try:
            data, sha = self.read_with_sha(old)
        except FileNotFoundError:
            return False
        if not overwrite and self.exists(new):
            return False
        self.put(new, data, expected=UNCONDITIONAL if overwrite else None)
        self.delete(old, expected=sha)
        return True


def _cas(expected: Expected) -> Dict[str, Any]:
    if expected is UNCONDITIONAL:
        return {}
    return {"expected_blob_sha": expected}


def _writer_error(exc: Exception, target: str) -> Exception:
    if getattr(exc, "code", None) == ECONFLICT:
        return CidxMetaConflictError(
            f"cidx-meta: {target} was changed by another writer: {exc}"
        )
    return CidxMetaUnavailableError(
        f"cidx-meta: writing {target} through the content writer failed: "
        f"{type(exc).__name__}: {exc}"
    )


def shared_cidx_meta(meta_dir: PathLike) -> Optional[SharedCidxMeta]:
    """cidx-meta's shared content when the server's content backend is
    delegated, else None -- the caller then reads and writes the local disk
    exactly as it always has.

    Raises CidxMetaUnavailableError when the backend is delegated but no
    content writer was parked: writing the pod's disk instead would "succeed"
    where no other pod and no index ever sees it.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        server_content_backend,
    )

    backend = server_content_backend()
    if backend is None or backend.is_local:
        return None
    from code_indexer.server.startup.content_writer_wiring import (
        server_content_writer,
    )

    writer = server_content_writer()
    if writer is None:
        raise CidxMetaUnavailableError(
            "cidx-meta lives with the content backend (content_backend="
            "'delegated') but no content writer is configured; refusing to "
            "write this pod's local disk."
        )
    return SharedCidxMeta(root=str(meta_dir), backend=backend, writer=writer)


def cidx_meta_root_of(path: PathLike) -> Optional[Path]:
    """The cidx-meta directory *path* is inside (the nearest ancestor named
    ``cidx-meta``, or *path* itself), or None."""
    candidate = Path(path)
    for parent in (candidate, *candidate.parents):
        if parent.name == CIDX_META_REPO:
            return parent
    return None


def shared_cidx_meta_for(path: PathLike) -> Optional[SharedCidxMeta]:
    """:func:`shared_cidx_meta` for the cidx-meta directory holding *path*.

    None under the local backend, and for a path outside any cidx-meta
    directory (not cidx-meta content).
    """
    root = cidx_meta_root_of(path)
    if root is None:
        return None
    return shared_cidx_meta(root)


# ── change detection ────────────────────────────────────────────────────

#: Directories a cidx-meta listing never needs: index state and git metadata
#: (a legacy cidx-meta on the shared volume may still hold its old index).
LISTING_PRUNE_DIRS: Tuple[str, ...] = (INDEX_DIRNAME, ".git")


def _files(shared: SharedCidxMeta) -> List[ContentEntry]:
    return [
        entry
        for entry in shared.entries(prune_dirs=LISTING_PRUNE_DIRS)
        if not entry.is_symlink
    ]


def visible_entries(shared: SharedCidxMeta) -> List[ContentEntry]:
    """The shared files the local refresh walk would see: no component of the
    path -- directory or file -- is hidden (starts with ``.``).

    Raises CidxMetaUnavailableError when the content cannot be listed.
    """
    return [
        entry
        for entry in _files(shared)
        if not any(part.startswith(".") for part in entry.path.split("/"))
    ]


def changed_since(shared: SharedCidxMeta, timestamp: float) -> bool:
    """Whether any visible shared file was modified after *timestamp* (seconds).

    The shared-content twin of the refresh's local mtime walk: no visible
    file is "no changes"; a file whose mtime the backend does not report is
    treated as changed (unknown is never the epoch).
    """
    for entry in visible_entries(shared):
        if entry.mtime_ns is None or entry.mtime_ns / 1_000_000_000 > timestamp:
            return True
    return False


def has_visible_files(shared: SharedCidxMeta) -> bool:
    """Whether the shared cidx-meta holds any visible file (listing it NOW)."""
    return bool(visible_entries(shared))


def has_files_with_extensions(
    shared: SharedCidxMeta, extensions: set, exclude_dirs: set
) -> bool:
    """The shared-content twin of the refresh's ``has_files_with_extensions``:
    a file with one of the bare *extensions* outside any excluded or hidden
    directory."""
    if not extensions:
        return False
    for entry in _files(shared):
        *dirs, name = entry.path.split("/")
        if any(d in exclude_dirs or d.startswith(".") for d in dirs):
            continue
        if os.path.splitext(name)[1].lstrip(".") in extensions:
            return True
    return False


# ── backup ──────────────────────────────────────────────────────────────


class CidxMetaBackupUnsupportedError(CidxMetaUnavailableError):
    """The git backup of cidx-meta cannot run against shared content."""


BACKUP_UNSUPPORTED = (
    "cidx-meta git backup is not supported with content_backend='delegated': "
    "cidx-meta's content lives with workspace-mcp, and this pod's {path} holds "
    "index state only, so a backup run here would commit and push an empty "
    "directory over the real one. Disable cidx_meta_backup on this server."
)


def refuse_backup_of_shared_content(cidx_meta_path: PathLike) -> None:
    """Raise CidxMetaBackupUnsupportedError when cidx-meta is shared content.

    A no-op under the local backend, where the backup runs exactly as before.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    if is_content_seam_delegated():
        raise CidxMetaBackupUnsupportedError(
            BACKUP_UNSUPPORTED.format(path=cidx_meta_path)
        )
