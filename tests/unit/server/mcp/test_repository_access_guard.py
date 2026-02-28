"""Unit tests for the centralized repository access guard.

Tests the _check_repository_access() function in protocol.py which provides
a centralized access control check before any MCP tool handler is invoked.

Story #319: Centralized Repository Access Guard.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock

from code_indexer.server.mcp.protocol import _check_repository_access
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str, role: UserRole = UserRole.NORMAL_USER) -> User:
    """Build a real User object for testing."""
    return User(
        username=username,
        password_hash="irrelevant_hash",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _make_access_service(
    accessible_repos: set,
    is_admin: bool = False,
) -> Mock:
    """Build a mock access_service with configurable accessible repos and admin flag."""
    svc = Mock()
    svc.is_admin_user.return_value = is_admin
    svc.get_accessible_repos.return_value = accessible_repos
    return svc


# ---------------------------------------------------------------------------
# Tests: access denied via different parameter names
# ---------------------------------------------------------------------------


class TestGuardBlocksUnauthorizedAccess:
    """Guard raises ValueError when user has no access to the requested repo."""

    def test_guard_blocks_unauthorized_repository_alias(self):
        """Restricted user with repository_alias targeting inaccessible repo raises ValueError."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos={"allowed-repo"}, is_admin=False)

        with pytest.raises(ValueError, match="Access denied"):
            _check_repository_access(
                arguments={"repository_alias": "secret-repo"},
                effective_user=user,
                tool_name="search_code",
                access_service=svc,
            )

    def test_guard_blocks_unauthorized_alias(self):
        """Restricted user with alias targeting inaccessible repo raises ValueError."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos={"allowed-repo"}, is_admin=False)

        with pytest.raises(ValueError, match="Access denied"):
            _check_repository_access(
                arguments={"alias": "forbidden-repo"},
                effective_user=user,
                tool_name="get_index_status",
                access_service=svc,
            )

    def test_guard_blocks_unauthorized_user_alias(self):
        """Restricted user with user_alias targeting inaccessible repo raises ValueError."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos={"allowed-repo"}, is_admin=False)

        with pytest.raises(ValueError, match="Access denied"):
            _check_repository_access(
                arguments={"user_alias": "private-repo"},
                effective_user=user,
                tool_name="browse_directory",
                access_service=svc,
            )


# ---------------------------------------------------------------------------
# Tests: access allowed
# ---------------------------------------------------------------------------


class TestGuardAllowsAuthorizedAccess:
    """Guard does not raise when user has access or is admin."""

    def test_guard_allows_authorized_repo(self):
        """Restricted user targeting an accessible repo does not raise."""
        user = _make_user("restricted_user")
        svc = _make_access_service(
            accessible_repos={"allowed-repo", "another-repo"}, is_admin=False
        )

        # Must not raise
        _check_repository_access(
            arguments={"repository_alias": "allowed-repo"},
            effective_user=user,
            tool_name="search_code",
            access_service=svc,
        )

    def test_guard_admin_bypasses_check(self):
        """Admin user targeting any repo (even inaccessible) does not raise.

        When is_admin_user returns True, get_accessible_repos is never called.
        """
        user = _make_user("admin_user", role=UserRole.ADMIN)
        svc = _make_access_service(accessible_repos=set(), is_admin=True)

        # Must not raise even though accessible_repos is empty
        _check_repository_access(
            arguments={"repository_alias": "any-repo"},
            effective_user=user,
            tool_name="search_code",
            access_service=svc,
        )

        # Verify admin bypass skipped the repo lookup entirely
        svc.get_accessible_repos.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: skip when no repo parameter
# ---------------------------------------------------------------------------


class TestGuardSkipsWhenNoRepoParam:
    """Guard performs no checks when no repo parameter is present or meaningful."""

    def test_guard_skips_when_no_repo_param(self):
        """Arguments dict with no repo param — no check performed, no calls to service."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos=set(), is_admin=False)

        # Must not raise
        _check_repository_access(
            arguments={"query_text": "some query", "limit": 10},
            effective_user=user,
            tool_name="search_code",
            access_service=svc,
        )

        svc.is_admin_user.assert_not_called()
        svc.get_accessible_repos.assert_not_called()

    def test_guard_skips_when_repo_param_empty_string(self):
        """repository_alias present but empty string — treated as absent, no check."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos=set(), is_admin=False)

        _check_repository_access(
            arguments={"repository_alias": ""},
            effective_user=user,
            tool_name="search_code",
            access_service=svc,
        )

        svc.is_admin_user.assert_not_called()
        svc.get_accessible_repos.assert_not_called()

    def test_guard_skips_when_repo_param_none(self):
        """repository_alias present but None value — treated as absent, no check."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos=set(), is_admin=False)

        _check_repository_access(
            arguments={"repository_alias": None},
            effective_user=user,
            tool_name="search_code",
            access_service=svc,
        )

        svc.is_admin_user.assert_not_called()
        svc.get_accessible_repos.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: -global suffix stripping
# ---------------------------------------------------------------------------


class TestGuardGlobalSuffixStripping:
    """Guard strips the -global suffix before comparing against accessible repos."""

    def test_guard_strips_global_suffix(self):
        """repository_alias with -global suffix is checked against base name."""
        user = _make_user("restricted_user")
        # Accessible repos stored without -global suffix
        svc = _make_access_service(
            accessible_repos={"code-indexer"}, is_admin=False
        )

        # "code-indexer-global" should strip to "code-indexer" — allowed
        _check_repository_access(
            arguments={"repository_alias": "code-indexer-global"},
            effective_user=user,
            tool_name="search_code",
            access_service=svc,
        )

    def test_guard_handles_alias_without_global_suffix(self):
        """repository_alias without -global suffix is checked as-is."""
        user = _make_user("restricted_user")
        svc = _make_access_service(
            accessible_repos={"code-indexer"}, is_admin=False
        )

        # "code-indexer" checked directly — allowed
        _check_repository_access(
            arguments={"repository_alias": "code-indexer"},
            effective_user=user,
            tool_name="search_code",
            access_service=svc,
        )

    def test_guard_strips_global_suffix_before_denying(self):
        """Stripping applies even when denying: uses stripped name in lookup."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos={"other-repo"}, is_admin=False)

        # "forbidden-global" strips to "forbidden" which is not in accessible set
        with pytest.raises(ValueError, match="Access denied"):
            _check_repository_access(
                arguments={"repository_alias": "forbidden-global"},
                effective_user=user,
                tool_name="search_code",
                access_service=svc,
            )


# ---------------------------------------------------------------------------
# Tests: error message content
# ---------------------------------------------------------------------------


class TestGuardErrorMessageContent:
    """Guard error messages contain actionable context."""

    def test_guard_error_message_includes_repo_and_user(self):
        """ValueError message includes both the repo alias and the username."""
        user = _make_user("alice")
        svc = _make_access_service(accessible_repos={"other-repo"}, is_admin=False)

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments={"repository_alias": "secret-repo"},
                effective_user=user,
                tool_name="search_code",
                access_service=svc,
            )

        message = str(exc_info.value)
        assert "secret-repo" in message, f"Repo alias missing from error: {message}"
        assert "alice" in message, f"Username missing from error: {message}"


# ---------------------------------------------------------------------------
# Tests: parameter priority ordering
# ---------------------------------------------------------------------------


class TestGuardParameterPriority:
    """Guard processes parameter names in the order: repository_alias, alias, user_alias."""

    def test_guard_checks_repository_alias_before_alias(self):
        """When both repository_alias and alias are present, repository_alias takes priority."""
        user = _make_user("restricted_user")
        # Only "primary-repo" is accessible; "secondary-repo" is not
        svc = _make_access_service(
            accessible_repos={"primary-repo"}, is_admin=False
        )

        # repository_alias points to allowed repo, alias points to forbidden repo.
        # Since repository_alias is checked first and is allowed, no error raised.
        _check_repository_access(
            arguments={
                "repository_alias": "primary-repo",
                "alias": "forbidden-repo",
            },
            effective_user=user,
            tool_name="search_code",
            access_service=svc,
        )

    def test_guard_falls_through_to_alias_when_repository_alias_absent(self):
        """When repository_alias is absent, alias is used as the repo identifier."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos={"my-repo"}, is_admin=False)

        # No repository_alias key — falls through to alias — allowed
        _check_repository_access(
            arguments={"alias": "my-repo"},
            effective_user=user,
            tool_name="get_index_status",
            access_service=svc,
        )

    def test_guard_falls_through_to_user_alias_when_others_absent(self):
        """When repository_alias and alias are absent, user_alias is used."""
        user = _make_user("restricted_user")
        svc = _make_access_service(accessible_repos={"my-repo"}, is_admin=False)

        # No repository_alias, no alias — falls through to user_alias — allowed
        _check_repository_access(
            arguments={"user_alias": "my-repo"},
            effective_user=user,
            tool_name="browse_directory",
            access_service=svc,
        )
