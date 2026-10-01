"""Bug #1997: ``collection_meta.json`` must not be re-read and re-parsed per
file on the indexing hot path -- and caching its parse must never hide a
layout change.

The per-file loop of an incremental / resume / reconcile run against an
EXISTING collection calls ``get_existing_content_hashes()`` (-> ``get_point``)
and ``upsert_points()`` once per file. Each of those consults
``_is_chunks_db_collection()``; before the fix that resolved the layout by
reading and ``json.loads``-ing the whole metadata file on every call (2-3
full parses per file; a multi-megabyte file costs tens of milliseconds per
parse, GIL-serialised).

Opens are counted by a SCOPED recorder that wraps ``builtins.open`` and
``io.open`` (pathlib opens through ``io.open``) and restores both on exit --
never by mtime, and never with a process-wide hook.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    clear_chunks_db_discriminator,
    resolve_chunk_layout,
    write_chunks_db_discriminator,
)
from code_indexer.storage.shared.collection_meta_cache import CollectionMetaCache

VECTOR_DIM = 16
COLLECTION = "coll"
_META_NAME = "collection_meta.json"


class _MetaOpenRecorder:
    """Counts opens of ``collection_meta.json`` under ``root`` for the
    duration of a ``with`` block: ``reads`` (read-only modes) and
    ``direct_writes`` (the target itself opened for writing, i.e. NOT a
    temp-file + ``os.replace`` write)."""

    def __init__(self, root: Path) -> None:
        self._root = str(root.resolve())
        self._lock = threading.Lock()
        self._saved = (builtins.open, io.open)
        self.reads = 0
        self.direct_writes = 0

    def _record(self, file: Any, mode: str) -> None:
        if not isinstance(file, (str, bytes, os.PathLike)):
            return
        path = os.path.abspath(os.fsdecode(file))
        if not (path.endswith(_META_NAME) and path.startswith(self._root)):
            return
        with self._lock:
            if any(flag in mode for flag in "wax+"):
                self.direct_writes += 1
            else:
                self.reads += 1

    def _wrap(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def recording_open(file: Any, mode: str = "r", *args: Any, **kw: Any) -> Any:
            self._record(file, mode)
            return real(file, mode, *args, **kw)

        return recording_open

    def __enter__(self) -> "_MetaOpenRecorder":
        setattr(builtins, "open", self._wrap(self._saved[0]))
        setattr(io, "open", self._wrap(self._saved[1]))
        return self

    def __exit__(self, *exc: Any) -> None:
        setattr(builtins, "open", self._saved[0])
        setattr(io, "open", self._saved[1])


# ---------------------------------------------------------------------------
# Fixtures: a real collection with N files, built by a PRIOR store instance.
# ---------------------------------------------------------------------------


def _point(i: int, rng: np.random.Generator) -> Dict[str, Any]:
    return {
        "id": f"pt_{i}",
        "vector": rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist(),
        "payload": {
            "path": f"pkg/mod_{i}.py",
            "language": "python",
            "chunk_index": 0,
            "content_hash": f"hash_{i}",
            "content": f"def f_{i}(): return {i}",
        },
    }


def _build_collection(base: Path, n_files: int, chunks_db: bool) -> List[Dict]:
    rng = np.random.default_rng(1997)
    points = [_point(i, rng) for i in range(n_files)]
    builder = FilesystemVectorStore(
        base_path=base, use_chunks_db_for_new_collections=chunks_db
    )
    builder.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    builder.begin_indexing(COLLECTION)
    builder.upsert_points(COLLECTION, points)
    builder.end_indexing(COLLECTION)
    return points


def _per_file_loop_reads(tmp_path: Path, n_files: int, chunks_db: bool) -> int:
    base = tmp_path / f"n{n_files}"
    points = _build_collection(base, n_files, chunks_db)
    expected = ChunkLayout.CHUNKS_DB if chunks_db else ChunkLayout.SHARDED_JSON
    assert resolve_chunk_layout(base / COLLECTION) == expected

    # Fresh instance == a new resume/reconcile process: no _chunks_db_mode.
    store = FilesystemVectorStore(base_path=base)
    store.begin_indexing(COLLECTION)
    seen: List[Optional[Dict]] = []

    with _MetaOpenRecorder(base) as recorder:
        for point in points:
            hashes = store.get_existing_content_hashes(
                point["payload"]["path"], COLLECTION
            )
            seen.append(hashes.get(0))
            store.upsert_points(COLLECTION, [point])
    store.end_indexing(COLLECTION)

    # Behaviour preserved: every existing file's chunk was found and reused.
    assert [entry["content_hash"] for entry in seen if entry] == [
        f"hash_{i}" for i in range(n_files)
    ]
    return recorder.reads


@pytest.mark.parametrize("chunks_db", [True, False], ids=["chunks_db", "sharded_json"])
def test_meta_reads_independent_of_file_count(tmp_path: Path, chunks_db: bool) -> None:
    reads_small = _per_file_loop_reads(tmp_path, 5, chunks_db)
    reads_large = _per_file_loop_reads(tmp_path, 20, chunks_db)

    # Guards against a vacuous pass (a recorder that sees nothing): the
    # first cached read in the loop is always a real open.
    assert reads_small > 0
    # Before the fix: 2 (CHUNKS_DB) or 3 (SHARDED_JSON) full reads per file.
    assert reads_large == reads_small, (
        f"collection_meta.json reads scale with file count: "
        f"N=5 -> {reads_small}, N=20 -> {reads_large}"
    )
    assert reads_large <= 3, f"expected a small constant, got {reads_large}"


# ---------------------------------------------------------------------------
# Freshness: caching the parsed metadata must never hide a layout change.
# ---------------------------------------------------------------------------


class _RecordingMetaCache(CollectionMetaCache):
    """A REAL cache that also records which directories were invalidated."""

    def __init__(self) -> None:
        super().__init__()
        self.invalidated: List[str] = []

    def invalidate(self, collection_dir: Any) -> None:
        self.invalidated.append(str(Path(collection_dir).resolve()))
        super().invalidate(collection_dir)


def _replace_meta(meta_path: Path, data: bytes) -> None:
    """Rewrite the way every real external writer does: a temp file in the
    same directory, then ``os.replace`` (a NEW inode at the target path)."""
    fd, tmp = tempfile.mkstemp(dir=str(meta_path.parent), suffix=".tmp")
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, meta_path)


@pytest.mark.parametrize(
    "writer", ["discriminator_write", "replace_clear", "in_place_clear"]
)
def test_warm_cache_sees_external_layout_flip_without_invalidate(
    tmp_path: Path, writer: str
) -> None:
    """A store with a WARM cache; a writer outside it (another component or
    process -- it never calls this store's ``invalidate``) flips the layout.
    The mtime is restored to model a same-tick / coarse-mtime filesystem
    write, so only the inode (os.replace) or size (in-place) can reveal it.
    """
    starts_chunks_db = writer != "discriminator_write"
    base = tmp_path / writer
    _build_collection(base, 3, chunks_db=starts_chunks_db)
    collection_path = base / COLLECTION
    meta_path = collection_path / _META_NAME

    cache = _RecordingMetaCache()
    warm = FilesystemVectorStore(base_path=base, collection_meta_cache=cache)
    assert warm._is_chunks_db_collection(COLLECTION, collection_path) is (
        starts_chunks_db
    )
    original = os.stat(meta_path)

    if writer == "discriminator_write":
        write_chunks_db_discriminator(collection_path)  # real os.replace writer
    else:
        stripped = clear_chunks_db_discriminator(meta_path.read_bytes())
        if writer == "replace_clear":
            _replace_meta(meta_path, stripped)
        else:
            meta_path.write_bytes(stripped)
    os.utime(meta_path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert os.stat(meta_path).st_mtime_ns == original.st_mtime_ns

    assert warm._is_chunks_db_collection(COLLECTION, collection_path) is (
        not starts_chunks_db
    )
    assert cache.invalidated == []


def test_invalidate_drops_entry_for_unchanged_stat_identity(tmp_path: Path) -> None:
    """The residual blind spot of a stat-keyed cache: a rewrite that keeps
    mtime tick, size AND inode. Only an explicit invalidate can reveal it."""
    collection_dir = tmp_path / "c"
    collection_dir.mkdir()
    meta_path = collection_dir / _META_NAME
    meta_path.write_text('{"layout": "aaaa"}')
    cache = CollectionMetaCache()
    assert cache.get(collection_dir) == {"layout": "aaaa"}

    before = os.stat(meta_path)
    with open(meta_path, "r+") as fh:  # in place: same inode, same size
        fh.write('{"layout": "bbbb"}')
    os.utime(meta_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(meta_path)
    assert (after.st_mtime_ns, after.st_size, after.st_ino) == (
        before.st_mtime_ns,
        before.st_size,
        before.st_ino,
    )
    assert cache.get(collection_dir) == {"layout": "aaaa"}  # identity unchanged

    cache.invalidate(collection_dir)
    assert cache.get(collection_dir) == {"layout": "bbbb"}


def test_invalidate_on_missing_file_is_a_no_op(tmp_path: Path) -> None:
    CollectionMetaCache().invalidate(tmp_path / "absent")


def test_in_process_meta_writers_invalidate_cache(tmp_path: Path) -> None:
    cache = _RecordingMetaCache()
    store = FilesystemVectorStore(
        base_path=tmp_path,
        use_chunks_db_for_new_collections=True,
        collection_meta_cache=cache,
    )
    collection_path = str((tmp_path / COLLECTION).resolve())

    store.create_collection(COLLECTION, vector_size=VECTOR_DIM)
    assert collection_path in cache.invalidated, "create_collection"

    rng = np.random.default_rng(7)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, [_point(i, rng) for i in range(3)])
    cache.invalidated.clear()
    store.end_indexing(COLLECTION)  # unique-file-count + discriminator commit
    assert collection_path in cache.invalidated, "end_indexing"
    assert resolve_chunk_layout(Path(collection_path)) == ChunkLayout.CHUNKS_DB

    cache.invalidated.clear()
    assert store.clear_collection(COLLECTION) is True
    assert collection_path in cache.invalidated, "clear_collection"


def test_clear_collection_restores_metadata_via_atomic_replace(tmp_path: Path) -> None:
    """Every collection_meta.json writer must use temp file + os.replace, so a
    warm cache elsewhere sees a NEW inode -- never a direct in-place write."""
    base = tmp_path / "clr"
    _build_collection(base, 3, chunks_db=False)
    store = FilesystemVectorStore(base_path=base)

    with _MetaOpenRecorder(base) as recorder:
        assert store.clear_collection(COLLECTION) is True

    assert recorder.direct_writes == 0
    restored = json.loads((base / COLLECTION / _META_NAME).read_text())
    assert restored["name"] == COLLECTION
    assert restored["vector_size"] == VECTOR_DIM
    assert list((base / COLLECTION).glob("*.tmp")) == []
