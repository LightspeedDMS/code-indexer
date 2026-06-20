"""Unit tests for query analytics export REST endpoints (Issue #1160).

Endpoints under test:
  POST /api/admin/search-events/export      -> 202 {job_id}
  GET  /api/admin/search-events/exports     -> 200 [{id, status, ...}]
  GET  /api/admin/search-events/exports/{id}/download -> 200 file / 404 / 409

Auth: FastAPI dependency_overrides with mock admin user (no real credentials).
Unauthenticated requests return exactly HTTP 401.
"""

import threading
import time
import uuid
from unittest.mock import Mock, patch

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
