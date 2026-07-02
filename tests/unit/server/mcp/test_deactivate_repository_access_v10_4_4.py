"""v10.4.4 tests for Finding 3.5: deactivate_repository access check fix.

deactivate_repository uses user_alias as the alias of the user's OWN activation,
not a golden-repo alias. The group-access check (which only knows golden aliases)
incorrectly rejects the owner. The activated_repo_manager already enforces
ownership at the data layer, so the protocol-level check must be skipped for
this tool.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Set
from unittest.mock import Mock

import pytest

from code_indexer.server.mcp.protocol import _check_repository_access
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    username: str = "Seba.Battig@lightspeeddms.com",
    role: UserRole = UserRole.NORMAL_USER,
) -> User:
    return User(
        username=username,
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _make_access_service(
    is_admin: bool = False,
    accessible_repos: Optional[Set[str]] = None,
) -> Mock:
    """Create a mock AccessFilteringService with call tracking."""
    service = Mock()
    service.is_admin_user = Mock(return_value=is_admin)
    service.get_accessible_repos = Mock(
        return_value=accessible_repos if accessible_repos is not None else {"cidx-meta"}
    )
    return service


# ---------------------------------------------------------------------------
# Finding 3.5: owner can deactivate their own activation
# ---------------------------------------------------------------------------


class TestDeactivateRepositoryOwnerAccess:
    """Owner can deactivate their own activation even though user_alias
    is not a golden-repo alias in accessible_repos."""

    def test_owner_can_deactivate_own_activation_non_admin(self):
        """Non-admin user with user_alias not in accessible_repos must NOT raise.

        This is the exact symptom from staging: owner gets 'Access denied'
        because user_alias 'test-v10-4-3-cidxmeta' is not a golden alias.
        """
        user = _make_user("Seba.Battig@lightspeeddms.com", UserRole.NORMAL_USER)
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},  # only golden aliases here
        )
        arguments = {"user_alias": "test-v10-4-3-cidxmeta"}

        # Should NOT raise — manager-layer enforces ownership
        _check_repository_access(
            arguments,
            user,
            "deactivate_repository",
            access_service,
        )

    def test_admin_bypass_still_works_for_deactivate(self):
        """Admin user can deactivate any activation — unchanged behavior."""
        user = _make_user("admin@example.com", UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)
        arguments = {"user_alias": "some-user-activation"}

        # Admin: should pass regardless
        _check_repository_access(
            arguments,
            user,
            "deactivate_repository",
            access_service,
        )

    def test_deactivate_skips_access_check_per_owner_enforcement_allowlist(self):
        """access_service.get_accessible_repos must NOT be called for
        deactivate_repository — the owner-enforcement allowlist causes an
        early return before the access check runs."""
        user = _make_user("Seba.Battig@lightspeeddms.com", UserRole.NORMAL_USER)
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),  # empty — would cause denial if checked
        )
        arguments = {"user_alias": "my-activation-alias"}

        _check_repository_access(
            arguments,
            user,
            "deactivate_repository",
            access_service,
        )

        # The access check must have been skipped entirely
        access_service.get_accessible_repos.assert_not_called()

    def test_other_tools_still_enforce_access_check(self):
        """search_code with a user_alias not in accessible_repos must still raise.

        The deactivate_repository exemption must be narrowly scoped."""
        user = _make_user("Seba.Battig@lightspeeddms.com", UserRole.NORMAL_USER)
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},  # user_alias not in here
        )
        arguments = {"user_alias": "some-repo-not-in-accessible"}

        with pytest.raises(ValueError, match="Access denied"):
            _check_repository_access(
                arguments,
                user,
                "search_code",
                access_service,
            )
