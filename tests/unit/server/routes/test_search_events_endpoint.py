"""Unit tests for GET /api/admin/search-events endpoint (Issue #1159).

Tests that the endpoint:
  - Returns events and total_count from the SearchEventLogWriter backend
  - Accepts query parameters: username, search_type, repo_alias, from_ts, to_ts,
    limit (default 100), offset (default 0)
  - Requires admin authentication (401/403 without credentials)
  - Returns 200 with empty list when no events exist
  - Returns 503 when app.state.search_event_log_writer is None (writer not initialized)
  - Reads from app.state.search_event_log_writer.backend.query()

Auth: uses FastAPI dependency_overrides with a mock admin user (no hardcoded credentials).
"""

import time
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import UserRole


# ---------------------------------------------------------------------------
# Helpers: mock admin user
# ---------------------------------------------------------------------------


def _make_admin_user() -> Mock:
    user = Mock()
    user.username = "testadmin"
    user.role = UserRole.ADMIN
    user.email = "testadmin@example.com"
    return user


# ---------------------------------------------------------------------------
# Stub backend and writer for injection into app.state
# ---------------------------------------------------------------------------


class _StubBackend:
    """In-memory backend that supports insert_batch, prune_older_than, and query."""

    def __init__(self) -> None:
        self._rows: List[Dict[str, Any]] = []

    def insert_batch(self, records) -> None:
        for r in records:
            self._rows.append(
                {
                    "id": len(self._rows) + 1,
                    "timestamp": r.timestamp,
                    "username": r.username,
                    "repo_alias": r.repo_alias,
                    "search_type": r.search_type,
                    "query_text": r.query_text,
                    "voyage_cache_hit": r.voyage_cache_hit,
                    "voyage_cache_mode": r.voyage_cache_mode,
                    "voyage_latency_ms": r.voyage_latency_ms,
                    "cohere_cache_hit": r.cohere_cache_hit,
                    "cohere_cache_mode": r.cohere_cache_mode,
                    "cohere_latency_ms": r.cohere_latency_ms,
                    "total_latency_ms": r.total_latency_ms,
                    "result_count": r.result_count,
                    "node_id": r.node_id,
                    "correlation_id": r.correlation_id,
                }
            )

    def prune_older_than(self, cutoff_timestamp: float) -> None:
        self._rows = [r for r in self._rows if r["timestamp"] >= cutoff_timestamp]

    def query(
        self,
        username: Optional[str] = None,
        search_type: Optional[str] = None,
        repo_alias: Optional[str] = None,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
    ):
        rows = list(self._rows)
        if username is not None:
            rows = [r for r in rows if r["username"] == username]
        if search_type is not None:
            rows = [r for r in rows if r["search_type"] == search_type]
        if repo_alias is not None:
            rows = [r for r in rows if r["repo_alias"] == repo_alias]
        if from_ts is not None:
            rows = [r for r in rows if r["timestamp"] >= from_ts]
        if to_ts is not None:
            rows = [r for r in rows if r["timestamp"] < to_ts]
        total = len(rows)
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return rows[offset : offset + limit], total


class _StubWriter:
    """Minimal SearchEventLogWriter stub exposing .backend for test data seeding."""

    def __init__(self) -> None:
        self.backend = _StubBackend()

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 10.0) -> None:
        pass

    def enqueue(self, record) -> None:
        self.backend.insert_batch([record])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_writer():
    """Return (app, stub_writer) with dependency overrides and writer in app.state."""
    from code_indexer.server.app import app

    writer = _StubWriter()
    admin_user = _make_admin_user()

    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[get_current_admin_user] = lambda: admin_user
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
    app.state.search_event_log_writer = writer

    yield app, writer

    app.dependency_overrides.clear()
    app.state.search_event_log_writer = None


@pytest.fixture
def client_and_writer(app_with_writer):
    """Return (TestClient, stub_writer) with admin auth already overridden."""
    app, writer = app_with_writer
    client = TestClient(app, raise_server_exceptions=True)
    return client, writer


def _make_record(
    username: str = "alice",
    repo_alias: Optional[str] = "repo1",
    search_type: str = "semantic",
    query_text: str = "hello world",
    timestamp: float = 0.0,
):
    from code_indexer.server.services.search_event_log_writer import SearchEventRecord

    return SearchEventRecord(
        timestamp=timestamp if timestamp != 0.0 else time.time(),
        username=username,
        repo_alias=repo_alias,
        search_type=search_type,
        query_text=query_text,
        voyage_cache_hit=None,
        voyage_cache_mode=None,
        voyage_latency_ms=None,
        cohere_cache_hit=None,
        cohere_cache_mode=None,
        cohere_latency_ms=None,
        total_latency_ms=100,
        result_count=5,
        node_id="node-1",
        correlation_id=None,
    )


# ---------------------------------------------------------------------------
# Tests: basic endpoint contract
# ---------------------------------------------------------------------------


class TestSearchEventsEndpointBasic:
    def test_returns_200_with_empty_list(self, client_and_writer):
        """When no events exist, returns 200 with empty events list and total=0."""
        client, _ = client_and_writer
        resp = client.get("/api/admin/search-events")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "events" in body, f"Missing 'events' key in response: {body}"
        assert "total" in body, f"Missing 'total' key in response: {body}"
        assert body["events"] == []
        assert body["total"] == 0

    def test_response_uses_total_key(self, client_and_writer):
        """Spec H1: response key is 'total', not 'total_count'."""
        client, writer = client_and_writer
        writer.backend.insert_batch([_make_record()])
        resp = client.get("/api/admin/search-events")
        assert resp.status_code == 200
        body = resp.json()
        assert "total" in body, f"Expected 'total' key, got keys: {list(body.keys())}"
        assert "total_count" not in body, (
            "Must not use 'total_count' — spec says 'total'"
        )

    def test_returns_events_after_insert(self, client_and_writer):
        """After inserting a record, endpoint returns it."""
        client, writer = client_and_writer
        writer.backend.insert_batch([_make_record(username="alice")])

        resp = client.get("/api/admin/search-events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["events"]) == 1
        assert body["events"][0]["username"] == "alice"
        assert body["events"][0]["query_text"] == "hello world"

    def test_limit_over_1000_returns_400(self, client_and_writer):
        """Spec H10: limit=1001 must return HTTP 400 Bad Request."""
        client, _ = client_and_writer
        resp = client.get("/api/admin/search-events?limit=1001")
        assert resp.status_code == 400, (
            f"Expected 400 for limit=1001, got {resp.status_code}: {resp.text}"
        )

    def test_requires_admin_auth_when_no_override(self):
        """Without auth override, unauthenticated request gets 401 or 403."""
        from code_indexer.server.app import app

        # Ensure no override is set for this test
        app.dependency_overrides.clear()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/admin/search-events")
        assert resp.status_code in (
            401,
            403,
            422,
        ), f"Expected auth error, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Tests: query parameter filtering
# ---------------------------------------------------------------------------


class TestSearchEventsFiltering:
    def _seed(self, writer):
        writer.backend.insert_batch(
            [
                _make_record(
                    username="alice",
                    repo_alias="repo-a",
                    search_type="semantic",
                    timestamp=1000.0,
                ),
                _make_record(
                    username="bob",
                    repo_alias="repo-b",
                    search_type="fts",
                    timestamp=2000.0,
                ),
                _make_record(
                    username="alice",
                    repo_alias="repo-a",
                    search_type="fts",
                    timestamp=3000.0,
                ),
            ]
        )

    def test_filter_by_username(self, client_and_writer):
        """Filter by username returns only that user's events."""
        client, writer = client_and_writer
        self._seed(writer)
        resp = client.get("/api/admin/search-events?username=alice")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert all(e["username"] == "alice" for e in body["events"])

    def test_filter_by_search_type(self, client_and_writer):
        """Filter by search_type returns only that type."""
        client, writer = client_and_writer
        self._seed(writer)
        resp = client.get("/api/admin/search-events?search_type=semantic")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["events"][0]["search_type"] == "semantic"

    def test_filter_by_repo_alias(self, client_and_writer):
        """Filter by repo_alias returns only that repo's events."""
        client, writer = client_and_writer
        self._seed(writer)
        resp = client.get("/api/admin/search-events?repo_alias=repo-b")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["events"][0]["username"] == "bob"

    def test_filter_by_time_range(self, client_and_writer):
        """from_ts and to_ts filter in half-open range [from_ts, to_ts)."""
        client, writer = client_and_writer
        self._seed(writer)
        resp = client.get("/api/admin/search-events?from_ts=1500.0&to_ts=3000.0")
        assert resp.status_code == 200
        body = resp.json()
        # timestamp=2000.0 is in [1500, 3000); 1000 and 3000 are excluded
        assert body["total"] == 1
        assert body["events"][0]["username"] == "bob"

    def test_pagination(self, client_and_writer):
        """Limit and offset control pagination; total is always the full count."""
        client, writer = client_and_writer
        self._seed(writer)
        resp = client.get("/api/admin/search-events?limit=2&offset=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["events"]) == 2

    def test_default_limit_returns_all_when_few_records(self, client_and_writer):
        """Default limit (100) returns all events when fewer than 100 exist."""
        client, writer = client_and_writer
        self._seed(writer)
        resp = client.get("/api/admin/search-events")
        assert resp.status_code == 200
        assert len(resp.json()["events"]) == 3


# ---------------------------------------------------------------------------
# Tests: writer absent (app.state.search_event_log_writer is None)
# ---------------------------------------------------------------------------


class TestRealWriterBackendAttribute:
    """Defect A regression: endpoint must work with the REAL SearchEventLogWriter.

    The real writer stores the backend as self._backend (private, with underscore).
    The endpoint previously called writer.backend (no underscore), causing HTTP 500
    with AttributeError. This test exercises the real writer to catch the regression.
    """

    def test_endpoint_uses_private_backend_attribute(self):
        """Defect A: GET /api/admin/search-events must return 200 when using
        the real SearchEventLogWriter (which stores backend as self._backend).

        Before the fix: writer.backend raises AttributeError -> HTTP 500.
        After the fix: writer._backend.query() returns ([], 0) -> HTTP 200.
        """
        import tempfile
        import os
        from code_indexer.server.app import app
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
            SearchEventLogSqliteBackend,
        )

        admin_user = _make_admin_user()
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "test_events.db")
            real_backend = SearchEventLogSqliteBackend(db_path)
            real_writer = SearchEventLogWriter(real_backend)
            app.state.search_event_log_writer = real_writer

            try:
                client = TestClient(app, raise_server_exceptions=False)
                resp = client.get("/api/admin/search-events")
                assert resp.status_code == 200, (
                    f"Expected 200 with real SearchEventLogWriter, got "
                    f"{resp.status_code}: {resp.text}\n"
                    f"Root cause: endpoint called writer.backend but real writer uses self._backend"
                )
                body = resp.json()
                assert body["events"] == []
                assert body["total"] == 0
            finally:
                app.dependency_overrides.clear()
                app.state.search_event_log_writer = None


class TestSearchEventsWriterAbsent:
    def test_returns_503_when_writer_not_initialized(self):
        """When app.state.search_event_log_writer is None, returns 503."""
        from code_indexer.server.app import app

        admin_user = _make_admin_user()
        app.dependency_overrides[get_current_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user] = lambda: admin_user
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
        app.state.search_event_log_writer = None

        try:
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/admin/search-events")
            assert resp.status_code == 503, (
                f"Expected 503 when writer is None, got {resp.status_code}: {resp.text}"
            )
        finally:
            app.dependency_overrides.clear()
            app.state.search_event_log_writer = None
