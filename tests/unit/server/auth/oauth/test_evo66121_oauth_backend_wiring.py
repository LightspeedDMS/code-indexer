"""EVO-66121: OAuth routes must use the app-wired (backend-aware) managers.

Regression guard for the OAuth backend-split bug: get_user_manager() and
get_mcp_credential_manager() used to construct a bare UserManager(), which
defaults to the per-node JSON-file store (~/.cidx-server/users.json). In a
multipod / Postgres deployment that store holds none of the real users or MCP
credentials, so the /oauth/token client_credentials grant and the /oauth/authorize
password grant both authenticate against the wrong (empty) backend and fail.

app_wiring wires the real backend-aware UserManager / MCPCredentialManager onto
the auth `dependencies` module singletons at startup. The OAuth route providers
must resolve THOSE, falling back to a fresh instance only when unwired
(CLI / solo / unit tests).
"""

import pytest

from code_indexer.server.auth import dependencies as deps
from code_indexer.server.auth.oauth import routes
from code_indexer.server.auth.user_manager import UserManager
from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager


@pytest.fixture
def restore_wired_singletons():
    """Save/restore the module-level wired singletons so tests don't leak state."""
    saved_um = deps.user_manager
    saved_mcp = deps.mcp_credential_manager
    try:
        yield
    finally:
        deps.user_manager = saved_um
        deps.mcp_credential_manager = saved_mcp


class TestOAuthBackendWiring:
    def test_get_user_manager_returns_wired_singleton(self, restore_wired_singletons):
        """When app_wiring has set deps.user_manager (Postgres-backed in cluster
        mode), get_user_manager() must return THAT exact instance, not a fresh
        bare UserManager() pointed at the JSON-file store."""
        sentinel = UserManager()  # stands in for the backend-aware wired instance
        deps.user_manager = sentinel

        assert routes.get_user_manager() is sentinel

    def test_get_mcp_credential_manager_returns_wired_singleton(
        self, restore_wired_singletons
    ):
        """get_mcp_credential_manager() must return the wired MCPCredentialManager
        (whose user_manager is the Postgres-backed one), not one built over a bare
        UserManager()."""
        wired_um = UserManager()
        wired_mcp = MCPCredentialManager(user_manager=wired_um)
        deps.mcp_credential_manager = wired_mcp

        assert routes.get_mcp_credential_manager() is wired_mcp

    def test_get_mcp_credential_manager_uses_wired_user_manager_when_no_mcp_singleton(
        self, restore_wired_singletons
    ):
        """If only the user_manager singleton is wired, a freshly-built
        MCPCredentialManager must still be backed by the WIRED user_manager, not a
        bare JSON-file one."""
        wired_um = UserManager()
        deps.user_manager = wired_um
        deps.mcp_credential_manager = None

        mgr = routes.get_mcp_credential_manager()
        assert mgr.user_manager is wired_um

    def test_get_user_manager_falls_back_when_unwired(self, restore_wired_singletons):
        """CLI / solo / unit-test path: no wired singleton -> a usable UserManager
        instance is still returned (backward compatible)."""
        deps.user_manager = None
        mgr = routes.get_user_manager()
        assert isinstance(mgr, UserManager)
