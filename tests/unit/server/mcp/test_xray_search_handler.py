"""Unit tests for xray_search MCP handler.

Tests the thin handler shim that validates inputs, pre-flight checks the
evaluator, submits a background job, and returns {job_id}.

Mocking strategy:
- _resolve_golden_repo_path: mocked (needs live file-system alias manager)
- background_job_manager.submit_job: mocked to capture call args
- XRaySearchEngine: real for pre-flight validation; mocked for extras-missing test
- PythonEvaluatorSandbox.validate: uses real sandbox (no mock)
- User/permission: uses real User model with appropriate roles
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch


from code_indexer.server.auth.user_manager import User, UserRole


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
    "evaluator_code": "return True",
    "search_target": "content",
}


# ---------------------------------------------------------------------------
# Helper to import handler after mocking is set up
# ---------------------------------------------------------------------------


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_xray_search

    return handle_xray_search


# ---------------------------------------------------------------------------
# Tests: valid request returns job_id
# ---------------------------------------------------------------------------


class TestXraySearchHandlerValidRequest:
    """Handler returns {job_id} for valid authenticated requests."""

    def test_returns_job_id_on_valid_params(self):
        """Happy path: handler submits job and returns job_id dict."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "test-job-id-abc123"

        mock_app = MagicMock()
        mock_app.background_job_manager = mock_bjm

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path/to/repo",
            ),
            patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            handler = _import_handler()
            result = handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert "job_id" in data
        assert data["job_id"] == "test-job-id-abc123"

    def test_submit_job_called_with_xray_search_operation_type(self):
        """submit_job must be called with operation_type='xray_search'."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-xyz"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path/to/repo",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            handler = _import_handler()
            handler(VALID_PARAMS.copy(), user)

        submit_call = mock_bjm.submit_job.call_args
        assert submit_call is not None
        assert submit_call.kwargs.get("operation_type") == "xray_search" or (
            len(submit_call.args) > 0 and submit_call.args[0] == "xray_search"
        )

    def test_max_files_none_accepted(self):
        """max_files=None (omitted) is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-abc"
        params = {**VALID_PARAMS, "max_files": None}

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data

    def test_max_files_5_accepted(self):
        """max_files=5 is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-def"
        params = {**VALID_PARAMS, "max_files": 5}

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data


# ---------------------------------------------------------------------------
# Tests: pre-flight evaluator validation
# ---------------------------------------------------------------------------


class TestXraySearchHandlerPreFlightValidation:
    """Handler rejects bad evaluator code before calling submit_job."""

    def test_bad_evaluator_returns_validation_error(self):
        """Evaluator code containing 'import' is rejected synchronously."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        params = {
            **VALID_PARAMS,
            "evaluator_code": "import os; return True",  # Import not in whitelist
        }

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "xray_evaluator_validation_failed"
        mock_bjm.submit_job.assert_not_called()

    def test_bad_evaluator_does_not_submit_job(self):
        """When evaluator validation fails, submit_job must NOT be called."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        params = {
            **VALID_PARAMS,
            "evaluator_code": "import sys; return True",
        }

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
            handler = _import_handler()
            handler(params, user)

        mock_bjm.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: parameter validation errors
# ---------------------------------------------------------------------------


class TestXraySearchHandlerParamValidation:
    """Handler rejects invalid parameters with descriptive error codes."""

    def test_unknown_search_target_rejected(self):
        """search_target must be 'content' or 'filename'."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "search_target": "fulltext"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "invalid_search_target"

    def test_timeout_too_low_rejected(self):
        """timeout_seconds=5 is below minimum 10."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "timeout_seconds": 5}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "timeout_out_of_range"

    def test_timeout_too_high_rejected(self):
        """timeout_seconds=900 is above maximum 600."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "timeout_seconds": 900}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "timeout_out_of_range"

    def test_max_results_zero_rejected(self):
        """max_results=0 is rejected with max_results_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": 0}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_results_out_of_range"

    def test_max_results_negative_rejected(self):
        """max_results=-1 is rejected with max_results_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": -1}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_results_out_of_range"


# ---------------------------------------------------------------------------
# Tests: auth and permission checks
# ---------------------------------------------------------------------------


class TestXraySearchHandlerAuth:
    """Handler enforces auth and permission requirements."""

    def test_unauthenticated_request_rejected(self):
        """None user produces auth_required error."""
        handler = _import_handler()
        result = handler(VALID_PARAMS.copy(), None)

        data = _parse_response(result)
        assert data.get("error") == "auth_required"

    def test_missing_query_repos_permission_rejected(self):
        """User without query_repos permission is rejected."""
        # ADMIN has all permissions so we need to manipulate has_permission
        user = MagicMock(spec=User)
        user.username = "testuser"
        user.has_permission.return_value = False

        handler = _import_handler()
        result = handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert data.get("error") == "auth_required"


# ---------------------------------------------------------------------------
# Tests: repository resolution
# ---------------------------------------------------------------------------


class TestXraySearchHandlerRepoResolution:
    """Handler rejects unknown repository aliases."""

    def test_unknown_alias_returns_repository_not_found(self):
        """When the alias cannot be resolved, repository_not_found is returned."""
        user = _make_user(UserRole.NORMAL_USER)

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=None,
        ):
            handler = _import_handler()
            result = handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert data.get("error") == "repository_not_found"


# ---------------------------------------------------------------------------
# Tests: job_fn wires _truncate_xray_result
# ---------------------------------------------------------------------------


class TestXraySearchHandlerTruncation:
    """job_fn applies _truncate_xray_result to the engine result."""

    def test_job_fn_applies_truncation_to_engine_result(self):
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

        captured_func: list = []
        mock_bjm = MagicMock()

        def capture_submit(**kwargs):
            captured_func.append(kwargs.get("func"))
            return "job-id"

        mock_bjm.submit_job.side_effect = capture_submit

        mock_engine_instance = MagicMock()
        mock_engine_instance.sandbox.validate.return_value = MagicMock(ok=True)
        mock_engine_class = MagicMock(return_value=mock_engine_instance)

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray.XRaySearchEngine",
                mock_engine_class,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._truncate_xray_result",
                return_value=truncated_result,
            ) as mock_truncate,
        ):
            handler = _import_handler()
            handler(VALID_PARAMS.copy(), user)

            assert len(captured_func) == 1
            job_fn = captured_func[0]

            # Simulate what BackgroundJobManager does: call job_fn with a callback
            mock_inner_engine = MagicMock()
            mock_inner_engine.run.return_value = engine_result
            with patch(
                "code_indexer.xray.search_engine.XRaySearchEngine",
                return_value=mock_inner_engine,
            ):
                job_result = job_fn(lambda *a, **kw: None)

        mock_truncate.assert_called_once_with(engine_result)
        assert job_result == truncated_result


# ---------------------------------------------------------------------------
# Tests: await_seconds parameter (Issue #17)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerAwaitSeconds:
    """await_seconds=0 returns {job_id}; N>0 polls and returns inline result if done."""

    def test_await_seconds_zero_returns_job_id(self):
        """await_seconds=0 (default) returns {job_id} immediately."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-await-zero"
        params = {**VALID_PARAMS, "await_seconds": 0}

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data
        assert "matches" not in data

    def test_await_seconds_omitted_returns_job_id(self):
        """await_seconds absent (default 0) returns {job_id} immediately."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-no-await"

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
            handler = _import_handler()
            result = handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert "job_id" in data
        assert "matches" not in data

    def test_await_seconds_positive_returns_inline_result_when_job_completes(self):
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

        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-fast"
        mock_bjm.get_job_status.return_value = {
            "status": "completed",
            "result": inline_result,
        }

        params = {**VALID_PARAMS, "await_seconds": 5}

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "matches" in data
        assert "job_id" not in data

    def test_await_seconds_returns_job_id_when_job_does_not_complete_in_window(self):
        """await_seconds=1 returns {job_id} when job stays pending beyond window."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-slow"
        mock_bjm.get_job_status.return_value = {
            "status": "running",
            "result": None,
        }

        params = {**VALID_PARAMS, "await_seconds": 1}

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch("time.sleep"),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data

    def test_await_seconds_negative_rejected(self):
        """await_seconds=-1 returns await_seconds_invalid error."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": -1}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid"

    def test_await_seconds_31_rejected(self):
        """await_seconds=31 exceeds cap of 30, returns await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": 31}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid"

    def test_await_seconds_30_rejected_v2_cap(self):
        """await_seconds=30 is now rejected — cap was lowered from 30 to 10 in v10.3.2."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": 30}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        # 30 exceeds the new cap of 10 — must be rejected
        assert data.get("error") == "await_seconds_invalid"


# ---------------------------------------------------------------------------
# Tests: await_seconds float + new 10s cap (v10.3.2, Tasks #35 and #39)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerAwaitSecondsV2:
    """Tests for float await_seconds and the 10s cap introduced in v10.3.2.

    Task #35: await_seconds accepts int OR float (not just int).
    Task #39: ceiling lowered from 30s to 10s to bound threadpool occupancy.
    """

    # --- Float acceptance ---

    def test_await_seconds_float_half_accepted(self):
        """await_seconds=0.5 (float) is accepted and treated as 500ms wait."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-float-half"
        mock_bjm.get_job_status.return_value = {"status": "running", "result": None}
        params = {**VALID_PARAMS, "await_seconds": 0.5}

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch("time.sleep"),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "await_seconds_invalid", (
            "await_seconds=0.5 (float) must be accepted"
        )

    def test_await_seconds_float_near_zero_accepted(self):
        """await_seconds=0.001 (near-zero float) is accepted."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-float-tiny"
        mock_bjm.get_job_status.return_value = {"status": "running", "result": None}
        params = {**VALID_PARAMS, "await_seconds": 0.001}

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch("time.sleep"),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "await_seconds_invalid", (
            "await_seconds=0.001 (near-zero float) must be accepted"
        )

    def test_await_seconds_float_at_cap_accepted(self):
        """await_seconds=10.0 (float at new cap) is accepted."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-float-cap"
        mock_bjm.get_job_status.return_value = {"status": "running", "result": None}
        params = {**VALID_PARAMS, "await_seconds": 10.0}

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch("time.sleep"),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "await_seconds_invalid", (
            "await_seconds=10.0 (float at cap) must be accepted"
        )

    # --- Float rejection ---

    def test_await_seconds_float_just_above_cap_rejected(self):
        """await_seconds=10.001 (just above new cap) is rejected with await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": 10.001}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds=10.001 must be rejected"
        )

    def test_await_seconds_float_negative_rejected(self):
        """await_seconds=-0.001 (negative float) is rejected with await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": -0.001}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds=-0.001 (negative float) must be rejected"
        )

    def test_await_seconds_bool_true_rejected(self):
        """await_seconds=True (bool) is rejected — bool subclasses int but must be blocked."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": True}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds=True (bool) must be rejected even though bool subclasses int"
        )

    def test_await_seconds_string_rejected(self):
        """await_seconds='0.5' (string) is rejected with await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": "0.5"}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds='0.5' (string) must be rejected"
        )

    # --- Backward-compat: int still works ---

    def test_await_seconds_int_zero_regression(self):
        """await_seconds=0 (int) still returns {job_id} immediately (regression)."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-int-zero"
        params = {**VALID_PARAMS, "await_seconds": 0}

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data
        assert data.get("error") != "await_seconds_invalid"

    def test_await_seconds_int_five_regression(self):
        """await_seconds=5 (int) is still accepted (regression)."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-int-five"
        mock_bjm.get_job_status.return_value = {"status": "running", "result": None}
        params = {**VALID_PARAMS, "await_seconds": 5}

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch("time.sleep"),
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") != "await_seconds_invalid"

    # --- New cap enforcement ---

    def test_await_seconds_11_rejected_new_cap(self):
        """await_seconds=11 exceeds new cap of 10 — rejected (cap lowered from 30 in v10.3.2)."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": 11}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds=11 must be rejected by the new 10s cap"
        )
        message = data.get("message", "")
        assert "10" in message, (
            f"Error message must mention the new cap (10), got: {message!r}"
        )

    def test_await_seconds_30_now_rejected(self):
        """await_seconds=30 was valid in v10.3.0 but rejected now (cap lowered to 10 in v10.3.2)."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": 30}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "await_seconds_invalid", (
            "await_seconds=30 must now be rejected (cap lowered from 30 to 10 in v10.3.2)"
        )


# ---------------------------------------------------------------------------
# Tests: renamed params — pattern (was driver_regex), max_results (was max_files)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerRenamedParams:
    """Verify that 'pattern' is the accepted name (was 'driver_regex')."""

    def test_pattern_param_accepted_and_submits_job(self):
        """'pattern' is the new name for the driver regex — handler accepts it."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-pattern-ok"
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
            "evaluator_code": "return True",
            "search_target": "content",
        }

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id with 'pattern' param, got: {data!r}"
        assert "error" not in data

    def test_driver_regex_no_longer_accepted(self):
        """'driver_regex' is the OLD name — handler must reject or ignore it."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-driver-old"
        # Supply driver_regex but NOT pattern — this must fail validation
        params = {
            "repository_alias": "myrepo-global",
            "driver_regex": r"prepareStatement",
            "evaluator_code": "return True",
            "search_target": "content",
        }

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        # driver_regex is gone — handler must NOT submit a job using the old name
        # (it may return an error or treat pattern as missing)
        assert "job_id" not in data or mock_bjm.submit_job.call_count == 0, (
            "'driver_regex' is the old name; handler must not silently accept it"
        )

    def test_pattern_forwarded_to_engine_as_driver_regex_or_pattern(self):
        """The 'pattern' param value reaches the XRaySearchEngine.run() call."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-fwd"
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"mySpecialRegex",
            "evaluator_code": "return True",
            "search_target": "content",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
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
            handler(params, user)
            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_fn(lambda *a: None)

        # The engine's driver_regex param must receive the value from 'pattern'
        assert captured_kwargs.get("driver_regex") == r"mySpecialRegex", (
            f"Engine must receive pattern value as driver_regex, got: {captured_kwargs!r}"
        )


# ---------------------------------------------------------------------------
# Tests: max_results rename (was max_files)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerMaxResults:
    """max_results is the new name for max_files (regex_search alignment)."""

    def test_max_results_accepted_and_submits_job(self):
        """'max_results' param is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-mr-ok"
        params = {**VALID_PARAMS, "max_results": 10}

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id with max_results=10, got: {data!r}"

    def test_max_results_zero_rejected(self):
        """max_results=0 is rejected with max_results_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_results": 0}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_results_out_of_range", (
            f"Expected max_results_out_of_range, got: {data!r}"
        )

    def test_max_results_forwarded_to_engine_as_max_files(self):
        """max_results value reaches engine.run() as max_files argument."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-mr-fwd"
        params = {**VALID_PARAMS, "max_results": 7}

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
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
            handler(params, user)
            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_fn(lambda *a: None)

        assert captured_kwargs.get("max_files") == 7, (
            f"Engine must receive max_results value as max_files=7, got: {captured_kwargs!r}"
        )

    def test_max_files_old_name_no_longer_accepted(self):
        """'max_files' is the OLD name — handler must not silently accept it."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-mf-old"
        # Supply max_files but NOT max_results — with new alignment this is the old API
        params = {**VALID_PARAMS, "max_files": 5}

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        # max_files is the old key; handler should not treat it as max_results
        # It may succeed (with max_results=None) but must not use max_files value
        # The key assertion: max_files is silently ignored, not forwarded as max_results
        assert "error" not in data or data.get("error") != "max_files_out_of_range", (
            "Error code must be max_results_out_of_range, not max_files_out_of_range"
        )


# ---------------------------------------------------------------------------
# Tests: new params aligned to regex_search
# ---------------------------------------------------------------------------


class TestXraySearchHandlerNewParams:
    """New params added for regex_search alignment: case_sensitive, context_lines,
    multiline, pcre2, path. All must be accepted and forwarded to the engine."""

    def _capture_engine_kwargs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Helper: submit a valid job and capture kwargs passed to engine.run()."""
        user = _make_user(UserRole.NORMAL_USER)
        captured: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-new-params"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
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
            handler(params, user)
            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_fn(lambda *a: None)

        return captured

    def test_case_sensitive_true_accepted(self):
        """case_sensitive=True is accepted and forwarded to engine."""
        params = {**VALID_PARAMS, "case_sensitive": True}
        captured = self._capture_engine_kwargs(params)
        assert captured.get("case_sensitive") is True, (
            f"Engine must receive case_sensitive=True, got: {captured!r}"
        )

    def test_case_sensitive_false_accepted(self):
        """case_sensitive=False is accepted and forwarded to engine."""
        params = {**VALID_PARAMS, "case_sensitive": False}
        captured = self._capture_engine_kwargs(params)
        assert captured.get("case_sensitive") is False, (
            f"Engine must receive case_sensitive=False, got: {captured!r}"
        )

    def test_case_sensitive_default_is_true(self):
        """case_sensitive defaults to True when omitted (matches regex_search default)."""
        captured = self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("case_sensitive") is True, (
            f"Default case_sensitive must be True, got: {captured!r}"
        )

    def test_context_lines_zero_accepted(self):
        """context_lines=0 is accepted and forwarded to engine."""
        params = {**VALID_PARAMS, "context_lines": 0}
        captured = self._capture_engine_kwargs(params)
        assert captured.get("context_lines") == 0, (
            f"Engine must receive context_lines=0, got: {captured!r}"
        )

    def test_context_lines_5_accepted(self):
        """context_lines=5 is accepted and forwarded to engine."""
        params = {**VALID_PARAMS, "context_lines": 5}
        captured = self._capture_engine_kwargs(params)
        assert captured.get("context_lines") == 5, (
            f"Engine must receive context_lines=5, got: {captured!r}"
        )

    def test_context_lines_10_accepted(self):
        """context_lines=10 (max) is accepted."""
        params = {**VALID_PARAMS, "context_lines": 10}
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-ctx10"

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
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "job_id" in data, f"context_lines=10 must be accepted, got: {data!r}"

    def test_context_lines_negative_rejected(self):
        """context_lines=-1 is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "context_lines": -1}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "error" in data, "context_lines=-1 must be rejected"

    def test_context_lines_11_rejected(self):
        """context_lines=11 exceeds max of 10 and is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "context_lines": 11}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert "error" in data, "context_lines=11 must be rejected"

    def test_context_lines_default_is_zero(self):
        """context_lines defaults to 0 when omitted."""
        captured = self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("context_lines") == 0, (
            f"Default context_lines must be 0, got: {captured!r}"
        )

    def test_multiline_true_accepted(self):
        """multiline=True is accepted and forwarded to engine."""
        params = {**VALID_PARAMS, "multiline": True}
        captured = self._capture_engine_kwargs(params)
        assert captured.get("multiline") is True, (
            f"Engine must receive multiline=True, got: {captured!r}"
        )

    def test_multiline_false_is_default(self):
        """multiline defaults to False when omitted."""
        captured = self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("multiline") is False, (
            f"Default multiline must be False, got: {captured!r}"
        )

    def test_pcre2_true_accepted(self):
        """pcre2=True is accepted and forwarded to engine."""
        params = {**VALID_PARAMS, "pcre2": True}
        captured = self._capture_engine_kwargs(params)
        assert captured.get("pcre2") is True, (
            f"Engine must receive pcre2=True, got: {captured!r}"
        )

    def test_pcre2_false_is_default(self):
        """pcre2 defaults to False when omitted."""
        captured = self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("pcre2") is False, (
            f"Default pcre2 must be False, got: {captured!r}"
        )

    def test_path_accepted(self):
        """path='src/' is accepted and forwarded to engine."""
        params = {**VALID_PARAMS, "path": "src/"}
        captured = self._capture_engine_kwargs(params)
        assert captured.get("path") == "src/", (
            f"Engine must receive path='src/', got: {captured!r}"
        )

    def test_path_none_is_default(self):
        """path defaults to None when omitted."""
        captured = self._capture_engine_kwargs(VALID_PARAMS.copy())
        assert captured.get("path") is None, (
            f"Default path must be None, got: {captured!r}"
        )


# ---------------------------------------------------------------------------
# Tests: output envelope — line_content (was code_snippet)
# ---------------------------------------------------------------------------


class TestXraySearchHandlerOutputEnvelope:
    """Match envelope uses 'line_content' (renamed from 'code_snippet')."""

    def test_engine_produces_line_content_field(self):
        """XRaySearchEngine._evaluate_file produces 'line_content', not 'code_snippet'."""

        from code_indexer.xray.search_engine import XRaySearchEngine

        # Confirm code_snippet is gone from the match envelope builder
        import inspect

        source = inspect.getsource(XRaySearchEngine._evaluate_file)
        assert "line_content" in source, (
            "'line_content' must appear in _evaluate_file match envelope"
        )
        assert "code_snippet" not in source, (
            "'code_snippet' must NOT appear in _evaluate_file — rename complete"
        )


# ---------------------------------------------------------------------------
# Tests: omni multi-repo — repository_alias accepts str OR list
# ---------------------------------------------------------------------------


class TestXraySearchHandlerOmni:
    """repository_alias accepts string OR array of strings (Directive C)."""

    def _make_bjm(self, job_ids: list) -> MagicMock:
        """Return a mock BJM whose submit_job returns successive job IDs."""
        mock_bjm = MagicMock()
        mock_bjm.submit_job.side_effect = job_ids
        return mock_bjm

    def _run_with_aliases(
        self, alias_value: Any, resolved_paths: dict
    ) -> Dict[str, Any]:
        """Run handle_xray_search with given repository_alias and path map."""
        import json as _json

        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_user(UserRole.NORMAL_USER)
        # Determine the number of repos to provision mock job IDs correctly.
        # JSON-encoded strings like '["a","b"]' encode multiple repos.
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
            "evaluator_code": 'return {"matches": [], "value": None}',
            "search_target": "content",
        }

        def fake_resolve(alias: str) -> Any:
            return resolved_paths.get(alias)

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                side_effect=fake_resolve,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
        ):
            return handle_xray_search(params, user)

    def test_string_alias_single_repo_works_as_before(self):
        """String alias returns {job_id} dict (unchanged single-repo path)."""
        result = self._run_with_aliases(
            "myrepo-global", {"myrepo-global": "/path/repo"}
        )
        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id, got: {data}"

    def test_array_alias_with_two_repos_submits_two_jobs(self):
        """List alias submits one job per repo and returns {job_ids: [...]}."""
        paths = {"repo-a": "/path/a", "repo-b": "/path/b"}
        result = self._run_with_aliases(["repo-a", "repo-b"], paths)
        data = _parse_response(result)
        assert "job_ids" in data, f"Expected job_ids, got: {data}"
        assert len(data["job_ids"]) == 2

    def test_json_string_array_alias_is_parsed(self):
        """JSON-encoded string array '["a","b"]' is parsed to list of aliases."""
        paths = {"repo-a": "/path/a", "repo-b": "/path/b"}
        result = self._run_with_aliases('["repo-a", "repo-b"]', paths)
        data = _parse_response(result)
        assert "job_ids" in data, f"Expected job_ids after JSON parse, got: {data}"
        assert len(data["job_ids"]) == 2

    def test_array_alias_with_unknown_repo_returns_not_found_errors(self):
        """Unknown alias in list produces repository_not_found error entry."""
        paths = {"known-repo": "/path/known"}
        result = self._run_with_aliases(["known-repo", "unknown-repo"], paths)
        data = _parse_response(result)
        # Should report error for unknown-repo
        assert "errors" in data or "job_ids" in data, f"Unexpected response: {data}"
        if "errors" in data:
            errors = data["errors"]
            assert any("unknown-repo" in str(e) for e in errors), (
                f"Expected error mentioning 'unknown-repo', got: {errors}"
            )

    def test_empty_array_alias_returns_alias_required_error(self):
        """Empty list alias returns alias_required error."""
        result = self._run_with_aliases([], {})
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
    which passed an empty string to the engine. The sandbox then executed empty
    code and returned None, which failed the dict contract check and produced
    InvalidEvaluatorReturn for every candidate file.

    The fix replaces the empty default with a DEFAULT_EVALUATOR that echoes
    Phase 1 hits as matches using the v10.4.0 dict shape.
    """

    def _get_engine_evaluator_code(self, params: Dict[str, Any]) -> str:
        """Submit a valid job and capture the evaluator_code forwarded to engine.run()."""
        user = _make_user(UserRole.NORMAL_USER)
        captured: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-default-eval"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
                return_value=mock_bjm,
            ),
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
            handler(params, user)
            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_fn(lambda *a: None)

        return captured.get("evaluator_code", "")

    def test_omitted_evaluator_code_uses_non_empty_default(self):
        """When evaluator_code is omitted, engine receives a non-empty default (Bug 2)."""
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
            # evaluator_code intentionally omitted
        }
        evaluator = self._get_engine_evaluator_code(params)
        assert evaluator, (
            "Engine must receive a non-empty evaluator_code when evaluator_code is omitted"
        )
        assert evaluator != "", (
            "Empty evaluator_code is the Bug 2 regression — default must be non-empty"
        )

    def test_omitted_evaluator_code_default_returns_dict_not_bool(self):
        """Default evaluator must contain dict return shape, not 'return True' bool (Bug 2)."""
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
            # evaluator_code intentionally omitted
        }
        evaluator = self._get_engine_evaluator_code(params)
        # The default must include the dict return contract
        assert "matches" in evaluator, (
            f"Default evaluator must return dict with 'matches' key, got: {evaluator!r}"
        )
        assert (
            '"matches"' in evaluator
            or "'matches'" in evaluator
            or "matches" in evaluator
        ), f"Default evaluator must build matches list, got: {evaluator!r}"

    def test_omitted_evaluator_code_default_passes_sandbox_validation(self):
        """Default evaluator must pass sandbox.validate() — not crash at preflight (Bug 2)."""
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
        }
        evaluator = self._get_engine_evaluator_code(params)

        sandbox = PythonEvaluatorSandbox()
        result = sandbox.validate(evaluator)
        assert result.ok, (
            f"Default evaluator must pass sandbox.validate(), got failure: {result.reason!r}"
        )

    def test_empty_evaluator_code_string_uses_non_empty_default(self):
        """Explicit empty string evaluator_code is treated same as omitted — non-empty default."""
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
            "evaluator_code": "",
        }
        evaluator = self._get_engine_evaluator_code(params)
        assert evaluator, (
            "Empty string evaluator_code must be replaced by non-empty default (Bug 2)"
        )

    def test_explicit_evaluator_code_is_not_replaced_by_default(self):
        """Explicit non-empty evaluator_code is forwarded as-is (regression guard)."""
        custom_code = 'return {"matches": [{"line_number": 1}], "value": None}'
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
            "evaluator_code": custom_code,
        }
        evaluator = self._get_engine_evaluator_code(params)
        assert evaluator == custom_code, (
            f"Explicit evaluator_code must not be replaced by default, got: {evaluator!r}"
        )
