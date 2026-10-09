"""Parallel multi-provider search fills the requested limit.

With both embedding providers configured, an unfiltered semantic REST
``/api/query`` routes to the parallel strategy automatically. Each provider
must fetch at least the requested number of results; otherwise a request for
more results than the per-provider fetch cap cannot be filled.

Real route, real SemanticQueryManager routing, health gating, score gate and
fusion over a repository of 400 neutral, distinct files. Replaced: the
per-provider search call (each provider's embedding + store lookup, an
external service) by a fake that returns that provider's distinct hits up to
the fetch limit it was asked for; authentication; the activated-repo listing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.query.semantic_query_manager import (
    QueryResult,
    SemanticQueryManager,
)
from tests.unit.server._isolated_app import isolated_app
from tests.unit.server.query.test_file_extensions_front_doors_2047 import ADMIN

REPO_ALIAS = "example-repo"
FILE_COUNT = 400
REQUESTED = 50


def _files(repo: Path) -> List[str]:
    return sorted(str(p.relative_to(repo)) for p in (repo / "src").glob("example_*.py"))


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Tuple[Any, Path]]:
    root = tmp_path_factory.mktemp("parallel-recall-2108")
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    for i in range(FILE_COUNT):
        (repo / "src" / f"example_{i:03d}.py").write_text(f"def op_{i}(): pass\n")
    with pytest.MonkeyPatch.context() as mp:
        # Both providers configured -> automatic parallel routing.
        mp.setenv("VOYAGE_API_KEY", "example-voyage-key")
        mp.setenv("CO_API_KEY", "example-cohere-key")
        with isolated_app(root / "app") as app:
            yield app, repo


def _provider_search(hits: Dict[str, List[str]], limits: Dict[str, int]) -> Any:
    def _search(**kwargs: Any) -> List[QueryResult]:
        provider = kwargs["provider_name"]
        limits[provider] = kwargs["limit"]
        return [
            QueryResult(
                file_path=path,
                line_number=1,
                code_snippet=f"chunk of {path}",
                similarity_score=0.95 - rank * 0.0001,
                repository_alias=kwargs["repository_alias"],
                source_provider=provider,
            )
            for rank, path in enumerate(hits[provider][: kwargs["limit"]])
        ]

    return _search


def _query(env: Tuple[Any, Path], hits: Dict[str, List[str]]) -> Tuple[Any, dict]:
    from code_indexer.server.auth.dependencies import get_current_user

    app, repo = env
    limits: Dict[str, int] = {}
    arm = app.state.semantic_query_manager.activated_repo_manager
    user_repos = [{"user_alias": REPO_ALIAS, "repo_path": str(repo)}]
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    try:
        with (
            patch.object(arm, "list_activated_repositories", return_value=user_repos),
            patch.object(arm, "get_activated_repo_path", return_value=str(repo)),
            patch.object(
                SemanticQueryManager,
                "_search_with_provider",
                side_effect=_provider_search(hits, limits),
                autospec=False,
            ),
        ):
            response = TestClient(app).post(
                "/api/query",
                json={
                    "query_text": "example operation",
                    "repository_alias": REPO_ALIAS,
                    "limit": REQUESTED,
                    "search_mode": "semantic",
                },
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    return response, limits


def test_parallel_search_fills_a_limit_above_the_fetch_cap(env) -> None:
    _, repo = env
    files = _files(repo)
    hits = {"voyage-ai": files, "cohere": list(reversed(files))}
    response, limits = _query(env, hits)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["query_metadata"]["effective_query_strategy"] == "parallel"
    assert set(limits) == {"voyage-ai", "cohere"}
    assert all(limit >= REQUESTED for limit in limits.values()), limits
    paths = [row["file_path"] for row in data["results"]]
    assert len(paths) == REQUESTED
    assert len(set(paths)) == REQUESTED


def test_one_empty_provider_still_fills_the_limit(env) -> None:
    _, repo = env
    hits = {"voyage-ai": _files(repo)[:REQUESTED], "cohere": []}
    response, limits = _query(env, hits)
    assert response.status_code == 200, response.text
    assert all(limit >= REQUESTED for limit in limits.values()), limits
    paths = [row["file_path"] for row in response.json()["results"]]
    assert sorted(paths) == _files(repo)[:REQUESTED]
