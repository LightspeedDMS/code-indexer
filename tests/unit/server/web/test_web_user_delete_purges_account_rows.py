"""Web user deletion removes every row keyed to the name, via the configured
stores only.

The route function is driven with real SQLite stores; only the admin-session
and CSRF checks (covered by their own tests) are replaced.  An SSO manager
whose ``db_path`` names a node-local file stands in for a cluster node: the
deletion must reach SSO links through the configured store and never open
that file.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.oidc import routes as oidc_routes
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.web import routes as web_routes
from tests.unit.server._account_rows import (
    OTHER_PASSWORD,
    PASSWORD,
    assert_account_has_no_inherited_rows,
    build_stores,
    seed_account_rows,
)


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


@pytest.mark.asyncio
async def test_web_delete_removes_rows_keyed_to_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = build_stores(tmp_path)
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    seeded = seed_account_rows(stores, "alice")
    node_local_sso_file = tmp_path / "node-local-oauth.db"

    monkeypatch.setattr(dependencies, "user_manager", stores.user_manager)
    monkeypatch.setattr(
        web_routes,
        "_require_admin_session",
        lambda _r: SimpleNamespace(username="admin"),
    )
    monkeypatch.setattr(web_routes, "validate_login_csrf_token", lambda _r, _t: True)
    monkeypatch.setattr(
        oidc_routes, "oidc_manager", SimpleNamespace(db_path=str(node_local_sso_file))
    )

    response = await web_routes.delete_user(_request(), "alice", csrf_token="token")

    assert response.status_code == 303
    assert "success=user_deleted" in response.headers["location"]
    assert not node_local_sso_file.exists()
    stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)
    assert_account_has_no_inherited_rows(stores, "alice", seeded)
