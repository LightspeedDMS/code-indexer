"""Story #1195 AC2: Server-side host/port confirmation guardrail.

Enforcement is SERVER-SIDE inside update_config_section.
Client-side modal is UX-only; direct POST must also be gated.

Behavioral guarantees:
  - Changed host WITHOUT confirm flag → rejected, NOT persisted
  - Changed host WITH confirm flag   → accepted, persisted
  - Same host/port re-save           → allowed without confirm
  - Changing log_level only          → allowed without confirm

Opus nit: pre-change host/port snapshot is read BEFORE persisting.
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

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ROUTES_PATH = _REPO_ROOT / "src" / "code_indexer" / "server" / "web" / "routes.py"

_TOKEN_USERNAME_BYTES = 8
_TEST_TIMEOUT = 60

# Default values from ServerConfig (used as the "unchanged" baseline)
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = "8000"


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


def _post_server_section(client, cookies, csrf_token, form_data):
    data = {"csrf_token": csrf_token, **form_data}
    return client.post(
        "/admin/config/server", data=data, cookies=cookies, follow_redirects=True
    )


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


# ---------------------------------------------------------------------------
# Source guard: pre-change snapshot ordering (Opus nit)
# ---------------------------------------------------------------------------


class TestAC2SourceGuards:
    def test_update_config_section_checks_confirm_flag(self) -> None:
        """update_config_section source must reference a confirm flag for host/port."""
        src = _ROUTES_PATH.read_text()
        fn_start = src.find("async def update_config_section(")
        assert fn_start != -1
        fn_body = src[fn_start : fn_start + 8000]
        assert "confirm_host_port_change" in fn_body, (
            "AC2: update_config_section must check 'confirm_host_port_change'"
        )

    def test_get_config_precedes_update_setting(self) -> None:
        """Opus nit: get_config() must appear BEFORE update_setting() in update_config_section."""
        src = _ROUTES_PATH.read_text()
        fn_start = src.find("async def update_config_section(")
        assert fn_start != -1
        fn_body = src[fn_start : fn_start + 8000]
        # Find positions of relevant calls
        get_pos = fn_body.find("get_config()")
        update_pos = fn_body.find("update_setting(")
        assert get_pos != -1, "get_config() not found in update_config_section"
        assert update_pos != -1, "update_setting() not found in update_config_section"
        assert get_pos < update_pos, (
            "AC2 Opus nit: get_config() (pre-change snapshot) must come BEFORE "
            "update_setting() so we compare against the ORIGINAL persisted value"
        )


# ---------------------------------------------------------------------------
# Behavioral tests (real TestClient + real DB)
# ---------------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timeout(_TEST_TIMEOUT)
class TestAC2Behavioral:
    def _read_persisted_host(self, tmpdir_path) -> str:
        """Re-read config from DB to verify persistence, not in-process cache."""
        from code_indexer.server.services.config_service import (
            get_config_service,
            reset_config_service,
        )

        db_path = str(tmpdir_path / "data" / "cidx_server.db")
        with patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": str(tmpdir_path)}):
            reset_config_service()
            cs = get_config_service()
            cs.initialize_runtime_db(db_path)
            config = cs.get_config()
            host = str(config.host)
            reset_config_service()
        return host

    def test_changed_host_without_confirm_is_rejected_and_not_persisted(
        self, client, admin_session, config_csrf, tmpdir_path
    ) -> None:
        """AC2: Changed host WITHOUT confirm must be rejected and NOT written to DB."""
        changed_host = "192.168.99.1"
        resp = _post_server_section(
            client,
            admin_session,
            config_csrf,
            {
                "host": changed_host,
                "port": _DEFAULT_PORT,
                "workers": "1",
                "log_level": "INFO",
                # NO confirm_host_port_change
            },
        )
        # Response must indicate rejection (explicit error, not silent success)
        assert resp.status_code == 200, f"Unexpected HTTP {resp.status_code}"
        html_lower = resp.text.lower()
        # Must render the exact guardrail rejection message from update_config_section:
        # "Changing host or port requires explicit confirmation — HAProxy backend
        #  and firewall port-lock warning. Please confirm the change via the
        #  confirmation dialog."
        # Assert the distinctive substring "requires explicit confirmation" which
        # only appears on the guardrail rejection path — never on a normal page
        # render.  The previous "host in html_lower or port in html_lower" branches
        # were no-ops: those substrings appear as field labels on every config page.
        assert "requires explicit confirmation" in html_lower, (
            "AC2: CHANGED host without confirm must render the guardrail rejection "
            "message containing 'requires explicit confirmation'. "
            "Got response did not contain this message. "
            f"First 400 chars: {resp.text[:400]}"
        )
        # Critical: the changed value must NOT be persisted to DB
        persisted = self._read_persisted_host(tmpdir_path)
        assert persisted != changed_host, (
            f"AC2: host '{changed_host}' was persisted even without confirm flag — "
            f"server-side guardrail failed. DB shows: {persisted!r}"
        )

    def test_changed_host_with_confirm_is_accepted_and_persisted(
        self, client, admin_session, config_csrf, tmpdir_path
    ) -> None:
        """AC2: Changed host WITH confirm flag must succeed and persist to DB."""
        changed_host = "127.0.0.1"
        resp = _post_server_section(
            client,
            admin_session,
            config_csrf,
            {
                "host": changed_host,
                "port": _DEFAULT_PORT,
                "workers": "1",
                "log_level": "INFO",
                "confirm_host_port_change": "1",
            },
        )
        assert resp.status_code == 200, f"Unexpected HTTP {resp.status_code}"
        # Must show success, not an error
        html_lower = resp.text.lower()
        assert "success" in html_lower or "saved" in html_lower, (
            "AC2: CHANGED host WITH confirm must show a success message"
        )
        # DB must have the new value
        persisted = self._read_persisted_host(tmpdir_path)
        assert persisted == changed_host, (
            f"AC2: host '{changed_host}' was NOT persisted after confirmed change. "
            f"DB shows: {persisted!r}"
        )

    def test_same_host_port_resave_needs_no_confirm(
        self, client, admin_session, config_csrf
    ) -> None:
        """AC2: Re-saving the SAME host/port must succeed without confirm flag."""
        resp = _post_server_section(
            client,
            admin_session,
            config_csrf,
            {
                "host": _DEFAULT_HOST,
                "port": _DEFAULT_PORT,
                "workers": "1",
                "log_level": "INFO",
                # NO confirm — same values, guardrail must NOT fire
            },
        )
        assert resp.status_code == 200
        html_lower = resp.text.lower()
        assert "success" in html_lower or "saved" in html_lower, (
            "AC2: Same host/port re-save must succeed without confirm"
        )

    def test_log_level_change_needs_no_host_port_confirm(
        self, client, admin_session, config_csrf
    ) -> None:
        """AC2: Changing only log_level must succeed without confirm_host_port_change."""
        resp = _post_server_section(
            client,
            admin_session,
            config_csrf,
            {
                "host": _DEFAULT_HOST,
                "port": _DEFAULT_PORT,
                "workers": "1",
                "log_level": "DEBUG",
                # NO confirm — changing log_level only
            },
        )
        assert resp.status_code == 200
        html_lower = resp.text.lower()
        assert "success" in html_lower or "saved" in html_lower, (
            "AC2: log_level change without confirm must succeed"
        )
