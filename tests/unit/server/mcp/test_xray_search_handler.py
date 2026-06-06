"""Unit tests for xray_search MCP handler.

Tests the thin handler shim that validates inputs, pre-flight checks the
evaluator, submits a background job via the dedicated xray executor, and
returns {job_id}.

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
from code_indexer.server.mcp.handlers.xray import _AWAIT_SECONDS_MAX


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


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_xray_search

    return handle_xray_search


# ---------------------------------------------------------------------------
# Tests: valid request returns job_id
# ---------------------------------------------------------------------------


class TestXraySearchHandlerValidRequest:
    """Handler returns {job_id} for valid authenticated requests."""

    async def test_returns_job_id_on_valid_params(self):
        """Happy path: handler submits job and returns job_id dict."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert "job_id" in data
        assert data["job_id"]

    async def test_submit_job_called_with_xray_search_operation_type(self):
        """register_job must be called with operation_type='xray_search'; bjm.submit_job NOT called."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            await handler(VALID_PARAMS.copy(), user)

        mock_bjm.submit_job.assert_not_called()
        mock_jt.register_job.assert_called_once()
        call_kwargs = mock_jt.register_job.call_args.kwargs
        assert call_kwargs.get("operation_type") == "xray_search"

    async def test_max_files_none_accepted(self):
        """max_files=None (omitted) is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_files": None}

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data

    async def test_max_files_5_accepted(self):
        """max_files=5 is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_files": 5}

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data


# ---------------------------------------------------------------------------
# Tests: pre-flight evaluator validation
# ---------------------------------------------------------------------------


class TestXraySearchHandlerPreFlightValidation:
    """Handler rejects bad evaluator code before calling submit_job."""

    async def test_bad_evaluator_returns_validation_error(self):
        """Evaluator code containing forbidden 'unsafe' construct is rejected synchronously."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            **VALID_PARAMS,
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { unsafe { vec![] } }",
        }

        # Use real validate_rust_evaluator — no mock, so infra patches that reach
        # the async part are not needed (validation short-circuits before them).
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
                return_value=MagicMock(),
            ),
        ):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "xray_evaluator_validation_failed"

    async def test_bad_evaluator_does_not_submit_job(self):
        """When evaluator validation fails, submit_job must NOT be called."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        params = {
            **VALID_PARAMS,
            "evaluator_code": 'fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { std::fs::read_to_string("x"); vec![] }',
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
            handler = _import_handler()
            await handler(params, user)

        mock_bjm.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: parameter validation errors
# ---------------------------------------------------------------------------


class TestXraySearchHandlerParamValidation:
    """Handler rejects invalid parameters with descriptive error codes."""

    async def test_unknown_search_target_rejected(self):
        """search_target must be 'content' or 'filename'."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "search_target": "fulltext"}

        handler = _import_handler()
        result = await handler(params, user)

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
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "timeout_out_of_range"

    async def test_timeout_too_high_rejected(self):
        """timeout_seconds=900 is above maximum 600."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "timeout_seconds": 900}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "timeout_out_of_range"

    async def test_max_results_zero_rejected(self):
        """max_results=0 is rejected with max_results_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": 0}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_results_out_of_range"

    async def test_max_results_negative_rejected(self):
        """max_results=-1 is rejected with max_results_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": -1}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_results_out_of_range"


# ---------------------------------------------------------------------------
# Tests: auth and permission checks
# ---------------------------------------------------------------------------


class TestXraySearchHandlerAuth:
    """Handler enforces auth and permission requirements."""

    async def test_unauthenticated_request_rejected(self):
        """None user produces auth_required error."""
        handler = _import_handler()
        result = await handler(VALID_PARAMS.copy(), None)

        data = _parse_response(result)
        assert data.get("error") == "auth_required"

    async def test_missing_query_repos_permission_rejected(self):
        """User without query_repos permission is rejected."""
        user = MagicMock(spec=User)
        user.username = "testuser"
        user.has_permission.return_value = False

        handler = _import_handler()
        result = await handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert data.get("error") == "auth_required"


# ---------------------------------------------------------------------------
# Tests: repository resolution
# ---------------------------------------------------------------------------


class TestXraySearchHandlerRepoResolution:
    """Handler rejects unknown repository aliases."""

    async def test_unknown_alias_returns_repository_not_found(self):
        """When the alias cannot be resolved, repository_not_found is returned."""
        user = _make_user(UserRole.NORMAL_USER)

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=None,
        ):
            handler = _import_handler()
            result = await handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert data.get("error") == "repository_not_found"


# ---------------------------------------------------------------------------
# Tests: job_fn wires _truncate_xray_result
# ---------------------------------------------------------------------------


class TestXraySearchHandlerTruncation:
    """job_fn applies _truncate_xray_result to the engine result."""

    async def test_job_fn_applies_truncation_to_engine_result(self):
        """The submitted job_fn passes engine output through _truncate_xray_result."""
        user = _make_user(UserRole.NORMAL_USER)

        engine_result = {
            "matches": [{"file_path": "a.py"}],
            "evaluation_errors": [],
            "files_processed": 1,
            "files_total": 1,
            "elapsed_seconds": 0.5,
        }
        truncated_result = {**engine_result, "truncated": False, "cache_handle": None}

        with (
            _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop),
            patch(
                "code_indexer.server.mcp.handlers.xray._truncate_xray_result",
                return_value=truncated_result,
            ) as mock_truncate,
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine",
                return_value=MagicMock(run=MagicMock(return_value=engine_result)),
            ),
        ):
            handler = _import_handler()
            await handler(VALID_PARAMS.copy(), user)

            assert mock_loop.run_in_executor.called
            job_fn = mock_loop.run_in_executor.call_args[0][1]
            job_fn()

        mock_truncate.assert_called_once_with(engine_result)


# ---------------------------------------------------------------------------
# Tests: await_seconds parameter
# ---------------------------------------------------------------------------


class TestXraySearchHandlerAwaitSeconds:
    """await_seconds=0 returns {job_id}; N>0 polls and returns inline result if done."""

    async def test_await_seconds_zero_returns_job_id(self):
        """await_seconds=0 (default) returns {job_id} immediately."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": 0}

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data
        assert "matches" not in data

    async def test_await_seconds_omitted_returns_job_id(self):
        """await_seconds absent (default 0) returns {job_id} immediately."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert "job_id" in data
        assert "matches" not in data

    async def test_await_seconds_positive_returns_inline_result_when_job_completes(
        self,
    ):
        """await_seconds=5 returns inline {matches, ...} when job completes within window."""
        user = _make_user(UserRole.NORMAL_USER)
        inline_result = {
            "matches": [{"file_path": "a.py"}],
            "evaluation_errors": [],
            "files_processed": 1,
            "files_total": 1,
            "elapsed_seconds": 0.1,
            "truncated": False,
            "cache_handle": None,
        }

        resolved_future = _make_resolved_future(inline_result)
        params = {**VALID_PARAMS, "await_seconds": 5}

        with _xray_single_repo_env(resolved_future=resolved_future) as (
            mock_bjm,
            mock_jt,
            mock_exec,
            mock_loop,
        ):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert "matches" in data
        assert "job_id" not in data

    async def test_await_seconds_returns_job_id_when_job_does_not_complete_in_window(
        self,
    ):
        """await_seconds=0.1 returns {job_id} when job stays pending beyond window."""
        user = _make_user(UserRole.NORMAL_USER)
        pending_future: asyncio.Future = asyncio.get_event_loop().create_future()
        params = {**VALID_PARAMS, "await_seconds": 0.1}

        with _xray_single_repo_env(resolved_future=pending_future) as (
            mock_bjm,
            mock_jt,
            mock_exec,
            mock_loop,
        ):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data

    async def test_await_seconds_negative_rejected(self):
        """await_seconds=-1 returns await_seconds_invalid error."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": -1}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid"

    async def test_await_seconds_above_cap_rejected(self):
        """await_seconds above _AWAIT_SECONDS_MAX is rejected with await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": _AWAIT_SECONDS_MAX + 1.0}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid"


# ---------------------------------------------------------------------------
# Tests: await_seconds float + cap (Bug #1070 lowered to 45s)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerAwaitSecondsV2:
    """Tests for float await_seconds and the cap enforced via _AWAIT_SECONDS_MAX.

    Task #35: await_seconds accepts int OR float (not just int).
    Bug #1070: ceiling lowered from 120s to 45s to avoid ALB 60s timeout 504s.
    """

    async def test_await_seconds_float_half_accepted(self):
        """await_seconds=0.5 (float) is accepted and treated as 500ms wait."""
        user = _make_user(UserRole.NORMAL_USER)
        pending_future: asyncio.Future = asyncio.get_event_loop().create_future()
        params = {**VALID_PARAMS, "await_seconds": 0.5}

        with _xray_single_repo_env(resolved_future=pending_future) as (
            mock_bjm,
            mock_jt,
            mock_exec,
            mock_loop,
        ):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "await_seconds_invalid", (
            "await_seconds=0.5 (float) must be accepted"
        )

    async def test_await_seconds_float_near_zero_accepted(self):
        """await_seconds=0.001 (near-zero float) is accepted."""
        user = _make_user(UserRole.NORMAL_USER)
        pending_future: asyncio.Future = asyncio.get_event_loop().create_future()
        params = {**VALID_PARAMS, "await_seconds": 0.001}

        with _xray_single_repo_env(resolved_future=pending_future) as (
            mock_bjm,
            mock_jt,
            mock_exec,
            mock_loop,
        ):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "await_seconds_invalid", (
            "await_seconds=0.001 (near-zero float) must be accepted"
        )

    async def test_await_seconds_float_at_cap_accepted(self):
        """await_seconds=_AWAIT_SECONDS_MAX (float at cap) is accepted."""
        user = _make_user(UserRole.NORMAL_USER)
        pending_future: asyncio.Future = _make_resolved_future(
            {
                "matches": [],
                "evaluation_errors": [],
                "files_processed": 0,
                "files_total": 0,
                "elapsed_seconds": 0.0,
            }
        )
        params = {**VALID_PARAMS, "await_seconds": _AWAIT_SECONDS_MAX}

        with _xray_single_repo_env(resolved_future=pending_future) as (
            mock_bjm,
            mock_jt,
            mock_exec,
            mock_loop,
        ):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "await_seconds_invalid", (
            f"await_seconds={_AWAIT_SECONDS_MAX} (float at cap) must be accepted"
        )

    async def test_await_seconds_float_just_above_cap_rejected(self):
        """await_seconds just above _AWAIT_SECONDS_MAX is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": _AWAIT_SECONDS_MAX + 0.001}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            f"await_seconds={_AWAIT_SECONDS_MAX + 0.001} (just above cap) must be rejected"
        )

    async def test_await_seconds_float_negative_rejected(self):
        """await_seconds=-0.001 (negative float) is rejected with await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": -0.001}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds=-0.001 (negative float) must be rejected"
        )

    async def test_await_seconds_bool_true_rejected(self):
        """await_seconds=True (bool) is rejected — bool subclasses int but must be blocked."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": True}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds=True (bool) must be rejected even though bool subclasses int"
        )

    async def test_await_seconds_string_rejected(self):
        """await_seconds='0.5' (string) is rejected with await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": "0.5"}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds='0.5' (string) must be rejected"
        )

    async def test_await_seconds_int_zero_regression(self):
        """await_seconds=0 (int) still returns {job_id} immediately (regression)."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": 0}

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data
        assert data.get("error") != "await_seconds_invalid"

    async def test_await_seconds_int_five_regression(self):
        """await_seconds=5 (int) is still accepted (regression)."""
        user = _make_user(UserRole.NORMAL_USER)
        pending_future: asyncio.Future = asyncio.get_event_loop().create_future()
        params = {**VALID_PARAMS, "await_seconds": 5}

        with _xray_single_repo_env(resolved_future=pending_future) as (
            mock_bjm,
            mock_jt,
            mock_exec,
            mock_loop,
        ):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "await_seconds_invalid"

    async def test_await_seconds_above_cap_rejected_with_cap_in_message(self):
        """await_seconds above cap is rejected and error message mentions the cap value."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": _AWAIT_SECONDS_MAX + 1.0}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            f"await_seconds above {_AWAIT_SECONDS_MAX} must be rejected"
        )
        message = data.get("message", "")
        assert (
            str(int(_AWAIT_SECONDS_MAX)) in message
            or str(_AWAIT_SECONDS_MAX) in message
        ), (
            f"Error message must mention the cap ({_AWAIT_SECONDS_MAX}), got: {message!r}"
        )


# ---------------------------------------------------------------------------
# Tests: renamed params — pattern (was driver_regex), max_results (was max_files)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerRenamedParams:
    """Verify that 'pattern' is the accepted name (was 'driver_regex')."""

    async def test_pattern_param_accepted_and_submits_job(self):
        """'pattern' is the new name for the driver regex — handler accepts it."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
            "search_target": "content",
        }

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id with 'pattern' param, got: {data!r}"
        assert "error" not in data

    async def test_driver_regex_no_longer_accepted(self):
        """'driver_regex' is the OLD name — handler must reject or ignore it."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {
            "repository_alias": "myrepo-global",
            "driver_regex": r"prepareStatement",
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
            "search_target": "content",
        }

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert "job_id" not in data, (
            "'driver_regex' is the old name; handler must not silently accept it"
        )

    async def test_pattern_forwarded_to_engine_as_driver_regex_or_pattern(self):
        """The 'pattern' param value reaches the XRaySearchEngine.run() call."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"mySpecialRegex",
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
            "search_target": "content",
        }

        with (
            _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured_kwargs.update(kw)
                or {
                    "matches": [],
                    "evaluation_errors": [],
                    "files_processed": 0,
                    "files_total": 0,
                    "elapsed_seconds": 0.0,
                },
            ),
        ):
            handler = _import_handler()
            await handler(params, user)
            job_fn = mock_loop.run_in_executor.call_args[0][1]
            job_fn()

        assert captured_kwargs.get("driver_regex") == r"mySpecialRegex", (
            f"Engine must receive pattern value as driver_regex, got: {captured_kwargs!r}"
        )


# ---------------------------------------------------------------------------
# Tests: max_results rename (was max_files)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerMaxResults:
    """max_results is the new name for max_files (regex_search alignment)."""

    async def test_max_results_accepted_and_submits_job(self):
        """'max_results' param is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": 10}

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id with max_results=10, got: {data!r}"

    async def test_max_results_zero_rejected(self):
        """max_results=0 is rejected with max_results_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": 0}

        handler = _import_handler()
        result = await handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_results_out_of_range", (
            f"Expected max_results_out_of_range, got: {data!r}"
        )

    async def test_max_results_forwarded_to_engine_as_max_files(self):
        """max_results value reaches engine.run() as max_files argument."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        params = {**VALID_PARAMS, "max_results": 7}

        with (
            _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured_kwargs.update(kw)
                or {
                    "matches": [],
                    "evaluation_errors": [],
                    "files_processed": 0,
                    "files_total": 0,
                    "elapsed_seconds": 0.0,
                },
            ),
        ):
            handler = _import_handler()
            await handler(params, user)
            job_fn = mock_loop.run_in_executor.call_args[0][1]
            job_fn()

        assert captured_kwargs.get("max_files") == 7, (
            f"Engine must receive max_results value as max_files=7, got: {captured_kwargs!r}"
        )

    async def test_max_files_old_name_no_longer_accepted(self):
        """'max_files' is the OLD name — handler must not silently forward it as max_results."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_files": 5}

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler(params, user)

        data = _parse_response(result)
        # max_files is the old key; handler should not treat it as max_results
        assert "error" not in data or data.get("error") != "max_files_out_of_range", (
            "Error code must be max_results_out_of_range, not max_files_out_of_range"
        )


# ---------------------------------------------------------------------------
# Tests: new params aligned to regex_search
# ---------------------------------------------------------------------------


class TestXraySearchHandlerNewParams:
    """New params added for regex_search alignment: case_sensitive, context_lines,
    multiline, pcre2, path. All must be accepted and forwarded to the engine."""

    async def _capture_engine_kwargs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Submit a valid job and capture kwargs passed to engine.run()."""
        user = _make_user(UserRole.NORMAL_USER)
        captured: Dict[str, Any] = {}

        with (
            _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured.update(kw)
                or {
                    "matches": [],
                    "evaluation_errors": [],
                    "files_processed": 0,
                    "files_total": 0,
                    "elapsed_seconds": 0.0,
                },
            ),
        ):
            handler = _import_handler()
            await handler(params, user)
            job_fn = mock_loop.run_in_executor.call_args[0][1]
            job_fn()

        return captured

    async def test_case_sensitive_true_accepted(self):
        """case_sensitive=True is accepted and forwarded to engine."""
        captured = await self._capture_engine_kwargs(
            {**VALID_PARAMS, "case_sensitive": True}
        )
        assert captured.get("case_sensitive") is True, (
            f"Engine must receive case_sensitive=True, got: {captured!r}"
        )

    async def test_case_sensitive_false_accepted(self):
        """case_sensitive=False is accepted and forwarded to engine."""
        captured = await self._capture_engine_kwargs(
            {**VALID_PARAMS, "case_sensitive": False}
        )
        assert captured.get("case_sensitive") is False, (
            f"Engine must receive case_sensitive=False, got: {captured!r}"
        )

    async def test_case_sensitive_default_is_true(self):
        """case_sensitive defaults to True when omitted (matches regex_search default)."""
        captured = await self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("case_sensitive") is True, (
            f"Default case_sensitive must be True, got: {captured!r}"
        )

    async def test_context_lines_zero_accepted(self):
        """context_lines=0 is accepted and forwarded to engine."""
        captured = await self._capture_engine_kwargs(
            {**VALID_PARAMS, "context_lines": 0}
        )
        assert captured.get("context_lines") == 0, (
            f"Engine must receive context_lines=0, got: {captured!r}"
        )

    async def test_context_lines_5_accepted(self):
        """context_lines=5 is accepted and forwarded to engine."""
        captured = await self._capture_engine_kwargs(
            {**VALID_PARAMS, "context_lines": 5}
        )
        assert captured.get("context_lines") == 5, (
            f"Engine must receive context_lines=5, got: {captured!r}"
        )

    async def test_context_lines_10_accepted(self):
        """context_lines=10 (max) is accepted."""
        user = _make_user(UserRole.NORMAL_USER)

        with _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop):
            handler = _import_handler()
            result = await handler({**VALID_PARAMS, "context_lines": 10}, user)

        data = _parse_response(result)
        assert "job_id" in data, f"context_lines=10 must be accepted, got: {data!r}"

    async def test_context_lines_negative_rejected(self):
        """context_lines=-1 is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        handler = _import_handler()
        result = await handler({**VALID_PARAMS, "context_lines": -1}, user)
        data = _parse_response(result)
        assert "error" in data, "context_lines=-1 must be rejected"

    async def test_context_lines_11_rejected(self):
        """context_lines=11 exceeds max of 10 and is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        handler = _import_handler()
        result = await handler({**VALID_PARAMS, "context_lines": 11}, user)
        data = _parse_response(result)
        assert "error" in data, "context_lines=11 must be rejected"

    async def test_context_lines_default_is_zero(self):
        """context_lines defaults to 0 when omitted."""
        captured = await self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("context_lines") == 0, (
            f"Default context_lines must be 0, got: {captured!r}"
        )

    async def test_multiline_true_accepted(self):
        """multiline=True is accepted and forwarded to engine."""
        captured = await self._capture_engine_kwargs(
            {**VALID_PARAMS, "multiline": True}
        )
        assert captured.get("multiline") is True, (
            f"Engine must receive multiline=True, got: {captured!r}"
        )

    async def test_multiline_false_is_default(self):
        """multiline defaults to False when omitted."""
        captured = await self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("multiline") is False, (
            f"Default multiline must be False, got: {captured!r}"
        )

    async def test_pcre2_true_accepted(self):
        """pcre2=True is accepted and forwarded to engine."""
        captured = await self._capture_engine_kwargs({**VALID_PARAMS, "pcre2": True})
        assert captured.get("pcre2") is True, (
            f"Engine must receive pcre2=True, got: {captured!r}"
        )

    async def test_pcre2_false_is_default(self):
        """pcre2 defaults to False when omitted."""
        captured = await self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("pcre2") is False, (
            f"Default pcre2 must be False, got: {captured!r}"
        )

    async def test_path_accepted(self):
        """path='src/' is accepted and forwarded to engine."""
        captured = await self._capture_engine_kwargs({**VALID_PARAMS, "path": "src/"})
        assert captured.get("path") == "src/", (
            f"Engine must receive path='src/', got: {captured!r}"
        )

    async def test_path_none_is_default(self):
        """path defaults to None when omitted."""
        captured = await self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("path") is None, (
            f"Default path must be None, got: {captured!r}"
        )


# ---------------------------------------------------------------------------
# Tests: output envelope — line_content (was code_snippet)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerOutputEnvelope:
    """Match envelope uses 'line_content' (renamed from 'code_snippet')."""

    def test_engine_produces_line_content_field(self):
        """XRaySearchEngine uses 'line_content', not 'code_snippet', in the match envelope."""
        from code_indexer.xray import search_engine as _search_engine_module
        from code_indexer.xray.search_engine import XRaySearchEngine

        import inspect

        evaluate_file_source = inspect.getsource(XRaySearchEngine._evaluate_file)
        assert "code_snippet" not in evaluate_file_source, (
            "'code_snippet' must NOT appear in _evaluate_file — rename complete"
        )

        module_source = inspect.getsource(_search_engine_module)
        assert "line_content" in module_source, (
            "'line_content' must appear in search_engine module "
            "(field name renamed from code_snippet)"
        )


# ---------------------------------------------------------------------------
# Tests: omni multi-repo — repository_alias accepts str OR list
# ---------------------------------------------------------------------------


class TestXraySearchHandlerOmni:
    """repository_alias accepts string OR array of strings (Directive C)."""

    def _make_bjm(self, job_ids: list) -> MagicMock:
        mock_bjm = MagicMock()
        mock_bjm.submit_job.side_effect = job_ids
        return mock_bjm

    async def _run_with_aliases(
        self, alias_value: Any, resolved_paths: dict
    ) -> Dict[str, Any]:
        """Run handle_xray_search with given repository_alias and path map."""
        import json as _json

        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_user(UserRole.NORMAL_USER)
        if isinstance(alias_value, list):
            aliases = alias_value
        elif isinstance(alias_value, str) and alias_value.startswith("["):
            try:
                parsed = _json.loads(alias_value)
                aliases = parsed if isinstance(parsed, list) else [alias_value]
            except _json.JSONDecodeError:
                aliases = [alias_value]
        else:
            aliases = [alias_value]

        job_ids = [f"job-{i}" for i in range(len(aliases))]
        mock_bjm = self._make_bjm(job_ids)

        params = {
            "repository_alias": alias_value,
            "pattern": r"TODO",
            "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
            "search_target": "content",
        }

        def fake_resolve(alias: str) -> Any:
            return resolved_paths.get(alias)

        mock_jt = MagicMock()
        mock_jt.register_job.return_value = MagicMock()
        mock_exec = MagicMock()
        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm
        mock_app.activated_repo_manager = None
        mock_app.golden_repo_manager = None

        pending_future: asyncio.Future = asyncio.get_event_loop().create_future()
        loop_instance = MagicMock()
        loop_instance.run_in_executor.return_value = pending_future

        with (
            patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                side_effect=fake_resolve,
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
            return cast(Dict[str, Any], await handle_xray_search(params, user))

    async def test_string_alias_single_repo_works_as_before(self):
        """String alias returns {job_id} dict (unchanged single-repo path)."""
        result = await self._run_with_aliases(
            "myrepo-global", {"myrepo-global": "/path/repo"}
        )
        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id, got: {data}"

    async def test_array_alias_with_two_repos_submits_two_jobs(self):
        """List alias submits one job per repo and returns {job_ids: [...]}."""
        paths = {"repo-a": "/path/a", "repo-b": "/path/b"}
        result = await self._run_with_aliases(["repo-a", "repo-b"], paths)
        data = _parse_response(result)
        assert "job_ids" in data, f"Expected job_ids, got: {data}"
        assert len(data["job_ids"]) == 2

    async def test_json_string_array_alias_is_parsed(self):
        """JSON-encoded string array '["a","b"]' is parsed to list of aliases."""
        paths = {"repo-a": "/path/a", "repo-b": "/path/b"}
        result = await self._run_with_aliases('["repo-a", "repo-b"]', paths)
        data = _parse_response(result)
        assert "job_ids" in data, f"Expected job_ids after JSON parse, got: {data}"
        assert len(data["job_ids"]) == 2

    async def test_array_alias_with_unknown_repo_returns_not_found_errors(self):
        """Unknown alias in list produces repository_not_found error entry."""
        paths = {"known-repo": "/path/known"}
        result = await self._run_with_aliases(["known-repo", "unknown-repo"], paths)
        data = _parse_response(result)
        assert "errors" in data or "job_ids" in data, f"Unexpected response: {data}"
        if "errors" in data:
            errors = data["errors"]
            assert any("unknown-repo" in str(e) for e in errors), (
                f"Expected error mentioning 'unknown-repo', got: {errors}"
            )

    async def test_empty_array_alias_returns_alias_required_error(self):
        """Empty list alias returns alias_required error."""
        result = await self._run_with_aliases([], {})
        data = _parse_response(result)
        assert data.get("error") == "alias_required", (
            f"Expected alias_required, got: {data}"
        )


# ---------------------------------------------------------------------------
# Tests: default evaluator produces dict contract (Bug 2 fix)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerDefaultEvaluator:
    """When evaluator_code is omitted, xray_search uses a dict-contract default.

    Bug 2 (v10.4.1): handle_xray_search used params.get("evaluator_code", "")
    which passed an empty string to the engine.
    """

    async def _get_engine_evaluator_code(self, params: Dict[str, Any]) -> str:
        """Submit a valid job and capture the evaluator_code forwarded to engine.run()."""
        user = _make_user(UserRole.NORMAL_USER)
        captured: Dict[str, Any] = {}

        with (
            _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=lambda **kw: captured.update(kw)
                or {
                    "matches": [],
                    "evaluation_errors": [],
                    "files_processed": 0,
                    "files_total": 0,
                    "elapsed_seconds": 0.0,
                },
            ),
        ):
            handler = _import_handler()
            await handler(params, user)
            job_fn = mock_loop.run_in_executor.call_args[0][1]
            job_fn()

        return cast(str, captured.get("evaluator_code", ""))

    async def test_omitted_evaluator_code_uses_non_empty_default(self):
        """When evaluator_code is omitted, engine receives a non-empty default (Bug 2)."""
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
        }
        evaluator = await self._get_engine_evaluator_code(params)
        assert evaluator, (
            "Engine must receive a non-empty evaluator_code when evaluator_code is omitted"
        )

    async def test_omitted_evaluator_code_default_returns_dict_not_bool(self):
        """Default evaluator must contain 'fn evaluate_node' (Rust contract)."""
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
        }
        evaluator = await self._get_engine_evaluator_code(params)
        assert "fn evaluate_node" in evaluator, (
            f"Default evaluator must contain 'fn evaluate_node', got: {evaluator!r}"
        )

    async def test_omitted_evaluator_code_default_passes_sandbox_validation(self):
        """Default evaluator must pass validate_rust_evaluator() — not crash at preflight."""
        from code_indexer.xray.sandbox import validate_rust_evaluator

        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
        }
        evaluator = await self._get_engine_evaluator_code(params)

        result = validate_rust_evaluator(evaluator)
        assert result.ok, (
            f"Default evaluator must pass validate_rust_evaluator(), got: {result.reason!r}"
        )

    async def test_empty_evaluator_code_string_uses_non_empty_default(self):
        """Explicit empty string evaluator_code is treated same as omitted — non-empty default."""
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
            "evaluator_code": "",
        }
        evaluator = await self._get_engine_evaluator_code(params)
        assert evaluator, (
            "Empty string evaluator_code must be replaced by non-empty default (Bug 2)"
        )

    async def test_explicit_evaluator_code_is_not_replaced_by_default(self):
        """Explicit non-empty evaluator_code is forwarded as-is (regression guard)."""
        custom_code = (
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"
        )
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
            "evaluator_code": custom_code,
        }
        evaluator = await self._get_engine_evaluator_code(params)
        assert evaluator == custom_code, (
            f"Explicit evaluator_code must not be replaced by default, got: {evaluator!r}"
        )
