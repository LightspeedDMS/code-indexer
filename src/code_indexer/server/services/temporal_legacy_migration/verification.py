"""Independent, field-level verification for temporal shard relocation."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout
from code_indexer.storage.shared.hnsw_sync_state import HNSW_SYNC_STATE_FILENAME
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

# Issue #1580 round-5 fix: the real staging convention used by
# ``services/temporal/temporal_projection_matrix.py``'s
# ``_atomic_replace_via_tmp`` (``f"{final_path.name}.tmp.{uuid.uuid4().hex}"``,
# e.g. ``projection_matrix.npy.tmp.<32-lowercase-hex-chars>``, written INSIDE
# the shard root ``_structural_manifest`` walks for the Bug #1242/#1264
# ``projection_matrix.npy`` self-heal). ``re.fullmatch`` (whole-string, never
# a substring search) requires EXACTLY 32 lowercase hex characters after a
# literal ``.tmp.`` -- precisely and only the shape ``uuid.uuid4().hex``
# produces -- so this cannot be satisfied by an unrelated content filename
# (e.g. ``something.tmp.json``) or a near-miss suffix of the wrong
# length/case, and therefore does not reopen the round-4 substring-match
# exploit this same file already closed for the bare ``.tmp``/``.lock``
# case.
#
# Issue #1580 round-6 MEDIUM fix (Codex): round 5's ``.+`` prefix matched
# ANY filename ahead of the ``.tmp.<32-hex>`` suffix, so a disguised real
# content filename with a coincidentally valid-shaped suffix -- e.g.
# ``vector_deadbeef.json.tmp.<uuid>`` or
# ``garbage_projection_matrix.npy.tmp.<uuid>`` -- was ALSO wrongly exempted,
# evading detection of a genuine alteration/corruption. The ONLY real
# production writer of this exact staging shape is
# ``_atomic_replace_via_tmp``, which always stages ``projection_matrix.npy``
# specifically (re-confirmed by re-reading that module's current source:
# ``tmp_path = final_path.parent / f"{final_path.name}.tmp.{uuid.uuid4().hex}"``
# where ``final_path.name`` is always the literal string
# ``"projection_matrix.npy"``) -- so the prefix is now anchored to that
# exact filename, never an arbitrary prefix.
_TMP_UUID_STAGING_SUFFIX_PATTERN = re.compile(
    r"projection_matrix\.npy\.tmp\.[0-9a-f]{32}"
)


class VerificationError(IOError):
    """Raised when source and destination records are not identical."""


def _validate_finite_numeric_vector(vector: Any) -> np.ndarray:
    """Validate *vector* is a non-empty, purely finite-numeric 1-D
    sequence and return it as a ``numpy.ndarray`` (dtype inferred, NOT
    forced -- a pure-integer list stays integer-typed, preserving exact
    precision for any downstream caller that needs it) -- raises
    ``VerificationError`` on anything else.

    Issue #1580 round-4 MEDIUM fix (Codex): the pre-fix digest path fed
    *vector* straight into ``np.asarray(vector, dtype=np.float32)``, which
    SILENTLY coerces ``None`` to ``nan``, a numeric string like ``"1"`` to
    ``1.0``, and treats any NaN-holding vector as indistinguishable from
    another NaN-holding vector of different origin. A malformed or
    corrupted record must raise here, not be silently normalized into
    something that happens to hash the same as an unrelated corrupt
    record. ``dtype.kind`` is checked WITHOUT forcing a dtype up front so a
    ``None``/string element (which forces numpy's object/unicode dtype
    kinds) is caught rather than being coerced away by the conversion
    itself.
    """
    if vector is None or isinstance(vector, (str, bytes, dict)):
        raise VerificationError(
            f"invalid temporal vector: {vector!r} is not a numeric sequence"
        )
    try:
        arr = np.asarray(vector)
    except Exception as exc:
        raise VerificationError(
            f"invalid temporal vector: {vector!r} could not be interpreted as an array"
        ) from exc
    if arr.ndim != 1 or arr.size == 0:
        raise VerificationError(
            f"invalid temporal vector: expected a non-empty 1-D sequence, "
            f"got shape {arr.shape}"
        )
    if arr.dtype.kind not in "iuf":
        raise VerificationError(
            f"invalid temporal vector: non-numeric element(s) in {vector!r}"
        )
    if not np.isfinite(arr.astype(np.float64, copy=False)).all():
        raise VerificationError(
            f"invalid temporal vector: contains NaN/Infinite value(s) in {vector!r}"
        )
    return arr


def _normalize_vector_for_digest(vector: Any) -> str:
    """Hex-encode *vector*'s complete float32 bytes -- identical output for
    a Python ``list`` (SHARDED_JSON) or a numerically-equal
    ``numpy.ndarray`` (CHUNKS_DB, ``ChunkStore._decode_vector``).

    Issue #1580 round-2 CRITICAL fix (opus): the previous digest fed a raw
    ``ndarray`` into ``json.dumps(..., default=str)``; numpy's ``str()``
    SUMMARIZES arrays over 1000 elements (first/last 3 only), so a change
    to any MIDDLE element of a real 1024/1536-dim production vector was
    invisible to the digest -- reproduced live, corruption undetected.
    ``.tobytes()`` is complete (every element affects the digest) and
    layout-agnostic (float32 is the precision ``ChunkStore._encode_vector``
    already stores, so a list and an equal-valued ndarray converge
    bit-for-bit), closing the companion cross-layout convergence gap.
    Deliberately no fallback: an unconvertible *vector* must raise, not
    silently digest as something else.

    This is the FLOAT32 path -- correct for a genuine CHUNKS_DB record
    (float32-native storage) and used whenever a cross-layout comparison
    requires both sides to converge on that shared, lossy precision (see
    ``_exact_vector_for_digest`` for the SHARDED_JSON-only alternative).
    """
    arr = _validate_finite_numeric_vector(vector)
    return arr.astype(np.float32).tobytes().hex()


def _exact_vector_for_digest(vector: Any) -> str:
    """Exact-precision string encoding of *vector* -- preserves the EXACT
    decimal value a SHARDED_JSON record was parsed with, never downcast to
    float32.

    Issue #1580 round-4 CRITICAL fix (Codex): round-3's fix made
    ``_normalize_vector_for_digest`` downcast EVERY vector to float32
    before hashing. That is correct for CHUNKS_DB (float32 is genuinely
    what ``ChunkStore._encode_vector`` stores) but WRONG for SHARDED_JSON,
    which stores exact JSON decimal values -- ``[1.0]`` and
    ``[1.0000000000000002]`` are distinct JSON values that collapse to the
    identical float32 bit pattern, so a real corruption in a SHARDED_JSON
    target went undetected.

    Deliberately built on ``repr(list(vector))``, NOT a numpy float64
    cast: ``json.load`` yields plain Python ``float``/``int`` elements, and
    ``repr()`` of a Python float is exact and round-trip-safe (PEP-3101),
    while Python ``int`` is exact at ANY magnitude. A numpy float64 cast
    would silently collapse two distinct large integers that exceed
    float64's 53-bit mantissa (e.g. ``9007199254740992`` vs
    ``9007199254740993``) onto the same value -- a second, subtler
    instance of exactly the precision-loss bug this function exists to
    fix. ``repr()`` of a plain ``list`` never summarizes/truncates
    regardless of length (unlike ``numpy.ndarray.__str__()``, the root
    cause of the round-2 mid-vector-truncation bug), so this is also safe
    against that failure mode.

    Validation runs first (``_validate_finite_numeric_vector``, raises on
    malformed/non-finite input) -- its returned array is discarded here;
    only the validated ORIGINAL elements are digested, so this path never
    silently converts a value validation didn't approve.
    """
    _validate_finite_numeric_vector(vector)
    return repr(list(vector))


def _digest(record: dict[str, Any], *, exact_json: bool = False) -> str:
    """Digest one point record. ``exact_json=True`` selects the
    exact-precision vector encoding instead of the default float32
    encoding -- see ``_manifest``'s layout dispatch for when each is used.
    Defaults to ``exact_json=False`` so every pre-existing direct caller
    (float32, cross-layout-safe) is unaffected.
    """
    normalized = dict(record)
    if "vector" in normalized:
        vector = normalized["vector"]
        normalized["vector"] = (
            _exact_vector_for_digest(vector)
            if exact_json
            else _normalize_vector_for_digest(vector)
        )
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _json_manifest(root: Path, *, exact_json: bool = False) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("vector_*.json")):
        with path.open(encoding="utf-8") as stream:
            record = json.load(stream)
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise VerificationError(f"invalid temporal record: {path}")
        point_id = record["id"]
        if point_id in manifest:
            raise VerificationError(f"duplicate temporal point id: {point_id}")
        manifest[point_id] = _digest(record, exact_json=exact_json)
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


def _manifest(root: Path, *, force_float32: bool = False) -> dict[str, str]:
    """Build the logical point-record manifest for *root*.

    Dispatches on *root*'s own resolved chunk layout (unchanged). For a
    SHARDED_JSON root, ``force_float32`` selects between the two vector
    digest precisions: ``False`` (default) preserves exact JSON decimal
    precision -- correct for a same-layout SHARDED_JSON-to-SHARDED_JSON
    comparison (``manifest_digest``, ``verify_shard_copy``, both of which
    only ever compare a root against itself/an identical-layout copy);
    ``True`` forces the lossy float32 encoding, needed ONLY when comparing
    against a CHUNKS_DB root (which is float32-native) so both sides
    converge on the same, shared precision instead of spuriously
    mismatching on decimal digits CHUNKS_DB's storage never had to begin
    with (see ``verify_source_subset_of_target``'s ``cross_layout`` gate).
    A CHUNKS_DB root always uses its native float32 encoding regardless of
    *force_float32* -- there is no higher precision to fall back to.
    """
    layout = resolve_chunk_layout(root)
    if layout is ChunkLayout.CHUNKS_DB:
        return _chunks_manifest(root)
    return _json_manifest(root, exact_json=not force_float32)


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
        if _is_transient_non_content_artifact(path.name):
            continue
        relative_path = str(path.relative_to(root))
        if _is_sqlite_sidecar_artifact(relative_path):
            continue
        manifest[relative_path] = _file_digest(path)
    return manifest


def _is_transient_non_content_artifact(name: str) -> bool:
    """True iff *name* is a transient, non-content artifact -- an fcntl
    lock file (``.metadata.lock``, ``.index_rebuild.lock``,
    ``temporal_progress.json.lock``) or an atomic-write scratch/temp file
    (this codebase's ubiquitous tmp-then-``os.replace`` pattern, e.g.
    ``temporal_progress.json.tmp``) -- excluded from ``_structural_
    manifest`` ENTIRELY, like ``PROVENANCE_MARKER_NAME`` already is,
    rather than merely allowed to "churn".

    Issue #1580 round-2 fix (opus): inspecting a REAL fixed-root temporal
    shard turned up several of these. Comparing lock/tmp file content is
    meaningless -- a lock file's bytes carry no information, and a
    genuine content file never legitimately uses either naming
    convention in this codebase -- so their transient presence, absence,
    or differing content must never itself cause an "unexpected
    addition" or "altered file" verdict. Matched by an ANCHORED suffix
    rather than an exact-name allowlist so a FUTURE lock/tmp file this
    codebase adds is covered automatically, rather than repeating the
    exact "will keep missing new ones" failure mode this fix exists to
    close.

    Issue #1580 round-4 HIGH fix (Codex): the ``.tmp`` half was previously
    an UNANCHORED substring match (``".tmp" in name``), so a real content
    filename that merely CONTAINS ``.tmp`` anywhere -- e.g.
    ``vector_a.tmpdata.json``, ``something.tmp.json`` -- was wrongly
    exempted from verification entirely, evading detection of a genuine
    alteration/corruption. Confirmed via every real tmp-scratch-file write
    site in this codebase (``hnsw_index_manager.py``, ``id_index_manager.
    py``, ``background_index_rebuilder.py``,
    ``temporal_progressive_metadata.py``, ``temporal_structure_marker.py``,
    ``filesystem_vector_store.py``, ``chunk_layout.py``,
    ``collection_migration.py``, ``collection_dedup_repair.py``,
    ``hnsw_sync_state.py``): every one of THOSE ends with exactly ``.tmp``
    -- none produces a name that merely contains it mid-string. Anchoring
    to ``endswith(".tmp")`` closed that exploit while still covering every
    one of those real naming patterns (a random/pid/tid-prefixed filename
    with a bare ``.tmp`` suffix). The ``.lock`` half was already correctly
    anchored and is unchanged.

    Issue #1580 round-5 fix: the round-4 anchoring above was, however, too
    STRICT for the one real site it was never actually checked against --
    ``temporal_projection_matrix.py``'s ``_atomic_replace_via_tmp`` (the
    Bug #1242/#1264 ``projection_matrix.npy`` self-heal write, which lands
    INSIDE the shard root this module walks) stages via
    ``f"{final_path.name}.tmp.{uuid.uuid4().hex}"`` -- e.g.
    ``projection_matrix.npy.tmp.<32-lowercase-hex-chars>`` -- which does
    NOT end in exactly ``.tmp``. Round 4's anchoring therefore reproduced
    the ORIGINAL #1580 symptom (a legitimate in-place refresh
    misclassified as a collision) for this one write path: a verification
    walk observing the staging file mid-write would trip "unexpected
    file(s)"/"altered file(s)". ``_TMP_UUID_STAGING_SUFFIX_PATTERN`` is
    matched via ``re.fullmatch`` (whole-string, never a substring search)
    and requires EXACTLY 32 lowercase hex characters after a literal
    ``.tmp.`` -- precisely and only the shape ``uuid.uuid4().hex``
    produces -- so it cannot be satisfied by an unrelated content filename
    (e.g. ``something.tmp.json``, or a near-miss suffix of the wrong
    length/case) and does not reopen the round-4 exploit.
    """
    if name.endswith(".lock") or name.endswith(".tmp"):
        return True
    return _TMP_UUID_STAGING_SUFFIX_PATTERN.fullmatch(name) is not None


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
    # Issue #1581 fix: a genuine CHUNKS_DB record's vector is a
    # ``numpy.ndarray`` (``ChunkStore._decode_vector`` / ``_row_to_record``),
    # NEVER a ``list`` -- the SHARDED_JSON branch's ``json.load`` always
    # yields a ``list`` instead. ``isinstance(vector, list)`` unconditionally
    # rejected every CHUNKS_DB shard regardless of validity, permanently
    # misclassifying it as incomplete on every completeness check that
    # depends on this function. Deliberately type-agnostic (accept anything
    # with a ``len()``, excluding str/bytes/dict which have one but are
    # never vectors) rather than special-casing ``numpy.ndarray`` alongside
    # ``list``, so a future array-like storage type does not repeat this
    # exact bug class a third time.
    if vector is None or isinstance(vector, (str, bytes, dict)):
        return None
    try:
        length = len(vector)
    except TypeError:
        return None
    if not length:
        return None
    return length


def verify_shard_copy(source: Path, published: Path) -> None:
    """Freshly read every source/destination record and compare all fields."""
    source_manifest = _manifest(source)
    destination_manifest = _manifest(published)
    if source_manifest != destination_manifest:
        raise VerificationError(
            f"temporal shard verification failed: {source} != {published}"
        )


_CHUNKS_DB_FILENAME = "chunks.db"
_ID_INDEX_FILENAME = "id_index.bin"

# Issue #1580 round-6 HIGH fix (Codex): the SQLite rollback-journal/WAL/SHM
# sidecar files that ``chunks.db`` itself can legitimately grow during an
# in-progress write. ``storage/sqlite_chunk_store.py``'s ``ChunkStore``
# unconditionally sets ``PRAGMA journal_mode=DELETE`` on every mutable
# open (never WAL) -- so in THIS codebase only ``chunks.db-journal`` is
# ever actually produced; the WAL/SHM names are included defensively in
# case ``journal_mode`` is ever reconfigured for a different code path.
# Before this fix, a verification walk observing ``chunks.db-journal``
# mid-write (a real, in-progress CHUNKS_DB write -- e.g. Bug #1529's
# in-place refresh) tripped "unexpected file(s)"/"altered file(s)" -- the
# exact false-collision symptom #1580 exists to close, this time for
# CHUNKS_DB targets specifically.
#
# Deliberately matched by EXACT root-relative path (never a basename
# match at any depth, unlike ``_is_transient_non_content_artifact``'s
# lock/tmp patterns) -- ``chunks.db`` itself is only ever created directly
# at a shard's root (``_CHUNKS_DB_FILENAME`` is joined straight onto
# ``root``/``target``, never nested), so its journal/WAL sidecars can
# never legitimately appear anywhere else either; a nested file
# coincidentally sharing one of these names is a genuine anomaly, not
# transient churn, and must still be reported. Symlinks are already
# rejected unconditionally, for every file in the walked tree, by
# ``_structural_manifest``'s existing Issue #1548 round-4 check -- this
# exemption is only ever consulted for a real regular file.
_SQLITE_SIDECAR_ROOT_FILENAMES = frozenset(
    {
        f"{_CHUNKS_DB_FILENAME}-journal",
        f"{_CHUNKS_DB_FILENAME}-wal",
        f"{_CHUNKS_DB_FILENAME}-shm",
    }
)


def _is_sqlite_sidecar_artifact(relative_path: str) -> bool:
    """True iff *relative_path* is EXACTLY one of the three standard
    SQLite rollback-journal/WAL/SHM sidecar names for ``chunks.db``, at
    the shard root. See ``_SQLITE_SIDECAR_ROOT_FILENAMES`` for why this is
    an exact-set membership test rather than a generic
    ``*-journal``/``*-wal``/``*-shm`` suffix pattern, and why it is never
    a basename-at-any-depth match.
    """
    return relative_path in _SQLITE_SIDECAR_ROOT_FILENAMES


# Issue #1580 adversarial-review round-2 critical fix: these artifacts are
# legitimate churn at the shard ROOT regardless of layout -- every real
# shard, either layout, has its own hnsw_index.bin/collection_meta.json.
# ``chunks.db``/``id_index.bin`` are handled separately below since they
# are layout-specific.
#
# Issue #1580 round-2, SECOND finding (opus): inspecting a REAL fixed-root
# temporal shard turned up ``path_index.bin``
# (``FilesystemVectorStore._save_path_index``, rewritten wholesale by
# EVERY begin/end-indexing cycle regardless of layout) and
# ``temporal_progress.json`` (``TemporalProgressiveMetadata.mark_
# completed``/the CLI temporal watch handler's post-index update,
# likewise rewritten by every ordinary refresh regardless of layout) --
# without these two, a real in-place refresh tripped "unexpectedly
# altered" and was misclassified as a permanent collision.
_ROOT_ONLY_CHURN_FILENAMES = frozenset(
    {
        "hnsw_index.bin",
        "collection_meta.json",
        "path_index.bin",
        "temporal_progress.json",
        # Bug #1619: the dedicated hnsw_sync dirty-marker sidecar file
        # (write_hnsw_sync_state()) is rewritten on EVERY
        # upsert_points()/delete_points() call -- the single highest-churn
        # file in a collection root, churnier than path_index.bin/
        # temporal_progress.json above.
        HNSW_SYNC_STATE_FILENAME,
    }
)

# Story #1456 permanently retires ``id_index.bin`` for CHUNKS_DB
# collections -- it is wholesale-rewritten churn (``IDIndexManager.
# save_index``, called from ``_save_path_index``) ONLY for a
# SHARDED_JSON-layout target, mirroring ``chunks.db``'s CHUNKS_DB-only
# gating below.
_ID_INDEX_LAYOUT_GATE = ChunkLayout.SHARDED_JSON

# ``temporal_indexer.py`` writes these ONLY when they are ABSENT
# (self-heal-on-missing: Bug #1242's broken-shard repair for the
# projection matrix, and the v2-structure-marker belt-and-suspenders write
# gated on ``not is_v2_structure(...)``) -- never as a wholesale rewrite
# of an already-present file. A target may therefore legitimately GAIN
# either as a new addition the legacy source never had, but if either
# already exists at BOTH sides it must remain byte-identical: an
# unexpected in-place ALTERATION is a genuine anomaly (e.g. a different
# embedder model or structure version), never something an ordinary
# refresh produces, and must still fail loud. Deliberately excluded from
# ``_ROOT_ONLY_CHURN_FILENAMES`` (full churn, additions AND alterations)
# for exactly this reason -- see ``_is_expected_addition``.
_ADDITION_ONLY_ARTIFACT_FILENAMES = frozenset(
    {"projection_matrix.npy", "temporal_structure.json"}
)


def _is_expected_churn_file(
    target: Path, relative_path: str, target_layout: ChunkLayout
) -> bool:
    """True iff *relative_path* -- a path that must exist at *target* --
    is one of the specific artifacts a legitimate in-place refresh (Bug
    #1529) is expected to rewrite wholesale (the rebuilt HNSW index, its
    metadata, the rewritten chunks database, the path/id index, or the
    progress record) or add (a NEW per-point vector record file, for a
    ``SHARDED_JSON``-layout target only). Used by
    ``verify_source_subset_of_target`` to tell genuine additive evolution
    apart from an unrelated foreign file. Deliberately NEVER used for
    ``_ADDITION_ONLY_ARTIFACT_FILENAMES`` -- see ``_is_expected_addition``.

    Issue #1580 adversarial-review round-2 critical finding: the previous
    implementation matched on ``Path(relative_path).name`` -- the bare
    basename, unanchored to location -- so a nested variant of any
    allowed filename at ANY depth was silently accepted as "expected
    churn", ``chunks.db`` was accepted regardless of whether the target's
    OWN resolved layout even uses that artifact, a directory or symlink
    sharing an allowed basename was never rejected as such, and none of
    the patterns' identity was independently re-confirmed. Fixed by:

      1. Matching every fixed-name artifact by the EXACT root-relative
         path (``relative_path`` itself, never just its basename) -- none
         of these files is ever legitimately nested in any layout this
         codebase produces, so a nested variant is never recognized as
         churn.
      2. Gating ``chunks.db`` additionally on *target_layout* being
         ``ChunkLayout.CHUNKS_DB``, and ``id_index.bin`` on it being
         ``ChunkLayout.SHARDED_JSON`` -- neither artifact is ever
         legitimately owned by the OTHER layout, so planting one there
         (regardless of path) is never treated as churn.
      3. Gating ``vector_*.json`` additions on *target_layout* being
         ``ChunkLayout.SHARDED_JSON`` -- a ``CHUNKS_DB`` target's real
         data lives exclusively in ``chunks.db``; ``_manifest()`` never
         scans ``vector_*.json`` for that layout, so such a file is
         invisible to logical-record verification and must never be
         silently trusted as churn there.
      4. Independently re-confirming every candidate is a real,
         non-symlinked regular file AT TARGET
         (``full_path.is_file()``/``not full_path.is_symlink()``) --
         defense in depth matching this module's existing symlink
         -rejection discipline (Issue #1548 round-4), so neither a
         directory nor a symlink sharing an allowed name/path is ever
         treated as churn.
    """
    full_path = target / relative_path
    if full_path.is_symlink() or not full_path.is_file():
        return False
    if relative_path in _ROOT_ONLY_CHURN_FILENAMES:
        return True
    if relative_path == _CHUNKS_DB_FILENAME:
        return target_layout is ChunkLayout.CHUNKS_DB
    if relative_path == _ID_INDEX_FILENAME:
        return target_layout is _ID_INDEX_LAYOUT_GATE
    name = Path(relative_path).name
    if name.startswith("vector_") and name.endswith(".json"):
        return target_layout is ChunkLayout.SHARDED_JSON
    return False


def _is_expected_addition(
    target: Path, relative_path: str, target_layout: ChunkLayout
) -> bool:
    """True iff *relative_path* may legitimately appear as a NEW file at
    *target* that the legacy *source* never had -- either because it is
    wholesale-rewritten churn (``_is_expected_churn_file``) or because it
    is a self-heal-on-missing artifact
    (``_ADDITION_ONLY_ARTIFACT_FILENAMES``) that an ordinary refresh adds
    exactly once and never touches again.

    Deliberately NOT used for the "altered" check in
    ``verify_source_subset_of_target`` -- an addition-only artifact that
    IS present at both source and target must still remain byte-identical
    (see ``_ADDITION_ONLY_ARTIFACT_FILENAMES``'s docstring); only
    ``_is_expected_churn_file`` governs tolerated alterations.
    """
    if _is_expected_churn_file(target, relative_path, target_layout):
        return True
    full_path = target / relative_path
    if full_path.is_symlink() or not full_path.is_file():
        return False
    return relative_path in _ADDITION_ONLY_ARTIFACT_FILENAMES


_SHARDED_JSON_NATIVE_FILENAMES = frozenset({_ID_INDEX_FILENAME})


def _is_layout_migrated_absence(
    target: Path, relative_path: str, target_layout: ChunkLayout
) -> bool:
    """True iff *relative_path* is a SHARDED_JSON-native artifact
    (``id_index.bin`` or a ``vector_*.json`` point file) that is
    GENUINELY ABSENT at *target* because *target*'s own resolved chunk
    layout is CHUNKS_DB, which never uses that artifact family at all --
    the expected structural fingerprint of an independent storage-layout
    migration (Bug #1528's in-place ``chunks.db`` consolidation of the
    fixed-root target), never data loss.

    The logical layer (``_manifest()``, dispatched by
    ``resolve_chunk_layout``) already re-verifies every point record
    survives that migration content-for-content in
    ``verify_source_subset_of_target``, before this function is ever
    consulted; this only tolerates the STRUCTURAL fingerprint the
    migration leaves behind. Must be genuinely absent, not merely
    unreadable -- an existing file (of any kind, including a symlink) at
    *relative_path* is never treated as a migrated absence.

    Absence is proven via ``os.stat`` + ``errno`` inspection, never
    ``Path.exists()``: ``errno.ENOENT`` means genuinely absent; any other
    errno (permission denied, a stale handle, ``ENOTDIR``, ...) raises
    ``VerificationError`` instead of guessing.
    """
    if target_layout is not ChunkLayout.CHUNKS_DB:
        return False
    if os.path.isabs(relative_path) or ".." in Path(relative_path).parts:
        return False  # defensive: never resolve a path escaping *target*
    full_path = target / relative_path
    if full_path.is_symlink():
        return False
    try:
        os.stat(full_path)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise VerificationError(
                f"temporal shard subset verification could not determine "
                f"whether {full_path} exists -- refusing to guess "
                f"(fail closed): {exc}"
            ) from exc
    else:
        return False
    if relative_path in _SHARDED_JSON_NATIVE_FILENAMES:
        return True
    name = Path(relative_path).name
    return name.startswith("vector_") and name.endswith(".json")


def verify_source_subset_of_target(source: Path, target: Path) -> None:
    """Freshly read every *source* record/file and confirm it is present,
    field-for-field (or byte-for-byte) identical, in *target*'s CURRENT
    data. Unlike ``verify_shard_copy`` (exact equality), *target* MAY hold
    ADDITIONS beyond *source*'s -- the expected shape of a legitimately
    evolved, in-place-refreshed fixed-root shard (Bug #1529: the ordinary
    temporal write path keeps indexing new commits at the fixed root after
    this migration mechanism originally published it there) -- but ONLY
    additions this codebase actually produces (``_is_expected_addition``).

    Issue #1580: authorizing legacy-source deletion only ever requires
    proving the source's data has not been lost, not that the target
    remains byte-for-byte equal to the source. Two layers, both required:
    (1) logical records -- every point id source has must exist in target
    with an identical digest; (2) structural files -- any file present at
    target but not source must be recognized refresh churn (an
    UNRELATED foreign file is a collision, closing the exact gap Issue
    #1548 round-4's exploit hardening exists to prevent), and any
    non-churn file present in BOTH trees must remain byte-identical. A
    SHARDED_JSON-native file present ONLY at source, genuinely absent at
    a CHUNKS_DB target, is tolerated (``_is_layout_migrated_absence``) --
    that data was already re-verified content-for-content at the logical
    layer above; its old physical file simply no longer exists.

    Issue #1580 adversarial-review round-2: an EMPTY source manifest is
    refused outright, before any other check -- a source with zero
    logical point records proves nothing was ever verified there, so it
    can never serve as positive proof that "nothing was lost" and must
    never vacuously authorize treating the target as safe to delete.

    Issue #1580 round-4 CRITICAL fix (Codex): the logical-record manifests
    for *source* and *target* must be computed with MATCHING vector
    precision. When both sides share the same resolved chunk layout (the
    common case), each side's own natural precision agrees already. When
    the layouts DIFFER -- source still SHARDED_JSON, target evolved to
    CHUNKS_DB via Bug #1528's independent in-place consolidation -- the
    SHARDED_JSON side must be forced onto the SAME lossy float32 precision
    CHUNKS_DB is native to, or a value that legitimately lost precision
    during that storage migration would spuriously fail to converge.
    """
    source_layout = resolve_chunk_layout(source)
    target_layout = resolve_chunk_layout(target)
    cross_layout = source_layout is not target_layout
    source_manifest = _manifest(source, force_float32=cross_layout)
    if not source_manifest:
        raise VerificationError(
            f"temporal shard subset verification refused: {source} has an "
            f"empty point-record manifest -- an empty source can never be "
            f"positive proof that its data survived at {target}, so it "
            f"must never vacuously authorize treating the target as "
            f"convergent/safe to delete"
        )
    target_manifest = _manifest(target, force_float32=cross_layout)
    missing_or_diverged = sorted(
        point_id
        for point_id, digest in source_manifest.items()
        if target_manifest.get(point_id) != digest
    )
    if missing_or_diverged:
        raise VerificationError(
            f"temporal shard subset verification failed: {source} is not "
            f"fully preserved within {target} -- point id(s) missing or "
            f"diverged: {missing_or_diverged}"
        )
    source_structural = _structural_manifest(source)
    target_structural = _structural_manifest(target)
    unexpected_additions = sorted(
        relative_path
        for relative_path in target_structural
        if relative_path not in source_structural
        and not _is_expected_addition(target, relative_path, target_layout)
    )
    if unexpected_additions:
        raise VerificationError(
            f"temporal shard subset verification failed: {target} has "
            f"unexpected file(s) not present at {source} and not "
            f"recognized as expected refresh churn: {unexpected_additions}"
        )
    unexpectedly_altered = sorted(
        relative_path
        for relative_path, digest in source_structural.items()
        if not _is_expected_churn_file(target, relative_path, target_layout)
        and not _is_layout_migrated_absence(target, relative_path, target_layout)
        and target_structural.get(relative_path) != digest
    )
    if unexpectedly_altered:
        raise VerificationError(
            f"temporal shard subset verification failed: {target} has "
            f"altered non-churn file(s) relative to {source}: "
            f"{unexpectedly_altered}"
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
