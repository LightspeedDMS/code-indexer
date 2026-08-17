"""Issue #1580 adversarial-review round 4 (Codex) findings.

Round 3's own fix (universal float32 downcast in
``verification._normalize_vector_for_digest``) genuinely fixed the round-2/3
mid-vector-truncation bug (confirmed by reproduction), but introduced/left
four issues of its own:

1. CRITICAL regression -- downcasting EVERY vector to float32 before
   hashing is correct for CHUNKS_DB (``ChunkStore._encode_vector`` genuinely
   stores float32) but wrong for SHARDED_JSON, which stores exact JSON
   decimal values. ``{"vector": [1.0]}`` and
   ``{"vector": [1.0000000000000002]}`` are distinct JSON values that hash
   IDENTICALLY after the float32 downcast -- a real corruption in a
   SHARDED_JSON target could be silently accepted.
2. HIGH -- ``temporal_metadata.db`` (+ WAL/SHM sidecars) is not in any
   allowlist tier. Proven below to be a non-issue: it lives in the SEPARATE
   shared bookkeeping directory (``LEGACY_TEMPORAL_COLLECTION``, a sibling
   of every quarter shard), synced via ``mover._sync_metadata_scope`` --
   verification.py's per-shard functions never receive that path as
   ``source``/``target``.
3. HIGH -- the transient-file exemption (``".tmp" in name``) is an
   unanchored substring match: ``vector_a.tmpdata.json``,
   ``something.tmp.json`` contain ``.tmp`` as a substring but are not
   transient scratch files -- real content could evade verification.
4. MEDIUM -- invalid/malformed vectors are silently coerced instead of
   rejected: ``None`` -> NaN, ``"1"`` -> ``1.0``, and NaN-holding vectors of
   different origin hash identically.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from code_indexer.server.services.temporal_legacy_migration.mover import (
    MigrationResult,
    migrate_temporal_shards,
    _discover_shards,
)
from code_indexer.server.services.temporal_legacy_migration.verification import (
    VerificationError,
    _digest,
    _is_transient_non_content_artifact,
    verify_shard_copy,
    verify_source_subset_of_target,
)
from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _make_sharded_json_shard(path: Path, point_id: str, vector: list) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "collection_meta.json").write_text('{"name": "q1"}')
    record = {"id": point_id, "vector": vector, "payload": {"source": "x"}}
    (path / f"vector_{point_id}.json").write_text(json.dumps(record))


def _make_chunks_db_shard(path: Path, point_id: str, vector: list) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "collection_meta.json").write_text(
        json.dumps({"chunks_db": {"version": 1}})
    )
    store = ChunkStore(path / "chunks.db")
    try:
        store.write_batch(
            [{"id": point_id, "vector": vector, "payload": {"source": "x"}}]
        )
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Finding 1 (CRITICAL): SHARDED_JSON-to-SHARDED_JSON must preserve exact
# JSON decimal precision, never downcast to float32.
# ---------------------------------------------------------------------------


def test_sharded_json_digest_distinguishes_values_that_collapse_under_float32():
    """Direct unit reproduction: two distinct JSON decimal values that are
    NOT distinguishable in float32 (``1.0`` vs ``1.0000000000000002``) must
    still produce DIFFERENT digests when both records are SHARDED_JSON
    (``exact_json=True`` path) -- this is the exact corruption class the
    round-2 float32 normalizer made invisible.

    RED against pre-fix code: ``_digest()`` had no ``exact_json`` parameter
    at all and always downcast via ``_normalize_vector_for_digest``, so
    both values round to the same float32 bit pattern and the two digests
    were IDENTICAL -- this assertion fails on the unpatched module.
    """
    record_a = {"id": "p1", "vector": [1.0]}
    record_b = {"id": "p1", "vector": [1.0000000000000002]}
    # Sanity: these two floats really are distinct at float64 precision but
    # collapse to the identical float32 bit pattern.
    assert record_a["vector"][0] != record_b["vector"][0]
    assert np.float32(record_a["vector"][0]) == np.float32(record_b["vector"][0])

    digest_a = _digest(record_a, exact_json=True)
    digest_b = _digest(record_b, exact_json=True)
    assert digest_a != digest_b


def test_sharded_json_to_sharded_json_verify_shard_copy_catches_precision_corruption(
    tmp_path: Path,
):
    """Integration-level version of the same reproduction through the real
    verification entry point ``verify_shard_copy`` -- a genuinely corrupted
    SHARDED_JSON target (last-bit-precision-different vector, undetectable
    in float32) must be REJECTED, not silently accepted as a valid copy.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0])
    _make_sharded_json_shard(target, "p1", [1.0000000000000002])

    with pytest.raises(VerificationError):
        verify_shard_copy(source, target)


def test_sharded_json_to_sharded_json_still_matches_for_genuinely_identical_data(
    tmp_path: Path,
):
    """Guard-rail: the precision fix must not become over-sensitive --
    byte-for-byte identical SHARDED_JSON data must still verify clean.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0, 2.5, -3.75])
    _make_sharded_json_shard(target, "p1", [1.0, 2.5, -3.75])

    verify_shard_copy(source, target)  # must not raise


def test_cross_layout_convergence_still_works_after_precision_fix(tmp_path: Path):
    """Guard-rail: fixing SHARDED_JSON-to-SHARDED_JSON precision must not
    break the legitimate cross-layout tolerance Bug #1528's in-place
    ``chunks.db`` consolidation depends on -- a SHARDED_JSON legacy source
    and its CHUNKS_DB-consolidated fixed-root target (float32-native
    storage) must still converge for the SAME logical vector.
    """
    vector = [1.0, 2.0, 3.0, 4.0]
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", vector)
    _make_chunks_db_shard(target, "p1", vector)

    verify_source_subset_of_target(source, target)  # must not raise


def test_cross_layout_precision_loss_from_float32_storage_does_not_false_positive(
    tmp_path: Path,
):
    """A value that is NOT exactly representable in float32
    (``1.0000000000000002``) legitimately loses precision when Bug #1528's
    consolidation stores it in ``chunks.db`` (float32-native). This is
    EXPECTED, lossy-but-legitimate storage precision, not corruption -- the
    cross-layout comparison must tolerate it (never demand float64-exact
    equality against a store that only ever had float32 precision).
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0000000000000002])
    # ChunkStore genuinely stores float32 -- this is what "the value survived
    # the legitimate storage migration" looks like.
    _make_chunks_db_shard(target, "p1", [1.0000000000000002])

    verify_source_subset_of_target(source, target)  # must not raise


def test_cross_layout_corruption_still_caught_after_precision_fix(tmp_path: Path):
    """Guard-rail: cross-layout tolerance must not become a loophole -- a
    genuinely different value must still be rejected across layouts.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0, 2.0, 3.0])
    _make_chunks_db_shard(target, "p1", [1.0, 999.0, 3.0])

    with pytest.raises(VerificationError):
        verify_source_subset_of_target(source, target)


# ---------------------------------------------------------------------------
# Finding 2 (HIGH, proven a non-issue): temporal_metadata.db lives OUTSIDE
# any shard root that verification.py's functions ever operate on.
# ---------------------------------------------------------------------------


def test_shared_metadata_bookkeeping_directory_is_never_discovered_as_a_shard(
    tmp_path: Path,
):
    """``_discover_shards`` (mover.py) enumerates only directories starting
    with ``code-indexer-temporal-`` (quarter/embedder shards). The bare
    ``code-indexer-temporal`` bookkeeping directory
    (``LEGACY_TEMPORAL_COLLECTION``) -- where ``TemporalMetadataSqliteBackend``
    creates ``temporal_metadata.db`` (+ ``-wal``/``-shm`` in WAL mode) --
    does not match that prefix (missing the trailing dash) and is never
    returned. This proves ``temporal_metadata.db`` can never be one of the
    ``source``/``target`` roots passed into ``verify_source_subset_of_target``
    / ``_structural_manifest`` -- it is synced through a completely separate
    mechanism (``mover._sync_metadata_scope``), so no allowlist entry is
    needed here for it or its WAL/SHM sidecars.
    """
    legacy_root = tmp_path / "legacy"
    shard_dir = legacy_root / "code-indexer-temporal-e-2026Q1"
    _make_sharded_json_shard(shard_dir, "p1", [1.0])

    bookkeeping_dir = legacy_root / LEGACY_TEMPORAL_COLLECTION
    bookkeeping_dir.mkdir(parents=True, exist_ok=True)
    (bookkeeping_dir / "temporal_metadata.db").write_bytes(b"sqlite-bytes")
    (bookkeeping_dir / "temporal_metadata.db-wal").write_bytes(b"wal-bytes")
    (bookkeeping_dir / "temporal_metadata.db-shm").write_bytes(b"shm-bytes")

    discovered = _discover_shards(legacy_root)
    assert discovered == [shard_dir]
    assert bookkeeping_dir not in discovered


def test_metadata_db_mutation_never_affects_shard_level_verification(tmp_path: Path):
    """Even while the sibling bookkeeping directory's ``temporal_metadata.db``
    (+ WAL/SHM) is freely mutated -- exactly what an ordinary concurrent
    temporal refresh does -- ``verify_source_subset_of_target`` over the
    ACTUAL shard roots is completely unaffected, because those roots never
    include the bookkeeping directory's path at all.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _make_sharded_json_shard(shard, "p1", [1.0])

    first: MigrationResult = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True
    )
    assert first.published == 1
    assert first.failed == 0
    fixed_shard = fixed / shard.name

    for root in (legacy, fixed):
        meta_dir = root / LEGACY_TEMPORAL_COLLECTION
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "temporal_metadata.db").write_bytes(b"mutated-differently")
        (meta_dir / "temporal_metadata.db-wal").write_bytes(b"mutated-wal-content")

    verify_source_subset_of_target(shard, fixed_shard)  # must not raise


# ---------------------------------------------------------------------------
# Finding 3 (HIGH): transient-file exemption must be an anchored suffix
# match, never an unanchored substring match.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "vector_a.tmpdata.json",
        "something.tmp.json",
        "vector_x.tmp_backup.json",
    ],
)
def test_real_content_filenames_containing_tmp_substring_are_not_exempted(name: str):
    """RED against pre-fix code: ``_is_transient_non_content_artifact``
    matched on ``".tmp" in name`` (unanchored substring), so a real content
    filename that merely CONTAINS ``.tmp`` anywhere was wrongly exempted
    from verification entirely. None of these names is a genuine
    lock/scratch-file naming convention this codebase produces (real ones
    always END with exactly ``.lock`` or ``.tmp``) -- they must be treated
    as ordinary content, not transient churn.
    """
    assert _is_transient_non_content_artifact(name) is False


@pytest.mark.parametrize(
    "name",
    [
        ".metadata.lock",
        ".index_rebuild.lock",
        "temporal_progress.json.lock",
        "temporal_progress.json.tmp",
        ".tmp_hnsw_abc123.tmp",
        "vector_p1.12345.67890.tmp",
    ],
)
def test_genuine_lock_and_tmp_filenames_are_still_exempted(name: str):
    """Guard-rail: the anchored fix must not regress the genuine, real
    lock/scratch naming conventions this codebase's write paths actually
    produce (``file_locking``/atomic-write ``.tmp``-suffixed scratch files,
    ``.lock``-suffixed fcntl lock files).
    """
    assert _is_transient_non_content_artifact(name) is True


def test_altered_content_disguised_with_tmp_substring_is_detected_as_corruption(
    tmp_path: Path,
):
    """Integration-level reproduction: a genuine, non-transient file whose
    name merely CONTAINS ``.tmp`` as a substring (never a real scratch file
    in this codebase -- genuine ones always END with exactly ``.tmp``) is
    present, byte-identical, at BOTH source and target -- then genuinely
    ALTERED at the target only. This must be caught as corruption, proving
    the exploit Codex reproduced (evading verification by choosing a
    ``.tmp``-containing name) is closed.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", [1.0])
    _make_sharded_json_shard(target, "p1", [1.0])

    (source / "config.tmp.json").write_text(json.dumps({"note": "original"}))
    (target / "config.tmp.json").write_text(json.dumps({"note": "original"}))
    verify_source_subset_of_target(source, target)  # baseline: must not raise

    (target / "config.tmp.json").write_text(json.dumps({"note": "corrupted"}))

    with pytest.raises(VerificationError):
        verify_source_subset_of_target(source, target)


# ---------------------------------------------------------------------------
# Finding 4 (MEDIUM): malformed vectors must be rejected, not coerced --
# on BOTH digest paths (the default float32 path AND the new exact-JSON
# path), since an implementation could validate one and not the other.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vector",
    [None, "1", [None], [float("nan")]],
    ids=["none-vector", "string-vector", "none-element", "nan-element"],
)
@pytest.mark.parametrize(
    "exact_json", [False, True], ids=["float32-path", "exact-json-path"]
)
def test_malformed_or_non_finite_vectors_are_rejected_rather_than_coerced(
    vector, exact_json: bool
):
    """Codex's exact reproduction: ``None`` silently converts to NaN, the
    string ``"1"`` silently converts to a valid float, and both
    ``{"vector": None}``/``{"vector": [None]}`` hash identically to their
    NaN equivalents under the pre-fix normalizer. Each must now raise
    instead of being silently coerced into something hashable -- on BOTH
    the default (float32) and the new ``exact_json=True`` digest path, so a
    fix that only validates one path is not sufficient.
    """
    with pytest.raises(VerificationError):
        _digest({"id": "p1", "vector": vector}, exact_json=exact_json)
