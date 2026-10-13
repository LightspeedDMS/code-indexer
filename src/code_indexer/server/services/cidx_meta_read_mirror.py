"""
A pod-local read mirror of cidx-meta's shared descriptions and dependency map.

The dependency-map readers (the depmap MCP tools and their parser, the domain
and dashboard services, the health detector, the quick-reference guide) take a
DIRECTORY and walk it: ``_domains.json``, ``_index.md``, the domain files, the
repository descriptions beside them. Under the local content backend that
directory is cidx-meta's versioned snapshot, exactly as before.

Under a delegated content backend that snapshot holds index state only -- the
content is workspace-mcp's. Rather than re-plumb a dozen parsers file by file,
the readers get a directory that MIRRORS the shared content they read:
``<golden_repos_dir>/.cidx-meta-read-mirror``, synced from the content manifest
on access (at most every :data:`SYNC_TTL_SECONDS`), fetching only files whose
blob sha changed and deleting files the shared content no longer has. The same
reader code then runs over a tree identical to the shared one -- the same idea
as the IndexStore's node-local checkout.

Read-your-writes on the writing pod: every write or delete through
SharedCidxMeta in this process is applied to the mirror at once (write-through;
if that local apply fails, the mirror is marked for a full re-sync on its next
read). Writes made by OTHER pods appear within :data:`SYNC_TTL_SECONDS`.

It is derived, pod-local cidx state, never written back, and never the pod's
own ``golden-repos/cidx-meta``. A sync that cannot list or fetch RAISES
(CidxMetaUnavailableError): readers never get a silently empty or half-updated
map.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from code_indexer.server.services.cidx_meta_content import (
    LISTING_PRUNE_DIRS,
    CidxMetaUnavailableError,
    SharedCidxMeta,
    add_write_listener,
    shared_cidx_meta,
)
from code_indexer.utils.blob_hash import git_blob_hash_bytes

logger = logging.getLogger(__name__)

#: The mirror directory, beside cidx-meta under the golden-repos directory.
MIRROR_DIRNAME = ".cidx-meta-read-mirror"
#: Where the mirror records the blob sha of every file it holds.
_STATE_FILENAME = ".mirror-state.json"
#: A mirror synced this recently is served as is.
SYNC_TTL_SECONDS = 5.0
#: Files per fetch request.
_FETCH_BATCH = 200

#: The dependency-map output directory inside cidx-meta.
DEPENDENCY_MAP_DIR = "dependency-map"


def mirrored(path: str) -> bool:
    """What the dependency-map readers read: everything under
    ``dependency-map/`` and the top-level ``.md``/``.json`` files (the
    repository descriptions). Memories, X-Ray patterns and the analysis
    staging directory are not mirrored."""
    if path.startswith(DEPENDENCY_MAP_DIR + "/"):
        return True
    return "/" not in path and path.endswith((".md", ".json"))


class CidxMetaReadMirror:
    """One mirror directory, synced from one shared cidx-meta."""

    def __init__(
        self,
        shared: SharedCidxMeta,
        mirror_root: Path,
        *,
        include: Callable[[str], bool] = mirrored,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._shared = shared
        self._root = Path(mirror_root)
        self._include = include
        self._clock = clock
        self._lock = threading.Lock()
        self._synced_at: Optional[float] = None

    @property
    def root(self) -> Path:
        return self._root

    def sync(self, *, force: bool = False) -> Path:
        """Bring the mirror up to date with the shared content; return its root."""
        with self._lock:
            now = self._clock()
            if (
                not force
                and self._synced_at is not None
                and now - self._synced_at < SYNC_TTL_SECONDS
            ):
                return self._root
            self._sync_locked()
            self._synced_at = self._clock()
            return self._root

    def apply_write(self, rel: str, data: Optional[bytes]) -> None:
        """Apply this pod's own write (*data*) or delete (None) of *rel* to
        the mirror now, so the next read sees it. A failure to apply marks the
        mirror for a full re-sync instead -- a later read is never stale."""
        if not self._include(rel):
            return
        with self._lock:
            try:
                held = self._read_state()
                if data is None:
                    (self._root / rel).unlink(missing_ok=True)
                    held.pop(rel, None)
                else:
                    _atomic_write(self._root / rel, data)
                    held[rel] = git_blob_hash_bytes(data)
                self._write_state(held)
            except OSError as exc:
                logger.warning(
                    "cidx-meta read mirror %s: write-through of %s failed (%s); "
                    "re-syncing on next read",
                    self._root,
                    rel,
                    exc,
                )
                self._synced_at = None

    def _sync_locked(self) -> None:
        wanted: Dict[str, str] = {
            entry.path: entry.blob_sha
            for entry in self._shared.entries(prune_dirs=LISTING_PRUNE_DIRS)
            if entry.blob_sha is not None
            and not entry.is_symlink
            and self._include(entry.path)
        }
        self._root.mkdir(parents=True, exist_ok=True)
        held = self._read_state()
        stale = sorted(p for p, sha in wanted.items() if held.get(p) != sha)
        for start in range(0, len(stale), _FETCH_BATCH):
            self._fetch_into_place(stale[start : start + _FETCH_BATCH], held)
        for path in sorted(set(held) - set(wanted)):
            (self._root / path).unlink(missing_ok=True)
            held.pop(path, None)
        self._write_state(held)
        logger.debug(
            "cidx-meta read mirror %s: %d files, %d fetched",
            self._root,
            len(wanted),
            len(stale),
        )

    def _fetch_into_place(self, paths: List[str], held: Dict[str, str]) -> None:
        try:
            records = list(self._shared.backend.fetch(self._shared.root, paths))
        except Exception as exc:
            raise CidxMetaUnavailableError(
                f"cidx-meta read mirror: fetching from the content backend "
                f"failed: {type(exc).__name__}: {exc}"
            ) from exc
        returned = {record.path for record in records}
        missing = [p for p in paths if p not in returned]
        if missing:
            raise CidxMetaUnavailableError(
                f"cidx-meta read mirror: the content backend returned nothing for "
                f"{missing[:5]}"
            )
        for record in records:
            if record.error is not None:
                raise CidxMetaUnavailableError(
                    f"cidx-meta read mirror: {record.path} could not be read: "
                    f"{record.error}"
                )
            _atomic_write(self._root / record.path, record.data)
            held[record.path] = record.blob_sha or git_blob_hash_bytes(record.data)

    def _read_state(self) -> Dict[str, str]:
        try:
            data = json.loads((self._root / _STATE_FILENAME).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, held: Dict[str, str]) -> None:
        _atomic_write(
            self._root / _STATE_FILENAME, json.dumps(held, sort_keys=True).encode()
        )


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_mirrors: Dict[str, CidxMetaReadMirror] = {}
_mirrors_lock = threading.Lock()


def cidx_meta_read_mirror(meta_dir: Path) -> Optional[CidxMetaReadMirror]:
    """The read mirror of the cidx-meta at *meta_dir* under a delegated
    content backend, else None (read cidx-meta's directories as before).

    One mirror per cidx-meta per process, so concurrent readers share one
    sync and one TTL.
    """
    shared = shared_cidx_meta(meta_dir)
    if shared is None:
        return None
    mirror_root = Path(meta_dir).parent / MIRROR_DIRNAME
    key = f"{shared.root}\0{mirror_root}"
    with _mirrors_lock:
        mirror = _mirrors.get(key)
        if mirror is None or mirror._shared.backend is not shared.backend:
            mirror = CidxMetaReadMirror(shared, mirror_root)
            _mirrors[key] = mirror
        return mirror


def _on_shared_write(root: str, rel: str, data: Optional[bytes]) -> None:
    """Write-through hook: apply a write to every mirror of *root*."""
    with _mirrors_lock:
        targets = [m for m in _mirrors.values() if m._shared.root == root]
    for mirror in targets:
        mirror.apply_write(rel, data)


add_write_listener(_on_shared_write)


def synced_read_root(meta_dir: Path) -> Optional[Path]:
    """The mirror's root, synced now, under a delegated backend; else None."""
    mirror = cidx_meta_read_mirror(meta_dir)
    return None if mirror is None else mirror.sync()


def reset_mirrors() -> None:
    """Forget every mirror (tests)."""
    with _mirrors_lock:
        _mirrors.clear()
