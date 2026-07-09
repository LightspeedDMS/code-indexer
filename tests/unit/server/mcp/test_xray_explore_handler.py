"""Unit tests for xray_explore MCP handler.

Tests the thin handler shim that validates inputs, pre-flight checks the
evaluator, submits a background job with include_ast_debug=True, and returns
{job_id}.

Mocking strategy (Bug #1070 async path):
- _resolve_repo_path: mocked (needs live file-system alias manager)
- _get_job_tracker: mocked (registers job_id without conflict check)
- _get_xray_executor: mocked (dedicated ThreadPoolExecutor)
- asyncio.get_running_loop: mocked (returns loop with controllable futures)
- validate_rust_evaluator: real for pre-flight; patched for extras-missing test
- _get_background_job_manager: mocked (still used for multi-repo omni path)
- User/permission: uses real User model with appropriate roles
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
# Shared infrastructure helpers (Bug #1070 async pattern)
# ---------------------------------------------------------------------------


def _make_resolved_future(result: dict) -> "asyncio.Future":
    """Resolved future carrying `result` (for inline await tests)."""
    f: asyncio.Future = asyncio.Future()
    f.set_result(result)
    return f


@contextmanager
def _xray_single_repo_env(
    resolved_future: "Optional[asyncio.Future]" = None,
) -> Generator:
    """Patch infra boundaries for the single-repo Bug #1070 path.

    Yields (mock_bjm, mock_job_tracker, mock_xray_executor, mock_loop) where
    mock_loop.run_in_executor is the call-recording mock returning the future.
    If resolved_future is None, a PENDING future is used (job_fn capture pattern).
    """
    mock_bjm = MagicMock()
    mock_jt = MagicMock()
    mock_jt.register_job.return_value = MagicMock()
    mock_exec = MagicMock()
    mock_app = MagicMock()
    mock_app.background_job_manager = mock_bjm
    mock_app.activated_repo_manager = None
    mock_app.golden_repo_manager = None

    if resolved_future is None:
        resolved_future = asyncio.Future()  # pending; capture-only tests use call_args

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    """Build a real User with the given role."""
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the MCP content envelope."""
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


VALID_PARAMS: Dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "pattern": r"prepareStatement",
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
    "search_target": "content",
}


# ---------------------------------------------------------------------------
# Helper to import handler after mocking is set up
# ---------------------------------------------------------------------------


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_xray_explore

    return handle_xray_explore


# ---------------------------------------------------------------------------
# Tests: valid request returns job_id
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerValidRequest:
    """Handler returns {job_id} for valid authenticated requests."""

    async def test_returns_job_id_on_valid_params(self):
        """Happy path: handler returns {job_id} dict with a non-empty UUID job_id.

        Bug #1070: job_id is now a uuid4 generated inside the handler (not
        returned by bjm.submit_job), so we assert truthy rather than a specific value.
        """
        user = _make_user(UserRole.NORMAL_USER)
        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()(VALID_PARAMS.copy(), user)
        data = _parse_response(result)
        assert "job_id" in data
        assert data["job_id"]  # uuid4 string — truthy

    async def test_submit_job_called_with_xray_explore_operation_type(self):
        """register_job must use operation_type='xray_explore' (Bug #1070: bjm.submit_job no longer called).

        The handler now calls job_tracker.register_job() instead of bjm.submit_job().
        The test name is preserved for git history; the assertion target has changed
        to reflect the new architectural path introduced in Bug #1070.
        """
        user = _make_user(UserRole.NORMAL_USER)
        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            await _import_handler()(VALID_PARAMS.copy(), user)
        bjm.submit_job.assert_not_called()
        jt.register_job.assert_called_once()
        assert jt.register_job.call_args.kwargs.get("operation_type") == "xray_explore"

    async def test_job_fn_passes_include_ast_debug_true(self):
        """The job_fn in run_in_executor calls engine.run with include_ast_debug=True.

        Bug #1070: job_fn is a zero-argument closure (no progress_callback).
        Captured via loop.run_in_executor.call_args[0][1] and invoked with no args.
        """
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        with (
            _xray_single_repo_env() as (bjm, jt, ex, loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured_kwargs.update(kw)
                or _NOOP_ENGINE_RESULT,
            ),
        ):
            await _import_handler()(VALID_PARAMS.copy(), user)
            loop.run_in_executor.call_args[0][1]()  # zero-arg closure
        assert captured_kwargs.get("include_ast_debug") is True

    async def test_max_debug_nodes_defaults_to_50(self):
        """When max_debug_nodes is omitted, job fn uses default of 50."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        with (
            _xray_single_repo_env() as (bjm, jt, ex, loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured_kwargs.update(kw)
                or _NOOP_ENGINE_RESULT,
            ),
        ):
            await _import_handler()(VALID_PARAMS.copy(), user)
            loop.run_in_executor.call_args[0][1]()
        assert captured_kwargs.get("max_debug_nodes") == 50

    async def test_max_debug_nodes_custom_value_forwarded(self):
        """When max_debug_nodes=20 is provided, job fn uses 20."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        with (
            _xray_single_repo_env() as (bjm, jt, ex, loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured_kwargs.update(kw)
                or _NOOP_ENGINE_RESULT,
            ),
        ):
            await _import_handler()({**VALID_PARAMS, "max_debug_nodes": 20}, user)
            loop.run_in_executor.call_args[0][1]()
        assert captured_kwargs.get("max_debug_nodes") == 20

    async def test_max_files_none_accepted(self):
        """max_files=None (omitted) is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()({**VALID_PARAMS, "max_files": None}, user)
        assert "job_id" in _parse_response(result)

    async def test_max_files_3_accepted(self):
        """max_files=3 is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()({**VALID_PARAMS, "max_files": 3}, user)
        assert "job_id" in _parse_response(result)


# ---------------------------------------------------------------------------
# Tests: max_debug_nodes validation
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerMaxDebugNodesValidation:
    """max_debug_nodes must be in range 1..500."""

    async def test_max_debug_nodes_zero_rejected(self):
        """max_debug_nodes=0 is rejected with max_debug_nodes_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()(
                {**VALID_PARAMS, "max_debug_nodes": 0}, user
            )
        assert _parse_response(result).get("error") == "max_debug_nodes_out_of_range"

    async def test_max_debug_nodes_1000_rejected(self):
        """max_debug_nodes=1000 is above maximum 500 and is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()(
                {**VALID_PARAMS, "max_debug_nodes": 1000}, user
            )
        assert _parse_response(result).get("error") == "max_debug_nodes_out_of_range"

    async def test_max_debug_nodes_1_accepted(self):
        """max_debug_nodes=1 is the minimum valid value."""
        user = _make_user(UserRole.NORMAL_USER)
        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()(
                {**VALID_PARAMS, "max_debug_nodes": 1}, user
            )
        assert "job_id" in _parse_response(result)

    async def test_max_debug_nodes_500_accepted(self):
        """max_debug_nodes=500 is the maximum valid value."""
        user = _make_user(UserRole.NORMAL_USER)
        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            result = await _import_handler()(
                {**VALID_PARAMS, "max_debug_nodes": 500}, user
            )
        assert "job_id" in _parse_response(result)

    async def test_max_debug_nodes_501_rejected(self):
        """max_debug_nodes=501 is above maximum 500 and is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()(
                {**VALID_PARAMS, "max_debug_nodes": 501}, user
            )
        assert _parse_response(result).get("error") == "max_debug_nodes_out_of_range"

    async def test_max_debug_nodes_negative_rejected(self):
        """max_debug_nodes=-1 is rejected with max_debug_nodes_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()(
                {**VALID_PARAMS, "max_debug_nodes": -1}, user
            )
        assert _parse_response(result).get("error") == "max_debug_nodes_out_of_range"

    async def test_max_debug_nodes_validation_does_not_submit_job(self):
        """When max_debug_nodes is invalid, submit_job must NOT be called."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            await _import_handler()({**VALID_PARAMS, "max_debug_nodes": 0}, user)
        mock_bjm.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: pre-flight evaluator validation
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerPreFlightValidation:
    """Handler rejects bad evaluator code before calling submit_job."""

    async def test_bad_evaluator_returns_validation_error(self):
        """Evaluator code containing forbidden 'unsafe' construct is rejected synchronously.

        Bug #1070: handler is now async. The _utils.app_module patch is required because
        the async path accesses app_module for the global-alias fallback check.
        """
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        params = {
            **VALID_PARAMS,
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { unsafe { vec![] } }",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._utils.app_module",
                MagicMock(activated_repo_manager=None, golden_repo_manager=None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            result = await _import_handler()(params, user)

        data = _parse_response(result)
        assert data.get("error") == "xray_evaluator_validation_failed"
        mock_bjm.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: shared parameter validation
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerSharedParamValidation:
    """Handler shares validation logic with xray_search for common parameters."""

    async def test_unknown_search_target_rejected(self):
        """search_target must be 'content' or 'filename'."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "search_target": "fulltext"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()(params, user)

        data = _parse_response(result)
        assert data.get("error") == "invalid_search_target"

    async def test_timeout_too_low_rejected(self):
        """timeout_seconds=5 is below minimum 10."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "timeout_seconds": 5}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()(params, user)

        data = _parse_response(result)
        assert data.get("error") == "timeout_out_of_range"

    async def test_max_results_zero_rejected(self):
        """max_results=0 is rejected with max_results_out_of_range (renamed from max_files)."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": 0}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            result = await _import_handler()(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_results_out_of_range"


# ---------------------------------------------------------------------------
# Tests: auth and permission checks
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerAuth:
    """Handler enforces auth and permission requirements."""

    async def test_unauthenticated_request_rejected(self):
        """None user produces auth_required error."""
        result = await _import_handler()(VALID_PARAMS.copy(), None)

        data = _parse_response(result)
        assert data.get("error") == "auth_required"

    async def test_missing_query_repos_permission_rejected(self):
        """User without query_repos permission is rejected."""
        user = MagicMock(spec=User)
        user.username = "testuser"
        user.has_permission.return_value = False

        result = await _import_handler()(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert data.get("error") == "auth_required"


# ---------------------------------------------------------------------------
# Tests: repository resolution
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerRepoResolution:
    """Handler rejects unknown repository aliases."""

    async def test_unknown_alias_returns_repository_not_found(self):
        """When the alias cannot be resolved, repository_not_found is returned."""
        user = _make_user(UserRole.NORMAL_USER)

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=None,
        ):
            result = await _import_handler()(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert data.get("error") == "repository_not_found"


# ---------------------------------------------------------------------------
# M5: operation_type must be 'xray_explore', not 'xray_search'
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerOperationType:
    """xray_explore submits jobs with operation_type='xray_explore'."""

    async def test_explore_uses_distinct_operation_type(self):
        """register_job must be called with operation_type='xray_explore', not 'xray_search' (M5).

        Bug #1070: handler now calls job_tracker.register_job() instead of bjm.submit_job().
        """
        user = _make_user(UserRole.NORMAL_USER)
        with _xray_single_repo_env() as (bjm, jt, ex, loop):
            await _import_handler()(VALID_PARAMS.copy(), user)

        bjm.submit_job.assert_not_called()
        jt.register_job.assert_called_once()
        actual_op_type = jt.register_job.call_args.kwargs.get("operation_type")
        assert actual_op_type == "xray_explore", (
            f"Expected operation_type='xray_explore', got {actual_op_type!r}"
        )
