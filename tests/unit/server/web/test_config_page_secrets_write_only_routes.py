"""Front-door check: stored secrets are write-only on the admin Config page.

Drives the REAL FastAPI app through TestClient (real login, real CSRF, real
config routes) against a temp server data directory:
  - GET /admin/config never contains a stored secret value;
  - POST /admin/config/oidc with a blank client secret keeps the stored one;
  - POST /admin/config/langfuse with a blank secret key keeps the stored one;
  - POST /admin/config/langfuse_pull with blank project secrets keeps each
    stored secret, and a non-blank value replaces it;
  - POST /admin/config/langfuse_pull that would leave a project with no
    secret key is refused with 400 and changes nothing.

App startup dominates the runtime, so the app is built ONCE per module (the
first test's setup pays it; every other test is sub-second). The explicit
per-test timeout covers that one-off build under a loaded gate.
"""

from __future__ import annotations

import json
import re
import secrets
import string
from typing import Any, Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from tests.unit.server._isolated_app import isolated_app

_TEST_TIMEOUT = 120

OIDC_SECRET = "example-oidc-client-secret-not-real-0101"
LANGFUSE_SECRET = "sk-lf-example-tracing-secret-not-real-0102"
PULL_SECRET_A = "sk-lf-example-pull-secret-a-not-real-0103"
PULL_SECRET_B = "sk-lf-example-pull-secret-b-not-real-0104"
PULL_PK_A = "pk-lf-example-route-project-a"
PULL_PK_B = "pk-lf-example-route-project-b"


def _make_test_password() -> str:
    from code_indexer.server.auth.password_strength_validator import (
        PasswordStrengthValidator,
    )

    validator = PasswordStrengthValidator()
    specials = "!@#%^&*"
    alphabet = string.ascii_letters + string.digits + specials
    for _ in range(10):
        chars = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice(specials),
        ] + [secrets.choice(alphabet) for _ in range(16)]
        secrets.SystemRandom().shuffle(chars)
        candidate = "".join(chars)
        ok, _ = validator.validate(candidate, username="testuser")
        if ok:
            return candidate
    raise AssertionError("_make_test_password() exhausted all attempts")


def _scrape_csrf_token(html: str) -> str:
    match = re.search(r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"', html)
    assert match is not None, "CSRF token not found in HTML"
    return match.group(1)


@pytest.fixture(scope="module")
def _app(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Tuple[TestClient, Any]]:
    """One app per module: ``(client, the app's DB-attached ConfigService)``.

    Built through ``isolated_app``: ``create_app()`` re-points process-wide
    singletons (the process app's user manager, the token blacklist, ...)
    at this module's server home, and every later test in the process must
    get them back once that home is deleted.
    """
    from code_indexer.server.auth.user_manager import UserRole
    from code_indexer.server.services.config_service import (
        get_config_service,
        reset_config_service,
    )

    root = tmp_path_factory.mktemp("config-secrets-write-only")
    reset_config_service()
    try:
        with isolated_app(root) as app, TestClient(app) as c:
            username = "example-admin-" + secrets.token_hex(4)
            password = _make_test_password()
            app.state.user_manager.create_user(
                username=username, password=password, role=UserRole.ADMIN
            )
            resp = c.get("/login")
            login = c.post(
                "/login",
                data={
                    "username": username,
                    "password": password,
                    "csrf_token": _scrape_csrf_token(resp.text),
                },
                follow_redirects=False,
            )
            assert login.status_code == 303, login.status_code
            for name, val in login.cookies.items():
                c.cookies.set(name, val)
            yield c, get_config_service()
    finally:
        reset_config_service()


@pytest.fixture
def client(_app) -> TestClient:
    """The shared client, with the app's DB-attached ConfigService reinstalled.

    tests/conftest.py resets the config-service singleton before every test;
    without reinstalling it, routes would run against a fresh service with no
    runtime database instead of the app's real one.
    """
    from code_indexer.server.services.config_service import set_config_service

    test_client: TestClient = _app[0]
    app_config_service = _app[1]
    set_config_service(app_config_service)
    # Raises unless a runtime database is attached (the real persistence path).
    app_config_service.read_committed_section("langfuse_config")
    return test_client


@pytest.fixture
def seeded(client):
    """Store every covered secret through the real config service."""
    from code_indexer.server.services.config_service import get_config_service

    svc = get_config_service()
    svc.update_setting("oidc", "client_secret", OIDC_SECRET)
    svc.update_setting("langfuse", "secret_key", LANGFUSE_SECRET)
    svc.update_setting(
        "langfuse",
        "pull_projects",
        json.dumps(
            [
                {"public_key": PULL_PK_A, "secret_key": PULL_SECRET_A},
                {"public_key": PULL_PK_B, "secret_key": PULL_SECRET_B},
            ]
        ),
    )
    return svc


def _config_page(client: TestClient) -> str:
    resp = client.get("/admin/config")
    assert resp.status_code == 200, resp.status_code
    return resp.text


@pytest.mark.timeout(_TEST_TIMEOUT)
class TestConfigPageFrontDoor:
    def test_config_page_contains_no_stored_secret(self, client, seeded) -> None:
        html = _config_page(client)
        for secret in (OIDC_SECRET, LANGFUSE_SECRET, PULL_SECRET_A, PULL_SECRET_B):
            assert secret not in html, "a stored secret was rendered into the page"
        assert PULL_PK_A in html

    def test_oidc_save_with_blank_secret_keeps_stored_secret(
        self, client, seeded
    ) -> None:
        resp = client.post(
            "/admin/config/oidc",
            data={
                "csrf_token": _scrape_csrf_token(_config_page(client)),
                "enabled": "false",
                "client_id": "example-route-client-id",
                "client_secret": "",
            },
        )
        assert resp.status_code == 200, resp.text[:300]
        oidc = seeded.get_config().oidc_provider_config
        assert oidc.client_id == "example-route-client-id"
        assert oidc.client_secret == OIDC_SECRET

    def test_langfuse_pull_save_merges_secrets_by_public_key(
        self, client, seeded
    ) -> None:
        projects = [
            {"public_key": PULL_PK_A, "secret_key": ""},
            {"public_key": PULL_PK_B, "secret_key": "sk-lf-example-route-rotated"},
        ]
        resp = client.post(
            "/admin/config/langfuse_pull",
            data={
                "csrf_token": _scrape_csrf_token(_config_page(client)),
                "pull_enabled": "false",
                "pull_projects": json.dumps(projects),
            },
        )
        assert resp.status_code == 200, resp.text[:300]
        stored = {
            p.public_key: p.secret_key
            for p in seeded.get_config().langfuse_config.pull_projects
        }
        assert stored == {
            PULL_PK_A: PULL_SECRET_A,
            PULL_PK_B: "sk-lf-example-route-rotated",
        }

    def test_langfuse_save_with_blank_secret_keeps_stored_secret(
        self, client, seeded
    ) -> None:
        resp = client.post(
            "/admin/config/langfuse",
            data={
                "csrf_token": _scrape_csrf_token(_config_page(client)),
                "enabled": "false",
                "public_key": "pk-lf-example-route-tracing",
                "secret_key": "",
                "host": "https://langfuse.example.com",
                "auto_trace_enabled": "false",
            },
        )
        assert resp.status_code == 200, resp.text[:300]
        langfuse = seeded.get_config().langfuse_config
        assert langfuse.public_key == "pk-lf-example-route-tracing"
        assert langfuse.secret_key == LANGFUSE_SECRET

    def test_langfuse_pull_save_without_secret_is_refused_with_400(
        self, client, seeded
    ) -> None:
        before = [
            (p.public_key, p.secret_key)
            for p in seeded.get_config().langfuse_config.pull_projects
        ]
        # Direct read of the persisted row: (version, langfuse section).
        committed_before = seeded.read_committed_section("langfuse_config")
        assert committed_before[1].get("pull_projects"), "seed not persisted"
        projects = [
            {"public_key": PULL_PK_A + "-renamed", "secret_key": ""},
            {"public_key": PULL_PK_B, "secret_key": ""},
        ]
        resp = client.post(
            "/admin/config/langfuse_pull",
            data={
                "csrf_token": _scrape_csrf_token(_config_page(client)),
                "pull_enabled": "false",
                "pull_projects": json.dumps(projects),
            },
        )
        assert resp.status_code == 400, resp.status_code
        assert "no secret key" in resp.text
        for secret in (PULL_SECRET_A, PULL_SECRET_B):
            assert secret not in resp.text
        after = [
            (p.public_key, p.secret_key)
            for p in seeded.get_config().langfuse_config.pull_projects
        ]
        assert after == before
        # The persisted row is untouched and its version did not advance.
        assert seeded.read_committed_section("langfuse_config") == committed_before
