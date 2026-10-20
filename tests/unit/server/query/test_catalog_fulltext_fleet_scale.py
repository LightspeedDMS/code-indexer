"""Restricted catalog full-text at fleet scale: denied text crowds nothing out.

A real app over the neutral catalog fixture grown to fleet size: 900
repository description files and 1,100 pattern files under a hidden alias,
all denied to the restricted caller and all repeating the query word, plus
ONE visible pattern file that mentions it once. Real per-chunk Tantivy
index; only the caller and the embedding provider are replaced. The
visible document is the restricted answer at every door, denied documents
never take engine result slots, and the restricted latency is reported.
"""

from __future__ import annotations

import os
import statistics
import time
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from tests.shared.cidx_meta_visibility_fixture import CANARY, RESTRICTED_USER
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
WORD = "crowdword"
DESCRIPTIONS = 900
HIDDEN_PATTERNS = 200
VISIBLE = "xray-patterns/__any__/crowd.yaml"
LIMIT = 5


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    with catalog_app(tmp_path_factory.mktemp("fulltext-fleet")) as catalog:
        root = catalog.root
        for n in range(DESCRIPTIONS):
            (root / f"fleet-repo-{n:04d}.md").write_text(
                f"# fleet-repo-{n:04d}\n\n{WORD} {WORD} {WORD} service\n",
                encoding="utf-8",
            )
        patterns = root / "xray-patterns"
        for n in range(DESCRIPTIONS):
            scope_dir = patterns / f"fleet-repo-{n:04d}"
            scope_dir.mkdir()
            (scope_dir / "crowd.yaml").write_text(
                f"name: {WORD} {WORD} {WORD} {WORD}\n", encoding="utf-8"
            )
        for n in range(HIDDEN_PATTERNS):
            (patterns / CANARY / f"crowd{n:04d}.yaml").write_text(
                f"name: {WORD} {WORD} {WORD} {WORD}\n", encoding="utf-8"
            )
        (root / VISIBLE).write_text(f"name: {WORD} once\n", encoding="utf-8")
        rebuild_fulltext(root)
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


def _single(env: CatalogEnv, text: str = WORD) -> List[str]:
    sqm = env.app.state.semantic_query_manager
    with ranked_store_search():
        rows = sqm.query_user_repositories(
            username=RESTRICTED_USER,
            query_text=text,
            repository_alias=CATALOG_ALIAS,
            limit=LIMIT,
            search_mode="fts",
        )["results"]
    assert CANARY not in dump(rows)
    return [str(r["file_path"]) for r in rows]


def test_visible_document_is_found_past_every_denied_one(
    env: CatalogEnv, http: TestClient
) -> None:
    assert _single(env) == [VISIBLE]
    rest = http.post(
        "/api/query",
        json={
            "query_text": WORD,
            "repository_alias": CATALOG_ALIAS,
            "search_mode": "fts",
            "limit": LIMIT,
        },
    )
    assert rest.status_code == 200, rest.text
    assert [r["path"] for r in rest.json()["fts_results"]] == [VISIBLE]
    multi = http.post(
        "/api/query/multi",
        json={
            "repositories": [CATALOG_ALIAS],
            "query": WORD,
            "search_type": "fts",
            "limit": LIMIT,
        },
    )
    assert multi.status_code == 200, multi.text
    body = multi.json()
    assert [r["file_path"] for r in body["results"][CATALOG_ALIAS]] == [VISIBLE]
    assert body["metadata"]["total_results"] == 1


def test_denied_documents_take_no_engine_slots(
    env: CatalogEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: List[Dict[str, Any]] = []
    real_search = TantivyIndexManager.search

    def recording(self: TantivyIndexManager, *args: Any, **kwargs: Any) -> Any:
        asked.append(dict(kwargs))
        return real_search(self, *args, **kwargs)

    monkeypatch.setattr(TantivyIndexManager, "search", recording)
    assert _single(env) == [VISIBLE]
    # One word, answered completely by the first round of 2 * limit hits.
    assert [call["limit"] for call in asked] == [2 * LIMIT]
    allowed = asked[0]["allowed_paths"]
    assert VISIBLE in allowed
    assert not any(path.startswith("fleet-repo-") for path in allowed)
    assert not any(CANARY in path for path in allowed)


def test_allowed_paths_read_only_visible_directories(
    env: CatalogEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services.cidx_meta_access.hit_projection import (
        open_catalog_scope,
    )
    from code_indexer.server.services.cidx_meta_access.universe_provider import (
        registry_of,
    )

    state = env.app.state
    scope = open_catalog_scope(
        RESTRICTED_USER, state.access_filtering_service, env.root, registry_of(state)
    )
    assert scope is not None
    started = time.perf_counter()
    whole_tree = [
        d for d, _, _ in os.walk(env.root) if "/." not in d[len(str(env.root)) :]
    ]
    walk_ms = (time.perf_counter() - started) * 1000
    reads: List[str] = []
    real_scandir = os.scandir

    def counting(path: Any = ".") -> Any:
        reads.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting)
    started = time.perf_counter()
    allowed = scope.allowed_paths()
    listing_ms = (time.perf_counter() - started) * 1000
    monkeypatch.setattr(os, "scandir", real_scandir)
    print(
        f"\nFLEET allowed paths: {len(reads)} directory reads, {listing_ms:.1f} ms"
        f" (whole-tree walk: {len(whole_tree)} directories, {walk_ms:.1f} ms)"
    )
    assert len(whole_tree) > DESCRIPTIONS
    assert len(reads) <= 10
    assert VISIBLE in allowed
    assert not any(path.startswith("fleet-repo-") for path in allowed)
    assert not any("/fleet-repo-" in path or CANARY in path for path in allowed)


def test_restricted_fleet_latency(env: CatalogEnv) -> None:
    for _ in range(3):
        _single(env)
    samples = []
    for _ in range(20):
        started = time.perf_counter()
        _single(env, f"{WORD} once")
        samples.append((time.perf_counter() - started) * 1000)
    median = statistics.median(samples)
    print(f"\nFLEET restricted catalog fts: median_ms={median:.1f}")
    assert median < 2000
