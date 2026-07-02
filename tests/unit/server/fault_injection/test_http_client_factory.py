"""
Tests for HttpClientFactory and the anti-regression scan (Scenario 18, 28).

Story #746 — Scenarios 18, 28.

TDD: tests written BEFORE production code.

Scenario 28: factory with fault_injection enabled wraps transport;
             factory without fault injection uses default transport.
Scenario 18: zero direct httpx.AsyncClient() outside HttpClientFactory
             in src/code_indexer/server/.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import httpx
import pytest

from code_indexer.server.fault_injection.fault_injection_service import (
    FaultInjectionService,
)
from code_indexer.server.fault_injection.fault_injecting_transport import (
    FaultInjectingTransport,
)
from code_indexer.server.fault_injection.fault_injecting_sync_transport import (
    FaultInjectingSyncTransport,
)
from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEED = 42
SERVER_SRC_ROOT = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
)
FACTORY_MODULE_PATH = SERVER_SRC_ROOT / "fault_injection" / "http_client_factory.py"
# NullFaultFactory is part of the factory module family — it legitimately
# constructs plain httpx clients by design, just like HttpClientFactory.
NULL_FACTORY_MODULE_PATH = SERVER_SRC_ROOT / "fault_injection" / "null_factory.py"

# Simple pattern — comment filtering done by _line_is_in_comment().
_DIRECT_ASYNC_CLIENT_PATTERN = re.compile(r"\bhttpx\.AsyncClient\s*\(")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(enabled: bool = True) -> FaultInjectionService:
    return FaultInjectionService(enabled=enabled, rng=random.Random(SEED))


# ===========================================================================
# Scenario 28: transport installation
# ===========================================================================


@pytest.mark.asyncio
async def test_factory_with_injection_enabled_returns_client():
    """Factory with active service returns an httpx.AsyncClient instance."""
    svc = _make_service(enabled=True)
    factory = HttpClientFactory(fault_injection_service=svc)
    async with factory.create_client() as client:
        assert isinstance(client, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_factory_without_injection_returns_client():
    """Factory with no service returns an httpx.AsyncClient instance."""
    factory = HttpClientFactory(fault_injection_service=None)
    async with factory.create_client() as client:
        assert isinstance(client, httpx.AsyncClient)


@pytest.mark.asyncio
async def test_factory_with_injection_installs_fault_transport():
    """
    Scenario 28: factory with enabled service installs FaultInjectingTransport
    as the async transport on the returned client.
    """
    svc = _make_service(enabled=True)
    factory = HttpClientFactory(fault_injection_service=svc)
    async with factory.create_client() as client:
        assert isinstance(client._transport, FaultInjectingTransport)


@pytest.mark.asyncio
async def test_factory_without_injection_does_not_install_fault_transport():
    """
    Scenario 28: factory with no service must NOT install FaultInjectingTransport.
    """
    factory = HttpClientFactory(fault_injection_service=None)
    async with factory.create_client() as client:
        assert not isinstance(client._transport, FaultInjectingTransport)


@pytest.mark.asyncio
async def test_factory_with_disabled_service_does_not_install_fault_transport():
    """Service disabled -> no fault transport installed."""
    svc = _make_service(enabled=False)
    factory = HttpClientFactory(fault_injection_service=svc)
    async with factory.create_client() as client:
        assert not isinstance(client._transport, FaultInjectingTransport)


@pytest.mark.asyncio
async def test_factory_passes_timeout_kwarg_to_client():
    """Extra timeout kwarg is forwarded to httpx.AsyncClient."""
    factory = HttpClientFactory(fault_injection_service=None)
    timeout = httpx.Timeout(30.0)
    async with factory.create_client(timeout=timeout) as client:
        assert client.timeout == timeout


@pytest.mark.asyncio
async def test_factory_creates_independent_clients():
    """Each create_client() call returns a distinct client instance."""
    factory = HttpClientFactory(fault_injection_service=None)
    async with factory.create_client() as c1:
        async with factory.create_client() as c2:
            assert c1 is not c2


# ===========================================================================
# Scenario 28 (sync): create_sync_client transport installation
# ===========================================================================


class TestSyncClientFactory:
    """Tests for HttpClientFactory.create_sync_client()."""

    def test_with_injection_enabled_returns_client(self) -> None:
        """Factory with active service returns an httpx.Client instance."""
        svc = _make_service(enabled=True)
        factory = HttpClientFactory(fault_injection_service=svc)
        with factory.create_sync_client() as client:
            assert isinstance(client, httpx.Client)

    def test_without_injection_returns_client(self) -> None:
        """Factory with no service returns an httpx.Client instance."""
        factory = HttpClientFactory(fault_injection_service=None)
        with factory.create_sync_client() as client:
            assert isinstance(client, httpx.Client)

    def test_with_injection_installs_fault_sync_transport(self) -> None:
        """Factory with enabled service installs FaultInjectingSyncTransport."""
        svc = _make_service(enabled=True)
        factory = HttpClientFactory(fault_injection_service=svc)
        with factory.create_sync_client() as client:
            assert isinstance(client._transport, FaultInjectingSyncTransport)

    def test_without_injection_does_not_install_fault_sync_transport(self) -> None:
        """Factory with no service must NOT install FaultInjectingSyncTransport."""
        factory = HttpClientFactory(fault_injection_service=None)
        with factory.create_sync_client() as client:
            assert not isinstance(client._transport, FaultInjectingSyncTransport)

    def test_with_disabled_service_does_not_install_fault_sync_transport(
        self,
    ) -> None:
        """Service disabled -> no fault transport installed on sync client."""
        svc = _make_service(enabled=False)
        factory = HttpClientFactory(fault_injection_service=svc)
        with factory.create_sync_client() as client:
            assert not isinstance(client._transport, FaultInjectingSyncTransport)

    def test_passes_timeout_kwarg_to_client(self) -> None:
        """Extra timeout kwarg is forwarded to httpx.Client."""
        factory = HttpClientFactory(fault_injection_service=None)
        timeout = httpx.Timeout(30.0)
        with factory.create_sync_client(timeout=timeout) as client:
            assert client.timeout == timeout

    def test_creates_independent_clients(self) -> None:
        """Each create_sync_client() call returns a distinct client instance."""
        factory = HttpClientFactory(fault_injection_service=None)
        with factory.create_sync_client() as c1:
            with factory.create_sync_client() as c2:
                assert c1 is not c2

    def test_caller_supplied_transport_wrapped_when_injection_active(self) -> None:
        """Caller-supplied transport is wrapped by FaultInjectingSyncTransport."""
        svc = _make_service(enabled=True)
        factory = HttpClientFactory(fault_injection_service=svc)
        caller_transport = httpx.HTTPTransport()
        with factory.create_sync_client(transport=caller_transport) as client:
            assert isinstance(client._transport, FaultInjectingSyncTransport)
            assert client._transport._wrapped is caller_transport


# ===========================================================================
# NullFaultFactory — passthrough factory tests
# ===========================================================================


class TestNullFaultFactory:
    """NullFaultFactory returns plain httpx clients — no fault injection installed."""

    def test_create_sync_client_returns_plain_client_no_transport(self) -> None:
        """create_sync_client() with no transport returns a plain httpx.Client."""
        from code_indexer.server.fault_injection.null_factory import NullFaultFactory

        factory = NullFaultFactory()
        with factory.create_sync_client() as client:
            assert isinstance(client, httpx.Client)
            # No fault injection transport — should not be FaultInjectingSyncTransport.
            assert not isinstance(client._transport, FaultInjectingSyncTransport)

    def test_create_sync_client_preserves_caller_transport(self) -> None:
        """create_sync_client(transport=t) wires the caller-supplied transport."""
        from code_indexer.server.fault_injection.null_factory import NullFaultFactory

        factory = NullFaultFactory()
        caller_transport = httpx.HTTPTransport()
        with factory.create_sync_client(transport=caller_transport) as client:
            assert isinstance(client, httpx.Client)
            assert client._transport is caller_transport

    async def test_create_client_returns_plain_async_client(self) -> None:
        """create_client() returns a plain httpx.AsyncClient with no fault transport."""
        from code_indexer.server.fault_injection.null_factory import NullFaultFactory

        factory = NullFaultFactory()
        async with factory.create_client() as client:
            assert isinstance(client, httpx.AsyncClient)


# ===========================================================================
# Scenario 18: anti-regression — no direct httpx.AsyncClient() in server/
# ===========================================================================

# Infrastructure/auth clients that legitimately use httpx.AsyncClient directly.
# These communicate with auth providers, CI/CD systems, and internal server
# infrastructure — NOT with external embedding/reranking providers.
# Fault injection is not required for these paths (Story #746 Scenario 18).
_EXCLUDED_PATHS = frozenset(
    {
        "auth/oidc/oidc_provider.py",
        "clients/claude_server_client.py",
        "clients/forge_client.py",
        "clients/github_actions_client.py",
        "clients/gitlab_ci_client.py",
        # Connectivity testers and diagnostic probes — one-time health checks,
        # not the production embedding/reranking hot path.
        "services/api_key_management.py",
        "services/diagnostics_service.py",
    }
)


def _python_files_in_server(root: Path):
    """Yield .py files under *root*, excluding factory modules and excluded paths."""
    _factory_paths = {FACTORY_MODULE_PATH.resolve(), NULL_FACTORY_MODULE_PATH.resolve()}
    for path in root.rglob("*.py"):
        if path.resolve() in _factory_paths:
            continue
        rel = path.relative_to(root)
        # Normalize to forward-slash strings for cross-platform comparison
        rel_str = rel.as_posix()
        if rel_str in _EXCLUDED_PATHS:
            continue
        yield path


def _line_is_in_comment(line: str) -> bool:
    """Return True if the line (stripped) starts with a Python # comment."""
    return line.lstrip().startswith("#")


def test_no_direct_async_client_construction_outside_factory():
    """
    Scenario 18: scan src/code_indexer/server/ for any direct
    httpx.AsyncClient(...) calls that are not in the factory module or
    in known infrastructure/auth clients excluded by _EXCLUDED_PATHS.

    Every outbound async HTTP client to external embedding/reranking providers
    must be created via HttpClientFactory so fault injection can be applied.
    """
    violations: list[str] = []

    for py_file in _python_files_in_server(SERVER_SRC_ROOT):
        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            pytest.fail(f"Failed to read server source file {py_file}: {exc}")

        for lineno, line in enumerate(source.splitlines(), start=1):
            if _DIRECT_ASYNC_CLIENT_PATTERN.search(line):
                if not _line_is_in_comment(line):
                    rel = py_file.relative_to(SERVER_SRC_ROOT)
                    violations.append(f"{rel}:{lineno}: {line.strip()}")

    assert violations == [], (
        "Direct httpx.AsyncClient() construction found outside HttpClientFactory "
        "(excluding known infrastructure/auth clients in _EXCLUDED_PATHS):\n"
        + "\n".join(violations)
    )
