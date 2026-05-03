"""
Structural elevation gate tests for MCP group-repo handlers (Task 5 / P1-A).

Verifies that 3 group-repo MCP handlers have @require_mcp_elevation() applied
by checking for the __wrapped__ attribute (set by @functools.wraps).
No invocation required — structural inspection only.
"""

from unittest.mock import patch

import pytest

_HANDLERS = [
    "handle_add_repos_to_group",
    "handle_remove_repo_from_group",
    "handle_bulk_remove_repos_from_group",
]


@pytest.fixture
def admin_handlers(tmp_path):
    """Import admin handlers with an isolated tempdir to avoid DB locking."""
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmp_path)}):
        import code_indexer.server.mcp.handlers.admin as _mod

        return _mod


@pytest.mark.parametrize("handler_name", _HANDLERS)
def test_mcp_group_repo_handler_is_elevation_wrapped(admin_handlers, handler_name):
    """Handler must have __wrapped__ attr proving @require_mcp_elevation() was applied."""
    handler = getattr(admin_handlers, handler_name, None)
    assert handler is not None, f"{handler_name} not found in admin handlers module"
    assert hasattr(handler, "__wrapped__"), (
        f"{handler_name} must have __wrapped__ attribute — "
        f"@require_mcp_elevation() was not applied"
    )
