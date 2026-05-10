"""
Structural elevation gate tests for P2 MCP handlers (Task 7 / P2).

Verifies that P2-priority MCP handlers have @require_mcp_elevation() applied
by checking for the __wrapped__ attribute (set by @functools.wraps).
No invocation required — structural inspection only.

Story #989: handle_admin_list_all/user/system_mcp_credentials removed;
replaced by inner handlers _list_all/_list_user/_list_system in the
mcp_credentials submodule (all decorated with @require_mcp_elevation()).
"""

from unittest.mock import patch

import pytest

# Handlers on admin_handlers module directly
_ADMIN_HANDLERS = [
    "handle_update_group",
    "list_users",
    # Gap 4: read-sensitive MCP handlers missing elevation
    "handle_admin_logs_query",
    "admin_logs_export",
    "handle_query_audit_logs",
]

# Inner handlers that live in the mcp_credentials submodule
_MCP_CREDS_INNER_HANDLERS = [
    "_list_all",
    "_list_user",
    "_list_system",
]


@pytest.fixture
def admin_handlers(tmp_path):
    """Import admin handlers with an isolated tempdir to avoid DB locking."""
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmp_path)}):
        import code_indexer.server.mcp.handlers.admin as _mod

        return _mod


@pytest.mark.parametrize("handler_name", _ADMIN_HANDLERS)
def test_mcp_p2_handler_is_elevation_wrapped(admin_handlers, handler_name):
    """Handler must have __wrapped__ attr proving @require_mcp_elevation() was applied."""
    handler = getattr(admin_handlers, handler_name, None)
    assert handler is not None, f"{handler_name} not found in admin handlers module"
    assert hasattr(handler, "__wrapped__"), (
        f"{handler_name} must have __wrapped__ attribute — "
        f"@require_mcp_elevation() was not applied"
    )


@pytest.mark.parametrize("handler_name", _MCP_CREDS_INNER_HANDLERS)
def test_mcp_creds_inner_handler_is_elevation_wrapped(admin_handlers, handler_name):
    """Inner mcp_credentials handler must have __wrapped__ (elevation applied).

    Story #989: old admin_list_*_mcp_credentials handlers replaced by inner
    handlers in mcp_credentials submodule — all must be elevation-gated.
    """
    handler = getattr(admin_handlers.mcp_credentials, handler_name, None)
    assert handler is not None, (
        f"{handler_name} not found in admin_handlers.mcp_credentials"
    )
    assert hasattr(handler, "__wrapped__"), (
        f"mcp_credentials.{handler_name} must have __wrapped__ attribute — "
        f"@require_mcp_elevation() was not applied"
    )
