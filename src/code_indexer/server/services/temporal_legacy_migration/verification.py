"""Independent, field-level verification for temporal shard relocation."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.sqlite_chunk_store import (
    chunk_store_has_real_data,
    open_chunk_store_for_path,
)

logger = logging.getLogger(__name__)

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


def logical_point_ids(root: Path) -> set[str]:
    """Return the full set of logical point-record IDs found under *root*.

    Issue #1548 round-5 exploit 1 fix: ``mover.py``'s HNSW structural-
    completeness gate previously only checked "the index file loads and
    is non-empty" (``index.get_current_count() > 0``) -- never that the
    index actually covers EVERY logical point record present at *root*.
    Codex reproduced a shard with 2 logical vector records but an HNSW
    index built from only 1 of them, which still passed the old check.
    This function exposes the same logical-record enumeration
    ``manifest_digest()`` already uses (``_manifest``) so a caller can
    compare it against the HNSW index's own ``id_mapping`` (written by
    ``HNSWIndexManager._update_metadata`` at build time) and refuse
    "already_complete" on any mismatch.
    """
    return set(_manifest(root).keys())


def hnsw_id_mapping_by_label(target: Path) -> Optional[Dict[int, str]]:
    """Read *target*'s ``collection_meta.json`` and return its HNSW
    ``id_mapping`` as ``{label: point_id}``, or ``None`` on any
    absent/malformed shape (including a non-string value, a key that
    cannot be parsed as an integer label, or two distinct raw keys that
    parse to the SAME integer label -- any of these invalidates the whole
    mapping rather than being silently dropped or overwritten).

    Issue #1548 round-5 exploit 1 fix: ``HNSWIndexManager._update_metadata``
    writes ``hnsw_index.id_mapping`` (label -> point_id) at build time.

    Issue #1548 round-6 exploit fix: this mapping is JSON metadata that is
    just as attacker-writable as everything else in
    ``collection_meta.json`` -- it is used ONLY to translate labels that
    are independently proven to exist in the ACTUAL loaded
    ``hnsw_index.bin`` binary (see ``hnsw_index_covers_all_logical_points``
    below), never trusted on its own for which point ids exist.
    """
    meta_path = target / "collection_meta.json"
    try:
        with meta_path.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        logger.debug(
            "hnsw_id_mapping_by_label: could not read/parse %s (%s) -- "
            "treating id_mapping as absent (fail closed)",
            meta_path,
            exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    hnsw_meta = data.get("hnsw_index")
    if not isinstance(hnsw_meta, dict):
        return None
    id_mapping = hnsw_meta.get("id_mapping")
    if not isinstance(id_mapping, dict):
        return None
    result: Dict[int, str] = {}
    for raw_label, point_id in id_mapping.items():
        if not isinstance(point_id, str):
            return None
        try:
            label = int(raw_label)
        except (TypeError, ValueError):
            return None
        if label in result:
            # Two distinct raw keys (e.g. "1" and "01") parsed to the SAME
            # integer label -- ambiguous/untrustworthy, fail closed rather
            # than silently letting the later entry win.
            return None
        result[label] = point_id
    return result


def hnsw_index_covers_all_logical_points(
    target: Path, actual_label_ids: set[int]
) -> bool:
    """True iff *actual_label_ids* -- translated through *target*'s
    ``id_mapping`` -- covers EXACTLY the set of logical point records
    present at *target*.

    Issue #1548 round-5 exploit 1 fix: Codex reproduced a shard with 2
    logical vector records but an HNSW index built from only 1 of them --
    the pre-fix completeness check only verified "loads and is non-empty",
    which that incomplete index still satisfied.

    Issue #1548 round-6 exploit fix (CRITICAL): the round-5 fix above still
    derived the "covered ids" set from ``collection_meta.json``'s
    ``id_mapping`` alone -- attacker-writable metadata, NOT the actual
    content of ``hnsw_index.bin``. Codex reproduced forging ``id_mapping``
    to claim 2 point ids while the real, loaded binary contained exactly 1
    element (``get_current_count() == 1``, ``get_ids_list() == [0]``); the
    old check still passed. ``actual_label_ids`` MUST be the REAL set of
    integer labels reported by ``index.get_ids_list()`` on the genuinely
    loaded hnswlib index -- this function never re-derives that set from
    metadata, and fails closed on every divergence:
      - an empty *actual_label_ids* (nothing genuinely loaded) fails;
      - any real label absent from ``id_mapping`` fails (a label that
        exists in the binary but has no claimed name cannot be proven to
        be an expected point);
      - two real labels mapping to the same point id fails (ambiguous);
      - the resulting real point-id set must equal the full expected
        logical set exactly -- no missing points, no unclaimed extras.
    """
    if not actual_label_ids:
        return False
    id_mapping = hnsw_id_mapping_by_label(target)
    if id_mapping is None:
        return False
    if not actual_label_ids.issubset(id_mapping.keys()):
        return False
    actual_point_ids = {id_mapping[label] for label in actual_label_ids}
    if len(actual_point_ids) != len(actual_label_ids):
        return False
    try:
        expected_ids = logical_point_ids(target)
    except Exception as exc:
        logger.debug(
            "hnsw_index_covers_all_logical_points: failed to enumerate "
            "logical point ids at %s (%s) -- treating as not covered "
            "(fail closed)",
            target,
            exc,
        )
        return False
    return actual_point_ids == expected_ids


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
