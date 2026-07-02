"""v10.4.5 admin-mask verification tests (Defect 3).

Previous test agents ran as admin, masking access-control bugs because admins
bypass _check_repository_access entirely. These tests explicitly use non-admin
users to verify the v10.4.4 fixes work for the code paths that matter.

C3.5 (deactivate ownership-enforced allowlist):
  Non-admin user can call deactivate_repository for their own activation.
  user_alias is NOT a golden-repo alias, so it is not in accessible_repos.
  The allowlist exemption for deactivate_repository must skip the access check.

C3.7 (accessible vs. inaccessible alias wording for non-admin):
  xray_search with an alias that IS in accessible_repos: guard passes silently.
  xray_search with an alias NOT in accessible_repos: exact "Access denied" raised.

All tests target the non-admin path explicitly — no admin bypass masking.
Exact error strings are asserted to pin the protocol message format.

Shared helpers declared:
- _make_user(username, role) — user factory
- _make_access_service(is_admin, accessible_repos) — mock AccessFilteringService
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from typing import Optional, Set
from unittest.mock import Mock

from code_indexer.server.mcp.protocol import _check_repository_access
from code_indexer.server.auth.user_manager import User, UserRole

CREATED_AT = datetime(2024, 1, 1, tzinfo=timezone.utc)
TEST_USERNAME = "Seba.Battig@lightspeeddms.com"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user(
    username: str = TEST_USERNAME,
    role: UserRole = UserRole.NORMAL_USER,
) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=CREATED_AT,
    )


def _make_access_service(
    is_admin: bool = False,
    accessible_repos: Optional[Set[str]] = None,
) -> Mock:
    service = Mock()
    service.is_admin_user = Mock(return_value=is_admin)
    service.get_accessible_repos = Mock(
        return_value=accessible_repos if accessible_repos is not None else set()
    )
    return service


# ---------------------------------------------------------------------------
# C3.5: non-admin owner can deactivate own activation
# ---------------------------------------------------------------------------


def test_deactivate_repository_non_admin_owner_succeeds():
    """Non-admin user with user_alias NOT in accessible_repos must NOT raise.

    user_alias is an activation alias, not a golden-repo alias.
    The allowlist exemption for deactivate_repository skips the access check,
    so the owner can deactivate regardless of accessible_repos.
    Admin bypass is NOT in effect here — the non-admin path is explicitly tested.
    """
    user = _make_user(TEST_USERNAME, UserRole.NORMAL_USER)
    access_service = _make_access_service(
        is_admin=False,
        accessible_repos={"cidx-meta"},  # user_alias deliberately absent
    )

    # Must NOT raise — allowlist exemption applies for non-admin owners
    _check_repository_access(
        arguments={"user_alias": "test-v10-4-3-cidxmeta"},
        effective_user=user,
        tool_name="deactivate_repository",
        access_service=access_service,
    )

    # The access check must have been skipped entirely (allowlist early return)
    access_service.get_accessible_repos.assert_not_called()


# ---------------------------------------------------------------------------
# C3.7: accessible alias passes the guard silently (no exception)
# ---------------------------------------------------------------------------


def test_xray_accessible_alias_passes_guard_for_non_admin():
    """Non-admin with an alias that IS in accessible_repos: guard does NOT raise.

    When the alias is accessible, _check_repository_access returns silently.
    The handler then resolves the alias; if it does not exist in the file system,
    the handler returns repository_not_found — that is a separate concern from
    the access guard tested here.
    """
    user = _make_user(TEST_USERNAME, UserRole.NORMAL_USER)
    access_service = _make_access_service(
        is_admin=False,
        accessible_repos={"cidx-meta", "accessible-repo"},
    )

    # Must NOT raise — the alias is in the user's accessible set
    _check_repository_access(
        arguments={"repository_alias": "accessible-repo"},
        effective_user=user,
        tool_name="xray_search",
        access_service=access_service,
    )


# ---------------------------------------------------------------------------
# C3.7: inaccessible alias raises exact "Access denied" message
# ---------------------------------------------------------------------------


def test_xray_inaccessible_repo_returns_access_denied_for_non_admin():
    """Non-admin with an alias NOT in accessible_repos: exact ValueError raised.

    The error message must name the alias AND the username — exact string match
    pins the protocol.py error format so regressions are caught.
    """
    user = _make_user(TEST_USERNAME, UserRole.NORMAL_USER)
    access_service = _make_access_service(
        is_admin=False,
        accessible_repos={"cidx-meta"},  # denied-repo NOT in set
    )

    expected_error = (
        f"Access denied: repository 'denied-repo' is not accessible"
        f" to user '{TEST_USERNAME}'"
    )

    with pytest.raises(ValueError) as exc_info:
        _check_repository_access(
            arguments={"repository_alias": "denied-repo"},
            effective_user=user,
            tool_name="xray_search",
            access_service=access_service,
        )

    assert str(exc_info.value) == expected_error
