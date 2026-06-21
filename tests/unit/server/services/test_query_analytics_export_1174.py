"""Unit tests for Bug #1174: two export defects in query analytics.

Defect A (job_id != export row id):
  POST /api/admin/search-events/export returns a BGM job_id.
  The export row in query_analytics_exports was getting a DIFFERENT UUID:
    Line 897: export_id = str(uuid.uuid4())   # pre-generated UUID #1
    Line 925: job_id = bgm.submit_job(...)    # BGM returns UUID #2
    return {"job_id": job_id}                 # client receives UUID #2
    job_id_holder["job_id"] = job_id          # set AFTER submit_job returns
    worker calls run_export(export_id=export_id)  # stores UUID #1 in DB
  Client looks up the export row by job_id but the row uses export_id -> not found.
  Fix: worker must read job_id_holder["job_id"] and use that as the export_id.

  Test approach:
  - _CapturingBGM.submit_job() captures the worker func and returns a known job_id
    WITHOUT running the worker — mimicking real BGM that spawns a thread.
  - Route handler returns, then route code sets job_id_holder["job_id"] = job_id.
  - After the route call, test runs the captured worker to simulate thread execution.
  - Assert the export row id equals the known job_id.

Defect B (GET .../exports?id={id} filter not wired):
  The list route lacked an `id` query parameter; all GET requests returned
  the full history.
  Fix: add `id: Optional[str]` to the route signature and forward it to
  the backend's list_exports(export_id=id).

  Test approach: test the SQLite backend filter directly (already correct at the
  service layer), and test the registered list route with `?id=` query parameter.

Tests:
  TestExportIdEqualsJobId::test_job_id_matches_export_row_id
  TestListExportsIdFilter::test_list_exports_id_filter_returns_matching_row
  TestListExportsIdFilter::test_list_exports_no_id_filter_returns_all
  TestListExportsIdFilter::test_list_route_id_filter_is_wired
"""

import time
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportService,
    QueryAnalyticsExportSqliteBackend,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth import dependencies as auth_deps


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_1174.db")


@pytest.fixture
def export_backend(tmp_db):
    return QueryAnalyticsExportSqliteBackend(tmp_db)


@pytest.fixture
def export_service(tmp_path, export_backend):
    return QueryAnalyticsExportService(
        backend=export_backend,
        golden_repos_dir=str(tmp_path),
    )


class _CapturingBGM:
    """Stub BackgroundJobManager that captures the submitted worker WITHOUT running it.

    submit_job() stores the worker function and returns a known job_id immediately.
    The test then calls run_captured_worker() after the route returns — mirroring
    the real BGM behaviour where the thread runs after job_id_holder is populated.
    """

    def __init__(self, job_id: str) -> None:
        self._job_id = job_id
        self._captured_func = None

    def submit_job(
        self, operation_type, func, *args, submitter_username, **kwargs
    ) -> str:
        self._captured_func = func
        return self._job_id

    def run_captured_worker(self) -> Any:
        """Run the captured worker function synchronously. Raises on failure."""
        if self._captured_func is None:
            raise AssertionError("No worker was captured — submit_job was never called")
        return self._captured_func()


def _make_admin_user(username: str = "admin") -> User:
    user = MagicMock(spec=User)
    user.username = username
    user.role = UserRole.ADMIN
    return user


def _make_minimal_app(export_svc, bgm, monkeypatch) -> FastAPI:
    """Build and return a FastAPI app with the admin export routes registered."""
    from code_indexer.server.routers.inline_admin_ops import register_admin_ops_routes

    fast_app = FastAPI()
    fast_app.state.query_analytics_export_service = export_svc
    fast_app.state.search_event_log_writer = None

    # Patch get_config_service used inside _get_export_retention_days
    cfg_mock = MagicMock()
    cfg_mock.get_config.return_value.export_retention_days = 7
    monkeypatch.setattr(
        "code_indexer.server.routers.inline_admin_ops.get_config_service",
        lambda: cfg_mock,
        raising=False,
    )

    # Minimal stubs for unused route dependencies
    stub = MagicMock()

    register_admin_ops_routes(
        fast_app,
        jwt_manager=stub,
        user_manager=stub,
        golden_repo_manager=stub,
        background_job_manager=bgm,
        workspace_cleanup_service=stub,
        config_service=stub,
        server_config=stub,
        data_dir="/tmp",
        job_tracker=stub,
    )

    admin = _make_admin_user()
    fast_app.dependency_overrides[auth_deps.get_current_admin_user_hybrid] = (
        lambda: admin
    )

    return fast_app


def _make_record(row_id: str, row_status: str = "pending") -> dict:
    return {
        "id": row_id,
        "initiated_by": "admin",
        "created_at": time.time(),
        "status": row_status,
        "filter_summary": "All searches",
    }


# ---------------------------------------------------------------------------
# Defect A: job_id returned by route must match the export row id in DB
# ---------------------------------------------------------------------------


class TestExportIdEqualsJobId:
    """Bug #1174A: the returned job_id must equal the export row id in the DB.

    The stub BGM captures the worker and returns a known job_id WITHOUT
    running the worker (matching real BGM thread-spawn behaviour).
    The test then calls run_captured_worker() after the route returns so that
    job_id_holder["job_id"] has been set — and the worker can read it.
    """

    def test_job_id_matches_export_row_id(
        self, export_backend, export_service, monkeypatch
    ):
        """POST /api/admin/search-events/export must return a job_id that equals
        the id of the created export row in query_analytics_exports."""
        known_job_id = str(uuid.uuid4())
        bgm = _CapturingBGM(job_id=known_job_id)

        fast_app = _make_minimal_app(export_service, bgm, monkeypatch)
        client = TestClient(fast_app, raise_server_exceptions=False)

        # Step 1: POST to the route — BGM captures worker, sets job_id_holder["job_id"]
        response = client.post(
            "/api/admin/search-events/export",
            json={},
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        returned_job_id = response.json().get("job_id")
        assert returned_job_id == known_job_id

        # Step 2: Run the captured worker (simulates BGM thread executing after route returns)
        bgm.run_captured_worker()

        # Step 3: The export row must be findable by the returned job_id
        rows = export_backend.list_exports(export_id=known_job_id)
        assert len(rows) == 1, (
            f"Bug #1174A: export row not found by job_id={known_job_id!r}. "
            f"All rows in DB: {export_backend.list_exports()!r}. "
            "The worker must use the BGM job_id as the export_id, not a pre-generated UUID."
        )
        assert rows[0]["id"] == known_job_id, (
            f"Bug #1174A: export row id={rows[0]['id']!r} != job_id={known_job_id!r}"
        )


# ---------------------------------------------------------------------------
# Defect B: list route ?id= filter
# ---------------------------------------------------------------------------


class TestListExportsIdFilter:
    """Bug #1174B: the list route must accept and honour the ?id= query parameter."""

    def test_list_exports_id_filter_returns_matching_row(self, export_backend):
        """Backend list_exports(export_id=X) returns only the matching row."""
        id_a = str(uuid.uuid4())
        id_b = str(uuid.uuid4())
        export_backend.create_export(_make_record(id_a, "completed"))
        export_backend.create_export(_make_record(id_b, "pending"))

        rows = export_backend.list_exports(export_id=id_a)

        assert len(rows) == 1, (
            f"Bug #1174B: expected 1 row for export_id={id_a!r}, got {len(rows)}."
        )
        assert rows[0]["id"] == id_a

    def test_list_exports_no_id_filter_returns_all(self, export_backend):
        """Without a filter, list_exports() returns all rows."""
        id_a = str(uuid.uuid4())
        id_b = str(uuid.uuid4())
        export_backend.create_export(_make_record(id_a))
        export_backend.create_export(_make_record(id_b))

        rows = export_backend.list_exports()

        assert len(rows) == 2, f"Expected 2 rows with no filter, got {len(rows)}"

    def test_list_route_id_filter_is_wired(
        self, export_backend, export_service, monkeypatch
    ):
        """GET /api/admin/search-events/exports?id=X returns only row X.

        Exercises the registered list route to verify the `id` query parameter
        is accepted and forwarded to the backend filter.
        """
        id_a = str(uuid.uuid4())
        id_b = str(uuid.uuid4())
        export_backend.create_export(_make_record(id_a, "completed"))
        export_backend.create_export(_make_record(id_b, "pending"))

        known_job_id = str(uuid.uuid4())
        bgm = _CapturingBGM(job_id=known_job_id)
        fast_app = _make_minimal_app(export_service, bgm, monkeypatch)
        client = TestClient(fast_app, raise_server_exceptions=False)

        response = client.get(f"/api/admin/search-events/exports?id={id_a}")

        assert response.status_code == status.HTTP_200_OK, (
            f"GET /exports?id={id_a} failed with {response.status_code}: {response.text}"
        )
        exports = response.json().get("exports", [])
        assert len(exports) == 1, (
            f"Bug #1174B: expected 1 export for id={id_a!r}, got {len(exports)}. "
            "The route must pass the `id` query param to the backend filter."
        )
        assert exports[0]["id"] == id_a
