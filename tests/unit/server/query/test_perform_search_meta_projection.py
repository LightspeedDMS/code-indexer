"""The search choke point (SemanticQueryManager._perform_search) serves a
restricted caller only their projected view of the catalog.

Real app, real SemanticQueryManager, real Tantivy and vector indexes over
the neutral catalog fixture (see catalog_search_env); only the embedding
provider is replaced. Deepening rounds are observed through the loop's own
per-round DEBUG log.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest

from code_indexer.server.services.cidx_meta_access.hit_projection import (
    CATALOG_HIT_FIELDS,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
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

pytestmark = pytest.mark.timeout(60)
ROUND_LOGGER = "code_indexer.server.services.cidx_meta_access.catalog_search"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    with catalog_app(tmp_path_factory.mktemp("catalog-choke")) as catalog:
        yield catalog


def _query(env: CatalogEnv, user: str, text: str, mode: str, limit: int) -> Any:
    sqm = env.app.state.semantic_query_manager
    with ranked_store_search():
        return sqm.query_user_repositories(
            username=user,
            query_text=text,
            repository_alias=CATALOG_ALIAS,
            limit=limit,
            search_mode=mode,
        )


def _rows(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(response["results"])


def _rounds(caplog: pytest.LogCaptureFixture) -> int:
    return sum(
        1
        for r in caplog.records
        if r.name == ROUND_LOGGER and r.getMessage().startswith("catalog round")
    )


def test_fts_for_the_canary_returns_nothing(env: CatalogEnv) -> None:
    assert _rows(_query(env, RESTRICTED_USER, CANARY, "fts", 10)) == []
    assert CANARY in dump(_query(env, ADMIN_USER, CANARY, "fts", 10))


def test_hidden_top_hits_still_fill_the_page(
    env: CatalogEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=ROUND_LOGGER)
    rows = _rows(_query(env, RESTRICTED_USER, QUERY, "fts", 3))
    assert len(rows) == 3
    assert all(r["file_path"].startswith("xray-patterns/__any__/") for r in rows)
    assert CANARY not in dump(rows)
    assert 1 < _rounds(caplog) <= 4

    caplog.clear()
    admin_rows = _rows(_query(env, ADMIN_USER, QUERY, "fts", 3))
    assert _rounds(caplog) == 0
    assert CANARY in dump(admin_rows)


@pytest.mark.parametrize("bypass_cache", [False, True], ids=["cache", "no-cache"])
def test_deepening_embeds_the_query_once(
    env: CatalogEnv, caplog: pytest.LogCaptureFixture, bypass_cache: bool
) -> None:
    # The 25 hidden widgets sit closer to rank13 than any visible file.
    deep_query = "rank13"
    caplog.set_level(logging.DEBUG, logger=ROUND_LOGGER)
    sqm = env.app.state.semantic_query_manager
    with ranked_store_search() as provider:
        real = provider._vector_for
        embedded: List[str] = []

        def counting(text: str) -> List[float]:
            if text == deep_query:
                embedded.append(text)
            return real(text)

        setattr(provider, "_vector_for", counting)
        response = sqm.query_user_repositories(
            username=RESTRICTED_USER,
            query_text=deep_query,
            repository_alias=CATALOG_ALIAS,
            limit=3,
            search_mode="semantic",
            no_embedding_cache_shortcut=bypass_cache,
        )
    rows = _rows(response)
    assert len(rows) == 3 and CANARY not in dump(rows)
    assert _rounds(caplog) > 1
    assert len(embedded) == 1


def test_semantic_rows_are_projected(env: CatalogEnv) -> None:
    response = _query(env, RESTRICTED_USER, "payments", "semantic", 40)
    dumped = dump(response)
    assert CANARY not in dumped and GAMMA not in dumped
    assert ALPHA in dumped
    for row in _rows(response):
        assert set(row) <= CATALOG_HIT_FIELDS
    assert CANARY in dump(_query(env, ADMIN_USER, "payments", "semantic", 40))


def _search_canary(
    env: CatalogEnv,
    user: str,
    repo_info: Optional[Dict[str, Any]] = None,
    query: str = CANARY,
) -> List[Any]:
    from code_indexer.server.services.cidx_meta_access.catalog_search import (
        TextRematch,
        search_for_caller,
    )

    sqm = env.app.state.semantic_query_manager
    info = repo_info or {"user_alias": CATALOG_ALIAS, "is_global": True}

    def run(k: int, mode: str, vector: Optional[List[float]]) -> List[Any]:
        return list(sqm._execute_fts_search(env.root, info["user_alias"], query, k))

    return search_for_caller(
        run,
        username=user,
        repo_info=info,
        repo_path=env.root,
        activated_repo_manager=sqm.activated_repo_manager,
        limit=10,
        search_mode="fts",
        rematch=TextRematch(query),
        merge=sqm._merge_hybrid_results,
        budget_spent=lambda: False,
    )


def test_ordinary_alias_serving_the_catalog_is_projected(env: CatalogEnv) -> None:
    ordinary = {"user_alias": f"{ALPHA}-global", "is_global": True}
    assert _search_canary(env, ADMIN_USER, ordinary)  # control: raw for admins
    assert _search_canary(env, RESTRICTED_USER, ordinary) == []
    visible = _search_canary(env, RESTRICTED_USER, ordinary, query=QUERY)
    assert visible and CANARY not in dump([r.to_dict() for r in visible])
    assert {r.source_repo for r in visible} == {CATALOG_ALIAS}


def test_stored_rows_of_an_alias_serving_the_catalog_are_rechecked(
    env: CatalogEnv,
) -> None:
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.server.services.cidx_meta_access.catalog_search import (
        reproject_job_result,
    )

    golden = Path(env.app.state.golden_repos_dir)
    AliasManager(str(golden / "aliases")).create_alias(
        "decoy-global", str(env.root), "decoy"
    )
    line = (env.root / f"{ALPHA}.md").read_text().splitlines()[2]
    row = {
        "file_path": f"{ALPHA}.md",
        "line_number": 3,
        "code_snippet": line,
        "similarity_score": 1.0,
        "repository_alias": "decoy-global",
    }
    stored = {"results": [row], "total_results": 1}
    rechecked = reproject_job_result(stored, RESTRICTED_USER, env.fx.access_service)
    assert CANARY in line and CANARY not in dump(rechecked)


def test_missing_access_service_serves_projected_hits_to_everyone(
    env: CatalogEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _search_canary(env, ADMIN_USER)  # control: admin sees canary hits
    monkeypatch.setattr(env.app.state, "access_filtering_service", None)
    assert _search_canary(env, ADMIN_USER) == []


def test_missing_registry_raises_instead_of_serving_unprojected(
    env: CatalogEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env.app.state, "backend_registry", None)
    with pytest.raises(RuntimeError):
        _search_canary(env, RESTRICTED_USER)


def test_hybrid_rows_are_projected(env: CatalogEnv) -> None:
    # The query echo in query_metadata is the caller's own input.
    rows = _rows(_query(env, RESTRICTED_USER, CANARY, "hybrid", 40))
    dumped = dump(rows)
    assert CANARY not in dumped and GAMMA not in dumped
    assert CANARY in dump(_rows(_query(env, ADMIN_USER, CANARY, "hybrid", 40)))
