"""
TestClient HTTP-level route tests for backfill journal HTMX partials (Story #1062).

Tests:
  GET /admin/partials/lifecycle-backfill-journal
  GET /admin/partials/description-backfill-journal

For each route:
  - admin session → HTTP 200 + ALL 6 X-Backfill-* headers present + correct values
    from a seeded sidecar (_status.json written to the route's journal dir)
  - non-admin (unauthenticated) → HTTP 401 + empty body

Auth gate is the real session_manager (same pattern as test_depmap_activity_journal_endpoint.py).
Sidecar seeding patches the service factory so the route uses a known-state
BackfillJournalService pointed at a tmp dir with a pre-written _status.json.
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services.backfill_journal_service import BackfillJournalService

# ─────────────────────────────────────────────────────────────────────────────
# Module-level constants: the 6 expected response headers
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_HEADERS = {
    "x-journal-offset",
    "x-backfill-active",
    "x-backfill-total",
    "x-backfill-done",
    "x-backfill-failed",
    "x-backfill-completed-at",
}


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures (mirror test_depmap_activity_journal_endpoint.py)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app():
    """Create FastAPI app with minimal startup."""
    from code_indexer.server.app import app as _app

    return _app


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def admin_session_cookie(client):
    """Get admin session cookie via form-based login."""
    login_page = client.get("/login")
    assert login_page.status_code == 200
    match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert match, "Could not extract CSRF token from login page"
    csrf_token = match.group(1)

    login_resp = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 303, f"Form login failed: {login_resp.status_code}"
    assert "session" in login_resp.cookies, "No session cookie set by form login"

    for name, value in login_resp.cookies.items():
        client.cookies.set(name, value)
    return login_resp.cookies


@pytest.fixture
def seeded_journal_dir(tmp_path: Path):
    """Return a tmp journal dir with a pre-written _status.json sidecar (running=True)."""
    journal_dir = tmp_path / "seeded-backfill-journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    sidecar = journal_dir / "_status.json"
    sidecar.write_text(
        json.dumps(
            {
                "running": True,
                "started_at": "2026-06-05T10:00:00+00:00",
                "completed_at": None,
                "total": 7,
                "done": 3,
                "failed": 1,
            }
        ),
        encoding="utf-8",
    )
    return journal_dir


def _make_seeded_svc(namespace: str, journal_dir: Path) -> BackfillJournalService:
    """Build a BackfillJournalService whose sidecar has already been seeded."""
    return BackfillJournalService(namespace=namespace, journal_dir=journal_dir)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: assert all 6 X-Backfill-* + X-Journal-Offset headers present
# ─────────────────────────────────────────────────────────────────────────────


def _assert_all_headers(headers_lower: dict) -> None:
    missing = EXPECTED_HEADERS - set(headers_lower.keys())
    assert not missing, f"Missing response headers: {missing}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: /admin/partials/lifecycle-backfill-journal
# ─────────────────────────────────────────────────────────────────────────────


class TestLifecycleBackfillJournalRoute:
    ENDPOINT = "/admin/partials/lifecycle-backfill-journal"

    def test_admin_gets_200(self, client, admin_session_cookie, seeded_journal_dir):
        """Admin session → HTTP 200."""
        seeded_svc = _make_seeded_svc("lifecycle", seeded_journal_dir)
        with patch(
            "code_indexer.server.web.dependency_map_routes._lifecycle_backfill_journal_svc",
            return_value=seeded_svc,
        ):
            resp = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert resp.status_code == 200

    def test_admin_gets_all_6_headers(
        self, client, admin_session_cookie, seeded_journal_dir
    ):
        """Admin session → all 6 X-Backfill-* + X-Journal-Offset headers present."""
        seeded_svc = _make_seeded_svc("lifecycle", seeded_journal_dir)
        with patch(
            "code_indexer.server.web.dependency_map_routes._lifecycle_backfill_journal_svc",
            return_value=seeded_svc,
        ):
            resp = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert resp.status_code == 200
        headers_lower = {k.lower(): v for k, v in resp.headers.items()}
        _assert_all_headers(headers_lower)

    def test_admin_headers_reflect_seeded_sidecar(
        self, client, admin_session_cookie, seeded_journal_dir
    ):
        """Header values match the seeded _status.json (total=7, done=3, failed=1, active=1)."""
        seeded_svc = _make_seeded_svc("lifecycle", seeded_journal_dir)
        with patch(
            "code_indexer.server.web.dependency_map_routes._lifecycle_backfill_journal_svc",
            return_value=seeded_svc,
        ):
            resp = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert resp.status_code == 200
        headers_lower = {k.lower(): v for k, v in resp.headers.items()}
        assert headers_lower["x-backfill-active"] == "1", "running=True → active=1"
        assert headers_lower["x-backfill-total"] == "7"
        assert headers_lower["x-backfill-done"] == "3"
        assert headers_lower["x-backfill-failed"] == "1"
        assert (
            headers_lower["x-backfill-completed-at"] == ""
        )  # running, no completed_at
        # Offset is numeric
        assert headers_lower["x-journal-offset"].isdigit()

    def test_admin_offset_override_accepted(
        self, client, admin_session_cookie, seeded_journal_dir
    ):
        """?offset= query param is accepted without error."""
        seeded_svc = _make_seeded_svc("lifecycle", seeded_journal_dir)
        with patch(
            "code_indexer.server.web.dependency_map_routes._lifecycle_backfill_journal_svc",
            return_value=seeded_svc,
        ):
            resp = client.get(
                f"{self.ENDPOINT}?offset=100", cookies=admin_session_cookie
            )
        assert resp.status_code == 200

    def test_non_admin_gets_401_empty_body(self, client):
        """Unauthenticated request → 401 with empty body."""
        resp = client.get(self.ENDPOINT, follow_redirects=False)
        assert resp.status_code == 401
        assert resp.text == ""


# ─────────────────────────────────────────────────────────────────────────────
# Tests: /admin/partials/description-backfill-journal
# ─────────────────────────────────────────────────────────────────────────────


class TestDescriptionBackfillJournalRoute:
    ENDPOINT = "/admin/partials/description-backfill-journal"

    def test_admin_gets_200(self, client, admin_session_cookie, seeded_journal_dir):
        """Admin session → HTTP 200."""
        seeded_svc = _make_seeded_svc("description", seeded_journal_dir)
        with patch(
            "code_indexer.server.web.dependency_map_routes._description_backfill_journal_svc",
            return_value=seeded_svc,
        ):
            resp = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert resp.status_code == 200

    def test_admin_gets_all_6_headers(
        self, client, admin_session_cookie, seeded_journal_dir
    ):
        """Admin session → all 6 X-Backfill-* + X-Journal-Offset headers present."""
        seeded_svc = _make_seeded_svc("description", seeded_journal_dir)
        with patch(
            "code_indexer.server.web.dependency_map_routes._description_backfill_journal_svc",
            return_value=seeded_svc,
        ):
            resp = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert resp.status_code == 200
        headers_lower = {k.lower(): v for k, v in resp.headers.items()}
        _assert_all_headers(headers_lower)

    def test_admin_headers_reflect_seeded_sidecar(
        self, client, admin_session_cookie, seeded_journal_dir
    ):
        """Header values match the seeded _status.json (total=7, done=3, failed=1, active=1)."""
        seeded_svc = _make_seeded_svc("description", seeded_journal_dir)
        with patch(
            "code_indexer.server.web.dependency_map_routes._description_backfill_journal_svc",
            return_value=seeded_svc,
        ):
            resp = client.get(self.ENDPOINT, cookies=admin_session_cookie)
        assert resp.status_code == 200
        headers_lower = {k.lower(): v for k, v in resp.headers.items()}
        assert headers_lower["x-backfill-active"] == "1", "running=True → active=1"
        assert headers_lower["x-backfill-total"] == "7"
        assert headers_lower["x-backfill-done"] == "3"
        assert headers_lower["x-backfill-failed"] == "1"
        assert headers_lower["x-backfill-completed-at"] == ""
        assert headers_lower["x-journal-offset"].isdigit()

    def test_admin_offset_override_accepted(
        self, client, admin_session_cookie, seeded_journal_dir
    ):
        """?offset= query param is accepted without error."""
        seeded_svc = _make_seeded_svc("description", seeded_journal_dir)
        with patch(
            "code_indexer.server.web.dependency_map_routes._description_backfill_journal_svc",
            return_value=seeded_svc,
        ):
            resp = client.get(
                f"{self.ENDPOINT}?offset=50", cookies=admin_session_cookie
            )
        assert resp.status_code == 200

    def test_non_admin_gets_401_empty_body(self, client):
        """Unauthenticated request → 401 with empty body."""
        resp = client.get(self.ENDPOINT, follow_redirects=False)
        assert resp.status_code == 401
        assert resp.text == ""
