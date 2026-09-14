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

from typing import Any, Dict
from unittest.mock import MagicMock, patch

# Named constants (self-documenting cache-boundary behavior) -----------------
_PREVIEW_SIZE_NEVER_TRIGGERS = 10_000  # larger than any small fixture's JSON
_PREVIEW_SIZE_LARGE_TRIGGER = 200  # smaller than the large fixture's JSON
_LARGE_FINDING_COUNT = 30  # enough findings to exceed _PREVIEW_SIZE_LARGE_TRIGGER
_INLINE_FINDING_LIMIT = 3  # matches _truncate_xray_result's own first-3 convention
_FINDING_MESSAGE_LENGTH = 80
_SIGNATURE_PADDING_LENGTH = 60
_TEST_COMPILE_MS = 42


class _FakePayloadCacheConfig:
    preview_size_chars: int = _PREVIEW_SIZE_LARGE_TRIGGER


class _FakePayloadCache:
    """Minimal fake PayloadCache with the same truncate_result contract."""

    def __init__(self, preview_size_chars: int = _PREVIEW_SIZE_LARGE_TRIGGER) -> None:
        self.config = _FakePayloadCacheConfig()
        self.config.preview_size_chars = preview_size_chars
        self._stored: Dict[str, str] = {}
        self._counter = 0

    def store(self, content: str) -> str:
        self._counter += 1
        handle = f"fake-handle-{self._counter}"
        self._stored[handle] = content
        return handle

    def truncate_result(self, content: str) -> dict:
        preview_size = self.config.preview_size_chars
        if len(content) > preview_size:
            cache_handle = self.store(content)
            return {
                "preview": content[:preview_size],
                "cache_handle": cache_handle,
                "has_more": True,
                "total_size": len(content),
            }
        return {"content": content, "cache_handle": None, "has_more": False}


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
    graph_result: Dict[str, Any], preview_size_chars: int
) -> Dict[str, Any]:
    """Shared harness: builds a fake PayloadCache with the given preview
    size, patches it onto the app singleton, and invokes
    `_truncate_graph_result` -- factored out so every test below states
    only what varies (the result fixture and the preview size)."""
    from code_indexer.server.mcp.handlers.xray_graph import _truncate_graph_result

    fake_cache = _FakePayloadCache(preview_size_chars=preview_size_chars)
    mock_state = MagicMock()
    mock_state.payload_cache = fake_cache
    mock_app = MagicMock()
    mock_app.state = mock_state

    with patch(
        "code_indexer.server.mcp.handlers._utils.app_module",
        **{"app": mock_app},
    ):
        return _truncate_graph_result(graph_result)


class TestTruncateGraphResultSmall:
    def test_small_result_returns_inline_no_cache_handle(self) -> None:
        result = _truncate_with_cache(
            _make_small_graph_result(), _PREVIEW_SIZE_NEVER_TRIGGERS
        )

        assert result.get("cache_handle") is None
        assert result.get("has_more") is False
        assert result.get("truncated") is False
        assert isinstance(result.get("findings"), list)
        assert isinstance(result.get("refine"), list)

    def test_small_result_preserves_non_findings_metadata(self) -> None:
        result = _truncate_with_cache(
            _make_small_graph_result(), _PREVIEW_SIZE_NEVER_TRIGGERS
        )

        assert result["fact_graph_complete"] is True
        assert result["build_status"] == "ok"
        assert result["compile_ms"] == _TEST_COMPILE_MS


class TestTruncateGraphResultLarge:
    def test_large_result_returns_cache_handle_and_bounds_findings(self) -> None:
        result = _truncate_with_cache(
            _make_large_graph_result(), _PREVIEW_SIZE_LARGE_TRIGGER
        )

        assert result["cache_handle"] is not None
        assert result["has_more"] is True
        assert result["truncated"] is True
        assert len(result["findings"]) == _INLINE_FINDING_LIMIT, (
            f"an oversized graph result must inline only the first "
            f"{_INLINE_FINDING_LIMIT} findings"
        )
        assert len(result["refine"]) == _INLINE_FINDING_LIMIT

    def test_large_result_preserves_completeness_metadata(self) -> None:
        """The honesty-signal fields (fact_graph_complete/degradation) must
        survive truncation unchanged -- a caller must still be able to tell
        a genuinely-clean truncated result apart from an incomplete one."""
        result = _truncate_with_cache(
            _make_large_graph_result(), _PREVIEW_SIZE_LARGE_TRIGGER
        )

        assert result["fact_graph_complete"] is True
        assert result["build_status"] == "ok"


class TestTruncateGraphResultCacheUnavailable:
    def test_cache_unavailable_returns_full_result(self) -> None:
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

        assert result == large


class TestTruncateGraphResultBoundsNestedFindingData:
    """R2-7 (Codex re-review): `_TRUNCATION_INLINE_LIMIT` bounds the OUTER
    findings/refine arrays to 3 entries, but each retained entry can
    itself carry unbounded nested `involved`/`signatures` arrays plus a
    huge `message` string. A single such finding among the first 3 must
    NOT reach the inline response unbounded."""

    def test_huge_finding_among_inlined_entries_has_nested_fields_bounded(
        self,
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

        result = _truncate_with_cache(graph_result, _PREVIEW_SIZE_LARGE_TRIGGER)

        assert result["has_more"] is True
        assert len(result["findings"]) == _INLINE_FINDING_LIMIT
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
