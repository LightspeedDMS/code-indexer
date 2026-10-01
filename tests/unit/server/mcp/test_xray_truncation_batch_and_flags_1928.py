"""Unit tests for xray_truncation's N=3-field batch path (xray_search_batch:
matches, errors, evaluation_errors) and the inline_entry_truncated flag
contract (Bug #1928 rework). See _xray_truncation_test_helpers.py for
shared fixtures.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import (
    cache_factory,  # noqa: F401 -- pytest fixture, used via injection
    fetch_all_pages,
    make_error,
    make_match,
)

BATCH_MATCH_COUNT = 40
BATCH_ERROR_COUNT = 10
BATCH_EVAL_ERROR_COUNT = 10
NORMAL_BUDGET_CHARS = 400
TINY_BUDGET_CHARS = 10
HUGE_MESSAGE_LENGTH = 20_000
HUGE_INVOLVED_ITEM_COUNT = 2000
TOTAL_REPOS_PLACEHOLDER = 2
ELAPSED_SECONDS_PLACEHOLDER = 1.0


class TestBatchPathThreeFields:
    def test_three_field_result_truncates_and_round_trips(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=NORMAL_BUDGET_CHARS)
        matches = [make_match(i) for i in range(BATCH_MATCH_COUNT)]
        errors = [
            {"error_level": "cell", "message": f"e{i}"}
            for i in range(BATCH_ERROR_COUNT)
        ]
        eval_errors = [make_error(i) for i in range(BATCH_EVAL_ERROR_COUNT)]
        result = {
            "matches": matches,
            "errors": errors,
            "evaluation_errors": eval_errors,
            "total_repos": TOTAL_REPOS_PLACEHOLDER,
        }

        truncated = xt.truncate_result_fields(
            result, cache, ["matches", "errors", "evaluation_errors"]
        )

        assert truncated["truncated"] is True
        assert "matches_and_errors_preview" not in truncated
        pages = fetch_all_pages(cache, truncated["cache_handle"])
        assert [m for p in pages for m in p["matches"]] == matches
        assert [e for p in pages for e in p["errors"]] == errors
        assert [e for p in pages for e in p["evaluation_errors"]] == eval_errors


class TestInlineEntryTruncatedFlagOrdinaryCase:
    def test_flag_is_false_when_first_entry_fits_alone(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=NORMAL_BUDGET_CHARS)
        matches = [make_match(i) for i in range(BATCH_MATCH_COUNT)]
        result = {
            "matches": matches,
            "evaluation_errors": [],
            "files_processed": BATCH_MATCH_COUNT,
            "files_total": BATCH_MATCH_COUNT,
            "elapsed_seconds": ELAPSED_SECONDS_PLACEHOLDER,
        }

        truncated = xt.truncate_result_fields(
            result, cache, ["matches", "evaluation_errors"]
        )

        assert truncated["truncated"] is True
        assert truncated["inline_entry_truncated"] is False
        assert len(truncated["matches"]) > 0
        assert truncated["matches"] == matches[: len(truncated["matches"])]


class TestInlineEmptyLogsWarning:
    def test_pathologically_tiny_budget_logs_warning_and_empties_inline(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
        caplog,
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=TINY_BUDGET_CHARS)
        huge_finding = {
            "pattern": "huge",
            "message": "m" * HUGE_MESSAGE_LENGTH,
            "involved": list(range(HUGE_INVOLVED_ITEM_COUNT)),
        }
        result: Dict[str, Any] = {
            "ok": True,
            "findings": [huge_finding],
            "refine": [],
        }

        with caplog.at_level(logging.WARNING):
            truncated = xt.truncate_result_fields(result, cache, ["findings", "refine"])

        assert truncated["inline_entry_truncated"] is True
        assert truncated["findings"] == []
        assert any(
            "inline preview is empty" in record.message for record in caplog.records
        )
        # Still fully recoverable via the cache -- no data loss.
        pages = fetch_all_pages(cache, truncated["cache_handle"])
        stored = [f for p in pages for f in p["findings"]]
        assert stored == [huge_finding]
