"""Unit tests for xray_truncation cache-page mechanics (Bug #1928
rework): every page independently parseable and concatenates in order;
each page is really its own PayloadCache row (direct SQLite row-count
proof, no mocking); paging survives payload_max_fetch_size_chars
changing between store and fetch; a pre-#1928-deploy legacy (raw,
non-manifest) handle still falls back to the old windowed retrieve().

See _xray_truncation_test_helpers.py for shared fixtures.
"""

from __future__ import annotations

import json as json_module
import sqlite3
from pathlib import Path

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import (
    cache_factory,  # noqa: F401 -- pytest fixture, used via injection
    fetch_all_pages,
    make_error,
    make_match,
)

MANY_MATCH_COUNT = 150
MANY_ERROR_COUNT = 15
PAGE_BUDGET_CHARS = 400
TINY_LEGACY_BUDGET_CHARS = 50
CONFIG_CHANGE_STORE_BUDGET = 300
CONFIG_CHANGE_FETCH_BUDGET = 50
CONFIG_CHANGE_MATCH_COUNT = 60
FIRST_PAGE_NUMBER = 1
ELAPSED_SECONDS_PLACEHOLDER = 1.0
MIN_PAGES_FOR_MULTI_PAGE_PROOF = 1


class TestEveryPageIndependentlyParseable:
    def test_many_pages_each_parse_standalone_and_concatenate_in_order(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=PAGE_BUDGET_CHARS)
        matches = [make_match(i) for i in range(MANY_MATCH_COUNT)]
        errors = [make_error(i) for i in range(MANY_ERROR_COUNT)]
        result = {
            "matches": matches,
            "evaluation_errors": errors,
            "files_processed": MANY_MATCH_COUNT,
            "files_total": MANY_MATCH_COUNT,
            "elapsed_seconds": ELAPSED_SECONDS_PLACEHOLDER,
        }

        truncated = xt.truncate_result_fields(
            result, cache, ["matches", "evaluation_errors"]
        )

        pages = fetch_all_pages(cache, truncated["cache_handle"])
        assert len(pages) > MIN_PAGES_FOR_MULTI_PAGE_PROOF, (
            "fixture must require multiple real pages"
        )
        reconstructed_matches = [m for p in pages for m in p["matches"]]
        reconstructed_errors = [e for p in pages for e in p["evaluation_errors"]]
        assert reconstructed_matches == matches
        assert reconstructed_errors == errors

    def test_sqlite_row_count_matches_pages_plus_one_manifest_row(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        """Direct, non-mocked proof: the number of rows physically
        written to the SQLite payload_cache table equals total_pages + 1
        (the manifest) -- i.e. every page really is its own row. Reads
        `cache.db_path` (PayloadCache's own public attribute) directly
        rather than guessing the fixture's internal naming convention."""
        cache = cache_factory(max_fetch_size_chars=PAGE_BUDGET_CHARS)
        matches = [make_match(i) for i in range(MANY_MATCH_COUNT)]
        result = {
            "matches": matches,
            "evaluation_errors": [],
            "files_processed": MANY_MATCH_COUNT,
            "files_total": MANY_MATCH_COUNT,
            "elapsed_seconds": ELAPSED_SECONDS_PLACEHOLDER,
        }

        truncated = xt.truncate_result_fields(
            result, cache, ["matches", "evaluation_errors"]
        )
        assert truncated["truncated"] is True
        total_pages = truncated["total_pages"]
        assert total_pages > MIN_PAGES_FOR_MULTI_PAGE_PROOF

        conn = sqlite3.connect(str(cache.db_path))
        try:
            row_count = conn.execute("SELECT COUNT(*) FROM payload_cache").fetchone()[0]
        finally:
            conn.close()

        assert row_count == total_pages + 1, (
            f"expected {total_pages} page rows + 1 manifest row, got {row_count}"
        )


class TestConfigChangeBetweenStoreAndFetch:
    def test_pages_still_parse_after_max_fetch_size_chars_changes(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "shared.db"
        store_config = PayloadCacheConfig(
            preview_size_chars=CONFIG_CHANGE_STORE_BUDGET,
            max_fetch_size_chars=CONFIG_CHANGE_STORE_BUDGET,
        )
        store_cache = PayloadCache(db_path=db_path, config=store_config)
        store_cache.initialize()
        matches = [make_match(i) for i in range(CONFIG_CHANGE_MATCH_COUNT)]
        result = {
            "matches": matches,
            "evaluation_errors": [],
            "files_processed": CONFIG_CHANGE_MATCH_COUNT,
            "files_total": CONFIG_CHANGE_MATCH_COUNT,
            "elapsed_seconds": ELAPSED_SECONDS_PLACEHOLDER,
        }
        try:
            truncated = xt.truncate_result_fields(
                result, store_cache, ["matches", "evaluation_errors"]
            )
            cache_handle = truncated["cache_handle"]
        finally:
            store_cache.close()

        # A DIFFERENT PayloadCache instance, pointed at the SAME db file,
        # with a DRASTICALLY different max_fetch_size_chars -- simulating
        # a Web UI config change or a node restart.
        fetch_config = PayloadCacheConfig(
            preview_size_chars=CONFIG_CHANGE_FETCH_BUDGET,
            max_fetch_size_chars=CONFIG_CHANGE_FETCH_BUDGET,
        )
        fetch_cache = PayloadCache(db_path=db_path, config=fetch_config)
        fetch_cache.initialize()
        try:
            pages = fetch_all_pages(fetch_cache, cache_handle)
        finally:
            fetch_cache.close()

        reconstructed = [m for p in pages for m in p["matches"]]
        assert reconstructed == matches


class TestLegacyHandleStillFetchable:
    def test_raw_non_manifest_handle_falls_back_to_windowed_retrieve(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=TINY_LEGACY_BUDGET_CHARS)
        raw_content = json_module.dumps({"matches": [make_match(0)]})
        legacy_handle = cache.store(raw_content)

        result = xt.fetch_cached_page(cache, legacy_handle, FIRST_PAGE_NUMBER)

        assert result["content"] == raw_content[:TINY_LEGACY_BUDGET_CHARS]
        assert result["total_pages"] == -(-len(raw_content) // TINY_LEGACY_BUDGET_CHARS)
