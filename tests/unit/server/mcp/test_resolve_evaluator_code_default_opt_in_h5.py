"""Consolidated review finding H5 (Issue #1811/Bug #1812).

``handlers.xray._resolve_evaluator_code`` silently substitutes
``_DEFAULT_EVALUATOR_CODE`` and reports SUCCESS whenever neither
``pattern_name`` nor ``evaluator_code`` is supplied (Rule 2, anti-fallback).
That silent default is a genuinely documented, intentional feature for the
two MCP handlers that call it (``xray_search``/``xray_explore`` -- see their
own tool_docs: "When omitted, the server substitutes a default that
produces one finding per Phase 1 hit"). But the function offered no way to
OPT OUT of that fallback, so the REST route (``xray_routes.py``) -- which
never wants the silent default -- had to duplicate its own hand-rolled
pre-check (``has_pattern_name``/``has_evaluator_code``) that must
independently agree with this function forever (Rule 4, anti-duplication).

The fix moves the "reject when both are absent" rule INTO
``_resolve_evaluator_code`` itself via an explicit ``allow_default_evaluator``
opt-in (default ``False`` -- safe by default), and the two MCP call sites
that genuinely want the fallback pass ``allow_default_evaluator=True``
explicitly. The REST route's own duplicate check is then deleted -- it no
longer needs to agree with anything, because the shared function is now the
single source of truth.
"""

from __future__ import annotations

import json
from typing import Any, Dict, cast

import pytest


def _import_resolve_evaluator_code():
    from code_indexer.server.mcp.handlers.xray import _resolve_evaluator_code

    return _resolve_evaluator_code


def _err_dict(err_resp: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the ``_mcp_response`` envelope's inner JSON error payload.

    Typed loosely (``Dict[str, Any]``) deliberately: this is a test-only
    helper over a generic MCP error envelope whose inner shape legitimately
    varies per error type (``error``, plus an error-specific ``message``/
    ``offending_construct``/etc.) -- there is no single production contract
    to type this against, only the one field (``error``) every case shares,
    which callers read explicitly.
    """
    return cast(Dict[str, Any], json.loads(err_resp["content"][0]["text"]))


class TestDefaultEvaluatorIsOptIn:
    def test_neither_pattern_name_nor_evaluator_code_is_rejected_by_default(
        self,
    ) -> None:
        """Without an explicit opt-in, the shared resolver must reject a
        request providing neither pattern_name nor evaluator_code -- never
        silently substitute the default evaluator. This is the safe-by-
        default behavior the REST route needs without any local
        duplicate check of its own."""
        resolve = _import_resolve_evaluator_code()

        evaluator_code, err_resp = resolve({}, "some-repo")

        assert evaluator_code == ""
        assert err_resp is not None, (
            "a plain call with neither pattern_name nor evaluator_code, and "
            "no opt-in, must return a structured error -- not silently "
            "resolve to the default evaluator"
        )
        assert _err_dict(err_resp)["error"] == "evaluator_code_required"

    def test_explicit_opt_in_preserves_the_default_evaluator_fallback(
        self,
    ) -> None:
        """The two MCP handlers that genuinely document this fallback
        (xray_search, xray_explore) must still get it when they pass
        allow_default_evaluator=True."""
        from code_indexer.server.mcp.handlers.xray import _DEFAULT_EVALUATOR_CODE

        resolve = _import_resolve_evaluator_code()

        evaluator_code, err_resp = resolve(
            {}, "some-repo", allow_default_evaluator=True
        )

        assert err_resp is None
        assert evaluator_code == _DEFAULT_EVALUATOR_CODE

    def test_explicit_evaluator_code_still_wins_regardless_of_opt_in(
        self,
    ) -> None:
        """A caller-supplied evaluator_code must always be used verbatim,
        whether or not allow_default_evaluator is set -- the opt-in only
        governs the "neither given" fallback."""
        resolve = _import_resolve_evaluator_code()
        custom_code = (
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }"
        )

        for allow in (False, True):
            evaluator_code, err_resp = resolve(
                {"evaluator_code": custom_code},
                "some-repo",
                allow_default_evaluator=allow,
            )
            assert err_resp is None
            assert evaluator_code == custom_code


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
