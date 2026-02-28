"""
Story #315: Verify File Access Tools Protected by Centralized Guard.

This is a verify-only test file. It does NOT modify any production code.
It confirms that the 4 file access tools are protected by the centralized
access guard implemented in Story #319.

The centralized guard lives in protocol.py _check_repository_access() and is
called from handle_tools_call() before any tool handler is invoked.

Tools verified:
- browse_directory     (repository_alias parameter)
- get_file_content     (repository_alias parameter)
- list_files           (repository_alias parameter)
- directory_tree       (repository_alias parameter)
"""

import pytest
from datetime import datetime
from unittest.mock import Mock

from code_indexer.server.mcp.protocol import _check_repository_access
from code_indexer.server.mcp.tools import TOOL_REGISTRY
from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Constants: the 4 file access tools under verification
# ---------------------------------------------------------------------------

FILE_ACCESS_TOOLS = [
    "browse_directory",
    "get_file_content",
    "list_files",
    "directory_tree",
]


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
    """Create a mock AccessFilteringService with configurable access."""
    service = Mock()
    service.is_admin_user = Mock(return_value=is_admin)
    service.get_accessible_repos = Mock(
        return_value=accessible_repos if accessible_repos is not None else set()
    )
    return service


# ---------------------------------------------------------------------------
# Structural verification: TOOL_REGISTRY inputSchema properties
# ---------------------------------------------------------------------------


class TestFileAccessToolsHaveRepositoryAliasInSchema:
    """
    Structural verification that each of the 4 file access tools declares
    'repository_alias' in their TOOL_REGISTRY inputSchema properties.

    This ensures the guard will catch them: the guard extracts the repo
    identifier from the arguments dict using the parameter names defined
    in inputSchema. If repository_alias is absent from the schema, the
    tool is not self-documenting about what it accepts.

    The guard operates on actual arguments passed at runtime - so the
    structural check is a canary: if someone renames the param in the
    schema and the handler, the guard will silently stop protecting the tool.
    """

    def test_all_four_file_access_tools_present_in_tool_registry(self):
        """TOOL_REGISTRY contains all 4 file access tools."""
        for tool_name in FILE_ACCESS_TOOLS:
            assert tool_name in TOOL_REGISTRY, (
                f"Tool '{tool_name}' not found in TOOL_REGISTRY. "
                f"Available tools: {sorted(TOOL_REGISTRY.keys())}"
            )

    def test_browse_directory_has_repository_alias_in_input_schema(self):
        """browse_directory inputSchema.properties includes 'repository_alias'."""
        tool_def = TOOL_REGISTRY["browse_directory"]
        properties = tool_def["inputSchema"]["properties"]
        assert "repository_alias" in properties, (
            "browse_directory inputSchema.properties must include 'repository_alias' "
            "so the centralized guard can intercept unauthorized access."
        )

    def test_get_file_content_has_repository_alias_in_input_schema(self):
        """get_file_content inputSchema.properties includes 'repository_alias'."""
        tool_def = TOOL_REGISTRY["get_file_content"]
        properties = tool_def["inputSchema"]["properties"]
        assert "repository_alias" in properties, (
            "get_file_content inputSchema.properties must include 'repository_alias' "
            "so the centralized guard can intercept unauthorized access."
        )

    def test_list_files_has_repository_alias_in_input_schema(self):
        """list_files inputSchema.properties includes 'repository_alias'."""
        tool_def = TOOL_REGISTRY["list_files"]
        properties = tool_def["inputSchema"]["properties"]
        assert "repository_alias" in properties, (
            "list_files inputSchema.properties must include 'repository_alias' "
            "so the centralized guard can intercept unauthorized access."
        )

    def test_directory_tree_has_repository_alias_in_input_schema(self):
        """directory_tree inputSchema.properties includes 'repository_alias'."""
        tool_def = TOOL_REGISTRY["directory_tree"]
        properties = tool_def["inputSchema"]["properties"]
        assert "repository_alias" in properties, (
            "directory_tree inputSchema.properties must include 'repository_alias' "
            "so the centralized guard can intercept unauthorized access."
        )


# ---------------------------------------------------------------------------
# Guard integration: _check_repository_access blocks unauthorized access
# for each of the 4 file access tools
# ---------------------------------------------------------------------------


class TestGuardBlocksUnauthorizedFileAccessByTool:
    """
    Guard integration verification: _check_repository_access() raises ValueError
    when a non-admin user attempts to access a repository they do not have
    access to, using argument structures typical of each file access tool.

    These tests call the guard directly (not through handle_tools_call) to
    confirm it correctly handles each tool's typical argument structure.
    """

    def test_browse_directory_blocked_for_unauthorized_user(self):
        """
        browse_directory: non-admin user without access to 'secret-repo'
        is blocked by the centralized guard.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "my-allowed-repo"},
        )

        # Typical browse_directory arguments: repository_alias + optional path
        arguments = {
            "repository_alias": "secret-repo-global",
            "path": "src",
            "recursive": True,
        }

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="browse_directory",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "secret-repo-global" in error_str
        assert "regularuser" in error_str

    def test_get_file_content_blocked_for_unauthorized_user(self):
        """
        get_file_content: non-admin user without access to 'private-repo'
        is blocked by the centralized guard.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        # Typical get_file_content arguments: repository_alias + file_path
        arguments = {
            "repository_alias": "private-repo-global",
            "file_path": "src/secret.py",
        }

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="get_file_content",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "private-repo-global" in error_str
        assert "regularuser" in error_str

    def test_list_files_blocked_for_unauthorized_user(self):
        """
        list_files: non-admin user without access to 'confidential-repo'
        is blocked by the centralized guard.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        # Typical list_files arguments: repository_alias + optional path
        arguments = {
            "repository_alias": "confidential-repo-global",
            "path": "src",
        }

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="list_files",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "confidential-repo-global" in error_str
        assert "regularuser" in error_str

    def test_directory_tree_blocked_for_unauthorized_user(self):
        """
        directory_tree: non-admin user without access to 'restricted-repo'
        is blocked by the centralized guard.
        """
        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )

        # Typical directory_tree arguments: repository_alias + optional path/depth
        arguments = {
            "repository_alias": "restricted-repo-global",
            "path": "src",
            "max_depth": 3,
        }

        with pytest.raises(ValueError) as exc_info:
            _check_repository_access(
                arguments=arguments,
                effective_user=user,
                tool_name="directory_tree",
                access_service=access_service,
            )

        error_str = str(exc_info.value)
        assert "Access denied" in error_str
        assert "restricted-repo-global" in error_str
        assert "regularuser" in error_str


# ---------------------------------------------------------------------------
# Admin bypass: admin user passes the guard for all 4 file access tools
# ---------------------------------------------------------------------------


class TestAdminBypassForFileAccessTools:
    """
    Admin bypass verification: an admin user can call any of the 4 file
    access tools against any repository alias without being blocked.

    The guard calls is_admin_user() first; if True, it returns without
    calling get_accessible_repos(). This is the AC3 behavior from Story #319.
    """

    def test_admin_bypasses_guard_for_browse_directory(self):
        """Admin user is not blocked by the guard for browse_directory."""
        admin = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos=set(),  # Empty - would block non-admin
        )

        arguments = {"repository_alias": "any-secret-repo-global", "path": "src"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=admin,
            tool_name="browse_directory",
            access_service=access_service,
        )

        access_service.get_accessible_repos.assert_not_called()

    def test_admin_bypasses_guard_for_get_file_content(self):
        """Admin user is not blocked by the guard for get_file_content."""
        admin = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos=set(),
        )

        arguments = {
            "repository_alias": "any-secret-repo-global",
            "file_path": "src/secret.py",
        }

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=admin,
            tool_name="get_file_content",
            access_service=access_service,
        )

        access_service.get_accessible_repos.assert_not_called()

    def test_admin_bypasses_guard_for_list_files(self):
        """Admin user is not blocked by the guard for list_files."""
        admin = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos=set(),
        )

        arguments = {"repository_alias": "any-secret-repo-global"}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=admin,
            tool_name="list_files",
            access_service=access_service,
        )

        access_service.get_accessible_repos.assert_not_called()

    def test_admin_bypasses_guard_for_directory_tree(self):
        """Admin user is not blocked by the guard for directory_tree."""
        admin = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos=set(),
        )

        arguments = {"repository_alias": "any-secret-repo-global", "max_depth": 5}

        # Must not raise
        _check_repository_access(
            arguments=arguments,
            effective_user=admin,
            tool_name="directory_tree",
            access_service=access_service,
        )

        access_service.get_accessible_repos.assert_not_called()
