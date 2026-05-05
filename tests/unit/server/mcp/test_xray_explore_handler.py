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
    "pattern": r"prepareStatement",
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

    def test_max_results_zero_rejected(self):
        """max_results=0 is rejected with max_results_out_of_range (renamed from max_files)."""
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
    """evaluator_code is optional; v10.4.1 default emits one match per Phase 1 hit (dict-return)."""

    def test_explore_accepts_missing_evaluator_code_v10_4_1(self):
        """xray_explore succeeds when evaluator_code is omitted; engine receives _DEFAULT_EVALUATOR_CODE (v10.4.1 dict-return contract)."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-no-eval"
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
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
        from code_indexer.server.mcp.handlers.xray import _DEFAULT_EVALUATOR_CODE
        assert captured_kwargs.get("evaluator_code") == _DEFAULT_EVALUATOR_CODE, (
            f"Engine must receive _DEFAULT_EVALUATOR_CODE (v10.4.1 dict-return) "
            f"when evaluator_code is omitted, got: {captured_kwargs.get('evaluator_code')!r}"
        )

    def test_explore_empty_evaluator_code_engine_receives_default_v10_4_1(self):
        """Engine receives _DEFAULT_EVALUATOR_CODE when evaluator_code is empty string (v10.4.1)."""
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-empty-eval"
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
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
        from code_indexer.server.mcp.handlers.xray import _DEFAULT_EVALUATOR_CODE
        assert captured_kwargs.get("evaluator_code") == _DEFAULT_EVALUATOR_CODE, (
            f"Engine must receive _DEFAULT_EVALUATOR_CODE (v10.4.1 dict-return) "
            f"when evaluator_code is empty string, got: {captured_kwargs.get('evaluator_code')!r}"
        )


# ---------------------------------------------------------------------------
# matched_node block (Issue #14)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerMatchedNode:
    """xray_explore engine is called with include_ast_debug=True which produces
    matched_node per match entry (Issue #14).

    The xray_explore handler passes include_ast_debug=True to XRaySearchEngine.run();
    the matched_node block appears in each match because search_engine._evaluate_file
    emits it alongside ast_debug when include_ast_debug=True.
    """

    def test_explore_job_fn_produces_matched_node_in_matches(self):
        """The job function result includes matched_node in each match entry (Issue #14).

        Verifies: (1) include_ast_debug=True is passed to engine.run, AND
        (2) the job result propagates match entries with matched_node field.
        """
        user = _make_user(UserRole.NORMAL_USER)
        captured_kwargs: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-matched-node"

        sample_match_with_matched_node = {
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

        engine_result = {
            "matches": [sample_match_with_matched_node],
            "evaluation_errors": [],
            "files_processed": 1,
            "files_total": 1,
            "elapsed_seconds": 0.1,
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
                side_effect=lambda **kw: captured_kwargs.update(kw) or engine_result,
            ),
        ):
            handler = _import_handler()
            handler(VALID_PARAMS.copy(), user)

            submit_call = mock_bjm.submit_job.call_args
            job_fn = submit_call.kwargs["func"]
            job_result = job_fn(lambda *a: None)

        # Verify include_ast_debug=True was passed so engine emits matched_node
        assert captured_kwargs.get("include_ast_debug") is True, (
            "xray_explore must pass include_ast_debug=True to engine"
        )
        # Verify job result propagates matches with matched_node
        assert "matches" in job_result
        assert len(job_result["matches"]) >= 1
        first_match = job_result["matches"][0]
        assert "matched_node" in first_match, (
            "Each match entry must contain matched_node block (Issue #14)"
        )


# ---------------------------------------------------------------------------
# Tests: await_seconds parameter (Issue #17)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerAwaitSeconds:
    """await_seconds=0 returns {job_id}; N>0 polls and returns inline result if done."""

    def test_await_seconds_zero_returns_job_id(self):
        """await_seconds=0 (default) returns {job_id} immediately."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-await-zero"
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

    def test_await_seconds_positive_returns_inline_result_when_job_completes(self):
        """await_seconds=5 returns inline {matches, ...} when explore job completes."""
        user = _make_user(UserRole.NORMAL_USER)
        inline_result = {
            "matches": [{"file_path": "a.py", "ast_debug": {}}],
            "evaluation_errors": [],
            "files_processed": 1,
            "files_total": 1,
            "elapsed_seconds": 0.2,
            "truncated": False,
            "cache_handle": None,
        }

        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-fast-job"
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


# ---------------------------------------------------------------------------
# Tests: await_seconds float + new 10s cap (v10.3.2, Tasks #35 and #39)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerAwaitSecondsV2:
    """Tests for float await_seconds and the 10s cap introduced in v10.3.2.

    Task #35: await_seconds accepts int OR float (not just int).
    Task #39: ceiling lowered from 30s to 10s to bound threadpool occupancy.
    """

    # --- Float acceptance ---

    def test_await_seconds_float_half_accepted(self):
        """await_seconds=0.5 (float) is accepted and treated as 500ms wait."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-float-half"
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
        mock_bjm.submit_job.return_value = "explore-float-tiny"
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
        mock_bjm.submit_job.return_value = "explore-float-cap"
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
        mock_bjm.submit_job.return_value = "explore-int-zero"
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
        mock_bjm.submit_job.return_value = "explore-int-five"
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


class TestXrayExploreHandlerRenamedParams:
    """Verify that 'pattern' is the accepted name (was 'driver_regex')."""

    def test_pattern_param_accepted_and_submits_job(self):
        """'pattern' is the new name for the driver regex — handler accepts it."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-job-pattern-ok"
        params = {
            "repository_alias": "myrepo-global",
            "pattern": r"prepareStatement",
            "evaluator_code": "return True",
            "search_target": "content",
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
        mock_bjm.submit_job.return_value = "explore-driver-old"
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
        assert "job_id" not in data or mock_bjm.submit_job.call_count == 0, (
            "'driver_regex' is the old name; handler must not silently accept it"
        )


# ---------------------------------------------------------------------------
# Tests: max_results rename (was max_files)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerMaxResults:
    """max_results is the new name for max_files in xray_explore."""

    def test_max_results_accepted_and_submits_job(self):
        """'max_results' param is accepted and does not produce an error."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-mr-ok"
        params = {**VALID_PARAMS, "max_results": 5}

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
        assert "job_id" in data, f"Expected job_id with max_results=5, got: {data!r}"

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


# ---------------------------------------------------------------------------
# Tests: new params aligned to regex_search
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerNewParams:
    """New params added for regex_search alignment: case_sensitive, context_lines,
    multiline, pcre2, path. All must be accepted by xray_explore."""

    def test_case_sensitive_true_accepted(self):
        """case_sensitive=True is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-cs-true"
        params = {**VALID_PARAMS, "case_sensitive": True}

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
        assert "job_id" in data, f"case_sensitive=True must be accepted, got: {data!r}"

    def test_context_lines_5_accepted(self):
        """context_lines=5 is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-ctx5"
        params = {**VALID_PARAMS, "context_lines": 5}

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
        assert "job_id" in data, f"context_lines=5 must be accepted, got: {data!r}"

    def test_multiline_true_accepted(self):
        """multiline=True is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-ml-true"
        params = {**VALID_PARAMS, "multiline": True}

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
        assert "job_id" in data, f"multiline=True must be accepted, got: {data!r}"

    def test_pcre2_true_accepted(self):
        """pcre2=True is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-pcre2-true"
        params = {**VALID_PARAMS, "pcre2": True}

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
        assert "job_id" in data, f"pcre2=True must be accepted, got: {data!r}"

    def test_path_accepted(self):
        """path='src/' is accepted by xray_explore."""
        user = _make_user(UserRole.NORMAL_USER)
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "explore-path-ok"
        params = {**VALID_PARAMS, "path": "src/"}

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
        assert "job_id" in data, f"path='src/' must be accepted, got: {data!r}"


# ---------------------------------------------------------------------------
# Tests: omni multi-repo — repository_alias accepts str OR list (Bug 1 fix)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerOmni:
    """repository_alias accepts string OR array of strings in xray_explore.

    Bug 1 (v10.4.1): handle_xray_explore was missing the _parse_json_string_array
    normalization step, causing AttributeError: 'list' object has no attribute
    'endswith' when a native list or JSON-encoded array was passed.
    """

    def _make_bjm(self, job_ids: list) -> MagicMock:
        """Return a mock BJM whose submit_job returns successive job IDs."""
        mock_bjm = MagicMock()
        mock_bjm.submit_job.side_effect = job_ids
        return mock_bjm

    def _run_with_aliases(
        self, alias_value: Any, resolved_paths: dict
    ) -> Dict[str, Any]:
        """Run handle_xray_explore with given repository_alias and path map."""
        import json as _json

        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        user = _make_user(UserRole.NORMAL_USER)
        # Determine the number of repos to provision mock job IDs correctly.
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
        job_ids = [f"explore-job-{i}" for i in range(len(aliases))]
        mock_bjm = self._make_bjm(job_ids)

        params = {
            "repository_alias": alias_value,
            "pattern": r"TODO",
            "search_target": "content",
            # evaluator_code omitted — default 'return True' path
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
            return handle_xray_explore(params, user)

    def test_string_alias_single_repo_works_as_before(self):
        """String alias returns {job_id} dict — unchanged single-repo path (regression)."""
        result = self._run_with_aliases(
            "myrepo-global", {"myrepo-global": "/path/repo"}
        )
        data = _parse_response(result)
        assert "job_id" in data, f"Expected job_id for single string alias, got: {data}"

    def test_native_list_alias_does_not_crash(self):
        """Native list ['repo-a', 'repo-b'] must NOT crash with AttributeError (Bug 1)."""
        paths = {"repo-a": "/path/a", "repo-b": "/path/b"}
        # This crashed with AttributeError: 'list' object has no attribute 'endswith'
        # before the fix because _resolve_repo_path received the raw list.
        result = self._run_with_aliases(["repo-a", "repo-b"], paths)
        data = _parse_response(result)
        assert "job_ids" in data, f"Expected job_ids for list alias, got: {data}"
        assert len(data["job_ids"]) == 2

    def test_json_string_array_alias_is_parsed(self):
        """JSON-encoded string '['a','b']' is parsed to list of aliases (Bug 1)."""
        paths = {"repo-a": "/path/a", "repo-b": "/path/b"}
        result = self._run_with_aliases('["repo-a", "repo-b"]', paths)
        data = _parse_response(result)
        assert "job_ids" in data, f"Expected job_ids after JSON parse, got: {data}"
        assert len(data["job_ids"]) == 2

    def test_empty_array_alias_returns_alias_required_error(self):
        """Empty list [] returns alias_required error, not crash (Bug 1)."""
        result = self._run_with_aliases([], {})
        data = _parse_response(result)
        assert data.get("error") == "alias_required", (
            f"Expected alias_required for empty list, got: {data}"
        )

    def test_list_with_unknown_repo_returns_errors_entry(self):
        """Unknown alias in list produces repository_not_found error entry."""
        paths = {"known-repo": "/path/known"}
        result = self._run_with_aliases(["known-repo", "unknown-repo"], paths)
        data = _parse_response(result)
        assert "errors" in data or "job_ids" in data, f"Unexpected response: {data}"
        if "errors" in data:
            errors = data["errors"]
            assert any("unknown-repo" in str(e) for e in errors), (
                f"Expected error mentioning 'unknown-repo', got: {errors}"
            )

    def test_list_does_not_reach_endswith(self):
        """Feeding a list never reaches .endswith() or any string-only method (Bug 1).

        This is the canonical regression: before the fix, passing a list to
        handle_xray_explore raised AttributeError: 'list' object has no attribute
        'endswith'. This test verifies the handler completes without that error.
        """
        paths = {"repo-x": "/path/x"}
        try:
            result = self._run_with_aliases(["repo-x"], paths)
            data = _parse_response(result)
            # Any valid response (job_ids, error codes) is fine — just no crash
            assert isinstance(data, dict), f"Expected dict response, got: {type(data)}"
        except AttributeError as exc:
            raise AssertionError(
                f"handle_xray_explore crashed with AttributeError when given a list: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Tests: default evaluator produces dict contract (Bug 2 fix)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerDefaultEvaluator:
    """When evaluator_code is omitted, xray_explore uses a dict-contract default.

    Bug 2 (v10.4.1): handle_xray_explore defaulted to 'return True' (legacy bool
    contract). Under v10.4.0, the sandbox treats a bool return as
    InvalidEvaluatorReturn for every candidate file, producing zero matches.

    The fix replaces the empty default with _DEFAULT_EVALUATOR_CODE which echoes
    Phase 1 hits as matches using the v10.4.0 dict shape.
    """

    def _get_engine_evaluator_code(self, params: Dict[str, Any]) -> str:
        """Submit a valid explore job and capture the evaluator_code forwarded to engine.run()."""
        user = _make_user(UserRole.NORMAL_USER)
        captured: Dict[str, Any] = {}
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "job-default-eval-explore"

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

    def test_default_evaluator_n_hits_produce_n_matches(self):
        """Omitting evaluator_code → N regex hits produce N matches[], no InvalidEvaluatorReturn.

        Bug 2 regression test (v10.4.1): the old 'return True' bool default triggered
        InvalidEvaluatorReturn for every file under the v10.4.0 dict contract.
        _DEFAULT_EVALUATOR_CODE must echo each match_position entry as a match dict.
        """
        from code_indexer.server.mcp.handlers.xray import _DEFAULT_EVALUATOR_CODE
        from code_indexer.xray.sandbox import PythonEvaluatorSandbox

        # Simulate 3 Phase 1 regex hits on a single candidate file.
        n_hits = 3
        match_positions = [
            {
                "line_number": i + 1,
                "line_content": f"TODO item {i + 1}",
                "column": 0,
                "byte_offset": i * 20,
                "context_before": [],
                "context_after": [],
            }
            for i in range(n_hits)
        ]

        # The sandbox needs a real XRayNode; use a minimal Python file parse.
        from code_indexer.xray.ast_engine import AstSearchEngine

        engine = AstSearchEngine()
        source = "x = 1\n" * n_hits
        root = engine.parse(source.encode(), "python")

        sandbox = PythonEvaluatorSandbox()
        result = sandbox.run(
            _DEFAULT_EVALUATOR_CODE,
            node=root,
            root=root,
            source=source,
            lang="python",
            file_path="/fake/file.py",
            match_positions=match_positions,
        )

        # Must not fail — no InvalidEvaluatorReturn.
        assert result.failure is None, (
            f"Default evaluator must not fail (failure={result.failure!r}, "
            f"detail={result.detail!r}). This is the Bug 2 regression."
        )
        assert isinstance(result.value, dict), (
            f"Default evaluator must return a dict, got {type(result.value).__name__!r}"
        )
        matches = result.value.get("matches", [])
        assert len(matches) == n_hits, (
            f"Expected {n_hits} matches for {n_hits} Phase 1 hits, got {len(matches)}. "
            f"matches={matches!r}"
        )
        # Each match must carry the line_number from the corresponding hit.
        for i, match in enumerate(matches):
            assert match.get("line_number") == i + 1, (
                f"Match {i} must have line_number={i + 1}, got {match!r}"
            )
