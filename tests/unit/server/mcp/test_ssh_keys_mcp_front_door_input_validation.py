"""
MCP elevation parity for manage_ssh_key, proven through the REAL protocol
dispatch path.

tests/unit/server/mcp/test_ssh_keys_mcp_elevation.py already
proves the DECORATED functions themselves gate correctly when called
directly (matching the pattern in
tests/unit/server/mcp/test_admin_tools_elevation_required.py). This file
instead drives ``handle_tools_call`` -> ``_invoke_handler`` -> the real
``require_mcp_elevation`` decorator wired on ``manage_ssh_key``'s
``assign_host`` action -- proving the session_key injection wiring
(``__mcp_requires_session_key__`` on the top-level dispatcher) actually
carries the JWT jti through to the decorator when a real MCP tool call is
dispatched, not merely that the decorated function behaves correctly in
isolation.

Modeled on the ``_handle_tools_call_context``/``_call_handle_tools_call``
harness in tests/unit/server/mcp/test_session_key_injection_protocol.py
(AC6/AC7), and the real-ElevatedSessionManager-with-no-window pattern in
tests/unit/server/mcp/test_admin_tools_elevation_required.py.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
import code_indexer.server.mcp.handlers.ssh_keys as ssh_keys_handlers

_SESSION_ID = "mcp-session-ssh-006-front-door"
_ELEVATION_KEY = "jwt-jti-fixture-ssh-keys-front-door"
_IDLE = 300
_MAX_AGE = 1800

_ENFORCEMENT_PATH = (
    "code_indexer.server.mcp.auth.elevation_decorator._is_elevation_enforcement_enabled"
)
_TOTP_PATH = "code_indexer.server.mcp.auth.elevation_decorator.get_totp_service"
_ESM_PATH = "code_indexer.server.mcp.auth.elevation_decorator.elevated_session_manager"


def _parse_mcp_response(response: dict) -> dict:
    content = response.get("content", [])
    return json.loads(content[0]["text"])  # type: ignore[no-any-return]


def _mock_admin_user() -> MagicMock:
    user = MagicMock()
    user.username = "admin"
    user.has_permission.return_value = True
    return user


@asynccontextmanager
async def _real_dispatch_context(enforcement_enabled: bool, esm, totp_svc):
    """Patch everything ``handle_tools_call`` needs EXCEPT the elevation
    decorator's own gates, which run for REAL -- that is the entire point
    of this test. Mirrors test_session_key_injection_protocol.py's
    _handle_tools_call_context, but with enforcement parametrized instead
    of always mocked off."""
    from code_indexer.server.mcp.protocol import handle_tools_call

    mock_session_state = MagicMock()
    mock_session_state.is_impersonating = False

    user = _mock_admin_user()

    with (
        patch(
            "code_indexer.server.mcp.handlers.HANDLER_REGISTRY",
            {"manage_ssh_key": ssh_keys_handlers.handle_manage_ssh_key},
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.tools.TOOL_REGISTRY",
            {
                "manage_ssh_key": {
                    "required_permission": "repository:admin",
                    "name": "manage_ssh_key",
                }
            },
            create=True,
        ),
        patch(
            "code_indexer.server.mcp.session_registry.get_session_registry"
        ) as mock_registry,
        patch("code_indexer.server.mcp.protocol._check_repository_access"),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch("code_indexer.server.mcp.protocol.api_metrics_service"),
        patch(_ENFORCEMENT_PATH, return_value=enforcement_enabled),
        patch(_ESM_PATH, esm),
        patch(_TOTP_PATH, return_value=totp_svc),
    ):
        mock_registry.return_value.get_or_create_session.return_value = (
            mock_session_state
        )
        yield user, handle_tools_call


@pytest.fixture
def real_elevated_session_manager(tmp_path: Path) -> ElevatedSessionManager:
    """A REAL ElevatedSessionManager with NO elevation window ever created
    for _ELEVATION_KEY -- reproduces "admin JWT, no live TOTP" exactly."""
    return ElevatedSessionManager(
        idle_timeout_seconds=_IDLE,
        max_age_seconds=_MAX_AGE,
        db_path=str(tmp_path / "elev.db"),
    )


@pytest.fixture
def totp_enabled() -> MagicMock:
    svc = MagicMock()
    svc.is_mfa_enabled.return_value = True
    return svc


@pytest.mark.asyncio
async def test_real_dispatch_enforces_elevation_when_enabled(
    real_elevated_session_manager: ElevatedSessionManager,
    totp_enabled: MagicMock,
) -> None:
    """The REAL end-to-end MCP call path: handle_tools_call dispatches
    manage_ssh_key/assign_host, which reaches the require_mcp_elevation
    decorator via the session_key injection wired on
    handle_manage_ssh_key.__mcp_requires_session_key__. With enforcement ON,
    TOTP configured, but NO active elevation window for this JWT jti, the
    call must be refused with elevation_required -- proving the wiring, not
    just the decorated function in isolation.

    Discriminating RED: without @require_mcp_elevation on
    mcp/handlers/ssh_keys.py, this exact dispatch would reach the
    real SSHKeyManager instead of being refused.
    """
    async with _real_dispatch_context(
        enforcement_enabled=True,
        esm=real_elevated_session_manager,
        totp_svc=totp_enabled,
    ) as (user, handle_tools_call):
        result = await handle_tools_call(
            params={
                "name": "manage_ssh_key",
                "arguments": {
                    "action": "assign_host",
                    "name": "deploy-key_1.v2",
                    "hostname": "github.com",
                },
            },
            user=user,
            session_id=_SESSION_ID,
            elevation_key=_ELEVATION_KEY,
        )

    parsed = _parse_mcp_response(result)
    assert parsed.get("error") == "elevation_required", (
        f"Expected elevation_required through the real dispatch path, got: {result}"
    )


@pytest.mark.asyncio
async def test_real_dispatch_passes_through_when_enforcement_disabled(
    real_elevated_session_manager: ElevatedSessionManager,
    totp_enabled: MagicMock,
) -> None:
    """With elevation_enforcement_enabled=False (the shipped default), the
    SAME real dispatch path must reach
    the real inner handler -- proving the decorator's kill-switch passthrough
    survives the full handle_tools_call -> _invoke_handler round trip, not
    only a direct call to the decorated function."""
    mock_manager = MagicMock()
    mock_meta = MagicMock()
    mock_meta.name = "deploy-key_1.v2"
    mock_meta.fingerprint = "SHA256:fake"
    mock_meta.key_type = "ed25519"
    mock_meta.hosts = ["github.com"]
    mock_meta.email = None
    mock_meta.description = None
    mock_manager.assign_key_to_host.return_value = mock_meta

    async with _real_dispatch_context(
        enforcement_enabled=False,
        esm=real_elevated_session_manager,
        totp_svc=totp_enabled,
    ) as (user, handle_tools_call):
        with patch(
            "code_indexer.server.mcp.handlers.ssh_keys.get_ssh_key_manager",
            return_value=mock_manager,
        ):
            result = await handle_tools_call(
                params={
                    "name": "manage_ssh_key",
                    "arguments": {
                        "action": "assign_host",
                        "name": "deploy-key_1.v2",
                        "hostname": "github.com",
                    },
                },
                user=user,
                session_id=_SESSION_ID,
                elevation_key=_ELEVATION_KEY,
            )

    parsed = _parse_mcp_response(result)
    assert parsed.get("error") != "elevation_required", (
        f"enforcement disabled must pass through, got: {result}"
    )
    assert parsed.get("success") is True
    mock_manager.assign_key_to_host.assert_called_once_with(
        key_name="deploy-key_1.v2", hostname="github.com", force=False
    )
