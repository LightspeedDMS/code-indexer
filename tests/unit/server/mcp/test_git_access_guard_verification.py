"""Verification tests confirming 8 git operation tools are protected by the centralized
access guard.

Story #317: Verify Git Operation Tools Protected by Centralized Guard.

Story #319 implemented _check_repository_access() in protocol.py.  This file
verifies that the 8 git tools whose handlers accept repository_alias are
correctly wired into that guard — both at the schema level (TOOL_REGISTRY
declares repository_alias) and at the function level (_check_repository_access
blocks unauthorized callers).

Tools under verification:
  git_log, git_diff, git_blame, git_file_history,
  git_search_commits, git_search_diffs, git_show_commit, git_file_at_revision
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock

from code_indexer.server.mcp.protocol import _check_repository_access
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GIT_TOOLS_WITH_REPO_ALIAS = [
    "git_log",
    "git_diff",
    "git_blame",
    "git_file_history",
    "git_search_commits",
    "git_search_diffs",
    "git_show_commit",
    "git_file_at_revision",
]


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
# Test 1: Structural verification — all 8 git tools declare repository_alias
# ---------------------------------------------------------------------------


class TestGitToolsHaveRepositoryAliasInSchema:
    """All 8 git operation tools must declare repository_alias in their inputSchema."""

    def test_all_git_tools_have_repository_alias_property(self):
        """Every git operation tool must expose repository_alias in its inputSchema
        properties so the centralized guard can detect and enforce access control."""
        missing = []
        for tool_name in GIT_TOOLS_WITH_REPO_ALIAS:
            assert tool_name in TOOL_REGISTRY, (
                f"Tool '{tool_name}' not found in TOOL_REGISTRY"
            )
            tool_def = TOOL_REGISTRY[tool_name]
            input_schema = tool_def.get("inputSchema", {})
            properties = input_schema.get("properties", {})
            if "repository_alias" not in properties:
                missing.append(tool_name)

        assert missing == [], (
            f"The following git tools are missing 'repository_alias' in their "
            f"inputSchema properties: {missing}.  The centralized guard "
            f"(_check_repository_access) looks for this parameter by name — "
            f"without it the guard cannot enforce access control for these tools."
        )


# ---------------------------------------------------------------------------
# Tests 2–9: Guard integration — one test per git tool
# ---------------------------------------------------------------------------


class TestGitToolsGuardBlocksUnauthorizedAccess:
    """Calling _check_repository_access() with arguments typical for each git
    tool must raise ValueError when the user has no access to the requested repo.

    These tests call the guard function directly (no handler invocation) to
    confirm that the guard itself works correctly for each tool's argument shape.
    """

    def _assert_guard_blocks(self, tool_name: str, arguments: dict) -> None:
        """Helper: confirm guard raises ValueError for a non-admin restricted user."""
        user = _make_user("restricted_user")
        svc = _make_access_service(
            accessible_repos={"allowed-repo"},
            is_admin=False,
        )
        with pytest.raises(
            ValueError,
            match="Access denied",
        ):
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name=tool_name,
                access_service=svc,
            )

    def test_git_log_blocks_unauthorized_access(self):
        """git_log: guard blocks user without access to target repository."""
        self._assert_guard_blocks(
            tool_name="git_log",
            arguments={"repository_alias": "secret-repo-global"},
        )

    def test_git_diff_blocks_unauthorized_access(self):
        """git_diff: guard blocks user without access to target repository."""
        self._assert_guard_blocks(
            tool_name="git_diff",
            arguments={
                "repository_alias": "secret-repo-global",
                "from_revision": "HEAD~1",
            },
        )

    def test_git_blame_blocks_unauthorized_access(self):
        """git_blame: guard blocks user without access to target repository."""
        self._assert_guard_blocks(
            tool_name="git_blame",
            arguments={
                "repository_alias": "secret-repo-global",
                "path": "src/main.py",
            },
        )

    def test_git_file_history_blocks_unauthorized_access(self):
        """git_file_history: guard blocks user without access to target repository."""
        self._assert_guard_blocks(
            tool_name="git_file_history",
            arguments={
                "repository_alias": "secret-repo-global",
                "path": "src/auth.py",
            },
        )

    def test_git_search_commits_blocks_unauthorized_access(self):
        """git_search_commits: guard blocks user without access to target repository."""
        self._assert_guard_blocks(
            tool_name="git_search_commits",
            arguments={
                "repository_alias": "secret-repo-global",
                "query": "fix bug",
            },
        )

    def test_git_search_diffs_blocks_unauthorized_access(self):
        """git_search_diffs: guard blocks user without access to target repository."""
        self._assert_guard_blocks(
            tool_name="git_search_diffs",
            arguments={
                "repository_alias": "secret-repo-global",
                "search_string": "calculateTotal",
            },
        )

    def test_git_show_commit_blocks_unauthorized_access(self):
        """git_show_commit: guard blocks user without access to target repository."""
        self._assert_guard_blocks(
            tool_name="git_show_commit",
            arguments={
                "repository_alias": "secret-repo-global",
                "commit_hash": "abc1234",
            },
        )

    def test_git_file_at_revision_blocks_unauthorized_access(self):
        """git_file_at_revision: guard blocks user without access to target repository."""
        self._assert_guard_blocks(
            tool_name="git_file_at_revision",
            arguments={
                "repository_alias": "secret-repo-global",
                "path": "src/utils.py",
                "revision": "HEAD~3",
            },
        )


# ---------------------------------------------------------------------------
# Test 10: Admin bypass — admin user passes through the guard for all git tools
# ---------------------------------------------------------------------------


class TestAdminBypassForGitTools:
    """Admin users must bypass the access guard for all git operation tools."""

    def test_admin_can_access_any_repo_for_all_git_tools(self):
        """An admin user must not be blocked by the guard for any of the 8 git tools,
        even when accessible_repos is empty (would block any non-admin)."""
        admin = _make_user("admin_user", role=UserRole.ADMIN)
        svc = _make_access_service(
            accessible_repos=set(),  # Empty — would block non-admin
            is_admin=True,
        )

        # Typical arguments per tool — all target the same restricted repo
        tool_arguments = {
            "git_log": {"repository_alias": "any-secret-repo-global"},
            "git_diff": {
                "repository_alias": "any-secret-repo-global",
                "from_revision": "HEAD~1",
            },
            "git_blame": {
                "repository_alias": "any-secret-repo-global",
                "path": "src/main.py",
            },
            "git_file_history": {
                "repository_alias": "any-secret-repo-global",
                "path": "src/auth.py",
            },
            "git_search_commits": {
                "repository_alias": "any-secret-repo-global",
                "query": "fix",
            },
            "git_search_diffs": {
                "repository_alias": "any-secret-repo-global",
                "search_string": "calculateTotal",
            },
            "git_show_commit": {
                "repository_alias": "any-secret-repo-global",
                "commit_hash": "abc1234",
            },
            "git_file_at_revision": {
                "repository_alias": "any-secret-repo-global",
                "path": "src/utils.py",
                "revision": "HEAD",
            },
        }

        for tool_name in GIT_TOOLS_WITH_REPO_ALIAS:
            arguments = tool_arguments[tool_name]
            # Must not raise for admin
            _check_repository_access(
                arguments=arguments,
                effective_user=admin,
                tool_name=tool_name,
                access_service=svc,
            )

        # get_accessible_repos must never be called for an admin user
        svc.get_accessible_repos.assert_not_called()
