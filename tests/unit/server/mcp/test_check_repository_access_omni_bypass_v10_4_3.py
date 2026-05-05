"""
Unit tests for _check_repository_access() — v10.4.3 omni-bypass security fix.

Finding 1 (HIGH-SEVERITY): When repository_alias, alias, user_alias, or
repo_alias is supplied as a NATIVE Python list (omni multi-repo form), the
for-loop in _check_repository_access only matches `isinstance(value, str)`,
so list-form values fall through with raw_alias=None and the function returns
without checking access. This means a non-admin caller can pass
repository_alias=['allowed', 'denied'] and access 'denied' unchecked.

Also covers JSON-encoded string arrays like '["allowed", "denied"]' which
_parse_json_string_array decodes to lists before passing to the handler, but
the decoded list was never passed back to the access guard.

These tests verify that EVERY alias in a list-form repository_alias is checked
and that the first inaccessible alias raises ValueError.
"""

from __future__ import annotations

import pytest
from datetime import datetime
from unittest.mock import Mock

from code_indexer.server.mcp.protocol import _check_repository_access
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers — reuse same pattern as test_check_repository_access_helper.py
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
    accessible_repos: set = None,  # type: ignore[assignment]
) -> Mock:
    """Create a mock AccessFilteringService."""
    service = Mock()
    service.is_admin_user = Mock(return_value=is_admin)
    service.get_accessible_repos = Mock(
        return_value=accessible_repos if accessible_repos is not None else set()
    )
    return service


# ---------------------------------------------------------------------------
# Native list — repository_alias as Python list
# ---------------------------------------------------------------------------


class TestNativeListRepositoryAliasAccess:
    """AC: native list in repository_alias must be checked entry by entry."""

    def test_non_admin_native_list_with_denied_repo_raises(self):
        """
        Non-admin user passes repository_alias=['allowed-repo', 'denied-repo'].
        The guard MUST raise ValueError mentioning 'denied-repo'.

        This was the staging bypass: list-form fell through isinstance(value, str)
        check with raw_alias=None and returned without checking.
        """
        user = _make_user("seba")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments={"repository_alias": ["allowed-repo", "denied-repo"]},
                effective_user=user,
                tool_name="xray_search",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "denied-repo" in error_str
        assert "Access denied" in error_str

    def test_non_admin_native_list_all_allowed_does_not_raise(self):
        """
        Non-admin user passes repository_alias=['allowed-a', 'allowed-b'].
        Both are accessible — guard must NOT raise.
        """
        user = _make_user("seba")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-a", "allowed-b"},
        )

        # Must not raise
        _check_repository_access(
            arguments={"repository_alias": ["allowed-a", "allowed-b"]},
            effective_user=user,
            tool_name="xray_search",
            access_service=access_service,
        )

    def test_non_admin_native_list_first_denied_raises_immediately(self):
        """
        When the first entry is denied, ValueError must be raised immediately.
        The error message must name the denied alias.
        """
        user = _make_user("seba")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments={"repository_alias": ["shortuuid-global", "cidx-meta"]},
                effective_user=user,
                tool_name="xray_search",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert (
            "shortuuid" in error_str
        )  # -global suffix stripped for lookup but raw kept in msg

    def test_admin_user_native_list_with_denied_entries_does_not_raise(self):
        """
        Admin users bypass the check entirely — even for multi-alias list form.
        get_accessible_repos must NOT be called.
        """
        user = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos=set(),  # Would block everything if checked
        )

        # Must not raise
        _check_repository_access(
            arguments={"repository_alias": ["denied-a", "denied-b", "denied-c"]},
            effective_user=user,
            tool_name="xray_search",
            access_service=access_service,
        )

        access_service.get_accessible_repos.assert_not_called()

    def test_empty_list_is_treated_as_absent_no_exception(self):
        """
        An empty list repository_alias behaves like absent — guard skips check.
        Consistent with existing empty-string behavior.
        """
        user = _make_user("seba")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),
        )

        # Must not raise
        _check_repository_access(
            arguments={"repository_alias": []},
            effective_user=user,
            tool_name="xray_search",
            access_service=access_service,
        )

    def test_list_with_non_string_entries_skips_non_strings(self):
        """
        A list containing non-string entries (e.g. integers) must skip those.
        String entries are still checked. Deny applies to denied string entries.
        """
        user = _make_user("seba")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments={
                    "repository_alias": [42, "denied-repo", None, "allowed-repo"]
                },
                effective_user=user,
                tool_name="xray_search",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "denied-repo" in error_str

    def test_list_with_only_non_string_entries_skips_all(self):
        """
        A list containing only non-string entries — no string to check.
        Guard treats this as absent; no exception raised.
        """
        user = _make_user("seba")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),
        )

        # Must not raise — no valid strings to check
        _check_repository_access(
            arguments={"repository_alias": [42, None, True]},
            effective_user=user,
            tool_name="xray_search",
            access_service=access_service,
        )

    def test_global_suffix_stripped_from_each_list_entry(self):
        """
        The -global suffix must be stripped from each list entry before lookup.
        'shortuuid-global' → checked as 'shortuuid' against accessible set.
        If 'shortuuid' not in accessible, the error names 'shortuuid-global'.
        """
        user = _make_user("seba")
        # Only cidx-meta accessible; NOT shortuuid
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments={"repository_alias": ["cidx-meta", "shortuuid-global"]},
                effective_user=user,
                tool_name="xray_search",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        # The raw alias (with -global) should appear in the error message
        assert "shortuuid-global" in error_str


# ---------------------------------------------------------------------------
# JSON-encoded string array — repository_alias as '["a", "b"]' string
# ---------------------------------------------------------------------------


class TestJsonEncodedArrayRepositoryAliasAccess:
    """AC: JSON-encoded array string in repository_alias must be decoded and checked."""

    def test_json_encoded_array_with_denied_raises(self):
        """
        Non-admin passes repository_alias='["allowed", "denied"]' as a JSON string.
        The access guard must decode it and deny access to 'denied'.

        This mirrors what _parse_json_string_array does in the handler — the guard
        must also handle JSON-encoded arrays passed directly as argument values.
        """
        user = _make_user("seba")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed"},
        )

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments={"repository_alias": '["allowed", "denied"]'},
                effective_user=user,
                tool_name="xray_search",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "denied" in error_str
        assert "Access denied" in error_str

    def test_json_encoded_array_all_allowed_does_not_raise(self):
        """
        Non-admin passes repository_alias='["allowed-a", "allowed-b"]'.
        All accessible — guard must NOT raise.
        """
        user = _make_user("seba")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-a", "allowed-b"},
        )

        # Must not raise
        _check_repository_access(
            arguments={"repository_alias": '["allowed-a", "allowed-b"]'},
            effective_user=user,
            tool_name="xray_search",
            access_service=access_service,
        )


# ---------------------------------------------------------------------------
# scoped_repos interaction with list form
# ---------------------------------------------------------------------------


class TestNativeListWithScopedRepos:
    """AC: scoped_repos path must also check native list entries."""

    def test_scoped_repos_denies_entry_not_in_scope(self):
        """
        When scoped_repos is active, any list entry outside the scope raises
        ValueError with the scoped-deny message.
        """
        user = _make_user("seba")
        access_service = _make_access_service(is_admin=False)
        scoped = {"cidx-meta", "allowed-in-scope"}

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments={"repository_alias": ["allowed-in-scope", "outside-scope"]},
                effective_user=user,
                tool_name="xray_search",
                access_service=access_service,
                scoped_repos=scoped,
            )

        error_str = str(exc_info.value)
        assert "outside-scope" in error_str
        assert "acting users" in error_str

    def test_scoped_repos_allows_all_entries_in_scope(self):
        """
        When scoped_repos is active and all list entries are in scope — no exception.
        """
        user = _make_user("seba")
        access_service = _make_access_service(is_admin=False)
        scoped = {"cidx-meta", "repo-a", "repo-b"}

        # Must not raise
        _check_repository_access(
            arguments={"repository_alias": ["repo-a", "repo-b"]},
            effective_user=user,
            tool_name="xray_search",
            access_service=access_service,
            scoped_repos=scoped,
        )

    def test_scoped_repos_overrides_admin_bypass_for_list(self):
        """
        scoped_repos takes precedence over admin bypass for list-form aliases too.
        Admin in scoped context is still subject to the scope restriction.
        """
        user = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(is_admin=True)
        scoped = {"cidx-meta"}  # Very narrow scope

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments={"repository_alias": ["cidx-meta", "outside-scope"]},
                effective_user=user,
                tool_name="xray_search",
                access_service=access_service,
                scoped_repos=scoped,
            )

        error_str = str(exc_info.value)
        assert "outside-scope" in error_str
