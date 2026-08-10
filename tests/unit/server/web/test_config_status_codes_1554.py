"""Issue #1554: /admin/config/{section} (and sibling config routes) must return
the correct HTTP status code when a write is REJECTED, not a blanket 200.

Root cause: `_create_config_page_response()` builds a `TemplateResponse` with no
`status_code` parameter, so every response it produces -- success AND rejection
alike -- is HTTP 200. The failure was only observable by parsing the rendered
HTML body for a string like "Invalid CSRF token". Any caller that checks the
status code (the normal, correct thing to do) reads a rejected write as a
successful one. This violates the project's anti-silent-failure rule (Messi
Rule #13): a failed operation must not be indistinguishable from a successful
one.

These tests exercise the REAL FastAPI app via TestClient (real request/response,
no mocking of the response object) and assert on the genuine HTTP status code
of genuinely rejected writes -- a mismatched CSRF token, an unknown config
section, and a field that fails `_validate_config_section` -- plus a regression
guard that the SUCCESS path still returns 200.
"""

from __future__ import annotations

import re
import secrets
import string
import tempfile
from pathlib import Path
from typing import Dict, Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

_TOKEN_USERNAME_BYTES = 8
_TEST_TIMEOUT = 60


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
    username = secrets.token_hex(_TOKEN_USERNAME_BYTES)
    password = _make_test_password()
    created_user = um.create_user(
        username=username, password=password, role=UserRole.ADMIN
    )
    assert created_user.username == username, (
        f"User creation must report the requested username back; "
        f"got {created_user.username!r}"
    )

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
    """A VALID csrf token, scraped from the real /admin/config page, paired
    with the real signed csrf cookie the TestClient already holds."""
    resp = client.get("/admin/config", cookies=admin_session)
    assert resp.status_code == 200
    return _scrape_csrf_token(resp.text)


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestCSRFRejectionStatusCode:
    def test_mismatched_csrf_token_returns_403(
        self, client, admin_session, config_csrf
    ) -> None:
        """A genuinely rejected write (submitted CSRF token does not match the
        signed CSRF cookie) must return 403, not 200. This is the exact
        scenario from Issue #1554: a caller checking only the status code
        must be able to tell the write was rejected."""
        resp = client.post(
            "/admin/config/search_event_log",
            data={
                "csrf_token": "this-does-not-match-the-real-cookie-value",
                "search_event_log_retention_days": "30",
            },
            cookies=admin_session,
            follow_redirects=True,
        )
        assert resp.status_code == 403, (
            f"Issue #1554: rejected write (bad CSRF token) must return 403, "
            f"got {resp.status_code}. Body: {resp.text[:300]}"
        )
        assert "invalid csrf token" in resp.text.lower()

    def test_absent_csrf_token_returns_403(
        self, client, admin_session, config_csrf
    ) -> None:
        """A POST with NO csrf_token field at all is also a genuine rejection
        and must not be indistinguishable (via status code) from success."""
        resp = client.post(
            "/admin/config/search_event_log",
            data={"search_event_log_retention_days": "30"},
            cookies=admin_session,
            follow_redirects=True,
        )
        assert resp.status_code == 403, (
            f"Issue #1554: rejected write (absent CSRF token) must return 403, "
            f"got {resp.status_code}. Body: {resp.text[:300]}"
        )


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestInvalidSectionStatusCode:
    def test_unknown_section_returns_400(
        self, client, admin_session, config_csrf
    ) -> None:
        resp = client.post(
            "/admin/config/this_section_does_not_exist_1554",
            data={"csrf_token": config_csrf, "foo": "bar"},
            cookies=admin_session,
            follow_redirects=True,
        )
        assert resp.status_code == 400, (
            f"Issue #1554: unknown config section must return 400, "
            f"got {resp.status_code}. Body: {resp.text[:300]}"
        )
        assert "invalid section" in resp.text.lower()


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestFieldValidationErrorStatusCode:
    def test_out_of_range_field_returns_400(
        self, client, admin_session, config_csrf
    ) -> None:
        """search_event_log_retention_days must be between 1 and 3650
        (_validate_config_section). A rejected, out-of-range value must
        return 400, not the previous blanket 200."""
        resp = client.post(
            "/admin/config/search_event_log",
            data={
                "csrf_token": config_csrf,
                "search_event_log_retention_days": "999999",
            },
            cookies=admin_session,
            follow_redirects=True,
        )
        assert resp.status_code == 400, (
            f"Issue #1554: field validation failure must return 400, "
            f"got {resp.status_code}. Body: {resp.text[:300]}"
        )
        assert "must be between 1 and 3650" in resp.text.lower()


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestSuccessPathStatusCode:
    def test_valid_update_still_returns_200(
        self, client, admin_session, config_csrf
    ) -> None:
        """Regression guard: the SUCCESS path must remain 200 -- the fix must
        not turn a legitimate, accepted write into a non-200 response."""
        resp = client.post(
            "/admin/config/search_event_log",
            data={
                "csrf_token": config_csrf,
                "search_event_log_retention_days": "30",
            },
            cookies=admin_session,
            follow_redirects=True,
        )
        assert resp.status_code == 200, (
            f"Issue #1554: a valid, accepted write must still return 200, "
            f"got {resp.status_code}. Body: {resp.text[:300]}"
        )
        assert "saved successfully" in resp.text.lower()


_SIBLING_ROUTES: Tuple[Tuple[str, Dict[str, str]], ...] = (
    ("/admin/config/reset", {}),
    ("/admin/config/langfuse_pull", {"pull_enabled": "false"}),
    ("/admin/config/cidx_meta_backup", {"enabled": "false"}),
)


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestSiblingRoutesCSRFStatusCode:
    """The comments in routes.py mark /config/reset, /config/langfuse_pull,
    and /config/cidx_meta_backup as deliberately declared BEFORE
    /config/{section} to avoid being shadowed -- they share the same
    _create_config_page_response() defect and must be fixed identically."""

    @pytest.mark.parametrize("route,extra_fields", _SIBLING_ROUTES)
    def test_mismatched_csrf_returns_403(
        self, client, admin_session, config_csrf, route, extra_fields
    ) -> None:
        resp = client.post(
            route,
            data={"csrf_token": "wrong-token-value", **extra_fields},
            cookies=admin_session,
            follow_redirects=True,
        )
        assert resp.status_code == 403, (
            f"Issue #1554: {route} rejected write (bad CSRF) must return "
            f"403, got {resp.status_code}. Body: {resp.text[:300]}"
        )


if __name__ == "__main__":
    import pytest as _pytest

    raise SystemExit(_pytest.main([__file__, "-v"]))
