"""Independent, field-level verification for temporal shard relocation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.sqlite_chunk_store import (
    chunk_store_has_real_data,
    open_chunk_store_for_path,
)

# Issue #1548 critical exploit fix (third adversarial review round): this
# name is shared with mover.py, which writes/reads the per-shard provenance
# marker at exactly this filename. It lives here -- not in mover.py -- so
# that ``_structural_manifest`` below can unambiguously exclude it from the
# digest it computes: the marker's own content (a digest) is written AFTER
# the digest it records is computed, so including the marker file in the
# thing it is a digest OF would make the marker self-referential and never
# reproducible on a fresh read.
PROVENANCE_MARKER_NAME = ".legacy-migration-provenance"


class VerificationError(IOError):
    """Raised when source and destination records are not identical."""


def _digest(record: dict[str, Any]) -> str:
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("vector_*.json")):
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise VerificationError(f"invalid temporal record: {path}")
        point_id = record["id"]
        if point_id in manifest:
            raise VerificationError(f"duplicate temporal point id: {point_id}")
        manifest[point_id] = _digest(record)
    return manifest


def _chunks_manifest(root: Path) -> dict[str, str]:
    db_path = root / "chunks.db"
    if not chunk_store_has_real_data(db_path, on_error="raise"):
        return {}
    store = open_chunk_store_for_path(db_path, str(root), read_only=True)
    try:
        manifest = {}
        for point_id in sorted(store.all_point_ids()):
            record = store.read(point_id)
            if record is None:
                raise VerificationError(f"chunk point disappeared: {point_id}")
            if point_id in manifest:
                raise VerificationError(f"duplicate temporal point id: {point_id}")
            manifest[point_id] = _digest(record)
        return manifest
    finally:
        store.close()


def _manifest(root: Path) -> dict[str, str]:
    layout = resolve_chunk_layout(root)
    return (
        _chunks_manifest(root)
        if layout is ChunkLayout.CHUNKS_DB
        else _json_manifest(root)
    )


def _file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _structural_manifest(root: Path) -> dict[str, str]:
    """Byte-for-byte digest of EVERY file under *root* (not just logical
    point records).

    Issue #1548 critical exploit fix: ``manifest_digest()`` previously
    hashed ONLY the logical point-record manifest (``_manifest()`` above --
    ``vector_*.json`` content, or a ``chunks.db`` row scan). It never
    covered ``collection_meta.json``, ``hnsw_index.bin``, or any other
    filesystem state that makes a shard directory an actually complete,
    queryable shard rather than a bare pile of point records. A reproduced
    exploit: identical logical point records at both the legacy source and
    the fixed-root target, but an INCOMPLETE target (missing
    ``hnsw_index.bin``/``collection_meta.json``), with a marker file whose
    recorded digest still "matched" both sides under the old, records-only
    definition -- so the code proceeded to classify the target as
    ``already_complete`` and delete the legacy copy, even though the
    target was never a real, complete shard.

    This function closes that gap directly: it walks every file under
    *root* (sorted relative path for determinism) and hashes its raw
    bytes, deliberately EXCLUDING the provenance marker file itself (whose
    content is a digest of what was verified at publish time -- including
    it here would make the marker digest a digest of itself, never
    reproducible). If the target is missing a file the source has (e.g.
    ``hnsw_index.bin``), the two structural manifests -- and therefore
    ``manifest_digest()`` -- can never agree, so no marker (forged or
    genuine) can satisfy both the source-side and target-side equality
    checks in ``mover._classify_existing_target`` unless the target
    genuinely mirrors the source's full file tree.

    Issue #1548 round-4 exploit fix: ALSO refuses to trust (raises
    ``VerificationError``) a tree containing any symlink, anywhere.
    ``Path.is_file()``/``.exists()`` resolve THROUGH a symlink to its
    target -- a reproduced exploit planted ``hnsw_index.bin`` and
    ``collection_meta.json`` at the fixed-root target as symlinks pointing
    back into the legacy SOURCE directory, so this function (before this
    fix) happily hashed the source's own bytes and produced a digest that
    trivially "matched" the source, even though the target held no
    independent data of its own -- its files would go dangling the moment
    the legacy source was deleted. ``Path.is_symlink()`` uses
    ``os.lstat()`` and never follows, so this check cannot be fooled the
    same way.
    """
    if root.is_symlink():
        raise VerificationError(
            f"provenance check refuses to trust a symlinked root: {root}"
        )
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise VerificationError(
                f"provenance check refuses to trust a tree containing a symlink: {path}"
            )
        if not path.is_file():
            continue
        if path.name == PROVENANCE_MARKER_NAME:
            continue
        manifest[str(path.relative_to(root))] = _file_digest(path)
    return manifest


def peek_one_vector_dimension(root: Path) -> Optional[int]:
    """Return the vector dimension of one real point record under *root*,
    or ``None`` if no record can be found.

    Issue #1548 round-4 exploit fix: used by ``mover.py``'s structural
    completeness gate to attempt a genuine ``hnswlib`` load of
    ``hnsw_index.bin`` without requiring external, repo-specific dimension
    configuration -- the dimension is read straight from the shard's own
    committed data, the same data the HNSW index is supposed to index.
    Read-only and side-effect-free by construction: never creates a
    ``chunks.db`` file, and returns ``None`` (rather than raising) on any
    layout/parse ambiguity so a caller doing safety-gated inspection can
    treat "cannot determine" as "not verifiably valid" without crashing.
    """
    layout = resolve_chunk_layout(root)
    record: Optional[dict[str, Any]] = None
    if layout is ChunkLayout.CHUNKS_DB:
        db_path = root / "chunks.db"
        if not chunk_store_has_real_data(db_path, on_error="treat_absent"):
            return None
        store = open_chunk_store_for_path(db_path, str(root), read_only=True)
        try:
            point_ids = sorted(store.all_point_ids())
            if not point_ids:
                return None
            record = store.read(point_ids[0])
        finally:
            store.close()
    else:
        candidates = sorted(root.rglob("vector_*.json"))
        if not candidates:
            return None
        try:
            with candidates[0].open(encoding="utf-8") as stream:
                record = json.load(stream)
        except (OSError, ValueError):
            return None
    if not isinstance(record, dict):
        return None
    vector = record.get("vector")
    if not isinstance(vector, list) or not vector:
        return None
    return len(vector)


def verify_shard_copy(source: Path, published: Path) -> None:
    """Freshly read every source/destination record and compare all fields."""
    source_manifest = _manifest(source)
    destination_manifest = _manifest(published)
    if source_manifest != destination_manifest:
        raise VerificationError(
            f"temporal shard verification failed: {source} != {published}"
        )


def manifest_digest(root: Path) -> str:
    """Hash of *root*'s full field-for-field manifest AND full file tree.

    Used by ``mover.py`` to write a content-bound provenance marker: unlike
    a bare sentinel file (which any independent process could coincidentally
    or accidentally create), a digest that must match the CURRENT legacy
    source's own manifest digest cannot be satisfied by data that merely
    happens to occupy the same path -- it can only be satisfied by data that
    is field-for-field identical to what was actually verified and
    published.

    Issue #1548 critical exploit fix: the digest now covers TWO
    independent layers -- the logical point-record manifest (``_manifest``,
    unchanged) AND the full-tree structural manifest (``_structural_
    manifest``, new). Covering only the logical layer let an incomplete
    target (real point records, but no HNSW index / metadata) satisfy a
    matching digest; covering the structural layer too means the digest
    can only match when the target is a genuine, byte-for-byte mirror of
    the source's entire directory -- including the files that make it an
    actually complete, queryable shard.
    """
    encoded = json.dumps(
        {"logical": _manifest(root), "structural": _structural_manifest(root)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
