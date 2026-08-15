"""Bug #1575 Part B: CHUNKS_DB scroll pagination must be keyset-based, never a
per-page full ``sorted(chunk_store.all_point_ids())`` sort.

``_scroll_points_chunks_db`` previously called ``sorted(chunk_store.all_point_ids())``
on EVERY call/page. For N points and page size L, that is ~(N/L) full O(N log N)
Python sorts of N ids across one scroll -- a cost that grows with collection
size regardless of how few rows a given page actually returns.

These tests prove (not merely assert-and-hope):
  1. ``ChunkStore.all_point_ids()`` is never called while paginating with the
     self-describing cursor this method itself always mints (operation-count
     evidence the O(N) enumeration was eliminated from the hot path).
  2. Every ``ChunkStore.point_ids_after()`` call is bounded by the page limit,
     never by the collection's total row count (the genuine "keyset, not a
     full sort" proof AC22 requires).
  3. The cursor contract survives: a deleted cursor id, non-matching
     intervening rows, a cursor minted under the OTHER layout, and a
     mid-pagination layout flip.
  4. A concrete cost-difference: total rows read across a full multi-page
     scroll is bounded by (pages * limit), not by (pages * N).
"""

from pathlib import Path
from typing import Dict, List
from unittest.mock import patch

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
from code_indexer.storage.sqlite_chunk_store import ChunkStore

VECTOR_SIZE = 32


def _make_points(n: int) -> List[Dict]:
    rng = np.random.default_rng(1575)
    points = []
    for i in range(n):
        v = rng.standard_normal(VECTOR_SIZE)
        v[i % VECTOR_SIZE] += 25.0
        points.append(
            {
                "id": f"p{i:05d}",
                "vector": v.astype(np.float64).tolist(),
                "payload": {"path": f"file_{i}.py", "language": "python"},
            }
        )
    return points


def _build_chunks_db_collection(
    base_path: Path, collection_name: str, n: int
) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=True
    )
    store.create_collection(collection_name, vector_size=VECTOR_SIZE)
    store.begin_indexing(collection_name)
    store.upsert_points(collection_name, _make_points(n))
    store.end_indexing(collection_name)
    return store


class TestChunksDbNeverFullySortsAllIdsPerPage:
    """Operation-count evidence (AC22): ``all_point_ids()`` must not be
    called on the hot pagination path once a self-describing cursor is in
    play (the ONLY cursor shape this codebase's scroll_points itself ever
    mints going forward)."""

    def test_all_point_ids_not_called_across_stable_scroll(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        n = 500
        store = _build_chunks_db_collection(base_path, "coll", n)
        assert (
            resolve_chunk_layout(store._get_collection_path("coll"))
            == ChunkLayout.CHUNKS_DB
        )

        collected: List[str] = []
        cursor = None
        guard = 0
        with patch.object(ChunkStore, "all_point_ids", autospec=True) as mock_all_ids:
            while True:
                guard += 1
                assert guard <= n + 2, "pagination did not terminate"
                page, cursor = store.scroll_points(
                    collection_name="coll", limit=17, offset=cursor
                )
                collected.extend(p["id"] for p in page)
                if cursor is None:
                    break

        assert mock_all_ids.call_count == 0, (
            f"all_point_ids() was called {mock_all_ids.call_count} times "
            f"during a stable-cursor scroll -- the O(N) full enumeration "
            f"was NOT eliminated from the per-page hot path"
        )
        assert len(collected) == len(set(collected))
        assert set(collected) == {f"p{i:05d}" for i in range(n)}

    def test_point_ids_after_batch_size_bounded_by_limit_not_collection_size(
        self, tmp_path: Path
    ) -> None:
        """Genuine keyset-pagination proof: every ``point_ids_after()`` call's
        ``limit`` argument stays small and constant, never proportional to
        the collection's total point count."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        n = 4000
        store = _build_chunks_db_collection(base_path, "coll", n)

        page_limit = 25
        observed_limits: List[int] = []
        original_point_ids_after = ChunkStore.point_ids_after

        def _spy(self, cursor, limit):  # type: ignore[no-untyped-def]
            observed_limits.append(limit)
            return original_point_ids_after(self, cursor, limit)

        cursor = None
        guard = 0
        with patch.object(ChunkStore, "point_ids_after", _spy):
            while True:
                guard += 1
                assert guard <= (n // page_limit) + 2
                page, cursor = store.scroll_points(
                    collection_name="coll", limit=page_limit, offset=cursor
                )
                if cursor is None:
                    break

        assert observed_limits, "point_ids_after was never called"
        # The whole point of keyset pagination: every call's limit is bounded
        # by the page size (plus the small existence-check probe of 1),
        # NEVER anywhere close to the collection's total row count (4000).
        assert max(observed_limits) <= page_limit, (
            f"point_ids_after was called with a limit up to "
            f"{max(observed_limits)}, expected <= {page_limit} "
            f"(collection has {n} points) -- this proves the fetch cost is "
            f"NOT bounded by the page size"
        )

    def test_total_rows_read_bounded_by_pages_times_limit_not_by_n(
        self, tmp_path: Path
    ) -> None:
        """Concrete before/after cost-difference proof (AC19-style, for the
        CHUNKS_DB layout): sum the total id-rows fetched via
        ``point_ids_after()`` across a full multi-page scroll and compare it
        against what the OLD ``sorted(all_point_ids())``-per-page code would
        have cost (N rows read on EVERY page).
        """
        base_path = tmp_path / "index"
        base_path.mkdir()
        n = 3000
        store = _build_chunks_db_collection(base_path, "coll", n)

        page_limit = 50
        total_rows_fetched_new = 0
        original_point_ids_after = ChunkStore.point_ids_after

        def _counting_spy(self, cursor, limit):  # type: ignore[no-untyped-def]
            nonlocal total_rows_fetched_new
            result = original_point_ids_after(self, cursor, limit)
            total_rows_fetched_new += len(result)
            return result

        cursor = None
        guard = 0
        pages = 0
        with patch.object(ChunkStore, "point_ids_after", _counting_spy):
            while True:
                guard += 1
                assert guard <= (n // page_limit) + 5
                page, cursor = store.scroll_points(
                    collection_name="coll", limit=page_limit, offset=cursor
                )
                pages += 1
                if cursor is None:
                    break

        # OLD behavior: sorted(all_point_ids()) reads ALL N ids on EVERY page.
        old_cost = pages * n
        # NEW behavior: bounded by (pages * page_limit) plus a small constant
        # of existence-check probes (one extra id-row per full page).
        assert total_rows_fetched_new <= pages * (page_limit + 1), (
            f"new code fetched {total_rows_fetched_new} id-rows across "
            f"{pages} pages -- expected <= {pages * (page_limit + 1)}"
        )
        assert total_rows_fetched_new < old_cost, (
            f"new code ({total_rows_fetched_new} rows) did not improve over "
            f"the old full-sort cost ({old_cost} rows) at N={n}"
        )
        # Concretely: the new cost is at least an order of magnitude smaller.
        assert old_cost / max(total_rows_fetched_new, 1) > 10


class TestChunksDbDeletedCursorAndInterveningFilterRows:
    """Two of the four MANDATORY discriminating scenarios (Bug #1575 issue
    text): a deleted cursor id, and real non-matching intervening rows."""

    def test_cursor_pointing_to_deleted_id_resumes_correctly(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_chunks_db_collection(base_path, "coll", 10)

        page1, cursor = store.scroll_points(collection_name="coll", limit=3)
        assert cursor is not None
        deleted_id = page1[-1]["id"]  # this is exactly what `cursor` names

        # Delete the point the cursor points at BEFORE requesting page 2.
        store.delete_points("coll", [deleted_id])

        page2, cursor2 = store.scroll_points(
            collection_name="coll", limit=3, offset=cursor
        )
        ids2 = [p["id"] for p in page2]
        assert deleted_id not in ids2
        # Must resume at the next greater id, never crash, never duplicate
        # anything from page1, never silently restart at page 1.
        assert not (set(ids2) & {p["id"] for p in page1})
        assert len(ids2) == 3

    def test_intervening_non_matching_rows_do_not_break_cursor_continuity(
        self, tmp_path: Path
    ) -> None:
        """Every 3rd point is a real, stored, non-matching row (language
        "text" instead of "python") -- the filter must skip over these
        intervening rows across MULTIPLE page boundaries (limit=4, so the
        1-in-3 non-matching ratio guarantees several pages contain a mix)
        without ever dropping a later match or duplicating an earlier one.
        """
        base_path = tmp_path / "index"
        base_path.mkdir()
        n = 30
        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=True
        )
        store.create_collection("coll", vector_size=VECTOR_SIZE)
        store.begin_indexing("coll")
        points = _make_points(n)
        matching_ids = set()
        for i, point in enumerate(points):
            if i % 3 == 0:
                point["payload"]["language"] = "text"  # non-matching
            else:
                matching_ids.add(point["id"])
        store.upsert_points("coll", points)
        store.end_indexing("coll")
        assert (
            resolve_chunk_layout(store._get_collection_path("coll"))
            == ChunkLayout.CHUNKS_DB
        )
        assert matching_ids and matching_ids != {f"p{i:05d}" for i in range(n)}, (
            "fixture must contain a genuine mix of matching/non-matching rows"
        )

        collected: List[str] = []
        cursor = None
        guard = 0
        while True:
            guard += 1
            assert guard <= n + 5
            page, cursor = store.scroll_points(
                collection_name="coll",
                limit=4,
                offset=cursor,
                filter_conditions={
                    "must": [{"key": "language", "match": {"value": "python"}}]
                },
            )
            collected.extend(p["id"] for p in page)
            if cursor is None:
                break

        assert len(collected) == len(set(collected)), (
            f"duplicate ids across pages with intervening non-matching rows: "
            f"{collected}"
        )
        assert set(collected) == matching_ids, (
            f"filter+cursor continuity dropped or leaked rows: "
            f"got {sorted(set(collected))}, expected {sorted(matching_ids)}"
        )


class TestChunksDbLayoutCrossingCursorDiscriminators:
    """The remaining two of the four MANDATORY discriminating scenarios: a
    cursor minted under the OTHER layout, and a mid-pagination layout flip."""

    def test_sharded_json_minted_cursor_honored_after_flip_to_chunks_db(
        self, tmp_path: Path
    ) -> None:
        """A cursor generated under SHARDED_JSON, presented to a collection
        that has since flipped to CHUNKS_DB, must resume correctly (not
        restart, not crash) -- the existing layout-independent cursor
        contract, preserved by the Part B rewrite."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        store.create_collection("coll", vector_size=VECTOR_SIZE)
        store.begin_indexing("coll")
        store.upsert_points("coll", _make_points(9))
        store.end_indexing("coll")
        collection_path = store._get_collection_path("coll")
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        page1, cursor = store.scroll_points(
            collection_name="coll", limit=3, with_payload=True
        )
        assert cursor is not None

        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        collected = [p["id"] for p in page1]
        guard = 0
        while cursor is not None:
            guard += 1
            assert guard <= 10
            page, cursor = store.scroll_points(
                collection_name="coll", limit=3, offset=cursor
            )
            collected.extend(p["id"] for p in page)

        assert len(collected) == len(set(collected))
        assert set(collected) == {f"p{i:05d}" for i in range(9)}

    def test_mid_pagination_layout_flip_does_not_corrupt_results(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        store.create_collection("coll", vector_size=VECTOR_SIZE)
        store.begin_indexing("coll")
        store.upsert_points("coll", _make_points(12))
        store.end_indexing("coll")
        collection_path = store._get_collection_path("coll")

        collected: List[str] = []
        page, cursor = store.scroll_points(collection_name="coll", limit=4)
        collected.extend(p["id"] for p in page)
        assert cursor is not None

        # Flip layout mid-scroll.
        consolidate_collection_in_place(collection_path)

        guard = 0
        while cursor is not None:
            guard += 1
            assert guard <= 10
            page, cursor = store.scroll_points(
                collection_name="coll", limit=4, offset=cursor
            )
            collected.extend(p["id"] for p in page)

        assert len(collected) == len(set(collected)), (
            f"duplicates across mid-pagination flip: {collected}"
        )
        assert set(collected) == {f"p{i:05d}" for i in range(12)}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
