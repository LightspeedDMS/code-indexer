"""Bug #1486 Codex Finding 5 (HIGH) + Claude Finding 3 (LOW perf).

Finding 5 -- the *live* TOCTOU window the initial #1486 re-resolve missed. All
three FSV read entrypoints re-resolve the chunk-layout discriminator, but they do
so only AFTER a *clean* legacy miss / before the scan. They do NOT absorb the
narrower race where a concurrent server-mode fleet migration deletes the legacy
``vector_*.json`` file in the window BETWEEN the reader's ``Path.exists()`` check
(or its layout snapshot) and the actual ``open()``:

  * ``get_point()``     -- ``open()`` raises a raw ``FileNotFoundError`` (Codex
    repro: ``GET_POINT_ESCAPED_EXCEPTION= FileNotFoundError .../vector_p.json``)
    instead of re-resolving to CHUNKS_DB and returning the row from chunks.db.
  * ``scroll_points()`` -- the flip lands after its pre-scan resolve, so the
    rglob walks an empty tree (all legacy files deleted) and returns an
    empty/partial page instead of dispatching to the chunk store.
  * ``search()``        -- the flip lands after its final hydration resolve, so
    the stale legacy hydration branch's ``open()`` raises ``FileNotFoundError``
    and propagates instead of re-resolving to chunks.db.

Finding 3 -- ``search()`` re-resolves the discriminator twice per query (entry
snapshot for the cache-key tokens + a second read for hydration). The only
dangerous transition is SHARDED_JSON -> CHUNKS_DB (migration only ADDS chunks.db
and deletes legacy AFTER the flip). A collection already CHUNKS_DB at the entry
snapshot can never transition back, so the second resolve is pure waste on the
server hot path -- it must be skipped.

These tests reproduce the race DETERMINISTICALLY (no sleeps, no mocking of the
resolver's own logic) by advancing the REAL on-disk state via a genuine
``consolidate_collection_in_place`` call inside the exact window each read
entrypoint exposes:

  * get_point / search  -- a thin FSV subclass wraps the id-index Path values so
    that the FIRST ``.exists()`` call (which happens on the main/calling thread,
    AFTER the layout snapshot, immediately BEFORE the legacy ``open()``) advances
    the on-disk state to a fully-migrated CHUNKS_DB collection. The resolver is
    never mocked; only physical state advances between check and open.
  * scroll_points       -- a real side-effect wrapper around ``resolve_chunk_layout``
    that returns the TRUTHFUL layout at every call and fires the migration
    immediately after the pre-scan resolve returns SHARDED_JSON, modelling a flip
    that lands the instant after the reader read the (now-stale) discriminator.

Real filesystem, real SQLite (via the real ChunkStore / consolidation), real HNSW.
"""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import Mock

import numpy as np
import pytest

import code_indexer.storage.shared.chunk_layout as chunk_layout_mod
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)

VECTOR_SIZE = 128
NUM_POINTS = 8


def _make_vectors() -> List[np.ndarray]:
    rng = np.random.default_rng(1486)
    vecs = []
    for i in range(NUM_POINTS):
        v = rng.standard_normal(VECTOR_SIZE)
        v[i % VECTOR_SIZE] += 25.0  # dominant, distinct component per point
        vecs.append(v.astype(np.float64))
    return vecs


def _build_sharded_collection(
    base_path: Path, collection_name: str
) -> tuple[FilesystemVectorStore, List[np.ndarray]]:
    """Build a real, searchable SHARDED_JSON collection via the FSV lifecycle."""
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(collection_name, vector_size=VECTOR_SIZE)
    vectors = _make_vectors()
    points = [
        {
            "id": f"vec_{i}",
            "vector": vectors[i].tolist(),
            "payload": {"path": f"file_{i}.py", "language": "python"},
        }
        for i in range(NUM_POINTS)
    ]
    store.begin_indexing(collection_name)
    store.upsert_points(collection_name, points)
    store.end_indexing(collection_name)
    return store, vectors


class _MigrationOnExistsPath:
    """A path-like whose FIRST ``.exists()`` fires the migration callback and then
    reports ``True`` -- so the caller proceeds to ``open()`` a file that has, by
    that instant, been deleted by a genuine concurrent migration. Usable directly
    in ``open()`` via ``__fspath__``.
    """

    def __init__(self, real: Path, fire: Callable[[], None]) -> None:
        self._real = real
        self._fire = fire

    def exists(self) -> bool:
        self._fire()
        return True

    def __fspath__(self) -> str:
        return str(self._real)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_MigrationOnExistsPath({self._real!r})"


class _MigrationOnExistsStore(FilesystemVectorStore):
    """Wraps every id-index Path so the first ``.exists()`` on the main thread
    advances the REAL on-disk state to a fully-migrated CHUNKS_DB collection
    (chunks.db built + discriminator flipped + legacy vector_*.json / id_index.bin
    deleted) -- modelling a concurrent migration completing in the window between
    the exists-check and the legacy open. The resolver is never mocked.
    """

    def _load_id_index(
        self, collection_name: str, subdirectory: Optional[str] = None
    ) -> Dict[str, Path]:  # type: ignore[override]
        real = super()._load_id_index(collection_name, subdirectory)
        collection_path = self._get_collection_path(collection_name, subdirectory)

        def fire() -> None:
            if not getattr(self, "_race_fired", False):
                self._race_fired = True
                consolidate_collection_in_place(collection_path)

        return {
            pid: _MigrationOnExistsPath(p, fire)  # type: ignore[misc]
            for pid, p in real.items()
        }


class TestGetPointToctouFinding5:
    def test_get_point_toctou_file_deleted_between_exists_and_open(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        plain, _vectors = _build_sharded_collection(base_path, "c")
        collection_path = plain._get_collection_path("c")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        store = _MigrationOnExistsStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        # Without the fix this raises FileNotFoundError from the legacy open().
        record = store.get_point("vec_3", "c")

        assert getattr(store, "_race_fired", False) is True
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))
        assert record is not None, "get_point returned None despite valid chunks.db"
        assert record["id"] == "vec_3"
        assert record["payload"]["path"] == "file_3.py"
        assert len(record["vector"]) == VECTOR_SIZE


class TestSearchToctouFinding5:
    def test_search_toctou_legacy_file_deleted_after_hydration_resolve(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        _plain, vectors = _build_sharded_collection(base_path, "c")
        collection_path = _plain._get_collection_path("c")

        store = _MigrationOnExistsStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        provider = Mock()
        provider.get_embedding.return_value = vectors[0].tolist()

        # The hydration resolve reads SHARDED_JSON (migration not yet fired);
        # the legacy Case-A open() then hits a file the migration deleted.
        results = store.search(
            query="q",
            embedding_provider=provider,
            collection_name="c",
            limit=5,
        )

        assert getattr(store, "_race_fired", False) is True
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))
        assert len(results) > 0, "search returned empty despite valid chunks.db"
        assert results[0]["id"] == "vec_0"
        assert results[0]["score"] > 0.99
        assert results[0]["payload"]["path"] == "file_0.py"

    def test_search_filtered_toctou_legacy_file_deleted_after_hydration_resolve(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        _plain, vectors = _build_sharded_collection(base_path, "c")
        collection_path = _plain._get_collection_path("c")

        store = _MigrationOnExistsStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        provider = Mock()
        provider.get_embedding.return_value = vectors[0].tolist()

        # Case B (filtered) legacy hydration must also absorb the vanish.
        results = store.search(
            query="q",
            embedding_provider=provider,
            collection_name="c",
            limit=5,
            filter_conditions={"language": "python"},
        )

        assert getattr(store, "_race_fired", False) is True
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert len(results) > 0
        assert results[0]["id"] == "vec_0"


class TestScrollToctouFinding5:
    def test_scroll_flip_lands_after_prescan_resolve_empty_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store, _vectors = _build_sharded_collection(base_path, "c")
        collection_path = store._get_collection_path("c")

        real_resolve = chunk_layout_mod.resolve_chunk_layout
        state: Dict[str, Any] = {"calls": 0, "fired": False}

        def side_effecting_resolve(path: Any) -> ChunkLayout:
            result = real_resolve(path)
            state["calls"] += 1
            # scroll_points resolves twice before the scan: (1) inside
            # _is_chunks_db_collection, (2) the pre-scan re-resolve. Fire the
            # migration the instant AFTER the pre-scan resolve returns
            # SHARDED_JSON -- modelling a flip that lands before the rglob.
            if (
                state["calls"] == 2
                and not state["fired"]
                and result == ChunkLayout.SHARDED_JSON
            ):
                state["fired"] = True
                consolidate_collection_in_place(Path(path))
            return result

        monkeypatch.setattr(
            chunk_layout_mod, "resolve_chunk_layout", side_effecting_resolve
        )

        points, _next = store.scroll_points(
            collection_name="c", limit=100, with_payload=True
        )

        assert state["fired"] is True
        # Assert via the ORIGINAL (unpatched) resolver reference bound at import.
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))
        assert len(points) == NUM_POINTS, "scroll returned empty despite chunks.db"
        assert {p["id"] for p in points} == {f"vec_{i}" for i in range(NUM_POINTS)}


class TestSearchPerfGateFinding3:
    """The entry snapshot governs whether the hydration re-resolve is paid."""

    def _count_resolves_for_search(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        migrate: bool,
    ) -> tuple[int, List[Dict[str, Any]]]:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store, vectors = _build_sharded_collection(base_path, "c")
        collection_path = store._get_collection_path("c")
        if migrate:
            consolidate_collection_in_place(collection_path)
            assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        else:
            assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        real_resolve = chunk_layout_mod.resolve_chunk_layout
        calls: List[Path] = []

        def counting_resolve(path: Any) -> ChunkLayout:
            calls.append(Path(path))
            return real_resolve(path)

        monkeypatch.setattr(chunk_layout_mod, "resolve_chunk_layout", counting_resolve)

        provider = Mock()
        provider.get_embedding.return_value = vectors[4].tolist()
        results = reader.search(
            query="q",
            embedding_provider=provider,
            collection_name="c",
            limit=3,
        )
        assert isinstance(results, list)
        resolved_target = sum(1 for p in calls if p == collection_path)
        return resolved_target, results

    def test_entry_chunks_db_skips_second_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolved_target, results = self._count_resolves_for_search(
            tmp_path, monkeypatch, migrate=True
        )
        assert len(results) > 0
        assert results[0]["id"] == "vec_4"
        # Entry snapshot resolved CHUNKS_DB -> hydration re-resolve is skipped.
        assert resolved_target == 1, (
            "search must resolve the discriminator exactly once for an "
            f"already-CHUNKS_DB collection, got {resolved_target}"
        )

    def test_entry_sharded_json_pays_second_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolved_target, results = self._count_resolves_for_search(
            tmp_path, monkeypatch, migrate=False
        )
        assert len(results) > 0
        assert results[0]["id"] == "vec_4"
        # SHARDED_JSON entry snapshot must still pay the hydration re-resolve so
        # the race fix stays intact (entry + hydration == 2 resolves).
        assert resolved_target == 2, (
            "search must re-resolve at hydration for a SHARDED_JSON entry "
            f"snapshot, got {resolved_target}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
