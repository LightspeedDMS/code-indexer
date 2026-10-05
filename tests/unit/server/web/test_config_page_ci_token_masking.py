"""Stored GitHub/GitLab tokens are shown on the admin Config page only as a
mask revealing at most their last 4 characters (finding 058).

Front door: the real app (``create_app``) over an isolated server home, a
real admin Web login, and ``GET /admin/config`` plus the HTMX partial
``GET /admin/partials/config-section``. Tokens are stored through the same
``CITokenManager`` factory the page reads from.
"""

from __future__ import annotations

import re
import uuid
from typing import Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import UserRole
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-Config-Admin-Passw0rd!"
# Neutral, format-valid sample tokens (never real credentials).
GITHUB_TOKEN = "ghp_" + "Qx7Example0Token0Sample0Value000" + "Gh4k"
GITLAB_TOKEN = "glpat-" + "Zx9Example0Sample0Value" + "Gl7m"
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture(scope="module")
def admin_client(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Tuple[TestClient, str]]:
    """Real app over an isolated home, logged in as a fresh admin, with both
    CI tokens stored."""
    root = tmp_path_factory.mktemp("config-ci-token-mask")
    with isolated_app(root) as app:
        from code_indexer.server.web.routes import _get_token_manager

        manager = _get_token_manager()
        manager.save_token("github", GITHUB_TOKEN)
        manager.save_token(
            "gitlab", GITLAB_TOKEN, base_url="https://gitlab.example.com"
        )

        name = f"admin-{uuid.uuid4().hex[:8]}"
        app.state.user_manager.create_user(name, PASSWORD, UserRole.ADMIN)
        client = TestClient(app, follow_redirects=False)
        match = _CSRF.search(client.get("/login").text)
        assert match, "csrf token not found on the login page"
        login = client.post(
            "/login",
            data={"username": name, "password": PASSWORD, "csrf_token": match.group(1)},
        )
        assert login.status_code == 303, login.status_code
        yield client, name


def _assert_masked(html: str, token: str) -> None:
    assert token not in html, "raw token rendered into the page"
    # No leading run of the token (provider prefix + entropy) may appear.
    assert token[:10] not in html
    assert token[-12:-4] not in html
    # The last 4 characters identify which token is stored.
    assert "••••" + token[-4:] in html


@pytest.mark.timeout(180)
def test_config_page_shows_only_last_four_of_ci_tokens(admin_client) -> None:
    client, _ = admin_client
    response = client.get("/admin/config")
    assert response.status_code == 200, response.status_code
    _assert_masked(response.text, GITHUB_TOKEN)
    _assert_masked(response.text, GITLAB_TOKEN)
    # Non-secret detail still shown.
    assert "https://gitlab.example.com" in response.text


@pytest.mark.timeout(180)
def test_config_section_partial_shows_only_last_four_of_ci_tokens(
    admin_client,
) -> None:
    client, _ = admin_client
    response = client.get("/admin/partials/config-section")
    assert response.status_code == 200, response.status_code
    _assert_masked(response.text, GITHUB_TOKEN)
    _assert_masked(response.text, GITLAB_TOKEN)
