"""Restricted catalog full-text results are computed per shown document.

Real app, real handlers and routes, and a real Tantivy index holding one
document per CHUNK (as production indexing writes it); only the caller
(authentication) and the embedding provider are replaced. Through every
full-text door that can include the catalog, the set of documents
returned at any limit (truncated pages included) and every count stay the
same when only text the caller cannot see changes, even when the hidden
change multiplies a query word; each document yields one row; and a
document whose query words sit in different chunks is still found.
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.cache.payload_cache import PayloadCache, PayloadCacheConfig
from code_indexer.server.mcp.handlers.search.code_search import search_code
from tests.shared.cidx_meta_visibility_fixture import CANARY, RESTRICTED_USER
from tests.unit.server.query.catalog_search_env import (
    CATALOG_ALIAS,
    QUERY,
    RESTRICTED,
    CatalogEnv,
    catalog_app,
    dump,
    rebuild_fulltext,
    rebuild_indexes,
)
from tests.unit.server.query.extension_filter_env_2047 import ranked_store_search

pytestmark = pytest.mark.timeout(300)
PAYMENTS = "dependency-map/payments.md"
CASES: List[Tuple[str, int]] = [
    ("src", 40),
    ("python", 40),
    ("python src", 40),
    ("writes rows", 40),
    ("alpha", 1),
    ("alpha", 2),
    ("alpha", 10),
    ("payments", 1),
    ("payments", 2),
    ("payments", 10),
    ("alpha payments", 1),
]
JOURNAL_ROW = (
    f"| {CANARY} | alpha | ledger | Data contracts | writes journal rows "
    "| db/journal.sql |"
)
STEP_ROW = f"| 1 | {CANARY} | step 1 local |"
# Hidden-only edits: a hidden path, and hidden rows repeating a query word.
EDITS = {
    "hidden_path": (
        JOURNAL_ROW,
        JOURNAL_ROW.replace("db/journal.sql", "src/python.sql"),
    ),
    "hidden_alpha_boost": (STEP_ROW, STEP_ROW.replace("step 1 local", "alpha " * 12)),
    "hidden_payments_boost": (
        STEP_ROW,
        STEP_ROW.replace("step 1 local", "payments " * 12),
    ),
}
SPLIT_FILE = "xray-patterns/__any__/split.yaml"
Row = Tuple[str, int]
# (rows returned, the count the door REPORTS for them): /api/query/multi
# reports metadata.total_results; the other doors carry no count field, so
# they report their row count.
Answer = Tuple[List[Row], int]
Door = Callable[[TestClient, str, int], Answer]


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    tmp = tmp_path_factory.mktemp("fulltext-per-document")
    with catalog_app(tmp) as catalog:
        cache = PayloadCache(
            db_path=tmp / "payload.db",
            config=PayloadCacheConfig(preview_size_chars=100000),
        )
        cache.initialize()
        previous = getattr(catalog.app.state, "payload_cache", None)
        catalog.app.state.payload_cache = cache
        try:
            yield catalog
        finally:
            from code_indexer.server.multi.multi_search_service import (
                MultiSearchService,
            )

            MultiSearchService._reset_singleton()
            catalog.app.state.payload_cache = previous
            cache.close()


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


def _mcp(alias: Any, mode: str) -> Door:
    def run(_http: TestClient, text: str, limit: int) -> Answer:
        params = dict(
            query_text=text, repository_alias=alias, search_mode=mode, limit=limit
        )
        response = search_code(params, RESTRICTED)
        payload = json.loads(response["content"][0]["text"])
        assert payload["success"], payload
        assert CANARY not in dump(payload)
        results = payload["results"]
        if isinstance(results, dict):
            results = results.get("results", [])
        rows = [(r["file_path"], int(r.get("line_number") or 0)) for r in results]
        return rows, len(rows)

    return run


def _rest(mode: str) -> Door:
    def run(http: TestClient, text: str, limit: int) -> Answer:
        body = {
            "query_text": text,
            "repository_alias": CATALOG_ALIAS,
            "search_mode": mode,
            "limit": limit,
        }
        response = http.post("/api/query", json=body)
        assert response.status_code == 200, response.text
        assert CANARY not in dump(response.json())
        rows = [(r["path"], 0) for r in response.json()["fts_results"]]
        return rows, len(rows)

    return run


def _multi(http: TestClient, text: str, limit: int) -> Answer:
    body = {
        "repositories": [CATALOG_ALIAS],
        "query": text,
        "search_type": "fts",
        "limit": limit,
    }
    response = http.post("/api/query/multi", json=body)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert CANARY not in dump(answer)
    found = answer["results"].get(CATALOG_ALIAS, [])
    rows = [(r["file_path"], int(r.get("line_start") or 0)) for r in found]
    reported_count = int(answer["metadata"]["total_results"])
    return rows, reported_count


DOORS: Dict[str, Door] = {
    "mcp-single-fts": _mcp(CATALOG_ALIAS, "fts"),
    "mcp-single-hybrid": _mcp(CATALOG_ALIAS, "hybrid"),
    "mcp-omni-fts": _mcp([CATALOG_ALIAS], "fts"),
    "rest-fts": _rest("fts"),
    "rest-hybrid": _rest("hybrid"),
    "rest-multi": _multi,
}
# Doors whose answer also holds the semantic half's rows (one per chunk).
SEMANTIC_MIXED = {"mcp-single-hybrid"}


def _answers(http: TestClient, door: Door) -> Dict[Tuple[str, int], Answer]:
    out: Dict[Tuple[str, int], Answer] = {}
    with ranked_store_search():
        for text, limit in CASES:
            rows, reported_count = door(http, text, limit)
            out[(text, limit)] = (sorted(rows), reported_count)
    return out


@contextmanager
def _rewritten(root: Path, rel: str, old: str, new: str) -> Iterator[None]:
    path = root / rel
    original = path.read_text(encoding="utf-8")
    assert old in original
    try:
        path.write_text(original.replace(old, new), encoding="utf-8")
        rebuild_indexes(root)
        yield
    finally:
        path.write_text(original, encoding="utf-8")
        rebuild_indexes(root)


@pytest.mark.parametrize("edit", sorted(EDITS))
@pytest.mark.parametrize("door", sorted(DOORS))
def test_hidden_text_change_keeps_results_and_counts(
    env: CatalogEnv, http: TestClient, door: str, edit: str
) -> None:
    before = _answers(http, DOORS[door])
    assert any(rows for rows, _ in before.values())
    old, new = EDITS[edit]
    with _rewritten(env.root, PAYMENTS, old, new):
        after = _answers(http, DOORS[door])
    changed = {
        key: (before[key], after[key]) for key in before if before[key] != after[key]
    }
    assert changed == {}


@pytest.mark.parametrize("door", sorted(set(DOORS) - SEMANTIC_MIXED))
def test_one_row_per_document(env: CatalogEnv, http: TestClient, door: str) -> None:
    for key, (rows, reported_count) in _answers(http, DOORS[door]).items():
        paths = Counter(path for path, _ in rows)
        assert [p for p, n in paths.items() if n > 1] == [], key
        # The count a door reports is the number of rows it returns.
        assert reported_count == len(rows), key
        assert len(rows) <= key[1], key


@pytest.mark.parametrize("door", sorted(DOORS))
def test_words_in_different_chunks_find_the_document(
    env: CatalogEnv, http: TestClient, door: str
) -> None:
    split = env.root / SPLIT_FILE
    lines = ["name: zephyrword"] + [f"step{n}: filler" for n in range(8)]
    split.write_text("\n".join(lines + ["kind: quillword"]) + "\n", encoding="utf-8")
    try:
        rebuild_fulltext(env.root)
        with ranked_store_search():
            rows, reported_count = DOORS[door](http, "zephyrword quillword", 40)
    finally:
        split.unlink()
        rebuild_fulltext(env.root)
    if door in SEMANTIC_MIXED:
        assert (SPLIT_FILE, 1) in rows
        return
    assert [path for path, _ in rows] == [SPLIT_FILE]
    assert reported_count == 1


CAPS = (1, 2, 4, 8, 16)


def _page_outcomes(
    env: CatalogEnv, monkeypatch: pytest.MonkeyPatch
) -> Dict[Tuple[str, int, int], Any]:
    """Each case's page under each engine cap: its paths, or the refusal."""
    from code_indexer.server.query.semantic_query_manager import SearchParameterError
    from code_indexer.server.services.cidx_meta_access import fulltext_pages
    from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
        FulltextOptions,
        parse_fulltext_terms,
    )
    from code_indexer.server.services.cidx_meta_access.hit_projection import (
        open_catalog_scope,
    )
    from code_indexer.server.services.cidx_meta_access.universe_provider import (
        registry_of,
    )

    state = env.app.state
    out: Dict[Tuple[str, int, int], Any] = {}
    for cap in CAPS:
        # QUERY is held by many visible stored files: refused at small caps.
        for text, limit in CASES + [(QUERY, 1)]:
            scope = open_catalog_scope(
                RESTRICTED_USER,
                state.access_filtering_service,
                env.root,
                registry_of(state),
            )
            terms = parse_fulltext_terms(text, 0)
            assert scope is not None and terms is not None
            monkeypatch.setattr(fulltext_pages, "_engine_cap", lambda c=cap: c)
            try:
                rows = fulltext_pages.catalog_fulltext_rows(
                    env.root, FulltextOptions(), terms, scope, limit
                )
                out[(text, limit, cap)] = sorted(str(r["path"]) for r in rows)
            except SearchParameterError as refused:
                out[(text, limit, cap)] = str(refused)
    monkeypatch.undo()
    return out


@pytest.mark.parametrize("edit", sorted(EDITS))
def test_refusal_never_depends_on_hidden_text(
    env: CatalogEnv, edit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _page_outcomes(env, monkeypatch)
    assert any(isinstance(v, str) for v in before.values())
    assert any(isinstance(v, list) and v for v in before.values())
    old, new = EDITS[edit]
    with _rewritten(env.root, PAYMENTS, old, new):
        after = _page_outcomes(env, monkeypatch)
    changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert changed == {}
