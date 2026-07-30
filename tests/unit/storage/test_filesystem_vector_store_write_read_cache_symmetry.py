"""Write/read cache-key symmetry across the top-level/nested-subdirectory
boundary for TWO of the three shared in-memory caches keyed by bare
``collection_name`` in ``FilesystemVectorStore``: ``self._id_index`` and
``self._path_indexes``.

A prior fix introduced ``_id_cache_key(collection_name, subdirectory)`` and
applied it to SOME read sites (``get_point``, search's ``_id_index``
fallback, ``_get_vector_size``, ``_save_path_index``). A code review found
this created an UNSAFE ASYMMETRY: the WRITE/active-session sites
(``create_collection``, ``begin_indexing``, ``upsert_points``) and the
ENTIRE ``_path_indexes`` dict still keyed by BARE ``collection_name``. For a
nested ``multimodal_index/X`` collection sharing a top-level collection name
``X`` on ONE long-lived store instance, a write-under-bare / read-under-
composed mismatch returns WRONG DATA or an empty page.

Test 1 (``_id_index``, Codex NEW Finding 1): drives the REAL nested-write
path (``create_collection``/``begin_indexing``/``upsert_points`` with an
explicit ``subdirectory``) on ONE store instance for BOTH a top-level and a
nested collection sharing the same bare name, then proves a top-level
``get_point`` read afterward still returns the TOP-LEVEL row (not corrupted
by the nested write). ``end_indexing`` is deliberately never called in this
test -- it is not needed to reproduce or observe the ``_id_index`` bug
(vector JSON files and the in-memory ``_id_index`` entry are both written
synchronously by ``upsert_points``), and calling it would incidentally
exercise ``_apply_incremental_hnsw_batch_update``'s SEPARATE, already known,
pre-existing physical-path bug (documented in
``test_filesystem_vector_store_nested_cache_collision.py``), which is
unrelated to what this test is proving and would risk a real hnswlib
dimension-mismatch crash if left unpatched.

Test 2 (``_path_indexes``, Codex NEW Finding 2): builds a top-level and a
nested collection independently (via isolated throwaway builder stores,
copied into place) so the live store under test never writes to either
collection in-memory -- it only READS. This isolates the exact bug: the
LAZY-LOAD/CACHE step inside ``scroll_points``'s path-equality fast path
(``if collection_name not in self._path_indexes: ... self._path_indexes[
collection_name] = loaded``) is keyed by bare ``collection_name``, so a
top-level path-filtered scroll that populates this cache first causes a
LATER nested-subdirectory scroll for the SAME bare name to reuse the
top-level's ``PathIndex`` object and silently return an EMPTY page instead
of the nested match.

Real filesystem + real on-disk JSON/binary index files; no mocking of the
store's own logic.
"""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_SIZE = 16
NESTED_SUBDIRECTORY = "multimodal_index"


def _vector(seed: int, dim: int = VECTOR_SIZE) -> List[float]:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(dim).astype(np.float64).tolist()  # type: ignore[no-any-return]


class TestNestedWriteReadIdIndexSymmetry:
    """Codex NEW Finding 1: write-path ``_id_index`` cache key must match
    the read-path key for the SAME physical (collection_name, subdirectory).
    """

    def test_top_level_write_survives_nested_write_on_same_store(
        self, tmp_path: Path
    ) -> None:
        collection_name = "shared_coll"
        base_path = tmp_path / "index"
        base_path.mkdir()

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        # --- Write top-level collection X (row A) ---
        store.create_collection(collection_name, vector_size=VECTOR_SIZE)
        store.begin_indexing(collection_name)
        top_point_id = "pA0"
        top_result = store.upsert_points(
            collection_name,
            [
                {
                    "id": top_point_id,
                    "vector": _vector(seed=1),
                    "payload": {"path": "top/file0.py"},
                }
            ],
            subdirectory=None,
        )
        assert top_result["status"] == "ok"

        # --- Write nested collection multimodal_index/X (row B, disjoint) ---
        # This is the REAL nested-write path: explicit subdirectory threaded
        # through create_collection/begin_indexing/upsert_points, exactly as
        # these methods' own signatures support.
        store.create_collection(
            collection_name, vector_size=VECTOR_SIZE, subdirectory=NESTED_SUBDIRECTORY
        )
        store.begin_indexing(collection_name, subdirectory=NESTED_SUBDIRECTORY)
        nested_point_id = "pB0"
        nested_result = store.upsert_points(
            collection_name,
            [
                {
                    "id": nested_point_id,
                    "vector": _vector(seed=2),
                    "payload": {"path": "nested/file0.py"},
                }
            ],
            subdirectory=NESTED_SUBDIRECTORY,
        )
        assert nested_result["status"] == "ok"

        # --- Read top-level: must still return row A, never row B nor None ---
        top_read = store.get_point(top_point_id, collection_name, subdirectory=None)
        assert top_read is not None, (
            "top-level get_point returned None after a nested write -- the "
            "nested write corrupted/overwrote the shared bare-keyed "
            "_id_index[collection_name] entry"
        )
        assert top_read["payload"]["path"] == "top/file0.py", (
            "top-level get_point returned the WRONG row after a nested "
            "write -- write-path/read-path _id_index cache-key asymmetry"
        )

        # --- Read nested: must return row B ---
        nested_read = store.get_point(
            nested_point_id, collection_name, subdirectory=NESTED_SUBDIRECTORY
        )
        assert nested_read is not None
        assert nested_read["payload"]["path"] == "nested/file0.py"


class TestScrollPathIndexReuseAcrossSubdirectory:
    """Codex NEW Finding 2: scroll_points' fast-path PathIndex lazy-load
    cache (``self._path_indexes[collection_name]``) must not be reused
    across the top-level/nested-subdirectory boundary.
    """

    @staticmethod
    def _build_on_disk(
        base_path: Path,
        collection_name: str,
        subdirectory: Optional[str],
        point_id: str,
        file_path: str,
    ) -> None:
        """Build a real, on-disk SHARDED_JSON collection at the target
        physical location using an ISOLATED, throwaway builder store (never
        the live store under test), so the live store's own in-memory
        ``_path_indexes`` cache is never pre-populated by this setup step --
        only by the ``scroll_points`` calls under test.
        """
        with TemporaryDirectory() as builder_base_str:
            builder_base = Path(builder_base_str)
            builder = FilesystemVectorStore(
                base_path=builder_base, use_chunks_db_for_new_collections=False
            )
            builder.create_collection(collection_name, vector_size=VECTOR_SIZE)
            builder.upsert_points(
                collection_name,
                [
                    {
                        "id": point_id,
                        "vector": _vector(seed=3),
                        "payload": {"path": file_path},
                    }
                ],
            )

            target_dir = (
                base_path / subdirectory / collection_name
                if subdirectory
                else base_path / collection_name
            )
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(builder_base / collection_name, target_dir)

    def test_nested_scroll_after_top_level_scroll_returns_nested_match(
        self, tmp_path: Path
    ) -> None:
        collection_name = "shared_coll"
        base_path = tmp_path / "index"
        base_path.mkdir()

        self._build_on_disk(base_path, collection_name, None, "pA0", "top/file0.py")
        self._build_on_disk(
            base_path,
            collection_name,
            NESTED_SUBDIRECTORY,
            "pB0",
            "nested/file0.py",
        )

        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        # Trigger the top-level path-index load FIRST -- populates
        # self._path_indexes[collection_name] (bare key) via the fast path's
        # own lazy-load/rebuild-from-disk mechanism.
        top_points, _ = store.scroll_points(
            collection_name,
            limit=10,
            filter_conditions={
                "must": [{"key": "path", "match": {"value": "top/file0.py"}}]
            },
        )
        assert [p["id"] for p in top_points] == ["pA0"]

        # Now scroll the NESTED collection sharing the SAME bare name. Must
        # find its OWN match, never an empty page caused by reusing the
        # top-level's cached PathIndex object.
        nested_points, _ = store.scroll_points(
            collection_name,
            subdirectory=NESTED_SUBDIRECTORY,
            limit=10,
            filter_conditions={
                "must": [{"key": "path", "match": {"value": "nested/file0.py"}}]
            },
        )
        assert nested_points, (
            "nested scroll_points returned an EMPTY page after a top-level "
            "path-index load populated the shared bare-keyed "
            "_path_indexes[collection_name] cache -- the nested scroll "
            "reused the top-level's PathIndex object instead of loading its "
            "own"
        )
        assert [p["id"] for p in nested_points] == ["pB0"]
