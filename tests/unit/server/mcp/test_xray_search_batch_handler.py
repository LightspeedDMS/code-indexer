"""Unit tests for xray_search_batch MCP handler (Story #1055).

Tests:
- resolve_batch_evaluator pure helper (inline code, default, pattern_not_found,
  repo-specific shadow, service exception)
- _truncate_xray_batch_result (small inline, large cached, no-cache passthrough)
- handle_xray_search_batch validation (all error codes from spec)
- handle_xray_search_batch repo resolution + global fallback
- handle_xray_search_batch job submission (single job_id, operation_type)
- _run_xray_batch_job worker (match tagging, progress per-repo, phase1_failed,
  evaluation_errors, cancellation, cell_execution_error, pattern error, counters,
  timeout)

Mocking strategy:
- _resolve_repo_path: mocked (needs live alias manager)
- BackgroundJobManager.submit_job: mocked (captured call args)
- XRaySearchEngine.run: mocked (canned cell results)
- _get_arm_and_grm: mocked (global fallback helpers)
- User/permission: real User model
- validate_rust_evaluator: real (no mock for whitelist tests)
- resolve_batch_evaluator: mocked in worker tests (external to cells)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap MCP content envelope."""
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


VALID_EVAL = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"


def _valid_params(**overrides) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "repository_alias": "myrepo-global",
        "scans": [
            {
                "driver_regex": r"def ",
                "evaluator_code": VALID_EVAL,
                "search_target": "content",
            }
        ],
    }
    base.update(overrides)
    return base


def _make_cell_result(
    matches=None, eval_errors=None, partial=False, phase1_failed=False
):
    return {
        "matches": matches or [],
        "evaluation_errors": eval_errors or [],
        "files_processed": 1,
        "files_total": 1,
        "elapsed_seconds": 0.1,
        "partial": partial,
        "phase1_failed": phase1_failed,
        "phase1_error": "driver failed" if phase1_failed else None,
    }


# ---------------------------------------------------------------------------
# Tests: resolve_batch_evaluator pure helper
# ---------------------------------------------------------------------------


class TestResolveBatchEvaluator:
    """Pure resolver returns (eval_code, None) or ('', structured_error)."""

    def test_inline_evaluator_code_returned(self):
        from code_indexer.server.mcp.handlers.xray_batch import resolve_batch_evaluator

        scan = {"driver_regex": r"def ", "evaluator_code": VALID_EVAL}
        code, err = resolve_batch_evaluator(scan, "myrepo-global", Path("/cidx-meta"))
        assert err is None
        assert "evaluate_node" in code

    def test_neither_returns_default_evaluator(self):
        from code_indexer.server.mcp.handlers.xray_batch import resolve_batch_evaluator

        scan = {"driver_regex": r"def "}
        code, err = resolve_batch_evaluator(scan, "myrepo-global", Path("/cidx-meta"))
        assert err is None
        assert "evaluate_node" in code

    def test_pattern_not_found_returns_structured_error(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import resolve_batch_evaluator

        cidx_meta = tmp_path / "cidx-meta"
        cidx_meta.mkdir()
        # No pattern files - should return pattern_not_found
        scan = {"driver_regex": r"def ", "pattern_name": "nonexistent"}
        code, err = resolve_batch_evaluator(scan, "myrepo-global", cidx_meta)
        assert code == ""
        assert err is not None
        assert err["error"] == "pattern_not_found"

    def test_pattern_found_in_any_scope(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import resolve_batch_evaluator

        cidx_meta = tmp_path / "cidx-meta"
        any_dir = cidx_meta / "xray-patterns" / "__any__"
        any_dir.mkdir(parents=True)
        (any_dir / "my-pattern.yaml").write_text(
            "name: my-pattern\ndescription: t\nlanguage: python\nauthor: t\n"
            "created_at: '2024-01-01'\n"
            f"evaluator_code: '{VALID_EVAL}'\n"
        )
        scan = {"driver_regex": r"def ", "pattern_name": "my-pattern"}
        code, err = resolve_batch_evaluator(scan, "myrepo-global", cidx_meta)
        assert err is None
        assert "evaluate_node" in code

    def test_repo_specific_pattern_shadows_any(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import resolve_batch_evaluator

        cidx_meta = tmp_path / "cidx-meta"
        any_dir = cidx_meta / "xray-patterns" / "__any__"
        repo_dir = cidx_meta / "xray-patterns" / "myrepo-global"
        any_dir.mkdir(parents=True)
        repo_dir.mkdir(parents=True)

        (any_dir / "p.yaml").write_text(
            "name: p\ndescription: any\nlanguage: python\nauthor: t\n"
            "created_at: '2024-01-01'\n"
            "evaluator_code: 'fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![EvalFinding { pattern: \"any_scope\".to_string(), line: 1, snippet: String::new() }] }'\n"
        )
        (repo_dir / "p.yaml").write_text(
            "name: p\ndescription: repo\nlanguage: python\nauthor: t\n"
            "created_at: '2024-01-01'\n"
            "evaluator_code: 'fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![EvalFinding { pattern: \"repo_scope\".to_string(), line: 1, snippet: String::new() }] }'\n"
        )

        scan = {"driver_regex": r"def ", "pattern_name": "p"}
        code, err = resolve_batch_evaluator(scan, "myrepo-global", cidx_meta)
        assert err is None
        assert "repo_scope" in code

    def test_malformed_yaml_returns_structured_error(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import resolve_batch_evaluator

        cidx_meta = tmp_path / "cidx-meta"
        any_dir = cidx_meta / "xray-patterns" / "__any__"
        any_dir.mkdir(parents=True)
        (any_dir / "bad.yaml").write_text(": bad : yaml : {{}")
        scan = {"driver_regex": r"def ", "pattern_name": "bad"}
        code, err = resolve_batch_evaluator(scan, "myrepo-global", cidx_meta)
        # Malformed YAML triggers an error (not_found or load_error)
        assert code == ""
        assert err is not None
        assert "error" in err


# ---------------------------------------------------------------------------
# Tests: _truncate_xray_batch_result
# ---------------------------------------------------------------------------


class TestTruncateXrayBatchResult:
    def test_small_result_returns_inline(self):
        from code_indexer.server.mcp.handlers.xray_batch import (
            _truncate_xray_batch_result,
        )

        mock_cache = MagicMock()
        mock_cache.truncate_result.return_value = {"has_more": False, "preview": ""}

        result = {
            "matches": [{"file_path": "a.py", "line_number": 1}],
            "errors": [],
            "evaluation_errors": [],
            "total_repos": 1,
            "total_scans": 1,
            "total_cells": 1,
            "repos_completed": 1,
            "partial": False,
            "timeout": False,
            "cancelled": False,
        }
        out = _truncate_xray_batch_result(result, mock_cache)
        assert out["truncated"] is False
        assert out["has_more"] is False
        assert out["cache_handle"] is None
        assert len(out["matches"]) == 1

    def test_large_result_stores_cache(self):
        from code_indexer.server.mcp.handlers.xray_batch import (
            _truncate_xray_batch_result,
        )

        mock_cache = MagicMock()
        mock_cache.truncate_result.return_value = {
            "has_more": True,
            "preview": "...",
            "cache_handle": "handle-abc",
            "total_size": 99999,
        }

        matches = [{"file_path": f"f{i}.py", "line_number": i} for i in range(10)]
        result = {
            "matches": matches,
            "errors": [{"error": "x"}] * 5,
            "evaluation_errors": [{"error_type": "Crash"}] * 5,
            "total_repos": 2,
            "total_scans": 1,
            "total_cells": 2,
            "repos_completed": 2,
            "partial": True,
            "timeout": False,
            "cancelled": False,
        }
        out = _truncate_xray_batch_result(result, mock_cache)
        assert out["truncated"] is True
        assert out["has_more"] is True
        assert out["cache_handle"] == "handle-abc"
        assert len(out["matches"]) == 3
        assert len(out["errors"]) == 3
        assert len(out["evaluation_errors"]) == 3
        assert "fetch_tool_hint" in out
        assert out["total_repos"] == 2
        assert out["partial"] is True

    def test_no_cache_returns_original(self):
        from code_indexer.server.mcp.handlers.xray_batch import (
            _truncate_xray_batch_result,
        )

        result = {"matches": [1, 2, 3], "errors": [], "evaluation_errors": []}
        out = _truncate_xray_batch_result(result, None)
        assert out is result


# ---------------------------------------------------------------------------
# Tests: handle_xray_search_batch — auth
# ---------------------------------------------------------------------------


class TestXraySearchBatchAuth:
    def test_unauthenticated_returns_auth_required(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        resp = _parse_response(handle_xray_search_batch(_valid_params(), None))
        assert resp["error"] == "auth_required"

    def test_user_without_query_repos_returns_auth_required(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch
        from unittest.mock import MagicMock

        # Build a user whose has_permission("query_repos") returns False.
        user = MagicMock(spec=User)
        user.has_permission.return_value = False
        resp = _parse_response(handle_xray_search_batch(_valid_params(), user))
        assert resp["error"] == "auth_required"


# ---------------------------------------------------------------------------
# Tests: handle_xray_search_batch — validation
# ---------------------------------------------------------------------------


class TestXraySearchBatchValidation:
    def test_missing_repository_alias_returns_alias_required(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        params = _valid_params()
        del params["repository_alias"]
        resp = _parse_response(handle_xray_search_batch(params, user))
        assert resp["error"] == "alias_required"

    def test_empty_string_alias_returns_alias_required(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(repository_alias=""), user)
        )
        assert resp["error"] == "alias_required"

    def test_empty_list_alias_returns_alias_required(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(repository_alias=[]), user)
        )
        assert resp["error"] == "alias_required"

    def test_missing_scans_returns_scans_required(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        params = _valid_params()
        del params["scans"]
        resp = _parse_response(handle_xray_search_batch(params, user))
        assert resp["error"] == "scans_required"

    def test_empty_scans_returns_scans_required(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        resp = _parse_response(handle_xray_search_batch(_valid_params(scans=[]), user))
        assert resp["error"] == "scans_required"

    def test_scans_not_list_returns_scans_required(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(scans="notalist"), user)
        )
        assert resp["error"] == "scans_required"

    def test_too_many_repositories(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        aliases = [f"repo-{i}" for i in range(51)]
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(repository_alias=aliases), user)
        )
        assert resp["error"] == "too_many_repositories"

    def test_too_many_scans(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        scans = [{"driver_regex": "x", "search_target": "content"}] * 51
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(scans=scans), user)
        )
        assert resp["error"] == "too_many_scans"

    def test_timeout_too_low(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(timeout_seconds=5), user)
        )
        assert resp["error"] == "timeout_out_of_range"

    def test_timeout_too_high(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(timeout_seconds=9999), user)
        )
        assert resp["error"] == "timeout_out_of_range"

    def test_await_seconds_out_of_range(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(await_seconds=99), user)
        )
        assert resp["error"] == "await_seconds_out_of_range"

    def test_missing_driver_regex_returns_error_with_scan_index(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        scans = [
            {"driver_regex": "ok", "search_target": "content"},
            {"search_target": "content"},  # missing driver_regex at index 1
        ]
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(scans=scans), user)
        )
        assert resp["error"] == "driver_regex_required"
        assert resp["scan_index"] == 1

    def test_empty_driver_regex_returns_error_with_scan_index(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        scans = [{"driver_regex": "", "search_target": "content"}]
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(scans=scans), user)
        )
        assert resp["error"] == "driver_regex_required"
        assert resp["scan_index"] == 0

    def test_mutually_exclusive_params_with_scan_index(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        scans = [
            {
                "driver_regex": "x",
                "evaluator_code": VALID_EVAL,
                "pattern_name": "catch-rethrow",
                "search_target": "content",
            }
        ]
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(scans=scans), user)
        )
        assert resp["error"] == "mutually_exclusive_params"
        assert resp["scan_index"] == 0

    def test_invalid_evaluator_code_returns_validation_error_with_scan_index(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        scans = [
            {
                "driver_regex": "x",
                "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { unsafe { vec![] } }",
                "search_target": "content",
            }
        ]
        resp = _parse_response(
            handle_xray_search_batch(_valid_params(scans=scans), user)
        )
        assert resp["error"] == "xray_evaluator_validation_failed"
        assert resp["scan_index"] == 0

    def test_json_string_array_alias_is_parsed(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "jid-json"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/repos/myrepo",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            params = _valid_params(repository_alias='["myrepo-global"]')
            resp = _parse_response(handle_xray_search_batch(params, user))
        assert "job_id" in resp

    def test_duplicate_aliases_deduped(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "jid-dedup"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/repos/myrepo",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            params = _valid_params(
                repository_alias=["myrepo-global", "myrepo-global", "myrepo-global"]
            )
            resp = _parse_response(handle_xray_search_batch(params, user))
        assert "job_id" in resp
        assert mock_bjm.submit_job.call_count == 1


# ---------------------------------------------------------------------------
# Tests: handle_xray_search_batch — repo resolution
# ---------------------------------------------------------------------------


class TestXraySearchBatchRepoResolution:
    def test_no_repositories_resolved_returns_error_with_errors_list(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
        ):
            resp = _parse_response(handle_xray_search_batch(_valid_params(), user))
        assert resp["error"] == "no_repositories_resolved"
        assert "errors" in resp

    def test_partial_resolution_submits_job_for_resolved_repos(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "jid-partial"

        def _resolve(alias):
            return "/repos/good" if alias == "good-repo" else None

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                side_effect=_resolve,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            params = _valid_params(repository_alias=["good-repo", "bad-repo"])
            resp = _parse_response(handle_xray_search_batch(params, user))
        assert "job_id" in resp

    def test_global_alias_fallback_resolves_bare_alias(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "jid-global"

        mock_arm = MagicMock()
        mock_arm.user_has_activated_repo.return_value = False
        mock_grm = MagicMock()
        mock_grm.is_globally_active.return_value = True

        def _resolve(alias):
            return "/repos/evolution-global" if alias == "evolution-global" else None

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                side_effect=_resolve,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(mock_arm, mock_grm),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            params = _valid_params(repository_alias="evolution")
            resp = _parse_response(handle_xray_search_batch(params, user))
        assert "job_id" in resp


# ---------------------------------------------------------------------------
# Tests: handle_xray_search_batch — job submission contract
# ---------------------------------------------------------------------------


class TestXraySearchBatchJobSubmission:
    def test_returns_single_job_id_not_job_ids(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "single-job-id"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/repos/myrepo",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            resp = _parse_response(handle_xray_search_batch(_valid_params(), user))
        assert resp == {"job_id": "single-job-id"}
        assert "job_ids" not in resp

    def test_operation_type_is_xray_search_batch(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "jid-op"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/repos/myrepo",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            handle_xray_search_batch(_valid_params(), user)

        call_kwargs = mock_bjm.submit_job.call_args.kwargs
        assert call_kwargs["operation_type"] == "xray_search_batch"

    def test_repo_alias_none_passed_to_submit_job(self):
        from code_indexer.server.mcp.handlers.xray_batch import handle_xray_search_batch

        user = _make_user()
        mock_bjm = MagicMock()
        mock_bjm.submit_job.return_value = "jid-none-alias"

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._resolve_repo_path",
                return_value="/repos/myrepo",
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_arm_and_grm",
                return_value=(None, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_background_job_manager",
                return_value=mock_bjm,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=Path("/cidx-meta"),
            ),
        ):
            handle_xray_search_batch(_valid_params(), user)

        call_kwargs = mock_bjm.submit_job.call_args.kwargs
        assert call_kwargs.get("repo_alias") is None


# ---------------------------------------------------------------------------
# Tests: _run_xray_batch_job worker
# ---------------------------------------------------------------------------


class TestRunXrayBatchJob:
    """Tests for the batch worker."""

    def test_matches_tagged_with_repo_alias_scan_index_line_number(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        match = {"file_path": "a.py", "line_number": 5, "pattern": "fn", "snippet": "x"}
        cell_result = _make_cell_result(matches=[match])

        mock_engine = MagicMock()
        mock_engine.run.return_value = cell_result
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-1": MagicMock(cancelled=False)}

        resolved_repos = [{"alias": "alpha-global", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-1",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert len(result["matches"]) == 1
        m = result["matches"][0]
        assert m["repository_alias"] == "alpha-global"
        assert m["scan_index"] == 0
        assert m["pattern_name"] is None
        assert m["line_number"] == 5

    def test_pattern_name_tagged_in_match(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        match = {"file_path": "a.py", "line_number": 3, "pattern": "fn", "snippet": "x"}
        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result(matches=[match])
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-pn": MagicMock(cancelled=False)}

        resolved_repos = [{"alias": "alpha-global", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": "catch-rethrow",
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-pn",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert result["matches"][0]["pattern_name"] == "catch-rethrow"

    def test_progress_called_once_per_repo(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-2": MagicMock(cancelled=False)}

        resolved_repos = [
            {"alias": "repo-a", "path": tmp_path},
            {"alias": "repo-b", "path": tmp_path},
        ]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            },
            {
                "driver_regex": "y",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            },
        ]
        progress_calls = []

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-2",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: progress_calls.append(p),
            )

        # 2 repos → 2 progress calls (not 4 for 4 cells)
        assert len(progress_calls) == 2
        assert progress_calls[0] == 50
        assert progress_calls[1] == 100

    def test_phase1_failed_captured_as_cell_error(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result(phase1_failed=True)
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-3": MagicMock(cancelled=False)}

        resolved_repos = [{"alias": "alpha-global", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-3",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        cell_errors = [e for e in result["errors"] if e.get("error_level") == "cell"]
        assert len(cell_errors) >= 1
        assert any(e["error"] == "phase1_failed" for e in cell_errors)
        assert result["partial"] is True

    def test_evaluation_errors_tagged_with_repo_and_scan_index(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        eval_error = {
            "file_path": "a.py",
            "line_number": 0,
            "error_type": "EvaluatorCrash",
            "error_message": "boom",
        }
        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result(eval_errors=[eval_error])
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-4": MagicMock(cancelled=False)}

        resolved_repos = [{"alias": "alpha-global", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-4",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert len(result["evaluation_errors"]) == 1
        ee = result["evaluation_errors"][0]
        assert ee["repository_alias"] == "alpha-global"
        assert ee["scan_index"] == 0
        assert ee["error_type"] == "EvaluatorCrash"
        assert result["partial"] is True

    def test_cancellation_before_first_cell_stops_loop(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-5": MagicMock(cancelled=True)}

        resolved_repos = [
            {"alias": "repo-a", "path": tmp_path},
            {"alias": "repo-b", "path": tmp_path},
        ]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-5",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert result["cancelled"] is True
        assert result["partial"] is True
        mock_engine.run.assert_not_called()

    def test_cell_execution_exception_captured_as_cell_error(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_engine.run.side_effect = RuntimeError("engine exploded")
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-6": MagicMock(cancelled=False)}

        resolved_repos = [{"alias": "alpha-global", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-6",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        cell_errors = [
            e for e in result["errors"] if e.get("error") == "cell_execution_error"
        ]
        assert len(cell_errors) == 1
        assert cell_errors[0]["error_level"] == "cell"
        assert result["partial"] is True

    def test_pattern_resolution_error_becomes_cell_error_and_skips_engine(
        self, tmp_path
    ):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-7": MagicMock(cancelled=False)}

        resolved_repos = [{"alias": "alpha-global", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "pattern_name": "missing",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(
                    "",
                    {"error": "pattern_not_found", "message": "not found"},
                ),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-7",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        cell_errors = [e for e in result["errors"] if e.get("error_level") == "cell"]
        assert len(cell_errors) == 1
        assert cell_errors[0]["error"] == "pattern_not_found"
        assert result["partial"] is True
        mock_engine.run.assert_not_called()

    def test_result_counters_correct_for_full_matrix(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-8": MagicMock(cancelled=False)}

        resolved_repos = [
            {"alias": "repo-a", "path": tmp_path},
            {"alias": "repo-b", "path": tmp_path},
        ]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            },
            {
                "driver_regex": "y",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            },
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-8",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert result["total_repos"] == 2
        assert result["total_scans"] == 2
        assert result["total_cells"] == 4
        assert result["repos_completed"] == 2
        assert result["partial"] is False
        assert result["timeout"] is False
        assert result["cancelled"] is False

    def test_pre_flight_repo_errors_make_result_partial(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-9": MagicMock(cancelled=False)}

        repo_errors = [
            {
                "error_level": "repo",
                "repository_alias": "bad-repo",
                "error": "repository_not_found",
                "message": "not found",
            }
        ]
        resolved_repos = [{"alias": "good-repo", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=repo_errors,
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-9",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        repo_level = [e for e in result["errors"] if e.get("error_level") == "repo"]
        assert len(repo_level) == 1
        assert result["partial"] is True

    def test_timeout_stops_matrix(self, tmp_path):
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-10": MagicMock(cancelled=False)}

        resolved_repos = [
            {"alias": "repo-a", "path": tmp_path},
            {"alias": "repo-b", "path": tmp_path},
        ]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=0,  # immediate timeout
                job_id="jid-10",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert result["timeout"] is True
        assert result["partial"] is True

    def test_timeout_mid_matrix_stops_after_first_repo(self, tmp_path):
        """Timeout trips AFTER the first repo completes, leaving second repo unprocessed.

        Uses a side_effect that lets the first call succeed, then patches
        time.monotonic to return a value past the deadline for the second repo's
        timeout check.
        """
        import time as _time

        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        first_match = {
            "file_path": "a.py",
            "line_number": 1,
            "pattern": "x",
            "snippet": "",
        }
        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result(matches=[first_match])

        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-midtimeout": MagicMock(cancelled=False)}

        resolved_repos = [
            {"alias": "repo-a", "path": tmp_path},
            {"alias": "repo-b", "path": tmp_path},
        ]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        # Set deadline so first repo's timeout check passes but second fails.
        # We do this by patching time.monotonic: call 0 = start (deadline set),
        # call 1 = repo-a between-cell timeout check (still under deadline),
        # call 2 = repo-b between-cell timeout check (past deadline).
        real_monotonic = _time.monotonic
        base = real_monotonic()
        call_count = [0]

        def _fake_monotonic():
            call_count[0] += 1
            if call_count[0] <= 1:
                # First call: sets deadline = base + 100
                return base
            elif call_count[0] == 2:
                # repo-a cell timeout check: deadline not yet exceeded
                return base + 50
            else:
                # repo-b cell timeout check: past deadline
                return base + 200

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.time.monotonic",
                side_effect=_fake_monotonic,
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=100,
                job_id="jid-midtimeout",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert result["timeout"] is True
        assert result["partial"] is True
        # repo-a completed (1 match collected), repo-b did not run
        assert len(result["matches"]) == 1
        assert result["matches"][0]["repository_alias"] == "repo-a"

    def test_cancellation_real_bjm_job_flag_flow(self, tmp_path):
        """Cancellation via a real-ish job object whose .cancelled flag is set between cells.

        Uses two repos; the job flag is set to cancelled after the first repo's
        between-cell check, so the second repo is never processed.
        """
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        first_match = {
            "file_path": "b.py",
            "line_number": 2,
            "pattern": "y",
            "snippet": "",
        }
        mock_engine = MagicMock()
        # First call succeeds; if second were called it would also succeed
        mock_engine.run.return_value = _make_cell_result(matches=[first_match])

        # Real-ish job object: starts uncancelled, will be flipped by side_effect
        class _FakeJob:
            def __init__(self):
                self.cancelled = False

        fake_job = _FakeJob()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-realcancel": fake_job}

        resolved_repos = [
            {"alias": "repo-a", "path": tmp_path},
            {"alias": "repo-b", "path": tmp_path},
        ]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        # Flip cancelled flag after the first engine.run call completes
        def _engine_run_side_effect(*args, **kwargs):
            result = _make_cell_result(matches=[first_match])
            # After first call, set cancelled so next between-repo check fires
            fake_job.cancelled = True
            return result

        mock_engine.run.side_effect = _engine_run_side_effect

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            result = _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-realcancel",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert result["cancelled"] is True
        assert result["partial"] is True
        # repo-a executed (1 match), repo-b skipped
        assert len(result["matches"]) == 1
        assert result["matches"][0]["repository_alias"] == "repo-a"
        # Engine called exactly once (repo-a only)
        assert mock_engine.run.call_count == 1

    def test_max_files_passed_to_engine(self, tmp_path):
        """max_results is forwarded as max_files= to XRaySearchEngine.run."""
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-maxfiles": MagicMock(cancelled=False)}

        resolved_repos = [{"alias": "repo-a", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=42,
                timeout_seconds=600,
                job_id="jid-maxfiles",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        assert mock_engine.run.call_args.kwargs["max_files"] == 42

    def test_on_process_spawned_is_wired_to_engine_run(self, tmp_path):
        """on_process_spawned is passed as a kwarg to XRaySearchEngine.run."""
        from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

        mock_engine = MagicMock()
        mock_engine.run.return_value = _make_cell_result()
        mock_bjm = MagicMock()
        mock_bjm.jobs = {"jid-spawned": MagicMock(cancelled=False)}
        mock_bjm.register_child_process = MagicMock()
        mock_bjm.unregister_child_processes = MagicMock()

        resolved_repos = [{"alias": "repo-a", "path": tmp_path}]
        scans = [
            {
                "driver_regex": "x",
                "search_target": "content",
                "case_sensitive": True,
                "multiline": False,
                "pcre2": False,
                "pattern_name": None,
            }
        ]

        with (
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine",
                return_value=mock_engine,
            ),
            patch(
                "code_indexer.server.mcp.handlers.xray_batch.resolve_batch_evaluator",
                return_value=(VALID_EVAL, None),
            ),
        ):
            _run_xray_batch_job(
                resolved_repos=resolved_repos,
                scans=scans,
                repo_errors=[],
                cidx_meta_path=tmp_path,
                max_results=None,
                timeout_seconds=600,
                job_id="jid-spawned",
                bjm=mock_bjm,
                progress_callback=lambda p, ph, d: None,
            )

        # on_process_spawned must be present in call kwargs (not None)
        call_kwargs = mock_engine.run.call_args.kwargs
        assert "on_process_spawned" in call_kwargs
        assert callable(call_kwargs["on_process_spawned"])


# ---------------------------------------------------------------------------
# Tests: default evaluator validation
# ---------------------------------------------------------------------------


class TestDefaultEvaluatorValidation:
    def test_default_evaluator_passes_rust_validation(self):
        """_DEFAULT_EVALUATOR_CODE must pass validate_rust_evaluator to catch future edits."""
        from code_indexer.server.mcp.handlers.xray_batch import _DEFAULT_EVALUATOR_CODE
        from code_indexer.xray.sandbox import validate_rust_evaluator

        result = validate_rust_evaluator(_DEFAULT_EVALUATOR_CODE)
        assert result.ok, (
            f"_DEFAULT_EVALUATOR_CODE failed Rust validation: {result.reason!r} "
            f"(error_code={result.error_code!r}, construct={result.offending_construct!r})"
        )
