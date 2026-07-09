"""xray_explore handler tests: optional evaluator, matched_node, renamed/new params.

Split from test_xray_explore_handler.py to keep modules under 500 lines.
Mocking strategy: Bug #1070 async path with _xray_single_repo_env() context manager.
Rejection-path tests only patch _resolve_repo_path (validation short-circuits
before the async infra is needed — consistent with the core test file pattern).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Optional, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Shared infrastructure (local copy — no cross-module imports)
# ---------------------------------------------------------------------------


@contextmanager
def _xray_single_repo_env(
    resolved_future: "Optional[asyncio.Future]" = None,
) -> Generator:
    """Patch infra for the single-repo Bug #1070 async path."""
    mock_bjm = MagicMock()
    mock_jt = MagicMock()
    mock_jt.register_job.return_value = MagicMock()
    mock_exec = MagicMock()
    mock_app = MagicMock()
    mock_app.background_job_manager = mock_bjm
    mock_app.activated_repo_manager = None
    mock_app.golden_repo_manager = None

    if resolved_future is None:
        resolved_future = asyncio.Future()

    loop_instance = MagicMock()
    loop_instance.run_in_executor.return_value = resolved_future

    with (
        patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
        patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/fake/repo/path",
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_job_tracker",
            return_value=mock_jt,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_xray_executor",
            return_value=mock_exec,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray.validate_rust_evaluator"
        ) as mock_validate,
        patch("asyncio.get_running_loop", return_value=loop_instance),
    ):
        mock_validate.return_value = MagicMock(ok=True)
        yield mock_bjm, mock_jt, mock_exec, loop_instance


_NOOP_ENGINE_RESULT: Dict[str, Any] = {
    "matches": [],
    "evaluation_errors": [],
    "files_processed": 0,
    "files_total": 0,
    "elapsed_seconds": 0.0,
}


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


VALID_PARAMS: Dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "pattern": r"prepareStatement",
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
    "search_target": "content",
}


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_xray_explore

    return handle_xray_explore


# ---------------------------------------------------------------------------
# M2: evaluator_code optional — receives _DEFAULT_EVALUATOR_CODE (v10.4.1)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerOptionalEvaluatorCode:
    """evaluator_code is optional; v10.4.1 default emits one match per Phase 1 hit."""

    async def test_explore_accepts_missing_evaluator_code_v10_4_1(self):
        """Engine receives _DEFAULT_EVALUATOR_CODE when evaluator_code is omitted."""
        from code_indexer.server.mcp.handlers.xray import _DEFAULT_EVALUATOR_CODE

        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
            "search_target": "content",
        }

        with (
            _xray_single_repo_env() as (bjm, jt, ex, loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured_kwargs.update(kw)
                or _NOOP_ENGINE_RESULT,
            ),
        ):
            result = await _import_handler()(params, user)
            loop.run_in_executor.call_args[0][1]()  # zero-arg closure

        data = _parse_response(result)
        assert "job_id" in data, (
            f"Expected job_id when evaluator_code omitted, got: {data!r}"
        )
        assert "error" not in data, f"Expected no error, got: {data!r}"
        assert captured_kwargs.get("evaluator_code") == _DEFAULT_EVALUATOR_CODE, (
            f"Engine must receive _DEFAULT_EVALUATOR_CODE, got: {captured_kwargs.get('evaluator_code')!r}"
        )

    async def test_explore_empty_evaluator_code_engine_receives_default_v10_4_1(self):
        """Engine receives _DEFAULT_EVALUATOR_CODE when evaluator_code is empty string."""
        from code_indexer.server.mcp.handlers.xray import _DEFAULT_EVALUATOR_CODE

        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
            "search_target": "content",
            "evaluator_code": "",
        }

        with (
            _xray_single_repo_env() as (bjm, jt, ex, loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured_kwargs.update(kw)
                or _NOOP_ENGINE_RESULT,
            ),
        ):
            result = await _import_handler()(params, user)
            loop.run_in_executor.call_args[0][1]()  # zero-arg closure

        data = _parse_response(result)
        assert "job_id" in data, (
            f"Expected job_id when evaluator_code empty, got: {data!r}"
        )
        assert captured_kwargs.get("evaluator_code") == _DEFAULT_EVALUATOR_CODE, (
            f"Engine must receive _DEFAULT_EVALUATOR_CODE, got: {captured_kwargs.get('evaluator_code')!r}"
        )


# ---------------------------------------------------------------------------
# matched_node block (Issue #14)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerMatchedNode:
    """xray_explore passes include_ast_debug=True, producing matched_node per match."""

    async def test_explore_job_fn_produces_matched_node_in_matches(self):
        """job_fn result includes matched_node in each match entry (Issue #14)."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}

        engine_result = {
            "matches": [
                {
                    "file_path": "/repo/foo.py",
                    "line_number": 1,
                    "code_snippet": "prepareStatement()",
                    "language": "python",
                    "evaluator_decision": True,
                    "ast_debug": {"type": "module", "children": []},
                    "matched_node": {
                        "type": "comment",
                        "start_byte": 17,
                        "end_byte": 35,
                        "start_point": [0, 17],
                        "end_point": [0, 35],
                    },
                }
            ],
            "evaluation_errors": [],
            "files_processed": 1,
            "files_total": 1,
            "elapsed_seconds": 0.1,
        }

        with (
            _xray_single_repo_env() as (bjm, jt, ex, loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured_kwargs.update(kw) or engine_result,
            ),
        ):
            await _import_handler()(VALID_PARAMS.copy(), user)
            job_result = loop.run_in_executor.call_args[0][1]()

        assert captured_kwargs.get("include_ast_debug") is True, (
            "xray_explore must pass include_ast_debug=True to engine"
        )
        assert job_result is not None
        assert "matches" in job_result
        assert len(job_result["matches"]) >= 1
        assert "matched_node" in job_result["matches"][0], (
            "Each match entry must contain matched_node block (Issue #14)"
        )


# ---------------------------------------------------------------------------
# Tests: renamed params — pattern (was driver_regex)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerRenamedParams:
    """Verify that 'pattern' is the accepted name (was 'driver_regex').

    Acceptance paths use _xray_single_repo_env(); rejection paths only need
    _resolve_repo_path since validation short-circuits before the async infra.
    """

    async def test_pattern_param_accepted_and_submits_job(self):
        """'pattern' is the new name for the driver regex — handler accepts it."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
            "search_target": "content",
        }

        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()(params, user)

        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id with 'pattern' param, got: {data!r}"
        assert "error" not in data

    async def test_driver_regex_no_longer_accepted(self):
        """'driver_regex' is the OLD name — handler rejects it (no 'pattern' key means missing required param)."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "driver_regex": r"prepareStatement",
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
            "search_target": "content",
        }

        # Validation rejects missing 'pattern' before reaching async infra
        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()(params, user)

        data = _parse_response(result)
        assert "job_id" not in data, (
            "'driver_regex' is the old name; handler must not silently accept it"
        )


# ---------------------------------------------------------------------------
# Tests: max_results rename (was max_files)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerMaxResults:
    """max_results is the new name for max_files in xray_explore."""

    async def test_max_results_accepted_and_submits_job(self):
        """'max_results' param is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()({**VALID_PARAMS, "max_results": 5}, user)

        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id with max_results=5, got: {data!r}"

    async def test_max_results_zero_rejected(self):
        """max_results=0 is rejected with max_results_out_of_range (validation short-circuits)."""
        user = _make_user(UserRole.NORMAL_USER)

        # Validation rejects before async infra needed
        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()({**VALID_PARAMS, "max_results": 0}, user)

        data = _parse_response(result)
        assert data.get("error") == "max_results_out_of_range", (
            f"Expected max_results_out_of_range, got: {data!r}"
        )


# ---------------------------------------------------------------------------
# Tests: new params aligned to regex_search
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerNewParams:
    """New params: case_sensitive, context_lines, multiline, pcre2, path.
    All must be accepted by xray_explore (async invocation conversion only).
    """

    async def test_case_sensitive_true_accepted(self):
        """case_sensitive=True is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()(
                {**VALID_PARAMS, "case_sensitive": True}, user
            )

        assert "job_id" in _parse_response(result), (
            "case_sensitive=True must be accepted"
        )

    async def test_context_lines_5_accepted(self):
        """context_lines=5 is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()({**VALID_PARAMS, "context_lines": 5}, user)

        assert "job_id" in _parse_response(result), "context_lines=5 must be accepted"

    async def test_multiline_true_accepted(self):
        """multiline=True is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()({**VALID_PARAMS, "multiline": True}, user)

        assert "job_id" in _parse_response(result), "multiline=True must be accepted"

    async def test_pcre2_true_accepted(self):
        """pcre2=True is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()({**VALID_PARAMS, "pcre2": True}, user)

        assert "job_id" in _parse_response(result), "pcre2=True must be accepted"

    async def test_path_accepted(self):
        """path='src/' is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()({**VALID_PARAMS, "path": "src/"}, user)

        assert "job_id" in _parse_response(result), "path='src/' must be accepted"
