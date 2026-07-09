"""Bug #1074: multi-repo xray_search / xray_explore must use register_job(repo_alias=None).

Bug description:
  The multi-repo fan-out paths in handle_xray_search and handle_xray_explore called
  bjm.submit_job(repo_alias=single_alias) — a non-NULL alias that routes through
  register_job_if_no_conflict and enforces idx_active_job_per_repo. Two concurrent
  multi-repo xray calls sharing any single alias get a DuplicateJobError -> 409.
  This also puts xray jobs in the 5-worker BJM pool, competing with refresh/indexing/depmap.

Fix acceptance criteria:
  1. For each alias in multi-repo loops: register_job(repo_alias=None) — NULL bypasses
     idx_active_job_per_repo.
  2. metadata["repo_alias"] == single_alias — alias stays observable via job metadata.
  3. bjm.submit_job is NOT called for multi-repo paths.
  4. operation_type is correctly set ("xray_search" or "xray_explore") — required since
     we replace bjm.submit_job with direct register_job + run_in_executor and must pass
     all fields that bjm.submit_job previously supplied.
  5. Concurrent multi-repo calls sharing aliases succeed without DuplicateJobError.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Tuple, cast
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole

# Named constant so the fake path is not scattered as a magic string literal.
_FAKE_REPO_PATH = "/fake/multi-repo/path"

# Aliases used across all multi-repo test cases.
_MULTI_ALIASES = ["myrepo-global", "otherrepo-global"]

# Multi-repo params: repository_alias is a list with two distinct aliases.
MULTI_REPO_SEARCH_PARAMS: Dict[str, Any] = {
    "repository_alias": _MULTI_ALIASES,
    "pattern": r"prepareStatement",
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
    "search_target": "content",
}

MULTI_REPO_EXPLORE_PARAMS: Dict[str, Any] = {
    **MULTI_REPO_SEARCH_PARAMS,
    "max_debug_nodes": 50,
}

# Single source of truth for the (handler, params, operation_type) matrix.
# Used in both TestXrayMultiRepoNullRepoAlias and TestXrayMultiRepoConcurrentSameAliasNoConflict.
HANDLER_CASES: List[Tuple[str, Dict[str, Any], str]] = [
    ("handle_xray_search", MULTI_REPO_SEARCH_PARAMS, "xray_search"),
    ("handle_xray_explore", MULTI_REPO_EXPLORE_PARAMS, "xray_explore"),
]


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
    # json.loads returns Any; narrowed to Dict[str, Any] — MCP responses always
    # produce a top-level dict so the cast is safe at this test boundary.
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _make_success_future() -> "asyncio.Future[Any]":
    """Return a resolved asyncio.Future carrying a minimal xray result."""
    f: asyncio.Future[Any] = asyncio.Future()
    f.set_result({"matches": [], "total_matches": 0})
    return f


@contextmanager
def _patched_xray_env_multi(
    mock_bjm: Any = None,
    mock_job_tracker: Any = None,
    mock_xray_executor: Any = None,
    success_future_count: int = 2,
) -> Generator[Tuple[Any, Any, Any, Any], None, None]:
    """Context manager: patch infrastructure boundaries for multi-repo unit tests.

    Mirrors the pattern from test_xray_bug1070_architectural_fix._patched_xray_env.
    Defaults success_future_count=2 (one per alias in the two-alias test params).
    Uses _FAKE_REPO_PATH constant for the patched _resolve_repo_path return value.
    """
    if mock_bjm is None:
        mock_bjm = MagicMock()
    if mock_job_tracker is None:
        mock_job_tracker = MagicMock()
        mock_job_tracker.register_job.return_value = MagicMock()
    if mock_xray_executor is None:
        mock_xray_executor = MagicMock()

    mock_app = MagicMock()
    mock_app.background_job_manager = mock_bjm
    mock_app.activated_repo_manager = None
    mock_app.golden_repo_manager = None

    futures: List["asyncio.Future[Any]"] = [
        _make_success_future() for _ in range(success_future_count)
    ]
    future_iter = iter(futures)

    with (
        patch("code_indexer.server.mcp.handlers._utils.app_module", mock_app),
        patch(
            "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
            return_value=_FAKE_REPO_PATH,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_background_job_manager",
            return_value=mock_bjm,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_job_tracker",
            return_value=mock_job_tracker,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray._get_xray_executor",
            return_value=mock_xray_executor,
        ),
        patch(
            "code_indexer.server.mcp.handlers.xray.validate_rust_evaluator"
        ) as mock_validate,
        patch("asyncio.get_running_loop") as mock_loop,
    ):
        mock_validate.return_value = MagicMock(ok=True)
        mock_loop.return_value.run_in_executor.side_effect = lambda *a, **kw: next(
            future_iter
        )
        yield mock_bjm, mock_job_tracker, mock_xray_executor, mock_loop


def _assert_register_job_null_alias(
    mock_jt: Any, expected_operation_type: str, expected_aliases: List[str]
) -> None:
    """Shared assertion: every register_job call uses repo_alias=None, correct
    operation_type, and metadata['repo_alias'] carries the per-alias real value."""
    assert mock_jt.register_job.call_count == len(expected_aliases), (
        f"Bug #1074: register_job must be called once per alias ({len(expected_aliases)}), "
        f"got {mock_jt.register_job.call_count} calls"
    )
    for i, c in enumerate(mock_jt.register_job.call_args_list):
        kw = c.kwargs
        assert "repo_alias" in kw, (
            f"Bug #1074: register_job call {i} must include repo_alias kwarg; "
            f"kwargs were: {kw}"
        )
        assert kw["repo_alias"] is None, (
            f"Bug #1074: register_job call {i} must have repo_alias=None, "
            f"got {kw['repo_alias']!r}"
        )
        assert kw.get("operation_type") == expected_operation_type, (
            f"Bug #1074: register_job call {i} must have "
            f"operation_type={expected_operation_type!r}, "
            f"got {kw.get('operation_type')!r}"
        )

    aliases_in_metadata = [
        c.kwargs.get("metadata", {}).get("repo_alias")
        for c in mock_jt.register_job.call_args_list
    ]
    assert set(aliases_in_metadata) == set(expected_aliases), (
        f"Bug #1074: metadata['repo_alias'] must carry each real alias; "
        f"expected {set(expected_aliases)}, got {set(aliases_in_metadata)}"
    )


# ---------------------------------------------------------------------------
# Test class 1: Multi-repo paths call register_job(repo_alias=None) per alias
# ---------------------------------------------------------------------------


class TestXrayMultiRepoNullRepoAlias:
    """Multi-repo xray handlers must call register_job(repo_alias=None) for each alias.

    Bug #1074: bjm.submit_job(repo_alias=single_alias) routes through
    idx_active_job_per_repo causing DuplicateJobError for concurrent calls.
    NULL repo_alias bypasses that constraint; alias is preserved in metadata.
    operation_type must be set correctly since we replace bjm.submit_job with
    direct register_job + run_in_executor and must pass all required fields.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler_name,params,expected_op_type", HANDLER_CASES)
    async def test_multi_repo_registers_with_null_alias(
        self,
        handler_name: str,
        params: Dict[str, Any],
        expected_op_type: str,
    ) -> None:
        """Multi-repo handler must call register_job(repo_alias=None) per alias,
        with correct operation_type, and preserve alias in metadata. bjm.submit_job
        must not be called."""
        import code_indexer.server.mcp.handlers.xray as xray_mod

        user = _make_user(UserRole.NORMAL_USER)
        mock_jt = MagicMock()
        mock_jt.register_job.return_value = MagicMock()

        with _patched_xray_env_multi(
            mock_job_tracker=mock_jt, success_future_count=len(_MULTI_ALIASES)
        ) as (mock_bjm, _jt, _xe, _loop):
            handler = getattr(xray_mod, handler_name)
            result = await handler(params.copy(), user)

        parsed = _parse_response(result)
        assert "job_ids" in parsed, (
            f"Expected job_ids in multi-repo response, got: {parsed}"
        )

        _assert_register_job_null_alias(mock_jt, expected_op_type, _MULTI_ALIASES)
        mock_bjm.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# Test class 2: Concurrent multi-repo calls sharing aliases do not conflict
# ---------------------------------------------------------------------------


class TestXrayMultiRepoConcurrentSameAliasNoConflict:
    """Concurrent multi-repo xray calls sharing aliases must not raise DuplicateJobError.

    Bug #1074: bjm.submit_job(repo_alias=single_alias) caused idx_active_job_per_repo
    to serialize concurrent calls — catastrophically wrong for read-only xray.
    After the fix, register_job(repo_alias=None) must allow all concurrent calls through.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler_name,params,_op_type",
        HANDLER_CASES,
    )
    async def test_concurrent_multi_repo_same_aliases_no_error(
        self,
        handler_name: str,
        params: Dict[str, Any],
        _op_type: str,
    ) -> None:
        """3 concurrent multi-repo handler calls with same 2-alias list must all succeed."""
        import code_indexer.server.mcp.handlers.xray as xray_mod

        user = _make_user(UserRole.NORMAL_USER)
        concurrency = 3
        aliases_per_call = len(_MULTI_ALIASES)

        # Each concurrent call needs one future per alias.
        mock_jt = MagicMock()
        mock_jt.register_job.side_effect = lambda **kw: MagicMock(
            job_id=str(uuid.uuid4())
        )

        with _patched_xray_env_multi(
            mock_job_tracker=mock_jt,
            success_future_count=concurrency * aliases_per_call,
        ) as (mock_bjm, _jt, _xe, _loop):
            handler = getattr(xray_mod, handler_name)
            results = await asyncio.gather(
                *[handler(params.copy(), user) for _ in range(concurrency)],
                return_exceptions=True,
            )

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, (
            f"Bug #1074: Concurrent multi-repo {handler_name} raised exceptions: "
            f"{exceptions}"
        )

        # Cast is safe here: isinstance guard above excludes BaseException instances.
        error_responses = [
            _parse_response(cast(Dict[str, Any], r))
            for r in results
            if not isinstance(r, Exception)
            and "error" in _parse_response(cast(Dict[str, Any], r))
        ]
        assert len(error_responses) == 0, (
            f"Bug #1074: {len(error_responses)} concurrent multi-repo {handler_name} "
            f"calls returned errors:\n{error_responses}"
        )

        for r in results:
            # Cast safe: exceptions filtered above.
            parsed = _parse_response(cast(Dict[str, Any], r))
            assert "job_ids" in parsed, f"Expected job_ids in response, got: {parsed}"
            assert len(parsed["job_ids"]) == aliases_per_call, (
                f"Expected {aliases_per_call} job_ids (one per alias), "
                f"got: {parsed['job_ids']}"
            )

        mock_bjm.submit_job.assert_not_called()
