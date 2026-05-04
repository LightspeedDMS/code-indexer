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
    "driver_regex": r"prepareStatement",
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

    def test_max_files_zero_rejected(self):
        """max_files=0 is rejected with max_files_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_files": 0}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_files_out_of_range"

    def test_max_files_negative_rejected(self):
        """max_files=-1 is rejected with max_files_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_files": -1}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_files_out_of_range"


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
# Tests: xray extras not installed
# ---------------------------------------------------------------------------


class TestXraySearchHandlerExtrasNotInstalled:
    """Handler returns xray_extras_not_installed when tree-sitter unavailable."""

    def test_extras_not_installed_error_returned(self):
        """When XRayExtrasNotInstalled is raised, handler returns the error code."""
        from code_indexer.xray.errors import XRayExtrasNotInstalled

        user = _make_user(UserRole.NORMAL_USER)

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray.XRaySearchEngine",
                side_effect=XRayExtrasNotInstalled("tree_sitter_languages"),
            ),
        ):
            handler = _import_handler()
            result = handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert data.get("error") == "xray_extras_not_installed"


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
