"""
Bug #943 POST handler tests for /admin/config/totp_elevation.

Verifies that the generic update_config_section route correctly dispatches
to _update_totp_elevation_setting for the 'totp_elevation' section.

Three cases:
  1. Valid form data -> 200 with success message in body.
  2. Invalid idle timeout (below range min=60) -> 200 with the specific
     validation message "elevation_idle_timeout_seconds must be between"
     proving the idle-timeout constraint fired, NOT a generic DB error.
  3. Missing/invalid CSRF -> 200 with CSRF error in body, no settings updated.

Pattern mirrors test_config_cache_size_caps.py: real create_app() + real
admin user + real login + real CSRF token scrape. No mocking of core code.
"""

import re
import secrets
import string
import tempfile
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOKEN_USERNAME_BYTES = 8
_TEST_TIMEOUT_SECONDS = 30

_SUCCESS_FRAGMENT = "Totp_Elevation configuration saved successfully"
_CSRF_ERROR_FRAGMENT = "Invalid CSRF token"

# Fragment from the exact ValueError raised by _update_totp_elevation_setting
# when elevation_idle_timeout_seconds is outside [60, 3600].
_IDLE_TIMEOUT_VALIDATION_FRAGMENT = "elevation_idle_timeout_seconds must be between"

# Valid form values in accepted ranges
_VALID_FORM = {
    "elevation_enforcement_enabled": "false",
    "elevation_idle_timeout_seconds": "300",
    "elevation_max_age_seconds": "1800",
}

# idle_timeout=0 is below the minimum of 60 — triggers the specific ValueError
_INVALID_IDLE_FORM = {
    "elevation_enforcement_enabled": "false",
    "elevation_idle_timeout_seconds": "0",
    "elevation_max_age_seconds": "1800",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_password() -> str:
    """Generate a random password accepted by the server's password validator."""
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
    """Extract the CSRF token value from the rendered page HTML."""
    match = re.search(r'<input[^>]+name="csrf_token"[^>]+value="([^"]+)"', html)
    assert match is not None, "CSRF token not found in HTML"
    return match.group(1)


def _post_totp_form(
    client, cookies: dict, csrf_token: str, form_data: dict
) -> httpx.Response:
    """POST form data to /admin/config/totp_elevation and return the response."""
    data = {**form_data, "csrf_token": csrf_token}
    # FastAPI TestClient.post() is typed as returning Any (its base class signature
    # erases the concrete httpx.Response type). Explicit cast restores the narrow
    # type so mypy's no-any-return rule passes for this helper's declared return.
    return cast(
        httpx.Response,
        client.post(
            "/admin/config/totp_elevation",
            data=data,
            cookies=cookies,
            follow_redirects=True,
        ),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def app_with_db(tmpdir_path):
    from unittest.mock import patch

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
    with TestClient(app_with_db) as test_client:
        yield test_client


@pytest.fixture
def admin_credentials(tmpdir_path, app_with_db):
    from code_indexer.server.auth.user_manager import UserManager, UserRole

    user_manager = UserManager(
        use_sqlite=True, db_path=str(tmpdir_path / "data" / "cidx_server.db")
    )
    username = secrets.token_hex(_TOKEN_USERNAME_BYTES)
    password = _make_test_password()
    user_manager.create_user(username=username, password=password, role=UserRole.ADMIN)
    return username, password


@pytest.fixture
def admin_session(client, admin_credentials):
    """Perform real login; return session cookies for subsequent requests."""
    username, password = admin_credentials
    resp_get = client.get("/login")
    assert resp_get.status_code == 200
    csrf_token = _scrape_csrf_token(resp_get.text)
    resp_post = client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": csrf_token},
        cookies=resp_get.cookies,
        follow_redirects=False,
    )
    assert resp_post.status_code == 303, f"Login failed: HTTP {resp_post.status_code}"
    for name, value in resp_post.cookies.items():
        client.cookies.set(name, value)
    return resp_post.cookies


@pytest.fixture
def config_csrf_token(client, admin_session):
    """Fetch /admin/config and scrape the CSRF token for form POSTs."""
    resp = client.get("/admin/config", cookies=admin_session)
    assert resp.status_code == 200
    return _scrape_csrf_token(resp.text)


# ---------------------------------------------------------------------------
# TestPostHandlerEndpoint
# ---------------------------------------------------------------------------


class TestPostHandlerEndpoint:
    """
    Bug #943: POST /admin/config/totp_elevation is accepted by the generic
    update_config_section route and dispatches to _update_totp_elevation_setting.
    """

    @pytest.mark.slow
    @pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
    def test_post_with_valid_form_returns_200_success(
        self, client, admin_session, config_csrf_token
    ):
        """Valid totp_elevation POST must return HTTP 200 with success message."""
        resp = _post_totp_form(client, admin_session, config_csrf_token, _VALID_FORM)
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}. Body snippet: {resp.text[:500]!r}"
        )
        assert _SUCCESS_FRAGMENT in resp.text, (
            f"Success message {_SUCCESS_FRAGMENT!r} not found in response. "
            f"Body snippet: {resp.text[:1000]!r}"
        )

    @pytest.mark.slow
    @pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
    def test_post_with_invalid_idle_timeout_shows_error(
        self, client, admin_session, config_csrf_token
    ):
        """idle_timeout=0 must show the specific idle-timeout validation message.

        The exact fragment 'elevation_idle_timeout_seconds must be between' comes
        directly from _update_totp_elevation_setting raising ValueError when the
        value is outside [60, 3600]. This proves the idle-timeout constraint fired
        (not a generic DB error or an unrelated failure path).
        """
        resp = _post_totp_form(
            client, admin_session, config_csrf_token, _INVALID_IDLE_FORM
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert _IDLE_TIMEOUT_VALIDATION_FRAGMENT in resp.text, (
            f"Expected idle-timeout validation message "
            f"{_IDLE_TIMEOUT_VALIDATION_FRAGMENT!r} in response. "
            f"Body snippet: {resp.text[:1000]!r}"
        )
        assert _SUCCESS_FRAGMENT not in resp.text, (
            "Success message must NOT appear when idle-timeout validation fails"
        )

    @pytest.mark.slow
    @pytest.mark.timeout(_TEST_TIMEOUT_SECONDS)
    def test_post_without_csrf_rejected(self, client, admin_session):
        """POST with a blank CSRF token must be rejected with the CSRF error message."""
        resp = _post_totp_form(
            client, admin_session, csrf_token="", form_data=_VALID_FORM
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        assert _CSRF_ERROR_FRAGMENT in resp.text, (
            f"{_CSRF_ERROR_FRAGMENT!r} not found in response. "
            f"Body snippet: {resp.text[:1000]!r}"
        )
        assert _SUCCESS_FRAGMENT not in resp.text, (
            "Success message must NOT appear when CSRF is rejected"
        )
