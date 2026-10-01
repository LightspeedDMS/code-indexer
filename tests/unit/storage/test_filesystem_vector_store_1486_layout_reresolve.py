"""Codex Finding 4 (HIGH): an in-flight FSV reader must NOT return empty/partial
results when a concurrent server-mode fleet migration flips a collection's
chunk-layout discriminator (SHARDED_JSON -> CHUNKS_DB) and deletes the legacy
``vector_*.json`` / ``id_index.bin`` files in the window between the reader's
initial layout snapshot and its actual hydration.

Bug #1486's contract: during migration of a repo, READS remain available and the
SHARDED_JSON -> CHUNKS_DB swap is atomic from a reader's perspective. The
discriminator flip (``write_chunks_db_discriminator``) is the atomic swap point
and is committed durably BEFORE the legacy files are deleted
(``_cleanup_old_sharded_files``). The fix therefore re-resolves the committed
discriminator at the point of hydration -- on the calling/main thread, never in
the HNSW-load worker (Story #1456 AC7) -- and switches to the CHUNKS_DB
hydration branch if the layout changed.

These tests reproduce the race DETERMINISTICALLY (no sleeps, no mocking of the
resolver's own logic) by advancing the REAL on-disk state via a genuine
``consolidate_collection_in_place`` call inside the exact window each read
entrypoint exposes:

  * ``search()``  -- the migration runs as a side effect of the (mocked, external)
    embedding provider's ``get_embedding``, which executes in the parallel section
    AFTER the entry snapshot but BEFORE main-thread hydration.
  * ``get_point()`` / ``scroll_points()`` -- a thin FSV subclass advances the real
    on-disk state right after the genuine (SHARDED_JSON) initial gate returns,
    simulating a concurrent migration completing before the legacy read/scan. The
    resolver is never mocked -- it returns the truthful layout at every call; only
    the physical on-disk state advances between check and hydration.

Real filesystem, real SQLite (via the real ChunkStore / consolidation), real HNSW.
"""

from pathlib import Path
from typing import List
from unittest.mock import Mock

import numpy as np
import pytest

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
    # Distinct, well-separated unit vectors so HNSW self-match is unambiguous.
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


class TestSearchLayoutReResolveRace:
    def test_search_survives_concurrent_migration_between_snapshot_and_hydration(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store, vectors = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")

        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        fired = {"done": False}

        def _embed_with_concurrent_migration(_query: str, **_kwargs) -> List[float]:
            # Runs in search()'s parallel section: AFTER the entry layout snapshot,
            # BEFORE main-thread hydration. Simulate a server-mode migration
            # completing in that exact window: chunks.db built + discriminator
            # flipped + legacy vector_*.json / id_index.bin deleted.
            if not fired["done"]:
                fired["done"] = True
                consolidate_collection_in_place(collection_path)
            return vectors[0].tolist()  # type: ignore[no-any-return]

        provider = Mock()
        provider.get_embedding.side_effect = _embed_with_concurrent_migration

        results = store.search(
            query="race query",
            embedding_provider=provider,
            collection_name="test_coll",
            limit=5,
        )

        # Migration completed mid-search: legacy files are gone, chunks.db is
        # authoritative. Without the re-resolve fix, the cached SHARDED_JSON
        # branch reads deleted JSON and returns []. With the fix, hydration
        # re-resolves to CHUNKS_DB and returns the correct rows.
        assert fired["done"] is True
        # Discriminator committed + legacy deleted == fully migrated CHUNKS_DB.
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))
        assert len(results) > 0, "reader returned empty despite valid chunks.db"
        assert results[0]["id"] == "vec_0"
        assert results[0]["score"] > 0.99
        assert results[0]["payload"]["path"] == "file_0.py"

    def test_search_with_filter_survives_concurrent_migration(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store, vectors = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")

        fired = {"done": False}

        def _embed(_query: str, **_kwargs) -> List[float]:
            if not fired["done"]:
                fired["done"] = True
                consolidate_collection_in_place(collection_path)
            return vectors[0].tolist()  # type: ignore[no-any-return]

        provider = Mock()
        provider.get_embedding.side_effect = _embed

        # Case B (filtered) hydration branch must also re-resolve.
        results = store.search(
            query="race query",
            embedding_provider=provider,
            collection_name="test_coll",
            limit=5,
            filter_conditions={"language": "python"},
        )

        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert len(results) > 0
        assert results[0]["id"] == "vec_0"


class _MigrationInjectingStore(FilesystemVectorStore):
    """Advances the REAL on-disk state to a fully-migrated CHUNKS_DB collection
    the first time the genuine ``_is_chunks_db_collection`` gate resolves
    SHARDED_JSON -- simulating a concurrent migration that completes in the window
    between that gate and the subsequent legacy hydration. The resolver is never
    mocked; ``super()`` returns the truthful layout, we only advance physical
    state afterward.
    """

    def _is_chunks_db_collection(self, collection_name, collection_path):  # type: ignore[override]
        result = super()._is_chunks_db_collection(collection_name, collection_path)
        if not result and not getattr(self, "_race_fired", False):
            self._race_fired = True
            consolidate_collection_in_place(Path(collection_path))
        return result


class TestGetPointLayoutReResolveRace:
    def test_get_point_survives_concurrent_migration(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        # Build the sharded collection with a plain store, then re-open with the
        # injecting subclass so the race fires on the get_point() gate.
        plain, vectors = _build_sharded_collection(base_path, "test_coll")
        collection_path = plain._get_collection_path("test_coll")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        store = _MigrationInjectingStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        record = store.get_point("vec_3", "test_coll")

        assert getattr(store, "_race_fired", False) is True
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert record is not None, "get_point returned None despite valid chunks.db"
        assert record["id"] == "vec_3"
        assert record["payload"]["path"] == "file_3.py"
        assert len(record["vector"]) == VECTOR_SIZE


class TestScrollPointsLayoutReResolveRace:
    def test_scroll_points_survives_concurrent_migration(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        plain, _vectors = _build_sharded_collection(base_path, "test_coll")
        collection_path = plain._get_collection_path("test_coll")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        store = _MigrationInjectingStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        # No path filter -> exercises the rglob safety-valve branch, whose scan
        # would find zero vector_*.json files post-migration without the fix.
        points, _next = store.scroll_points(
            collection_name="test_coll", limit=100, with_payload=True
        )

        assert getattr(store, "_race_fired", False) is True
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert len(points) == NUM_POINTS, "scroll returned empty despite chunks.db"
        ids = {p["id"] for p in points}
        assert ids == {f"vec_{i}" for i in range(NUM_POINTS)}


class TestSteadyStateReadsUnchanged:
    """Regression: a permanently-SHARDED_JSON collection and a permanently-
    CHUNKS_DB collection both still read correctly and identically -- the cheap
    re-resolve must never break the steady-state paths.
    """

    def test_permanent_sharded_json_reads_correctly(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store, vectors = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        provider = Mock()
        provider.get_embedding.return_value = vectors[2].tolist()
        results = store.search(
            query="q",
            embedding_provider=provider,
            collection_name="test_coll",
            limit=3,
        )
        assert len(results) > 0
        assert results[0]["id"] == "vec_2"

        record = store.get_point("vec_5", "test_coll")
        assert record is not None
        assert record["id"] == "vec_5"

        points, _ = store.scroll_points("test_coll", limit=100)
        assert {p["id"] for p in points} == {f"vec_{i}" for i in range(NUM_POINTS)}

    def test_permanent_chunks_db_reads_correctly(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store, vectors = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")

        # Fully migrate up front -> permanent, steady-state CHUNKS_DB.
        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        # Fresh instance (no in-session build intent) -> pure resolver-driven reads.
        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        provider = Mock()
        provider.get_embedding.return_value = vectors[4].tolist()
        results = reader.search(
            query="q",
            embedding_provider=provider,
            collection_name="test_coll",
            limit=3,
        )
        assert len(results) > 0
        assert results[0]["id"] == "vec_4"

        record = reader.get_point("vec_1", "test_coll")
        assert record is not None
        assert record["id"] == "vec_1"

        points, _ = reader.scroll_points("test_coll", limit=100)
        assert {p["id"] for p in points} == {f"vec_{i}" for i in range(NUM_POINTS)}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
