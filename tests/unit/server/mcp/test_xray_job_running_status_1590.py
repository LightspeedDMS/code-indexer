"""Bug #1590 AC3: xray_search/xray_explore jobs never transition to
job_tracker status "running" -- they go straight from "pending" to
"completed"/"failed", so a genuinely stuck job is indistinguishable from a
healthy in-progress one on the Jobs dashboard.

These tests verify that the job body itself calls
job_tracker.update_status(job_id, status="running") AFTER acquiring the
xray cell-limiter slot (so "running" genuinely means "actively executing",
not merely "queued") and BEFORE XRaySearchEngine.run() is invoked.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Generator, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


@contextmanager
def _xray_single_repo_env(
    resolved_future: "Optional[asyncio.Future]" = None,
    xray_cell_limiter: "Optional[Any]" = None,
) -> Generator:
    """Patch infra boundaries for the single-repo Bug #1070 path (mirrors
    the fixture already used in test_xray_search_handler.py /
    test_xray_explore_handler.py). Reused for the multi-repo/omni test too
    -- it accepts a custom (e.g. pending) future for exactly that case.

    ``xray_cell_limiter`` defaults to None -> _get_xray_cell_limiter()
    returns None (wired at ``app_module.app.state.xray_cell_limiter`` --
    the SAME attribute chain _get_xray_cell_limiter() actually reads, per
    xray.py:274-279 -- NOT ``app_module.state``, a distinct, unrelated
    attribute a prior version of this fixture set by mistake, silently
    making the limiter parameter inert), so the job body's "acquire slot"
    branch is skipped and update_status must still fire (running means
    "executing", regardless of whether a limiter is configured for this
    deployment). A caller proving code-review finding F2 (real limiter
    slot leak on a raising update_status) passes a REAL ResizableLimiter
    instead.
    """
    mock_bjm = MagicMock()
    mock_jt = MagicMock()
    mock_jt.register_job.return_value = MagicMock()
    mock_exec = MagicMock()
    mock_app = MagicMock()
    mock_app.background_job_manager = mock_bjm
    mock_app.activated_repo_manager = None
    mock_app.golden_repo_manager = None
    mock_app.app.state.xray_cell_limiter = xray_cell_limiter

    if resolved_future is None:
        resolved_future = asyncio.Future()

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


_SEARCH_PARAMS: Dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "pattern": r"prepareStatement",
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
    "search_target": "content",
}

_ENGINE_RESULT: Dict[str, Any] = {
    "matches": [],
    "evaluation_errors": [],
    "files_processed": 0,
    "files_total": 0,
    "elapsed_seconds": 0.0,
}


def _make_call_order_run(call_order: list) -> Any:
    def _fake_run(**kwargs: Any) -> Dict[str, Any]:
        call_order.append("engine_run")
        return dict(_ENGINE_RESULT)

    return _fake_run


class TestSingleRepoSearchRunningStatus:
    async def test_single_repo_search_job_transitions_to_running(self):
        """job_fn must call update_status(job_id, status='running') AFTER
        acquiring the cell-limiter slot and BEFORE engine.run() executes."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_user(UserRole.NORMAL_USER)
        call_order: list = []

        with (
            _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=_make_call_order_run(call_order),
            ),
        ):
            mock_jt.update_status.side_effect = lambda *a, **kw: call_order.append(
                "update_status"
            )
            await handle_xray_search(_SEARCH_PARAMS.copy(), user)
            job_id = mock_jt.register_job.call_args.kwargs["job_id"]

            job_fn = mock_loop.run_in_executor.call_args[0][1]
            job_fn()

        mock_jt.update_status.assert_any_call(job_id, status="running")
        assert call_order == ["update_status", "engine_run"], (
            f"expected update_status('running') to fire before engine.run(), "
            f"got order: {call_order}"
        )


class TestMultiRepoSearchRunningStatus:
    async def test_multi_repo_search_job_transitions_to_running(self):
        """The omni multi-repo _make_search_job_fn job body must also
        transition each per-alias job to 'running' before engine.run()."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_search

        user = _make_user(UserRole.NORMAL_USER)
        call_order: list = []
        pending_future: asyncio.Future = asyncio.get_event_loop().create_future()
        params = {**_SEARCH_PARAMS, "repository_alias": ["repo-a"]}

        with (
            _xray_single_repo_env(resolved_future=pending_future) as (
                mock_bjm,
                mock_jt,
                mock_exec,
                loop_instance,
            ),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=_make_call_order_run(call_order),
            ),
        ):
            mock_jt.update_status.side_effect = lambda *a, **kw: call_order.append(
                "update_status"
            )
            await handle_xray_search(params, user)
            jid = mock_jt.register_job.call_args.kwargs["job_id"]

            job_fn = loop_instance.run_in_executor.call_args[0][1]
            job_fn()

        mock_jt.update_status.assert_any_call(jid, status="running")
        assert call_order == ["update_status", "engine_run"]


class TestSingleRepoExploreRunningStatus:
    async def test_single_repo_explore_job_transitions_to_running(self):
        """handle_xray_explore's single-repo job body must also transition
        to 'running' before engine.run() executes."""
        from code_indexer.server.mcp.handlers.xray import handle_xray_explore

        user = _make_user(UserRole.NORMAL_USER)
        call_order: list = []

        with (
            _xray_single_repo_env() as (mock_bjm, mock_jt, mock_exec, mock_loop),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=_make_call_order_run(call_order),
            ),
        ):
            mock_jt.update_status.side_effect = lambda *a, **kw: call_order.append(
                "update_status"
            )
            await handle_xray_explore(_SEARCH_PARAMS.copy(), user)
            job_id = mock_jt.register_job.call_args.kwargs["job_id"]

            job_fn = mock_loop.run_in_executor.call_args[0][1]
            job_fn()

        mock_jt.update_status.assert_any_call(job_id, status="running")
        assert call_order == ["update_status", "engine_run"]


class TestRunningStatusUpdateDoesNotLeakSlot:
    """Code-review finding F2: job_tracker.update_status(status="running")
    must be covered by the SAME try/finally that releases the xray
    cell-limiter slot. Placed outside that try/finally, a raising
    update_status() (e.g. a SQLite write failure) leaks the slot forever
    -- one of only 4 global xray concurrency slots gone until a server
    restart, exactly the failure class Bug #1590 exists to eliminate."""

    async def test_slot_released_even_if_update_status_raises(self):
        from code_indexer.server.mcp.handlers.xray import handle_xray_search
        from code_indexer.server.services.resizable_limiter import (
            ResizableLimiter,
        )

        user = _make_user(UserRole.NORMAL_USER)
        real_limiter = ResizableLimiter(initial=1, k_min=1, k_max=1)

        with (
            _xray_single_repo_env(xray_cell_limiter=real_limiter) as (
                mock_bjm,
                mock_jt,
                mock_exec,
                mock_loop,
            ),
            patch(
                "code_indexer.xray.search_engine.XRaySearchEngine.run",
                side_effect=_make_call_order_run([]),
            ),
        ):
            mock_jt.update_status.side_effect = RuntimeError(
                "simulated SQLite write failure"
            )
            await handle_xray_search(_SEARCH_PARAMS.copy(), user)

            job_fn = mock_loop.run_in_executor.call_args[0][1]
            with pytest.raises(RuntimeError, match="simulated SQLite write failure"):
                job_fn()

        assert real_limiter.acquire(timeout=0.1) is True, (
            "xray cell-limiter slot was NOT released after "
            "job_tracker.update_status raised -- the slot leaked, "
            "exactly the failure class Bug #1590 exists to eliminate"
        )
