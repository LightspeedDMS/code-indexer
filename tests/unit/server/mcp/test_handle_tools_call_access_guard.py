"""
Integration tests for centralized repository access guard in handle_tools_call()
dispatcher (Story #319).

Verifies that handle_tools_call() in protocol.py applies _check_repository_access()
automatically for any tool with repository-identifying parameters, before handler
invocation.

AC1: All tools with repository_alias/alias/user_alias are automatically checked
AC3: Admin users bypass the check
AC4: New tools added in future are automatically protected (dispatcher-level)
AC5: Existing properly-filtered tools (search_code) continue to work
"""

import json
import pytest
from datetime import datetime
from unittest.mock import Mock, patch

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


def _make_mock_tool_registry(tool_name: str, permission: str = "query_repos") -> dict:
    """Build a minimal TOOL_REGISTRY for a single tool."""
    return {
        tool_name: {
            "name": tool_name,
            "required_permission": permission,
        }
    }


def _make_mock_handler(return_value: dict = None) -> Mock:
    """Build a mock sync handler returning given value."""
    if return_value is None:
        return_value = {"content": [{"type": "text", "text": json.dumps({"success": True})}]}
    return Mock(return_value=return_value)


# ---------------------------------------------------------------------------
# Guard blocks unauthorized access through dispatcher
# ---------------------------------------------------------------------------


class TestHandleToolsCallBlocksUnauthorized:
    """AC1: Dispatcher guard blocks unauthorized repository access."""

    @pytest.mark.asyncio
    async def test_raises_value_error_for_unauthorized_repository_alias(self):
        """
        AC1: handle_tools_call raises ValueError when user requests a tool
        with a repository_alias they cannot access.
        The ValueError propagates out (becomes -32602 Invalid params in JSON-RPC).
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )
        mock_app_state = Mock()
        mock_app_state.access_filtering_service = access_service

        mock_handler = _make_mock_handler()

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.session_registry.get_session_registry") as mock_reg,
            patch("code_indexer.server.services.langfuse_service.get_langfuse_service", return_value=None),
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"browse_directory": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("browse_directory"),
                ),
            ):
                params = {
                    "name": "browse_directory",
                    "arguments": {
                        "repository_alias": "secret-repo",
                        "path": "/src",
                    },
                }

                with pytest.raises(ValueError) as exc_info:
                    await handle_tools_call(params, user)

        error_str = str(exc_info.value).lower()
        assert "access denied" in error_str
        # Handler must NOT have been called
        mock_handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_references_repo_and_user(self):
        """
        AC2: Error message names the denied repository and the user.
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("alice")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta"},
        )
        mock_app_state = Mock()
        mock_app_state.access_filtering_service = access_service

        mock_handler = _make_mock_handler()

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.session_registry.get_session_registry") as mock_reg,
            patch("code_indexer.server.services.langfuse_service.get_langfuse_service", return_value=None),
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"browse_directory": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("browse_directory"),
                ),
            ):
                params = {
                    "name": "browse_directory",
                    "arguments": {"repository_alias": "bob-private-repo"},
                }

                with pytest.raises(ValueError) as exc_info:
                    await handle_tools_call(params, user)

        error_str = str(exc_info.value)
        assert "bob-private-repo" in error_str
        assert "alice" in error_str


# ---------------------------------------------------------------------------
# Guard allows authorized access through dispatcher
# ---------------------------------------------------------------------------


class TestHandleToolsCallAllowsAuthorized:
    """AC1: Dispatcher guard allows authorized repository access."""

    @pytest.mark.asyncio
    async def test_allows_tool_execution_for_authorized_repo(self):
        """
        AC1: When user has access to the repo, handle_tools_call executes handler.
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )
        mock_app_state = Mock()
        mock_app_state.access_filtering_service = access_service

        expected = {"content": [{"type": "text", "text": json.dumps({"success": True, "files": []})}]}
        mock_handler = _make_mock_handler(expected)

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.session_registry.get_session_registry") as mock_reg,
            patch("code_indexer.server.services.langfuse_service.get_langfuse_service", return_value=None),
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"browse_directory": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("browse_directory"),
                ),
            ):
                params = {
                    "name": "browse_directory",
                    "arguments": {
                        "repository_alias": "allowed-repo",
                        "path": "/src",
                    },
                }

                result = await handle_tools_call(params, user)

        assert result == expected
        mock_handler.assert_called_once()

    @pytest.mark.asyncio
    async def test_allows_access_when_global_suffix_strips_to_accessible_repo(self):
        """
        Guard strips -global suffix: 'allowed-repo-global' matches 'allowed-repo'.
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos={"cidx-meta", "allowed-repo"},
        )
        mock_app_state = Mock()
        mock_app_state.access_filtering_service = access_service

        expected = {"content": [{"type": "text", "text": json.dumps({"success": True})}]}
        mock_handler = _make_mock_handler(expected)

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.session_registry.get_session_registry") as mock_reg,
            patch("code_indexer.server.services.langfuse_service.get_langfuse_service", return_value=None),
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"browse_directory": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("browse_directory"),
                ),
            ):
                params = {
                    "name": "browse_directory",
                    "arguments": {"repository_alias": "allowed-repo-global"},
                }

                result = await handle_tools_call(params, user)

        assert result == expected
        mock_handler.assert_called_once()


# ---------------------------------------------------------------------------
# Admin bypass through dispatcher
# ---------------------------------------------------------------------------


class TestHandleToolsCallAdminBypass:
    """AC3: Admin users bypass the guard in the dispatcher."""

    @pytest.mark.asyncio
    async def test_admin_can_access_any_repo(self):
        """
        AC3: Admin user passes guard for any repository without restriction.
        get_accessible_repos must NOT be called for admin.
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        admin_user = _make_user("admin", role=UserRole.ADMIN)
        access_service = _make_access_service(
            is_admin=True,
            accessible_repos=set(),  # Empty - would block non-admin
        )
        mock_app_state = Mock()
        mock_app_state.access_filtering_service = access_service

        expected = {"content": [{"type": "text", "text": json.dumps({"success": True})}]}
        mock_handler = _make_mock_handler(expected)

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.session_registry.get_session_registry") as mock_reg,
            patch("code_indexer.server.services.langfuse_service.get_langfuse_service", return_value=None),
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"browse_directory": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("browse_directory"),
                ),
            ):
                params = {
                    "name": "browse_directory",
                    "arguments": {"repository_alias": "any-secret-repo"},
                }

                result = await handle_tools_call(params, admin_user)

        assert result == expected
        mock_handler.assert_called_once()
        # Admin bypass: get_accessible_repos must NOT be called
        access_service.get_accessible_repos.assert_not_called()


# ---------------------------------------------------------------------------
# Guard skips when no repo param
# ---------------------------------------------------------------------------


class TestHandleToolsCallGuardSkipsNoRepoParam:
    """AC4: Tools without repo params are unaffected by the guard."""

    @pytest.mark.asyncio
    async def test_tool_without_repo_param_executes_without_access_check(self):
        """
        AC4: When arguments contain no repo identifier, guard skips entirely.
        Access service methods are not called.
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("regularuser")
        access_service = _make_access_service(
            is_admin=False,
            accessible_repos=set(),  # Would block if checked
        )
        mock_app_state = Mock()
        mock_app_state.access_filtering_service = access_service

        expected = {"content": [{"type": "text", "text": json.dumps({"status": "ok"})}]}
        mock_handler = _make_mock_handler(expected)

        with (
            patch("code_indexer.server.mcp.handlers.app_module") as mock_app_module,
            patch("code_indexer.server.mcp.session_registry.get_session_registry") as mock_reg,
            patch("code_indexer.server.services.langfuse_service.get_langfuse_service", return_value=None),
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"check_hnsw_health": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("check_hnsw_health"),
                ),
            ):
                params = {
                    "name": "check_hnsw_health",
                    "arguments": {},
                }

                result = await handle_tools_call(params, user)

        assert result == expected
        mock_handler.assert_called_once()
        # No repo param - service must not be interrogated
        access_service.is_admin_user.assert_not_called()
        access_service.get_accessible_repos.assert_not_called()
