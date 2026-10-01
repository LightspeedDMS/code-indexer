"""Consolidated review finding H7 (Issue #1811/Bug #1812).

`handlers/xray.py`'s `_truncate_xray_result` routes `xray_search`/
`xray_explore` results through `PayloadCache` so an oversized
`matches`/`evaluation_errors` payload is cached and paged instead of
returned raw. `handlers/xray_graph.py`'s `handle_analyze_graph` returns
`findings`/`refine` with NO equivalent bounding -- a whole-repo graph
analysis produces STRICTLY MORE output than a single-file search (every
`ReduceFinding` carries `involved` plus a parallel `signatures` array of
full declaration lines), so a dead-code sweep over a large repo can be a
multi-megabyte MCP response.

This test proves `_truncate_graph_result` (the new graph-shaped
equivalent of `_truncate_xray_result`) is available from
`handlers.xray_graph` -- the module that actually uses it, mirroring that
module's existing convention of importing shared xray.py helpers (it
already does `from .xray import _resolve_repo_path`) -- and truncates
`findings`/`refine` the same way `_truncate_xray_result` truncates
`matches`/`evaluation_errors`. The actual wiring into `handle_analyze_graph`
is proven separately in test_analyze_graph_handler.py.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

# Named constants (self-documenting cache-boundary behavior) -----------------
_PREVIEW_SIZE_NEVER_TRIGGERS = 10_000  # larger than any small fixture's JSON
_PREVIEW_SIZE_LARGE_TRIGGER = 200  # smaller than the large fixture's JSON
_LARGE_FINDING_COUNT = 30  # enough findings to exceed _PREVIEW_SIZE_LARGE_TRIGGER
# Bug #1928: inline count is now byte-budget-driven, never a fixed count --
# this budget is large enough that several (but not all) findings fit
# VERBATIM, unlike _PREVIEW_SIZE_LARGE_TRIGGER (200), which is smaller than
# even a single finding entry and would trip the single-entry adaptive
# shrink instead of exercising ordinary multi-entry packing.
_PREVIEW_SIZE_MULTI_ENTRY_TRIGGER = 1000
_FINDING_MESSAGE_LENGTH = 80
_SIGNATURE_PADDING_LENGTH = 60
_TEST_COMPILE_MS = 42
_CACHE_TTL_SECONDS = 900
_CACHE_CLEANUP_INTERVAL_SECONDS = 60


def _make_real_cache(tmp_path, max_fetch_size_chars: int):
    """A REAL, on-disk PayloadCache. Bug #1928 rework: there is now a
    SINGLE budget (max_fetch_size_chars) -- xray_truncation.truncate_result_fields()
    reads ONLY config.max_fetch_size_chars, never a separate
    preview_size_chars, and calls the real PayloadCache.store_batch()
    (Bug #1181 one-transaction-per-batch, one row per page) plus store()
    for the pages-v1 manifest once truncation genuinely triggers -- a fake
    that only overrides preview_size_chars silently never truncates at
    all, and a fake lacking store_batch() cannot exercise a real
    truncation call anyway. Matches the real-cache pattern used across
    tests/unit/server/mcp/test_xray_truncation_*_1928.py."""
    from code_indexer.server.cache.payload_cache import (
        PayloadCache,
        PayloadCacheConfig,
    )

    config = PayloadCacheConfig(
        preview_size_chars=max_fetch_size_chars,
        max_fetch_size_chars=max_fetch_size_chars,
        cache_ttl_seconds=_CACHE_TTL_SECONDS,
        cleanup_interval_seconds=_CACHE_CLEANUP_INTERVAL_SECONDS,
    )
    cache = PayloadCache(db_path=tmp_path / "payload_cache.db", config=config)
    cache.initialize()
    return cache


def _make_finding(pattern: str) -> Dict[str, Any]:
    return {
        "pattern": pattern,
        "message": "x" * _FINDING_MESSAGE_LENGTH,
        "involved": [1, 2, 3],
        "signatures": ["fn signature line here " + "y" * _SIGNATURE_PADDING_LENGTH],
    }


# R2-7 (Codex re-review): a single retained finding can itself be huge --
# _TRUNCATION_INLINE_LIMIT bounds the OUTER array to 3 entries, but says
# nothing about what's INSIDE each of those 3.
_HUGE_INVOLVED_COUNT = 500
_HUGE_SIGNATURES_COUNT = 200
_HUGE_MESSAGE_LENGTH = 20_000


def _make_finding_with_huge_nested_fields(pattern: str) -> Dict[str, Any]:
    return {
        "pattern": pattern,
        "message": "m" * _HUGE_MESSAGE_LENGTH,
        "involved": list(range(_HUGE_INVOLVED_COUNT)),
        "signatures": [
            f"fn sig_{i}(x: i32) -> i32" for i in range(_HUGE_SIGNATURES_COUNT)
        ],
    }


def _make_small_graph_result() -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "ran_ok",
        "findings": [_make_finding("dead_code")],
        "refine": [],
        "fact_graph_complete": True,
        "build_status": "ok",
        "degradation": {"files_with_parse_errors": 0},
        "cached": False,
        "compile_ms": _TEST_COMPILE_MS,
    }


def _make_large_graph_result(
    n_findings: int = _LARGE_FINDING_COUNT,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "ran_ok",
        "findings": [_make_finding(f"pattern_{i}") for i in range(n_findings)],
        "refine": list(range(n_findings)),
        "fact_graph_complete": True,
        "build_status": "ok",
        "degradation": {"files_with_parse_errors": 0},
        "cached": False,
        "compile_ms": _TEST_COMPILE_MS,
    }


def _truncate_with_cache(
    graph_result: Dict[str, Any], budget_chars: int, tmp_path
) -> Dict[str, Any]:
    """Shared harness: builds a REAL on-disk PayloadCache with the given
    single byte budget (max_fetch_size_chars), patches it onto the app
    singleton, invokes `_truncate_graph_result`, and closes the cache --
    factored out so every test below states only what varies (the result
    fixture and the budget)."""
    from code_indexer.server.mcp.handlers.xray_graph import _truncate_graph_result

    cache = _make_real_cache(tmp_path, max_fetch_size_chars=budget_chars)
    try:
        mock_state = MagicMock()
        mock_state.payload_cache = cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            return _truncate_graph_result(graph_result)
    finally:
        cache.close()


class TestTruncateGraphResultSmall:
    def test_small_result_returns_inline_no_cache_handle(self, tmp_path) -> None:
        result = _truncate_with_cache(
            _make_small_graph_result(), _PREVIEW_SIZE_NEVER_TRIGGERS, tmp_path
        )

        assert result.get("cache_handle") is None
        assert result.get("has_more") is False
        assert result.get("truncated") is False
        assert isinstance(result.get("findings"), list)
        assert isinstance(result.get("refine"), list)

    def test_small_result_preserves_non_findings_metadata(self, tmp_path) -> None:
        result = _truncate_with_cache(
            _make_small_graph_result(), _PREVIEW_SIZE_NEVER_TRIGGERS, tmp_path
        )

        assert result["fact_graph_complete"] is True
        assert result["build_status"] == "ok"
        assert result["compile_ms"] == _TEST_COMPILE_MS


class TestTruncateGraphResultLarge:
    def test_large_result_returns_cache_handle_and_bounds_findings(
        self, tmp_path
    ) -> None:
        """Bug #1928: inline findings/refine are a whole-entry prefix
        bounded by the byte budget, never a fixed count."""
        large = _make_large_graph_result()
        result = _truncate_with_cache(
            large, _PREVIEW_SIZE_MULTI_ENTRY_TRIGGER, tmp_path
        )

        assert result["cache_handle"] is not None
        assert result["has_more"] is True
        assert result["truncated"] is True
        n_inline = len(result["findings"])
        assert 0 < n_inline < _LARGE_FINDING_COUNT, (
            "an oversized graph result must inline a genuine partial "
            "prefix, governed by the byte budget -- not all, not none, "
            "and never a hardcoded count of 3"
        )
        assert result["findings"] == large["findings"][:n_inline]
        assert len(result["refine"]) <= n_inline
        # The budget is a Python str char count (PayloadCache
        # stores/slices `str`, never bytes), but we encode to UTF-8 here
        # too so this assertion holds even if the fixture ever grows
        # non-ASCII content.
        inline_size = len(
            json.dumps(
                {"findings": result["findings"], "refine": result["refine"]}
            ).encode("utf-8")
        )
        assert inline_size <= _PREVIEW_SIZE_MULTI_ENTRY_TRIGGER

    def test_large_result_preserves_completeness_metadata(self, tmp_path) -> None:
        """The honesty-signal fields (fact_graph_complete/degradation) must
        survive truncation unchanged -- a caller must still be able to tell
        a genuinely-clean truncated result apart from an incomplete one."""
        result = _truncate_with_cache(
            _make_large_graph_result(), _PREVIEW_SIZE_MULTI_ENTRY_TRIGGER, tmp_path
        )

        assert result["fact_graph_complete"] is True
        assert result["build_status"] == "ok"


class TestTruncateGraphResultCacheUnavailable:
    def test_cache_unavailable_bounds_a_large_result(self) -> None:
        """Bug #1928 round 3 (P3, Codex): a large result must be BOUNDED
        when the cache is unavailable, never returned in full."""
        from code_indexer.server.mcp.handlers.xray_graph import (
            _truncate_graph_result,
        )

        mock_state = MagicMock(spec=[])  # no payload_cache attribute
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_graph_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            result = _truncate_graph_result(large)

        assert result["cache_unavailable"] is True
        assert result["truncated"] is True
        assert result["cache_handle"] is None
        assert len(result["findings"]) < len(large["findings"]), (
            "cache-unavailable degrade must still BOUND the result -- "
            "returning everything defeats the whole truncation contract"
        )


class TestTruncateGraphResultBoundsNestedFindingData:
    """R2-7 (Codex re-review): `_TRUNCATION_INLINE_LIMIT` bounds the OUTER
    findings/refine arrays to 3 entries, but each retained entry can
    itself carry unbounded nested `involved`/`signatures` arrays plus a
    huge `message` string. A single such finding among the first 3 must
    NOT reach the inline response unbounded."""

    def test_huge_finding_among_inlined_entries_has_nested_fields_bounded(
        self, tmp_path
    ) -> None:
        findings = [_make_finding_with_huge_nested_fields("huge_one")] + [
            _make_finding(f"pattern_{i}") for i in range(_LARGE_FINDING_COUNT)
        ]
        graph_result: Dict[str, Any] = {
            "ok": True,
            "status": "ran_ok",
            "findings": findings,
            "refine": [],
            "fact_graph_complete": True,
            "build_status": "ok",
            "degradation": {"files_with_parse_errors": 0},
            "cached": False,
            "compile_ms": _TEST_COMPILE_MS,
        }

        result = _truncate_with_cache(
            graph_result, _PREVIEW_SIZE_LARGE_TRIGGER, tmp_path
        )

        assert result["has_more"] is True
        assert len(result["findings"]) >= 1, (
            "the adaptive shrink (Bug #1928) must get the huge first "
            "finding to fit rather than excluding it -- 200 chars is "
            "above its structural floor"
        )
        huge_finding_inline = result["findings"][0]
        assert huge_finding_inline["pattern"] == "huge_one"
        assert len(huge_finding_inline["involved"]) < _HUGE_INVOLVED_COUNT, (
            "a single finding's 'involved' array must be bounded even when "
            "it survives outer-array truncation into the inline preview"
        )
        assert len(huge_finding_inline["signatures"]) < _HUGE_SIGNATURES_COUNT, (
            "a single finding's 'signatures' array must be bounded even "
            "when it survives outer-array truncation into the inline preview"
        )
        assert len(huge_finding_inline["message"]) < _HUGE_MESSAGE_LENGTH, (
            "a single finding's 'message' string must be bounded even when "
            "it survives outer-array truncation into the inline preview"
        )
