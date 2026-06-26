"""Unit tests for query analytics export REST endpoints (Issue #1160).

Endpoints under test:
  POST /api/admin/search-events/export      -> 202 {job_id}
  GET  /api/admin/search-events/exports     -> 200 [{id, status, ...}]
  GET  /api/admin/search-events/exports/{id}/download -> 200 file / 404 / 409
  GET  /analytics-export                    -> 200 rendered HTML page

Auth: FastAPI dependency_overrides with mock admin user (no real credentials).
Unauthenticated requests return exactly HTTP 401.
"""

import threading
import time
import uuid
from unittest.mock import Mock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportService,
    QueryAnalyticsExportSqliteBackend,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_admin_user() -> Mock:
    user = Mock()
    user.username = "testadmin"
    user.role = UserRole.ADMIN
    user.email = "testadmin@example.com"
    return user


def _valid_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def export_service(tmp_path):
    """Real QueryAnalyticsExportService backed by an in-memory SQLite DB."""
    db_path = str(tmp_path / "test_exports.db")
    backend = QueryAnalyticsExportSqliteBackend(db_path)
    svc = QueryAnalyticsExportService(
        backend=backend,
        golden_repos_dir=str(tmp_path),
    )
    return svc


@pytest.fixture
def app_with_export_service(export_service):
    """App with admin dependency overrides and export service wired into app.state."""
    from code_indexer.server.app import app

    admin_user = _make_admin_user()
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[get_current_admin_user] = lambda: admin_user
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
    app.state.query_analytics_export_service = export_service
    # Ensure search_event_log_writer is set (needed if search-events endpoint is loaded)
    if not hasattr(app.state, "search_event_log_writer"):
        app.state.search_event_log_writer = None

    yield app, export_service

    app.dependency_overrides.clear()
    app.state.query_analytics_export_service = None


@pytest.fixture
def client(app_with_export_service):
    """TestClient with admin auth and export service pre-wired."""
    app, svc = app_with_export_service
    return TestClient(app, raise_server_exceptions=True), svc


# ---------------------------------------------------------------------------
# Tests: POST /api/admin/search-events/export (trigger export job)
# ---------------------------------------------------------------------------


class TestPostExport:
    def test_returns_202_with_job_id(self, client):
        """POST export returns HTTP 202 Accepted with a job_id."""
        http_client, _ = client
        resp = http_client.post("/api/admin/search-events/export", json={})
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "job_id" in body, f"Missing job_id in: {body}"
        assert body["job_id"]  # non-empty

    def test_unauthenticated_returns_401(self):
        """Without auth credentials, POST export must return exactly 401."""
        from code_indexer.server.app import app

        app.dependency_overrides.clear()
        http_client = TestClient(app, raise_server_exceptions=False)
        resp = http_client.post("/api/admin/search-events/export", json={})
        assert resp.status_code == 401, (
            f"Expected 401 for unauthenticated POST export, got {resp.status_code}: {resp.text}"
        )

    def test_accepts_filter_parameters(self, client):
        """POST export with filter params returns 202 (filters are forwarded to worker)."""
        http_client, _ = client
        filters = {
            "username": "alice",
            "search_type": "semantic",
            "repo_alias": "my-repo",
            "from_ts": 1000.0,
            "to_ts": 2000.0,
        }
        resp = http_client.post("/api/admin/search-events/export", json=filters)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert "job_id" in body

    def test_returns_503_when_service_not_initialized(self):
        """POST export returns 503 when query_analytics_export_service is None."""
        from code_indexer.server.app import app

        admin_user = _make_admin_user()
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
        app.state.query_analytics_export_service = None

        try:
            http_client = TestClient(app, raise_server_exceptions=False)
            resp = http_client.post("/api/admin/search-events/export", json={})
            assert resp.status_code == 503, (
                f"Expected 503 when export service is None, got {resp.status_code}: {resp.text}"
            )
        finally:
            app.dependency_overrides.clear()
            app.state.query_analytics_export_service = None

    def test_export_trigger_reads_json_body_not_query_params(self, client):
        """POST export reads filter values from JSON body, not query params.

        The handler must bind a Pydantic body model so that fields like
        'user', 'from_timestamp', and 'cache_hit_filter' come from the JSON
        body, not from URL query string parameters.
        """
        http_client, export_svc = client
        captured_filters: dict = {}
        called_event = threading.Event()

        original_run_export = export_svc.run_export

        def capturing_run_export(**kwargs):
            captured_filters.update(kwargs.get("filters", {}))
            called_event.set()
            return original_run_export(**kwargs)

        patcher = patch.object(
            export_svc, "run_export", side_effect=capturing_run_export
        )
        patcher.start()
        try:
            resp = http_client.post(
                "/api/admin/search-events/export",
                json={
                    "user": "alice",
                    "from_timestamp": 1000.0,
                    "cache_hit_filter": "hits_only",
                },
            )
            assert resp.status_code == 202, resp.text
            called_event.wait(timeout=5)
        finally:
            patcher.stop()

        assert called_event.is_set(), (
            "run_export was never called by the background worker"
        )
        assert captured_filters.get("user") == "alice", (
            f"Expected filters['user']='alice', got: {captured_filters}"
        )
        assert captured_filters.get("from_timestamp") == 1000.0, (
            f"Expected filters['from_timestamp']=1000.0, got: {captured_filters}"
        )
        assert captured_filters.get("cache_hit_filter") == "hits_only", (
            f"Expected filters['cache_hit_filter']='hits_only', got: {captured_filters}"
        )

    def test_export_trigger_filter_keys_match_service_contract(self, client):
        """All six filter keys passed to run_export match the service contract.

        Service expects: user, search_type, repo_alias, from_timestamp,
        to_timestamp, cache_hit_filter — NOT username/from_ts/to_ts.
        """
        http_client, export_svc = client
        captured_filters: dict = {}
        called_event = threading.Event()

        original_run_export = export_svc.run_export

        def capturing_run_export(**kwargs):
            captured_filters.update(kwargs.get("filters", {}))
            called_event.set()
            return original_run_export(**kwargs)

        body = {
            "user": "bob",
            "search_type": "semantic",
            "repo_alias": "my-repo",
            "from_timestamp": 1000.0,
            "to_timestamp": 2000.0,
            "cache_hit_filter": "misses_only",
        }

        patcher = patch.object(
            export_svc, "run_export", side_effect=capturing_run_export
        )
        patcher.start()
        try:
            resp = http_client.post("/api/admin/search-events/export", json=body)
            assert resp.status_code == 202, resp.text
            called_event.wait(timeout=5)
        finally:
            patcher.stop()

        assert called_event.is_set(), (
            "run_export was never called by the background worker"
        )

        assert captured_filters.get("user") == "bob", f"Got: {captured_filters}"
        assert captured_filters.get("search_type") == "semantic", (
            f"Got: {captured_filters}"
        )
        assert captured_filters.get("repo_alias") == "my-repo", (
            f"Got: {captured_filters}"
        )
        assert captured_filters.get("from_timestamp") == 1000.0, (
            f"Got: {captured_filters}"
        )
        assert captured_filters.get("to_timestamp") == 2000.0, (
            f"Got: {captured_filters}"
        )
        assert captured_filters.get("cache_hit_filter") == "misses_only", (
            f"Got: {captured_filters}"
        )

        assert "username" not in captured_filters, (
            f"Old key 'username' must not appear in filters: {captured_filters}"
        )
        assert "from_ts" not in captured_filters, (
            f"Old key 'from_ts' must not appear in filters: {captured_filters}"
        )
        assert "to_ts" not in captured_filters, (
            f"Old key 'to_ts' must not appear in filters: {captured_filters}"
        )

    def test_export_trigger_cache_hit_filter_defaults_to_all(self, client):
        """When cache_hit_filter is omitted, the service receives 'all' not None."""
        http_client, export_svc = client
        captured_filters: dict = {}
        called_event = threading.Event()

        original_run_export = export_svc.run_export

        def capturing_run_export(**kwargs):
            captured_filters.update(kwargs.get("filters", {}))
            called_event.set()
            return original_run_export(**kwargs)

        patcher = patch.object(
            export_svc, "run_export", side_effect=capturing_run_export
        )
        patcher.start()
        try:
            resp = http_client.post("/api/admin/search-events/export", json={})
            assert resp.status_code == 202, resp.text
            called_event.wait(timeout=5)
        finally:
            patcher.stop()

        assert called_event.is_set(), (
            "run_export was never called by the background worker"
        )
        assert captured_filters.get("cache_hit_filter") == "all", (
            f"Expected cache_hit_filter='all' when omitted, got: {captured_filters}"
        )


# ---------------------------------------------------------------------------
# Tests: GET /api/admin/search-events/exports (list exports)
# ---------------------------------------------------------------------------


class TestGetExports:
    def test_returns_200_with_empty_list(self, client):
        """GET exports returns 200 with empty list when no exports exist."""
        http_client, _ = client
        resp = http_client.get("/api/admin/search-events/exports")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "exports" in body, f"Missing 'exports' key in: {body}"
        assert body["exports"] == []

    def test_unauthenticated_returns_401(self):
        """Without auth credentials, GET exports must return exactly 401."""
        from code_indexer.server.app import app

        app.dependency_overrides.clear()
        http_client = TestClient(app, raise_server_exceptions=False)
        resp = http_client.get("/api/admin/search-events/exports")
        assert resp.status_code == 401, (
            f"Expected 401 for unauthenticated GET exports, got {resp.status_code}: {resp.text}"
        )

    def test_lists_existing_export_rows(self, client):
        """GET exports returns rows previously inserted into the backend."""
        http_client, svc = client
        export_id = _valid_uuid()
        now = time.time()
        # Insert a completed export row directly into the backend
        svc._backend.create_export(
            {
                "id": export_id,
                "initiated_by": "alice",
                "created_at": now,
                "status": "completed",
                "filter_summary": "No filters",
                "retention_until": now + 86400,
            }
        )
        svc._backend.update_export(
            export_id,
            status="completed",
            file_path="/some/path.xlsx",
            file_size_bytes=1024,
            row_count=10,
        )
        resp = http_client.get("/api/admin/search-events/exports")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["exports"]) == 1
        row = body["exports"][0]
        assert row["id"] == export_id
        assert row["status"] == "completed"
        assert row["initiated_by"] == "alice"

    def test_response_includes_download_link_for_completed(self, client):
        """Completed exports include a non-empty download_link in the response."""
        http_client, svc = client
        export_id = _valid_uuid()
        now = time.time()
        svc._backend.create_export(
            {
                "id": export_id,
                "initiated_by": "testadmin",
                "created_at": now,
                "status": "completed",
                "filter_summary": "No filters",
                "retention_until": now + 86400,
            }
        )
        svc._backend.update_export(
            export_id,
            status="completed",
            file_path="/some/path.xlsx",
        )
        resp = http_client.get("/api/admin/search-events/exports")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["exports"]) == 1
        row = body["exports"][0]
        assert "download_link" in row, f"Missing download_link in: {row}"
        assert row["download_link"], (
            "download_link must be non-empty for completed export"
        )
        assert export_id in row["download_link"]

    def test_pending_export_has_no_download_link(self, client):
        """Pending exports have download_link as None or empty."""
        http_client, svc = client
        export_id = _valid_uuid()
        now = time.time()
        svc._backend.create_export(
            {
                "id": export_id,
                "initiated_by": "testadmin",
                "created_at": now,
                "status": "pending",
                "filter_summary": "No filters",
                "retention_until": now + 86400,
            }
        )
        resp = http_client.get("/api/admin/search-events/exports")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        row = body["exports"][0]
        # pending exports should not have a usable download link
        assert not row.get("download_link"), (
            f"Pending export must not have download_link, got: {row.get('download_link')}"
        )

    def test_returns_503_when_service_not_initialized(self):
        """GET exports returns 503 when query_analytics_export_service is None."""
        from code_indexer.server.app import app

        admin_user = _make_admin_user()
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
        app.state.query_analytics_export_service = None

        try:
            http_client = TestClient(app, raise_server_exceptions=False)
            resp = http_client.get("/api/admin/search-events/exports")
            assert resp.status_code == 503, (
                f"Expected 503 when export service is None, got {resp.status_code}: {resp.text}"
            )
        finally:
            app.dependency_overrides.clear()
            app.state.query_analytics_export_service = None


# ---------------------------------------------------------------------------
# Tests: GET /api/admin/search-events/exports/{id}/download
# ---------------------------------------------------------------------------


class TestDownloadExport:
    def test_unauthenticated_returns_401(self):
        """Without auth credentials, download endpoint must return exactly 401."""
        from code_indexer.server.app import app

        app.dependency_overrides.clear()
        http_client = TestClient(app, raise_server_exceptions=False)
        fake_id = _valid_uuid()
        resp = http_client.get(f"/api/admin/search-events/exports/{fake_id}/download")
        assert resp.status_code == 401, (
            f"Expected 401 for unauthenticated download, got {resp.status_code}: {resp.text}"
        )

    def test_returns_404_for_unknown_export_id(self, client):
        """Download for an unknown export ID returns 404."""
        http_client, _ = client
        fake_id = _valid_uuid()
        resp = http_client.get(
            f"/api/admin/search-events/exports/{fake_id}/download",
        )
        assert resp.status_code == 404, (
            f"Expected 404 for unknown export ID, got {resp.status_code}: {resp.text}"
        )

    def test_returns_409_for_pending_export(self, client):
        """Download for a pending export returns 409 Conflict."""
        http_client, svc = client
        export_id = _valid_uuid()
        now = time.time()
        svc._backend.create_export(
            {
                "id": export_id,
                "initiated_by": "testadmin",
                "created_at": now,
                "status": "pending",
                "filter_summary": "No filters",
                "retention_until": now + 86400,
            }
        )
        resp = http_client.get(
            f"/api/admin/search-events/exports/{export_id}/download",
        )
        assert resp.status_code == 409, (
            f"Expected 409 for pending export, got {resp.status_code}: {resp.text}"
        )

    def test_returns_409_for_running_export(self, client):
        """Download for a running export returns 409 Conflict."""
        http_client, svc = client
        export_id = _valid_uuid()
        now = time.time()
        svc._backend.create_export(
            {
                "id": export_id,
                "initiated_by": "testadmin",
                "created_at": now,
                "status": "running",
                "filter_summary": "No filters",
                "retention_until": now + 86400,
            }
        )
        resp = http_client.get(
            f"/api/admin/search-events/exports/{export_id}/download",
        )
        assert resp.status_code == 409, (
            f"Expected 409 for running export, got {resp.status_code}: {resp.text}"
        )

    def test_returns_409_for_failed_export(self, client):
        """Download for a failed export returns 409 Conflict."""
        http_client, svc = client
        export_id = _valid_uuid()
        now = time.time()
        svc._backend.create_export(
            {
                "id": export_id,
                "initiated_by": "testadmin",
                "created_at": now,
                "status": "failed",
                "filter_summary": "No filters",
                "retention_until": now + 86400,
            }
        )
        resp = http_client.get(
            f"/api/admin/search-events/exports/{export_id}/download",
        )
        assert resp.status_code == 409, (
            f"Expected 409 for failed export, got {resp.status_code}: {resp.text}"
        )

    def test_returns_200_for_completed_export_with_file(self, client, tmp_path):
        """Download for a completed export with an existing file returns 200."""
        http_client, svc = client
        export_id = _valid_uuid()
        now = time.time()

        # Create a real xlsx file at the export path
        export_file = svc.export_path(export_id)
        export_file.parent.mkdir(parents=True, exist_ok=True)
        export_file.write_bytes(b"PK fake xlsx content")

        svc._backend.create_export(
            {
                "id": export_id,
                "initiated_by": "testadmin",
                "created_at": now,
                "status": "completed",
                "filter_summary": "No filters",
                "retention_until": now + 86400,
            }
        )
        svc._backend.update_export(
            export_id,
            status="completed",
            file_path=str(export_file),
            file_size_bytes=20,
            row_count=0,
        )
        resp = http_client.get(
            f"/api/admin/search-events/exports/{export_id}/download",
        )
        assert resp.status_code == 200, (
            f"Expected 200 for completed export with file, got {resp.status_code}: {resp.text}"
        )
        # Should return spreadsheet content-type
        content_type = resp.headers.get("content-type", "")
        assert "spreadsheetml" in content_type or "octet-stream" in content_type, (
            f"Expected xlsx content-type, got: {content_type}"
        )

    def test_returns_503_when_service_not_initialized(self):
        """Download endpoint returns 503 when export service is None."""
        from code_indexer.server.app import app

        admin_user = _make_admin_user()
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
        app.state.query_analytics_export_service = None

        try:
            fake_id = _valid_uuid()
            http_client = TestClient(app, raise_server_exceptions=False)
            resp = http_client.get(
                f"/api/admin/search-events/exports/{fake_id}/download"
            )
            assert resp.status_code == 503, (
                f"Expected 503 when export service is None, got {resp.status_code}: {resp.text}"
            )
        finally:
            app.dependency_overrides.clear()
            app.state.query_analytics_export_service = None


# ---------------------------------------------------------------------------
# Tests: GET /analytics-export page render (datetime-local UX fix)
# ---------------------------------------------------------------------------


def _make_admin_session_mock():
    """Return a mock SessionManager whose get_session() returns an admin session.

    The analytics_export_page route calls:
      1. _require_admin_session(request) -> get_session_manager().get_session(request)
      2. get_csrf_token_from_cookie(request) -> _get_csrf_serializer() ->
             get_session_manager()._serializer.secret_key
      3. set_csrf_cookie(response, ...) -> _get_csrf_serializer() -> same path

    We patch code_indexer.server.web.routes.get_session_manager to return this
    mock so the route renders the template without redirecting to login or
    raising AttributeError on _serializer.secret_key.
    """
    session = MagicMock()
    session.role = "admin"
    session.username = "testadmin"
    session.csrf_token = "test_csrf"

    # _get_csrf_serializer() accesses session_manager._serializer.secret_key
    # It must be a real string for URLSafeTimedSerializer to accept it.
    serializer_mock = MagicMock()
    serializer_mock.secret_keys = ["test-secret-key-for-csrf-unit-tests"]
    serializer_mock.secret_key = "test-secret-key-for-csrf-unit-tests"

    session_manager = MagicMock()
    session_manager.get_session.return_value = session
    session_manager._serializer = serializer_mock
    return session_manager


class TestAnalyticsExportPageRender:
    """GET /analytics-export must render datetime-local pickers (not raw epoch inputs).

    These tests verify the UX fix: the From/To filter inputs are
    type="datetime-local" (datetime pickers) instead of type="number"
    (raw UTC epoch number fields).
    """

    def _get_page_html(self) -> str:
        """Render GET /admin/analytics-export with a mocked admin session and return HTML."""
        from code_indexer.server.app import app

        with patch(
            "code_indexer.server.web.routes.get_session_manager",
            return_value=_make_admin_session_mock(),
        ):
            http_client = TestClient(app, raise_server_exceptions=True)
            resp = http_client.get("/admin/analytics-export")
        assert resp.status_code == 200, (
            f"Expected 200 from /admin/analytics-export, got {resp.status_code}: {resp.text[:300]}"
        )
        return resp.text

    def test_page_renders_200(self):
        """GET /admin/analytics-export returns HTTP 200 with an authenticated admin session."""
        self._get_page_html()  # asserts 200 internally

    def test_from_input_is_datetime_local(self):
        """The From filter input must be type=\"datetime-local\", not type=\"number\"."""
        html = self._get_page_html()
        assert 'id="from_timestamp"' in html, (
            "from_timestamp input not found in rendered HTML"
        )
        assert 'type="datetime-local"' in html, (
            'Expected type="datetime-local" in rendered analytics-export HTML. '
            "The From/To epoch number inputs should have been replaced with datetime pickers."
        )

    def test_to_input_is_datetime_local(self):
        """Both From and To inputs must use type=\"datetime-local\"."""
        html = self._get_page_html()
        assert 'id="to_timestamp"' in html, (
            "to_timestamp input not found in rendered HTML"
        )
        count = html.count('type="datetime-local"')
        assert count >= 2, (
            f'Expected at least 2 type="datetime-local" inputs (From and To), '
            f"found {count} in rendered HTML."
        )

    def test_no_raw_epoch_number_inputs(self):
        """The rendered HTML must NOT contain the old epoch placeholder text.

        This is the regression guard: the old epoch number inputs with their
        specific placeholder values must be gone after the datetime-local fix.
        """
        html = self._get_page_html()
        assert 'placeholder="e.g. 1750000000"' not in html, (
            "Old epoch placeholder 'e.g. 1750000000' found in rendered HTML. "
            "The from_timestamp input was not updated to datetime-local."
        )
        assert 'placeholder="e.g. 1759999999"' not in html, (
            "Old epoch placeholder 'e.g. 1759999999' found in rendered HTML. "
            "The to_timestamp input was not updated to datetime-local."
        )
