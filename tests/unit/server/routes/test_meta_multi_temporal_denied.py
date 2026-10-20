"""Temporal search of the internal catalog is admin-only on POST /api/query/multi.

The real multi-repository router on a small FastAPI app with the real group
store. The multi search service and the search-event writer are recording
doubles: a temporal request naming the catalog must be rejected before
either is reached (a request rejection records no search event). Neutral
names only.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routes import multi_query_routes
from code_indexer.server.services.cidx_meta_access import acting_scope
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
    build_cidx_meta_fixture,
)

TEMPORAL_REFUSAL = CatalogSurface.TEMPORAL.refusal
SERVICE_REACHED = "multi search service reached"


class RecordingSearchService:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    def search(self, request: Any) -> Any:
        self.calls.append((request.search_type, list(request.repositories)))
        raise RuntimeError(SERVICE_REACHED)


class RecordingWriter:
    def __init__(self) -> None:
        self.enqueued: List[Any] = []

    def enqueue(self, *args: Any, **kwargs: Any) -> None:
        self.enqueued.append((args, kwargs))


@pytest.fixture
def door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    fx = build_cidx_meta_fixture(tmp_path)
    service = RecordingSearchService()
    writer = RecordingWriter()
    monkeypatch.setattr(multi_query_routes, "get_multi_search_service", lambda: service)
    current: Dict[str, Any] = {"scope": None}
    app = FastAPI()
    app.state.access_filtering_service = fx.access_service
    app.state.search_event_log_writer = writer

    @app.middleware("http")
    async def _acting(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # The MCP dispatcher's acting-user scope, bound per request.
        with acting_scope(current["scope"]):
            return await call_next(request)

    app.include_router(multi_query_routes.router)
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(
            client=client, current=current, service=service, writer=writer
        )


def _post(
    door: SimpleNamespace,
    username: str,
    repositories: List[str],
    search_type: str = "temporal",
    scope: Optional[List[str]] = None,
) -> Any:
    door.current["user"] = User(
        username=username,
        password_hash="hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(),
    )
    door.current["scope"] = scope
    body = {"repositories": repositories, "query": "sample", "search_type": search_type}
    return door.client.post("/api/query/multi", json=body)


CATALOG_LISTS = [
    ["cidx-meta-global"],
    ["cidx-meta"],
    [f"{ALPHA}-global", "cidx-meta-global"],
]


@pytest.mark.parametrize("repositories", CATALOG_LISTS, ids=str)
@pytest.mark.parametrize(
    "username, scope", [(RESTRICTED_USER, None), (ADMIN_USER, [ALPHA])]
)
def test_temporal_multi_search_naming_the_catalog_is_rejected(
    door: SimpleNamespace,
    repositories: List[str],
    username: str,
    scope: Optional[List[str]],
) -> None:
    response = _post(door, username, repositories, scope=scope)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": TEMPORAL_REFUSAL}
    assert door.service.calls == []
    assert door.writer.enqueued == []


def test_temporal_multi_search_of_granted_repositories_reaches_the_service(
    door: SimpleNamespace,
) -> None:
    _post(door, RESTRICTED_USER, [f"{ALPHA}-global"])
    assert door.service.calls == [("temporal", [f"{ALPHA}-global"])]


def test_non_temporal_multi_search_of_the_catalog_reaches_the_service(
    door: SimpleNamespace,
) -> None:
    _post(door, RESTRICTED_USER, ["cidx-meta-global"], search_type="semantic")
    assert door.service.calls == [("semantic", ["cidx-meta-global"])]


def test_admin_temporal_multi_search_of_the_catalog_reaches_the_service(
    door: SimpleNamespace,
) -> None:
    _post(door, ADMIN_USER, ["cidx-meta-global"])
    assert door.service.calls == [("temporal", ["cidx-meta-global"])]
