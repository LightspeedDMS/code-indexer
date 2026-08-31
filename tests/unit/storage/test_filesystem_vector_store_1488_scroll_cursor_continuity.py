"""Codex Finding C (Bug #1488): ``scroll_points()`` must not return DUPLICATE
rows (and silently DROP others) when a pagination cursor issued under one chunk
layout is honored after a concurrent layout flip.

Root cause reproduced here: the cursor FORMAT used to be layout-dependent --
the SHARDED_JSON branch returned a filesystem-PATH offset, while the CHUNKS_DB
branch expected a POINT-ID offset and, given an unrecognized (path-format)
cursor, SILENTLY restarted at offset 0. So:

  1. A client reads page 1 while the collection is SHARDED_JSON -> gets a
     path-format ``next`` cursor.
  2. A concurrent server-mode fleet migration flips the collection to
     CHUNKS_DB and deletes the legacy ``vector_*.json`` files.
  3. The client requests page 2 passing that path-format cursor -> the
     CHUNKS_DB scroll does not recognize it -> silently restarts at 0 ->
     returns PAGE 1 AGAIN (duplicate rows) AND never returns page 2's rows
     (data effectively missing from the paginated view).

The fix makes the cursor LAYOUT-INDEPENDENT (a stable point-id-based cursor
honored by both layouts, with a legacy path-format cursor translated, never
silently reset -- Messi #13). These tests reproduce the exact sequence with
REAL filesystem + REAL SQLite (via the real ``consolidate_collection_in_place``),
deterministically, no sleeps, no mocking of the store's own logic.
"""

from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    _SCROLL_CURSOR_PREFIX,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)

VECTOR_SIZE = 64
NUM_POINTS = 9  # 3 full pages at limit=3


def _make_points() -> List[Dict]:
    rng = np.random.default_rng(1488)
    points = []
    for i in range(NUM_POINTS):
        v = rng.standard_normal(VECTOR_SIZE)
        v[i % VECTOR_SIZE] += 25.0
        points.append(
            {
                # Zero-padded so lexicographic order == numeric order, and no
                # '/' so the sharded filename token == the real point-id.
                "id": f"p{i:03d}",
                "vector": v.astype(np.float64).tolist(),
                "payload": {"path": f"file_{i}.py", "language": "python"},
            }
        )
    return points


def _build_sharded_collection(
    base_path: Path, collection_name: str
) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(collection_name, vector_size=VECTOR_SIZE)
    store.begin_indexing(collection_name)
    store.upsert_points(collection_name, _make_points())
    store.end_indexing(collection_name)
    return store


ALL_IDS = {f"p{i:03d}" for i in range(NUM_POINTS)}


class TestScrollCursorLayoutFlipContinuity:
    """Codex Finding C: a cursor issued under SHARDED_JSON must resume correctly
    after a mid-pagination flip to CHUNKS_DB -- no duplicates, no dropped rows.
    """

    def test_cursor_survives_layout_flip_no_dup_no_gap(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        collected: List[str] = []

        # Page 1 under SHARDED_JSON.
        page1, cursor = store.scroll_points(
            collection_name="test_coll", limit=3, with_payload=True
        )
        assert len(page1) == 3
        assert cursor is not None, "page 1 must yield a next cursor"
        collected.extend(p["id"] for p in page1)

        # Concurrent server-mode migration completes between pages: chunks.db
        # built + discriminator flipped + legacy vector_*.json deleted.
        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))

        # Remaining pages under CHUNKS_DB, resuming from the SHARDED cursor.
        guard = 0
        while cursor is not None:
            guard += 1
            assert guard <= NUM_POINTS + 2, "pagination did not terminate"
            page, cursor = store.scroll_points(
                collection_name="test_coll",
                limit=3,
                with_payload=True,
                offset=cursor,
            )
            collected.extend(p["id"] for p in page)

        # No duplicates: the path-format cursor must NOT have silently reset the
        # CHUNKS_DB scroll to offset 0 (which would re-emit page 1).
        assert len(collected) == len(set(collected)), (
            f"duplicate ids across the layout flip: {collected}"
        )
        # No dropped rows: the paginated union must equal the full set.
        assert set(collected) == ALL_IDS, (
            f"paginated view lost rows across the flip: got {sorted(set(collected))}"
        )


class TestStableLayoutPaginationRegression:
    """Regression: normal multi-page scroll within a stable layout returns the
    full set, no duplicates -- for BOTH layouts.
    """

    def _paginate_all(self, store: FilesystemVectorStore) -> List[str]:
        collected: List[str] = []
        cursor = None
        guard = 0
        while True:
            guard += 1
            assert guard <= NUM_POINTS + 2, "pagination did not terminate"
            page, cursor = store.scroll_points(
                collection_name="test_coll",
                limit=2,
                with_payload=True,
                offset=cursor,
            )
            collected.extend(p["id"] for p in page)
            if cursor is None:
                break
        return collected

    def test_stable_sharded_pagination(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_sharded_collection(base_path, "test_coll")
        assert (
            resolve_chunk_layout(store._get_collection_path("test_coll"))
            == ChunkLayout.SHARDED_JSON
        )
        collected = self._paginate_all(store)
        assert len(collected) == len(set(collected)), collected
        assert set(collected) == ALL_IDS

    def test_stable_chunks_db_pagination(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")
        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        collected = self._paginate_all(reader)
        assert len(collected) == len(set(collected)), collected
        assert set(collected) == ALL_IDS


class TestUnrecognizedCursorNeverSilentReset:
    """Messi #13: an unrecognized/legacy path-format cursor must be resolved to
    the correct resume position (or fail loud) -- NEVER silently restart at 0.
    """

    def test_legacy_path_cursor_translated_on_chunks_db(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")
        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        # A legacy path-format cursor pointing at the 3rd point (p002). The
        # buggy code did not recognize it in the CHUNKS_DB branch and restarted
        # at 0, re-emitting p000..p002. Correct behaviour resumes AFTER p002.
        legacy_cursor = str(collection_path / "ab" / "cd" / "vector_p002.json")
        page, _cursor = reader.scroll_points(
            collection_name="test_coll",
            limit=3,
            with_payload=True,
            offset=legacy_cursor,
        )
        ids = [p["id"] for p in page]
        assert ids == ["p003", "p004", "p005"], (
            f"legacy path cursor silently reset instead of resuming: {ids}"
        )

    def test_pointid_cursor_past_end_returns_empty_not_page1(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")
        consolidate_collection_in_place(collection_path)

        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        # A VALID self-describing cursor whose id sorts after every stored id
        # must yield an empty page with no next-cursor -- never a silent restart
        # at page 1.
        page, cursor = reader.scroll_points(
            collection_name="test_coll",
            limit=3,
            with_payload=True,
            offset=_SCROLL_CURSOR_PREFIX + "zzz_past_end",
        )
        assert page == []
        assert cursor is None

    def test_garbage_cursor_fails_loud_not_page1(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_sharded_collection(base_path, "test_coll")
        collection_path = store._get_collection_path("test_coll")
        consolidate_collection_in_place(collection_path)

        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        # A genuinely unrecognized cursor (neither self-describing nor a legacy
        # vector_<token>.json path) must FAIL LOUD (Messi #13), never silently
        # mis-bisect and re-emit page 1.
        with pytest.raises(ValueError):
            reader.scroll_points(
                collection_name="test_coll",
                limit=3,
                with_payload=True,
                offset="zzz_past_end",
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
