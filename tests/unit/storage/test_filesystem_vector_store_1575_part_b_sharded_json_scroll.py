"""Bug #1575 Part B: SHARDED_JSON scroll pagination must build the
``id_to_file`` enumeration ONCE per scroll session, never per page.

The legacy branch of ``scroll_points()`` used to rebuild the complete
``id_to_file`` map by ``rglob``-ing the collection and opening/parsing EVERY
vector JSON file on EVERY call/page -- approximately quadratic in file count
and pathological on NFS-backed storage.

These tests prove, via pure black-box observation (real wall-clock timing --
never by mocking/patching any of the store's own methods or dependencies):
  1. The wall-clock cost of a CONTINUATION page (offset given -- the case
     that used to pay the full O(N) rebuild on every call) does not scale
     proportionally with the collection's total size -- a >=15x size
     difference between two real collections produces nowhere near a 15x
     time difference on a continuation page (cost-difference proof, AC22).
  2. The session cache tolerates staleness safely: a point deleted between
     pages, non-matching intervening rows under a filter, and a
     mid-pagination layout flip to CHUNKS_DB all still produce correct
     results -- no crash, no duplicate, no dropped row.
  3. A point ADDED between pages of an in-progress scroll is observed by
     the continuation page (Codex review Finding 1), and a DETERMINISTIC
     call-count proof (Codex review Finding 2, via ``sys.setprofile`` --
     never mocking/patching anything) shows the O(N) rebuild is not
     repeated across continuation pages, regardless of collection size.
"""

import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

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

VECTOR_SIZE = 32
POINT_ID_WIDTH = 5
RNG_SEED = 1575
VECTOR_BOOST = 25.0  # nudges one dimension so vectors are non-degenerate
LAST_PAGE_POINT_INDEX = -1

# Cost-difference proof sizing (TestShardedJsonContinuationPageCostDecoupledFromCollectionSize).
SMALL_COLLECTION_POINT_COUNT = 400
LARGE_COLLECTION_POINT_COUNT = 8000  # 20x SMALL_COLLECTION_POINT_COUNT
COST_PROOF_PAGE_LIMIT = 20
# Guards a near-zero denominator on a very fast machine; not a pass/fail
# threshold on its own -- only used to keep the ratio computation stable.
TIME_RATIO_FLOOR_SECONDS = 1e-4
# The timed continuation page must cost far less than proportional to the
# size ratio -- an arbitrary-but-generous half of the raw size ratio still
# clearly discriminates "rebuilt from scratch" (ratio ~= size ratio) from
# "served from cache" (ratio ~= 1).
COST_PROOF_RATIO_TOLERANCE_FACTOR = 2

# Staleness-safety fixture sizing (TestShardedJsonSessionCacheStalenessSafety).
DELETED_CURSOR_COLLECTION_POINT_COUNT = 12
DELETED_CURSOR_PAGE_LIMIT = 4
EXPECTED_SINGLE_DELETION_COUNT = 1
LAYOUT_FLIP_COLLECTION_POINT_COUNT = 12
LAYOUT_FLIP_PAGE_LIMIT = 4
FILTER_CONTINUITY_COLLECTION_POINT_COUNT = 24
FILTER_CONTINUITY_PAGE_LIMIT = 3
NON_MATCHING_ROW_STRIDE = 3  # every Nth point is a non-matching row
# Safety bound on pagination loops -- must exceed the max possible page
# count (collection_size / page_limit) so a genuine infinite-loop bug fails
# the test rather than hanging; not a correctness threshold itself.
PAGINATION_GUARD_MARGIN = 5

# Fresh-scroll fixture sizing (test_each_new_scroll_rebuilds_its_own_fresh_view).
FRESH_SCROLL_INITIAL_POINT_COUNT = 5
FRESH_SCROLL_PAGE_LIMIT = 100
FRESH_SCROLL_NEW_POINT_INDEX = 99
NEW_POINT_VECTOR_FILL_VALUE = 0.1
SINGLE_UPSERTED_POINT_COUNT = 1

# Mid-scroll-write cache-invalidation fixture sizing
# (TestShardedJsonScrollCacheObservesWritesBetweenPages, Codex Finding 1).
MID_SCROLL_WRITE_INITIAL_POINT_COUNT = 4
MID_SCROLL_WRITE_PAGE_LIMIT = 1  # forces a continuation cursor after page 1
MID_SCROLL_WRITE_NEW_POINT_INDEX = 99  # sorts after every initial point-id

# Deterministic rebuild-call-count proof sizing
# (TestShardedJsonContinuationPageRglobCallCountDeterministic, Codex Finding 2).
DETERMINISTIC_PROOF_SMALL_POINT_COUNT = 50
DETERMINISTIC_PROOF_LARGE_POINT_COUNT = 2000
DETERMINISTIC_PROOF_PAGE_LIMIT = 10
# The O(N) rebuild must not run again across ANY continuation page once
# the session cache holds a fresh enumeration.
EXPECTED_REBUILD_CALLS_ACROSS_CONTINUATION_PAGES = 0


def _point_id(i: int) -> str:
    return f"p{i:0{POINT_ID_WIDTH}d}"


FRESH_SCROLL_NEW_POINT_ID = _point_id(FRESH_SCROLL_NEW_POINT_INDEX)
MID_SCROLL_WRITE_NEW_POINT_ID = _point_id(MID_SCROLL_WRITE_NEW_POINT_INDEX)


def _make_points(n: int) -> List[Dict]:
    rng = np.random.default_rng(RNG_SEED)
    points = []
    for i in range(n):
        v = rng.standard_normal(VECTOR_SIZE)
        v[i % VECTOR_SIZE] += VECTOR_BOOST
        points.append(
            {
                "id": _point_id(i),
                "vector": v.astype(np.float64).tolist(),
                "payload": {"path": f"file_{i}.py", "language": "python"},
            }
        )
    return points


def _build_sharded_json_collection(
    base_path: Path, collection_name: str, n: int
) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    created = store.create_collection(collection_name, vector_size=VECTOR_SIZE)
    assert created is True, f"create_collection({collection_name!r}) failed"
    store.begin_indexing(collection_name)  # returns None: prep-only, no result to check
    upsert_result = store.upsert_points(collection_name, _make_points(n))
    assert upsert_result.get("status") == "ok"
    assert upsert_result.get("count") == n
    store.end_indexing(
        collection_name
    )  # returns None: finalize-only, no result to check
    return store


def _upsert_single_new_point(
    store: FilesystemVectorStore, collection_name: str, point_id: str, path: str
) -> None:
    """Add ONE new point to an already-built collection -- simulates a
    write landing between two scroll operations (or between two pages of
    the SAME open scroll session)."""
    store.begin_indexing(collection_name)
    upsert_result = store.upsert_points(
        collection_name,
        [
            {
                "id": point_id,
                "vector": [NEW_POINT_VECTOR_FILL_VALUE] * VECTOR_SIZE,
                "payload": {"path": path, "language": "python"},
            }
        ],
    )
    assert upsert_result.get("status") == "ok"
    assert upsert_result.get("count") == SINGLE_UPSERTED_POINT_COUNT
    store.end_indexing(collection_name)


def _time_continuation_page(
    store: FilesystemVectorStore, cursor: str, page_limit: int
) -> Tuple[List[Dict], float]:
    start = time.perf_counter()
    page, _ = store.scroll_points(
        collection_name="coll", limit=page_limit, offset=cursor
    )
    elapsed = time.perf_counter() - start
    return page, elapsed


class TestShardedJsonContinuationPageCostDecoupledFromCollectionSize:
    """Cost-difference proof (AC22): a CONTINUATION page's wall-clock cost
    must not scale with the collection's total size -- that is exactly the
    cost the per-page id_to_file rebuild used to impose."""

    def test_continuation_page_time_does_not_scale_with_collection_size(
        self, tmp_path: Path
    ) -> None:
        small_store = _build_sharded_json_collection(
            tmp_path / "small", "coll", SMALL_COLLECTION_POINT_COUNT
        )
        large_store = _build_sharded_json_collection(
            tmp_path / "large", "coll", LARGE_COLLECTION_POINT_COUNT
        )

        # Page 1 always rebuilds fresh (offset=None) for BOTH -- this cost is
        # expected to scale with N regardless, before and after this fix, and
        # is deliberately excluded from the timed comparison below.
        _, cursor_small = small_store.scroll_points(
            collection_name="coll", limit=COST_PROOF_PAGE_LIMIT
        )
        _, cursor_large = large_store.scroll_points(
            collection_name="coll", limit=COST_PROOF_PAGE_LIMIT
        )
        assert cursor_small is not None
        assert cursor_large is not None

        # Timed continuation page -- the case the OLD code paid a full O(N)
        # rebuild for on EVERY call, regardless of how few files changed.
        page_small, elapsed_small = _time_continuation_page(
            small_store, cursor_small, COST_PROOF_PAGE_LIMIT
        )
        page_large, elapsed_large = _time_continuation_page(
            large_store, cursor_large, COST_PROOF_PAGE_LIMIT
        )

        assert len(page_small) == COST_PROOF_PAGE_LIMIT
        assert len(page_large) == COST_PROOF_PAGE_LIMIT

        size_ratio = LARGE_COLLECTION_POINT_COUNT / SMALL_COLLECTION_POINT_COUNT
        time_ratio = elapsed_large / max(elapsed_small, TIME_RATIO_FLOOR_SECONDS)
        assert time_ratio < size_ratio / COST_PROOF_RATIO_TOLERANCE_FACTOR, (
            f"continuation-page cost scaled with collection size: "
            f"small(N={SMALL_COLLECTION_POINT_COUNT})={elapsed_small:.4f}s, "
            f"large(N={LARGE_COLLECTION_POINT_COUNT})={elapsed_large:.4f}s, "
            f"time_ratio={time_ratio:.2f} vs size_ratio={size_ratio:.2f} -- "
            f"expected the continuation page to be served from a cached "
            f"enumeration, not rebuilt from scratch"
        )

    def test_each_new_scroll_rebuilds_its_own_fresh_view(self, tmp_path: Path) -> None:
        """A NEW scroll (offset=None) must always rebuild fresh, so a second,
        independent scroll after a write sees the current state -- the
        session cache must not leak across unrelated scroll operations."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_sharded_json_collection(
            base_path, "coll", FRESH_SCROLL_INITIAL_POINT_COUNT
        )

        page1, _ = store.scroll_points(
            collection_name="coll", limit=FRESH_SCROLL_PAGE_LIMIT
        )
        assert len(page1) == FRESH_SCROLL_INITIAL_POINT_COUNT

        # Add a new point directly (simulating a write between two
        # unrelated scroll operations).
        _upsert_single_new_point(
            store, "coll", FRESH_SCROLL_NEW_POINT_ID, "new_file.py"
        )

        page2, _ = store.scroll_points(
            collection_name="coll", limit=FRESH_SCROLL_PAGE_LIMIT
        )
        ids2 = {p["id"] for p in page2}
        assert FRESH_SCROLL_NEW_POINT_ID in ids2, (
            "a fresh scroll (offset=None) must always see the current "
            "on-disk state, never a stale cached view from an earlier scroll"
        )


class TestShardedJsonScrollCacheObservesWritesBetweenPages:
    """Codex review Finding 1 (Bug #1575 Part B): a point added BETWEEN
    pages of an in-progress scroll must be observed by the continuation
    page. Pre-Part-B, every page rebuilt fresh, so a continuation page
    WOULD have seen a mid-scroll write immediately -- Part B's session
    cache must not silently freeze a page-1 snapshot that hides later
    writes. This is a DIFFERENT case from
    ``test_each_new_scroll_rebuilds_its_own_fresh_view`` above (a
    brand-new, unrelated scroll): here the write lands mid-scroll, during
    a CONTINUATION of the SAME already-open scroll -- Codex's own live
    reproduction scenario."""

    def test_point_added_between_pages_appears_in_continuation_page(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        n = MID_SCROLL_WRITE_INITIAL_POINT_COUNT
        store = _build_sharded_json_collection(base_path, "coll", n)

        page1, cursor = store.scroll_points(
            collection_name="coll", limit=MID_SCROLL_WRITE_PAGE_LIMIT
        )
        assert cursor is not None
        assert len(page1) == MID_SCROLL_WRITE_PAGE_LIMIT

        # Write lands BETWEEN page 1 and the continuation page. The new id
        # sorts after every id already enumerated by the cached
        # id_to_file map built for page 1.
        _upsert_single_new_point(
            store, "coll", MID_SCROLL_WRITE_NEW_POINT_ID, "mid_scroll_new_file.py"
        )

        collected = [p["id"] for p in page1]
        guard = 0
        while cursor is not None:
            guard += 1
            assert guard <= n + PAGINATION_GUARD_MARGIN
            page, cursor = store.scroll_points(
                collection_name="coll",
                limit=MID_SCROLL_WRITE_PAGE_LIMIT,
                offset=cursor,
            )
            collected.extend(p["id"] for p in page)

        assert MID_SCROLL_WRITE_NEW_POINT_ID in collected, (
            "a point added between pages of an in-progress scroll must be "
            "observed by the continuation page, not hidden by a stale "
            "session-cached enumeration"
        )
        assert len(collected) == len(set(collected)), (
            f"duplicates in continuation results after a mid-scroll write: {collected}"
        )
        expected = {_point_id(i) for i in range(n)} | {MID_SCROLL_WRITE_NEW_POINT_ID}
        assert set(collected) == expected


class TestShardedJsonContinuationPageRglobCallCountDeterministic:
    """Codex review Finding 2 (Bug #1575 Part B): the AC22 timing proof
    above is wall-clock-based, which can pass old code on a host where
    fixed overhead dominates, or fail new code on a slow/busy host. This
    class adds a DETERMINISTIC operation-count proof, WITHOUT mocking or
    monkey-patching anything: CPython's built-in ``sys.setprofile`` call-
    event hook (the same mechanism coverage/profiling tools use) passively
    counts invocations of ``_build_sharded_json_scroll_index`` -- the O(N)
    rglob+parse-every-file rebuild -- by comparing each call event's code
    object identity. No attribute is replaced on any class; the real
    implementation runs completely unmodified. Proves the rebuild runs AT
    MOST ONCE per scroll session (page 1's fresh build) and ZERO further
    times across every continuation page, for two collection sizes 40x
    apart -- the genuine "does not scale with N" proof."""

    @pytest.mark.parametrize(
        "point_count",
        [
            DETERMINISTIC_PROOF_SMALL_POINT_COUNT,
            DETERMINISTIC_PROOF_LARGE_POINT_COUNT,
        ],
    )
    def test_rebuild_call_count_stays_zero_across_continuation_pages(
        self, tmp_path: Path, point_count: int
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_sharded_json_collection(base_path, "coll", point_count)

        page1, cursor = store.scroll_points(
            collection_name="coll", limit=DETERMINISTIC_PROOF_PAGE_LIMIT
        )
        assert cursor is not None

        collected = [p["id"] for p in page1]
        target_code = FilesystemVectorStore._build_sharded_json_scroll_index.__code__
        call_count = 0

        def _count_rebuild_calls(frame, event, arg):  # type: ignore[no-untyped-def]
            nonlocal call_count
            if event == "call" and frame.f_code is target_code:
                call_count += 1

        previous_profiler = sys.getprofile()
        sys.setprofile(_count_rebuild_calls)
        try:
            guard = 0
            while cursor is not None:
                guard += 1
                assert guard <= point_count + PAGINATION_GUARD_MARGIN
                page, cursor = store.scroll_points(
                    collection_name="coll",
                    limit=DETERMINISTIC_PROOF_PAGE_LIMIT,
                    offset=cursor,
                )
                collected.extend(p["id"] for p in page)
        finally:
            sys.setprofile(previous_profiler)

        assert call_count == EXPECTED_REBUILD_CALLS_ACROSS_CONTINUATION_PAGES, (
            f"_build_sharded_json_scroll_index was invoked {call_count} "
            f"times across continuation pages for a {point_count}-point "
            f"collection -- expected exactly "
            f"{EXPECTED_REBUILD_CALLS_ACROSS_CONTINUATION_PAGES} (the O(N) "
            f"rebuild must be served from the session cache, never "
            f"repeated per page, regardless of collection size)"
        )
        assert len(collected) == len(set(collected))
        assert set(collected) == {_point_id(i) for i in range(point_count)}


class TestShardedJsonSessionCacheStalenessSafety:
    """The session cache must never corrupt results even when the data it
    describes changes between pages -- a deleted point, a mid-pagination
    layout flip, or non-matching intervening rows under a filter."""

    def test_point_deleted_between_pages_does_not_crash_or_corrupt(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        n = DELETED_CURSOR_COLLECTION_POINT_COUNT
        store = _build_sharded_json_collection(base_path, "coll", n)

        page1, cursor = store.scroll_points(
            collection_name="coll", limit=DELETED_CURSOR_PAGE_LIMIT
        )
        assert cursor is not None
        deleted_id = page1[LAST_PAGE_POINT_INDEX]["id"]

        # Delete the point BEFORE requesting page 2 -- this stales the
        # cached id_to_file map's entry for this id.
        delete_result = store.delete_points("coll", [deleted_id])
        assert delete_result.get("status") == "ok"
        assert delete_result.get("deleted") == EXPECTED_SINGLE_DELETION_COUNT

        collected = [p["id"] for p in page1]
        guard = 0
        while cursor is not None:
            guard += 1
            assert guard <= n + PAGINATION_GUARD_MARGIN
            page, cursor = store.scroll_points(
                collection_name="coll",
                limit=DELETED_CURSOR_PAGE_LIMIT,
                offset=cursor,
            )
            collected.extend(p["id"] for p in page)

        # The deleted id was already legitimately served in page1 (BEFORE
        # the delete happened) -- it must never be served AGAIN by a later
        # page (that would be a duplicate caused by a stale cache
        # re-emitting it), but the full collected set still equals the
        # complete original id set, since page1's result is unaffected by a
        # later deletion.
        assert deleted_id not in collected[len(page1) :]
        assert len(collected) == len(set(collected)), (
            f"stale-cache self-heal produced duplicates: {collected}"
        )
        expected = {_point_id(i) for i in range(n)}
        assert set(collected) == expected

    def test_mid_pagination_layout_flip_does_not_corrupt_results(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        n = LAYOUT_FLIP_COLLECTION_POINT_COUNT
        store = _build_sharded_json_collection(base_path, "coll", n)
        collection_path = base_path / "coll"

        collected: List[str] = []
        page, cursor = store.scroll_points(
            collection_name="coll", limit=LAYOUT_FLIP_PAGE_LIMIT
        )
        collected.extend(p["id"] for p in page)
        assert cursor is not None

        # Flip layout mid-scroll -- the cached id_to_file map (built for
        # page 1) now describes deleted legacy files.
        consolidation_result = consolidate_collection_in_place(collection_path)
        assert consolidation_result.status in (
            "consolidated",
            "already_consolidated",
        )
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        guard = 0
        while cursor is not None:
            guard += 1
            assert guard <= n + PAGINATION_GUARD_MARGIN
            page, cursor = store.scroll_points(
                collection_name="coll",
                limit=LAYOUT_FLIP_PAGE_LIMIT,
                offset=cursor,
            )
            collected.extend(p["id"] for p in page)

        assert len(collected) == len(set(collected)), (
            f"duplicates across mid-pagination flip with session cache "
            f"active: {collected}"
        )
        assert set(collected) == {_point_id(i) for i in range(n)}

    def test_intervening_non_matching_rows_do_not_break_cursor_continuity(
        self, tmp_path: Path
    ) -> None:
        """Every Nth point is a real non-matching row -- proves filter+cursor
        continuity holds across page boundaries with the session cache
        serving id_to_file for continuation pages."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        n = FILTER_CONTINUITY_COLLECTION_POINT_COUNT
        store = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        created = store.create_collection("coll", vector_size=VECTOR_SIZE)
        assert created is True
        store.begin_indexing("coll")  # returns None: prep-only, no result to check
        points = _make_points(n)
        matching_ids = set()
        for i, point in enumerate(points):
            if i % NON_MATCHING_ROW_STRIDE == 0:
                point["payload"]["language"] = "text"  # non-matching
            else:
                matching_ids.add(point["id"])
        upsert_result = store.upsert_points("coll", points)
        assert upsert_result.get("status") == "ok"
        assert upsert_result.get("count") == n
        store.end_indexing("coll")  # returns None: finalize-only, no result to check
        assert matching_ids and matching_ids != {_point_id(i) for i in range(n)}

        collected: List[str] = []
        cursor = None
        guard = 0
        while True:
            guard += 1
            assert guard <= n + PAGINATION_GUARD_MARGIN
            page, cursor = store.scroll_points(
                collection_name="coll",
                limit=FILTER_CONTINUITY_PAGE_LIMIT,
                offset=cursor,
                filter_conditions={
                    "must": [{"key": "language", "match": {"value": "python"}}]
                },
            )
            collected.extend(p["id"] for p in page)
            if cursor is None:
                break

        assert len(collected) == len(set(collected))
        assert set(collected) == matching_ids


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
