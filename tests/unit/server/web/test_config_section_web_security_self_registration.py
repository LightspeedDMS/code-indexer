"""Hotfix: Web UI Config screen exposes web_security.self_registration_enabled.

The 'web_security' section was already a valid POST target and passed into
the template context, but was never rendered -- so the new flag gating
POST /auth/register would have been invisible to operators. These tests
cover the rendered section (display + edit form, mirroring the Alias Lock
section structure), section validation, and the real
POST /admin/config/web_security save round trip.
"""

import re
import secrets
import string
import tempfile
import unittest.mock as mock
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.web import routes

_TEST_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Rendering helpers (mirrors test_temporal_legacy_migration_display_1749.py)
# ---------------------------------------------------------------------------


def _get_current_config_with(svc: ConfigService) -> dict:
    with mock.patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=svc,
    ):
        return routes._get_current_config()


def _render_section(config: dict) -> str:
    template = routes.templates.env.get_template("partials/config_section.html")
    html = template.render(
        request=None,
        csrf_token="test_csrf_token",
        config=config,
        validation_errors={},
        restart_required_fields=[],
        api_keys_status={},
        github_token_data=None,
        gitlab_token_data=None,
    )
    start = html.find('id="section-web-security"')
    assert start != -1, "Missing Web Security <details> section"
    section_start = html.rfind("<details", 0, start)
    end = html.find("</details>", start)
    assert section_start != -1 and end != -1
    return html[section_start : end + len("</details>")]


def _extract_edit_form(section: str) -> str:
    start = section.find('<form id="edit-form-web-security"')
    assert start != -1, "Missing edit-form-web-security form"
    end = section.find("</form>", start)
    return section[start : end + len("</form>")]


@pytest.fixture
def svc(tmp_path) -> ConfigService:
    service = ConfigService(server_dir_path=str(tmp_path))
    service.load_config()
    return service


class TestWebSecuritySectionRendering:
    def test_section_rendered_with_display_and_form(self, svc) -> None:
        section = _render_section(_get_current_config_with(svc))
        assert 'id="display-web-security"' in section
        assert "toggleEditMode('web-security')" in section
        assert "cancelEdit('web-security')" in section
        form = _extract_edit_form(section)
        assert 'action="/admin/config/web_security"' in form
        assert 'method="post"' in form
        assert 'name="csrf_token"' in form

    def test_form_contains_only_self_registration_field(self, svc) -> None:
        form = _extract_edit_form(_render_section(_get_current_config_with(svc)))
        names = set(re.findall(r'name="([^"]+)"', form))
        assert names == {"csrf_token", "self_registration_enabled"}, names

    def test_display_and_select_reflect_default_disabled(self, svc) -> None:
        section = _render_section(_get_current_config_with(svc))
        assert re.search(
            r'Self-Registration Enabled</td>\s*<td class="config-value">No</td>',
            section,
        )
        form = _extract_edit_form(section)
        assert re.search(r'<option value="false" selected>No</option>', form)
        assert re.search(r'<option value="true" >Yes</option>', form)

    def test_display_and_select_reflect_enabled(self, svc) -> None:
        svc.update_setting("web_security", "self_registration_enabled", "true")
        section = _render_section(_get_current_config_with(svc))
        assert re.search(
            r'Self-Registration Enabled</td>\s*<td class="config-value">Yes</td>',
            section,
        )
        form = _extract_edit_form(section)
        assert re.search(r'<option value="true" selected>Yes</option>', form)
        assert re.search(r'<option value="false" >No</option>', form)


class TestValidateWebSecuritySection:
    @pytest.mark.parametrize("value", ["true", "false"])
    def test_boolean_strings_accepted(self, value) -> None:
        error = routes._validate_config_section(
            "web_security", {"self_registration_enabled": value}
        )
        assert error is None

    @pytest.mark.parametrize("value", ["yes", "1", "", "TRUE-ish"])
    def test_non_boolean_rejected(self, value) -> None:
        error = routes._validate_config_section(
            "web_security", {"self_registration_enabled": value}
        )
        assert error is not None
        assert "self-registration" in error.lower()


# ---------------------------------------------------------------------------
# Real HTTP save round trip (fixture pattern from test_config_status_codes_1554.py)
# ---------------------------------------------------------------------------


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
    with mock.patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmpdir_path)}):
        reset_config_service()
        app = create_app()
        yield app
        reset_config_service()


@pytest.fixture
def client(app_with_db):
    with TestClient(app_with_db) as c:
        yield c


@pytest.fixture
def admin_session(client, tmpdir_path):
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


def _persisted_flag() -> bool:
    from code_indexer.server.services.config_service import get_config_service

    web_sec = get_config_service().get_config().web_security_config
    assert web_sec is not None
    return web_sec.self_registration_enabled


def _post_web_security(client, admin_session, value: str):
    page = client.get("/admin/config", cookies=admin_session)
    assert page.status_code == 200
    return client.post(
        "/admin/config/web_security",
        data={
            "csrf_token": _scrape_csrf_token(page.text),
            "self_registration_enabled": value,
        },
        cookies=admin_session,
        follow_redirects=True,
    )


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestWebSecuritySaveRoundTrip:
    def test_post_true_then_false_persists_and_gates_register(
        self, client, admin_session
    ) -> None:
        register_body = {
            "username": "selfreg" + secrets.token_hex(4),
            "email": "selfreg@example.invalid",
            "password": _make_test_password(),
        }

        # Default closed: the real app's /auth/register is denied.
        assert client.post("/auth/register", json=register_body).status_code == 403

        resp = _post_web_security(client, admin_session, "true")
        assert resp.status_code == 200, resp.text[:300]
        assert "saved successfully" in resp.text.lower()
        assert _persisted_flag() is True
        assert client.post("/auth/register", json=register_body).status_code == 200

        resp = _post_web_security(client, admin_session, "false")
        assert resp.status_code == 200, resp.text[:300]
        assert _persisted_flag() is False
        register_body["username"] = "selfreg" + secrets.token_hex(4)
        assert client.post("/auth/register", json=register_body).status_code == 403

    def test_post_invalid_value_rejected_with_400(self, client, admin_session) -> None:
        resp = _post_web_security(client, admin_session, "maybe")
        assert resp.status_code == 400
        assert _persisted_flag() is False
