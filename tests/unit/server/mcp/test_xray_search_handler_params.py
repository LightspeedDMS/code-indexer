"""xray_search handler tests — renamed params: pattern/driver_regex (Bug #1070 async rewrite).

Covers TestXraySearchHandlerRenamedParams:
  'pattern' accepted, 'driver_regex' rejected, pattern forwarded to engine.

Single-repo path (Bug #1070): job_fn captured from loop.run_in_executor — NO args.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

from .test_xray_search_handler import (
    _import_handler,
    _make_user,
    _parse_response,
    _xray_single_repo_env,
)
from code_indexer.server.auth.user_manager import UserRole


async def _capture_engine_kwargs(params: Dict[str, Any]) -> Dict[str, Any]:
    """Run handler with given params; capture kwargs passed to XRaySearchEngine.run().

    Extracts job_fn from loop.run_in_executor call (single-repo Bug #1070 path),
    then executes it with a mocked engine.
    """
    user = _make_user(UserRole.NORMAL_USER)
    captured: Dict[str, Any] = {}
    with patch(
        "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
        return_value="/some/path",
    ):
        with _xray_single_repo_env() as (_bjm, _jt, _exec, mock_loop):
            await _import_handler()(params, user)
            job_fn = mock_loop.run_in_executor.call_args[0][1]
            with patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured.update(kw)
                or {
                    "matches": [],
                    "evaluation_errors": [],
                    "files_processed": 0,
                    "files_total": 0,
                    "elapsed_seconds": 0.0,
                },
            ):
                job_fn()  # NO arguments — single-repo path (Bug #1070)
    return captured


# ---------------------------------------------------------------------------
# Tests: renamed params — pattern (was driver_regex)
# ---------------------------------------------------------------------------

_VALID_EVAL = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"


class TestXraySearchHandlerRenamedParams:
    """'pattern' is the accepted name; 'driver_regex' must be rejected."""

    async def test_pattern_param_accepted(self):
        """'pattern' key is accepted; handler returns job_id."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
            "evaluator_code": _VALID_EVAL,
            "search_target": "content",
        }
        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            with _xray_single_repo_env():
                result = await _import_handler()(params, user)
        data = _parse_response(result)
        assert "job_id" in data and "error" not in data

    async def test_driver_regex_no_longer_accepted(self):
        """'driver_regex' (old name) without 'pattern' must not produce a job_id."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "driver_regex": r"prepareStatement",
            "evaluator_code": _VALID_EVAL,
            "search_target": "content",
        }
        result = await _import_handler()(params, user)
        data = _parse_response(result)
        assert "job_id" not in data

    async def test_pattern_forwarded_to_engine_as_driver_regex(self):
        """'pattern' value reaches engine.run() as driver_regex kwarg."""
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"mySpecialRegex",
            "evaluator_code": _VALID_EVAL,
            "search_target": "content",
        }
        captured = await _capture_engine_kwargs(params)
        assert captured.get("driver_regex") == r"mySpecialRegex", (
            f"Engine must receive pattern as driver_regex, got: {captured!r}"
        )


# ---------------------------------------------------------------------------
# Tests: max_results rename (was max_files)
# ---------------------------------------------------------------------------

from .test_xray_search_handler import VALID_PARAMS  # noqa: E402


class TestXraySearchHandlerMaxResults:
    """max_results is the accepted name; old max_files is silently ignored."""

    async def test_max_results_accepted(self):
        """max_results=10 is accepted; handler returns job_id."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": 10}
        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            with _xray_single_repo_env():
                result = await _import_handler()(params, user)
        data = _parse_response(result)
        assert "job_id" in data

    async def test_max_results_zero_rejected(self):
        """max_results=0 is rejected with max_results_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": 0}
        result = await _import_handler()(params, user)
        assert _parse_response(result).get("error") == "max_results_out_of_range"

    async def test_max_results_forwarded_to_engine_as_max_files(self):
        """max_results=7 reaches engine.run() as max_files=7."""
        captured = await _capture_engine_kwargs({**VALID_PARAMS, "max_results": 7})
        assert captured.get("max_files") == 7, (
            f"Engine must receive max_results as max_files=7, got: {captured!r}"
        )

    async def test_old_max_files_not_forwarded_as_max_files_value(self):
        """max_files=99 (old key) is silently ignored; engine must not receive 99."""
        captured = await _capture_engine_kwargs({**VALID_PARAMS, "max_files": 99})
        assert captured.get("max_files") != 99, (
            f"Old max_files=99 must not be forwarded to engine, got: {captured!r}"
        )


# ---------------------------------------------------------------------------
# Tests: new params aligned to regex_search
# ---------------------------------------------------------------------------

import pytest as _pytest  # noqa: E402

# Out-of-range context_lines boundary values
_CONTEXT_LINES_BELOW_MIN = -1
_CONTEXT_LINES_ABOVE_MAX = 11


class TestXraySearchHandlerNewParams:
    """New params added for regex_search alignment: case_sensitive, context_lines,
    multiline, pcre2, path. All forwarded to engine; context_lines range enforced."""

    @_pytest.mark.parametrize(
        "override, kwarg, expected",
        [
            ({"case_sensitive": True}, "case_sensitive", True),
            ({"case_sensitive": False}, "case_sensitive", False),
            ({"context_lines": 5}, "context_lines", 5),
            ({"multiline": True}, "multiline", True),
            ({"pcre2": True}, "pcre2", True),
            ({"path": "src/"}, "path", "src/"),
        ],
        ids=[
            "case_sensitive_true",
            "case_sensitive_false",
            "context_lines_5",
            "multiline_true",
            "pcre2_true",
            "path_src",
        ],
    )
    async def test_param_forwarded_to_engine(
        self, override: dict, kwarg: str, expected: object
    ) -> None:
        """Provided param value reaches XRaySearchEngine.run() as the correct kwarg."""
        captured = await _capture_engine_kwargs({**VALID_PARAMS, **override})
        assert captured.get(kwarg) == expected, (
            f"Engine must receive {kwarg}={expected!r}, got: {captured!r}"
        )

    @_pytest.mark.parametrize(
        "kwarg, default",
        [
            ("case_sensitive", True),
            ("context_lines", 0),
            ("multiline", False),
            ("pcre2", False),
            ("path", None),
        ],
        ids=[
            "case_sensitive_default",
            "context_lines_default",
            "multiline_default",
            "pcre2_default",
            "path_default",
        ],
    )
    async def test_param_default_forwarded(self, kwarg: str, default: object) -> None:
        """Omitted params reach engine with the documented default value."""
        captured = await _capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get(kwarg) == default, (
            f"Default {kwarg} must be {default!r}, got: {captured!r}"
        )

    @_pytest.mark.parametrize(
        "value",
        [_CONTEXT_LINES_BELOW_MIN, _CONTEXT_LINES_ABOVE_MAX],
        ids=["below_min", "above_max"],
    )
    async def test_context_lines_out_of_range_rejected(self, value: int) -> None:
        """context_lines outside [0, 10] is rejected with an error."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "context_lines": value}
        result = await _import_handler()(params, user)
        data = _parse_response(result)
        assert "error" in data, f"context_lines={value} must be rejected, got: {data!r}"
