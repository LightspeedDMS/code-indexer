"""Bug #2105: a search_event_log read failure must never look like an empty result.

Every test runs against a REAL store on both backends:

- SQLite: a temp-file database.
- PostgreSQL: a live database from TEST_POSTGRES_DSN, isolated in a throwaway
  schema per test (skipped when psycopg is absent or the env var is unset).

The read failure is produced by dropping the search_event_log table, never by
a mock.
"""

import uuid
from typing import Any, Iterator, List

import pytest

from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportService,
)
from code_indexer.server.services.search_event_log_writer import SearchEventRecord
from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
    RealStore,
    make_record,
    open_store,
)

_record = make_record


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON),
        ),
    ]
)
def real_store(request, tmp_path) -> Iterator[RealStore]:
    with open_store(request.param, tmp_path) as store:
        yield store


def _insert(store: RealStore, records: List[SearchEventRecord]) -> None:
    store.backend.insert_batch(records)


# ---------------------------------------------------------------------------
# Backend read contract
# ---------------------------------------------------------------------------


class TestBackendQueryRaisesOnReadFailure:
    def test_query_raises_when_table_missing(self, real_store):
        _insert(real_store, [_record()])
        real_store.drop_table()

        with pytest.raises(Exception) as excinfo:
            real_store.backend.query(limit=10, offset=0)

        assert "search_event_log" in str(excinfo.value)

    def test_query_successful_empty_read_returns_empty(self, real_store):
        events, total = real_store.backend.query(limit=10, offset=0)

        assert events == []
        assert total == 0


# ---------------------------------------------------------------------------
# Export outcome
# ---------------------------------------------------------------------------


def _service(store: RealStore, tmp_path) -> QueryAnalyticsExportService:
    return QueryAnalyticsExportService(
        backend=store.export_backend, golden_repos_dir=str(tmp_path)
    )


class TestRunExportReadFailure:
    def test_read_failure_marks_export_failed_with_reason_and_raises(
        self, real_store, tmp_path
    ):
        _insert(real_store, [_record()])
        real_store.drop_table()
        service = _service(real_store, tmp_path)
        export_id = str(uuid.uuid4())

        # The background job itself must fail too, never report success.
        with pytest.raises(Exception):
            service.run_export(
                export_id=export_id,
                filters={},
                initiated_by="admin",
                export_retention_days=7,
                search_event_log_backend=real_store.backend,
            )

        [record] = service.list_exports(export_id=export_id)
        assert record["status"] == "failed"
        assert "search_event_log" in (record["error_message"] or "")
        assert record["row_count"] is None
        assert record["file_path"] is None
        assert record["download_link"] is None
        assert not service.export_path(export_id).exists()

    def test_successful_empty_read_is_completed_with_zero_rows(
        self, real_store, tmp_path
    ):
        service = _service(real_store, tmp_path)
        export_id = str(uuid.uuid4())

        service.run_export(
            export_id=export_id,
            filters={},
            initiated_by="admin",
            export_retention_days=7,
            search_event_log_backend=real_store.backend,
        )

        [record] = service.list_exports(export_id=export_id)
        assert record["status"] == "completed"
        assert record["row_count"] == 0
        assert record["error_message"] is None


# ---------------------------------------------------------------------------
# REST front door
# ---------------------------------------------------------------------------


class _CapturingBGM:
    """Captures the submitted export worker so the test runs it synchronously."""

    def __init__(self) -> None:
        self.job_id = str(uuid.uuid4())
        self.worker: Any = None

    def submit_job(self, operation_type, func, *args, submitter_username, **kwargs):
        self.worker = func
        return self.job_id


def _client(store: RealStore, tmp_path, bgm: Any, monkeypatch):
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from code_indexer.server.auth import dependencies as auth_deps
    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.routers.inline_admin_ops import (
        register_admin_ops_routes,
    )
    from code_indexer.server.services.search_event_log_writer import (
        SearchEventLogWriter,
    )

    app = FastAPI()
    app.state.query_analytics_export_service = _service(store, tmp_path)
    app.state.search_event_log_writer = SearchEventLogWriter(store.backend)
    cfg = MagicMock()
    cfg.get_config.return_value.export_retention_days = 7
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        lambda: cfg,
    )
    stub = MagicMock()
    register_admin_ops_routes(
        app,
        jwt_manager=stub,
        user_manager=stub,
        golden_repo_manager=stub,
        background_job_manager=bgm,
        workspace_cleanup_service=stub,
        config_service=stub,
        server_config=stub,
        data_dir=str(tmp_path),
        job_tracker=stub,
    )
    admin = MagicMock(spec=User)
    admin.username = "admin"
    admin.role = UserRole.ADMIN
    app.dependency_overrides[auth_deps.get_current_admin_user_hybrid] = lambda: admin
    return TestClient(app, raise_server_exceptions=False)


class TestFrontDoor2105:
    def test_history_shows_failed_status_and_reason(
        self, real_store, tmp_path, monkeypatch
    ):
        _insert(real_store, [_record()])
        real_store.drop_table()
        bgm = _CapturingBGM()
        client = _client(real_store, tmp_path, bgm, monkeypatch)

        resp = client.post("/api/admin/search-events/export", json={})
        assert resp.status_code == 202
        with pytest.raises(Exception):
            bgm.worker()

        history = client.get("/api/admin/search-events/exports")
        assert history.status_code == 200
        [row] = history.json()["exports"]
        assert row["id"] == bgm.job_id
        assert row["status"] == "failed"
        assert "search_event_log" in (row["error_message"] or "")
        assert row["download_link"] is None

    def test_search_events_listing_returns_500_not_empty_page(
        self, real_store, tmp_path, monkeypatch
    ):
        _insert(real_store, [_record()])
        real_store.drop_table()
        client = _client(real_store, tmp_path, _CapturingBGM(), monkeypatch)

        resp = client.get("/api/admin/search-events")

        assert resp.status_code == 500
        assert "search_event_log" in resp.json()["detail"]

    def test_unknown_cache_hit_filter_is_rejected_before_any_job(
        self, real_store, tmp_path, monkeypatch
    ):
        bgm = _CapturingBGM()
        client = _client(real_store, tmp_path, bgm, monkeypatch)

        resp = client.post(
            "/api/admin/search-events/export", json={"cache_hit_filter": "hit_only"}
        )

        assert resp.status_code == 422
        assert "cache_hit_filter" in resp.text
        assert bgm.worker is None, "an invalid filter must not start an export"


def test_history_table_renders_error_message():
    """The Export History table must show a failed export's reason (escaped)."""
    from pathlib import Path

    import code_indexer.server.web as web_pkg

    template = (
        Path(web_pkg.__file__).parent / "templates" / "analytics_export.html"
    ).read_text()
    render_fn = template.split("function renderHistory", 1)[1].split("\n}\n", 1)[0]

    assert "esc(e.error_message)" in render_fn
