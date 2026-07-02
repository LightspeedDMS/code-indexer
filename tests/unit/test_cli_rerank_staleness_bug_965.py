"""Regression test for bug #965: staleness conversion loop must preserve reranked order.

When all results are fresh, apply_staleness_detection internally sorts by
(is_stale, -similarity_score), producing score-descending order. The buggy
conversion loop iterated that sorted list, discarding the reranker's order.

The fix extracts a helper _annotate_staleness(results, enhanced_results) that
iterates `results` (reranked order) and looks up staleness data by path,
preserving order while attaching metadata.
"""

from typing import Any, Dict, List
from code_indexer.remote.staleness_detector import EnhancedQueryResultItem


def _make_raw_result(path: str, score: float) -> Dict[str, Any]:
    """Build a raw CLI result dict as returned by the vector store / reranker."""
    return {
        "score": score,
        "payload": {
            "path": path,
            "content": f"snippet from {path}",
            "line_start": 1,
            "file_last_modified": None,
            "indexed_at": None,
        },
    }


def _make_raw_result_with_line(
    path: str, score: float, line_start: int
) -> Dict[str, Any]:
    """Build a raw CLI result dict with an explicit line_start (for multi-chunk files)."""
    return {
        "score": score,
        "payload": {
            "path": path,
            "content": f"snippet from {path}:{line_start}",
            "line_start": line_start,
            "file_last_modified": None,
            "indexed_at": None,
        },
    }


def _make_enhanced_with_line(
    path: str, score: float, line_number: int, is_stale: bool = False
) -> EnhancedQueryResultItem:
    """Build an EnhancedQueryResultItem with an explicit line_number."""
    return EnhancedQueryResultItem(
        file_path=path,
        line_number=line_number,
        code_snippet=f"snippet from {path}:{line_number}",
        similarity_score=score,
        repository_alias="test-repo",
        file_last_modified=None,
        indexed_timestamp=None,
        local_file_mtime=None,
        is_stale=is_stale,
        staleness_delta_seconds=None,
        staleness_indicator="🟢" if not is_stale else "🔴",
    )


def _make_enhanced(
    path: str, score: float, is_stale: bool = False
) -> EnhancedQueryResultItem:
    """Build an EnhancedQueryResultItem as returned by StalenessDetector."""
    return EnhancedQueryResultItem(
        file_path=path,
        line_number=1,
        code_snippet=f"snippet from {path}",
        similarity_score=score,
        repository_alias="test-repo",
        file_last_modified=None,
        indexed_timestamp=None,
        local_file_mtime=None,
        is_stale=is_stale,
        staleness_delta_seconds=None,
        staleness_indicator="🟢" if not is_stale else "🔴",
    )


class TestBug965StalenessPreservesRerankedOrder:
    """Bug #965: _annotate_staleness must iterate results (reranked order), not enhanced_results."""

    def test_staleness_conversion_preserves_reranked_order(self) -> None:
        """Reranked order is preserved when _annotate_staleness is called.

        Scenario: reranker places README.md (score 0.632) first, cli.py (0.665)
        second, termui.py (0.645) third. StalenessDetector internally sorts by
        (is_stale, -score), returning [cli.py, termui.py, README.md] when all
        fresh. _annotate_staleness must produce [README.md, cli.py, termui.py].
        """
        from code_indexer.cli import _annotate_staleness  # real production function

        # Reranked order: README.md first despite lowest score
        scores = {"README.md": 0.632, "cli.py": 0.665, "termui.py": 0.645}
        reranked_order = ["README.md", "cli.py", "termui.py"]
        results: List[Dict[str, Any]] = [
            _make_raw_result(p, scores[p]) for p in reranked_order
        ]

        # StalenessDetector returns results sorted by (is_stale, -score):
        # all fresh -> score-descending: [cli.py, termui.py, README.md]
        staleness_sorted_order = ["cli.py", "termui.py", "README.md"]
        enhanced_results: List[EnhancedQueryResultItem] = [
            _make_enhanced(p, scores[p], is_stale=False) for p in staleness_sorted_order
        ]

        output = _annotate_staleness(results, enhanced_results, preserve_order=True)
        output_paths = [r["payload"]["path"] for r in output]

        assert output_paths == reranked_order, (
            f"Expected reranked order {reranked_order}, got {output_paths}. "
            f"Bug #965: staleness conversion loop must iterate results (reranked), "
            f"not enhanced_results (score-sorted)."
        )

    def test_staleness_metadata_attached_to_results(self) -> None:
        """Each result must have a staleness dict with is_stale, staleness_indicator,
        and staleness_delta_seconds keys when a matching enhanced result exists."""
        from code_indexer.cli import _annotate_staleness

        results: List[Dict[str, Any]] = [
            _make_raw_result("a.py", 0.9),
            _make_raw_result("b.py", 0.8),
        ]
        enhanced_results: List[EnhancedQueryResultItem] = [
            _make_enhanced("a.py", 0.9, is_stale=False),
            _make_enhanced("b.py", 0.8, is_stale=True),
        ]

        output = _annotate_staleness(results, enhanced_results, preserve_order=True)

        assert len(output) == 2

        # a.py is fresh
        a_staleness = output[0].get("staleness")
        assert a_staleness is not None, "a.py result must have 'staleness' key"
        assert a_staleness["is_stale"] is False
        assert a_staleness["staleness_indicator"] == "🟢"
        assert "staleness_delta_seconds" in a_staleness

        # b.py is stale
        b_staleness = output[1].get("staleness")
        assert b_staleness is not None, "b.py result must have 'staleness' key"
        assert b_staleness["is_stale"] is True
        assert b_staleness["staleness_indicator"] == "🔴"
        assert "staleness_delta_seconds" in b_staleness

    def test_preserve_order_false_sorts_fresh_first(self) -> None:
        """When preserve_order=False, fresh results appear before stale ones.

        Non-reranked queries should get fresh-first ordering so users see
        up-to-date files at the top. Ties within freshness group are broken
        by score descending.
        """
        from code_indexer.cli import _annotate_staleness

        # Input order: stale.py first, then two fresh files
        results: List[Dict[str, Any]] = [
            _make_raw_result("stale.py", 0.95),  # highest score but stale
            _make_raw_result("fresh_hi.py", 0.80),
            _make_raw_result("fresh_lo.py", 0.70),
        ]
        enhanced_results: List[EnhancedQueryResultItem] = [
            _make_enhanced("stale.py", 0.95, is_stale=True),
            _make_enhanced("fresh_hi.py", 0.80, is_stale=False),
            _make_enhanced("fresh_lo.py", 0.70, is_stale=False),
        ]

        output = _annotate_staleness(results, enhanced_results, preserve_order=False)
        output_paths = [r["payload"]["path"] for r in output]

        # Fresh files must come before stale file; within fresh group, higher score first
        assert output_paths == ["fresh_hi.py", "fresh_lo.py", "stale.py"], (
            f"Expected fresh-first order, got {output_paths}. "
            f"preserve_order=False must sort by (is_stale, -score)."
        )

    def test_preserve_order_true_keeps_reranked_order_with_stale(self) -> None:
        """When preserve_order=True, reranked order is kept even when some items are stale."""
        from code_indexer.cli import _annotate_staleness

        # Reranker places stale.py first despite being stale
        reranked_order = ["stale.py", "fresh.py"]
        results: List[Dict[str, Any]] = [
            _make_raw_result("stale.py", 0.95),
            _make_raw_result("fresh.py", 0.80),
        ]
        # StalenessDetector would sort fresh first: [fresh.py, stale.py]
        enhanced_results: List[EnhancedQueryResultItem] = [
            _make_enhanced("fresh.py", 0.80, is_stale=False),
            _make_enhanced("stale.py", 0.95, is_stale=True),
        ]

        output = _annotate_staleness(results, enhanced_results, preserve_order=True)
        output_paths = [r["payload"]["path"] for r in output]

        assert output_paths == reranked_order, (
            f"Expected reranked order {reranked_order}, got {output_paths}. "
            f"preserve_order=True must keep caller's order regardless of staleness."
        )

    def test_multiple_chunks_same_file_no_collision(self) -> None:
        """Multiple chunks from the same file must not be silently dropped.

        CIDX emits multiple chunks per file (same path, different line_start).
        A path-only lookup key causes dict comprehension to overwrite sibling
        chunks — only the last chunk per file survives, and remaining chunks
        lose their staleness annotation.  The fix uses (path, line_start) as
        the composite key so every chunk is distinct.
        """
        from code_indexer.cli import _annotate_staleness

        # Three chunks: file.py has two chunks at line 1 and line 50; other.py has one.
        results: List[Dict[str, Any]] = [
            _make_raw_result_with_line("file.py", 0.9, 1),
            _make_raw_result_with_line("file.py", 0.85, 50),
            _make_raw_result_with_line("other.py", 0.7, 1),
        ]
        enhanced_results: List[EnhancedQueryResultItem] = [
            _make_enhanced_with_line("file.py", 0.9, 1, is_stale=False),
            _make_enhanced_with_line("file.py", 0.85, 50, is_stale=True),
            _make_enhanced_with_line("other.py", 0.7, 1, is_stale=False),
        ]

        # --- preserve_order=True ---
        output_ordered = _annotate_staleness(
            results, enhanced_results, preserve_order=True
        )
        assert len(output_ordered) == 3, (
            f"preserve_order=True: expected 3 items, got {len(output_ordered)}. "
            f"Path-only key drops sibling chunks from same file."
        )
        # file.py@1 should be fresh, file.py@50 should be stale
        chunk1 = next(
            r
            for r in output_ordered
            if r["payload"]["path"] == "file.py" and r["payload"]["line_start"] == 1
        )
        chunk2 = next(
            r
            for r in output_ordered
            if r["payload"]["path"] == "file.py" and r["payload"]["line_start"] == 50
        )
        assert chunk1["staleness"]["is_stale"] is False
        assert chunk2["staleness"]["is_stale"] is True

        # --- preserve_order=False ---
        output_unsorted = _annotate_staleness(
            results, enhanced_results, preserve_order=False
        )
        assert len(output_unsorted) == 3, (
            f"preserve_order=False: expected 3 items, got {len(output_unsorted)}. "
            f"Path-only key drops sibling chunks from same file."
        )
        # All three must have a staleness annotation
        for item in output_unsorted:
            assert "staleness" in item, (
                f"Item {item['payload']['path']}@{item['payload']['line_start']} "
                f"missing staleness annotation."
            )
