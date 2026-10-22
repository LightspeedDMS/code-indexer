"""REST full-text and multi-repository catalog results, and every count in
them, depend only on the text the caller may see.

Real app routes, real indexes and a real small-preview payload cache
(test_query_meta_doors' environment); only authentication (the caller)
and the embedding provider are replaced.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.cache.handle_binding import HANDLE_BINDING_MARKER
from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    FULLTEXT_FORM_REFUSAL,
)
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY
from tests.unit.server.query.catalog_search_env import CATALOG_ALIAS, CatalogEnv
from tests.unit.server.routers.test_query_meta_doors import (  # noqa: F401
    _handles,
    client,
    env,
)

pytestmark = pytest.mark.timeout(90)
ABSENT = "zzqqxxnotthere"


def _query(http: TestClient, text: str, mode: str = "fts") -> Any:
    return http.post(
        "/api/query",
        json={
            "query_text": text,
            "repository_alias": CATALOG_ALIAS,
            "search_mode": mode,
            "limit": 10,
        },
    )


def _multi(http: TestClient, query: str, search_type: str = "fts") -> Any:
    return http.post(
        "/api/query/multi",
        json={
            "repositories": [CATALOG_ALIAS],
            "query": query,
            "search_type": search_type,
            "limit": 40,
        },
    )


def _fts_paths(body: Dict[str, Any]) -> List[str]:
    return sorted(str(row["path"]) for row in body["fts_results"])


def test_rest_fts_hidden_word_answers_like_an_absent_word(
    client: TestClient,  # noqa: F811
) -> None:
    hidden = _query(client, f"payments {CANARY}")
    absent = _query(client, f"payments {ABSENT}")
    assert hidden.status_code == absent.status_code == 200, hidden.text
    assert _fts_paths(hidden.json()) == _fts_paths(absent.json()) == []
    assert _fts_paths(_query(client, "payments").json())


@pytest.mark.parametrize("mode", ["fts", "hybrid"])
def test_rest_unevaluable_forms_are_refused_identically(
    client: TestClient,  # noqa: F811
    mode: str,
) -> None:
    words = (CANARY, ABSENT)
    answers = [_query(client, f"payments NOT {word}", mode) for word in words]
    assert [a.status_code for a in answers] == [400, 400]
    assert [a.json()["detail"] for a in answers] == [FULLTEXT_FORM_REFUSAL] * 2


def test_multi_fts_hidden_word_answers_like_an_absent_word(
    client: TestClient,  # noqa: F811
) -> None:
    hidden = _multi(client, f"payments {CANARY}").json()
    absent = _multi(client, f"payments {ABSENT}").json()
    assert hidden["results"].get(CATALOG_ALIAS, []) == []
    assert absent["results"].get(CATALOG_ALIAS, []) == []
    assert hidden["metadata"]["total_results"] == 0
    assert absent["metadata"]["total_results"] == 0
    visible = _multi(client, "payments").json()
    assert visible["results"].get(CATALOG_ALIAS)


def test_multi_unevaluable_forms_are_refused_identically(
    client: TestClient,  # noqa: F811
) -> None:
    answers = [_multi(client, f"payments NOT {word}") for word in (CANARY, ABSENT)]
    assert [a.status_code for a in answers] == [422, 422]
    assert [a.json()["detail"] for a in answers] == [FULLTEXT_FORM_REFUSAL] * 2


def test_ordinary_repository_boolean_query_is_not_refused(
    client: TestClient,  # noqa: F811
) -> None:
    ordinary = f"{ALPHA}-global"
    text = f"payments NOT {ABSENT}"
    multi = client.post(
        "/api/query/multi",
        json={"repositories": [ordinary], "query": text, "search_type": "fts"},
    )
    assert multi.status_code == 200, multi.text
    rows = multi.json()["results"][ordinary]
    assert [row["file_path"] for row in rows] == ["README.md"]
    rest = client.post(
        "/api/query",
        json={"query_text": text, "repository_alias": ordinary, "search_mode": "fts"},
    )
    assert rest.status_code == 200, rest.text
    assert [row["path"] for row in rest.json()["fts_results"]] == ["README.md"]


@pytest.mark.parametrize("search_type", ["fts", "regex"])
@pytest.mark.parametrize("query", [CANARY, ABSENT, "payments"])
def test_multi_counts_are_the_projected_rows(
    client: TestClient,  # noqa: F811
    search_type: str,
    query: str,
) -> None:
    response = _multi(client, query, search_type)
    assert response.status_code == 200, response.text
    body = response.json()
    rows = sum(len(group) for group in body["results"].values())
    assert body["metadata"]["total_results"] == rows


def test_multi_catalog_handles_are_bound_to_the_callers_view(
    client: TestClient,  # noqa: F811
    env: CatalogEnv,  # noqa: F811
) -> None:
    body = _multi(client, "payments", "semantic").json()
    handles = _handles(body)
    assert handles, "the small preview size must mint handles"
    assert all(HANDLE_BINDING_MARKER in handle for handle in handles)
    assert client.get(f"/cache/{handles[0]}").status_code == 200

    gam = env.fx.group_manager
    team = gam.get_group_by_name("fixture-team")
    assert team is not None
    gam.revoke_repo_access(ALPHA, team.id)
    try:
        assert client.get(f"/cache/{handles[0]}").status_code == 404
    finally:
        gam.grant_repo_access(ALPHA, team.id, granted_by="test")
