"""Consolidated review finding H8 (Issue #1811/Bug #1812, Codex).

`handlers/xray.py`'s xray_search/xray_explore/xray_search_batch cells all
acquire the shared X-Ray cell limiter (`_get_xray_cell_limiter()`) before
doing their real work, and release it in a `finally`. `handlers/
xray_graph.py`'s `_run_analyze_graph_pipeline` never acquires it at all --
concurrent whole-repo graph analyses can each allocate a full
candidate-path list, graph+facts files, Rust graph memory, rayon workers
and compiler subprocesses with zero admission control, violating the
~900-repo production-scale invariant (node-level memory/CPU exhaustion
risk).

These tests prove: (1) when the limiter denies a slot (queue timeout), the
pipeline returns a structured `xray_cell_queue_timeout` error and NEVER
invokes the real (expensive) backend call; (2) when the limiter grants a
slot, it is acquired exactly once and released exactly once even though
the backend call succeeds.

`_get_xray_cell_limiter` is patched with `create=True`: it does not exist
in `handlers.xray_graph`'s namespace yet (that is precisely the gap this
finding reports) -- the GREEN fix imports it there. Using `create=True`
keeps the RED failure diagnostic (the real assertions below fail because
admission control is genuinely missing) instead of an unrelated
AttributeError from `patch()` itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest


def _import_pipeline():
    from code_indexer.server.mcp.handlers.xray_graph import (
        _run_analyze_graph_pipeline,
    )

    return _run_analyze_graph_pipeline


VALID_EVALUATOR = (
    "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> {\n"
    "    Vec::new()\n"
    "}\n"
    "fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult {\n"
    "    GraphResult::default()\n"
    "}\n"
)


def _write_fixture_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "A.java").write_text("class A {}\n")
    return repo_root


@pytest.mark.asyncio
async def test_denied_slot_returns_structured_error_and_never_calls_backend(
    tmp_path: Path,
) -> None:
    """A limiter that denies admission (acquire() -> False) must short-circuit
    the pipeline with a structured error -- the expensive
    RustNativeBackend.run_graph_analysis call must NEVER be reached."""
    pipeline = _import_pipeline()
    repo_root = _write_fixture_repo(tmp_path)

    denying_limiter = MagicMock()
    denying_limiter.acquire.return_value = False

    with (
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
            return_value=str(repo_root),
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._get_xray_cell_limiter",
            return_value=denying_limiter,
            create=True,
        ),
        patch(
            "code_indexer.xray.rust_backend.RustNativeBackend.run_graph_analysis"
        ) as mock_run,
    ):
        result: Dict[str, Any] = await pipeline(
            VALID_EVALUATOR, "myrepo-global", [], [], 30
        )

    mock_run.assert_not_called()
    denying_limiter.release.assert_not_called()
    assert result.get("error") == "xray_cell_queue_timeout", f"got: {result}"


@pytest.mark.asyncio
async def test_granted_slot_is_acquired_once_and_released_once(
    tmp_path: Path,
) -> None:
    """A limiter that grants admission must be acquired exactly once and
    released exactly once, even though the backend call itself succeeds."""
    pipeline = _import_pipeline()
    repo_root = _write_fixture_repo(tmp_path)

    granting_limiter = MagicMock()
    granting_limiter.acquire.return_value = True

    fake_backend_result = {
        "ok": True,
        "status": "ran_ok",
        "findings": [],
        "refine": [],
        "fact_graph_complete": True,
        "build_status": "ok",
        "degradation": {},
        "cached": False,
        "compile_ms": 1,
    }

    with (
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._resolve_repo_path",
            return_value=str(repo_root),
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray_graph._get_xray_cell_limiter",
            return_value=granting_limiter,
            create=True,
        ),
        patch(
            "code_indexer.xray.rust_backend.RustNativeBackend.run_graph_analysis",
            return_value=fake_backend_result,
        ) as mock_run,
    ):
        result = await pipeline(VALID_EVALUATOR, "myrepo-global", [], [], 30)

    mock_run.assert_called_once()
    granting_limiter.acquire.assert_called_once()
    granting_limiter.release.assert_called_once()
    assert result == fake_backend_result
