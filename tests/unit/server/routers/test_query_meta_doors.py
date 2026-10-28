"""REST /api/query serves a restricted caller only their projected view of
the catalog: sync semantic, FTS, no-alias fan-out, async job reads (re-checked
on read) and payload-cache handles bound to the caller's view.

Real app routes, real indexes and a real small-preview payload cache
(catalog_search_env); only authentication (the caller) and the embedding
provider are replaced.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.cache.handle_binding import HANDLE_BINDING_MARKER
from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY, GAMMA
from tests.unit.server.query.catalog_search_env import (
    CATALOG_ALIAS,
    RESTRICTED,
    CatalogEnv,
    catalog_app,
    dump,
)
from tests.unit.server.query.extension_filter_env_2047 import ranked_store_search

pytestmark = pytest.mark.timeout(90)
_JOB_POLL_SECONDS = 30.0


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    tmp = tmp_path_factory.mktemp("rest-query-doors")
    with catalog_app(tmp) as catalog:
        cache = PayloadCache(
            db_path=tmp / "payload.db",
            config=PayloadCacheConfig(preview_size_chars=12),
        )
        cache.initialize()
        previous = getattr(catalog.app.state, "payload_cache", None)
        catalog.app.state.payload_cache = cache
        try:
            yield catalog
        finally:
            catalog.app.state.payload_cache = previous
            cache.close()


@pytest.fixture
def client(env: CatalogEnv) -> Iterator[TestClient]:
    from code_indexer.server.auth import dependencies

    overrides = env.app.dependency_overrides
    for dep in (dependencies.get_current_user, dependencies.get_current_user_hybrid):
        overrides[dep] = lambda: RESTRICTED
    try:
        with ranked_store_search():
            yield TestClient(env.app)
    finally:
        for dep in (
            dependencies.get_current_user,
            dependencies.get_current_user_hybrid,
        ):
            overrides.pop(dep, None)


def _post(client: TestClient, **body: Any) -> Dict[str, Any]:
    response = client.post("/api/query", json=body)
    assert response.status_code in (200, 202), response.text
    return dict(response.json())


def _handles(value: Any) -> List[str]:
    found: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("cache_handle") and isinstance(item, str):
                found.append(item)
            else:
                found.extend(_handles(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_handles(item))
    return found


def test_sync_semantic_catalog_query_is_projected(client: TestClient) -> None:
    body = _post(
        client, query_text="payments", repository_alias=CATALOG_ALIAS, limit=40
    )
    dumped = dump(body["results"])
    assert CANARY not in dumped and GAMMA not in dumped
    assert ALPHA in dumped


def test_no_alias_query_is_projected(client: TestClient) -> None:
    body = _post(client, query_text="payments", limit=40)
    dumped = dump(body["results"])
    assert CANARY not in dumped and GAMMA not in dumped
    assert body["results"]


def test_fts_for_the_canary_returns_nothing(client: TestClient) -> None:
    body = _post(
        client,
        query_text=CANARY,
        repository_alias=CATALOG_ALIAS,
        search_mode="fts",
        limit=10,
    )
    assert body["fts_results"] == []


def test_rest_catalog_handles_are_bound_and_projected(
    client: TestClient, env: CatalogEnv
) -> None:
    body = _post(
        client, query_text="payments", repository_alias=CATALOG_ALIAS, limit=40
    )
    handles = _handles(body)
    assert handles, "the small preview size must mint handles"
    for handle in handles:
        assert HANDLE_BINDING_MARKER in handle
        assert CANARY not in env.app.state.payload_cache.retrieve_full(handle).lower()


@pytest.mark.parametrize(
    "search_type",
    ["semantic", "fts", pytest.param("regex", marks=pytest.mark.slow)],
)
def test_multi_query_catalog_rows_are_projected(
    client: TestClient, search_type: str
) -> None:
    query = "payments" if search_type == "semantic" else CANARY
    response = client.post(
        "/api/query/multi",
        json={
            "repositories": [CATALOG_ALIAS, f"{ALPHA}-global"],
            "query": query,
            "search_type": search_type,
            "limit": 40,
        },
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    catalog_rows = results.get(CATALOG_ALIAS, [])
    if search_type == "semantic":
        assert catalog_rows
        dumped = dump(results)
        assert CANARY not in dumped and GAMMA not in dumped
    else:
        assert catalog_rows == []


def _wait_job(client: TestClient, job_id: str) -> Dict[str, Any]:
    deadline = time.monotonic() + _JOB_POLL_SECONDS
    while time.monotonic() < deadline:
        status = dict(client.get(f"/api/jobs/{job_id}").json())
        if status.get("status") in ("completed", "failed"):
            return status
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish")


def test_async_job_results_are_rechecked_on_read(
    client: TestClient, env: CatalogEnv
) -> None:
    job_id = _post(
        client,
        query_text="talks",
        repository_alias=CATALOG_ALIAS,
        limit=40,
        async_query=True,
    )["job_id"]
    status = _wait_job(client, job_id)
    assert status["status"] == "completed", status
    assert CANARY not in dump(status["result"])
    assert f"{ALPHA}.md" in dump(status["result"])
    polled = dict(client.get(f"/api/query/result/{job_id}").json())
    assert CANARY not in dump(polled)

    gam = env.fx.group_manager
    team = gam.get_group_by_name("fixture-team")
    assert team is not None
    gam.revoke_repo_access(ALPHA, team.id)
    try:
        after = dict(client.get(f"/api/jobs/{job_id}").json())
        assert f"{ALPHA}.md" not in dump(after["result"])
        polled_after = dict(client.get(f"/api/query/result/{job_id}").json())
        assert f"{ALPHA}.md" not in dump(polled_after)
    finally:
        gam.grant_repo_access(ALPHA, team.id, granted_by="test")


@pytest.mark.parametrize("mode", ["fts", "semantic", "hybrid"])
def test_query_is_refused_without_the_access_service(
    env: CatalogEnv,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setattr(env.app.state, "access_filtering_service", None)
    response = client.post(
        "/api/query",
        json={
            "query_text": CANARY,
            "repository_alias": CATALOG_ALIAS,
            "search_mode": mode,
            "limit": 10,
        },
    )
    assert response.status_code == 500, response.text
    assert "access_control_unavailable" in response.text
    assert CANARY not in response.text.replace(f'"{CANARY}"', "").lower()
