"""Nested-collection cache collision: ``_id_index`` / ``_vector_size_cache``
are keyed by BARE ``collection_name``, but a top-level collection
(``base_path/X``) and a nested collection sharing the SAME name
(``base_path/multimodal_index/X``) are two DIFFERENT physical directories.

Confirmed reachable-today collision: ``MultiIndexQueryService`` reuses ONE
long-lived ``FilesystemVectorStore`` (daemon mode). ``_query_code_index``
calls ``search(collection_name=X, subdirectory=None)``; the legacy
``_query_multimodal_index`` branch calls
``search(collection_name=X, subdirectory="multimodal_index")`` with the SAME
``collection_name`` X. Whichever query runs first populates
``self._id_index[X]`` (and ``self._vector_size_cache[X]``) with THAT
physical collection's map; the second query finds the bare-name entry
already cached and reuses the WRONG physical directory's data.

These tests build TWO real, disjoint SHARDED_JSON collections that share a
bare name -- top-level ``base_path/X`` and nested
``base_path/multimodal_index/X`` -- on ONE ``FilesystemVectorStore``
instance, and prove that querying one after the other returns the SECOND
query's OWN data, never the FIRST query's cached wrong-directory data, in
both orderings.

Real filesystem + real HNSW + real id_index.bin; no mocking of the store's
own logic.
"""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional

import numpy as np
from unittest.mock import Mock

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
)

VECTOR_SIZE = 32
NUM_POINTS = 4
SHARED_COLLECTION_NAME = "shared_coll"
NESTED_SUBDIRECTORY = "multimodal_index"


def _make_vectors(seed: int, count: int, dim: int = VECTOR_SIZE) -> List[np.ndarray]:
    rng = np.random.default_rng(seed)
    vecs = []
    for i in range(count):
        v = rng.standard_normal(dim)
        v[i % dim] += 25.0  # dominant, distinct component per point
        vecs.append(v.astype(np.float64))
    return vecs


def _build_collection(
    base_path: Path,
    collection_name: str,
    subdirectory: Optional[str],
    vectors: List[np.ndarray],
    path_prefix: str,
    vector_size: int = VECTOR_SIZE,
) -> None:
    """Build a real, searchable SHARDED_JSON collection and place it at the
    target physical location (``base_path/collection_name`` when
    ``subdirectory`` is ``None``, else
    ``base_path/subdirectory/collection_name``).

    The collection is built via the NORMAL top-level write path
    (``begin_indexing``/``upsert_points``/``end_indexing`` with
    ``subdirectory=None``) in an ISOLATED, throwaway
    ``TemporaryDirectory`` (auto-cleaned on exit), then the finished
    directory tree is copied into place. This deliberately avoids ever
    calling the write-path lifecycle methods with a non-None
    ``subdirectory`` (a combination no production caller ever exercises --
    confirmed by grep: no ``begin_indexing``/``upsert_points`` call site in
    the codebase passes a non-None ``subdirectory``). That untested
    combination used to also hit a pre-existing gap in the retired
    ``_apply_incremental_hnsw_batch_update`` (it recomputed a bare
    ``self.base_path / collection_name``, ignoring ``subdirectory``, and
    could segfault when a same-named top-level collection already existed
    on disk at ``base_path``); Bug #1575 Part C's replacement,
    ``_apply_visibility_aware_incremental_update``, fixes this by
    construction -- it receives ``collection_path`` as an already-resolved
    parameter and never recomputes it. The isolation-then-copy approach is
    kept regardless, since the ``subdirectory`` combination itself remains
    untested by any production caller -- this fixture stays focused purely
    on the READ-side ``_id_index``/``_vector_size_cache`` collision under
    test.
    """
    with TemporaryDirectory() as builder_base_str:
        builder_base = Path(builder_base_str)
        builder = FilesystemVectorStore(
            base_path=builder_base, use_chunks_db_for_new_collections=False
        )
        builder.create_collection(collection_name, vector_size=vector_size)
        points = [
            {
                "id": f"p{i}",
                "vector": vectors[i].tolist(),
                "payload": {
                    "path": f"{path_prefix}/file{i}.py",
                    "language": "python",
                },
            }
            for i in range(len(vectors))
        ]
        builder.begin_indexing(collection_name)
        result = builder.upsert_points(collection_name, points)
        assert result["status"] == "ok"
        builder.end_indexing(collection_name)

        target_dir = (
            base_path / subdirectory / collection_name
            if subdirectory
            else base_path / collection_name
        )
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(builder_base / collection_name, target_dir)


class TestGetPointNestedCacheCollision:
    """Task 1: ``get_point`` must not leak the OTHER physical collection's
    ``_id_index`` map across a bare-name top-level/nested pair."""

    def test_top_level_then_nested_get_point_returns_nested_data(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        top_vectors = _make_vectors(seed=1, count=NUM_POINTS)
        nested_vectors = _make_vectors(seed=2, count=NUM_POINTS)
        _build_collection(base_path, SHARED_COLLECTION_NAME, None, top_vectors, "top")
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            NESTED_SUBDIRECTORY,
            nested_vectors,
            "nested",
        )

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        # First call: top-level, subdirectory=None -- populates the shared
        # bare-keyed cache with the TOP-LEVEL collection's id->file map.
        top_result = store.get_point("p0", SHARED_COLLECTION_NAME, subdirectory=None)
        assert top_result is not None
        assert top_result["payload"]["path"] == "top/file0.py"

        # Second call: SAME bare collection_name, but nested subdirectory.
        # Must hydrate from the NESTED collection's own data, not the
        # top-level map cached by the first call.
        nested_result = store.get_point(
            "p0", SHARED_COLLECTION_NAME, subdirectory=NESTED_SUBDIRECTORY
        )
        assert nested_result is not None, (
            "nested get_point must not silently miss due to a stale "
            "top-level-keyed id_index cache entry"
        )
        assert nested_result["payload"]["path"] == "nested/file0.py", (
            "nested get_point returned the TOP-LEVEL collection's cached "
            "data -- bare collection_name id_index cache collision"
        )

    def test_nested_then_top_level_get_point_returns_top_level_data(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        top_vectors = _make_vectors(seed=1, count=NUM_POINTS)
        nested_vectors = _make_vectors(seed=2, count=NUM_POINTS)
        _build_collection(base_path, SHARED_COLLECTION_NAME, None, top_vectors, "top")
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            NESTED_SUBDIRECTORY,
            nested_vectors,
            "nested",
        )

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        # Reverse order: nested FIRST, then top-level.
        nested_result = store.get_point(
            "p0", SHARED_COLLECTION_NAME, subdirectory=NESTED_SUBDIRECTORY
        )
        assert nested_result is not None
        assert nested_result["payload"]["path"] == "nested/file0.py"

        top_result = store.get_point("p0", SHARED_COLLECTION_NAME, subdirectory=None)
        assert top_result is not None, (
            "top-level get_point must not silently miss due to a stale "
            "nested-keyed id_index cache entry"
        )
        assert top_result["payload"]["path"] == "top/file0.py", (
            "top-level get_point returned the NESTED collection's cached "
            "data -- bare collection_name id_index cache collision"
        )


class TestSearchNestedCacheCollision:
    """Task 1: ``search()``'s bare ``self._id_index`` fallback branch
    (``id_index_cache is None`` -- the CLI/solo/daemon default) must not
    leak the OTHER physical collection's map across a bare-name
    top-level/nested pair."""

    def test_top_level_then_nested_search_returns_nested_data(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        top_vectors = _make_vectors(seed=1, count=NUM_POINTS)
        nested_vectors = _make_vectors(seed=2, count=NUM_POINTS)
        _build_collection(base_path, SHARED_COLLECTION_NAME, None, top_vectors, "top")
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            NESTED_SUBDIRECTORY,
            nested_vectors,
            "nested",
        )

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        provider = Mock()

        provider.get_embedding.return_value = top_vectors[0].tolist()
        top_results = store.search(
            query="anything",
            embedding_provider=provider,
            collection_name=SHARED_COLLECTION_NAME,
            limit=1,
            subdirectory=None,
        )
        assert len(top_results) == 1
        assert top_results[0]["id"] == "p0"
        assert top_results[0]["payload"]["path"] == "top/file0.py"

        provider.get_embedding.return_value = nested_vectors[0].tolist()
        nested_results = store.search(
            query="anything",
            embedding_provider=provider,
            collection_name=SHARED_COLLECTION_NAME,
            limit=1,
            subdirectory=NESTED_SUBDIRECTORY,
        )
        assert len(nested_results) == 1, (
            "nested search must not silently drop its own match due to a "
            "stale top-level-keyed id_index cache entry"
        )
        assert nested_results[0]["id"] == "p0"
        assert nested_results[0]["payload"]["path"] == "nested/file0.py", (
            "nested search returned the TOP-LEVEL collection's cached "
            "data -- bare collection_name id_index cache collision"
        )

    def test_nested_then_top_level_search_returns_top_level_data(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        top_vectors = _make_vectors(seed=1, count=NUM_POINTS)
        nested_vectors = _make_vectors(seed=2, count=NUM_POINTS)
        _build_collection(base_path, SHARED_COLLECTION_NAME, None, top_vectors, "top")
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            NESTED_SUBDIRECTORY,
            nested_vectors,
            "nested",
        )

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        provider = Mock()

        provider.get_embedding.return_value = nested_vectors[0].tolist()
        nested_results = store.search(
            query="anything",
            embedding_provider=provider,
            collection_name=SHARED_COLLECTION_NAME,
            limit=1,
            subdirectory=NESTED_SUBDIRECTORY,
        )
        assert len(nested_results) == 1
        assert nested_results[0]["id"] == "p0"
        assert nested_results[0]["payload"]["path"] == "nested/file0.py"

        provider.get_embedding.return_value = top_vectors[0].tolist()
        top_results = store.search(
            query="anything",
            embedding_provider=provider,
            collection_name=SHARED_COLLECTION_NAME,
            limit=1,
            subdirectory=None,
        )
        assert len(top_results) == 1, (
            "top-level search must not silently drop its own match due to "
            "a stale nested-keyed id_index cache entry"
        )
        assert top_results[0]["id"] == "p0"
        assert top_results[0]["payload"]["path"] == "top/file0.py", (
            "top-level search returned the NESTED collection's cached "
            "data -- bare collection_name id_index cache collision"
        )


class TestGetVectorSizeNestedCacheCollision:
    """Task 1: ``_get_vector_size``'s ``_vector_size_cache`` must not leak
    the OTHER physical collection's dimension across a bare-name
    top-level/nested pair with DIFFERENT vector sizes."""

    def test_top_level_then_nested_uses_nested_own_dimension(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        top_dim = 32
        nested_dim = 64
        top_vectors = _make_vectors(seed=1, count=NUM_POINTS, dim=top_dim)
        nested_vectors = _make_vectors(seed=2, count=NUM_POINTS, dim=nested_dim)
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            None,
            top_vectors,
            "top",
            vector_size=top_dim,
        )
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            NESTED_SUBDIRECTORY,
            nested_vectors,
            "nested",
            vector_size=nested_dim,
        )

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        assert (
            store._get_vector_size(SHARED_COLLECTION_NAME, subdirectory=None) == top_dim
        )
        nested_size = store._get_vector_size(
            SHARED_COLLECTION_NAME, subdirectory=NESTED_SUBDIRECTORY
        )
        assert nested_size == nested_dim, (
            f"_get_vector_size returned {nested_size} (the cached TOP-LEVEL "
            f"collection's dimension {top_dim}) instead of the nested "
            f"collection's OWN dimension {nested_dim} -- bare "
            f"collection_name _vector_size_cache collision"
        )

    def test_nested_then_top_level_uses_top_level_own_dimension(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        top_dim = 32
        nested_dim = 64
        top_vectors = _make_vectors(seed=1, count=NUM_POINTS, dim=top_dim)
        nested_vectors = _make_vectors(seed=2, count=NUM_POINTS, dim=nested_dim)
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            None,
            top_vectors,
            "top",
            vector_size=top_dim,
        )
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            NESTED_SUBDIRECTORY,
            nested_vectors,
            "nested",
            vector_size=nested_dim,
        )

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        assert (
            store._get_vector_size(
                SHARED_COLLECTION_NAME, subdirectory=NESTED_SUBDIRECTORY
            )
            == nested_dim
        )
        top_size = store._get_vector_size(SHARED_COLLECTION_NAME, subdirectory=None)
        assert top_size == top_dim, (
            f"_get_vector_size returned {top_size} (the cached NESTED "
            f"collection's dimension {nested_dim}) instead of the "
            f"top-level collection's OWN dimension {top_dim} -- bare "
            f"collection_name _vector_size_cache collision"
        )


class TestSavePathIndexCoPersistCollision:
    """Task 1 (additional finding): ``_save_path_index``'s id_index
    co-persist step reads ``self._id_index`` -- reachable from
    ``scroll_points`` via ``_rebuild_path_index_from_disk`` when a nested
    collection's ``path_index.bin`` is missing/empty.

    NOTE on why this test reads RAW bytes instead of going through
    ``IDIndexManager.load_index``: a bare-keyed collision here writes the
    OTHER (differently-rooted) collection's absolute file paths into this
    collection's ``id_index.bin``. ``IDIndexManager``'s OWN existing
    corruption defense (``_safe_relative_path`` rejects absolute paths)
    then self-heals this on the VERY NEXT ``load_index()`` call via
    ``rebuild_from_vectors`` -- so asserting through ``load_index()`` would
    silently pass even on unpatched code (the self-heal masks the transient
    wrong write). Reading the raw on-disk bytes directly, immediately after
    calling ``_save_path_index`` and BEFORE anything else touches the file,
    observes the actual bytes ``_save_path_index`` wrote, bypassing that
    self-heal entirely -- the only way to genuinely discriminate this
    call site.
    """

    def test_save_path_index_does_not_write_other_collections_paths(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        top_vectors = _make_vectors(seed=1, count=NUM_POINTS)
        nested_vectors = _make_vectors(seed=2, count=NUM_POINTS)
        _build_collection(base_path, SHARED_COLLECTION_NAME, None, top_vectors, "top")
        _build_collection(
            base_path,
            SHARED_COLLECTION_NAME,
            NESTED_SUBDIRECTORY,
            nested_vectors,
            "nested",
        )

        top_collection_path = base_path / SHARED_COLLECTION_NAME
        nested_collection_path = (
            base_path / NESTED_SUBDIRECTORY / SHARED_COLLECTION_NAME
        )

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        # Populate the shared bare-keyed _id_index cache with the TOP-LEVEL
        # collection's id->file map (real absolute paths under
        # top_collection_path).
        top_result = store.get_point("p0", SHARED_COLLECTION_NAME, subdirectory=None)
        assert top_result is not None
        assert top_result["payload"]["path"] == "top/file0.py"

        # Call _save_path_index DIRECTLY for the NESTED collection. If its
        # id_index co-persist step reads the bare-keyed cache, it will try
        # to persist the TOP-LEVEL collection's id->file map (absolute
        # paths rooted at top_collection_path) into the NESTED collection's
        # id_index.bin.
        store._save_path_index(
            SHARED_COLLECTION_NAME, PathIndex(), subdirectory=NESTED_SUBDIRECTORY
        )

        # Read the RAW bytes immediately -- no IDIndexManager.load_index()
        # call (which would self-heal the corruption via rebuild_from_vectors
        # before this assertion ever ran).
        raw_bytes = (nested_collection_path / "id_index.bin").read_bytes()
        assert str(top_collection_path).encode("utf-8") not in raw_bytes, (
            f"_save_path_index wrote the TOP-LEVEL collection's absolute "
            f"path ({top_collection_path}) into the NESTED collection's "
            f"id_index.bin -- bare collection_name _id_index cache "
            f"collision in _save_path_index's co-persist step"
        )
