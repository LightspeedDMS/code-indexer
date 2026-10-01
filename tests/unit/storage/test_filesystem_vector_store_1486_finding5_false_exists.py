"""Bug #1486 Codex Finding 5 (HIGH, STILL-OPEN) -- search() silently returns
empty on a concurrent flip+delete that ``Path.exists()`` masks.

The sibling file ``test_filesystem_vector_store_1486_finding5_toctou.py`` already
covers the variant where a concurrent migration deletes a legacy ``vector_*.json``
in the window between the reader's ``exists()`` check and its ``open()`` -- there
the legacy ``open()`` raises ``FileNotFoundError`` and the existing
``except FileNotFoundError -> re-resolve`` handler absorbs it.

This file covers the NARROWER, still-open race Codex reproduced as
``SEARCH_FALSE_EXISTS layout chunks_db result_count 0``:

  * ``search()`` re-resolves the layout at hydration (SHARDED_JSON -- flip not yet
    landed) and takes the legacy hydration branch.
  * A concurrent server-mode fleet migration flips the discriminator to CHUNKS_DB
    AND deletes every legacy file in the window BEFORE the legacy branch's
    ``Path.exists()`` filter runs.
  * ``exists()`` therefore returns ``False`` -> the file is SKIPPED -> NO
    ``FileNotFoundError`` is ever raised -> the existing ``except FileNotFoundError``
    handler never runs -> ``search()`` silently returns EMPTY/partial results even
    though a fully-valid ``chunks.db`` exists.

The correctness principle (per Codex): a SHARDED_JSON -> CHUNKS_DB flip is the only
meaningful transition, and it must be detected by RE-RESOLVING the committed
discriminator after the legacy hydration completes -- NEVER inferred from whether a
legacy file happened to still exist. ``exists()`` cannot be the race discriminator.

Reproduced DETERMINISTICALLY (no sleeps, no mocking of the resolver's own logic) by
wrapping every id-index Path so its FIRST ``.exists()`` fires a genuine
``consolidate_collection_in_place`` (real chunks.db build + discriminator flip +
legacy vector_*.json / id_index.bin delete) and then reports the REAL, post-delete
existence (``False``) -- exactly the SEARCH_FALSE_EXISTS state.

Real filesystem, real SQLite (via the real ChunkStore / consolidation), real HNSW.
"""

from pathlib import Path
from typing import Callable, Dict, List, Optional
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
    rng = np.random.default_rng(14865)
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


class _MigrationThenFalseExistsPath:
    """A path-like whose FIRST ``.exists()`` fires the migration callback and then
    reports the REAL, post-migration existence (``False`` -- the migration deleted
    it). The caller therefore SKIPS the file with NO ``open()`` attempt and NO
    ``FileNotFoundError`` -- reproducing Codex's SEARCH_FALSE_EXISTS. Usable
    directly in ``open()`` via ``__fspath__`` should any caller still try.
    """

    def __init__(self, real: Path, fire: Callable[[], None]) -> None:
        self._real = real
        self._fire = fire

    def exists(self) -> bool:
        self._fire()
        # Report the genuine post-migration truth: the legacy file is gone.
        return self._real.exists()

    def __fspath__(self) -> str:
        return str(self._real)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_MigrationThenFalseExistsPath({self._real!r})"


class _MigrationThenFalseExistsStore(FilesystemVectorStore):
    """Wraps every id-index Path so the first ``.exists()`` on the main thread
    advances the REAL on-disk state to a fully-migrated CHUNKS_DB collection
    (chunks.db built + discriminator flipped + legacy vector_*.json / id_index.bin
    deleted) and then reports the file as ABSENT -- modelling a concurrent
    migration that lands in the window before the legacy branch's exists() filter,
    so exists() returns False and no exception is raised. The resolver is never
    mocked; only physical state advances between the hydration resolve and the
    exists() check.
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
            pid: _MigrationThenFalseExistsPath(p, fire)  # type: ignore[misc]
            for pid, p in real.items()
        }


class TestSearchFalseExistsFinding5:
    """The still-open Finding 5 case: exists() returns False, no exception."""

    def test_search_unfiltered_flip_delete_masked_by_false_exists(
        self, tmp_path: Path
    ) -> None:
        """Case A (unfiltered top-limit): exists()-False must NOT yield empty."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        _plain, vectors = _build_sharded_collection(base_path, "c")
        collection_path = _plain._get_collection_path("c")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        store = _MigrationThenFalseExistsStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        provider = Mock()
        provider.get_embedding.return_value = vectors[0].tolist()

        # Hydration resolve reads SHARDED_JSON (migration not yet fired). The
        # legacy Case-A exists() filter then fires the migration and returns
        # False for every legacy file -- no FileNotFoundError is raised.
        results = store.search(
            query="q",
            embedding_provider=provider,
            collection_name="c",
            limit=5,
        )

        assert getattr(store, "_race_fired", False) is True
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))
        assert len(results) > 0, (
            "search returned empty despite a fully-valid chunks.db -- "
            "exists()-False silently masked the flip (SEARCH_FALSE_EXISTS)"
        )
        assert results[0]["id"] == "vec_0"
        assert results[0]["score"] > 0.99
        assert results[0]["payload"]["path"] == "file_0.py"

    def test_search_filtered_flip_delete_masked_by_false_exists(
        self, tmp_path: Path
    ) -> None:
        """Case B (filtered/overfetch): exists()-False must NOT yield empty."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        _plain, vectors = _build_sharded_collection(base_path, "c")
        collection_path = _plain._get_collection_path("c")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        store = _MigrationThenFalseExistsStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        provider = Mock()
        provider.get_embedding.return_value = vectors[0].tolist()

        results = store.search(
            query="q",
            embedding_provider=provider,
            collection_name="c",
            limit=5,
            filter_conditions={"language": "python"},
        )

        assert getattr(store, "_race_fired", False) is True
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))
        assert len(results) > 0, (
            "filtered search returned empty despite a fully-valid chunks.db"
        )
        assert results[0]["id"] == "vec_0"
        assert results[0]["payload"]["language"] == "python"


class TestSearchSteadyStateRegression:
    """The fix must not perturb permanently-single-layout collections."""

    def test_permanent_sharded_json_search_correct(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store, vectors = _build_sharded_collection(base_path, "c")
        collection_path = store._get_collection_path("c")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        provider = Mock()
        provider.get_embedding.return_value = vectors[2].tolist()
        results = store.search(
            query="q",
            embedding_provider=provider,
            collection_name="c",
            limit=3,
        )
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON
        assert len(results) > 0
        assert results[0]["id"] == "vec_2"
        assert results[0]["score"] > 0.99

    def test_permanent_chunks_db_search_correct(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        built, vectors = _build_sharded_collection(base_path, "c")
        collection_path = built._get_collection_path("c")
        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        provider = Mock()
        provider.get_embedding.return_value = vectors[5].tolist()
        results = reader.search(
            query="q",
            embedding_provider=provider,
            collection_name="c",
            limit=3,
        )
        assert len(results) > 0
        assert results[0]["id"] == "vec_5"
        assert results[0]["score"] > 0.99


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
