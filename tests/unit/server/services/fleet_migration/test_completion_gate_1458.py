"""Unit tests for repo_temporal_dirs_fully_consolidated() (Story #1458 AC1 /
AC10 completion gate, Bug #1528 revision).

Bug #1528 replaced the previous unconditional PHYSICAL ABSENCE predicate
(``repo_has_zero_residual_temporal_dirs``). That contract only made sense
under the retired Story #1457 sister-location model, where a migrated
temporal namespace was published elsewhere and its in-repo directory
reclaimed. Temporal now migrates IN PLACE through the same
``consolidate_collection_in_place`` engine semantic collections use, so the
directory legitimately REMAINS and its LAYOUT is the completion signal:
every real temporal shard must verify as fully consolidated to chunks.db.

Real filesystem, real legacy fixture written by the production writer, real
in-place SQLite consolidation -- no mocking of the predicate's own logic.
"""

from pathlib import Path
from typing import Any, Dict, List

import numpy as np

from code_indexer.server.services.fleet_migration.completion_gate import (
    invalidate_post_consolidation_snapshot_marker,
    mark_post_consolidation_snapshot_published,
    repo_has_published_post_consolidation_snapshot,
    repo_temporal_dirs_fully_consolidated,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)

_SHARD_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
_VECTOR_SIZE = 8
_RNG_SEED = 1458
_CHUNK_INDEX = 0
_COMMIT_STUB_WIDTH = 8
_ROW_ID = f"proj:commit:{'a' * _COMMIT_STUB_WIDTH}:{_CHUNK_INDEX}"


def _points() -> List[Dict[str, Any]]:
    # Any is unavoidable here: a point record is the store's own
    # heterogeneous public contract (str id, List[float] vector, nested
    # payload dict, str chunk_text) and has no narrower published type.
    rng = np.random.default_rng(_RNG_SEED)
    return [
        {
            "id": _ROW_ID,
            "vector": rng.standard_normal(_VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": "src/a.py"},
            "chunk_text": "x",
        }
    ]


def _build_legacy_shard(index_path: Path) -> Path:
    """Write a REAL legacy (SHARDED_JSON) temporal shard with the production
    writer by explicitly requesting the legacy layout."""
    store = FilesystemVectorStore(
        base_path=index_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(_SHARD_NAME, vector_size=_VECTOR_SIZE)
    store.begin_indexing(_SHARD_NAME)
    store.upsert_points(_SHARD_NAME, _points())
    store.end_indexing(_SHARD_NAME)

    shard_dir = index_path / _SHARD_NAME
    assert list(shard_dir.rglob("vector_*.json")), "fixture is not legacy-layout"
    return shard_dir


class TestRepoTemporalDirsFullyConsolidated:
    def test_true_when_index_dir_has_no_temporal_directories(
        self, tmp_path: Path
    ) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        (index_path / "some_semantic_collection").mkdir()

        assert repo_temporal_dirs_fully_consolidated(index_path) is True

    def test_true_when_index_dir_does_not_exist(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        # Never created -- e.g. a repo with no indexes at all yet.
        assert repo_temporal_dirs_fully_consolidated(index_path) is True

    def test_false_before_and_true_after_in_place_consolidation(
        self, tmp_path: Path
    ) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        shard_dir = _build_legacy_shard(index_path)

        assert repo_temporal_dirs_fully_consolidated(index_path) is False

        consolidate_collection_in_place(shard_dir, deletion_authorized=True)

        assert shard_dir.is_dir(), "in-place migration keeps the directory"
        assert repo_temporal_dirs_fully_consolidated(index_path) is True


class TestRepoTemporalDirsFullyConsolidatedSkips:
    def test_true_for_rowless_artifact_without_collection_metadata(
        self, tmp_path: Path
    ) -> None:
        """A rowless "empty artifact" directory (Story #1458 AC1a) has no
        collection_meta.json, so nothing can consolidate it -- it must not
        block completion forever now that migration never deletes it."""
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        (index_path / "code-indexer-temporal-voyage_code_3-2024Q2").mkdir()

        assert repo_temporal_dirs_fully_consolidated(index_path) is True

    def test_ignores_non_directories_and_unparseable_names(
        self, tmp_path: Path
    ) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        # A regular semantic collection dir must never false-positive.
        (index_path / "not_a_temporal_collection").mkdir()
        # A FILE (not a dir) named with the temporal prefix must not count.
        (index_path / "code-indexer-temporal-stray-file.txt").write_text("x")
        # The bare bookkeeping directory is never a shard.
        (index_path / "code-indexer-temporal").mkdir()

        assert repo_temporal_dirs_fully_consolidated(index_path) is True


class TestPostConsolidationSnapshotPublishedMarker:
    """New CRITICAL finding: 'consolidation done' and 'snapshot published'
    must be DISTINCT, independently-durable states. Without this, a crash
    after consolidation+temporal-bootstrap complete but before/during the
    AC10 snapshot trigger leaves is_repo_already_migrated() reporting
    'migrated' (consolidation-wise it genuinely is) while the snapshot
    NEVER fires -- and since discovery would then skip the repo forever,
    there is no other retry path."""

    def test_absent_by_default(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        assert repo_has_published_post_consolidation_snapshot(index_path) is False

    def test_absent_when_index_dir_does_not_exist(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        # Never created.
        assert repo_has_published_post_consolidation_snapshot(index_path) is False

    def test_present_after_marking(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        mark_post_consolidation_snapshot_published(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is True

    def test_marking_is_idempotent_and_durable_across_repeated_calls(
        self, tmp_path: Path
    ) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)

        mark_post_consolidation_snapshot_published(index_path)
        mark_post_consolidation_snapshot_published(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is True

    def test_marker_creates_index_dir_if_missing(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        # Never created ahead of time.

        mark_post_consolidation_snapshot_published(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is True

    def test_two_repos_are_independently_tracked(self, tmp_path: Path) -> None:
        index_path_a = tmp_path / "repo-a" / ".code-indexer" / "index"
        index_path_b = tmp_path / "repo-b" / ".code-indexer" / "index"
        index_path_a.mkdir(parents=True)
        index_path_b.mkdir(parents=True)

        mark_post_consolidation_snapshot_published(index_path_a)

        assert repo_has_published_post_consolidation_snapshot(index_path_a) is True
        assert repo_has_published_post_consolidation_snapshot(index_path_b) is False

    def test_invalidate_removes_existing_marker(self, tmp_path: Path) -> None:
        """Codex CRITICAL finding (round 4): a marker from a PRIOR
        migration generation must be durably invalidatable, so it is
        never mistaken for a NEW generation's completion."""
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        mark_post_consolidation_snapshot_published(index_path)
        assert repo_has_published_post_consolidation_snapshot(index_path) is True

        invalidate_post_consolidation_snapshot_marker(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is False

    def test_invalidate_is_noop_when_marker_absent(self, tmp_path: Path) -> None:
        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        assert repo_has_published_post_consolidation_snapshot(index_path) is False

        invalidate_post_consolidation_snapshot_marker(index_path)  # must not raise

        assert repo_has_published_post_consolidation_snapshot(index_path) is False

    def test_invalidate_fsyncs_the_parent_directory_after_unlink(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex CRITICAL finding (round 5): a bare unlink() with no
        parent-directory fsync is inconsistent with the publication path
        (mark_post_consolidation_snapshot_published), which correctly
        fsyncs both the file and the directory. A power loss after the
        unlink but before the directory entry removal is durable can
        leave the OLD marker reappearing on next boot, reopening the
        exact multi-generation staleness bug this fix was supposed to
        close. Spies on the stdlib os.fsync (the actual OS syscall
        nfs_safe_fsync wraps -- a genuinely external dependency) to prove
        a directory-fd fsync is actually attempted."""
        import os as os_module

        index_path = tmp_path / ".code-indexer" / "index"
        index_path.mkdir(parents=True)
        mark_post_consolidation_snapshot_published(index_path)
        assert repo_has_published_post_consolidation_snapshot(index_path) is True

        fsync_calls = []
        original_fsync = os_module.fsync

        def _spy_fsync(fd):
            fsync_calls.append(fd)
            return original_fsync(fd)

        monkeypatch.setattr(os_module, "fsync", _spy_fsync)

        invalidate_post_consolidation_snapshot_marker(index_path)

        assert repo_has_published_post_consolidation_snapshot(index_path) is False
        assert fsync_calls, (
            "Bug: invalidate_post_consolidation_snapshot_marker() did NOT "
            "fsync the parent directory after unlink() -- the deletion is "
            "not crash-durable."
        )
