"""
AC7 RED/GREEN test: MCP admin `create_user` must assign a group.

Story #1593 AC7: a user created through the MCP admin handler
(`mcp/handlers/admin/__init__.py::create_user`) must land in a group
membership row, via the shared
`GroupAccessManager.ensure_user_group_membership()` primitive. The story
text explicitly flags this path as a verified gap: "does not assign a
group today."

Elevation is bypassed via `create_user.__wrapped__` (the raw handler
underneath `@require_mcp_elevation()`) -- elevation enforcement itself
has its own dedicated coverage (test_admin_tools_elevation_required.py);
this file is only about the group-assignment business logic.

These MCP admin handlers resolve `user_manager`/`group_manager` from a
server-wide module/app-state singleton by design (not constructor
injection), matching every other MCP admin-handler test in this
codebase (e.g. test_bug1315_omni_alias_index_path_fallback.py, which
substitutes test doubles at the identical two seams). `unittest.mock.
patch.object` as a context manager is used here as the substitution
mechanism at that same seam, scoped to the single request under test and
automatically restored on exit.

TDD: written FIRST, asserting the DESIRED behavior. Against unmodified
code this fails -- the created user has NO group membership at all.
"""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import admin as admin_module
from code_indexer.server.mcp.handlers import _utils
from code_indexer.server.services.group_access_manager import GroupAccessManager


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager(temp_db_path):
    return GroupAccessManager(temp_db_path)


class _StubUserManager:
    def create_user(self, username, password, role):
        return User(
            username=username,
            password_hash="x",
            role=role,
            created_at=datetime.now(timezone.utc),
        )


@pytest.fixture
def acting_admin():
    return User(
        username="mcp-admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "username,role,expected_group_name",
    [
        ("mcp-created-user", "normal_user", "users"),
        ("mcp-created-admin", "admin", "admins"),
    ],
)
def test_created_user_is_assigned_to_expected_group(
    group_manager, acting_admin, username, role, expected_group_name
):
    from code_indexer.server.app import app as real_app

    with (
        patch.object(_utils.app_module, "user_manager", _StubUserManager()),
        patch.object(real_app.state, "group_manager", group_manager, create=True),
    ):
        raw_result = admin_module.create_user.__wrapped__(
            {"username": username, "password": "StrongPass1!", "role": role},
            acting_admin,
        )

    result = json.loads(raw_result["content"][0]["text"])

    assert result["success"] is True, result
    membership = group_manager.get_user_group(username)
    assert membership is not None, (
        "user created via MCP create_user must have a group membership "
        "(Story #1593 AC7)"
    )
    assert membership.name == expected_group_name
