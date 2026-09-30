"""
POST /admin/config/{section}: host normalisation and change-only validation
through the real Web UI front door.

- The host submitted in the server section is stripped once; the stripped
  value is what is validated and stored (the route's guard and the storage
  layer never disagree about whitespace).
- A host persisted before host validation existed never blocks saving an
  unrelated section, nor a server-section save that leaves the host
  unchanged. Changing the host to an invalid value is still refused.

Real FastAPI app via TestClient, real admin login, real CSRF token.
"""

from __future__ import annotations

import re
import secrets
import string
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_TEST_TIMEOUT = 120
_LEGACY_HOST = "fe80::1%eth0"


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


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def app_with_db(tmpdir_path):
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service
    from code_indexer.server.storage.database_manager import DatabaseSchema

    DatabaseSchema(str(tmpdir_path / "test.db")).initialize_database()
    with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmpdir_path)}):
        reset_config_service()
        app = create_app()
        yield app
        reset_config_service()


@pytest.fixture
def client(app_with_db):
    with TestClient(app_with_db) as c:
        yield c


@pytest.fixture
def admin_session(client, tmpdir_path, app_with_db):
    from code_indexer.server.auth.user_manager import UserManager, UserRole

    um = UserManager(
        use_sqlite=True, db_path=str(tmpdir_path / "data" / "cidx_server.db")
    )
    username = secrets.token_hex(8)
    password = _make_test_password()
    um.create_user(username=username, password=password, role=UserRole.ADMIN)

    resp = client.get("/login")
    csrf = _scrape_csrf_token(resp.text)
    login = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf},
        cookies=resp.cookies,
        follow_redirects=False,
    )
    assert login.status_code == 303, f"Login failed: {login.status_code}"
    for name, val in login.cookies.items():
        client.cookies.set(name, val)
    return login.cookies


@pytest.fixture
def config_csrf(client, admin_session):
    resp = client.get("/admin/config", cookies=admin_session)
    assert resp.status_code == 200
    return _scrape_csrf_token(resp.text)


def _persist_host(host: str) -> None:
    """Persist a host directly (no validation), as a pre-existing
    deployment's stored value would be."""
    from code_indexer.server.services.config_service import get_config_service

    svc = get_config_service()
    cfg = svc.get_config()
    cfg.host = host
    svc.save_config(cfg)


def _persisted_config():
    from code_indexer.server.services.config_service import get_config_service

    return get_config_service().get_config()


def _server_form(csrf: str, host: str, **extra: str) -> dict:
    cfg = _persisted_config()
    form = {
        "csrf_token": csrf,
        "host": host,
        "port": str(cfg.port),
        "workers": str(cfg.workers),
        "log_level": cfg.log_level,
    }
    form.update(extra)
    return form


@pytest.mark.slow  # full create_app() per test (~10-14 s setup)
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestServerSectionHostFrontDoor:
    def test_padded_host_is_saved_stripped(self, client, admin_session, config_csrf):
        resp = client.post(
            "/admin/config/server",
            data=_server_form(
                config_csrf, "  192.0.2.10  ", confirm_host_port_change="1"
            ),
            cookies=admin_session,
        )

        assert resp.status_code == 200, resp.text[:500]
        assert _persisted_config().host == "192.0.2.10"

    def test_padded_unchanged_host_is_not_a_host_change(
        self, client, admin_session, config_csrf
    ):
        persisted = _persisted_config().host
        resp = client.post(
            "/admin/config/server",
            data=_server_form(config_csrf, f"  {persisted}  ", log_level="DEBUG"),
            cookies=admin_session,
        )

        assert resp.status_code == 200, resp.text[:500]
        assert _persisted_config().host == persisted
        assert _persisted_config().log_level == "DEBUG"

    def test_interior_newline_host_is_refused(self, client, admin_session, config_csrf):
        before = _persisted_config().host
        resp = client.post(
            "/admin/config/server",
            data=_server_form(
                config_csrf,
                "192.0.2.10\nExecStartPre=+/bin/true",
                confirm_host_port_change="1",
            ),
            cookies=admin_session,
        )

        assert resp.status_code == 400, resp.text[:500]
        assert _persisted_config().host == before

    def test_unchanged_legacy_host_does_not_block_server_section_save(
        self, client, admin_session, config_csrf
    ):
        _persist_host(_LEGACY_HOST)
        resp = client.post(
            "/admin/config/server",
            data=_server_form(config_csrf, _LEGACY_HOST, log_level="DEBUG"),
            cookies=admin_session,
        )

        assert resp.status_code == 200, resp.text[:500]
        assert _persisted_config().log_level == "DEBUG"
        assert _persisted_config().host == _LEGACY_HOST

    def test_legacy_host_does_not_block_unrelated_section_save(
        self, client, admin_session, config_csrf
    ):
        _persist_host(_LEGACY_HOST)
        resp = client.post(
            "/admin/config/search_event_log",
            data={"csrf_token": config_csrf, "search_event_log_retention_days": "45"},
            cookies=admin_session,
        )

        assert resp.status_code == 200, resp.text[:500]

    def test_legacy_host_does_not_block_oidc_section_save(
        self, client, admin_session, config_csrf
    ):
        _persist_host(_LEGACY_HOST)
        resp = client.post(
            "/admin/config/oidc",
            data={"csrf_token": config_csrf, "enabled": "false"},
            cookies=admin_session,
        )

        assert resp.status_code == 200, resp.text[:500]
        assert _persisted_config().host == _LEGACY_HOST
