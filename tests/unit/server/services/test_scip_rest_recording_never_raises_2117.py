"""SCIP REST doors: recording a search never breaks the request (Story #2117).

Search event recording must never raise into a request. The SCIP REST doors
record a row naming their endpoint ("GET /scip/definition", "POST
/api/scip/multi/definition", ...). These tests call the route functions with
a REAL Starlette Request whose scope carries no matched route (a direct call,
not routed by FastAPI) and require the search to answer exactly as it would
without recording -- results on success, the handled error on failure --
and the row to name the endpoint. They also pin the multi-repository tool
identifiers through the real front door, and that `record_scip_search`
swallows an error from any of its reads.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest
from starlette.requests import Request

from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    Recorder,
    make_user,
    recorder,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

_MULTI_PATHS = ["definition", "references", "dependencies", "dependents", "callchain"]


@pytest.fixture(params=_KINDS)
def rec(request, tmp_path: Path) -> Iterator[Recorder]:
    with recorder(request.param, tmp_path) as recording:
        yield recording


def _state(rec: Recorder) -> SimpleNamespace:
    return SimpleNamespace(
        search_event_log_writer=rec.writer,
        access_filtering_service=SimpleNamespace(is_admin_user=lambda u: True),
    )


def _unrouted_request(rec: Recorder, method: str, path: str) -> Request:
    """A real Request with no "route" in its scope (not matched by FastAPI)."""
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "app": SimpleNamespace(state=_state(rec)),
        }
    )


class _DefinitionService:
    def __init__(self, error: Exception = None) -> None:  # type: ignore[assignment]
        self.error = error

    def find_definition(self, **_kwargs: Any) -> List[Dict[str, Any]]:
        if self.error is not None:
            raise self.error
        return [{"symbol": "Calculator", "file_path": "src/a.py", "line": 1}]


def _get_definition(rec: Recorder, service: _DefinitionService) -> Dict[str, Any]:
    from code_indexer.server.routers import scip_queries

    with patch.object(scip_queries, "_get_scip_query_service", lambda r: service):
        result: Dict[str, Any] = scip_queries.get_definition(
            request=_unrouted_request(rec, "GET", "/scip/definition"),
            symbol="Calculator",
            exact=False,
            project="example-repo",
            current_user=make_user("bob"),
        )
    return result


class TestScipRestRecordingNeverBreaksTheRequest:
    def test_definition_succeeds_and_records_its_endpoint(self, rec):
        body = _get_definition(rec, _DefinitionService())

        assert body["success"] is True, body
        assert body["total_results"] == 1
        [row] = rec.rows()
        assert (row["tool"], row["door"], row["outcome"]) == (
            "GET /scip/definition",
            "rest",
            "success",
        )

    def test_failed_definition_returns_its_error_and_records_a_failure(self, rec):
        body = _get_definition(
            rec, _DefinitionService(RuntimeError("SCIP database unreadable"))
        )

        assert body == {"success": False, "error": "SCIP database unreadable"}
        [row] = rec.rows()
        assert (row["tool"], row["door"], row["outcome"], row["error_class"]) == (
            "GET /scip/definition",
            "rest",
            "failure",
            "RuntimeError",
        )

    def test_multi_definition_runs_and_records_its_endpoint(self, rec):
        from code_indexer.server.multi.scip_models import SCIPMultiRequest
        from code_indexer.server.routes import scip_multi_routes

        service = _MultiService()
        with patch.object(scip_multi_routes, "get_scip_multi_service", lambda: service):
            response = scip_multi_routes.multi_repository_definition(
                SCIPMultiRequest.model_validate(
                    {"symbol": "Calculator", "repositories": ["first-repo"]}
                ),
                http_request=_unrouted_request(
                    rec, "POST", "/api/scip/multi/definition"
                ),
                user=make_user("carol"),
            )

        assert service.calls == ["definition"]
        assert response["metadata"]["total_results"] == 0
        [row] = rec.rows()
        assert (row["tool"], row["door"], row["repo_alias"]) == (
            "POST /api/scip/multi/definition",
            "rest",
            "first-repo",
        )


class _MultiService:
    """SCIPMultiService stand-in: every operation resolves `first-repo`."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __getattr__(self, operation: str) -> Any:
        def _run(_request: Any) -> Any:
            from code_indexer.server.multi.scip_models import (
                SCIPMultiMetadata,
                SCIPMultiResponse,
            )
            from code_indexer.server.services.search_event_context import (
                note_current_repositories,
            )

            self.calls.append(operation)
            note_current_repositories(searched=["first-repo"], failed=[])
            return SCIPMultiResponse(
                results={"first-repo": []},
                metadata=SCIPMultiMetadata(
                    total_results=0,
                    repos_searched=1,
                    repos_with_results=0,
                    execution_time_ms=1,
                ),
                skipped={},
                errors={},
            )

        return _run


class TestScipMultiToolIdentifiers:
    @pytest.mark.parametrize("path", _MULTI_PATHS)
    def test_front_door_records_the_endpoint_template(self, rec, path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from code_indexer.server.auth.dependencies import get_current_user
        from code_indexer.server.routes import scip_multi_routes

        app = FastAPI()
        app.include_router(scip_multi_routes.router)
        app.state.search_event_log_writer = rec.writer
        app.state.access_filtering_service = _state(rec).access_filtering_service
        app.dependency_overrides[get_current_user] = lambda: make_user("carol")
        body: Dict[str, Any] = {"symbol": "Calculator", "repositories": ["first-repo"]}
        if path == "callchain":
            body.update(symbol="", from_symbol="Calculator", to_symbol="add")
        with patch.object(
            scip_multi_routes, "get_scip_multi_service", lambda: _MultiService()
        ):
            response = TestClient(app).post(f"/api/scip/multi/{path}", json=body)

        assert response.status_code == 200, response.text
        [row] = rec.rows()
        assert (row["tool"], row["door"]) == (f"POST /api/scip/multi/{path}", "rest")


class _UnreadableContext:
    @property
    def resolved_repositories(self) -> Any:
        raise RuntimeError("context unreadable")

    searched_repositories = None


class TestRecordScipSearchNeverRaises:
    def test_an_unreadable_context_is_logged_not_raised(self, rec, caplog):
        from code_indexer.server.services.search_event_recording import (
            SearchOrigin,
            record_scip_search,
        )

        record_scip_search(
            rec.writer,
            {"success": True, "results": []},
            origin=SearchOrigin(tool="GET /scip/definition", door="rest"),
            username="bob",
            repo_alias="example-repo",
            query_text="Calculator",
            started_monotonic=0.0,
            correlation_id=None,
            context=_UnreadableContext(),  # type: ignore[arg-type]
        )

        assert rec.rows() == []
        assert "search event not recorded (scip)" in caplog.text
