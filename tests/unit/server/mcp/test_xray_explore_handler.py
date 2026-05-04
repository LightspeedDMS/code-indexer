"""Unit tests for xray_explore MCP handler.

Tests the thin handler shim that validates inputs, pre-flight checks the
evaluator, submits a background job with include_ast_debug=True, and returns
{job_id}.

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
    from code_indexer.server.mcp.handlers.xray import handle_xray_explore

    return handle_xray_explore


# ---------------------------------------------------------------------------
# Tests: valid request returns job_id
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerValidRequest:
    """Handler returns {job_id} for valid authenticated requests."""

    def test_returns_job_id_on_valid_params(self):
        """Happy path: handler submits job and returns job_id dict."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-job-id-abc123"

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
            result = handler(VALID_PARAMS.copy(), user)

        data = _parse_response(result)
        assert "job_id" in data
        assert data["job_id"] == "explore-job-id-abc123"

    def test_submit_job_called_with_xray_explore_operation_type(self):
        """submit_job must be called with operation_type='xray_explore' (M5: distinct from xray_search)."""
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
        assert submit_call.kwargs.get("operation_type") == "xray_explore" or (
            len(submit_call.args) > 0 and submit_call.args[0] == "xray_explore"
        )

    def test_job_fn_passes_include_ast_debug_true(self):
        """The job function submitted to BJM calls engine.run with include_ast_debug=True."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()

        def fake_submit_job(**kwargs):
            # We'll capture via patching XRaySearchEngine.run
            return "captured-job-id"

        mock_bjm.submit_job.side_effect = fake_submit_job

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path/to/repo",
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
            handler(VALID_PARAMS.copy(), user)

            # Extract the submitted func from the call and invoke it
            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_fn(lambda *a: None)

        assert captured_kwargs.get("include_ast_debug") is True

    def test_max_debug_nodes_defaults_to_50(self):
        """When max_debug_nodes is omitted, job fn uses default of 50."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-default"

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
            handler(VALID_PARAMS.copy(), user)

            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_fn(lambda *a: None)

        assert captured_kwargs.get("max_debug_nodes") == 50

    def test_max_debug_nodes_custom_value_forwarded(self):
        """When max_debug_nodes=20 is provided, job fn uses 20."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-custom"
        params = {**VALID_PARAMS, "max_debug_nodes": 20}

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

        assert captured_kwargs.get("max_debug_nodes") == 20

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

    def test_max_files_3_accepted(self):
        """max_files=3 is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-def"
        params = {**VALID_PARAMS, "max_files": 3}

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
# Tests: max_debug_nodes validation
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerMaxDebugNodesValidation:
    """max_debug_nodes must be in range 1..500."""

    def test_max_debug_nodes_zero_rejected(self):
        """max_debug_nodes=0 is rejected with max_debug_nodes_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_debug_nodes": 0}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_debug_nodes_out_of_range"

    def test_max_debug_nodes_1000_rejected(self):
        """max_debug_nodes=1000 is above maximum 500 and is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_debug_nodes": 1000}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_debug_nodes_out_of_range"

    def test_max_debug_nodes_1_accepted(self):
        """max_debug_nodes=1 is the minimum valid value."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-min"
        params = {**VALID_PARAMS, "max_debug_nodes": 1}

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

    def test_max_debug_nodes_500_accepted(self):
        """max_debug_nodes=500 is the maximum valid value."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-max"
        params = {**VALID_PARAMS, "max_debug_nodes": 500}

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

    def test_max_debug_nodes_501_rejected(self):
        """max_debug_nodes=501 is above maximum 500 and is rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_debug_nodes": 501}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_debug_nodes_out_of_range"

    def test_max_debug_nodes_negative_rejected(self):
        """max_debug_nodes=-1 is rejected with max_debug_nodes_out_of_range."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "max_debug_nodes": -1}

        with patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value="/some/path",
        ):
            handler = _import_handler()
            result = handler(params, user)

        data = _parse_response(result)
        assert data.get("error") == "max_debug_nodes_out_of_range"

    def test_max_debug_nodes_validation_does_not_submit_job(self):
        """When max_debug_nodes is invalid, submit_job must NOT be called."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        params = {**VALID_PARAMS, "max_debug_nodes": 0}

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
# Tests: pre-flight evaluator validation
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerPreFlightValidation:
    """Handler rejects bad evaluator code before calling submit_job."""

    def test_bad_evaluator_returns_validation_error(self):
        """Evaluator code containing 'import' is rejected synchronously."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        params = {
            **VALID_PARAMS,
            "evaluator_code": "import os; return True",
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


# ---------------------------------------------------------------------------
# Tests: shared parameter validation
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerSharedParamValidation:
    """Handler shares validation logic with xray_search for common parameters."""

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


# ---------------------------------------------------------------------------
# Tests: auth and permission checks
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerAuth:
    """Handler enforces auth and permission requirements."""

    def test_unauthenticated_request_rejected(self):
        """None user produces auth_required error."""
        handler = _import_handler()
        result = handler(VALID_PARAMS.copy(), None)

        data = _parse_response(result)
        assert data.get("error") == "auth_required"

    def test_missing_query_repos_permission_rejected(self):
        """User without query_repos permission is rejected."""
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


class TestXrayExploreHandlerRepoResolution:
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


class TestXrayExploreHandlerExtrasNotInstalled:
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
# M5: operation_type must be 'xray_explore', not 'xray_search'
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerOperationType:
    """xray_explore submits jobs with operation_type='xray_explore'."""

    def test_explore_uses_distinct_operation_type(self):
        """submit_job must be called with operation_type='xray_explore', not 'xray_search' (M5)."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-op-type"

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
        actual_op_type = submit_call.kwargs.get("operation_type") or (
            submit_call.args[0] if submit_call.args else None
        )
        assert actual_op_type == "xray_explore", (
            f"Expected operation_type='xray_explore', got {actual_op_type!r}"
        )


# ---------------------------------------------------------------------------
# M2: evaluator_code optional with default 'return True'
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerOptionalEvaluatorCode:
    """evaluator_code is optional; default is 'return True'."""

    def test_explore_accepts_missing_evaluator_code(self):
        """xray_explore succeeds when evaluator_code is omitted; engine receives 'return True' (M2)."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-no-eval"
        params = {
            "repository_alias": "myrepo-global",
            "driver_regex": r"prepareStatement",
            "search_target": "content",
            # evaluator_code intentionally omitted
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path/to/repo",
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
            result = handler(params, user)

            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_fn(lambda *a: None)

        data = _parse_response(result)
        assert "job_id" in data, (
            f"Expected job_id when evaluator_code is omitted, got: {data!r}"
        )
        assert "error" not in data, (
            f"Expected no error when evaluator_code is omitted, got: {data!r}"
        )
        assert captured_kwargs.get("evaluator_code") == "return True", (
            f"Engine must receive 'return True' when evaluator_code is omitted, "
            f"got: {captured_kwargs.get('evaluator_code')!r}"
        )

    def test_explore_empty_evaluator_code_engine_receives_default(self):
        """Engine receives 'return True' when evaluator_code is empty string (M2)."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-empty-eval"
        params = {
            "repository_alias": "myrepo-global",
            "driver_regex": r"prepareStatement",
            "search_target": "content",
            "evaluator_code": "",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
                return_value="/some/path/to/repo",
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
            result = handler(params, user)

            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_fn(lambda *a: None)

        data = _parse_response(result)
        assert "job_id" in data, (
            f"Expected job_id when evaluator_code is empty, got: {data!r}"
        )
        assert captured_kwargs.get("evaluator_code") == "return True", (
            f"Engine must receive 'return True' when evaluator_code is empty string, "
            f"got: {captured_kwargs.get('evaluator_code')!r}"
        )
