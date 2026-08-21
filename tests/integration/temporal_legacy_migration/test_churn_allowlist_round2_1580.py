"""Issue #1580 adversarial-review round 2 (opus) -- the churn allowlist
does not match REAL production temporal shards.

Opus inspected an ACTUAL fixed-root temporal shard on this machine and
found 8 sidecar files beyond the round-2 (basename-anchoring) allowlist:
``projection_matrix.npy``, ``temporal_structure.json``,
``temporal_progress.json``, ``temporal_progress.json.lock``,
``id_index.bin``, ``path_index.bin``, ``.metadata.lock``,
``.index_rebuild.lock``. Two are unconditionally rewritten by an ordinary
refresh (``temporal_progress.json`` via
``TemporalProgressiveMetadata``/the CLI temporal watch handler,
``path_index.bin`` via ``FilesystemVectorStore._save_path_index``) --
without these, a real refresh trips "unexpectedly altered" and gets
misclassified as a collision, meaning the underlying #1580 bug may still
be unfixed in production. The others fall into two further categories:

- ``id_index.bin``: wholesale-rewritten, but ONLY for a SHARDED_JSON
  target (Story #1456 retires it for CHUNKS_DB) -- mirrors the existing
  ``chunks.db``/``vector_*.json`` layout gating.
- ``projection_matrix.npy``/``temporal_structure.json``: written ONLY
  when missing (self-heal-on-missing, ``temporal_indexer.py``) -- may
  legitimately appear as a NEW addition, but if already present at BOTH
  sides must remain byte-identical (an alteration here is a genuine
  anomaly, never something an ordinary refresh produces).
- ``.metadata.lock``/``.index_rebuild.lock``/``temporal_progress.json.lock``:
  transient fcntl lock files -- never content, excluded from comparison
  entirely rather than "permitted to churn".
"""

import json
from pathlib import Path
from typing import Tuple

import numpy as np
import pytest

from code_indexer.server.services.temporal_legacy_migration.mover import (
    MigrationResult,
    migrate_temporal_shards,
)
from code_indexer.server.services.temporal_legacy_migration.verification import (
    _is_expected_churn_file,
)
from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import ChunkLayout


def _write_vector_shard(shard_dir: Path, point_id: str, vector: list) -> None:
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": point_id, "vector": vector, "payload": {"source": "legacy"}}
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))
    (shard_dir / "collection_meta.json").write_text('{"name":"q1"}')
    manager = HNSWIndexManager(vector_dim=len(vector), space="cosine")
    manager.build_index(shard_dir, np.array([vector], dtype=np.float32), [point_id])


def _rebuild_hnsw(shard_dir: Path, points: list) -> None:
    ids = [point_id for point_id, _ in points]
    vectors = np.array([vector for _, vector in points], dtype=np.float32)
    manager = HNSWIndexManager(vector_dim=vectors.shape[1], space="cosine")
    manager.build_index(shard_dir, vectors, ids)


def _publish_single_point_shard(legacy: Path, fixed: Path) -> Tuple[Path, Path]:
    """Shared setup for the integration-level tests below: write a real
    single-point SHARDED_JSON legacy shard and publish it via a first
    ``migrate_temporal_shards`` pass. Returns ``(shard, fixed_shard)``.
    """
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", [1.0])
    first: MigrationResult = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True
    )
    assert first.published == 1
    assert first.failed == 0
    return shard, fixed / shard.name


def _write_realistic_refresh_sidecars(shard_dir: Path) -> None:
    """Write every sidecar file a REAL ordinary temporal refresh produces
    for a SHARDED_JSON-layout shard, per opus's inspection of an actual
    fixed-root shard on this machine.
    """
    (shard_dir / "path_index.bin").write_bytes(b"real-path-index-bytes")
    (shard_dir / "id_index.bin").write_bytes(b"real-id-index-bytes")
    (shard_dir / "temporal_progress.json").write_text(
        json.dumps({"completed_commits": ["abc123"], "format_version": 2})
    )
    (shard_dir / "temporal_progress.json.lock").write_bytes(b"")
    (shard_dir / "projection_matrix.npy").write_bytes(b"fake-npy-bytes")
    (shard_dir / "temporal_structure.json").write_text(
        json.dumps({"version": 2, "layout": "per_commit", "model": "voyage_context_4"})
    )
    (shard_dir / ".metadata.lock").write_bytes(b"")
    (shard_dir / ".index_rebuild.lock").write_bytes(b"")


def test_realistic_temporal_refresh_sidecars_converge_instead_of_collision_1580_round2(
    tmp_path: Path,
):
    """The production-realistic reproduction: after publish, an ordinary
    in-place refresh (Bug #1529) writes ALL 8 real sidecar files opus
    found, plus a new commit's point record. This must converge
    (already_complete + deleted), never a permanent collision.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard, fixed_shard = _publish_single_point_shard(legacy, fixed)

    # Legitimate in-place refresh: a new commit lands (p2), full realistic
    # sidecar set is (re)written exactly as the real write path does.
    (fixed_shard / "vector_p2.json").write_text(
        json.dumps({"id": "p2", "vector": [2.0], "payload": {"source": "refresh"}})
    )
    _rebuild_hnsw(fixed_shard, [("p1", [1.0]), ("p2", [2.0])])
    _write_realistic_refresh_sidecars(fixed_shard)

    second = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert second.collisions == 0, (
        "a realistic in-place refresh with its full real sidecar set must "
        "never be misclassified as an unresolvable collision"
    )
    assert second.already_complete == 1
    assert second.deleted == 1
    assert second.failed == 0
    assert not shard.exists()

    # A third pass over the (now nonexistent) legacy source is a true
    # no-op -- genuine convergence, not a one-off coincidence.
    third = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert third.collisions == 0
    assert third.failed == 0


@pytest.mark.parametrize(
    "lock_filename",
    [".metadata.lock", ".index_rebuild.lock", "temporal_progress.json.lock"],
)
def test_lock_files_never_cause_a_collision_regardless_of_content_or_presence_1580_round2(
    tmp_path: Path, lock_filename: str
):
    """Lock files are transient, never content -- differing (or
    target-only) lock file bytes must never trigger a collision, since
    they are excluded from comparison entirely rather than merely
    "permitted to churn". Covers all three known lock filenames, and both
    an ALTERED-content case and a target-only ADDITION case.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard, fixed_shard = _publish_single_point_shard(legacy, fixed)
    (shard / lock_filename).write_bytes(b"legacy-lock-bytes")

    # Republish is unnecessary: the lock file is planted post-publish at
    # both sides with DIFFERENT content, plus a second lock name that
    # never existed at the source at all (pure target-only addition).
    (fixed_shard / lock_filename).write_bytes(b"different-target-lock-bytes")
    (fixed_shard / ".a-brand-new.lock").write_bytes(b"never-at-source")

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert result.collisions == 0
    assert result.already_complete == 1
    assert result.deleted == 1
    assert result.failed == 0


@pytest.mark.parametrize(
    "artifact_filename,original_bytes,corrupted_bytes",
    [
        ("projection_matrix.npy", b"original-matrix-bytes", b"corrupted-matrix-bytes"),
        (
            "temporal_structure.json",
            json.dumps({"version": 2, "model": "voyage_context_4"}).encode(),
            json.dumps({"version": 2, "model": "cohere_embed_v4_0"}).encode(),
        ),
    ],
)
def test_addition_only_artifact_altered_in_place_is_still_rejected_1580_round2(
    tmp_path: Path,
    artifact_filename: str,
    original_bytes: bytes,
    corrupted_bytes: bytes,
):
    """Guard-rail: ``projection_matrix.npy``/``temporal_structure.json``
    are self-heal-on-missing, not wholesale churn -- if present at BOTH
    sides already, an in-place ALTERATION must still be treated as a
    genuine anomaly and rejected, never silently tolerated as "expected
    churn".
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", [1.0])
    (shard / artifact_filename).write_bytes(original_bytes)

    first: MigrationResult = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True
    )
    assert first.published == 1
    assert first.failed == 0
    fixed_shard = fixed / shard.name
    assert (fixed_shard / artifact_filename).read_bytes() == original_bytes

    # Genuine anomaly: the artifact is altered in place at the target
    # while nothing else about the shard legitimately changed.
    (fixed_shard / artifact_filename).write_bytes(corrupted_bytes)

    result = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert result.collisions == 1, (
        "an altered addition-only artifact must never be silently "
        "tolerated as expected churn"
    )
    assert result.already_complete == 0
    assert result.deleted == 0
    assert shard.exists(), "legacy data must survive an unproven target"


@pytest.mark.parametrize(
    "filename,layout_gated",
    [
        ("id_index.bin", True),
        ("path_index.bin", False),
        ("temporal_progress.json", False),
    ],
)
def test_wholesale_churn_filenames_layout_gating_1580_round2(
    tmp_path: Path, filename: str, layout_gated: bool
):
    """``id_index.bin`` is retired for CHUNKS_DB collections (Story
    #1456) -- its wholesale-churn allowance is gated to a SHARDED_JSON
    target, mirroring ``chunks.db``'s existing CHUNKS_DB-only gating.
    ``path_index.bin``/``temporal_progress.json`` are rewritten by every
    ordinary refresh regardless of layout -- not layout-gated at all.
    """
    target = tmp_path / "target"
    target.mkdir()
    (target / filename).write_bytes(b"data")

    sharded_json_result = _is_expected_churn_file(
        target, filename, ChunkLayout.SHARDED_JSON
    )
    chunks_db_result = _is_expected_churn_file(target, filename, ChunkLayout.CHUNKS_DB)

    assert sharded_json_result is True
    assert chunks_db_result is (not layout_gated)


def test_hnsw_sync_state_sidecar_altered_in_place_converges_not_collision_1619(
    tmp_path: Path,
):
    """Bug #1619: ``hnsw_sync_state.json`` (the dedicated per-mutation
    dirty-marker sidecar `_mark_hnsw_dirty_before_mutation()` writes) is
    the single highest-churn file in a collection root -- rewritten on
    EVERY ``upsert_points()``/``delete_points()`` call, churnier than
    ``path_index.bin``/``temporal_progress.json`` (which were already
    added to ``_ROOT_ONLY_CHURN_FILENAMES`` after a real incident for
    exactly this reason). A shard that has this sidecar present at
    publish time, then a real refresh rewrites it in place (a dirty ->
    clean epoch transition), must still converge -- never be
    misclassified as a permanent collision.
    """
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-e-2026Q1"
    _write_vector_shard(shard, "p1", [1.0])
    (shard / "hnsw_sync_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mutation_epoch": 1,
                "published_epoch": 1,
                "status": "clean",
                "current_branch": None,
                "layout": "sharded_json",
            }
        )
    )

    first = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert first.published == 1
    assert first.failed == 0
    fixed_shard = fixed / shard.name

    # Ordinary in-place refresh: a new commit lands (p2), HNSW rebuilt,
    # and hnsw_sync_state.json rewritten in place with a new dirty ->
    # clean epoch transition -- exactly what a real upsert_points() +
    # end_indexing() cycle does.
    (fixed_shard / "vector_p2.json").write_text(
        json.dumps({"id": "p2", "vector": [2.0], "payload": {"source": "refresh"}})
    )
    _rebuild_hnsw(fixed_shard, [("p1", [1.0]), ("p2", [2.0])])
    (fixed_shard / "hnsw_sync_state.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mutation_epoch": 2,
                "published_epoch": 2,
                "status": "clean",
                "current_branch": None,
                "layout": "sharded_json",
            }
        )
    )

    second = migrate_temporal_shards(
        legacy, fixed, relocation_enabled=True, cleanup_authorized=True
    )
    assert second.collisions == 0, (
        "hnsw_sync_state.json being rewritten in place by an ordinary "
        "refresh must never be misclassified as a permanent collision"
    )
    assert second.already_complete == 1
    assert second.deleted == 1
    assert second.failed == 0
    assert not shard.exists()
