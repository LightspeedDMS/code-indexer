"""Live PostgreSQL companion for the Bug #1928 xray_truncation suite --
gated by TEST_POSTGRES_DSN (same convention as
test_per_consumer_rate_limiter_live_pg_1332.py), skipped when no real
PostgreSQL is reachable. Mirrors the SQLite huge-single-entry
round-trip-intact proof from test_xray_truncation_roundtrip_1928.py,
against a REAL PostgreSQL-backed PayloadCache.
"""

from __future__ import annotations

from typing import Any, Dict

from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import (
    fetch_all_pages,
    make_finding,
    pg_cache,  # noqa: F401 -- pytest fixture, used via injection
    pg_dsn,  # noqa: F401 -- pytest fixture, used via injection
)

HUGE_MESSAGE_LENGTH = 20_000
HUGE_INVOLVED_ITEM_COUNT = 2000
OTHER_FINDING_COUNT = 20


class TestLivePostgresPagesRoundTrip:
    def test_oversized_entry_round_trips_intact_on_real_postgres(
        self,
        pg_cache,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        huge_finding = {
            "pattern": "huge",
            "message": "m" * HUGE_MESSAGE_LENGTH,
            "involved": list(range(HUGE_INVOLVED_ITEM_COUNT)),
        }
        other_findings = [make_finding(i) for i in range(OTHER_FINDING_COUNT)]
        result: Dict[str, Any] = {
            "ok": True,
            "findings": [huge_finding] + other_findings,
            "refine": [],
        }

        truncated = xt.truncate_result_fields(result, pg_cache, ["findings", "refine"])

        pages = fetch_all_pages(pg_cache, truncated["cache_handle"])
        all_findings = [f for p in pages for f in p["findings"]]
        stored_huge = next(f for f in all_findings if f.get("pattern") == "huge")
        assert stored_huge == huge_finding
        assert len(stored_huge["involved"]) == HUGE_INVOLVED_ITEM_COUNT
