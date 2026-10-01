"""Bug #1928 round 3 (P3 -- Codex): payload_cache unavailable must return
a BOUNDED inline page 1 (never the full unbounded result), with
cache_handle=null, cache_unavailable=true, truncated=true, and an ERROR
log -- bounded and honest instead of silently dumping everything inline.
"""

from __future__ import annotations

import logging

from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import make_match, make_tiny_match  # noqa: F401

_MANY_MATCH_COUNT = (
    60  # ~8000 chars serialized, reliably over the 5000-char default budget
)


class TestCacheUnavailableBoundedResponse:
    def test_large_result_is_bounded_not_full_when_cache_unavailable(self) -> None:
        matches = [make_match(i) for i in range(_MANY_MATCH_COUNT)]
        result = {"matches": matches, "evaluation_errors": []}

        out = xt.truncate_result_fields(result, None, ["matches", "evaluation_errors"])

        assert out["cache_unavailable"] is True
        assert out["truncated"] is True
        assert out["cache_handle"] is None
        assert len(out["matches"]) < len(matches), (
            "cache-unavailable degrade must still BOUND the inline result "
            "-- returning everything defeats the whole truncation contract"
        )

    def test_small_result_still_flags_cache_unavailable(self) -> None:
        result = {"matches": [make_tiny_match(0)], "evaluation_errors": []}

        out = xt.truncate_result_fields(result, None, ["matches", "evaluation_errors"])

        assert out["cache_unavailable"] is True
        assert out["truncated"] is False
        assert out["matches"] == [make_tiny_match(0)]

    def test_bounded_degrade_logs_an_error(self, caplog) -> None:
        matches = [make_match(i) for i in range(_MANY_MATCH_COUNT)]
        result = {"matches": matches, "evaluation_errors": []}

        with caplog.at_level(
            logging.ERROR, logger="code_indexer.server.mcp.handlers.xray_truncation"
        ):
            xt.truncate_result_fields(result, None, ["matches", "evaluation_errors"])

        assert any(
            record.levelno == logging.ERROR and "cache" in record.message.lower()
            for record in caplog.records
        )
