"""TDD tests for Codex review follow-up on Bug #1575 Part A -- CRITICAL
finding 2: the HNSW rebuild's visibility filter (both the SHARDED_JSON and
CHUNKS_DB loaders in ``rebuild_from_vectors()``) compares a RAW stored
``payload.path`` against the (relative) ``visible_files`` set with no
normalization, unlike ``_normalize_stored_path()`` (Bug #1575 AC6) used
elsewhere for the exact same absolute-vs-relative stored-path problem.

RED phase: every test in this file must FAIL against the pre-fix
``HNSWIndexManager`` (no ``project_root`` parameter on
``rebuild_from_vectors()``, and no normalization applied to the stored
path before the ``visible_files`` membership check).
"""

import json
from pathlib import Path

import numpy as np

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager
from code_indexer.storage.shared.chunk_layout import ChunkLayout
from code_indexer.storage.sqlite_chunk_store import ChunkStore


VECTOR_DIM = 128


def _make_collection_meta(collection_path: Path, vector_dim: int = VECTOR_DIM) -> None:
    meta = {
        "name": "test_collection",
        "vector_size": vector_dim,
        "vector_dim": vector_dim,
        "created_at": "2025-01-01T00:00:00Z",
        "quantization_range": {"min": -0.75, "max": 0.75},
        "index_version": 1,
    }
    meta_file = collection_path / "collection_meta.json"
    meta_file.parent.mkdir(parents=True, exist_ok=True)
    with open(meta_file, "w") as f:
        json.dump(meta, f)


def _write_vector_file(
    collection_path: Path,
    point_id: str,
    file_path: str,
    vector_dim: int = VECTOR_DIM,
) -> Path:
    vector_subdir = collection_path / "vectors"
    vector_subdir.mkdir(parents=True, exist_ok=True)
    vector_file = vector_subdir / f"vector_{point_id}.json"
    data = {
        "id": point_id,
        "vector": np.random.randn(vector_dim).tolist(),
        "payload": {"path": file_path, "type": "content"},
    }
    with open(vector_file, "w") as f:
        json.dump(data, f)
    return vector_file


class TestShardedJsonVisibilityFilterNormalizesAbsolutePaths:
    """Bug #1575 CRITICAL finding 2, SHARDED_JSON loader
    (``_load_vectors_from_json_files``)."""

    def test_absolute_stored_path_is_included_against_relative_visible_set(
        self, tmp_path: Path
    ):
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path)

        absolute_path = str(tmp_path / "src" / "a.py")
        _write_vector_file(collection_path, "vec_abs", absolute_path)

        manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path,
            visible_files={"src/a.py"},
            project_root=tmp_path,
        )

        assert count == 1, (
            "An ABSOLUTE stored path must be normalized against "
            "project_root before comparing to the (relative) "
            "visible_files set -- a raw string comparison always "
            "excludes it, silently disagreeing with the payload-visibility "
            "state that _batch_hide_files_in_branch/"
            "_batch_ensure_files_visible_in_branch already normalize."
        )

    def test_absolute_stored_path_normalizing_outside_visible_set_is_excluded(
        self, tmp_path: Path
    ):
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path)

        absolute_path = str(tmp_path / "src" / "not_visible.py")
        _write_vector_file(collection_path, "vec_abs", absolute_path)

        manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path,
            visible_files={"src/other.py"},
            project_root=tmp_path,
        )

        assert count == 0


class TestChunksDbVisibilityFilterNormalizesAbsolutePaths:
    """Bug #1575 CRITICAL finding 2, CHUNKS_DB loader
    (``_load_vectors_from_chunks_db``)."""

    def test_absolute_stored_path_is_included_against_relative_visible_set(
        self, tmp_path: Path
    ):
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path)

        absolute_path = str(tmp_path / "src" / "a.py")
        with ChunkStore(collection_path / "chunks.db") as store:
            store.write_batch(
                [
                    {
                        "id": "vec_abs",
                        "vector": np.random.randn(VECTOR_DIM).tolist(),
                        "payload": {"path": absolute_path, "type": "content"},
                    }
                ]
            )

        manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path,
            visible_files={"src/a.py"},
            project_root=tmp_path,
            layout_override=ChunkLayout.CHUNKS_DB,
        )

        assert count == 1, (
            "An ABSOLUTE stored path in a CHUNKS_DB collection must be "
            "normalized against project_root before comparing to the "
            "(relative) visible_files set."
        )

    def test_absolute_stored_path_normalizing_outside_visible_set_is_excluded(
        self, tmp_path: Path
    ):
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path)

        absolute_path = str(tmp_path / "src" / "not_visible.py")
        with ChunkStore(collection_path / "chunks.db") as store:
            store.write_batch(
                [
                    {
                        "id": "vec_abs",
                        "vector": np.random.randn(VECTOR_DIM).tolist(),
                        "payload": {"path": absolute_path, "type": "content"},
                    }
                ]
            )

        manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path,
            visible_files={"src/other.py"},
            project_root=tmp_path,
            layout_override=ChunkLayout.CHUNKS_DB,
        )

        assert count == 0


class TestRebuildHnswFilteredWiresProjectRoot:
    """Bug #1575 CRITICAL finding 2: ``FilesystemVectorStore.rebuild_hnsw_filtered()``
    (the REAL production call site branch isolation uses) must pass
    ``project_root`` through to ``HNSWIndexManager.rebuild_from_vectors()``
    so the normalization fix actually takes effect end-to-end, not merely
    at the unit level.
    """

    def test_absolute_stored_path_is_correctly_included_via_real_store(
        self, tmp_path: Path
    ):
        from code_indexer.storage.filesystem_vector_store import (
            FilesystemVectorStore,
        )

        store = FilesystemVectorStore(
            tmp_path, project_root=tmp_path, use_chunks_db_for_new_collections=False
        )
        store.create_collection("test_collection", vector_size=VECTOR_DIM)

        collection_path = tmp_path / "test_collection"
        absolute_path = str(tmp_path / "src" / "a.py")
        _write_vector_file(collection_path, "vec_abs", absolute_path)

        count = store.rebuild_hnsw_filtered(
            "test_collection", visible_files={"src/a.py"}
        )

        assert count == 1, (
            "rebuild_hnsw_filtered() must pass project_root through to "
            "rebuild_from_vectors() so an ABSOLUTE stored path is "
            "correctly normalized and included."
        )


class TestNoProjectRootPreservesRawComparisonBehavior:
    """project_root=None (every pre-existing caller) must remain
    byte-identical to today: a relative stored path against a relative
    visible_files set still works with no normalization applied.
    """

    def test_relative_paths_still_match_without_project_root(self, tmp_path: Path):
        collection_path = tmp_path / "test_coll"
        collection_path.mkdir()
        _make_collection_meta(collection_path)

        _write_vector_file(collection_path, "vec_rel", "src/a.py")

        manager = HNSWIndexManager(vector_dim=VECTOR_DIM, space="cosine")
        count = manager.rebuild_from_vectors(
            collection_path,
            visible_files={"src/a.py"},
        )

        assert count == 1
