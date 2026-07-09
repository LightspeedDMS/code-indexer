"""Bug #1070: xray_search/xray_explore architectural misclassification fix.

Tests for acceptance criteria:
1. _AWAIT_SECONDS_MAX capped at 45.0 (was 120.0)
2. handle_xray_search and handle_xray_explore are async (iscoroutinefunction)
3. BJM submit_job is NOT called — dedicated xray_executor used instead
4. 3 concurrent asyncio.gather calls per handler (search + explore) on same repo succeed
5. Module-level accessors _get_job_tracker, _get_xray_executor, set_xray_executor exist
6. BackgroundJobsConfig.xray_max_concurrent_jobs == 20

Patching strategy:
Infrastructure-boundary accessors (_resolve_repo_path, _get_background_job_manager,
_get_job_tracker, _get_xray_executor, validate_rust_evaluator) are the true external
dependencies of the handler: they delegate to golden-repo filesystem state, BJM singleton,
JobTracker singleton, executor singleton, and Rust FFI. All handler logic runs real.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, cast
from unittest.mock import MagicMock, patch

import pytest

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
    # json.loads returns Any; cast is safe — MCP responses always produce a dict here.
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


VALID_SEARCH_PARAMS: Dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "pattern": r"prepareStatement",
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
    "search_target": "content",
}

VALID_EXPLORE_PARAMS: Dict[str, Any] = {
    **VALID_SEARCH_PARAMS,
    "max_debug_nodes": 50,
}


def _make_success_future() -> asyncio.Future:
    """Return a resolved asyncio.Future carrying a minimal xray result."""
    f: asyncio.Future = asyncio.Future()
    f.set_result({"matches": [], "total_matches": 0})
    return f


@contextmanager
def _patched_xray_env(
    mock_bjm: Any = None,
    mock_job_tracker: Any = None,
    mock_xray_executor: Any = None,
    success_future_count: int = 1,
) -> Generator:
    """Context manager: patch infrastructure boundaries for unit tests.

    Only patches true external-service access points that the handler delegates to:
    - _utils.app_module (golden-repo / ARM singleton; no ARM/GRM = skip global-fallback)
    - _resolve_repo_path (golden-repo filesystem state)
    - _get_background_job_manager (BJM singleton)
    - _get_job_tracker (JobTracker singleton)
    - _get_xray_executor (ThreadPoolExecutor singleton)
    - validate_rust_evaluator (Rust FFI)
    - asyncio.get_running_loop().run_in_executor (actual CPU-bound xray work)

    All handler branching and error-handling logic runs real.
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

    futures = [_make_success_future() for _ in range(success_future_count)]
    future_iter = iter(futures)

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


# ---------------------------------------------------------------------------
# Test 1: _AWAIT_SECONDS_MAX capped at 45.0
# ---------------------------------------------------------------------------


class TestAwaitSecondsMaxCapped:
    """_AWAIT_SECONDS_MAX must be <= 45.0 to avoid 504s at ALB 60s boundary."""

    def test_await_seconds_max_is_at_most_45(self):
        """_AWAIT_SECONDS_MAX must be <= 45.0 (Bug #1070 AC: cap from 120.0)."""
        from code_indexer.server.mcp.handlers.xray import _AWAIT_SECONDS_MAX

        assert _AWAIT_SECONDS_MAX <= 45.0, (
            f"_AWAIT_SECONDS_MAX must be <= 45.0 to avoid ALB 60s timeout 504s, "
            f"got {_AWAIT_SECONDS_MAX}"
        )

    def test_await_seconds_max_is_exactly_45(self):
        """_AWAIT_SECONDS_MAX must be exactly 45.0 per Bug #1070 spec."""
        from code_indexer.server.mcp.handlers.xray import _AWAIT_SECONDS_MAX

        assert _AWAIT_SECONDS_MAX == 45.0, (
            f"Expected _AWAIT_SECONDS_MAX == 45.0, got {_AWAIT_SECONDS_MAX}"
        )


# ---------------------------------------------------------------------------
# Test 2: Handlers are async coroutine functions
# ---------------------------------------------------------------------------


class TestHandlersAreAsync:
    """handle_xray_search and handle_xray_explore must be async def."""

    def test_handle_xray_search_is_coroutine_function(self):
        """asyncio.iscoroutinefunction(handle_xray_search) must be True.

        Protocol dispatches async handlers directly on the event loop,
        avoiding run_in_executor and the _mcp_executor thread pool.
        """
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        assert asyncio.iscoroutinefunction(handle_xray_search), (
            "handle_xray_search must be an async def coroutine function.\n"
            "Bug #1070: sync handlers hold _mcp_executor threads with time.sleep "
            "polling loops for up to 120s, causing 504s at the ALB 60s timeout."
        )

    def test_handle_xray_explore_is_coroutine_function(self):
        """asyncio.iscoroutinefunction(handle_xray_explore) must be True."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        assert asyncio.iscoroutinefunction(handle_xray_explore), (
            "handle_xray_explore must be an async def coroutine function.\n"
            "Bug #1070: sync handlers hold _mcp_executor threads with time.sleep "
            "polling loops for up to 120s, causing 504s at the ALB 60s timeout."
        )


# ---------------------------------------------------------------------------
# Test 3: BJM submit_job NOT called during xray_search / xray_explore
# ---------------------------------------------------------------------------


class TestXrayDoesNotUseBjmWorkerPool:
    """xray handlers must NOT route through BJM's 5-worker pool."""

    @pytest.mark.asyncio
    async def test_xray_search_does_not_call_bjm_submit_job(self):
        """handle_xray_search must NOT call bjm.submit_job.

        Bug #1070: BJM has only 5 workers shared with refresh/indexing/depmap.
        Routing xray through BJM causes starvation under load.
        """
        user = _make_user(UserRole.NORMAL_USER)

        with _patched_xray_env(success_future_count=1) as (mock_bjm, _jt, _xe, _loop):
            from code_indexer.server.mcp.handlers.xray import handle_xray_search

            await handle_xray_search(VALID_SEARCH_PARAMS.copy(), user)

        mock_bjm.submit_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_xray_explore_does_not_call_bjm_submit_job(self):
        """handle_xray_explore must NOT call bjm.submit_job."""
        user = _make_user(UserRole.NORMAL_USER)

        with _patched_xray_env(success_future_count=1) as (mock_bjm, _jt, _xe, _loop):
            from code_indexer.server.mcp.handlers.xray import handle_xray_explore

            await handle_xray_explore(VALID_EXPLORE_PARAMS.copy(), user)

        mock_bjm.submit_job.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Concurrent same-repo xray jobs are allowed (asyncio.gather)
# ---------------------------------------------------------------------------


class TestXrayConcurrentSameRepoNoConflict:
    """Multiple concurrent xray jobs on the same repo must all succeed without error.

    Bug #1070: idx_active_job_per_repo serializes concurrent xray on same repo.
    xray is read-only — serialization is catastrophically wrong.
    """

    @pytest.mark.asyncio
    async def test_concurrent_xray_search_same_repo_no_error(self):
        """3 concurrent handle_xray_search calls via asyncio.gather for the same repo must all succeed."""
        user = _make_user(UserRole.NORMAL_USER)

        mock_job_tracker = MagicMock()
        mock_job_tracker.register_job.side_effect = lambda *a, **kw: MagicMock(
            job_id=str(uuid.uuid4())
        )

        with _patched_xray_env(
            mock_job_tracker=mock_job_tracker, success_future_count=3
        ) as (_bjm, _jt, _xe, _loop):
            from code_indexer.server.mcp.handlers.xray import handle_xray_search

            results = await asyncio.gather(
                handle_xray_search(VALID_SEARCH_PARAMS.copy(), user),
                handle_xray_search(VALID_SEARCH_PARAMS.copy(), user),
                handle_xray_search(VALID_SEARCH_PARAMS.copy(), user),
                return_exceptions=True,
            )

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, (
            f"Bug #1070: Concurrent xray_search jobs on same repo raised exceptions: {exceptions}\n"
            "xray is read-only — per-repo serialization is wrong."
        )

        error_responses = [
            # cast safe: isinstance guard above ensures r is not BaseException here
            _parse_response(cast(Dict[str, Any], r))
            for r in results
            if not isinstance(r, Exception)
            and "error" in _parse_response(cast(Dict[str, Any], r))
        ]
        assert len(error_responses) == 0, (
            f"Bug #1070: {len(error_responses)} concurrent xray_search calls returned errors:\n"
            f"{error_responses}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_xray_explore_same_repo_no_error(self):
        """3 concurrent handle_xray_explore calls via asyncio.gather for the same repo must all succeed."""
        user = _make_user(UserRole.NORMAL_USER)

        mock_job_tracker = MagicMock()
        mock_job_tracker.register_job.side_effect = lambda *a, **kw: MagicMock(
            job_id=str(uuid.uuid4())
        )

        with _patched_xray_env(
            mock_job_tracker=mock_job_tracker, success_future_count=3
        ) as (_bjm, _jt, _xe, _loop):
            from code_indexer.server.mcp.handlers.xray import handle_xray_explore

            results = await asyncio.gather(
                handle_xray_explore(VALID_EXPLORE_PARAMS.copy(), user),
                handle_xray_explore(VALID_EXPLORE_PARAMS.copy(), user),
                handle_xray_explore(VALID_EXPLORE_PARAMS.copy(), user),
                return_exceptions=True,
            )

        exceptions = [r for r in results if isinstance(r, Exception)]
        assert len(exceptions) == 0, (
            f"Bug #1070: Concurrent xray_explore jobs on same repo raised exceptions: {exceptions}\n"
            "xray is read-only — per-repo serialization is wrong."
        )

        error_responses = [
            # cast safe: isinstance guard above ensures r is not BaseException here
            _parse_response(cast(Dict[str, Any], r))
            for r in results
            if not isinstance(r, Exception)
            and "error" in _parse_response(cast(Dict[str, Any], r))
        ]
        assert len(error_responses) == 0, (
            f"Bug #1070: {len(error_responses)} concurrent xray_explore calls returned errors:\n"
            f"{error_responses}"
        )


# ---------------------------------------------------------------------------
# Test 5: Module-level accessors exist in xray.py
# ---------------------------------------------------------------------------


class TestXrayModuleLevelAccessors:
    """Module-level accessor and setter functions must exist in xray.py for wiring."""

    def test_get_job_tracker_function_exists(self):
        """xray.py must expose _get_job_tracker() for JobTracker access."""
        import code_indexer.server.mcp.handlers.xray as xray_mod

        assert hasattr(xray_mod, "_get_job_tracker"), (
            "xray.py must have _get_job_tracker() for JobTracker access"
        )
        assert callable(xray_mod._get_job_tracker)

    def test_get_xray_executor_function_exists(self):
        """xray.py must expose _get_xray_executor() for executor access."""
        import code_indexer.server.mcp.handlers.xray as xray_mod

        assert hasattr(xray_mod, "_get_xray_executor"), (
            "xray.py must have _get_xray_executor() for ThreadPoolExecutor access"
        )
        assert callable(xray_mod._get_xray_executor)

    def test_set_xray_executor_function_exists(self):
        """xray.py must expose set_xray_executor() called from lifespan."""
        import code_indexer.server.mcp.handlers.xray as xray_mod

        assert hasattr(xray_mod, "set_xray_executor"), (
            "xray.py must have set_xray_executor() to allow lifespan injection"
        )
        assert callable(xray_mod.set_xray_executor)


# ---------------------------------------------------------------------------
# Test 6: xray_max_concurrent_jobs in BackgroundJobsConfig
# ---------------------------------------------------------------------------


class TestBackgroundJobsConfigXraySlot:
    """BackgroundJobsConfig must have xray_max_concurrent_jobs field."""

    def test_background_jobs_config_has_xray_max_concurrent_jobs(self):
        """BackgroundJobsConfig must include xray_max_concurrent_jobs: int = 20."""
        from code_indexer.server.utils.config_manager import BackgroundJobsConfig

        config = BackgroundJobsConfig()

        assert hasattr(config, "xray_max_concurrent_jobs"), (
            "BackgroundJobsConfig must have xray_max_concurrent_jobs field.\n"
            "Bug #1070: dedicated xray executor size is configurable via this field."
        )
        assert isinstance(config.xray_max_concurrent_jobs, int)
        assert config.xray_max_concurrent_jobs == 20, (
            f"xray_max_concurrent_jobs default must be 20, got {config.xray_max_concurrent_jobs}"
        )


# ---------------------------------------------------------------------------
# Test 7: register_job must pass repo_alias=None (Bug #1073)
# ---------------------------------------------------------------------------


class TestXrayRegisterJobNullRepoAlias:
    """xray register_job must pass repo_alias=None to bypass idx_active_job_per_repo.

    Bug #1073: passing repo_alias=repo_alias_parsed caused UniqueViolation on
    PostgreSQL when concurrent xray calls were made on the same repo, because
    idx_active_job_per_repo serializes rows where repo_alias IS NOT NULL.
    xray is read-only — the escape hatch (repo_alias=None) must always be used.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "handler_name, params",
        [
            ("handle_xray_search", VALID_SEARCH_PARAMS),
            ("handle_xray_explore", VALID_EXPLORE_PARAMS),
        ],
    )
    async def test_handler_registers_with_null_repo_alias(
        self, handler_name: str, params: Dict[str, Any]
    ) -> None:
        """Both xray handlers must call register_job with repo_alias explicitly None."""
        import code_indexer.server.mcp.handlers.xray as xray_mod

        user = _make_user(UserRole.NORMAL_USER)
        mock_jt = MagicMock()
        mock_jt.register_job.return_value = MagicMock()

        with _patched_xray_env(mock_job_tracker=mock_jt, success_future_count=1) as (
            _bjm,
            _jt,
            _xe,
            _loop,
        ):
            handler = getattr(xray_mod, handler_name)
            await handler(params.copy(), user)

        assert mock_jt.register_job.called, "register_job must be called"
        call_kwargs = mock_jt.register_job.call_args.kwargs
        assert "repo_alias" in call_kwargs, (
            f"Bug #1073: {handler_name} must pass repo_alias as an explicit keyword "
            f"argument to register_job; kwargs were: {call_kwargs}"
        )
        assert call_kwargs["repo_alias"] is None, (
            f"Bug #1073: {handler_name} must pass repo_alias=None to register_job "
            f"to bypass idx_active_job_per_repo; "
            f"got repo_alias={call_kwargs['repo_alias']!r}"
        )
        metadata = call_kwargs.get("metadata") or {}
        assert metadata.get("repo_alias") == params["repository_alias"], (
            f"Bug #1073: {handler_name} must preserve repo alias in metadata so it "
            f"remains observable; expected metadata['repo_alias']={params['repository_alias']!r}, "
            f"got metadata={metadata!r}"
        )
