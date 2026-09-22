"""Unit tests for xray_truncation.truncate_result_fields() round-trip
behavior (Bug #1928 rework): small results stay fully inline at the REAL
default config; an oversized single entry (2000-item involved list)
survives the cache round-trip byte-for-byte, replacing the test that
previously locked in data loss; a match with a huge nested dict field
(never touched by the flat string/list inline capper) also survives
intact via the cache, since cache pages are never shrunk.

See _xray_truncation_test_helpers.py for shared fixtures.
"""

from __future__ import annotations

import json as json_module
from pathlib import Path
from typing import Any, Dict

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import (
    cache_factory,  # noqa: F401 -- pytest fixture, used via injection
    fetch_all_pages,
    make_finding,
    make_match,
    make_tiny_match,
)

TINY_MATCH_COUNT = 50
SMALL_BUDGET_CHARS = 300
HUGE_MESSAGE_LENGTH = 20_000
HUGE_INVOLVED_ITEM_COUNT = 2000
OTHER_FINDING_COUNT = 20
HUGE_NESTED_STRING_LENGTH = 6000
OTHER_MATCH_COUNT = 15


class TestSmallResultAllInlineAtDefaultConfig:
    def test_50_tiny_matches_all_inline_at_real_default_config(
        self, tmp_path: Path
    ) -> None:
        """Bug #1928 acceptance: uses PayloadCacheConfig() with NO
        override (the real default payload_max_fetch_size_chars, 5000),
        proving this is not an artifact of an inflated test-only budget.
        Uses the tiny match shape so the fixture's genuine total size
        (computed below, not assumed) is comfortably under 5000 chars."""
        matches = [make_tiny_match(i) for i in range(TINY_MATCH_COUNT)]
        result = {
            "matches": matches,
            "evaluation_errors": [],
            "files_processed": TINY_MATCH_COUNT,
            "files_total": TINY_MATCH_COUNT,
            "elapsed_seconds": 0.2,
        }
        fixture_size = len(
            json_module.dumps({"matches": matches, "evaluation_errors": []})
        )
        assert fixture_size < PayloadCacheConfig().max_fetch_size_chars, (
            "fixture premise: the 50-entry payload must genuinely fit the "
            f"real default budget; got {fixture_size} chars"
        )

        cache = PayloadCache(
            db_path=tmp_path / "default.db", config=PayloadCacheConfig()
        )
        cache.initialize()
        try:
            truncated = xt.truncate_result_fields(
                result, cache, ["matches", "evaluation_errors"]
            )
        finally:
            cache.close()

        assert truncated["cache_handle"] is None
        assert truncated["has_more"] is False
        assert truncated["truncated"] is False
        assert truncated["matches"] == matches


class TestHugeSingleEntryRoundTripsIntact:
    def test_involved_list_survives_the_cache_round_trip(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=SMALL_BUDGET_CHARS)
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
            "fact_graph_complete": True,
        }

        truncated = xt.truncate_result_fields(result, cache, ["findings", "refine"])

        assert truncated["truncated"] is True
        assert truncated["inline_entry_truncated"] is True, (
            f"the huge entry alone exceeds the {SMALL_BUDGET_CHARS}-char "
            "budget, so the INLINE preview must be flagged as capped"
        )

        pages = fetch_all_pages(cache, truncated["cache_handle"])
        all_findings = [f for p in pages for f in p["findings"]]
        stored_huge = next(f for f in all_findings if f.get("pattern") == "huge")
        assert stored_huge == huge_finding
        assert len(stored_huge["involved"]) == HUGE_INVOLVED_ITEM_COUNT
        assert [f["pattern"] for f in all_findings if f.get("pattern") != "huge"] == [
            f["pattern"] for f in other_findings
        ]


class TestNestedDictOversizedEntryRoundTrips:
    def test_match_with_huge_nested_dict_field_survives_intact(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=SMALL_BUDGET_CHARS)
        nested_match = {
            "file_path": "big.py",
            "line_number": 1,
            "code_snippet": "x",
            "context": {"deep": {"deeper": "d" * HUGE_NESTED_STRING_LENGTH}},
        }
        other_matches = [make_match(i) for i in range(OTHER_MATCH_COUNT)]
        result = {
            "matches": [nested_match] + other_matches,
            "evaluation_errors": [],
            "files_processed": OTHER_MATCH_COUNT + 1,
            "files_total": OTHER_MATCH_COUNT + 1,
            "elapsed_seconds": 1.0,
        }

        truncated = xt.truncate_result_fields(
            result, cache, ["matches", "evaluation_errors"]
        )

        assert truncated["truncated"] is True
        pages = fetch_all_pages(cache, truncated["cache_handle"])
        all_matches = [m for p in pages for m in p["matches"]]
        stored_nested = next(m for m in all_matches if m.get("file_path") == "big.py")
        assert stored_nested == nested_match
        assert (
            stored_nested["context"]["deep"]["deeper"]
            == "d" * HUGE_NESTED_STRING_LENGTH
        )
