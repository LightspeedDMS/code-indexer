"""Bug #1928 round 3 (P3 -- Opus): pack_entries_into_pages must not
re-serialize the whole page per entry (was O(n^2): ~24s at a 200k
budget with many entries). Must track a running size instead.
"""

from __future__ import annotations

import time

from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import make_tiny_match  # noqa: F401

_LARGE_BUDGET_CHARS = 200_000
_LARGE_ENTRY_COUNT = 3000
_MAX_PACK_SECONDS = 3.0


class TestPackingPerformanceStaysLinear:
    def test_large_budget_many_entries_stays_fast(self) -> None:
        matches = [make_tiny_match(i) for i in range(_LARGE_ENTRY_COUNT)]
        fields = {"matches": matches, "evaluation_errors": []}

        start = time.monotonic()
        pages = xt.pack_entries_into_pages(
            ["matches", "evaluation_errors"], fields, _LARGE_BUDGET_CHARS
        )
        elapsed = time.monotonic() - start

        assert elapsed < _MAX_PACK_SECONDS, (
            f"pack_entries_into_pages took {elapsed:.2f}s for "
            f"{_LARGE_ENTRY_COUNT} entries at budget={_LARGE_BUDGET_CHARS} "
            f"-- must stay well under {_MAX_PACK_SECONDS}s (was ~24s "
            "quadratic before the fix)"
        )
        reconstructed = [m for p in pages for m in p["matches"]]
        assert reconstructed == matches
