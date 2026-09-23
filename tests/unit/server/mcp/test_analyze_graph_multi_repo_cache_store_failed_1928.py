"""Bug #1928 final round (Opus P3.2): analyze_graph's multi-repo path
(`_run_multi_repo_analyze_graph`) must route a per-repo PayloadCache
write failure into `errors[]`, never `results[alias]`.

Before this fix, `_truncate_graph_result`'s `{"success": False, "error":
"cache_store_failed", ...}` response (Bug #1928 round 3, P1) was stored
straight into `results[alias]` -- the top-level `ok` flag stayed True
(computed as `len(errors) == 0`) even though that alias's data was NEVER
durably written, silently presenting a failure as a success.

Mocking strategy mirrors test_analyze_graph_deadline_1913.py:
`_run_analyze_graph_pipeline` is an AsyncMock (unit-tests the routing
LOGIC, not the real xray-cli pipeline). `payload_cache` is patched via
`_utils.app_module` to a real-shaped fake whose `store_batch_with_keys()`
always raises (same fake as
test_xray_truncation_round3_store_failure_wrappers_1928.py), and the
canned findings are large enough to exceed its small budget so
`_finalize_truncated`/`store_pages` actually runs.
"""

from __future__ import annotations

from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from code_indexer.server.cache.payload_cache import PayloadCacheConfig
from code_indexer.server.mcp.handlers.xray_graph import (
    _run_multi_repo_analyze_graph,
)

from ._xray_truncation_test_helpers import make_finding  # noqa: F401

_SMALL_BUDGET_CHARS = 300
_MANY_ENTRY_COUNT = 30

EVALUATOR_CODE = (
    "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n"
    "    Vec::new()\n"
    "}\n"
    "fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> "
    "GraphResult {\n"
    "    GraphResult::default()\n"
    "}\n"
)


class _FailingStoreBatchCache:
    """A real-shaped fake whose store_batch_with_keys() always raises --
    same pattern as test_xray_truncation_round3_store_failure_wrappers_1928.py."""

    def __init__(self, max_fetch_size_chars: int) -> None:
        self.config = PayloadCacheConfig(
            preview_size_chars=max_fetch_size_chars,
            max_fetch_size_chars=max_fetch_size_chars,
        )

    def store_batch_with_keys(self, items: List[Tuple[str, str]]) -> None:
        raise RuntimeError("simulated page-set write failure")


@pytest.mark.asyncio
async def test_per_repo_cache_store_failure_routes_to_errors_not_results() -> None:
    good_result = {"ok": True, "status": "ran_ok", "findings": [], "refine": []}
    large_findings = [make_finding(i) for i in range(_MANY_ENTRY_COUNT)]
    fail_result = {
        "ok": True,
        "status": "ran_ok",
        "findings": large_findings,
        "refine": [],
    }

    async def _fake_pipeline(evaluator_code, alias, *args, **kwargs):
        return good_result if alias == "repo-good" else fail_result

    cache = _FailingStoreBatchCache(_SMALL_BUDGET_CHARS)
    mock_app = MagicMock()
    mock_app.state.payload_cache = cache

    with (
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._run_analyze_graph_pipeline",
            new=AsyncMock(side_effect=_fake_pipeline),
        ),
        patch(
            "code_indexer.server.mcp.handlers._utils.app_module", **{"app": mock_app}
        ),
    ):
        result = await _run_multi_repo_analyze_graph(
            ["repo-good", "repo-fail"],
            EVALUATOR_CODE,
            [],
            [],
            timeout_seconds=120,
            refine=False,
        )

    assert result["ok"] is False, f"Expected ok=False, got: {result}"
    assert "repo-good" in result["results"]
    assert "repo-fail" not in result["results"], (
        "a cache_store_failed truncation must never land in results[alias]"
    )
    failing_entries = [
        e for e in result["errors"] if e.get("repository_alias") == "repo-fail"
    ]
    assert len(failing_entries) == 1, f"Expected exactly one error entry: {result}"
    assert failing_entries[0]["error"] == "cache_store_failed"
    assert failing_entries[0].get("message")

    # Completeness invariant the docstring promises:
    # len(results) + len(errors) == len(repositories)
    assert len(result["results"]) + len(result["errors"]) == len(result["repositories"])
