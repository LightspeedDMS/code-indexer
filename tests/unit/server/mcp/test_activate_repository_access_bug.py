"""
Regression tests for activate_repository access-check bug.

Bug 2: activate_repository denied access to user_alias before creating it.

Root cause: _check_repository_access in protocol.py scans arguments for
repo-identifying parameters in order:
  repository_alias, alias, user_alias, repo_alias

When activate_repository is called with:
  golden_repo_alias='humanize-global', user_alias='humanize-test'

The guard finds user_alias='humanize-test' (because golden_repo_alias is
not in the scan list) and checks if 'humanize-test' is accessible.
'humanize-test' doesn't exist yet (it's being CREATED), so access is denied.

Fix: Add 'golden_repo_alias' (and handle 'golden_repo_aliases' list) to the
parameter scan list in _check_repository_access, prioritized BEFORE user_alias.

These tests verify:
  - golden_repo_alias is checked instead of user_alias
  - golden_repo_aliases list items are each checked
  - user_alias is not checked during activation (it's the new alias being created)
  - Error message references golden_repo_alias, not user_alias
  - Admin user bypasses check for activation
"""
import pytest
from datetime import datetime
from unittest.mock import Mock

from code_indexer.server.mcp.protocol import _check_repository_access
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username=username,
        password_hash="hashed",
        role=role,
        created_at=datetime(2024, 1, 1),
    )


def _make_access_service(
    is_admin: bool = False,
    accessible_repos: set = None,  # type: ignore[assignment]
) -> Mock:
    svc = Mock()
    svc.is_admin_user = Mock(return_value=is_admin)
    svc.get_accessible_repos = Mock(
        return_value=accessible_repos if accessible_repos is not None else set()
    )
    return svc


class TestActivateRepositoryAccessGuard:
    """_check_repository_access must check golden_repo_alias not user_alias."""

    def test_golden_repo_alias_is_checked_not_user_alias(self):
        """
        Bug regression: when golden_repo_alias is accessible and user_alias is
        a new name (not existing), activation must SUCCEED — not be denied.

        Reproduces: activate_repository('humanize-global', user_alias='humanize-test')
        -> "Access denied: repository 'humanize-test' is not accessible"
        """
        user = _make_user("Seba.Battig@lightspeeddms.com")
        # User has access to 'humanize' (bare name of 'humanize-global')
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"humanize", "cidx-meta"},
        )

        # Exactly what activate_repository passes to the guard
        arguments = {
            "golden_repo_alias": "humanize-global",
            "user_alias": "humanize-test",  # New alias being created — does NOT exist
        }

        # Must NOT raise — golden_repo_alias is accessible
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="activate_repository",
            access_service=access_service,
        )

    def test_access_denied_when_golden_repo_alias_inaccessible(self):
        """
        Access is denied when user lacks access to golden_repo_alias itself.
        Error message must reference golden_repo_alias, not user_alias.
        """
        user = _make_user("bob")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},  # humanize NOT accessible
        )

        arguments = {
            "golden_repo_alias": "humanize-global",
            "user_alias": "my-humanize",
        }

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="activate_repository",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "humanize" in error_str, (
            f"Error must reference golden_repo_alias 'humanize', got: {error_str}"
        )
        # user_alias ('my-humanize') must NOT appear as the blocked resource
        assert "my-humanize" not in error_str, (
            f"Error must NOT reference user_alias 'my-humanize', got: {error_str}"
        )

    def test_user_alias_alone_does_not_trigger_access_check_for_activation(self):
        """
        When only user_alias is present (no golden_repo_alias), access guard
        should still check user_alias for non-activation tools (backward compat).

        This verifies that the fix for golden_repo_alias doesn't break the
        existing user_alias check for other tools like get_repository_status.
        """
        user = _make_user("alice")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        # Tool that uses user_alias for existing repo (get_repository_status)
        arguments = {"user_alias": "hidden-repo"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="get_repository_status",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "hidden-repo" in error_str

    def test_admin_user_bypasses_golden_repo_alias_check(self):
        """Admin user (via is_admin_user) can activate any golden repo."""
        admin_user = _make_user("admin")
        access_service = _make_access_service(is_admin=True)

        arguments = {
            "golden_repo_alias": "restricted-global",
            "user_alias": "my-restricted",
        }

        # Must NOT raise — admin bypasses
        _check_repository_access(
            arguments=arguments,
            effective_user=admin_user,
            tool_name="activate_repository",
            access_service=access_service,
        )

        access_service.get_accessible_repos.assert_not_called()

    def test_golden_repo_aliases_list_each_checked(self):
        """
        When golden_repo_aliases (list) is provided for composite activation,
        each alias in the list must be checked against accessible repos.
        Access is denied if ANY alias in the list is inaccessible.
        """
        user = _make_user("charlie")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"humanize", "cidx-meta"},
            # 'shortuuid' is NOT accessible
        )

        arguments = {
            "golden_repo_aliases": ["humanize-global", "shortuuid-global"],
            "user_alias": "my-composite",
        }

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="activate_repository",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        # The inaccessible golden_repo_alias must appear in the error
        assert "shortuuid" in error_str, (
            f"Error must reference blocked golden_repo 'shortuuid', got: {error_str}"
        )

    def test_golden_repo_aliases_list_all_accessible_succeeds(self):
        """
        When all golden_repo_aliases are accessible, composite activation succeeds.
        """
        user = _make_user("dave")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"humanize", "shortuuid", "cidx-meta"},
        )

        arguments = {
            "golden_repo_aliases": ["humanize-global", "shortuuid-global"],
            "user_alias": "my-composite",
        }

        # Must NOT raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="activate_repository",
            access_service=access_service,
        )
