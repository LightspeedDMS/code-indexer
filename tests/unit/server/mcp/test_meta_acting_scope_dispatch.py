"""The MCP dispatcher hands the acting-user scope to catalog visibility.

Front door: the real handle_tools_call() with real registries; only the
handler body is a probe that records what MetaVisibility it observes. The
access decision uses the real AccessFilteringService over a real group
store. Neutral names only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
from code_indexer.server.mcp.protocol import handle_tools_call
from code_indexer.server.services.cidx_meta_access import (
    MetaVisibility,
    resolve_meta_visibility,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

PROBE_TOOL = "list_repositories"
RESTRICTED_EMAIL = "fixture-user@example.com"


def _user(username: str, role: UserRole, email: Optional[str] = None) -> User:
    return User(
        username=username,
        password_hash="hash",
        role=role,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        email=email,
    )


class _Users:
    """Email lookup the acting-users resolution performs."""

    def __init__(self, users: List[User]):
        self._by_email = {u.email: u for u in users}

    def get_user_by_email(self, email: str) -> Optional[User]:
        return self._by_email.get(email)


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


async def _observe(
    fx: CidxMetaFixture, user: User, arguments: Dict[str, Any]
) -> MetaVisibility:
    observed: List[MetaVisibility] = []

    def _probe(args: Dict[str, Any], user: User) -> Dict[str, Any]:
        observed.append(resolve_meta_visibility(user.username, fx.access_service))
        return {"content": [{"type": "text", "text": "{}"}]}

    app_state = Mock()
    app_state.access_filtering_service = fx.access_service
    app_state.user_manager = _Users(
        [_user(RESTRICTED_USER, UserRole.NORMAL_USER, RESTRICTED_EMAIL)]
    )
    with (
        patch("code_indexer.server.mcp.handlers.app_module") as app_module,
        patch(
            "code_indexer.server.services.langfuse_service.get_langfuse_service",
            return_value=None,
        ),
        patch.dict(HANDLER_REGISTRY, {PROBE_TOOL: _probe}),
    ):
        app_module.app.state = app_state
        await handle_tools_call({"name": PROBE_TOOL, "arguments": arguments}, user)
    assert len(observed) == 1
    return observed[0]


async def test_acting_scope_applies_and_resets(fx: CidxMetaFixture) -> None:
    admin = _user(ADMIN_USER, UserRole.ADMIN)
    scoped = await _observe(fx, admin, {"acting_users": [RESTRICTED_EMAIL]})
    unscoped = await _observe(fx, admin, {})
    assert scoped.unrestricted is False
    assert scoped.visible == frozenset({ALPHA})
    assert unscoped.unrestricted is True


async def test_admin_role_without_admins_group_is_restricted(
    fx: CidxMetaFixture,
) -> None:
    role_admin = _user(RESTRICTED_USER, UserRole.ADMIN)
    vis = await _observe(fx, role_admin, {})
    assert vis.unrestricted is False
    assert vis.visible == frozenset({ALPHA})
