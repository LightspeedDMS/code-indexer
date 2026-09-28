"""
The self-service API Keys menu entry is shown only once the viewer has MFA
enabled, when elevation enforcement is on (creating a personal API key then
requires TOTP). With enforcement off the entry is shown exactly as before.
The page itself stays reachable, so existing keys can still be listed.

Front door: the real user web router rendering the real templates, over
real auth components on isolated files.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.web.auth import SESSION_COOKIE_NAME
from code_indexer.server.web.routes import user_router
from tests.unit.server.self_service_elevation_harness import (
    SelfServiceStack,
    build_stack,
    enforcement,
)

_MENU_LINK = '<a href="/user/api-keys"'
# Every page that extends user_base.html.
_USER_PAGES = ["/user/api-keys", "/user/mcp-credentials", "/user/git-credentials"]


@pytest.fixture
def stack(tmp_path, monkeypatch) -> SelfServiceStack:
    return build_stack(tmp_path, monkeypatch)


@pytest.fixture
def client(stack) -> TestClient:
    app = FastAPI()
    app.include_router(user_router, prefix="/user")
    return TestClient(app, raise_server_exceptions=False)


def _page(client: TestClient, stack: SelfServiceStack, path: str, user) -> str:
    client.cookies.set(SESSION_COOKIE_NAME, stack.session_cookie(user))
    try:
        resp = client.get(path)
    finally:
        client.cookies.clear()
    assert resp.status_code == 200, resp.text
    return resp.text


@pytest.mark.parametrize("path", _USER_PAGES)
class TestUserApiKeysMenu:
    def test_hidden_without_mfa_when_enforcement_on(self, client, stack, path):
        user = stack.create_user("menu-no-mfa")
        with enforcement(True):
            html = _page(client, stack, path, user)
        assert _MENU_LINK not in html

    def test_shown_with_mfa_when_enforcement_on(self, client, stack, path):
        user = stack.create_user("menu-with-mfa")
        stack.enroll_mfa(user.username)
        with enforcement(True):
            html = _page(client, stack, path, user)
        assert _MENU_LINK in html

    def test_shown_without_mfa_when_enforcement_off(self, client, stack, path):
        user = stack.create_user("menu-off")
        with enforcement(False):
            html = _page(client, stack, path, user)
        assert _MENU_LINK in html


def test_api_keys_page_stays_reachable_without_mfa(client, stack):
    user = stack.create_user("menu-page-no-mfa")
    with enforcement(True):
        html = _page(client, stack, "/user/api-keys", user)
    assert "Generate New Key" in html
