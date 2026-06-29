"""
Unit tests for _wait_for_health in scripts/analysis/multi_worker_throughput.py.

Covers:
- Returns True when /health responds 200 with a bearer token (C1 fix).
- Returns False when /health responds 401 (unauthenticated, no token).
- Returns False when connection always fails (timeout exhausted).

These tests use httpx's AsyncBaseTransport so no real server is needed and the
function under test is exercised without mocking its internals.
"""

from __future__ import annotations

import asyncio
import importlib.util
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Import the benchmark module from its non-package path
# ---------------------------------------------------------------------------

_SCRIPT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "scripts"
    / "analysis"
    / "multi_worker_throughput.py"
)


def _import_benchmark() -> types.ModuleType:
    """Dynamically import the benchmark script as a module."""
    spec = importlib.util.spec_from_file_location(
        "multi_worker_throughput", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"Could not load spec from {_SCRIPT_PATH}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_benchmark = _import_benchmark()
_wait_for_health = _benchmark._wait_for_health


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: Any) -> Any:
    """Run a coroutine synchronously using a fresh event loop.

    Uses asyncio.run() rather than asyncio.get_event_loop().run_until_complete()
    so that this helper is immune to event-loop state left by prior async tests
    (pytest-asyncio in auto/function mode closes the managed loop after each
    async test, leaving get_event_loop() with no current loop in Python 3.9+).
    """
    return asyncio.run(coro)


def _patch_async_client(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport
) -> None:
    """Inject a custom transport into every httpx.AsyncClient constructed."""

    class _PatchedClient(httpx.AsyncClient):
        def __init__(self, *a: Any, **kw: Any) -> None:
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _PatchedClient)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWaitForHealth:
    """Unit tests for the _wait_for_health helper (C1 fix: authenticated probe)."""

    def test_wait_for_health_returns_true_on_200_with_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        When /health returns 200 and _wait_for_health is called with a bearer
        token, it must send the Authorization header and return True.
        This is the core C1 fix: health probe happens AFTER login with the token.
        """
        captured_headers: dict[str, str] = {}

        class _OKTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                captured_headers.update(dict(request.headers))
                return httpx.Response(200, json={"status": "healthy"})

        _patch_async_client(monkeypatch, _OKTransport())

        result = _run(
            _wait_for_health("http://test-server", token="testtoken123", timeout=5.0)
        )

        assert result is True
        assert "authorization" in captured_headers
        assert captured_headers["authorization"] == "Bearer testtoken123"

    def test_wait_for_health_returns_false_on_401_without_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        When /health returns 401 for every request (unauthenticated probe),
        _wait_for_health returns False after the timeout.
        This demonstrates the original bug: the old no-token probe always got 401
        and the default run aborted before benchmarking.
        """

        class _UnauthorizedTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                return httpx.Response(401, json={"detail": "Not authenticated"})

        _patch_async_client(monkeypatch, _UnauthorizedTransport())

        result = _run(_wait_for_health("http://test-server", token=None, timeout=1.5))

        assert result is False

    def test_wait_for_health_returns_false_on_connection_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        When every request raises ConnectError (server not up), _wait_for_health
        returns False after the timeout expires without raising an exception.
        """

        class _DownTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx.Request
            ) -> httpx.Response:
                raise httpx.ConnectError("Connection refused")

        _patch_async_client(monkeypatch, _DownTransport())

        result = _run(_wait_for_health("http://test-server", token="tok", timeout=1.5))

        assert result is False
