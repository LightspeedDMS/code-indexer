"""Edge and failure paths of the per-document catalog full-text page.

Real app, real per-chunk Tantivy index over the neutral catalog fixture
(catalog_search_env); only the embedding provider is replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import pytest

from code_indexer.server.multi.fts_rows import open_fts_index
from code_indexer.server.multi.models import (
    MultiSearchMetadata,
    MultiSearchRequest,
    MultiSearchResponse,
)
from code_indexer.server.query.semantic_query_manager import SearchParameterError
from code_indexer.server.services.cidx_meta_access.catalog_fulltext import (
    rest_fulltext_rows,
)
from code_indexer.server.services.cidx_meta_access.catalog_search import (
    CatalogProjectionUnavailable,
    project_multi_fulltext,
)
from code_indexer.server.services.cidx_meta_access.fulltext_pages import (
    fulltext_page,
)
from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    parse_fulltext_terms,
)
from code_indexer.server.services.cidx_meta_access.hit_projection import (
    open_catalog_scope,
)
from code_indexer.server.services.cidx_meta_access.universe_provider import (
    registry_of,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    CANARY,
    RESTRICTED_USER,
)
from tests.unit.server.query.catalog_search_env import (
    CATALOG_ALIAS,
    QUERY,
    CatalogEnv,
    catalog_app,
    dump,
)
from tests.unit.server.query.extension_filter_env_2047 import ranked_store_search

pytestmark = pytest.mark.timeout(90)


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    with catalog_app(tmp_path_factory.mktemp("fulltext-edges")) as catalog:
        yield catalog


@dataclass
class _RestQuery:
    query_text: str
    limit: int = 10
    case_sensitive: bool = False
    regex: bool = False
    fuzzy: bool = False
    edit_distance: int = 0
    snippet_lines: int = 3


def _rest(env: CatalogEnv, user: str, request: _RestQuery) -> List[Dict[str, Any]]:
    return rest_fulltext_rows(
        open_fts_index(env.root), request, {}, CATALOG_ALIAS, env.root, user
    )


def test_rest_rows_refuse_an_unevaluable_form(env: CatalogEnv) -> None:
    with pytest.raises(SearchParameterError):
        _rest(env, RESTRICTED_USER, _RestQuery(f"payments NOT {CANARY}"))


def test_rest_rows_need_the_registry(
    env: CatalogEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.app.state, "backend_registry", None)
    with pytest.raises(CatalogProjectionUnavailable):
        _rest(env, RESTRICTED_USER, _RestQuery("payments"))


def test_rest_rows_for_an_admin_are_the_engine_rows(env: CatalogEnv) -> None:
    rows = _rest(env, ADMIN_USER, _RestQuery("payments"))
    engine = open_fts_index(env.root).search("payments", limit=10, snippet_lines=3)
    assert rows == engine


def _multi(rows: Optional[List[Dict[str, Any]]] = None) -> MultiSearchResponse:
    results = {CATALOG_ALIAS: rows or [{"file_path": "alpha.md", "line_start": 1}]}
    metadata = MultiSearchMetadata.model_validate(
        {
            "total_results": len(results[CATALOG_ALIAS]),
            "total_repos_searched": 1,
            "execution_time_ms": 0,
        }
    )
    return MultiSearchResponse.model_validate(
        {"results": results, "metadata": metadata}
    )


def _project(env: CatalogEnv, user: str, response: MultiSearchResponse) -> None:
    request = MultiSearchRequest.model_validate(
        {"repositories": [CATALOG_ALIAS], "query": "payments", "search_type": "fts"}
    )
    project_multi_fulltext(
        response, request, lambda: 10, user, env.app.state.access_filtering_service
    )


def test_multi_leaves_unrestricted_callers_and_failed_aliases(
    env: CatalogEnv,
) -> None:
    admin = _multi([{"file_path": f"xray-patterns/{CANARY}/w01.yaml"}])
    _project(env, ADMIN_USER, admin)
    assert CANARY in dump(admin.results)
    failed = _multi([{"file_path": f"xray-patterns/{CANARY}/w01.yaml"}])
    failed.errors = {CATALOG_ALIAS: "search failed"}
    _project(env, RESTRICTED_USER, failed)
    assert failed.results[CATALOG_ALIAS] == [
        {"file_path": f"xray-patterns/{CANARY}/w01.yaml"}
    ]


def test_multi_drops_a_catalog_group_it_cannot_project(
    env: CatalogEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.app.state, "backend_registry", None)
    response = _multi()
    _project(env, RESTRICTED_USER, response)
    assert CATALOG_ALIAS not in response.results
    assert response.metadata.total_results == 0


def test_page_stops_deepening_when_the_budget_is_spent(env: CatalogEnv) -> None:
    index = open_fts_index(env.root)
    state = env.app.state
    scope = open_catalog_scope(
        RESTRICTED_USER, state.access_filtering_service, env.root, registry_of(state)
    )
    assert scope is not None
    # The 25 hidden widget files rank first: a round of 24 shows nothing.
    terms = parse_fulltext_terms(QUERY, 0)
    assert terms is not None

    def asked_with(spent: bool) -> List[int]:
        asked: List[int] = []

        def search(word: str, k: int) -> List[Dict[str, Any]]:
            asked.append(k)
            return list(index.search(word, limit=k))

        fulltext_page(search, lambda: scope, terms, 12, lambda: spent)
        return asked

    assert asked_with(spent=True) == [24]
    assert asked_with(spent=False)[:2] == [24, 48]


def test_restricted_regex_fulltext_projects_each_hit(env: CatalogEnv) -> None:
    sqm = env.app.state.semantic_query_manager
    with ranked_store_search():
        rows = sqm.query_user_repositories(
            username=RESTRICTED_USER,
            query_text="widget",
            repository_alias=CATALOG_ALIAS,
            limit=10,
            search_mode="fts",
            regex=True,
        )["results"]
    assert rows and CANARY not in dump(rows)
