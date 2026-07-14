"""
Regression test for Bug #1353: OAuth discovery endpoint missing at the
/mcp-suffixed path.

ChatGPT's MCP connector requests
GET /.well-known/oauth-authorization-server/mcp (path-suffixed with the MCP
resource's own path segment) instead of the root-level
GET /.well-known/oauth-authorization-server. Before the fix, the suffixed
path 404s (no route registered), causing the client to assume PKCE is
unsupported.

This test asserts the new alias route returns HTTP 200 with a body
field-for-field identical to the existing root discovery endpoint.
"""

from tests.unit.server.routes._smoke_registry import (
    client,  # noqa: F401 — fixture re-export
    mock_admin_user,  # noqa: F401 — fixture re-export
)

ROOT_PATH = "/.well-known/oauth-authorization-server"
MCP_ALIAS_PATH = "/.well-known/oauth-authorization-server/mcp"


def test_mcp_alias_path_returns_200(client):  # noqa: F811
    """The /mcp-suffixed discovery alias must be reachable (not 404)."""
    response = client.get(MCP_ALIAS_PATH)
    assert response.status_code == 200, (
        f"Expected 200 from {MCP_ALIAS_PATH}, got {response.status_code}: "
        f"{response.text[:500]}"
    )


def test_mcp_alias_body_matches_root_discovery(client):  # noqa: F811
    """The alias must return byte-identical (field-for-field) JSON metadata."""
    root_response = client.get(ROOT_PATH)
    alias_response = client.get(MCP_ALIAS_PATH)

    assert root_response.status_code == 200
    assert alias_response.status_code == 200
    assert alias_response.json() == root_response.json()
