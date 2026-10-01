"""Regression coverage for ADR-001 legacy X-Ray front-door mode gates.

Both tests below exercise the REAL `_resolve_evaluator_code` mode-gate logic
(C3, #1858-#1861 remediation) against a genuinely stored graph-mode pattern
on disk -- never a mock of the resolver itself (Rule 1, anti-mock: the
resolver IS the system under test here).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import patch

import pytest
import yaml
from fastapi import HTTPException

from code_indexer.server.routes.xray_routes import (
    XRaySearchRequest,
    _resolve_evaluator_code_or_raise,
)


GRAPH_EVALUATOR = (
    "fn collect_facts(node: &OwnedNode, file: &str) -> Vec<UserFact> { Vec::new() }\n"
    "fn analyze_graph(g: &GraphHandle<'_>, facts: &FactsHandle<'_>) -> GraphResult { GraphResult::default() }\n"
)


def _write_graph_pattern(cidx_meta: Path, *, name: str) -> None:
    """Writes a real, on-disk graph-mode pattern YAML."""
    spec = {
        "name": name,
        "description": "graph pattern for legacy-mode-gate regression",
        "language": "java",
        "execution_mode": "graph",
        "evaluator_code": GRAPH_EVALUATOR,
    }
    path = cidx_meta / "xray-patterns" / "__any__" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")


def _assert_rest_pattern_mode_mismatch(detail: Dict[str, Any]) -> None:
    """REST's HTTPException.detail shape (`error_code`) differs from the
    MCP/batch error-dict shape (`error`) -- see
    `_resolve_evaluator_code_or_raise`'s `{"error_code": ..., "detail": ...}`
    construction vs. `_mcp_response`'s `{"error": ..., "message": ...}`.
    """
    assert detail.get("error_code") == "pattern_mode_mismatch", detail


def _assert_batch_pattern_mode_mismatch(error: Dict[str, Any]) -> None:
    assert error.get("error") == "pattern_mode_mismatch", error


def test_rest_resolver_rejects_real_graph_pattern_with_pattern_mode_mismatch(
    tmp_path: Path,
) -> None:
    """A genuinely stored graph-mode pattern requested through the REST
    front door must be rejected with a named `pattern_mode_mismatch`, not
    an opaque downstream compile/load error.
    """
    cidx_meta = tmp_path / "cidx-meta"
    _write_graph_pattern(cidx_meta, name="graph-only")

    body = XRaySearchRequest(
        repository_alias="repo-global",
        driver_regex="class",
        pattern_name="graph-only",
        search_target="content",
    )

    with (
        patch(
            "code_indexer.server.mcp.handlers.xray._infra._get_cidx_meta_path",
            return_value=cidx_meta,
        ),
        patch("code_indexer.server.mcp.handlers.xray._infra._seeds_ensured", True),
    ):
        with pytest.raises(HTTPException) as exc_info:
            _resolve_evaluator_code_or_raise(body)

    # HTTPException.detail is statically typed `str` by the Starlette/
    # FastAPI stub, but `_resolve_evaluator_code_or_raise` always passes a
    # dict at runtime (see its own {"error_code": ..., "detail": ...}
    # construction) -- cast to the real runtime shape rather than loosening
    # the assertion helper's parameter type.
    _assert_rest_pattern_mode_mismatch(cast(Dict[str, Any], exc_info.value.detail))


def test_batch_resolver_rejects_real_graph_pattern_with_pattern_mode_mismatch(
    tmp_path: Path,
) -> None:
    """A genuinely stored graph-mode pattern requested through
    xray_search_batch must be rejected with a named `pattern_mode_mismatch`,
    proving `resolve_batch_evaluator` now routes through the shared
    resolver's mode gate instead of bypassing it.
    """
    from code_indexer.server.mcp.handlers.xray_batch import resolve_batch_evaluator

    cidx_meta = tmp_path / "cidx-meta"
    _write_graph_pattern(cidx_meta, name="graph-only")

    code, error = resolve_batch_evaluator(
        {"pattern_name": "graph-only"},
        "repo-global",
        cidx_meta,
    )

    assert code == ""
    assert error is not None
    _assert_batch_pattern_mode_mismatch(error)
