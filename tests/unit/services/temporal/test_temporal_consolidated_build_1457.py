"""Shared "build-fresh-consolidated-version" primitive (Story #1457 AC6).

Builds a NEW, collision-safe version directory containing a consolidated
`chunks.db` (Story #1455 `ChunkStore`) + a built HNSW index (reusing
`HNSWIndexManager.rebuild_from_vectors` verbatim, via its `layout_override`
parameter -- documented as "reserved for the fresh-CHUNKS_DB-build
orchestrator") + the durably-committed CHUNKS_DB discriminator (Story #1456
`write_chunks_db_discriminator`, committed ONLY after the HNSW index is
fully built, per its documented ordering contract).

This is the shared primitive AC6's three build branches (A / B-bootstrap /
B-fresh) and AC11's bootstrap all reuse, with a MULTI-SOURCE row input.

Real filesystem, real SQLite (`ChunkStore`), real `hnswlib` index build --
no mocking of the code under test.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)
from code_indexer.services.temporal.temporal_consolidated_build import (
    _verify_consolidated_version,
    build_fresh_consolidated_temporal_version,
    copy_and_extend_consolidated_temporal_version,
)
from code_indexer.services.temporal.temporal_reconciliation import reconcile_shard
from code_indexer.services.temporal.temporal_structure_marker import (
    read_structure_marker,
)
from code_indexer.storage.temporal_metadata_store import TemporalMetadataStore
from code_indexer.services.temporal.models import CommitInfo
from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
)
import numpy as np
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
    write_chunks_db_discriminator as _real_write_chunks_db_discriminator,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _fake_records(count: int, dim: int = 4):
    for i in range(count):
        yield {
            "id": f"point-{i}",
            "vector": [float(i + j) for j in range(dim)],
            "payload": {"commit_hash": f"c{i}"},
        }


def test_build_creates_version_dir_with_chunks_db_hnsw_and_discriminator(tmp_path):
    sister_root = tmp_path / "sister"
    records = list(_fake_records(5))

    version_dir = build_fresh_consolidated_temporal_version(
        sister_root,
        "evolution-temporal-voyage_code_3-2024Q1",
        [records],
        vector_dim=4,
    )

    assert version_dir.is_dir()
    assert version_dir.parent.name == "evolution-temporal-voyage_code_3-2024Q1"
    assert version_dir.name.startswith("v_")

    # chunks.db is populated and readable back via ChunkStore.
    store = ChunkStore(version_dir / "chunks.db", immutable=True)
    try:
        assert store.count() == 5
        row = store.read("point-2")
        assert row is not None
        assert row["payload"]["commit_hash"] == "c2"
    finally:
        store.close()

    # HNSW index was genuinely built (real hnswlib file on disk).
    assert (version_dir / "hnsw_index.bin").exists()

    # Discriminator committed AFTER the HNSW build -- resolve_chunk_layout
    # sees this version as a real CHUNKS_DB collection.
    assert resolve_chunk_layout(version_dir) == ChunkLayout.CHUNKS_DB


def test_build_merges_multiple_row_sources_into_one_chunks_db(tmp_path):
    """AC6/AC11's MULTI-SOURCE row input: Branch B-bootstrap feeds
    [legacy_scan, new_delta], AC11 bootstrap feeds [legacy_scan] -- both
    sources' rows must land in the SAME consolidated chunks.db."""
    sister_root = tmp_path / "sister"
    legacy_rows = list(_fake_records(3, dim=4))
    delta_rows = [
        {
            "id": f"delta-{i}",
            "vector": [float(i)] * 4,
            "payload": {"commit_hash": f"d{i}"},
        }
        for i in range(2)
    ]

    version_dir = build_fresh_consolidated_temporal_version(
        sister_root,
        "evolution-temporal-voyage_code_3-2024Q2",
        [legacy_rows, delta_rows],
        vector_dim=4,
    )

    store = ChunkStore(version_dir / "chunks.db", immutable=True)
    try:
        assert store.count() == 5
        assert store.read("point-0") is not None
        assert store.read("delta-1") is not None
    finally:
        store.close()


def test_build_retries_on_version_id_collision(tmp_path):
    """Story #1457 AC9 collision-safety, applied to this shared build
    primitive: a pre-existing v_{ts} destination for this namespace must
    not be reused/corrupted -- generation retries to a fresh id."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q3"
    colliding_dir = sister_root / ".versioned" / ns / "v_1700000000"
    colliding_dir.mkdir(parents=True)
    (colliding_dir / "sentinel.txt").write_text("pre-existing")

    with patch(
        "code_indexer.services.temporal.temporal_consolidated_build.time"
    ) as mock_time:
        mock_time.time.return_value = 1700000000
        version_dir = build_fresh_consolidated_temporal_version(
            sister_root, ns, [list(_fake_records(1, dim=4))], vector_dim=4
        )

    assert version_dir != colliding_dir
    assert version_dir.name != "v_1700000000"
    # The pre-existing colliding directory's content is untouched.
    assert (colliding_dir / "sentinel.txt").read_text() == "pre-existing"


def test_extend_copies_current_version_and_applies_delta_rows(tmp_path):
    """AC6 Branch A: pointer EXISTS -- reflink/copy the current v_* into a
    fresh v_{unix_ts}, then apply ONLY the new-commit delta rows into the
    COPY's chunk store. The original historical rows must survive (via the
    copy), and the new delta rows must be added on top."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q4"

    current_version = build_fresh_consolidated_temporal_version(
        sister_root, ns, [list(_fake_records(3, dim=4))], vector_dim=4
    )

    snapshot_manager = VersionedSnapshotManager(versioned_base=str(sister_root))
    delta_rows = [{"id": "delta-only", "vector": [9.0, 9.0, 9.0, 9.0], "payload": {}}]

    extended_version = copy_and_extend_consolidated_temporal_version(
        snapshot_manager, ns, current_version, delta_rows, vector_dim=4
    )

    assert extended_version != current_version
    assert extended_version.parent.name == ns

    # Original version directory untouched (the copy is independent).
    assert current_version.is_dir()

    store = ChunkStore(extended_version / "chunks.db", immutable=True)
    try:
        assert store.count() == 4  # 3 historical + 1 delta
        assert store.read("point-0") is not None  # historical row survived
        assert store.read("delta-only") is not None  # delta row added
    finally:
        store.close()

    assert (extended_version / "hnsw_index.bin").exists()
    assert resolve_chunk_layout(extended_version) == ChunkLayout.CHUNKS_DB


def test_build_output_is_recognized_by_filesystem_vector_store_collection_exists(
    tmp_path,
):
    """AC7 compatibility: reconciliation needs vector_store.collection_exists()
    to work against a built version directory. collection_exists() checks
    for "vector_size" specifically (not "vector_dim"), so the build
    primitive's metadata must include both."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q1"

    version_dir = build_fresh_consolidated_temporal_version(
        sister_root, ns, [list(_fake_records(2, dim=4))], vector_dim=4
    )

    # A FilesystemVectorStore rooted at the version directory's PARENT,
    # treating the version directory's basename as the "collection name",
    # mirrors how a future dedicated temporal store would probe it.
    store = FilesystemVectorStore(base_path=version_dir.parent)
    assert store.collection_exists(version_dir.name) is True


def test_build_marks_consolidated_commits_as_completed_for_reconciliation(tmp_path):
    """AC6/AC7 integration: a freshly built consolidated version's commits
    must be marked complete in temporal_progress.json, or AC7's
    reconciliation (reconcile_shard) would treat every one of them as
    PARTIAL (points present, no completion marker) and attempt to
    delete-and-reindex them on the very next reconciliation pass -- even
    though the build just finished successfully."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q1"
    records = [
        {
            "id": f"proj:commit:c{i}:0",
            "vector": [float(i)] * 4,
            "payload": {"commit_hash": f"c{i}"},
        }
        for i in range(3)
    ]

    version_dir = build_fresh_consolidated_temporal_version(
        sister_root, ns, [records], vector_dim=4
    )

    vector_store = FilesystemVectorStore(base_path=version_dir.parent)
    commits = [
        CommitInfo(
            hash=f"c{i}",
            timestamp=0,
            author_name="A",
            author_email="a@test.com",
            message="msg",
            parent_hashes="",
        )
        for i in range(3)
    ]

    missing = reconcile_shard(vector_store, version_dir.name, commits, "voyage-code-3")

    assert missing == []


def test_temporal_progress_written_before_discriminator_commit(tmp_path):
    """Story #1457 CRITICAL #4 (2026-07-23 code review): the discriminator
    (which signals this version is COMPLETE and queryable) must be the
    truly final write -- verify-then-commit, not commit-then-verify.
    temporal_progress.json was previously written AFTER the discriminator;
    it must be written BEFORE."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q1"

    observed = {}

    def _spy_write_discriminator(version_dir):
        observed["progress_json_exists_at_discriminator_time"] = (
            version_dir / "temporal_progress.json"
        ).exists()
        return _real_write_chunks_db_discriminator(version_dir)

    with patch(
        "code_indexer.services.temporal.temporal_consolidated_build"
        ".write_chunks_db_discriminator",
        side_effect=_spy_write_discriminator,
    ):
        build_fresh_consolidated_temporal_version(
            sister_root,
            ns,
            [
                [
                    {
                        "id": "proj:commit:c0:0",
                        "vector": [0.1, 0.2, 0.3, 0.4],
                        "payload": {"commit_hash": "c0"},
                    }
                ]
            ],
            vector_dim=4,
        )

    assert observed.get("progress_json_exists_at_discriminator_time") is True, (
        "temporal_progress.json must already exist BEFORE the discriminator "
        "commit -- the discriminator must be the truly final write"
    )


def test_verify_consolidated_version_detects_row_count_mismatch(tmp_path):
    """Story #1457 CRITICAL #4 (2026-07-23 code review): the read-back
    verification must genuinely CATCH a real defect, not merely avoid
    false-positiving on correct data."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q1"

    version_dir = build_fresh_consolidated_temporal_version(
        sister_root, ns, [list(_fake_records(2, dim=4))], vector_dim=4
    )

    import pytest

    wrong_expected_records = list(_fake_records(3, dim=4))  # one too many
    with pytest.raises(RuntimeError, match="expected 3 unique rows"):
        _verify_consolidated_version(version_dir, wrong_expected_records, vector_dim=4)


def test_build_creates_path_index_projection_matrix_and_structure_marker(tmp_path):
    """Story #1457 CRITICAL #4 (2026-07-23 code review): the published
    version must be a COMPLETE constituent-file set, not just
    chunks.db + hnsw_index.bin + discriminator. path_index.bin,
    projection_matrix.npy, and temporal_structure.json must also be
    built."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q1"
    records = [
        {
            "id": "proj:commit:c0:0",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"commit_hash": "c0", "primary_path": "src/foo.py"},
        }
    ]

    version_dir = build_fresh_consolidated_temporal_version(
        sister_root, ns, [records], vector_dim=4, embedder_slug="voyage_code_3"
    )

    path_index = PathIndex.load(version_dir / "path_index.bin")
    assert path_index.get_point_ids("src/foo.py") == {"proj:commit:c0:0"}

    matrix = np.load(version_dir / "projection_matrix.npy")
    assert matrix.ndim == 2
    assert matrix.size > 0
    assert matrix.shape[0] == 4  # input_dim matches vector_dim

    marker = read_structure_marker(version_dir)
    assert marker is not None
    assert marker["model"] == "voyage_code_3"


def test_extend_updates_path_index_and_verifies_before_marking_complete(tmp_path):
    """Story #1457 CRITICAL #4 (2026-07-23 code review), Branch A: the
    copied path_index.bin (inherited from the source version) must be
    UPDATED with the new delta rows' paths, merged with the copied
    historical entries -- not left stale."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q4"

    historical_records = [
        {
            "id": "point-0",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"commit_hash": "c0", "primary_path": "src/old.py"},
        }
    ]
    current_version = build_fresh_consolidated_temporal_version(
        sister_root,
        ns,
        [historical_records],
        vector_dim=4,
        embedder_slug="voyage_code_3",
    )

    snapshot_manager = VersionedSnapshotManager(versioned_base=str(sister_root))
    delta_rows = [
        {
            "id": "delta-only",
            "vector": [9.0, 9.0, 9.0, 9.0],
            "payload": {"commit_hash": "c1", "primary_path": "src/new.py"},
        }
    ]

    extended_version = copy_and_extend_consolidated_temporal_version(
        snapshot_manager, ns, current_version, delta_rows, vector_dim=4
    )

    path_index = PathIndex.load(extended_version / "path_index.bin")
    assert path_index.get_point_ids("src/old.py") == {"point-0"}, (
        "historical path_index entry (copied from the source version) must survive"
    )
    assert path_index.get_point_ids("src/new.py") == {"delta-only"}, (
        "the new delta row's path must be added to the copied path_index"
    )


def test_extend_force_rebuild_rebuilds_hnsw_even_with_no_delta_rows(tmp_path):
    """Story #1457 HIGH #11 (2026-07-23 code review): a locally-repaired-
    but-commit-delta-empty shard must still get its sister HNSW index
    rebuilt on republish -- otherwise Branch A republishes the stale
    sister copy forever (the copy carries over the source's OLD
    hnsw_index.bin unchanged when delta_batch is empty)."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q1"

    current_version = build_fresh_consolidated_temporal_version(
        sister_root, ns, [list(_fake_records(2, dim=4))], vector_dim=4
    )
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(sister_root))

    with patch(
        "code_indexer.services.temporal.temporal_consolidated_build.HNSWIndexManager"
    ) as mock_hnsw_cls:
        mock_hnsw_cls.return_value.rebuild_from_vectors = MagicMock()
        copy_and_extend_consolidated_temporal_version(
            snapshot_manager,
            ns,
            current_version,
            [],  # NO new delta rows this refresh cycle
            vector_dim=4,
            force_rebuild=False,
        )
        assert mock_hnsw_cls.return_value.rebuild_from_vectors.call_count == 0, (
            "without force_rebuild, an empty delta must NOT trigger a "
            "rebuild -- preserves existing behavior"
        )

    with patch(
        "code_indexer.services.temporal.temporal_consolidated_build.HNSWIndexManager"
    ) as mock_hnsw_cls:
        mock_hnsw_cls.return_value.rebuild_from_vectors = MagicMock()
        copy_and_extend_consolidated_temporal_version(
            snapshot_manager,
            ns,
            current_version,
            [],  # STILL no new delta rows
            vector_dim=4,
            force_rebuild=True,  # but a local repair happened
        )
        assert mock_hnsw_cls.return_value.rebuild_from_vectors.call_count == 1, (
            "force_rebuild=True must rebuild the sister HNSW index even "
            "with zero delta rows -- propagating a local repair into the "
            "republished sister version"
        )


def test_extend_verifies_unconditionally_even_with_no_delta_and_no_force_rebuild(
    tmp_path,
):
    """Story #1457 CRITICAL #4 remaining gap (2026-07-24 re-review, Codex):
    a refresh with NO delta rows AND no force_rebuild used to return early
    without ever verifying the copy, so temporal_refresh_dispatch.py could
    publish a genuinely BROKEN copy untouched. This proves the check is
    real (not a mock-spy): create_snapshot (a genuine external
    VersionedSnapshotManager collaborator, not the SUT) is patched to
    return a deliberately INCOMPLETE copy directory (0 rows instead of the
    source's 2), and copy_and_extend_consolidated_temporal_version must
    raise rather than silently returning that broken path."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q1"

    current_version = build_fresh_consolidated_temporal_version(
        sister_root, ns, [list(_fake_records(2, dim=4))], vector_dim=4
    )
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(sister_root))

    broken_copy_path = sister_root / ns / "v_broken"
    broken_copy_path.mkdir(parents=True)
    broken_store = ChunkStore(broken_copy_path / "chunks.db", expected_dim=4)
    broken_store.close()  # 0 rows -- source has 2, so this copy is incomplete

    with patch.object(
        snapshot_manager, "create_snapshot", return_value=str(broken_copy_path)
    ):
        try:
            copy_and_extend_consolidated_temporal_version(
                snapshot_manager,
                ns,
                current_version,
                [],  # NO new delta rows
                vector_dim=4,
                force_rebuild=False,  # NO local repair either
            )
            raised = False
        except RuntimeError:
            raised = True

    assert raised, (
        "copy_and_extend_consolidated_temporal_version must verify the "
        "copy before returning it EVEN when there is no delta and no "
        "force_rebuild -- an incomplete copy (0 rows vs source's 2) was "
        "silently accepted instead of raising"
    )


def test_build_creates_temporal_metadata_db_with_real_point_id_mappings(tmp_path):
    """Story #1457 CRITICAL #4 remaining gap (2026-07-24 re-review, Codex):
    temporal_metadata.db is a genuine AC6 solo-format requirement (used by
    TemporalMetadataStore.detect_format() for dashboard/status reporting,
    dashboard_service.py:906) -- its ABSENCE causes a perfectly healthy
    CHUNKS_DB sister-location collection to be misreported as legacy v1
    needing reindex. build_fresh_consolidated_temporal_version must write
    real point_id-to-hash-prefix mappings, not just create the file."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q1"
    records = [
        {
            "id": "proj:commit:c0:0",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {
                "commit_hash": "c0",
                "path": "src/foo.py",
                "chunk_index": 0,
            },
        }
    ]

    version_dir = build_fresh_consolidated_temporal_version(
        sister_root, ns, [records], vector_dim=4, embedder_slug="voyage_code_3"
    )

    assert (version_dir / "temporal_metadata.db").exists(), (
        "build_fresh_consolidated_temporal_version must create "
        "temporal_metadata.db -- its absence makes "
        "TemporalMetadataStore.detect_format() misreport this healthy "
        "CHUNKS_DB collection as legacy v1"
    )
    assert TemporalMetadataStore.detect_format(version_dir) == "v2"

    store = TemporalMetadataStore(version_dir)
    hash_prefix = store.generate_hash_prefix("proj:commit:c0:0")
    assert store.get_point_id(hash_prefix) == "proj:commit:c0:0", (
        "temporal_metadata.db must contain a REAL point_id-to-hash-prefix "
        "mapping for the record, not just an empty database file"
    )


def test_extend_appends_delta_rows_to_inherited_temporal_metadata_db(tmp_path):
    """Story #1457 CRITICAL #4 remaining gap, Branch A: the copy inherits
    the source's temporal_metadata.db via create_snapshot (whole-directory
    copy) -- only the NEW delta rows need appending, mirroring the
    path_index.bin merge-not-replace pattern."""
    sister_root = tmp_path / "sister"
    ns = "evolution-temporal-voyage_code_3-2024Q4"

    historical_records = [
        {
            "id": "point-0",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {"commit_hash": "c0", "path": "src/old.py", "chunk_index": 0},
        }
    ]
    current_version = build_fresh_consolidated_temporal_version(
        sister_root,
        ns,
        [historical_records],
        vector_dim=4,
        embedder_slug="voyage_code_3",
    )

    snapshot_manager = VersionedSnapshotManager(versioned_base=str(sister_root))
    delta_rows = [
        {
            "id": "delta-only",
            "vector": [9.0, 9.0, 9.0, 9.0],
            "payload": {"commit_hash": "c1", "path": "src/new.py", "chunk_index": 0},
        }
    ]

    extended_version = copy_and_extend_consolidated_temporal_version(
        snapshot_manager, ns, current_version, delta_rows, vector_dim=4
    )

    store = TemporalMetadataStore(extended_version)
    historical_prefix = store.generate_hash_prefix("point-0")
    delta_prefix = store.generate_hash_prefix("delta-only")
    assert store.get_point_id(historical_prefix) == "point-0", (
        "historical temporal_metadata.db entry (inherited via create_snapshot) "
        "must survive"
    )
    assert store.get_point_id(delta_prefix) == "delta-only", (
        "the new delta row's point_id mapping must be appended to the "
        "inherited temporal_metadata.db"
    )
