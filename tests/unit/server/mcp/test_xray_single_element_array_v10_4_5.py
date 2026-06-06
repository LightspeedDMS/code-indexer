"""v10.4.5 Defect 5 tests: single-element list alias returns single-repo response shape.

Verifies that repository_alias=["cidx-meta-global"] (1-element list — native or
JSON-encoded) returns {"job_id":"..."} (single-repo shape), NOT {"job_ids":[...],
"errors":[]} (multi-repo shape).

Multi-element lists must still take the multi-repo path unchanged:
- "job_ids" list has one entry per alias
- "errors" list is empty when all aliases resolve successfully

Plain-string alias must continue to return single-repo shape (regression guard).

Applies to both handle_xray_search and handle_xray_explore.

Test design: parametrize over handler_name so search/explore share one test matrix
without copy-paste. External dependencies (_resolve_repo_path, _get_background_job_manager,
_get_job_tracker, _get_xray_executor) are mocked; internal omni helpers are NOT mocked
so the real branching logic is exercised.

Bug #1070: handlers are now async def — _invoke_handler must await them.
Single-repo path now uses a dedicated xray_executor (UUID job_id, not from submit_job).
Multi-repo path still uses bjm.submit_job (job_ids come from submit_job.return_value).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_FAKE_JOB_ID = "test-job-123"

_BASE_PARAMS: Dict[str, Any] = {
    "pattern": r"prepareStatement",
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
    "search_target": "content",
}

# Handler names under test — parametrize over these to avoid copy-paste.
_HANDLER_NAMES = ["handle_xray_search", "handle_xray_explore"]


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap the MCP content envelope."""
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _make_success_future() -> asyncio.Future:
    """Return a resolved asyncio.Future carrying a minimal xray result."""
    f: asyncio.Future = asyncio.Future()
    f.set_result({"matches": [], "total_matches": 0})
    return f


async def _invoke_handler(handler_name: str, repo_alias: Any) -> Dict[str, Any]:
    """Call the named handler with mocked external dependencies only.

    Bug #1070: handlers are now async def — must be awaited.

    Mocks:
    - _resolve_repo_path (filesystem lookup)
    - _get_background_job_manager (server singleton)
    - _get_job_tracker (JobTracker singleton — single-repo path)
    - _get_xray_executor (ThreadPoolExecutor singleton — single-repo path)
    - asyncio.get_running_loop (run_in_executor — single-repo path)

    Does NOT mock any internal helpers (_submit_xray_explore_omni) so the
    real branching logic is exercised.
    """
    import code_indexer.server.mcp.handlers.xray as _xray_module

    handler = getattr(_xray_module, handler_name)

    user = _make_user(UserRole.NORMAL_USER)
    mock_bjm = MagicMock()
    mock_bjm.submit_job.return_value = _FAKE_JOB_ID

    mock_job_tracker = MagicMock()
    mock_job_tracker.register_job.return_value = MagicMock()

    mock_xray_executor = MagicMock()

    params = dict(_BASE_PARAMS)
    params["repository_alias"] = repo_alias

    future = _make_success_future()

    with (
        patch.object(
            _xray_module, "_resolve_repo_path", return_value="/some/path/to/repo"
        ),
        patch.object(
            _xray_module, "_get_background_job_manager", return_value=mock_bjm
        ),
        patch.object(
            _xray_module, "_get_job_tracker", return_value=mock_job_tracker
        ),
        patch.object(
            _xray_module, "_get_xray_executor", return_value=mock_xray_executor
        ),
        patch("asyncio.get_running_loop") as mock_loop,
    ):
        mock_loop.return_value.run_in_executor.return_value = future
        result = await handler(params, user)

    return _parse_response(result)


# ---------------------------------------------------------------------------
# Parametrized tests — applied equally to search and explore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", _HANDLER_NAMES)
async def test_single_element_string_array_returns_single_repo_shape(
    handler_name: str,
) -> None:
    """repository_alias=["cidx-meta-global"] -> {job_id: ...}, not {job_ids: [...]}."""
    data = await _invoke_handler(handler_name, ["cidx-meta-global"])
    assert "job_id" in data, (
        f"[{handler_name}] Expected 'job_id' key, got: {list(data.keys())}"
    )
    # Single-repo path: job_id is a UUID from uuid.uuid4() (not submit_job.return_value).
    assert isinstance(data["job_id"], str) and len(data["job_id"]) > 0
    assert "job_ids" not in data


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", _HANDLER_NAMES)
async def test_single_element_json_encoded_array_returns_single_repo_shape(
    handler_name: str,
) -> None:
    """repository_alias='["cidx-meta-global"]' (JSON-encoded string) -> {job_id: ...}."""
    data = await _invoke_handler(handler_name, '["cidx-meta-global"]')
    assert "job_id" in data, (
        f"[{handler_name}] Expected 'job_id' key, got: {list(data.keys())}"
    )
    # Single-repo path: job_id is a UUID from uuid.uuid4() (not submit_job.return_value).
    assert isinstance(data["job_id"], str) and len(data["job_id"]) > 0
    assert "job_ids" not in data


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", _HANDLER_NAMES)
async def test_two_element_array_returns_multi_repo_shape(handler_name: str) -> None:
    """repository_alias=["repo-a","repo-b"] -> {job_ids:[job,job], errors:[]}.

    Real internal omni helper is exercised (not mocked).
    Asserts: one job_id submitted per alias, errors list is empty (all resolved).
    Multi-repo path still uses bjm.submit_job — job_ids come from submit_job.return_value.
    """
    data = await _invoke_handler(handler_name, ["repo-a", "repo-b"])
    assert "job_ids" in data, (
        f"[{handler_name}] Expected 'job_ids' key, got: {list(data.keys())}"
    )
    assert "errors" in data, (
        f"[{handler_name}] Expected 'errors' key, got: {list(data.keys())}"
    )
    assert "job_id" not in data
    # One job submitted per alias — verify the count matches the input list length.
    assert len(data["job_ids"]) == 2, (
        f"[{handler_name}] Expected 2 job_ids (one per alias), got: {data['job_ids']}"
    )
    # Both aliases resolve successfully so errors must be empty.
    assert data["errors"] == [], (
        f"[{handler_name}] Expected no errors (both aliases resolved), got: {data['errors']}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", _HANDLER_NAMES)
async def test_string_alias_returns_single_repo_shape(handler_name: str) -> None:
    """Plain string alias (regression guard) -> {job_id: ...} unchanged."""
    data = await _invoke_handler(handler_name, "cidx-meta-global")
    assert "job_id" in data, (
        f"[{handler_name}] Expected 'job_id' key, got: {list(data.keys())}"
    )
    # Single-repo path: job_id is a UUID from uuid.uuid4() (not submit_job.return_value).
    assert isinstance(data["job_id"], str) and len(data["job_id"]) > 0
    assert "job_ids" not in data
