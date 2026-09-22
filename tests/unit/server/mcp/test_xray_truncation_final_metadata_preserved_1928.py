"""Bug #1928 final round (Opus P4.6): a truncation-time cache-store
failure must NOT replace the whole result with a bare
{"success": False, "error": ...} -- it must keep every non-truncated
metadata field (fact_graph_complete, ok, degradation, cached,
compile_ms, status, ...) alongside success:false/error, so a caller
that only reads THOSE fields (never findings/refine, which genuinely
cannot be delivered since the write failed) is not needlessly starved
of information the analysis already produced.

Same failure-injection fake as
test_xray_truncation_round3_store_failure_wrappers_1928.py.
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


class TestAnalyzeGraphWrapperPreservesMetadataOnStoreFailure:
    def test_fact_graph_complete_and_ok_survive_the_store_failure(self) -> None:
        from code_indexer.server.mcp.handlers.xray_graph import (
            _truncate_graph_result,
        )

        cache = _FailingStoreBatchCache(_SMALL_BUDGET_CHARS)
        mock_app = MagicMock()
        mock_app.state.payload_cache = cache
        findings = [make_finding(i) for i in range(_MANY_ENTRY_COUNT)]
        result = {
            "ok": True,
            "status": "ran_ok",
            "findings": findings,
            "refine": [],
            "fact_graph_complete": True,
            "build_status": "ok",
            "degradation": {"files_with_parse_errors": 0},
            "cached": True,
            "compile_ms": 42,
        }

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module", **{"app": mock_app}
        ):
            out = _truncate_graph_result(result)

        assert out["success"] is False
        assert out["error"] == "cache_store_failed"
        assert out.get("ok") is True, f"metadata dropped: {out}"
        assert out.get("fact_graph_complete") is True, f"metadata dropped: {out}"
        assert out.get("build_status") == "ok"
        assert out.get("degradation") == {"files_with_parse_errors": 0}
        assert out.get("cached") is True
        assert out.get("compile_ms") == 42


class TestXraySearchWrapperPreservesMetadataOnStoreFailure:
    def test_arbitrary_base_metadata_survives_the_store_failure(self) -> None:
        from code_indexer.server.mcp.handlers.xray import _truncate_xray_result

        cache = _FailingStoreBatchCache(_SMALL_BUDGET_CHARS)
        mock_app = MagicMock()
        mock_app.state.payload_cache = cache
        matches = [make_match(i) for i in range(_MANY_ENTRY_COUNT)]
        result = {
            "matches": matches,
            "evaluation_errors": [],
            "repository_alias": "some-repo",
            "pattern_name": "some-pattern",
        }

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module", **{"app": mock_app}
        ):
            out = _truncate_xray_result(result)

        assert out["success"] is False
        assert out["error"] == "cache_store_failed"
        assert out.get("repository_alias") == "some-repo"
        assert out.get("pattern_name") == "some-pattern"
