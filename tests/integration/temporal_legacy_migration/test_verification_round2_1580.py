"""Issue #1580 adversarial-review round 2 (opus) -- digest completeness and
cross-layout convergence.

Two related findings on the per-record digest function
(``verification._digest``, shared by ``_json_manifest``/``_chunks_manifest``
-> ``_manifest`` -> ``verify_shard_copy``/``verify_source_subset_of_target``/
``manifest_digest``):

1. CRITICAL -- a CHUNKS_DB record's ``vector`` field is a real
   ``numpy.ndarray`` (``ChunkStore._decode_vector``). Feeding it into
   ``json.dumps(..., default=str)`` calls Python's ``str()`` on the array,
   which numpy SUMMARIZES for arrays over 1000 elements (showing only the
   first/last 3 elements). Production vector dimensions (VoyageAI
   voyage-code-3: 1024, voyage-large-2: 1536) both exceed this threshold,
   so a change to any MIDDLE element was completely invisible to the
   digest -- corrupting 2 middle elements of a real vector in a real
   ``ChunkStore`` left the digest unchanged, meaning
   ``verify_source_subset_of_target`` would have authorized deleting the
   legacy source while silently accepting corrupted target data.

2. HIGH -- a Python ``list`` (SHARDED_JSON, straight from ``json.load``)
   and a numerically-identical ``numpy.ndarray`` (CHUNKS_DB) serialize
   completely differently under ``json.dumps``, so cross-layout comparison
   never converged even for logically identical data -- a real production
   combination, since Bug #1528's in-place ``chunks.db`` consolidation of
   the fixed-root TARGET runs independently of (and typically ahead of)
   this migration mechanism's verify-and-delete pass over the
   (permanently SHARDED_JSON) legacy SOURCE.
"""

import json
from pathlib import Path
from typing import Tuple

import numpy as np
import pytest

from code_indexer.server.services.temporal_legacy_migration.verification import (
    VerificationError,
    _digest,
    verify_shard_copy,
    verify_source_subset_of_target,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore

# Corruption is applied strictly BETWEEN a numpy array's first/last 3
# printed elements (the summarization edge for arrays over 1000 elements)
# -- this is the exact region the pre-fix bug rendered invisible.
_CORRUPTION_DELTA = 5.0


def _write_point_to_chunks_db(
    path: Path, point_id: str, vector: list, source_tag: str
) -> None:
    store = ChunkStore(path / "chunks.db")
    try:
        store.write_batch(
            [{"id": point_id, "vector": vector, "payload": {"source": source_tag}}]
        )
    finally:
        store.close()


def _make_chunks_db_shard(
    path: Path, point_id: str, vector: list, source_tag: str
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "collection_meta.json").write_text(
        json.dumps({"chunks_db": {"version": 1}})
    )
    _write_point_to_chunks_db(path, point_id, vector, source_tag)


def _make_sharded_json_shard(
    path: Path, point_id: str, vector: list, source_tag: str
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "collection_meta.json").write_text('{"name": "q1"}')
    record = {"id": point_id, "vector": vector, "payload": {"source": source_tag}}
    (path / f"vector_{point_id}.json").write_text(json.dumps(record))


def _make_matching_chunks_db_pair(
    tmp_path: Path, vector: list, point_id: str = "p1"
) -> Tuple[Path, Path]:
    """Build a source/target pair of CHUNKS_DB shards holding the SAME
    logical record -- shared setup for the large-vector digest tests.
    """
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_chunks_db_shard(source, point_id, vector, "same")
    _make_chunks_db_shard(target, point_id, vector, "same")
    return source, target


def _corrupt_middle_elements(vector: list, *indices: int) -> list:
    """Return a copy of *vector* with each of *indices* nudged by
    ``_CORRUPTION_DELTA`` -- every index must fall strictly between the
    first/last 3 elements numpy's ``str()`` prints for an array over 1000
    elements, the exact region the pre-fix digest could not see.
    """
    corrupted = list(vector)
    for index in indices:
        corrupted[index] += _CORRUPTION_DELTA
    return corrupted


def test_digest_detects_mid_vector_corruption_in_real_1024_dim_chunks_db_record(
    tmp_path: Path,
):
    """Opus's exact reproduction at VoyageAI's voyage-code-3 dimension
    (1024). Corrupting 2 MIDDLE elements must be caught by
    ``verify_shard_copy`` -- RED against the unpatched ``_digest()``
    (numpy ``str()`` summarization hides any change strictly between the
    first 3 and last 3 elements of an array over 1000 elements), GREEN
    once the digest hashes the vector's complete raw bytes.
    """
    vector = np.random.default_rng(1024).standard_normal(1024).tolist()
    source, target = _make_matching_chunks_db_pair(tmp_path, vector)

    # Sanity: identical, uncorrupted data must verify clean first.
    verify_shard_copy(source, target)

    corrupted = _corrupt_middle_elements(vector, len(vector) // 2, len(vector) // 2 + 1)
    _write_point_to_chunks_db(target, "p1", corrupted, "same")

    with pytest.raises(VerificationError):
        verify_shard_copy(source, target)


def test_digest_detects_mid_vector_corruption_in_real_1536_dim_chunks_db_record(
    tmp_path: Path,
):
    """Same reproduction at VoyageAI's other real production dimension
    (voyage-large-2, 1536) -- confirms the fix is not narrowly tied to one
    specific dimension or a specific summarization edge.
    """
    vector = np.random.default_rng(1536).standard_normal(1536).tolist()
    source, target = _make_matching_chunks_db_pair(tmp_path, vector)

    corrupted = _corrupt_middle_elements(vector, len(vector) // 2)
    _write_point_to_chunks_db(target, "p1", corrupted, "same")

    with pytest.raises(VerificationError):
        verify_shard_copy(source, target)


def test_digest_still_matches_for_genuinely_identical_large_vectors(tmp_path: Path):
    """Guard-rail: the fix must not become over-sensitive -- two genuinely
    identical large (>1000 element) vectors must still verify clean.
    """
    vector = np.random.default_rng(2026).standard_normal(1024).tolist()
    source, target = _make_matching_chunks_db_pair(tmp_path, vector)

    verify_shard_copy(source, target)  # must not raise


def test_cross_layout_identical_vectors_produce_equal_digests():
    """Direct unit test of ``_digest()``: a Python ``list`` (SHARDED_JSON
    record shape) and a numerically-equal ``numpy.ndarray`` (CHUNKS_DB
    record shape, ``ChunkStore._decode_vector``'s actual return type) must
    produce the IDENTICAL digest for the same logical values -- otherwise
    cross-layout comparison can never converge even when both sides hold
    the same data.
    """
    values = [0.125, -3.5, 42.0, 1e-3, -1e-3]
    list_record = {"id": "p1", "vector": values, "payload": {"source": "x"}}
    ndarray_record = {
        "id": "p1",
        "vector": np.asarray(values, dtype=np.float32),
        "payload": {"source": "x"},
    }
    assert _digest(list_record) == _digest(ndarray_record)


def test_cross_layout_digest_still_detects_divergent_values():
    """Guard-rail: cross-layout normalization must not become a loophole
    -- genuinely different values across the two representations must
    still produce different digests.
    """
    base_values = [1.0, 2.0, 3.0]
    divergent_values = _corrupt_middle_elements(base_values, 2)
    list_record = {"id": "p1", "vector": base_values}
    ndarray_record = {
        "id": "p1",
        "vector": np.asarray(divergent_values, dtype=np.float32),
    }
    assert _digest(list_record) != _digest(ndarray_record)


def test_cross_layout_convergence_sharded_json_source_chunks_db_target(
    tmp_path: Path,
):
    """Integration-level proof: a SHARDED_JSON legacy source and a
    CHUNKS_DB fixed-root target holding the SAME logical vector for the
    SAME point must be accepted as converged by
    ``verify_source_subset_of_target`` -- the real scenario Bug #1528's
    independent chunks.db consolidation of the fixed-root shard produces
    while the (about-to-be-deleted) legacy source stays on the old layout
    forever.
    """
    vector = [1.0, 2.0, 3.0, 4.0]
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", vector, "same")
    _make_chunks_db_shard(target, "p1", vector, "same")

    # Must not raise: identical logical data, different physical layouts.
    verify_source_subset_of_target(source, target)


def test_cross_layout_corruption_is_still_caught(tmp_path: Path):
    """Guard-rail: cross-layout convergence must not become a loophole --
    a genuinely altered value at the CHUNKS_DB target must still be
    rejected even when compared against a SHARDED_JSON source.
    """
    vector = [1.0, 2.0, 3.0, 4.0]
    source = tmp_path / "source"
    target = tmp_path / "target"
    _make_sharded_json_shard(source, "p1", vector, "same")
    corrupted = _corrupt_middle_elements(vector, 2)
    _make_chunks_db_shard(target, "p1", corrupted, "same")

    with pytest.raises(VerificationError):
        verify_source_subset_of_target(source, target)
