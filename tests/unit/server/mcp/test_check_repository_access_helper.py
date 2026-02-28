"""
Unit tests for _check_repository_access() helper function (Story #319).

Verifies the centralized repository access guard helper that is called
from handle_tools_call() to automatically protect MCP tools accepting
repository-identifying parameters.

AC1: All tools with repository_alias/alias/user_alias are automatically checked
AC2: Access denied returns clear error message
AC3: Admin users bypass the check
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
    """Create a real User object for testing."""
    return User(
        username=username,
        password_hash="hashed_password",
        role=role,
        created_at=datetime(2024, 1, 1),
    )


def _make_access_service(
    is_admin: bool = False,
    accessible_repos: set = None,
) -> Mock:
    """Create a mock AccessFilteringService."""
    service = Mock()
    service.is_admin_user = Mock(return_value=is_admin)
    service.get_accessible_repos = Mock(
        return_value=accessible_repos if accessible_repos is not None else set()
    )
    return service


# ---------------------------------------------------------------------------
# Blocking: guard raises ValueError for unauthorized repos
# ---------------------------------------------------------------------------


class TestCheckRepositoryAccessBlocking:
    """AC1, AC2: Guard blocks access to unauthorized repositories."""

    def test_blocks_access_with_repository_alias_param(self):
        """
        AC1: Tool with repository_alias targeting inaccessible repo raises ValueError.

        Regular user does NOT have access to 'secret-repo'.
        Expect ValueError with 'Access denied' message.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        arguments = {"repository_alias": "secret-repo", "query_text": "something"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="search_code",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "secret-repo" in error_str
        assert "regularuser" in error_str

    def test_blocks_access_with_alias_param(self):
        """
        AC1: Tool with 'alias' param (admin tools like add_golden_repo)
        targeting inaccessible repo raises ValueError.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        arguments = {"alias": "restricted-golden-repo"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="add_golden_repo",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "restricted-golden-repo" in error_str

    def test_blocks_access_with_user_alias_param(self):
        """
        AC1: Tool with 'user_alias' param (get_repository_status)
        targeting inaccessible repo raises ValueError.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        arguments = {"user_alias": "hidden-repo"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="get_repository_status",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "hidden-repo" in error_str

    def test_error_message_names_both_repo_and_user(self):
        """
        AC2: The error message must be clear about which repo and which user.
        Not a vague or empty message.
        """
        user = _make_user("alice")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        arguments = {"repository_alias": "bob-private-repo"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="browse_directory",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "bob-private-repo" in error_str
        assert "alice" in error_str


# ---------------------------------------------------------------------------
# Allowing: guard passes for authorized repos
# ---------------------------------------------------------------------------


class TestCheckRepositoryAccessAllowing:
    """Guard allows access to authorized repositories."""

    def test_allows_access_to_authorized_repo_via_repository_alias(self):
        """Regular user with access to repo does NOT raise ValueError."""
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        arguments = {"repository_alias": "allowed-repo", "query_text": "something"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="search_code",
            access_service=access_service,
        )

    def test_allows_access_to_authorized_repo_via_alias_param(self):
        """Regular user with access to a repo via 'alias' param does not raise."""
        user = _make_user("poweruser", role=UserRole.POWER_USER)
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "my-golden-repo"},
        )

        arguments = {"alias": "my-golden-repo"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="global_repo_status",
            access_service=access_service,
        )

    def test_allows_cidx_meta_access(self):
        """cidx-meta is always accessible when service includes it in accessible_repos."""
        user = _make_user("minimaluser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        arguments = {"repository_alias": "cidx-meta"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="search_code",
            access_service=access_service,
        )


# ---------------------------------------------------------------------------
# Admin bypass: AC3
# ---------------------------------------------------------------------------


class TestCheckRepositoryAccessAdminBypass:
    """AC3: Admin users bypass the check entirely."""

    def test_admin_bypasses_guard_for_any_repo(self):
        """
        AC3: Admin users can access any repo without access check.
        get_accessible_repos should NOT be called.
        """
        user = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos=set(),  # Empty - would block if checked
        )

        arguments = {"repository_alias": "any-repo-whatsoever"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="browse_directory",
            access_service=access_service,
        )

        # Admin bypass must NOT call get_accessible_repos
        access_service.get_accessible_repos.assert_not_called()

    def test_admin_bypass_works_with_alias_param(self):
        """AC3: Admin bypass works for 'alias' parameter."""
        user = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        arguments = {"alias": "secret-golden-repo"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="add_golden_repo",
            access_service=access_service,
        )

    def test_admin_bypass_works_with_user_alias_param(self):
        """AC3: Admin bypass works for 'user_alias' parameter."""
        user = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)

        arguments = {"user_alias": "any-activated-repo"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="get_repository_status",
            access_service=access_service,
        )


# ---------------------------------------------------------------------------
# Skip cases: no repo param or empty param
# ---------------------------------------------------------------------------


class TestCheckRepositoryAccessSkipCases:
    """Guard skips check when no repo param is present or param is empty."""

    def test_skips_when_no_repo_param_in_arguments(self):
        """Guard does nothing when no repo identifier in arguments."""
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),  # Would block everything if checked
        )

        arguments = {"some_other_param": "value", "limit": 10}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="list_global_repos",
            access_service=access_service,
        )

        # No repo param found - service methods should not be called
        access_service.is_admin_user.assert_not_called()
        access_service.get_accessible_repos.assert_not_called()

    def test_skips_when_repository_alias_is_empty_string(self):
        """Guard skips check when repository_alias is empty string."""
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),
        )

        arguments = {"repository_alias": ""}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="search_code",
            access_service=access_service,
        )

    def test_skips_when_repository_alias_is_none(self):
        """Guard skips check when repository_alias is None."""
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),
        )

        arguments = {"repository_alias": None}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="search_code",
            access_service=access_service,
        )

    def test_skips_when_arguments_is_empty_dict(self):
        """Guard skips check when arguments dict is empty."""
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),
        )

        # Must not raise
        _check_repository_access(
            arguments={},
            effective_user=user,
            tool_name="check_hnsw_health",
            access_service=access_service,
        )


# ---------------------------------------------------------------------------
# Global suffix stripping
# ---------------------------------------------------------------------------


class TestCheckRepositoryAccessGlobalSuffixStripping:
    """Guard strips -global suffix before checking accessible repos."""

    def test_strips_global_suffix_from_repository_alias(self):
        """
        'code-indexer-global' is checked as 'code-indexer'.
        The -global suffix is an alias convention, not the stored repo name.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "code-indexer"},  # Without -global suffix
        )

        # Pass alias WITH -global suffix (as MCP tools receive them)
        arguments = {"repository_alias": "code-indexer-global"}

        # Must not raise - 'code-indexer-global' should match 'code-indexer'
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="browse_directory",
            access_service=access_service,
        )

    def test_blocked_after_global_suffix_stripped_if_not_accessible(self):
        """After stripping -global, if repo still not in accessible set, block it."""
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},  # 'secret-repo' not included
        )

        arguments = {"repository_alias": "secret-repo-global"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="search_code",
                access_service=access_service,
            )

        # Error message should reference the original alias (with -global)
        assert "secret-repo-global" in str(exc_info.value)

    def test_strips_global_suffix_from_alias_param(self):
        """'alias' param with -global suffix is also stripped for lookup."""
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "my-repo"},
        )

        arguments = {"alias": "my-repo-global"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="global_repo_status",
            access_service=access_service,
        )


# ---------------------------------------------------------------------------
# Impersonation: guard uses effective_user
# ---------------------------------------------------------------------------


class TestCheckRepositoryAccessImpersonation:
    """Guard uses effective_user (the impersonated user) for access checks."""

    def test_uses_effective_user_username_for_is_admin_check(self):
        """
        When impersonation is active, guard checks the impersonated user's access.
        The effective_user passed to _check_repository_access IS the impersonated user.
        We verify the service is called with effective_user.username.
        """
        impersonated_user = _make_user("limited-user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        arguments = {"repository_alias": "admin-only-repo"}

        with pytest.raises(ValueError):
            _check_repository_access(
                arguments=arguments,
                effective_user=impersonated_user,
                tool_name="browse_directory",
                access_service=access_service,
            )

        # Verify the service was called with the impersonated user's username
        access_service.is_admin_user.assert_called_once_with("limited-user")

    def test_impersonated_limited_user_cannot_access_restricted_repo(self):
        """
        When admin impersonates a limited user, that limited user's restrictions apply.
        The effective_user is the limited user - they should be blocked.
        """
        effective_user = _make_user("limited-user")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        arguments = {"repository_alias": "restricted-repo"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=effective_user,
                tool_name="search_code",
                access_service=access_service,
            )

        assert "limited-user" in str(exc_info.value)

    def test_service_call_ordering_non_admin(self):
        """
        Guard calls is_admin_user first; get_accessible_repos only called if not admin.
        Both are called for non-admin user with repo param.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "test-repo"},
        )

        arguments = {"repository_alias": "test-repo"}

        _check_repository_access(
            arguments=arguments,
            effective_user=user,
            tool_name="git_log",
            access_service=access_service,
        )

        access_service.is_admin_user.assert_called_once_with("regularuser")
        access_service.get_accessible_repos.assert_called_once_with("regularuser")
