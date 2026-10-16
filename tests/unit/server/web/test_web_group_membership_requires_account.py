"""Web group assignment is only written for names that have an account.

The route function runs over a real ``GroupAccessManager`` and a real SQLite
``UserManager``.  The admin-session and CSRF checks (covered by their own
tests) and the page rendering are replaced; the rendering double records the
message the page would show.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from starlette.requests import Request

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.web import routes as web_routes
from tests.unit.server._account_rows import PASSWORD, Stores, build_stores


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stores:
    built = build_stores(tmp_path)
    monkeypatch.setattr(dependencies, "user_manager", built.user_manager)
    monkeypatch.setattr(web_routes, "_get_group_manager", lambda: built.registry.groups)
    monkeypatch.setattr(
        web_routes,
        "_require_admin_session",
        lambda _r: SimpleNamespace(username="admin"),
    )
    monkeypatch.setattr(web_routes, "validate_login_csrf_token", lambda _r, _t: True)
    return built


@pytest.fixture
def rendered(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    pages: List[Dict[str, Any]] = []

    def _capture(_request: Request, _session: Any, **kwargs: Any) -> Dict[str, Any]:
        pages.append(kwargs)
        return kwargs

    monkeypatch.setattr(web_routes, "_create_groups_page_response", _capture)
    return pages


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _admins_id(stores: Stores) -> int:
    group = stores.registry.groups.get_group_by_name("admins")
    assert group is not None
    return int(group.id)


def test_web_assign_refuses_name_without_account(
    stores: Stores, rendered: List[Dict[str, Any]]
) -> None:
    web_routes.assign_user_to_group(_request(), "ghost", _admins_id(stores), "token")

    assert len(rendered) == 1
    assert "ghost" in rendered[0]["error_message"]
    assert "not found" in rendered[0]["error_message"].lower()
    assert stores.registry.groups.get_user_group("ghost") is None


def test_web_assign_writes_membership_for_existing_account(
    stores: Stores, rendered: List[Dict[str, Any]]
) -> None:
    stores.user_manager.create_user("alice", PASSWORD, UserRole.NORMAL_USER)

    web_routes.assign_user_to_group(_request(), "alice", _admins_id(stores), "token")

    assert "success_message" in rendered[0]
    group = stores.registry.groups.get_user_group("alice")
    assert group is not None and group.name == "admins"
