"""xray_search handler tests — await_seconds behavior (Bug #1070 async rewrite).

Covers TestXraySearchHandlerAwaitSeconds:
  positive await inline, pending future falls back to job_id,
  negative value rejected, above-cap value rejected.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict
from unittest.mock import patch

from .test_xray_search_handler import (
    VALID_PARAMS,
    _AWAIT_SECONDS_MAX,
    _import_handler,
    _make_resolved_future,
    _make_user,
    _parse_response,
    _xray_single_repo_env,
)
from code_indexer.server.auth.user_manager import UserRole

# Named constants — avoids magic numbers in test bodies
_AWAIT_ABOVE_CAP = int(_AWAIT_SECONDS_MAX) + 1  # 46 when cap is 45.0
_AWAIT_INLINE_SECONDS = 5       # positive; future must resolve within window
_AWAIT_PENDING_SECONDS = 0.1    # tiny window; pending future will not resolve
_AWAIT_NEGATIVE = -1            # below minimum 0.0
_ELAPSED_STUB = 0.1             # stub elapsed_seconds in inline result payloads


def _make_inline_result() -> Dict[str, Any]:
    """Return a minimal inline xray result dict for testing."""
    return {
        "matches": [{"file_path": "a.py", "line": 1, "snippet": "x"}],
        "evaluation_errors": [],
        "files_processed": 1,
        "files_total": 1,
        "elapsed_seconds": _ELAPSED_STUB,
        "truncated": False,
        "cache_handle": None,
    }


async def _run_with_await(
    await_seconds: float,
    resolved_future: "asyncio.Future[Any] | None" = None,
) -> Dict[str, Any]:
    """Run handler with given await_seconds and optional pre-resolved future.

    Returns the parsed response dict.
    """
    user = _make_user(UserRole.NORMAL_USER)
    params = {**VALID_PARAMS, "await_seconds": await_seconds}
    with patch(
        "code_indexer.server.mcp.handlers.xray._resolve_repo_path",
        return_value="/some/path",
    ):
        with _xray_single_repo_env(resolved_future=resolved_future):
            result = await _import_handler()(params, user)
    return _parse_response(result)


class TestXraySearchHandlerAwaitSeconds:
    """await_seconds=0 returns {job_id}; positive value awaits the future inline."""

    async def test_await_positive_returns_inline_when_future_resolves(self):
        """await_seconds=5 returns inline matches when future completes in window."""
        resolved = _make_resolved_future(_make_inline_result())
        data = await _run_with_await(_AWAIT_INLINE_SECONDS, resolved)
        assert "matches" in data
        assert "job_id" not in data

    async def test_await_positive_returns_job_id_when_future_stays_pending(self):
        """await_seconds=0.1 returns {job_id} when future does not complete."""
        pending: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        data = await _run_with_await(_AWAIT_PENDING_SECONDS, pending)
        assert "job_id" in data

    async def test_await_negative_rejected(self):
        """await_seconds=-1 is rejected with await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": _AWAIT_NEGATIVE}
        result = await _import_handler()(params, user)
        assert _parse_response(result).get("error") == "await_seconds_invalid"

    async def test_await_above_cap_rejected(self):
        """await_seconds above _AWAIT_SECONDS_MAX is rejected with await_seconds_invalid."""
        user = _make_user(UserRole.NORMAL_USER)
        params = {**VALID_PARAMS, "await_seconds": _AWAIT_ABOVE_CAP}
        result = await _import_handler()(params, user)
        assert _parse_response(result).get("error") == "await_seconds_invalid"


# ---------------------------------------------------------------------------
# Tests: float values and new 45s cap (Bug #1070: cap lowered from 120 to 45)
# ---------------------------------------------------------------------------

import pytest as _pytest  # noqa: E402

# Float boundary constants (precise float values, not cast from int)
_FLOAT_AT_CAP = float(_AWAIT_SECONDS_MAX)           # 45.0 — boundary (accepted)
_FLOAT_JUST_ABOVE_CAP = _AWAIT_SECONDS_MAX + 0.001  # 45.001 — just over (rejected)
_FLOAT_NEGATIVE = -0.001                             # smallest negative (rejected)


class TestXraySearchHandlerAwaitSecondsV2:
    """Float await_seconds and the 45s cap lowered by Bug #1070."""

    @_pytest.mark.parametrize(
        "value",
        [_FLOAT_JUST_ABOVE_CAP, _FLOAT_NEGATIVE, True, "0.5"],
        ids=["float_just_above_cap", "float_negative", "bool_true", "string"],
    )
    async def test_invalid_await_seconds_rejected(self, value: object) -> None:
        """Non-numeric, bool, negative, and above-cap values are rejected."""
        user = _make_user(UserRole.NORMAL_USER)
        result = await _import_handler()({**VALID_PARAMS, "await_seconds": value}, user)
        assert _parse_response(result).get("error") == "await_seconds_invalid"

    @_pytest.mark.parametrize(
        "value",
        [0.5, 0.001, _FLOAT_AT_CAP, 0, 5],
        ids=["float_half", "float_tiny", "float_at_cap", "int_zero", "int_five"],
    )
    async def test_valid_await_seconds_accepted(self, value: float) -> None:
        """Float and int values within [0.0, 45.0] are accepted (no error)."""
        pending: asyncio.Future[object] = asyncio.get_event_loop().create_future()
        data = await _run_with_await(value, pending)
        assert data.get("error") != "await_seconds_invalid", (
            f"await_seconds={value!r} must be accepted, got error: {data!r}"
        )
