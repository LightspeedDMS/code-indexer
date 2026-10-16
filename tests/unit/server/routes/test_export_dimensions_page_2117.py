"""Story #2117: export filters for the new dimensions, and page statements.

- POST /api/admin/search-events/export validates door/outcome (enums) and
  tool/correlation id (bounded free text).
- The export's filter summary names the new filters.
- GET /api/admin/search-events filters by the same dimensions.
- The Query Analytics Export page states the effective retention (live
  config), the oldest stored timestamp, the query text cap, that exports
  continue on further sheets, and that the cache filter applies only to
  embedding-based searches.
"""

import time
from typing import Any, Iterator
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.services.search_event_log_writer import (
    SearchEventLogSqliteBackend,
    SearchEventLogWriter,
)
from tests.unit.server.services.search_event_real_stores import make_record


def _admin() -> Mock:
    user = Mock()
    user.username = "testadmin"
    user.role = UserRole.ADMIN
    return user


@pytest.fixture
def client(tmp_path) -> Iterator[Any]:
    from code_indexer.server.app import app

    backend = SearchEventLogSqliteBackend(str(tmp_path / "events.db"))
    backend.insert_batch(
        [
            make_record(
                query_text="ok",
                tool="search_code",
                door="mcp",
                outcome="success",
                correlation_id="c-1",
                timestamp=1_700_000_000.0,
            ),
            make_record(
                query_text="bad",
                tool="POST /api/query",
                door="rest",
                outcome="failure",
                error_class="timeout",
                correlation_id="c-2",
                timestamp=1_700_000_100.0,
            ),
        ]
    )
    previous = getattr(app.state, "search_event_log_writer", None)
    app.state.search_event_log_writer = SearchEventLogWriter(backend)
    admin = _admin()
    for dependency in (
        get_current_user,
        get_current_admin_user,
        get_current_admin_user_hybrid,
    ):
        app.dependency_overrides[dependency] = lambda: admin
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        app.state.search_event_log_writer = previous


class TestExportRequestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {"door": "ftp"},
            {"outcome": "partial"},
            {"tool": "x" * 101},
            {"correlation_id": "c" * 129},
        ],
    )
    def test_invalid_dimension_filter_is_rejected(self, client, body):
        response = client.post("/api/admin/search-events/export", json=body)
        assert response.status_code == 422, response.text

    def test_filter_summary_names_the_new_filters(self, tmp_path):
        from code_indexer.server.services.query_analytics_export_service import (
            QueryAnalyticsExportService,
            QueryAnalyticsExportSqliteBackend,
        )

        service = QueryAnalyticsExportService(
            QueryAnalyticsExportSqliteBackend(str(tmp_path / "x.db")), str(tmp_path)
        )
        summary = service.build_filter_summary(
            {
                "door": "mcp",
                "outcome": "failure",
                "tool": "search_code",
                "correlation_id": "c-9",
            }
        )
        assert summary == (
            "door=mcp, outcome=failure, tool=search_code, correlation_id=c-9"
        )


class TestEventListFilters:
    @pytest.mark.parametrize(
        "params, expected",
        [
            ({"outcome": "failure"}, ["bad"]),
            ({"door": "mcp"}, ["ok"]),
            ({"tool": "POST /api/query"}, ["bad"]),
            ({"correlation_id": "c-1"}, ["ok"]),
        ],
    )
    def test_event_list_filters_by_dimension(self, client, params, expected):
        response = client.get("/api/admin/search-events", params=params)
        assert response.status_code == 200, response.text
        assert [e["query_text"] for e in response.json()["events"]] == expected

    def test_unknown_outcome_is_rejected(self, client):
        response = client.get("/api/admin/search-events", params={"outcome": "x"})
        assert response.status_code == 422, response.text


def _page(client: Any) -> str:
    session = MagicMock(role="admin", username="admin", csrf_token="t")
    session_manager = MagicMock()
    session_manager.get_session.return_value = session
    session_manager._serializer.secret_key = "unit-test-secret"
    session_manager._serializer.secret_keys = ["unit-test-secret"]
    with patch(
        "code_indexer.server.web.routes.get_session_manager",
        return_value=session_manager,
    ):
        response = client.get("/admin/analytics-export")
    assert response.status_code == 200, response.text[:300]
    return str(response.text)


class TestPageStatements:
    def test_page_states_retention_oldest_row_and_limits(self, client, monkeypatch):
        from code_indexer.server.services.config_service import get_config_service
        from code_indexer.server.services.search_event_recording import (
            QUERY_TEXT_MAX_CHARS,
        )

        # The page reads the COMMITTED setting (the committed read itself is
        # pinned on both backends in test_search_event_retention_live_2117).
        def _committed(name: str) -> Any:
            assert name == "search_event_log_retention_days"
            return 1, 37  # (version, value)

        monkeypatch.setattr(get_config_service(), "read_committed_setting", _committed)
        html = _page(client)

        assert 'id="retention-days">37<' in html
        oldest = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(1_700_000_000.0))
        assert oldest in html
        assert f'id="query-text-cap">{QUERY_TEXT_MAX_CHARS}<' in html
        assert "further sheets" in html
        assert "embedding-based searches" in html

    def test_page_states_unavailable_retention_when_the_committed_read_fails(
        self, client, monkeypatch
    ):
        from code_indexer.server.services.config_service import get_config_service

        def _unreadable(name: str) -> Any:
            raise RuntimeError("no committed runtime configuration row")

        monkeypatch.setattr(get_config_service(), "read_committed_setting", _unreadable)
        html = _page(client)

        assert 'id="retention-days">unavailable (could not be read)<' in html
