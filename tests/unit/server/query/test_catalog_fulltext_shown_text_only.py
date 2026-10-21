"""Catalog full-text judging is a function of the shown text alone.

Real app, real per-chunk Tantivy index over the neutral catalog fixture;
only the caller and the embedding provider are replaced. Two hidden
aliases, one whose name begins with the query word, make the projection
show the SAME line of a visible description: every full-text door answers
the same for both. Text the caller is shown (a placeholder too) matches.
A long line the projection changes is judged in milliseconds.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    parse_fulltext_terms,
)
from code_indexer.server.services.cidx_meta_access.hit_projection import (
    open_catalog_scope,
)
from code_indexer.server.services.cidx_meta_access.universe_provider import (
    registry_of,
)
from tests.shared.cidx_meta_visibility_fixture import ALPHA, RESTRICTED_USER
from tests.unit.server.query.catalog_search_env import (
    CATALOG_ALIAS,
    RESTRICTED,
    CatalogEnv,
    catalog_app,
    dump,
    rebuild_fulltext,
)
from tests.unit.server.query.extension_filter_env_2047 import ranked_store_search

pytestmark = pytest.mark.timeout(300)
WORD = "quokka"
HIDDEN_ALIASES = (f"{WORD}hidden", "zedhidden")
DESCRIPTION = f"{ALPHA}.md"
SHOWN_LINE = f"zeta [restricted repository] {WORD}"
Answer = Tuple[List[str], int]


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    with catalog_app(tmp_path_factory.mktemp("shown-text-only")) as catalog:
        for alias in HIDDEN_ALIASES:
            catalog.fx.registry.register_repo(
                alias_name=f"{alias}-global",
                repo_name=alias,
                repo_url=None,
                index_path=f"/golden/{alias}",
            )
        yield catalog


@pytest.fixture
def http(env: CatalogEnv) -> Iterator[TestClient]:
    from code_indexer.server.auth import dependencies

    overrides = env.app.dependency_overrides
    deps = (dependencies.get_current_user, dependencies.get_current_user_hybrid)
    for dep in deps:
        overrides[dep] = lambda: RESTRICTED
    try:
        yield TestClient(env.app)
    finally:
        for dep in deps:
            overrides.pop(dep, None)


@contextmanager
def _described(env: CatalogEnv, line: str) -> Iterator[None]:
    path = env.root / DESCRIPTION
    original = path.read_text(encoding="utf-8")
    try:
        path.write_text(original + line + "\n", encoding="utf-8")
        rebuild_fulltext(env.root)
        yield
    finally:
        path.write_text(original, encoding="utf-8")
        rebuild_fulltext(env.root)


def _scope(env: CatalogEnv) -> Any:
    state = env.app.state
    scope = open_catalog_scope(
        RESTRICTED_USER, state.access_filtering_service, env.root, registry_of(state)
    )
    assert scope is not None
    return scope


def _doors(env: CatalogEnv, http: TestClient, text: str) -> Dict[str, Answer]:
    sqm = env.app.state.semantic_query_manager
    with ranked_store_search():
        single = sqm.query_user_repositories(
            username=RESTRICTED_USER,
            query_text=text,
            repository_alias=CATALOG_ALIAS,
            limit=10,
            search_mode="fts",
        )["results"]
    rest = http.post(
        "/api/query",
        json={
            "query_text": text,
            "repository_alias": CATALOG_ALIAS,
            "search_mode": "fts",
            "limit": 10,
        },
    )
    multi = http.post(
        "/api/query/multi",
        json={
            "repositories": [CATALOG_ALIAS],
            "query": text,
            "search_type": "fts",
            "limit": 10,
        },
    )
    assert rest.status_code == 200 and multi.status_code == 200
    multi_body = multi.json()
    multi_rows = multi_body["results"].get(CATALOG_ALIAS, [])
    for hidden in HIDDEN_ALIASES:
        assert hidden not in dump([single, rest.json(), multi_body])
    return {
        "single": (sorted(r["file_path"] for r in single), len(single)),
        "rest": (
            sorted(r["path"] for r in rest.json()["fts_results"]),
            len(rest.json()["fts_results"]),
        ),
        "multi": (
            sorted(r["file_path"] for r in multi_rows),
            int(multi_body["metadata"]["total_results"]),
        ),
    }


@pytest.mark.parametrize(
    "text", [WORD, f"zeta {WORD}", "restricted", "restricted repository"]
)
def test_same_shown_line_answers_the_same_whatever_was_hidden(
    env: CatalogEnv, http: TestClient, text: str
) -> None:
    answers = []
    for hidden in HIDDEN_ALIASES:
        with _described(env, f"zeta {hidden} {WORD}"):
            shown = _scope(env).shown_lines(DESCRIPTION)[1]
            assert shown[-1] == SHOWN_LINE
            answers.append(_doors(env, http, text))
    assert answers[0] == answers[1]
    for paths, count in answers[0].values():
        assert DESCRIPTION in paths and count == len(paths)


def _single(env: CatalogEnv, text: str, **options: Any) -> List[str]:
    sqm = env.app.state.semantic_query_manager
    with ranked_store_search():
        rows = sqm.query_user_repositories(
            username=RESTRICTED_USER,
            query_text=text,
            repository_alias=CATALOG_ALIAS,
            limit=40,
            search_mode="fts",
            **options,
        )["results"]
    return sorted(str(r["file_path"]) for r in rows)


def _rest(http: TestClient, text: str, **options: Any) -> List[str]:
    body = {
        "query_text": text,
        "repository_alias": CATALOG_ALIAS,
        "search_mode": "fts",
        "limit": 40,
        **options,
    }
    response = http.post("/api/query", json=body)
    assert response.status_code == 200, response.text
    return sorted(str(r["path"]) for r in response.json()["fts_results"])


def test_path_filter_applies_to_every_catalog_document(
    env: CatalogEnv, http: TestClient
) -> None:
    pattern = "xray-patterns/*"
    multi = http.post(
        "/api/query/multi",
        json={
            "repositories": [CATALOG_ALIAS],
            "query": "alpha",
            "search_type": "fts",
            "limit": 40,
            "path_filter": pattern,
        },
    )
    assert multi.status_code == 200, multi.text
    multi_paths = [
        str(r["file_path"]) for r in multi.json()["results"].get(CATALOG_ALIAS, [])
    ]
    for paths in (
        _single(env, "alpha", path_filter=pattern),
        _rest(http, "alpha", path_filter=pattern),
        multi_paths,
    ):
        assert all(path.startswith("xray-patterns/") for path in paths), paths
    assert DESCRIPTION in _single(env, "alpha")


def test_case_sensitive_query_respects_case(env: CatalogEnv, http: TestClient) -> None:
    with _described(env, "zeta Mixedword"):
        for door in (
            lambda text, **o: _single(env, text, **o),
            lambda text, **o: _rest(http, text, **o),
        ):
            assert DESCRIPTION not in door("mixedword", case_sensitive=True)
            assert DESCRIPTION in door("Mixedword", case_sensitive=True)
            assert DESCRIPTION in door("mixedword")


def test_a_long_changed_line_is_judged_in_milliseconds(env: CatalogEnv) -> None:
    long_line = f"{WORD} " + " ".join(["zedhidden", "filler"] * 300)
    assert len(long_line) >= 4096
    with _described(env, long_line):
        scope = _scope(env)
        raw, shown = scope.shown_lines(DESCRIPTION)
        assert raw[-1] != shown[-1]
        terms = parse_fulltext_terms(WORD, 0)
        assert terms is not None
        started = time.perf_counter()
        found = scope.shown_match(DESCRIPTION, terms)
        elapsed = time.perf_counter() - started
    assert found is not None
    assert elapsed < 0.1, elapsed


def _rest_snippets(http: TestClient, snippet_lines: int) -> List[str]:
    response = http.post(
        "/api/query",
        json={
            "query_text": WORD,
            "repository_alias": CATALOG_ALIAS,
            "search_mode": "fts",
            "limit": 10,
            "snippet_lines": snippet_lines,
        },
    )
    assert response.status_code == 200, response.text
    return [
        r["snippet"] for r in response.json()["fts_results"] if r["path"] == DESCRIPTION
    ]


@pytest.mark.parametrize("snippet_lines", [0, 1, 2])
def test_snippet_window_follows_the_request_only(
    env: CatalogEnv, http: TestClient, snippet_lines: int
) -> None:
    snippets = []
    for hidden in HIDDEN_ALIASES:
        with _described(env, f"zeta {hidden} {WORD}"):
            shown = _scope(env).shown_lines(DESCRIPTION)[1]
            # The appended line is the anchor and the document's last line:
            # the window is the snippet_lines shown lines before it, then it.
            anchor = len(shown)
            first = max(0, anchor - 1 - snippet_lines)
            expected = "" if snippet_lines == 0 else "\n".join(shown[first:anchor])
            assert _rest_snippets(http, snippet_lines) == [expected]
            snippets.append(expected)
    assert snippets[0] == snippets[1]


def test_an_incomplete_gather_is_refused_at_every_door(
    env: CatalogEnv, http: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.mcp.handlers.search.code_search import search_code
    from code_indexer.server.services.cidx_meta_access import fulltext_pages
    from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
        FULLTEXT_TOO_BROAD,
    )

    # "widget" matches more stored pattern files than a cap of 1 can hold.
    monkeypatch.setattr(fulltext_pages, "_engine_cap", lambda: 1)
    with ranked_store_search():
        mcp = search_code(
            {
                "query_text": "widget",
                "repository_alias": CATALOG_ALIAS,
                "search_mode": "fts",
            },
            RESTRICTED,
        )
    assert FULLTEXT_TOO_BROAD in mcp["content"][0]["text"]
    rest = http.post(
        "/api/query",
        json={
            "query_text": "widget",
            "repository_alias": CATALOG_ALIAS,
            "search_mode": "fts",
        },
    )
    assert rest.status_code == 400 and FULLTEXT_TOO_BROAD in rest.text
    multi = http.post(
        "/api/query/multi",
        json={
            "repositories": [CATALOG_ALIAS],
            "query": "widget",
            "search_type": "fts",
        },
    )
    assert multi.status_code in (400, 422) and FULLTEXT_TOO_BROAD in multi.text
