"""Two Codex-16 correctness findings in
``FilesystemVectorStore.scroll_points``'s nested (``multimodal_index/<coll>``)
subdirectory support, both OUTSIDE an active indexing session (fresh store
instance, empty ``_active_subdirectories``).

Finding 3 (MEDIUM) -- ``scroll_points``'s PathIndex fast path calls
    ``self._rebuild_path_index_from_disk(collection_name)`` WITHOUT the
    explicit ``subdirectory`` it already resolved. That helper then falls
    back to ``self._active_subdirectories`` (empty outside an active
    indexing session), mis-resolving the collection path and silently
    walking a non-existent top-level directory -- the rebuild finds no
    files, persists an empty ``path_index.bin`` at the WRONG location, and
    the scroll returns no matches for a collection that genuinely has real
    rows. Fix: thread the explicit ``subdirectory`` through
    ``_rebuild_path_index_from_disk`` (and the ``_save_path_index`` calls it
    makes internally) instead of relying on the active-session fallback.

Finding 4 (latent) -- ``_get_vector_size`` resolves its collection path via
    ``self._active_subdirectories`` only, ignoring an explicit subdirectory a
    caller (e.g. ``scroll_points(with_vectors=True)``) already knows. Outside
    an active session this mis-resolves to a non-existent top-level
    ``collection_meta.json`` and raises ``RuntimeError`` instead of returning
    the real vectors. Fix: thread the explicit ``subdirectory`` through
    ``_get_vector_size`` from every scroll code path that calls it under
    ``with_vectors=True`` (the PathIndex fast path, the CHUNKS_DB helper, and
    the rglob safety-valve path).

A THIRD, unrelated, pre-existing, out-of-scope defect was discovered while
writing these tests: ``get_point()`` (line ~2718) calls
``self._load_id_index(collection_name)`` for SHARDED_JSON hydration, and
``_load_id_index()`` (line ~2294) unconditionally resolves
``self.base_path / collection_name`` -- it does not consult
``_active_subdirectories`` at all, let alone an explicit ``subdirectory``, so
it ALWAYS mis-resolves a nested collection's ``id_index.bin`` outside an
active indexing session, regardless of any fix here. That defect sits
outside this story's two Findings (scoped to ``_rebuild_path_index_from_disk``
and ``_get_vector_size`` only) and is shared by 14 call sites across this
module, so it is not fixed in this change. Finding 3's end-to-end test below
therefore builds its nested collection with
``use_chunks_db_for_new_collections=True``: CHUNKS_DB hydration goes through
``_get_point_from_chunk_store(collection_path, point_id)`` using the
ALREADY-correctly-resolved ``collection_path`` computed at the top of
``get_point()``, bypassing ``_load_id_index`` entirely -- isolating the
Finding-3 rebuild defect (which is layout-agnostic: it mis-resolves the
collection PATH before layout detection even runs) from that separate,
unrelated hydration defect. Finding 4's tests are unaffected -- they never
reach ``get_point()``.

CLOSURE NOTE (Bug #1488 follow-up): the THIRD defect described above is now
FIXED. ``_load_id_index()`` gained an optional ``subdirectory`` parameter
(``None`` -> byte-identical ``self.base_path / collection_name``, matching
every existing write-path caller); ``get_point()`` threads its own
``subdirectory`` through to it (see
``TestGetPointNestedIdIndexOutsideActiveSession`` below). ``search()``'s
``load_index()`` worker closure had the IDENTICAL defect at its own two
``_load_id_index(collection_name)`` call sites and is fixed the same way
(see ``TestSearchNestedIdIndexOutsideActiveSession`` below) -- both were
already-in-scope ``subdirectory`` values simply never threaded through.
The historical analysis above (why Finding 4's test avoided an end-to-end
route through ``get_point()``) is preserved as an accurate record of the
constraint that existed AT THE TIME that test was written.

Real filesystem + real SQLite (via FilesystemVectorStore's own real I/O),
deterministic, no sleeps, no mocking of the store's own logic (embedding
providers are mocked -- they are an external dependency, not the store).
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import Mock

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_SIZE = 32
COLLECTION = "coll"


def _points(spec: List[Tuple[str, str, str]]) -> List[Dict]:
    """spec is a list of (point_id, path, language) tuples."""
    rng = np.random.default_rng(1616)
    out: List[Dict] = []
    for i, (pid, path, language) in enumerate(spec):
        v = rng.standard_normal(VECTOR_SIZE)
        v[i % VECTOR_SIZE] += 10.0
        out.append(
            {
                "id": pid,
                "vector": v.astype(np.float64).tolist(),
                "payload": {"path": path, "language": language},
            }
        )
    return out


def _build(
    base_path: Path,
    spec: List[Tuple[str, str, str]],
    *,
    subdirectory: Optional[str] = None,
    chunks_db: bool = False,
) -> FilesystemVectorStore:
    base_path.mkdir(parents=True, exist_ok=True)
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=chunks_db
    )
    store.create_collection(
        COLLECTION, vector_size=VECTOR_SIZE, subdirectory=subdirectory
    )
    store.begin_indexing(COLLECTION, subdirectory=subdirectory)
    store.upsert_points(COLLECTION, _points(spec))
    store.end_indexing(COLLECTION, subdirectory=subdirectory)
    return store


def _path_eq(value: str) -> Dict:
    return {"key": "path", "match": {"value": value}}


class TestNestedMultimodalPathIndexRebuildOutsideActiveSession:
    """Codex-16 Finding 3: ``_rebuild_path_index_from_disk`` must honor an
    explicit ``subdirectory`` instead of falling back to the (empty, outside
    an active indexing session) ``_active_subdirectories`` map."""

    def test_rebuild_finds_real_rows_for_nested_collection_missing_path_index(
        self, tmp_path: Path
    ) -> None:
        """Uses a CHUNKS_DB collection (see module docstring) so hydration
        goes through ``_get_point_from_chunk_store`` -- bypassing the
        unrelated, out-of-scope ``_load_id_index`` defect -- and isolates
        purely the Finding-3 ``_rebuild_path_index_from_disk`` path
        resolution defect, which is layout-agnostic (it mis-resolves the
        collection PATH before any layout detection runs)."""
        base_path = tmp_path / "index"
        _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "b.py", "python")],
            subdirectory="multimodal_index",
            chunks_db=True,
        )
        nested = base_path / "multimodal_index" / COLLECTION
        path_index_file = nested / "path_index.bin"
        assert path_index_file.exists(), "sanity: end_indexing persisted it"
        path_index_file.unlink()

        # Fresh store instance rooted at the SAME base_path: empty
        # _path_indexes cache (forces a real rebuild) AND empty
        # _active_subdirectories map (no active indexing session at all).
        fresh_store = FilesystemVectorStore(base_path=base_path)
        assert fresh_store._active_subdirectories == {}
        assert COLLECTION not in fresh_store._path_indexes

        filt = {"must": [_path_eq("b.py")]}
        points, _ = fresh_store.scroll_points(
            COLLECTION,
            limit=10,
            filter_conditions=filt,
            subdirectory="multimodal_index",
        )

        assert [p["id"] for p in points] == ["p001"], (
            f"nested collection's path-index rebuild dropped a real row "
            f"outside an active indexing session, got {points}"
        )

        # The rebuild's own persist step must also land the freshly-rebuilt
        # file at the REAL nested location, not a spurious top-level
        # directory -- otherwise every subsequent scroll outside an active
        # session repeats the same expensive rebuild forever.
        assert path_index_file.exists(), (
            "rebuilt path_index.bin must be persisted at the nested "
            "collection location, not a wrong top-level path"
        )
        assert not (base_path / COLLECTION).exists(), (
            "the rebuild must not create a spurious top-level "
            f"{COLLECTION!r} directory when resolving a nested collection"
        )


class TestNestedMultimodalVectorSizeOutsideActiveSession:
    """Codex-16 Finding 4: ``_get_vector_size`` must honor an explicit
    ``subdirectory`` instead of mis-resolving via the (empty, outside an
    active indexing session) ``_active_subdirectories`` map."""

    def test_rglob_safety_valve_with_vectors_resolves_nested_dimension(
        self, tmp_path: Path
    ) -> None:
        """No path filter -> the rglob safety-valve branch (not the
        PathIndex fast path) is exercised."""
        base_path = tmp_path / "index"
        _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "b.py", "python")],
            subdirectory="multimodal_index",
        )

        fresh_store = FilesystemVectorStore(base_path=base_path)
        assert fresh_store._active_subdirectories == {}
        assert fresh_store._vector_size_cache == {}

        points, _ = fresh_store.scroll_points(
            COLLECTION,
            limit=10,
            with_vectors=True,
            subdirectory="multimodal_index",
        )

        assert sorted(p["id"] for p in points) == ["p000", "p001"]
        for p in points:
            assert len(p["vector"]) == VECTOR_SIZE, (
                f"vector for {p['id']} has wrong dimension "
                f"{len(p['vector'])}, expected {VECTOR_SIZE} -- the nested "
                f"collection's vector dimension was mis-resolved"
            )

    def test_get_vector_size_resolves_nested_dimension_directly(
        self, tmp_path: Path
    ) -> None:
        """Direct unit test on ``_get_vector_size(subdirectory=...)`` --
        NOT an end-to-end scroll -- because the true end-to-end route (a
        path-equality filter through the PathIndex fast path with
        ``with_vectors=True``) is genuinely blocked by an UNRELATED,
        pre-existing, out-of-scope defect: ``get_point()``
        (``filesystem_vector_store.py`` line ~2718) calls
        ``self._load_id_index(collection_name)``, and ``_load_id_index()``
        (line ~2294) unconditionally resolves
        ``self.base_path / collection_name`` -- it does not consult
        ``_active_subdirectories`` at all, let alone an explicit
        ``subdirectory``, so it ALWAYS mis-resolves a nested collection's
        ``id_index.bin`` regardless of any fix to ``_get_vector_size``. That
        defect sits outside this story's two Findings (scoped to
        ``_rebuild_path_index_from_disk`` and ``_get_vector_size`` only) and
        is shared by 14 call sites across this module, so it is not fixed
        here. This test instead directly proves ``_get_vector_size``'s own
        fix resolves a nested collection's dimension correctly, isolated
        from that unrelated defect.
        """
        base_path = tmp_path / "index"
        _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "b.py", "python")],
            subdirectory="multimodal_index",
        )

        fresh_store = FilesystemVectorStore(base_path=base_path)
        assert fresh_store._active_subdirectories == {}
        assert fresh_store._vector_size_cache == {}

        resolved = fresh_store._get_vector_size(
            COLLECTION, subdirectory="multimodal_index"
        )

        assert resolved == VECTOR_SIZE


class TestGetPointNestedIdIndexOutsideActiveSession:
    """Closes the THIRD defect this module's docstring flagged as
    pre-existing and out-of-scope: ``get_point()``'s SHARDED_JSON hydration
    branch calls ``self._load_id_index(collection_name)`` without the
    ``subdirectory`` it already receives/resolves via
    ``self._get_collection_path(collection_name, subdirectory)``, and
    ``_load_id_index()`` itself unconditionally resolved
    ``self.base_path / collection_name`` -- it never consulted
    ``_active_subdirectories``, let alone an explicit ``subdirectory`` --
    so it ALWAYS mis-resolved a nested collection's ``id_index.bin`` (and
    its ``vector_*.json`` rglob fallback) outside an active indexing
    session, regardless of the Finding 3/4 fixes above.

    Fix: ``_load_id_index`` gained an optional ``subdirectory`` parameter
    (``None`` -> byte-identical ``self.base_path / collection_name``,
    matching every existing write-path caller); ``get_point()`` now passes
    its own ``subdirectory`` through to it.
    """

    def test_get_point_resolves_nested_sharded_json_collection_outside_active_session(
        self, tmp_path: Path
    ) -> None:
        """Real nested SHARDED_JSON collection (not chunks_db -- this
        defect lives specifically in the legacy id-index hydration path),
        read via a FRESH store instance (empty _active_subdirectories,
        empty _id_index -- no active indexing session at all)."""
        base_path = tmp_path / "index"
        _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "b.py", "python")],
            subdirectory="multimodal_index",
            chunks_db=False,
        )

        fresh_store = FilesystemVectorStore(base_path=base_path)
        assert fresh_store._active_subdirectories == {}
        assert fresh_store._id_index == {}

        result = fresh_store.get_point(
            "p001", COLLECTION, subdirectory="multimodal_index"
        )

        assert result is not None, (
            "get_point mis-resolved id_index.bin to a non-existent "
            "top-level directory for a nested collection outside an "
            "active indexing session"
        )
        assert result["id"] == "p001"
        assert result["payload"]["path"] == "b.py"

        # Must never have created a spurious top-level collection
        # directory while mis-resolving the nested id_index.bin location.
        assert not (base_path / COLLECTION).exists(), (
            "get_point/_load_id_index must not touch a wrong top-level "
            f"{COLLECTION!r} directory when resolving a nested collection"
        )


class TestSearchNestedIdIndexOutsideActiveSession:
    """``search()``'s ``load_index()`` worker closure has the IDENTICAL
    defect ``get_point()`` had: two ``self._load_id_index(collection_name)``
    call sites (the ``id_index_cache`` lambda branch and the direct
    ``self._id_index`` branch) ignore the ``subdirectory`` already resolved
    into ``collection_path`` at the top of ``search()``, in scope for the
    closure. Outside an active indexing session (fresh store, empty
    ``_id_index``), a nested SHARDED_JSON collection's id-index hydration
    for query results mis-resolves to a non-existent top-level directory,
    leaving every candidate absent from ``existing_id_index`` and dropping
    all results from a collection that genuinely has real rows.
    """

    def test_search_resolves_nested_sharded_json_collection_outside_active_session(
        self, tmp_path: Path
    ) -> None:
        """Real nested SHARDED_JSON collection, real HNSW index (built by
        ``end_indexing`` inside ``_build``), queried via a FRESH store
        instance (empty ``_active_subdirectories``, empty ``_id_index`` --
        default (uncached) ``id_index_cache``/``hnsw_index_cache``, so the
        direct ``self._id_index`` branch at the closure's ``else`` clause is
        exercised). A mock embedding provider returns the EXACT stored
        vector for ``p001`` -- the embedding provider is an external
        dependency, not the store under test."""
        base_path = tmp_path / "index"
        spec = [("p000", "a.py", "java"), ("p001", "b.py", "python")]
        _build(base_path, spec, subdirectory="multimodal_index", chunks_db=False)

        p001_vector = next(p["vector"] for p in _points(spec) if p["id"] == "p001")

        fresh_store = FilesystemVectorStore(base_path=base_path)
        assert fresh_store._active_subdirectories == {}
        assert fresh_store._id_index == {}

        mock_provider = Mock()
        mock_provider.get_embedding.return_value = p001_vector
        mock_provider.get_provider_name.return_value = "mock-provider"

        results = fresh_store.search(
            query="anything",
            embedding_provider=mock_provider,
            collection_name=COLLECTION,
            limit=5,
            subdirectory="multimodal_index",
        )

        assert len(results) > 0, (
            "search() mis-resolved the nested collection's id_index.bin "
            "to a non-existent top-level directory outside an active "
            "indexing session, dropping every real result"
        )
        assert results[0]["id"] == "p001"
        assert results[0]["payload"]["path"] == "b.py"
