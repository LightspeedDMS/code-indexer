"""
Tests for protocol-level API metrics tracking (Bug #350).

Verifies that handle_tools_call() in protocol.py tracks API metrics
at the dispatch level, replacing scattered service-level tracking.

AC1: Protocol tracks increment_other_api_call() for non-search tools
AC2: Protocol skips tracking for search_code (self-tracking)
AC3: Protocol skips tracking for regex_search (self-tracking)
AC4: Tracking does NOT happen when handler raises an exception
AC5: file_crud_service no longer imports or calls api_metrics_service
AC6: git_operations_service no longer imports or calls api_metrics_service
AC7: ssh_key_manager no longer imports or calls api_metrics_service
"""

import json
import pytest
from datetime import datetime
from unittest.mock import Mock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(username: str, role: UserRole = UserRole.ADMIN) -> User:
    """Create a real User object for testing (admin to skip permission checks)."""
    return User(
        username=username,
        password_hash="hashed_password",
        role=role,
        created_at=datetime(2024, 1, 1),
    )


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
        return_value = {
            "content": [{"type": "text", "text": json.dumps({"success": True})}]
        }
    return Mock(return_value=return_value)


def _make_raising_handler(exc: Exception) -> Mock:
    """Build a mock handler that raises the given exception."""
    handler = Mock(side_effect=exc)
    return handler


def _standard_patches(mock_app_state: Mock = None):
    """Return the standard patch context managers needed for handle_tools_call tests."""
    if mock_app_state is None:
        access_service = Mock()
        access_service.is_admin_user = Mock(return_value=True)
        access_service.get_accessible_repos = Mock(return_value=set())
        mock_app_state = Mock()
        mock_app_state.access_filtering_service = access_service

    return mock_app_state


# ---------------------------------------------------------------------------
# AC1: Protocol tracks increment_other_api_call for non-search tools
# ---------------------------------------------------------------------------


class TestProtocolTracksNonSearchTools:
    """AC1: Protocol-level dispatch tracks other_api_calls for non-search tools."""

    @pytest.mark.asyncio
    async def test_protocol_tracks_other_api_for_browse_directory(self):
        """
        handle_tools_call should call increment_other_api_call() once for
        browse_directory tool (a non-search tool).
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("admin")
        mock_app_state = _standard_patches()
        mock_handler = _make_mock_handler()

        with (
            patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.session_registry.get_session_registry"
            ) as mock_reg,
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.protocol.api_metrics_service"
            ) as mock_metrics,
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
                    "arguments": {},
                }

                await handle_tools_call(params, user)

        mock_metrics.increment_other_api_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_protocol_tracks_other_api_for_list_files(self):
        """
        handle_tools_call should call increment_other_api_call() once for
        list_files tool (another non-search tool).
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("admin")
        mock_app_state = _standard_patches()
        mock_handler = _make_mock_handler()

        with (
            patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.session_registry.get_session_registry"
            ) as mock_reg,
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.protocol.api_metrics_service"
            ) as mock_metrics,
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"list_files": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("list_files"),
                ),
            ):
                params = {
                    "name": "list_files",
                    "arguments": {},
                }

                await handle_tools_call(params, user)

        mock_metrics.increment_other_api_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_protocol_tracks_other_api_for_scip_definition(self):
        """
        handle_tools_call should call increment_other_api_call() once for
        scip_definition tool (a SCIP tool that previously had no tracking).
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("admin")
        mock_app_state = _standard_patches()
        mock_handler = _make_mock_handler()

        with (
            patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.session_registry.get_session_registry"
            ) as mock_reg,
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.protocol.api_metrics_service"
            ) as mock_metrics,
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"scip_definition": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("scip_definition"),
                ),
            ):
                params = {
                    "name": "scip_definition",
                    "arguments": {},
                }

                await handle_tools_call(params, user)

        mock_metrics.increment_other_api_call.assert_called_once()


# ---------------------------------------------------------------------------
# AC2/AC3: Protocol skips tracking for self-tracking tools
# ---------------------------------------------------------------------------


class TestProtocolSkipsSelfTrackingTools:
    """AC2+AC3: search_code and regex_search handle their own tracking."""

    @pytest.mark.asyncio
    async def test_protocol_skips_tracking_for_search_code(self):
        """
        handle_tools_call should NOT call increment_other_api_call() for
        search_code - it manages its own tracking in the handler.
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("admin")
        mock_app_state = _standard_patches()
        mock_handler = _make_mock_handler()

        with (
            patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.session_registry.get_session_registry"
            ) as mock_reg,
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.protocol.api_metrics_service"
            ) as mock_metrics,
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"search_code": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("search_code"),
                ),
            ):
                params = {
                    "name": "search_code",
                    "arguments": {"query_text": "test"},
                }

                await handle_tools_call(params, user)

        # search_code handles its own tracking - protocol must not double-count
        mock_metrics.increment_other_api_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_protocol_skips_tracking_for_regex_search(self):
        """
        handle_tools_call should NOT call increment_other_api_call() for
        regex_search - it manages its own tracking in the handler.
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("admin")
        mock_app_state = _standard_patches()
        mock_handler = _make_mock_handler()

        with (
            patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.session_registry.get_session_registry"
            ) as mock_reg,
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.protocol.api_metrics_service"
            ) as mock_metrics,
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"regex_search": mock_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("regex_search"),
                ),
            ):
                params = {
                    "name": "regex_search",
                    "arguments": {"pattern": "test.*"},
                }

                await handle_tools_call(params, user)

        # regex_search handles its own tracking - protocol must not double-count
        mock_metrics.increment_other_api_call.assert_not_called()


# ---------------------------------------------------------------------------
# AC4: Tracking does NOT happen when handler raises
# ---------------------------------------------------------------------------


class TestProtocolDoesNotTrackFailedCalls:
    """AC4: Failed tool calls (handler raises) should not be counted."""

    @pytest.mark.asyncio
    async def test_protocol_does_not_track_when_handler_raises(self):
        """
        When a handler raises an exception, increment_other_api_call()
        should NOT be called - we only count successful calls.
        """
        from code_indexer.server.mcp.protocol import handle_tools_call

        user = _make_user("admin")
        mock_app_state = _standard_patches()
        raising_handler = _make_raising_handler(ValueError("Tool execution failed"))

        with (
            patch(
                "code_indexer.server.mcp.handlers._utils.app_module"
            ) as mock_app_module,
            patch(
                "code_indexer.server.mcp.session_registry.get_session_registry"
            ) as mock_reg,
            patch(
                "code_indexer.server.services.langfuse_service.get_langfuse_service",
                return_value=None,
            ),
            patch(
                "code_indexer.server.mcp.protocol.api_metrics_service"
            ) as mock_metrics,
        ):
            mock_app_module.app.state = mock_app_state
            mock_reg.return_value.get_or_create_session.return_value = None

            from code_indexer.server.mcp import handlers as handlers_module
            from code_indexer.server.mcp import tools as tools_module

            with (
                patch.object(
                    handlers_module,
                    "HANDLER_REGISTRY",
                    {"list_files": raising_handler},
                ),
                patch.object(
                    tools_module,
                    "TOOL_REGISTRY",
                    _make_mock_tool_registry("list_files"),
                ),
            ):
                params = {
                    "name": "list_files",
                    "arguments": {},
                }

                with pytest.raises(ValueError, match="Tool execution failed"):
                    await handle_tools_call(params, user)

        # Failed calls must not be counted
        mock_metrics.increment_other_api_call.assert_not_called()


# ---------------------------------------------------------------------------
# AC5/AC6/AC7: Service files no longer import or call api_metrics_service
# ---------------------------------------------------------------------------


class TestServiceLevelTrackingRemoved:
    """AC5-AC7: Service-level api_metrics_service calls have been removed."""

    def test_file_crud_service_does_not_import_api_metrics_service(self):
        """
        file_crud_service should NOT import api_metrics_service after Bug #350 fix.
        Tracking is now centralized in protocol.py.
        """
        import pathlib

        source_path = pathlib.Path(
            "/home/jsbattig/Dev/code-indexer/src/code_indexer/server/services/file_crud_service.py"
        )
        source = source_path.read_text()

        # Check for inline imports of api_metrics_service
        assert "from .api_metrics_service import api_metrics_service" not in source, (
            "file_crud_service.py still imports api_metrics_service inline. "
            "Remove all increment_other_api_call() calls - tracking moved to protocol.py."
        )
        assert "api_metrics_service.increment_other_api_call()" not in source, (
            "file_crud_service.py still calls increment_other_api_call(). "
            "Remove all calls - tracking moved to protocol.py."
        )

    def test_git_operations_service_does_not_import_api_metrics_service(self):
        """
        git_operations_service should NOT import api_metrics_service after Bug #350 fix.
        Tracking is now centralized in protocol.py.
        """
        import pathlib

        source_path = pathlib.Path(
            "/home/jsbattig/Dev/code-indexer/src/code_indexer/server/services/git_operations_service.py"
        )
        source = source_path.read_text()

        assert "from .api_metrics_service import api_metrics_service" not in source, (
            "git_operations_service.py still imports api_metrics_service inline. "
            "Remove all increment_other_api_call() calls - tracking moved to protocol.py."
        )
        assert "api_metrics_service.increment_other_api_call()" not in source, (
            "git_operations_service.py still calls increment_other_api_call(). "
            "Remove all calls - tracking moved to protocol.py."
        )

    def test_ssh_key_manager_does_not_import_api_metrics_service(self):
        """
        ssh_key_manager should NOT import api_metrics_service after Bug #350 fix.
        Tracking is now centralized in protocol.py.
        """
        import pathlib

        source_path = pathlib.Path(
            "/home/jsbattig/Dev/code-indexer/src/code_indexer/server/services/ssh_key_manager.py"
        )
        source = source_path.read_text()

        assert "from .api_metrics_service import api_metrics_service" not in source, (
            "ssh_key_manager.py still imports api_metrics_service inline. "
            "Remove all increment_other_api_call() calls - tracking moved to protocol.py."
        )
        assert "api_metrics_service.increment_other_api_call()" not in source, (
            "ssh_key_manager.py still calls increment_other_api_call(). "
            "Remove all calls - tracking moved to protocol.py."
        )


# ---------------------------------------------------------------------------
# Module-level constant verification
# ---------------------------------------------------------------------------


class TestSelfTrackingToolsConstant:
    """Verify the _SELF_TRACKING_TOOLS constant is defined correctly in protocol."""

    def test_self_tracking_tools_frozenset_contains_search_code(self):
        """_SELF_TRACKING_TOOLS must contain 'search_code'."""
        from code_indexer.server.mcp.protocol import _SELF_TRACKING_TOOLS

        assert "search_code" in _SELF_TRACKING_TOOLS

    def test_self_tracking_tools_frozenset_contains_regex_search(self):
        """_SELF_TRACKING_TOOLS must contain 'regex_search'."""
        from code_indexer.server.mcp.protocol import _SELF_TRACKING_TOOLS

        assert "regex_search" in _SELF_TRACKING_TOOLS

    def test_self_tracking_tools_is_frozenset(self):
        """_SELF_TRACKING_TOOLS must be a frozenset for O(1) lookup."""
        from code_indexer.server.mcp.protocol import _SELF_TRACKING_TOOLS

        assert isinstance(_SELF_TRACKING_TOOLS, frozenset)
