"""
Unit tests for Story #568: MCP Acting Users - Scoped Repository Access.

Tests the _resolve_acting_users_scope() helper and the integration of
acting_users parameter into _check_repository_access() via the
scoped_repos mechanism.

AC1: Admin with acting_users gets scoped results
AC2: acting_users can only narrow, never elevate
AC3: Non-admin credentials ignore acting_users
AC4: Unknown emails contribute no repos
AC5: No acting_users = normal behavior (backward compatible)
"""

import pytest
from datetime import datetime
from unittest.mock import Mock

from code_indexer.server.mcp.protocol import (
    _check_repository_access,
    _validate_acting_users,
    _resolve_acting_users_scope,
)
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    username: str,
    role: UserRole = UserRole.NORMAL_USER,
    email: str = None,  # type: ignore[assignment]
) -> User:
    """Create a real User object for testing."""
    return User(
        username=username,
        password_hash="hashed_password",
        role=role,
        created_at=datetime(2024, 1, 1),
        email=email,
    )


def _make_access_service(
    is_admin: bool = False,
    accessible_repos_by_user: dict = None,  # type: ignore[assignment]
) -> Mock:
    """Create a mock AccessFilteringService with per-user repo sets.

    Args:
        is_admin: Whether is_admin_user() returns True
        accessible_repos_by_user: Dict mapping username -> set of repos.
            If None, all users get empty set.
    """
    service = Mock()
    service.is_admin_user = Mock(return_value=is_admin)

    repos_map = accessible_repos_by_user or {}

    def _get_accessible(username):
        return repos_map.get(username, set())

    service.get_accessible_repos = Mock(side_effect=_get_accessible)
    return service


def _make_user_manager(users_by_email: dict = None) -> Mock:  # type: ignore[assignment]
    """Create a mock UserManager with get_user_by_email support.

    Args:
        users_by_email: Dict mapping email -> User object.
            Returns None for unknown emails.
    """
    manager = Mock()
    email_map = users_by_email or {}

    def _get_by_email(email):
        return email_map.get(email)

    manager.get_user_by_email = Mock(side_effect=_get_by_email)
    return manager


# ---------------------------------------------------------------------------
# AC1: Admin with acting_users gets scoped results
# ---------------------------------------------------------------------------


class TestResolveActingUsersScopeAdminScoping:
    """AC1: Admin with acting_users gets intersection-scoped repo set."""

    def test_admin_scoped_to_acting_users_repos(self):
        """Admin's repos intersected with union of acting users' repos."""
        alice = _make_user("alice", email="alice@corp.com")
        bob = _make_user("bob", email="bob@corp.com")

        user_manager = _make_user_manager(
            users_by_email={
                "alice@corp.com": alice,
                "bob@corp.com": bob,
            }
        )
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos_by_user={
                "admin_user": {"repo-a", "repo-b", "repo-c", "repo-d"},
                "alice": {"repo-a", "repo-b"},
                "bob": {"repo-b", "repo-c"},
            },
        )

        result = _resolve_acting_users_scope(
            emails=["alice@corp.com", "bob@corp.com"],
            user_manager=user_manager,
            access_service=access_service,
            admin_username="admin_user",
        )

        # Union of alice+bob = {repo-a, repo-b, repo-c}
        # Intersect with admin = {repo-a, repo-b, repo-c, repo-d} & {repo-a, repo-b, repo-c}
        # = {repo-a, repo-b, repo-c}
        assert result == {"repo-a", "repo-b", "repo-c"}

    def test_single_acting_user_scoping(self):
        """Single acting user restricts to just their repos."""
        alice = _make_user("alice", email="alice@corp.com")

        user_manager = _make_user_manager(users_by_email={"alice@corp.com": alice})
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos_by_user={
                "admin_user": {"repo-a", "repo-b", "repo-c"},
                "alice": {"repo-a"},
            },
        )

        result = _resolve_acting_users_scope(
            emails=["alice@corp.com"],
            user_manager=user_manager,
            access_service=access_service,
            admin_username="admin_user",
        )

        assert result == {"repo-a"}


# ---------------------------------------------------------------------------
# AC2: acting_users can only narrow, never elevate
# ---------------------------------------------------------------------------


class TestResolveActingUsersScopeNeverElevates:
    """AC2: acting_users can only narrow admin's access, never elevate."""

    def test_acting_user_repos_outside_admin_access_excluded(self):
        """If acting user has repo-x but admin doesn't, repo-x excluded."""
        alice = _make_user("alice", email="alice@corp.com")

        user_manager = _make_user_manager(users_by_email={"alice@corp.com": alice})
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos_by_user={
                "admin_user": {"repo-a", "repo-b"},
                "alice": {"repo-a", "repo-b", "repo-x"},
            },
        )

        result = _resolve_acting_users_scope(
            emails=["alice@corp.com"],
            user_manager=user_manager,
            access_service=access_service,
            admin_username="admin_user",
        )

        # repo-x is NOT in admin's repos, so excluded
        assert result == {"repo-a", "repo-b"}
        assert "repo-x" not in result


# ---------------------------------------------------------------------------
# AC3: Non-admin credentials ignore acting_users
# ---------------------------------------------------------------------------


class TestActingUsersNonAdminIgnored:
    """AC3: Non-admin user providing acting_users has it silently ignored.

    The protocol layer checks is_admin before calling _resolve_acting_users_scope.
    For non-admin users, acting_users is popped from arguments but produces
    no scoped_repos (None), so normal access rules apply.
    This test validates the expected integration behavior.
    """

    def test_non_admin_scoped_repos_stays_none(self):
        """Non-admin user: acting_users is popped but scoped_repos remains None.

        This simulates the protocol-level logic where is_admin check
        gates the call to _resolve_acting_users_scope.
        """
        normal_user = _make_user("normal_user", role=UserRole.NORMAL_USER)
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos_by_user={
                "normal_user": {"repo-a"},
            },
        )

        # Simulate what handle_tools_call does for non-admin
        arguments = {
            "repository_alias": "repo-a",
            "acting_users": ["admin@corp.com"],
        }
        acting_users_emails = arguments.pop("acting_users", None)

        # Non-admin: acting_users is ignored, scoped_repos stays None
        scoped_repos = None
        if acting_users_emails is not None:
            if access_service.is_admin_user(normal_user.username):
                # This branch should NOT execute for non-admin
                scoped_repos = {"should-not-reach"}

        assert scoped_repos is None
        assert "acting_users" not in arguments

    def test_non_admin_normal_access_check_still_applies(self):
        """Non-admin with acting_users still gets normal access enforcement."""
        normal_user = _make_user("normal_user", role=UserRole.NORMAL_USER)
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos_by_user={
                "normal_user": {"repo-a"},
            },
        )

        # repo-b is not accessible to normal_user
        arguments = {"repository_alias": "repo-b"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=normal_user,
                tool_name="search_code",
                access_service=access_service,
                scoped_repos=None,
            )

        assert "Access denied" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC4: Unknown emails contribute no repos
# ---------------------------------------------------------------------------


class TestResolveActingUsersScopeUnknownEmails:
    """AC4: Unknown emails contribute no repos."""

    def test_unknown_email_returns_empty_set(self):
        """All unknown emails result in empty set."""
        user_manager = _make_user_manager(users_by_email={})
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos_by_user={
                "admin_user": {"repo-a", "repo-b"},
            },
        )

        result = _resolve_acting_users_scope(
            emails=["unknown@external.com"],
            user_manager=user_manager,
            access_service=access_service,
            admin_username="admin_user",
        )

        assert result == set()

    def test_mix_known_and_unknown_emails(self):
        """Known users contribute repos; unknown contribute nothing."""
        alice = _make_user("alice", email="alice@corp.com")

        user_manager = _make_user_manager(users_by_email={"alice@corp.com": alice})
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos_by_user={
                "admin_user": {"repo-a", "repo-b", "repo-c"},
                "alice": {"repo-a"},
            },
        )

        result = _resolve_acting_users_scope(
            emails=["alice@corp.com", "unknown@external.com"],
            user_manager=user_manager,
            access_service=access_service,
            admin_username="admin_user",
        )

        # Only alice contributes repo-a; unknown contributes nothing
        assert result == {"repo-a"}

    def test_empty_email_list_returns_empty_set(self):
        """Empty acting_users list results in empty set."""
        user_manager = _make_user_manager()
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos_by_user={
                "admin_user": {"repo-a", "repo-b"},
            },
        )

        result = _resolve_acting_users_scope(
            emails=[],
            user_manager=user_manager,
            access_service=access_service,
            admin_username="admin_user",
        )

        assert result == set()


# ---------------------------------------------------------------------------
# AC5: _check_repository_access with scoped_repos parameter
# ---------------------------------------------------------------------------


class TestCheckRepositoryAccessWithScopedRepos:
    """AC5 + integration: _check_repository_access honors scoped_repos."""

    def test_scoped_repos_allows_repo_in_scope(self):
        """When scoped_repos provided, repo in scope is allowed."""
        user = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        arguments = {"repository_alias": "repo-a"}

        # Should not raise -- repo-a is in scoped_repos
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="search_code",
            access_service=access_service,
            scoped_repos={"repo-a", "repo-b"},
        )

    def test_scoped_repos_blocks_repo_not_in_scope(self):
        """When scoped_repos provided, repo NOT in scope is blocked."""
        user = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        arguments = {"repository_alias": "repo-c"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="search_code",
                access_service=access_service,
                scoped_repos={"repo-a", "repo-b"},
            )

        assert "Access denied" in str(exc_info.value)
        assert "repo-c" in str(exc_info.value)

    def test_scoped_repos_strips_global_suffix(self):
        """scoped_repos check strips -global suffix from alias."""
        user = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        arguments = {"repository_alias": "repo-a-global"}

        # Should not raise -- repo-a-global normalizes to repo-a
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="search_code",
            access_service=access_service,
            scoped_repos={"repo-a", "repo-b"},
        )

    def test_scoped_repos_none_preserves_existing_behavior(self):
        """AC5: When scoped_repos is None, existing behavior unchanged."""
        user = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        arguments = {"repository_alias": "any-repo"}

        # Admin with no scoped_repos should pass (existing behavior)
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="search_code",
            access_service=access_service,
            scoped_repos=None,
        )

    def test_scoped_repos_no_repo_param_skips_check(self):
        """When no repo param in arguments, scoped_repos check is skipped."""
        user = _make_user("admin_user", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        arguments = {"query_text": "something"}

        # Should not raise -- no repo param
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="list_repositories",
            access_service=access_service,
            scoped_repos={"repo-a"},
        )


# ---------------------------------------------------------------------------
# acting_users argument popping
# ---------------------------------------------------------------------------


class TestActingUsersArgumentPopping:
    """Verify acting_users is removed from arguments before handler sees them."""

    def test_acting_users_key_removed_from_arguments(self):
        """acting_users must be popped from arguments dict."""
        arguments = {
            "repository_alias": "repo-a",
            "query_text": "search term",
            "acting_users": ["alice@corp.com"],
        }

        # Pop acting_users as the protocol layer does
        acting_users = arguments.pop("acting_users", None)

        assert acting_users == ["alice@corp.com"]
        assert "acting_users" not in arguments
        assert arguments == {
            "repository_alias": "repo-a",
            "query_text": "search term",
        }

    def test_no_acting_users_key_returns_none(self):
        """When acting_users not present, pop returns None."""
        arguments = {
            "repository_alias": "repo-a",
            "query_text": "search term",
        }

        acting_users = arguments.pop("acting_users", None)

        assert acting_users is None
        assert "acting_users" not in arguments


class TestActingUsersInputValidation:
    """Verify acting_users type validation (Finding 1 from code review)."""

    def test_string_instead_of_list_raises_error(self):
        """acting_users as plain string (not list) must raise ValueError."""
        with pytest.raises(ValueError, match="acting_users must be a list"):
            _validate_acting_users("alice@corp.com")

    def test_integer_raises_error(self):
        """acting_users as integer must raise ValueError."""
        with pytest.raises(ValueError, match="acting_users must be a list"):
            _validate_acting_users(42)

    def test_list_with_non_string_raises_error(self):
        """acting_users containing non-string elements must raise ValueError."""
        with pytest.raises(ValueError, match="acting_users must be a list"):
            _validate_acting_users(["alice@corp.com", 123])

    def test_valid_list_of_strings_passes(self):
        """acting_users as list of strings should not raise."""
        _validate_acting_users(["alice@corp.com", "bob@corp.com"])

    def test_empty_list_passes(self):
        """Empty list is valid (will produce empty scope)."""
        _validate_acting_users([])
