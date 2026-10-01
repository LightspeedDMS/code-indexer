"""Bug #1575 Part B: keyset pagination primitive for ChunkStore.

``_scroll_points_chunks_db`` previously called ``sorted(chunk_store.all_point_ids())``
on EVERY page -- for N points and page size L that is ~(N/L) full O(N log N)
Python sorts of N ids across one scroll. ``point_ids_after()`` replaces that
with a keyset query (``WHERE point_id > ? ORDER BY point_id LIMIT ?``) that
uses the ``point_id`` PRIMARY KEY's own index -- the cost of one page no
longer depends on the total row count.
"""

from pathlib import Path

from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _record(point_id: str, path: str) -> dict:
    return {
        "id": point_id,
        "vector": [0.1, 0.2, 0.3],
        "payload": {"path": path},
        "chunk_text": "x",
    }


def _seeded_store(tmp_path: Path, n: int = 50) -> ChunkStore:
    """Shared setup for the query-plan tests below: a fresh ChunkStore with
    ``n`` sequentially-named records already written."""
    store = ChunkStore(tmp_path / "chunks.db")
    store.write_batch([_record(f"p{i:05d}", f"f{i}.py") for i in range(n)])
    return store


class TestPointIdsAfterBasics:
    def test_returns_first_page_sorted_when_cursor_is_none(
        self, tmp_path: Path
    ) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch(
            [_record("p003", "d.py"), _record("p001", "b.py"), _record("p002", "c.py")]
        )

        result = store.point_ids_after(None, limit=2)

        assert result == ["p001", "p002"]

    def test_resumes_strictly_after_cursor(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record(f"p{i:03d}", f"f{i}.py") for i in range(5)])

        page1 = store.point_ids_after(None, limit=2)
        assert page1 == ["p000", "p001"]
        page2 = store.point_ids_after(page1[-1], limit=2)
        assert page2 == ["p002", "p003"]
        page3 = store.point_ids_after(page2[-1], limit=2)
        assert page3 == ["p004"]
        page4 = store.point_ids_after(page3[-1], limit=2)
        assert page4 == []

    def test_empty_store_returns_empty_list(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")

        assert store.point_ids_after(None, limit=10) == []

    def test_cursor_past_end_returns_empty(self, tmp_path: Path) -> None:
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record("p001", "a.py")])

        assert store.point_ids_after("zzz_past_end", limit=10) == []

    def test_cursor_pointing_to_deleted_id_resumes_at_next_greater_id(
        self, tmp_path: Path
    ) -> None:
        """A cursor whose id has since been deleted must resume at the next
        id greater than it -- never crash, never duplicate, never skip.
        """
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record(f"p{i:03d}", f"f{i}.py") for i in range(5)])
        store.delete(["p002"])  # cursor will point at a now-deleted id

        result = store.point_ids_after("p002", limit=10)

        assert result == ["p003", "p004"]

    def test_limit_is_bounded_regardless_of_total_row_count(
        self, tmp_path: Path
    ) -> None:
        """The whole point of keyset pagination: a page's cost is bounded by
        ``limit``, never by the total number of stored rows.
        """
        store = ChunkStore(tmp_path / "chunks.db")
        store.write_batch([_record(f"p{i:05d}", f"f{i}.py") for i in range(3000)])

        result = store.point_ids_after(None, limit=5)

        assert len(result) == 5
        assert result == ["p00000", "p00001", "p00002", "p00003", "p00004"]


class TestPointIdsAfterQueryPlan:
    """Structural proof (AC22, corrected -- Bug #1575 Part B Codex review
    Finding 3): the two ``point_ids_after`` query forms produce DIFFERENT
    plan shapes (SEARCH vs SCAN), so they must be verified separately and
    precisely -- never with one blanket "SCAN chunks not in plan_text"
    assertion applied to both. What actually matters for both forms is that
    they are answered index-ONLY, via the ``point_id`` PRIMARY KEY's own
    covering index, and never fall back to reading the full row (the
    ``vector``/``data`` blob columns)."""

    def test_cursor_present_query_uses_index_search_not_a_row_scan(
        self, tmp_path: Path
    ) -> None:
        store = _seeded_store(tmp_path)

        plan_rows = store._conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT point_id FROM chunks WHERE point_id > ? "
            "ORDER BY point_id LIMIT ?",
            ("p00010", 5),
        ).fetchall()
        plan_text = " ".join(str(row) for row in plan_rows)

        # Verified empirically: SQLite answers this exact query via
        # "SEARCH TABLE chunks USING COVERING INDEX sqlite_autoindex_chunks_1
        # (point_id>?)" -- an index SEARCH driven by the WHERE clause,
        # answered entirely from the implicit unique index SQLite creates
        # for a non-integer TEXT PRIMARY KEY (never touching the
        # vector/data blob columns).
        assert "SEARCH" in plan_text, (
            f"expected an index SEARCH plan for the cursor-present form, "
            f"got: {plan_text}"
        )
        assert "USING COVERING INDEX" in plan_text, (
            f"expected the query to be answered index-only via the "
            f"covering index (never reading the full row): {plan_text}"
        )

    def test_cursor_none_query_uses_covering_index_not_a_row_scan(
        self, tmp_path: Path
    ) -> None:
        """The unconditional (cursor=None) form has no WHERE clause to
        search on, so SQLite reports a SCAN rather than a SEARCH -- that is
        expected and NOT a defect. What matters is that the SCAN is still
        answered entirely from the covering index (``USING COVERING
        INDEX``), never a full row read of the ``vector``/``data`` blob
        columns. The earlier blanket "SCAN chunks not in plan_text"
        assertion was imprecise: this exact plan's text IS "SCAN TABLE
        chunks USING COVERING INDEX ..." -- a real, literal "SCAN" that a
        naive substring check for "SCAN chunks" happened to miss only
        because of the intervening word "TABLE".
        """
        store = _seeded_store(tmp_path)

        plan_rows = store._conn.execute(
            "EXPLAIN QUERY PLAN SELECT point_id FROM chunks ORDER BY point_id LIMIT ?",
            (5,),
        ).fetchall()
        plan_text = " ".join(str(row) for row in plan_rows)

        # Explicitly confirm the expected SCAN shape (not merely absence of
        # a stray substring) -- if SQLite's plan for this query ever changed
        # to a SEARCH, this assertion catches that shape change directly.
        assert "SCAN" in plan_text, (
            f"expected a SCAN plan for the unconditional cursor-None form "
            f"(no WHERE clause to search on): {plan_text}"
        )
        assert "USING COVERING INDEX" in plan_text, (
            f"expected an index-only covering-index scan, never a full row "
            f"read of the vector/data blob columns: {plan_text}"
        )
