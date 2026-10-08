"""#2047 (S21): multi-repository search applies the same ``file_extensions``
rule as single-repository search, through both front doors: MCP
``search_code`` with a list of aliases (omni) and REST ``/api/query/multi``.

Real handler / route, real MultiSearchService, SemanticSearchService,
FilesystemVectorStore and Tantivy indexes over two real git repos. Replaced:
the embedding provider (external service), authentication, and the
golden-repo registry lookup that maps an alias to its path
(MultiSearchService._get_repository_path).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole
from tests.unit.server.query.extension_filter_env_2047 import (
    LIMIT,
    QUERY,
    build_corpus_repo,
    corpus_app,
    expected_fts,
    expected_semantic,
    ranked_store_search,
)

ADMIN = User(
    username="admin",
    password_hash="$2b$12$hash",
    role=UserRole.ADMIN,
    created_at=datetime.now(timezone.utc),
)
VALUES = ["py", ".MD"]
NEVER_A_SUFFIX = "never be a file extension"


@pytest.fixture(scope="module")
def omni_env(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Tuple[Any, Dict[str, Path]]]:
    from code_indexer.server.services.access_filtering_service import (
        AccessFilteringService,
    )
    from code_indexer.server.services.group_access_manager import (
        GroupAccessManager,
    )

    root = tmp_path_factory.mktemp("omni-2047")
    repos = {}
    for alias in ("repo-a-global", "repo-b-global"):
        (root / alias).mkdir()
        repos[alias] = build_corpus_repo(root / alias)
    groups = GroupAccessManager(root / "groups.db")
    admins = groups.get_group_by_name("admins")
    assert admins is not None
    groups.assign_user_to_group(ADMIN.username, admins.id, assigned_by="test")
    (root / "golden-repos").mkdir()
    with corpus_app(root / "app") as app, pytest.MonkeyPatch.context() as mp:
        # What the lifespan would install (TestClient here runs no lifespan).
        mp.setattr(
            app.state, "golden_repos_dir", str(root / "golden-repos"), raising=False
        )
        mp.setattr(
            app.state,
            "access_filtering_service",
            AccessFilteringService(groups),
            raising=False,
        )
        yield app, repos


def _expected(mode: str, repo: Path) -> List[str]:
    if mode == "semantic":
        return expected_semantic(VALUES, None)
    return expected_fts(repo, VALUES, None)


def _registry(repos: Dict[str, Path], service_class: Any = None) -> Any:
    """Patch the alias -> path lookup on *service_class*: the class the door
    under test instantiates (another test may have re-imported the module,
    leaving more than one MultiSearchService class object in the process)."""
    from code_indexer.server.multi.multi_search_service import MultiSearchService

    return patch.object(
        service_class or MultiSearchService,
        "_get_repository_path",
        autospec=True,
        side_effect=lambda _self, repo_id: str(repos[repo_id]),
    )


def _omni(
    repos: Dict[str, Path], mode: str, values: List[str], **extra: Any
) -> Dict[str, Any]:
    from code_indexer.server.mcp.handlers import search_code
    from code_indexer.server.multi.multi_search_service import MultiSearchService

    MultiSearchService._reset_singleton()
    try:
        with ranked_store_search(), _registry(repos):
            response = search_code(
                {
                    "query_text": QUERY,
                    "repository_alias": list(repos),
                    "limit": LIMIT * len(repos),
                    "search_mode": mode,
                    "file_extensions": values,
                    "aggregation_mode": "per_repo",
                    "response_format": "flat",
                    **extra,
                },
                ADMIN,
            )
    finally:
        MultiSearchService._reset_singleton()
    body: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return body


@pytest.mark.parametrize("mode", ["semantic", "fts"])
def test_mcp_omni_returns_only_matching_files_from_both_repos(omni_env, mode) -> None:
    _, repos = omni_env
    body = _omni(repos, mode, VALUES)

    assert body["success"] is True, body
    by_repo: Dict[str, List[str]] = {}
    for row in body["results"]["results"]:
        by_repo.setdefault(row["source_repo"], []).append(row["file_path"])
    assert set(by_repo) == set(repos), body
    for alias, got in by_repo.items():
        assert sorted(got) == sorted(_expected(mode, repos[alias])), alias


def test_mcp_omni_semantic_loads_each_hnsw_index_once(omni_env) -> None:
    """Solo fan-out bypasses the HNSW cache (Bug #881), so every store query
    loads the index from disk (~277 ms on a real repo). A filtered search
    must still load each repository's index exactly once per request."""
    from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

    _, repos = omni_env
    with patch.object(
        HNSWIndexManager,
        "load_index",
        autospec=True,
        side_effect=HNSWIndexManager.load_index,
    ) as loads:
        body = _omni(repos, "semantic", VALUES)

    assert body["success"] is True, body
    by_repo: Dict[str, List[str]] = {}
    for row in body["results"]["results"]:
        by_repo.setdefault(row["source_repo"], []).append(row["file_path"])
    for alias, got in by_repo.items():
        assert sorted(got) == sorted(_expected("semantic", repos[alias])), alias
    assert loads.call_count == len(repos)


def test_mcp_omni_rejects_value_that_is_never_a_suffix(omni_env) -> None:
    _, repos = omni_env
    body = _omni(repos, "fts", ["py", "tar.gz"])
    assert body["success"] is False, body
    assert NEVER_A_SUFFIX in json.dumps(body)


@pytest.fixture
def multi_client(omni_env, monkeypatch) -> Iterator[Tuple[TestClient, Dict]]:
    from code_indexer.server.auth.dependencies import get_current_user
    from code_indexer.server.routes import multi_query_routes

    app, repos = omni_env
    monkeypatch.setattr(multi_query_routes, "_multi_search_service", None)
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    try:
        with (
            ranked_store_search(),
            _registry(repos, multi_query_routes.MultiSearchService),
        ):
            yield TestClient(app), repos
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("mode", ["semantic", "fts"])
def test_rest_multi_returns_only_matching_files(multi_client, mode) -> None:
    """Each repository's answer equals the shared oracle -- the same one the
    MCP omni test above checks, so REST multi and MCP omni agree."""
    client, repos = multi_client
    response = client.post(
        "/api/query/multi",
        json={
            "repositories": list(repos),
            "query": QUERY,
            "search_type": mode,
            "limit": LIMIT,
            "file_extensions": VALUES,
        },
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert set(results) == set(repos)
    for alias, rows in results.items():
        got = [row["file_path"] for row in rows]
        assert got == _expected(mode, repos[alias]), alias


def _set_omni_repo_cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    from code_indexer.server.services.config_service import get_config_service

    limits = get_config_service().get_config().multi_search_limits_config
    monkeypatch.setattr(limits, "omni_max_repos_per_search", cap)


def _post_multi(client: TestClient, repos: Dict, **extra: Any) -> Dict[str, Any]:
    response = client.post(
        "/api/query/multi",
        json={
            "repositories": list(repos),
            "query": QUERY,
            "search_type": "semantic",
            "limit": LIMIT,
            "file_extensions": VALUES,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    body: Dict[str, Any] = response.json()
    return body


def test_rest_multi_ignores_a_client_extension_deadline_of_zero(
    multi_client,
) -> None:
    """The request-wide deadline is computed by the server only: a client
    value (here one already in the past) is ignored, not honoured."""
    client, repos = multi_client
    body = _post_multi(client, repos, extension_deadline=0)
    assert not body.get("errors"), body
    assert set(body["results"]) == set(repos)


def test_rest_multi_client_far_future_deadline_cannot_extend_the_budget(
    multi_client, monkeypatch
) -> None:
    from code_indexer.server.services.config_service import get_config_service

    client, repos = multi_client
    timeouts = get_config_service().get_config().search_timeouts_config
    monkeypatch.setattr(timeouts, "search_code_handler_timeout_seconds", 0)
    monkeypatch.setattr(timeouts, "rest_query_handler_timeout_seconds", 0)
    body = _post_multi(client, repos, extension_deadline=1e12)
    assert not body["results"], body
    assert set(body["errors"]) == set(repos)
    assert all("time budget" in m for m in body["errors"].values()), body


def test_mcp_omni_ignores_a_client_extension_deadline(omni_env) -> None:
    _, repos = omni_env
    body = _omni(repos, "semantic", VALUES, extension_deadline=0)
    assert body["success"] is True, body
    sources = {row["source_repo"] for row in body["results"]["results"]}
    assert sources == set(repos), body


def _configured_provider(monkeypatch: pytest.MonkeyPatch) -> Tuple[str, List[float]]:
    """Give the corpus provider a (neutral, example) config, so its provider
    digest is a real one rather than the no-config sentinel; return that
    digest and a query vector pointing at the LOWEST-ranked corpus file."""
    from types import SimpleNamespace

    from code_indexer.server.services.coalescer_registry import _digest_for_provider
    from tests.unit.server.query.extension_filter_env_2047 import (
        RankedEmbeddingProvider,
    )

    config = SimpleNamespace(
        model="example-model",
        api_key="example-key",
        api_endpoint="https://example.com/v1",
        connect_timeout=5,
        timeout=30,
        max_retries=1,
        retry_delay=1,
        exponential_backoff=False,
    )
    monkeypatch.setattr(RankedEmbeddingProvider, "config", config, raising=False)
    provider = RankedEmbeddingProvider()
    return _digest_for_provider(provider), provider._vector_for("rank29")


def test_rest_multi_query_vector_is_computed_by_the_server(
    multi_client, monkeypatch
) -> None:
    client, repos = multi_client
    digest, client_vector = _configured_provider(monkeypatch)
    body = _post_multi(
        client,
        repos,
        precomputed_query_vector=client_vector,
        precomputed_query_vector_digest=digest,
    )
    for alias in repos:
        got = [row["file_path"] for row in body["results"][alias]]
        assert got == expected_semantic(VALUES, None), alias


def test_mcp_omni_query_vector_is_computed_by_the_server(omni_env, monkeypatch) -> None:
    _, repos = omni_env
    digest, client_vector = _configured_provider(monkeypatch)
    body = _omni(
        repos,
        "semantic",
        VALUES,
        precomputed_query_vector=client_vector,
        precomputed_query_vector_digest=digest,
    )
    assert body["success"] is True, body
    by_repo: Dict[str, List[str]] = {}
    for row in body["results"]["results"]:
        by_repo.setdefault(row["source_repo"], []).append(row["file_path"])
    for alias in repos:
        assert sorted(by_repo[alias]) == sorted(expected_semantic(VALUES, None))


def test_rest_multi_expired_budget_starts_no_repo_search(
    multi_client, monkeypatch
) -> None:
    from code_indexer.server.routes import multi_query_routes
    from code_indexer.server.services.config_service import get_config_service

    MultiSearchService = multi_query_routes.MultiSearchService
    client, repos = multi_client
    timeouts = get_config_service().get_config().search_timeouts_config
    monkeypatch.setattr(timeouts, "search_code_handler_timeout_seconds", 0)
    monkeypatch.setattr(timeouts, "rest_query_handler_timeout_seconds", 0)
    with patch.object(
        MultiSearchService,
        "_search_single_repo_sync",
        autospec=True,
        side_effect=MultiSearchService._search_single_repo_sync,
    ) as repo_searches:
        response = client.post(
            "/api/query/multi",
            json={
                "repositories": list(repos),
                "query": QUERY,
                "search_type": "semantic",
                "limit": LIMIT,
                "file_extensions": VALUES,
            },
        )
    assert response.status_code == 200, response.text
    assert repo_searches.call_count == 0
    errors = response.json()["errors"]
    assert set(errors) == set(repos)
    assert all("time budget" in message for message in errors.values()), errors


def test_rest_multi_rejects_more_repositories_than_the_omni_cap(
    multi_client, monkeypatch
) -> None:
    client, repos = multi_client
    _set_omni_repo_cap(monkeypatch, 1)
    response = client.post(
        "/api/query/multi",
        json={
            "repositories": list(repos),
            "query": QUERY,
            "search_type": "fts",
            "limit": LIMIT,
        },
    )
    assert response.status_code == 422, response.text
    assert "repo_count_cap_exceeded" in response.text


def test_mcp_omni_rejects_more_repositories_than_the_omni_cap(
    omni_env, monkeypatch
) -> None:
    _, repos = omni_env
    _set_omni_repo_cap(monkeypatch, 1)
    body = _omni(repos, "fts", VALUES)
    assert body["success"] is False, body
    assert "repo_count_cap_exceeded" in json.dumps(body)


def test_rest_multi_rejects_value_that_is_never_a_suffix(multi_client) -> None:
    client, repos = multi_client
    response = client.post(
        "/api/query/multi",
        json={
            "repositories": list(repos),
            "query": QUERY,
            "search_type": "fts",
            "limit": LIMIT,
            "file_extensions": ["tar.gz"],
        },
    )
    assert response.status_code == 422, response.text
    assert NEVER_A_SUFFIX in response.text
