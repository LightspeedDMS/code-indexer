"""Bug #1928 round 3 (P1 -- Codex REJECT): "the xray caller surfaces a
store failure as an explicit error, not a handle." All three thin
wrapper functions (xray._truncate_xray_result, xray_graph._truncate_graph_result,
xray_batch._truncate_xray_batch_result) must catch PageSetStoreError
raised by the shared xray_truncation.truncate_result_fields() and
surface it as an explicit {"success": False, "error": "cache_store_failed"}
response -- never let the raw exception propagate uncaught, and never
return a cache_handle for data that was not durably written.

Failure injected via a real-shaped fake cache whose store_batch_with_keys()
raises a plain RuntimeError -- simulating a real backend/connection
failure at the external-dependency boundary (same pattern as
test_xray_truncation_round3_store_failure_1928.py and the backend-level
tests in tests/unit/storage/test_payload_cache_backend_store_batch_strict_1928.py).
xray_truncation_fetch.py's store_pages() already wraps ANY such failure
into PageSetStoreError before it reaches these wrappers -- so the
wrappers only ever need to catch PageSetStoreError, which is exactly
what's under test here.
"""

from __future__ import annotations

from typing import List, Tuple
from unittest.mock import MagicMock, patch

from code_indexer.server.cache.payload_cache import PayloadCacheConfig

from ._xray_truncation_test_helpers import make_finding, make_match  # noqa: F401

_SMALL_BUDGET_CHARS = 300
_MANY_ENTRY_COUNT = 30


class _FailingStoreBatchCache:
    """A real-shaped fake whose store_batch_with_keys() always raises."""

    def __init__(self, max_fetch_size_chars: int) -> None:
        self.config = PayloadCacheConfig(
            preview_size_chars=max_fetch_size_chars,
            max_fetch_size_chars=max_fetch_size_chars,
        )

    def store_batch_with_keys(self, items: List[Tuple[str, str]]) -> None:
        raise RuntimeError("simulated page-set write failure")


class TestXraySearchWrapperSurfacesStoreFailure:
    """_truncate_xray_result reads payload_cache from app.state itself
    (via _lazy_singleton_app_or_none()), so it is patched via app_module."""

    def test_truncate_xray_result_returns_explicit_error(self) -> None:
        from code_indexer.server.mcp.handlers.xray import _truncate_xray_result

        cache = _FailingStoreBatchCache(_SMALL_BUDGET_CHARS)
        mock_app = MagicMock()
        mock_app.state.payload_cache = cache
        matches = [make_match(i) for i in range(_MANY_ENTRY_COUNT)]
        result = {"matches": matches, "evaluation_errors": []}

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module", **{"app": mock_app}
        ):
            out = _truncate_xray_result(result)

        assert out["success"] is False
        assert out["error"] == "cache_store_failed"
        assert "cache_handle" not in out


class TestAnalyzeGraphWrapperSurfacesStoreFailure:
    """_truncate_graph_result reads payload_cache from app.state itself
    (same pattern as _truncate_xray_result above)."""

    def test_truncate_graph_result_returns_explicit_error(self) -> None:
        from code_indexer.server.mcp.handlers.xray_graph import (
            _truncate_graph_result,
        )

        cache = _FailingStoreBatchCache(_SMALL_BUDGET_CHARS)
        mock_app = MagicMock()
        mock_app.state.payload_cache = cache
        findings = [make_finding(i) for i in range(_MANY_ENTRY_COUNT)]
        result = {"findings": findings, "refine": []}

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module", **{"app": mock_app}
        ):
            out = _truncate_graph_result(result)

        assert out["success"] is False
        assert out["error"] == "cache_store_failed"
        assert "cache_handle" not in out


class TestXraySearchBatchWrapperSurfacesStoreFailure:
    """_truncate_xray_batch_result has a DIFFERENT calling convention from
    the other two wrappers: its real signature is
    `_truncate_xray_batch_result(result, payload_cache)` -- payload_cache
    is passed explicitly by its one caller (_job_fn, which already reads
    it from app-state itself before calling this), so no app_module
    patching is needed here -- the cache is just passed directly."""

    def test_truncate_xray_batch_result_returns_explicit_error(self) -> None:
        from code_indexer.server.mcp.handlers.xray_batch import (
            _truncate_xray_batch_result,
        )

        cache = _FailingStoreBatchCache(_SMALL_BUDGET_CHARS)
        matches = [make_match(i) for i in range(_MANY_ENTRY_COUNT)]
        result = {"matches": matches, "errors": [], "evaluation_errors": []}

        out = _truncate_xray_batch_result(result, cache)

        assert out["success"] is False
        assert out["error"] == "cache_store_failed"
        assert "cache_handle" not in out
