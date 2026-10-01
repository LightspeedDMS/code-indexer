"""
The MCP elevation gate points a caller without TOTP at the setup page for
their role, exactly as the REST gate does (``_mfa_setup_url_for_role``):
admins get the admin page, every other role the self-service page.

Driven through the real MCP ``tools/call`` dispatcher with a real
``TOTPService`` (caller not enrolled) and a real ``ElevatedSessionManager``.
The gated tool is ``manage_mcp_credential`` (action=create for self), which
every role may call and which is elevation-gated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from code_indexer.server import app as real_app_module
from code_indexer.server.auth.dependencies import (
    _TOTP_SETUP_URL,
    _USER_TOTP_SETUP_URL,
)
from code_indexer.server.auth.elevated_session_manager import ElevatedSessionManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import handle_tools_call

_DECORATOR = "code_indexer.server.mcp.auth.elevation_decorator"


def _user(role: UserRole) -> User:
    return User(
        username=f"no-mfa-{role.value}",
        password_hash="hashed",
        role=role,
        created_at=datetime.now(timezone.utc),
    )


async def _call_gated_tool(user: User, tmp_path: Path) -> Dict[str, Any]:
    totp = TOTPService(
        db_path=str(tmp_path / "totp.db"),
        mfa_encryption_key=Fernet.generate_key().decode(),
    )
    esm = ElevatedSessionManager(
        idle_timeout_seconds=300,
        max_age_seconds=1800,
        db_path=str(tmp_path / "elevation.db"),
    )
    with (
        patch.object(real_app_module.app.state, "group_manager", None, create=True),
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch(f"{_DECORATOR}._is_elevation_enforcement_enabled", return_value=True),
        patch(f"{_DECORATOR}.elevated_session_manager", esm),
        patch(f"{_DECORATOR}.get_totp_service", return_value=totp),
    ):
        response = await handle_tools_call(
            {
                "name": "manage_mcp_credential",
                "arguments": {"action": "create", "description": "laptop"},
            },
            user,
            elevation_key="jti-setup-url-by-role",
        )
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role, expected_url",
    [
        (UserRole.NORMAL_USER, _USER_TOTP_SETUP_URL),
        (UserRole.POWER_USER, _USER_TOTP_SETUP_URL),
        (UserRole.ADMIN, _TOTP_SETUP_URL),
    ],
    ids=["normal", "power", "admin"],
)
async def test_totp_setup_required_points_at_setup_page_for_role(
    tmp_path, role, expected_url
):
    data = await _call_gated_tool(_user(role), tmp_path)

    assert data.get("error") == "totp_setup_required"
    assert data.get("setup_url") == expected_url
