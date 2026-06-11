"""xray_explore handler tests — Omni alias parsing and default evaluator (Bug #1070 async).

Recovered from test_xray_explore_handler.py after agent split; converted to async pattern.

TestXrayExploreHandlerOmni: repository_alias accepts string OR list of strings.
TestXrayExploreHandlerDefaultEvaluator: omitted/empty evaluator_code uses dict-contract default.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import UserRole

from .test_xray_explore_handler import (
    _import_handler,
    _make_user,
    _parse_response,
    _xray_single_repo_env,
)


# ---------------------------------------------------------------------------
# Tests: Omni alias parsing
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerOmni:
    """repository_alias accepts string OR array of strings in xray_explore.

    Bug 1 (v10.4.1): handle_xray_explore was missing the _parse_json_string_array
    normalization step, causing AttributeError: 'list' object has no attribute
    'endswith' when a native list or JSON-encoded array was passed.
    """

    async def _run_with_aliases(
        self,
        alias_value: Any,
        resolved_paths: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run handle_xray_explore with given repository_alias and path map."""
        user = _make_user(UserRole.NORMAL_USER)

        # Compute how many job IDs to provision for the multi-repo path.
        if isinstance(alias_value, list):
            aliases = alias_value
        elif isinstance(alias_value, str) and alias_value.startswith("["):
            try:
                parsed = json.loads(alias_value)
                aliases = parsed if isinstance(parsed, list) else [alias_value]
            except json.JSONDecodeError:
                aliases = [alias_value]
        else:
            aliases = [alias_value]

        job_ids = [f"explore-job-{i}" for i in range(len(aliases))]
        mock_bjm = MagicMock()
        mock_bjm.submit_job.side_effect = job_ids

        mock_jt = MagicMock()
        mock_jt.register_job.return_value = MagicMock()
        mock_exec = MagicMock()

        # Pre-resolved future for single-repo path (await_seconds defaults to 0 so
        # the result is not polled, but run_in_executor still needs a valid Future).
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        future.set_result({"matches": [], "total_matches": 0})

        loop_instance = MagicMock()
        loop_instance.run_in_executor.return_value = future

        def fake_resolve(alias: str) -> Any:
            return resolved_paths.get(alias)

        params: Dict[str, Any] = {
            "repository_alias": alias_value,
            "pattern": r"TODO",
            "search_target": "content",
            # evaluator_code omitted — default path
        }

        with (
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
                "code_indexer.server.mcp.handlers.xray.validate_rust_evaluator",
                return_value=MagicMock(ok=True),
            ),
            patch("asyncio.get_running_loop", return_value=loop_instance),
        ):
            return _parse_response(await _import_handler()(params, user))

    async def test_string_alias_single_repo_works_as_before(self) -> None:
        """String alias returns {job_id} dict — unchanged single-repo path (regression)."""
        result = await self._run_with_aliases(
            "myrepo-global", {"myrepo-global": "/path/repo"}
        )
        assert "job_id" in result, (
            f"Expected job_id for single string alias, got: {result}"
        )

    async def test_native_list_alias_does_not_crash(self) -> None:
        """Native list ['repo-a', 'repo-b'] must NOT crash with AttributeError (Bug 1)."""
        paths = {"repo-a": "/path/a", "repo-b": "/path/b"}
        result = await self._run_with_aliases(["repo-a", "repo-b"], paths)
        assert "job_ids" in result, f"Expected job_ids for list alias, got: {result}"
        assert len(result["job_ids"]) == 2

    async def test_json_string_array_alias_is_parsed(self) -> None:
        """JSON-encoded string '['a','b']' is parsed to list of aliases (Bug 1)."""
        paths = {"repo-a": "/path/a", "repo-b": "/path/b"}
        result = await self._run_with_aliases('["repo-a", "repo-b"]', paths)
        assert "job_ids" in result, f"Expected job_ids after JSON parse, got: {result}"
        assert len(result["job_ids"]) == 2

    async def test_empty_array_alias_returns_alias_required_error(self) -> None:
        """Empty list [] returns alias_required error, not crash (Bug 1)."""
        result = await self._run_with_aliases([], {})
        assert result.get("error") == "alias_required", (
            f"Expected alias_required for empty list, got: {result}"
        )

    async def test_list_with_unknown_repo_returns_errors_entry(self) -> None:
        """Unknown alias in list produces repository_not_found error entry."""
        paths = {"known-repo": "/path/known"}
        result = await self._run_with_aliases(["known-repo", "unknown-repo"], paths)
        assert "errors" in result or "job_ids" in result, (
            f"Unexpected response: {result}"
        )
        if "errors" in result:
            errors = result["errors"]
            assert any("unknown-repo" in str(e) for e in errors), (
                f"Expected error mentioning 'unknown-repo', got: {errors}"
            )

    async def test_list_does_not_reach_endswith(self) -> None:
        """Feeding a list never reaches .endswith() or any string-only method (Bug 1).

        Before the fix, passing a list raised AttributeError: 'list' object has no
        attribute 'endswith'. This test verifies the handler completes without that error.
        """
        paths = {"repo-x": "/path/x"}
        try:
            result = await self._run_with_aliases(["repo-x"], paths)
            assert isinstance(result, dict), (
                f"Expected dict response, got: {type(result)}"
            )
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
    Phase 1 hits as matches using the Rust dict shape.
    """

    async def _get_engine_evaluator_code(self, params: Dict[str, Any]) -> str:
        """Submit a valid explore job and capture evaluator_code forwarded to engine.run()."""
        user = _make_user(UserRole.NORMAL_USER)
        captured: Dict[str, Any] = {}

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
                job_fn()  # NO args — closure captures everything

        return cast(str, captured.get("evaluator_code", ""))

    async def test_omitted_evaluator_code_uses_non_empty_default(self) -> None:
        """When evaluator_code is omitted, engine receives a non-empty default (Bug 2)."""
        params: Dict[str, Any] = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
        }
        evaluator = await self._get_engine_evaluator_code(params)
        assert evaluator, (
            "Engine must receive a non-empty evaluator_code when evaluator_code is omitted"
        )

    async def test_omitted_evaluator_code_default_returns_dict_not_bool(self) -> None:
        """Default evaluator must contain 'fn evaluate_node', not 'return True' (Bug 2)."""
        params: Dict[str, Any] = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
        }
        evaluator = await self._get_engine_evaluator_code(params)
        assert "fn evaluate_node" in evaluator, (
            f"Default evaluator must contain 'fn evaluate_node' (Rust contract), got: {evaluator!r}"
        )

    async def test_omitted_evaluator_code_default_passes_sandbox_validation(
        self,
    ) -> None:
        """Default evaluator must pass validate_rust_evaluator() (Bug 2)."""
        from code_indexer.xray.sandbox import validate_rust_evaluator

        params: Dict[str, Any] = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
        }
        evaluator = await self._get_engine_evaluator_code(params)
        result = validate_rust_evaluator(evaluator)
        assert result.ok, (
            f"Default evaluator must pass validate_rust_evaluator(), got failure: {result.reason!r}"
        )

    async def test_empty_evaluator_code_string_uses_non_empty_default(self) -> None:
        """Explicit empty string evaluator_code is treated same as omitted (Bug 2)."""
        params: Dict[str, Any] = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
            "evaluator_code": "",
        }
        evaluator = await self._get_engine_evaluator_code(params)
        assert evaluator, (
            "Empty string evaluator_code must be replaced by non-empty default (Bug 2)"
        )

    async def test_explicit_evaluator_code_is_not_replaced_by_default(self) -> None:
        """Explicit non-empty evaluator_code is forwarded as-is (regression guard)."""
        custom_code = (
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"
        )
        params: Dict[str, Any] = {
            "repository_alias": "myrepo-global",
            "pattern": r"TODO",
            "search_target": "content",
            "evaluator_code": custom_code,
        }
        evaluator = await self._get_engine_evaluator_code(params)
        assert evaluator == custom_code, (
            f"Explicit evaluator_code must not be replaced by default, got: {evaluator!r}"
        )

    async def test_default_evaluator_is_valid_rust_and_passes_validation(self) -> None:
        """_DEFAULT_EVALUATOR_CODE is valid Rust with fn evaluate_node signature (Bug 2)."""
        from code_indexer.server.mcp.handlers.xray import _DEFAULT_EVALUATOR_CODE
        from code_indexer.xray.sandbox import validate_rust_evaluator

        assert _DEFAULT_EVALUATOR_CODE, (
            "Default evaluator must be non-empty (Bug 2 regression)"
        )
        assert "fn evaluate_node" in _DEFAULT_EVALUATOR_CODE, (
            f"Default evaluator must contain 'fn evaluate_node', got: {_DEFAULT_EVALUATOR_CODE!r}"
        )
        result = validate_rust_evaluator(_DEFAULT_EVALUATOR_CODE)
        assert result.ok, (
            f"Default evaluator must pass validation (failure={result.error_code!r}, "
            f"reason={result.reason!r})"
        )
