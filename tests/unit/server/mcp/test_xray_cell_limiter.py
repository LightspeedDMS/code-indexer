"""Tests for the global xray cell concurrency limiter.

Covers:
  1. set_xray_cell_limiter / _get_xray_cell_limiter wiring.
  2. _get_xray_cell_limiter returns None when not wired.
  3. _run_xray_batch_job acquires and releases the limiter for each cell.
  4. _run_xray_batch_job times out when limiter is full.
  5. Config-service _update_xray_setting calls set_limit on the live limiter.
  6. single-repo job_fn returns xray_cell_queue_timeout error on acquire timeout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from code_indexer.server.services.resizable_limiter import ResizableLimiter


# ---------------------------------------------------------------------------
# Test 1: set_xray_cell_limiter / _get_xray_cell_limiter round-trip
# ---------------------------------------------------------------------------


def test_set_and_get_xray_cell_limiter():
    """set_xray_cell_limiter stores the limiter on app.state; _get_xray_cell_limiter returns it."""
    from code_indexer.server.mcp.handlers import xray as xray_module
    from code_indexer.server.mcp.handlers.xray import (
        _get_xray_cell_limiter,
        set_xray_cell_limiter,
    )

    limiter = ResizableLimiter(initial=4, k_min=1, k_max=50)
    mock_app = MagicMock()

    with patch.object(xray_module._utils, "app_module", mock_app):
        set_xray_cell_limiter(limiter)
        result = _get_xray_cell_limiter()

    assert result is limiter, (
        "set_xray_cell_limiter should store the limiter on app.state.xray_cell_limiter"
    )


# ---------------------------------------------------------------------------
# Test 2: _get_xray_cell_limiter returns None when not wired
# ---------------------------------------------------------------------------


def test_get_xray_cell_limiter_returns_none_when_not_wired():
    """_get_xray_cell_limiter returns None when app.state has no xray_cell_limiter."""
    from code_indexer.server.mcp.handlers import xray as xray_module
    from code_indexer.server.mcp.handlers.xray import _get_xray_cell_limiter

    mock_app = MagicMock(spec=[])  # spec=[] means no attributes defined
    mock_app.state = MagicMock(spec=[])  # no xray_cell_limiter attribute

    with patch.object(xray_module._utils, "app_module", mock_app):
        result = _get_xray_cell_limiter()

    assert result is None, (
        "_get_xray_cell_limiter must return None when xray_cell_limiter is not set on app.state"
    )


# ---------------------------------------------------------------------------
# Test 3: _run_xray_batch_job acquires and releases the limiter
# ---------------------------------------------------------------------------


def test_batch_cell_acquires_and_releases_limiter():
    """_run_xray_batch_job acquires a limiter slot per cell and releases it after."""
    from code_indexer.server.mcp.handlers import xray_batch as xray_batch_module
    from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

    limiter = ResizableLimiter(initial=2, k_min=1, k_max=10)

    mock_app = MagicMock()
    mock_app.state.xray_cell_limiter = limiter

    mock_cell_result: Dict[str, Any] = {
        "matches": [],
        "evaluation_errors": [],
        "phase1_failed": False,
        "partial": False,
    }

    mock_bjm = MagicMock()
    mock_bjm.jobs = {}
    mock_progress = MagicMock()

    resolved_repos = [{"alias": "myrepo", "path": Path("/fake/repo")}]
    scans = [
        {
            "driver_regex": "TODO",
            "search_target": "content",
            "case_sensitive": True,
            "multiline": False,
            "pcre2": False,
        }
    ]

    with (
        patch.object(xray_batch_module._utils, "app_module", mock_app),
        patch(
            "code_indexer.server.mcp.handlers.xray_batch.XRaySearchEngine"
        ) as MockEngine,
    ):
        MockEngine.return_value.run.return_value = mock_cell_result

        _run_xray_batch_job(
            resolved_repos=resolved_repos,
            scans=scans,
            repo_errors=[],
            cidx_meta_path=Path("/fake/cidx-meta"),
            max_results=None,
            timeout_seconds=60,
            job_id="test-job-001",
            bjm=mock_bjm,
            progress_callback=mock_progress,
        )

    assert limiter.in_flight == 0, (
        f"Expected in_flight=0 after job completion, got {limiter.in_flight}"
    )


# ---------------------------------------------------------------------------
# Test 4: _run_xray_batch_job times out when limiter is full
# ---------------------------------------------------------------------------


def test_batch_cell_times_out_on_full_limiter():
    """_run_xray_batch_job sets timed_out=True when limiter is fully occupied."""
    from code_indexer.server.mcp.handlers import xray_batch as xray_batch_module
    from code_indexer.server.mcp.handlers.xray_batch import _run_xray_batch_job

    # 1-slot limiter, already acquired externally
    limiter = ResizableLimiter(initial=1, k_min=1, k_max=10)
    assert limiter.acquire(timeout=1.0), "pre-acquire for saturation must succeed"

    mock_app = MagicMock()
    mock_app.app.state.xray_cell_limiter = limiter

    mock_bjm = MagicMock()
    mock_bjm.jobs = {}
    mock_progress = MagicMock()

    resolved_repos = [{"alias": "myrepo", "path": Path("/fake/repo")}]
    scans = [
        {
            "driver_regex": "TODO",
            "search_target": "content",
            "case_sensitive": True,
            "multiline": False,
            "pcre2": False,
        }
    ]

    with patch.object(xray_batch_module._utils, "app_module", mock_app):
        result = _run_xray_batch_job(
            resolved_repos=resolved_repos,
            scans=scans,
            repo_errors=[],
            cidx_meta_path=Path("/fake/cidx-meta"),
            max_results=None,
            timeout_seconds=1,  # 1-second total budget -> deadline reached fast
            job_id="test-job-002",
            bjm=mock_bjm,
            progress_callback=mock_progress,
        )

    # Release the pre-acquired slot
    limiter.release()

    assert result.get("timeout") is True, (
        f"Expected timeout=True when limiter is full, got: {result}"
    )
    assert result.get("partial") is True, (
        f"Expected partial=True when job timed out, got: {result}"
    )


# ---------------------------------------------------------------------------
# Test 5: Config-service _update_xray_setting calls set_limit on the live limiter
# ---------------------------------------------------------------------------


def test_config_update_calls_set_limit():
    """_update_xray_setting for xray_worker_threads calls set_limit on the live limiter."""
    from code_indexer.server.mcp.handlers import xray as xray_module
    from code_indexer.server.mcp.handlers.xray import set_xray_cell_limiter
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.utils.config_manager import ServerConfig

    limiter = ResizableLimiter(initial=4, k_min=1, k_max=50)
    mock_app = MagicMock()
    mock_app.state.xray_cell_limiter = limiter

    config = ServerConfig(server_dir="/tmp")
    assert config.xray_config is not None

    with patch.object(xray_module._utils, "app_module", mock_app):
        set_xray_cell_limiter(limiter)

        # Invoke the real update path in ConfigService
        svc = ConfigService.__new__(ConfigService)
        svc._update_xray_setting(config, "xray_worker_threads", 8)

    assert limiter.limit == 8, (
        f"_update_xray_setting should call set_limit(8) on the live limiter, got {limiter.limit}"
    )


# ---------------------------------------------------------------------------
# Test 6: single-repo job_fn returns xray_cell_queue_timeout on acquire timeout
# ---------------------------------------------------------------------------


def test_job_fn_returns_error_on_acquire_timeout():
    """When the limiter is full, the single-repo job_fn captured from handle_xray_search
    returns a dict with error='xray_cell_queue_timeout'."""
    import asyncio

    from code_indexer.server.mcp.handlers import xray as xray_module
    from code_indexer.server.mcp.handlers.xray import (
        handle_xray_search,
        set_xray_cell_limiter,
    )
    from code_indexer.server.auth.user_manager import User

    # 1-slot limiter, already occupied
    limiter = ResizableLimiter(initial=1, k_min=1, k_max=50)
    assert limiter.acquire(timeout=1.0), "pre-acquire must succeed"

    mock_app = MagicMock()
    mock_app.state.xray_cell_limiter = limiter
    mock_app.activated_repo_manager = None
    mock_app.golden_repo_manager = None

    mock_user = MagicMock(spec=User)
    mock_user.username = "testuser"
    mock_user.has_permission.return_value = True

    mock_bjm = MagicMock()
    mock_bjm.jobs = {}
    mock_job_tracker = MagicMock()
    mock_executor = MagicMock()

    # Capture job_fn submitted to the executor
    captured_job_fn: list = []

    def _fake_run_in_executor(executor, fn):
        captured_job_fn.append(fn)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        fut.set_result({"job_id": "placeholder"})
        return fut

    mock_loop = MagicMock()
    mock_loop.run_in_executor.side_effect = _fake_run_in_executor

    fake_repo_path = "/fake/repo"

    params = {
        "repository_alias": "myrepo",
        "pattern": "TODO",
        "search_target": "content",
        "evaluator_code": (
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"
        ),
        "timeout_seconds": 10,
        "await_seconds": 0,
    }

    with (
        patch.object(xray_module._utils, "app_module", mock_app),
        patch.object(xray_module, "_get_background_job_manager", return_value=mock_bjm),
        patch.object(xray_module, "_get_job_tracker", return_value=mock_job_tracker),
        patch.object(xray_module, "_get_xray_executor", return_value=mock_executor),
        patch.object(xray_module, "_resolve_repo_path", return_value=fake_repo_path),
        patch("asyncio.get_running_loop", return_value=mock_loop),
    ):
        set_xray_cell_limiter(limiter)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(handle_xray_search(params, mock_user))
        finally:
            loop.close()

        assert len(captured_job_fn) == 1, (
            "Expected exactly one job_fn submitted to executor"
        )

        # Invoke the captured job_fn — limiter is still full, it must time out
        result = captured_job_fn[0]()

    # Release the pre-acquired slot
    limiter.release()

    assert result.get("error") == "xray_cell_queue_timeout", (
        f"Expected error='xray_cell_queue_timeout' from job_fn when limiter full, got: {result}"
    )
