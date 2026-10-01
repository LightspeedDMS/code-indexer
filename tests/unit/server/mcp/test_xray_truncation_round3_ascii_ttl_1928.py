"""Bug #1928 round 3 (P4): non-ASCII entries round-trip intact through
the whole store/pack/fetch cycle; the page-set write's shared timestamp
means a backdated TTL expires the WHOLE set together (pages AND
manifest), never partially -- a page surviving while the manifest is
gone (or vice versa) would reintroduce the original P1 bug in a
different guise.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List

import pytest

from code_indexer.server.cache.payload_cache import CacheNotFoundError
from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import (  # noqa: F401
    cache_factory,
    fetch_all_pages,
    make_match,
)

_SMALL_BUDGET_CHARS = 300
_MANY_MATCH_COUNT = 30
_SHORT_TTL_SECONDS = 900
_TTL_BACKDATE_SECONDS = 10_000  # well past any real TTL used in this file


class TestNonAsciiRoundTrip:
    def test_non_ascii_entries_round_trip_intact(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=_SMALL_BUDGET_CHARS)
        matches: List[Dict[str, Any]] = [
            {
                "file_path": "日本語.py",
                "line_number": i,
                "snippet": "café ☃ \U0001f600",
            }
            for i in range(10)
        ]
        result = {"matches": matches, "evaluation_errors": []}

        truncated = xt.truncate_result_fields(
            result, cache, ["matches", "evaluation_errors"]
        )
        pages = fetch_all_pages(cache, truncated["cache_handle"])
        reconstructed = [m for p in pages for m in p["matches"]]

        assert reconstructed == matches


class TestTtlBoundaryAllRowsExpireTogether:
    def test_backdated_shared_timestamp_expires_the_whole_set(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=_SMALL_BUDGET_CHARS)
        matches = [make_match(i) for i in range(_MANY_MATCH_COUNT)]
        truncated = xt.truncate_result_fields(
            {"matches": matches, "evaluation_errors": []},
            cache,
            ["matches", "evaluation_errors"],
        )
        cache_handle = truncated["cache_handle"]
        total_pages = truncated["total_pages"]

        # First, prove the production write actually shared ONE
        # timestamp across every page row + the manifest row -- read the
        # real values BEFORE any test-side mutation, rather than assuming
        # it and later overwriting them uniformly ourselves.
        conn = sqlite3.connect(str(cache.db_path))
        try:
            rows = conn.execute("SELECT created_at FROM payload_cache").fetchall()
        finally:
            conn.close()
        assert len(rows) == total_pages + 1, (
            "fixture premise: exactly total_pages page rows + 1 manifest "
            "row expected in this fresh cache"
        )
        distinct_timestamps = {r[0] for r in rows}
        assert len(distinct_timestamps) == 1, (
            "the page-set write must share ONE timestamp across every "
            f"page + the manifest, got {distinct_timestamps}"
        )
        shared_timestamp = distinct_timestamps.pop()

        # Deterministically force expiry (real sleeping for TTL seconds
        # would be slow/flaky) by backdating that SAME verified shared
        # value -- not an arbitrary new one.
        backdated = shared_timestamp - _SHORT_TTL_SECONDS - _TTL_BACKDATE_SECONDS
        conn = sqlite3.connect(str(cache.db_path))
        try:
            conn.execute("UPDATE payload_cache SET created_at = ?", (backdated,))
            conn.commit()
        finally:
            conn.close()

        deleted = cache.cleanup_expired()
        assert deleted == total_pages + 1, (
            f"expected ALL {total_pages + 1} rows (every page + the "
            f"manifest) to be swept together since they share one "
            f"timestamp, got {deleted}"
        )

        conn = sqlite3.connect(str(cache.db_path))
        try:
            row_count_after = conn.execute(
                "SELECT COUNT(*) FROM payload_cache"
            ).fetchone()[0]
        finally:
            conn.close()
        assert row_count_after == 0, "no page or manifest rows should survive"

        with pytest.raises(CacheNotFoundError):
            xt.fetch_cached_page(cache, cache_handle, 1)
