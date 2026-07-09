"""xray_explore handler tests — await_seconds behavior (Bug #1070 async rewrite).

Covers TestXrayExploreHandlerAwaitSeconds and TestXrayExploreHandlerAwaitSecondsV2:
  positive await inline, pending future falls back to job_id,
  negative/above-cap/bool/string values rejected.

Imports shared helpers from test_xray_explore_handler to avoid duplication.
Cap boundary values use _AWAIT_SECONDS_MAX (45.0) instead of hardcoded 120.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.mcp.handlers.xray import _AWAIT_SECONDS_MAX

from .test_xray_explore_handler import (
    VALID_PARAMS,
    _import_handler,
    _make_resolved_future,
    _make_user,
    _parse_response,
    _xray_single_repo_env,
)

# Named constants — avoids magic numbers in test bodies
_AWAIT_ABOVE_CAP = int(_AWAIT_SECONDS_MAX) + 1  # 46 when cap is 45.0
_AWAIT_INLINE_SECONDS = 5  # positive; future must resolve
_FLOAT_AT_CAP = float(_AWAIT_SECONDS_MAX)  # 45.0 — at boundary (accepted)
_FLOAT_JUST_ABOVE_CAP = _AWAIT_SECONDS_MAX + 0.001  # 45.001 — just over (rejected)
_FLOAT_NEGATIVE = -0.001  # smallest negative (rejected)


def _make_inline_result() -> Dict[str, Any]:
    """Return a minimal inline xray explore result dict."""
    return {
        "matches": [{"file_path": "a.py", "ast_debug": {}}],
        "evaluation_errors": [],
        "files_processed": 1,
        "files_total": 1,
        "elapsed_seconds": 0.1,
        "truncated": False,
        "cache_handle": None,
    }


async def _run_with_await(
    await_seconds: Any,
    resolved_future: "asyncio.Future[Any] | None" = None,
) -> Dict[str, Any]:
    """Run handler with given await_seconds and optional pre-resolved future.

    Returns the parsed response dict.
    await_seconds validation fires before repo lookup so _xray_single_repo_env
    is sufficient — no extra _resolve_repo_path patch needed for rejection paths.
    """
    user = _make_user(UserRole.NORMAL_USER)
    params = {**VALID_PARAMS, "await_seconds": await_seconds}
    with _xray_single_repo_env(resolved_future=resolved_future):
        result = await _import_handler()(params, user)
    return _parse_response(result)


# ---------------------------------------------------------------------------
# Tests: core await_seconds behavior
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerAwaitSeconds:
    """await_seconds=0 returns {job_id}; positive value awaits the future inline."""

    async def test_await_zero_returns_job_id(self):
        """await_seconds=0 (default) returns {job_id} immediately."""
        data = await _run_with_await(0)
        assert "job_id" in data
        assert "matches" not in data

    async def test_await_positive_returns_inline_when_future_resolves(self):
        """await_seconds=5 returns inline matches when future completes in window."""
        resolved = _make_resolved_future(_make_inline_result())
        data = await _run_with_await(_AWAIT_INLINE_SECONDS, resolved)
        assert "matches" in data
        assert "job_id" not in data

    async def test_await_positive_returns_job_id_when_future_stays_pending(self):
        """await_seconds=0.1 returns {job_id} when future does not complete."""
        pending: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        data = await _run_with_await(0.1, pending)
        assert "job_id" in data


# ---------------------------------------------------------------------------
# Tests: float values and the 45s cap (Bug #1070)
# ---------------------------------------------------------------------------


class TestXrayExploreHandlerAwaitSecondsV2:
    """Float await_seconds and the 45s cap lowered by Bug #1070.

    Task #35: await_seconds accepts int OR float.
    Parametrized for rejection and acceptance groups to avoid duplication.
    """

    @pytest.mark.parametrize(
        "value",
        [_FLOAT_JUST_ABOVE_CAP, _FLOAT_NEGATIVE, True, "0.5", _AWAIT_ABOVE_CAP],
        ids=[
            "float_just_above_cap",
            "float_negative",
            "bool_true",
            "string",
            "int_above_cap",
        ],
    )
    async def test_invalid_await_seconds_rejected(self, value: object) -> None:
        """Non-numeric, bool, negative, and above-cap values are rejected."""
        data = await _run_with_await(value)
        assert data.get("error") == "await_seconds_invalid", (
            f"await_seconds={value!r} must be rejected, got: {data!r}"
        )

    @pytest.mark.parametrize(
        "value",
        [0.5, 0.001, _FLOAT_AT_CAP, 0, 5],
        ids=["float_half", "float_tiny", "float_at_cap", "int_zero", "int_five"],
    )
    async def test_valid_await_seconds_accepted(self, value: float) -> None:
        """Float and int values within [0.0, _AWAIT_SECONDS_MAX] are accepted."""
        resolved = _make_resolved_future(_make_inline_result())
        data = await _run_with_await(value, resolved)
        assert data.get("error") != "await_seconds_invalid", (
            f"await_seconds={value!r} must be accepted, got error: {data!r}"
        )

    async def test_await_above_cap_message_mentions_cap(self):
        """Error message for above-cap await_seconds mentions the cap value."""
        data = await _run_with_await(_AWAIT_SECONDS_MAX + 1.0)
        assert data.get("error") == "await_seconds_invalid"
        message = data.get("message", "")
        assert (
            str(int(_AWAIT_SECONDS_MAX)) in message
            or str(_AWAIT_SECONDS_MAX) in message
        ), f"Error message must mention cap ({_AWAIT_SECONDS_MAX}), got: {message!r}"
