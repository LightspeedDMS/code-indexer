"""The catalog wiki follows the caller's catalog view.

The real wiki router on a FastAPI app with a real WikiCache (SQLite), a
real alias pointer to the fixture catalog and real access decisions; only
the authentication dependency is replaced to name the caller. The
restricted caller holds the ADMIN role (so ``manage_users``) but is not in
the admins group: restricted.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.server.wiki import routes as wiki_routes
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    add_depmap_extension,
    build_cidx_meta_fixture,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path_factory.mktemp("wiki-vis"))
    add_depmap_extension(fixture)
    (fixture.root / "xray-patterns" / CANARY / "diagram.png").write_bytes(PNG)
    (fixture.root / "xray-patterns" / "__any__" / "diagram.png").write_bytes(PNG)
    (fixture.root / "xray-patterns" / "__any__" / "link.png").symlink_to(
        fixture.root / "xray-patterns" / CANARY / "diagram.png"
    )
    (fixture.root / "dependency-map" / "linked.md").symlink_to(
        fixture.root / f"{CANARY}.md"
    )
    return fixture


@pytest.mark.parametrize(
    "spelling",
    [
        f"./xray-patterns/{CANARY}/diagram.png",
        f"xray-patterns/__any__/../{CANARY}/diagram.png",
        f"xray-patterns//{CANARY}/diagram.png",
        f"xray-patterns\\{CANARY}\\diagram.png",
        "xray-patterns/__any__/link.png",
    ],
)
def test_asset_judged_on_the_served_file_not_the_spelling(
    wiki: "_Wiki", spelling: str
) -> None:
    client = wiki.client(RESTRICTED_USER)
    response = client.get(f"/wiki/cidx-meta/_assets/{spelling}")
    missing = client.get("/wiki/cidx-meta/_assets/xray-patterns/__any__/none.png")
    assert response.status_code == 404
    assert response.text == missing.text


@pytest.mark.parametrize(
    "spelling",
    [
        "./xray-patterns/__any__/diagram.png",
        "xray-patterns/x/../__any__/diagram.png",
        "xray-patterns//__any__/diagram.png",
        "xray-patterns\\__any__\\diagram.png",
    ],
)
def test_only_the_canonical_spelling_of_a_visible_file_is_served(
    fx: CidxMetaFixture, spelling: str
) -> None:
    # Direct call: an HTTP client would normalise some spellings first.
    from fastapi import HTTPException

    from code_indexer.server.services.cidx_meta_access import (
        MetaProjector,
        resolve_meta_visibility,
    )
    from code_indexer.server.services.cidx_meta_access.catalog_articles import (
        catalog_decider,
    )
    from code_indexer.server.services.cidx_meta_access.universe_provider import (
        process_alias_universe,
    )
    from code_indexer.server.wiki.catalog_wiki import (
        CatalogWikiView,
        served_relative_path,
    )

    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    view = CatalogWikiView(
        vis,
        catalog_decider(fx.root, vis, fx.access_service),
        MetaProjector(
            vis,
            dep_map_dir=fx.dep_map_dir,
            universe=process_alias_universe(fx.registry),
        ),
        fx.root,
    )
    canonical = "xray-patterns/__any__/diagram.png"
    assert served_relative_path(view, canonical)[1] == canonical
    with pytest.raises(HTTPException) as refused:
        served_relative_path(view, spelling)
    assert refused.value.status_code == 404


def test_symlinked_article_to_a_hidden_file_answers_not_found(wiki: "_Wiki") -> None:
    client = wiki.client(RESTRICTED_USER)
    response = client.get("/wiki/cidx-meta/dependency-map/linked")
    assert response.status_code == 404
    assert CANARY not in client.get("/wiki/cidx-meta/").text


class _Wiki:
    def __init__(self, fx: CidxMetaFixture, tmp_path: Path) -> None:
        golden = tmp_path / "golden-repos"
        (golden / "aliases").mkdir(parents=True)
        AliasManager(str(golden / "aliases")).create_alias(
            "cidx-meta-global", str(fx.root)
        )
        self.db_path = str(tmp_path / "wiki.db")
        self.app = FastAPI()
        self.app.include_router(wiki_routes.wiki_router, prefix="/wiki")
        self.app.state.golden_repo_manager = SimpleNamespace(
            db_path=self.db_path,
            golden_repos_dir=str(golden),
            get_wiki_enabled=lambda alias: True,
        )
        self.app.state.access_filtering_service = fx.access_service
        self.app.state.backend_registry = SimpleNamespace(global_repos=fx.registry)
        self.app.state.semantic_query_manager = SimpleNamespace(
            query_user_repositories=lambda **kwargs: {
                "results": [
                    {"file_path": f"{ALPHA}.md", "similarity_score": 0.9},
                    {"file_path": f"{CANARY}.md", "similarity_score": 0.8},
                    {"file_path": "dependency-map/payments.md"},
                    {"file_path": f"dependency-map/{CANARY}.md"},
                ]
            }
        )

    def client(self, username: str) -> TestClient:
        user = User(
            username=username,
            password_hash="hash",
            role=UserRole.ADMIN,
            created_at=datetime.now(),
        )
        overrides = self.app.dependency_overrides
        overrides[wiki_routes.get_wiki_user_hybrid] = lambda: user
        overrides[get_current_user_hybrid] = lambda: user
        return TestClient(self.app)


@pytest.fixture
def wiki(fx: CidxMetaFixture, tmp_path: Path) -> Iterator[_Wiki]:
    wiki_routes._reset_wiki_cache()
    w = _Wiki(fx, tmp_path)
    try:
        yield w
    finally:
        wiki_routes._reset_wiki_cache()
        DatabaseConnectionManager.get_instance(w.db_path).close_all()


def _hidden_absent(text: str) -> None:
    assert CANARY not in text, text
    assert GAMMA not in text, text


@pytest.mark.parametrize(
    "path",
    [
        f"/wiki/cidx-meta/{CANARY}",
        f"/wiki/cidx-meta/dependency-map/{CANARY}",
        "/wiki/cidx-meta/dependency-map/inventory",
        f"/wiki/cidx-meta/_assets/xray-patterns/{CANARY}/diagram.png",
        "/wiki/cidx-meta/_assets/.code-indexer/index/meta.json",
    ],
)
def test_denied_catalog_paths_answer_not_found(wiki: _Wiki, path: str) -> None:
    client = wiki.client(RESTRICTED_USER)
    response = client.get(path)
    missing = client.get("/wiki/cidx-meta/no-such-article")
    assert response.status_code == 404
    assert response.text == missing.text


def test_visible_asset_is_served(wiki: _Wiki) -> None:
    client = wiki.client(RESTRICTED_USER)
    response = client.get("/wiki/cidx-meta/_assets/xray-patterns/__any__/diagram.png")
    assert response.status_code == 200


@pytest.mark.parametrize("article", [ALPHA, "dependency-map/payments"])
def test_visible_articles_are_projected(wiki: _Wiki, article: str) -> None:
    response = wiki.client(RESTRICTED_USER).get(f"/wiki/cidx-meta/{article}")
    assert response.status_code == 200
    _hidden_absent(response.text)
    assert ALPHA in response.text
    assert "[restricted repository]" in response.text


def test_restricted_request_writes_no_page_to_the_shared_cache(
    wiki: _Wiki, fx: CidxMetaFixture
) -> None:
    wiki.client(RESTRICTED_USER).get("/wiki/cidx-meta/dependency-map/payments")
    cache = wiki_routes._wiki_cache
    assert cache is not None
    page_file = fx.dep_map_dir / "payments.md"
    assert cache.get_page("cidx-meta", "dependency-map/payments", page_file) is None
    admin = wiki.client(ADMIN_USER).get("/wiki/cidx-meta/dependency-map/payments")
    assert admin.status_code == 200
    assert CANARY in admin.text


def test_root_index_and_sidebar_list_no_denied_entry(wiki: _Wiki) -> None:
    response = wiki.client(RESTRICTED_USER).get("/wiki/cidx-meta/")
    assert response.status_code == 200
    _hidden_absent(response.text)
    assert ALPHA in response.text
    admin = wiki.client(ADMIN_USER).get("/wiki/cidx-meta/")
    assert CANARY in admin.text


def test_search_results_list_no_denied_article(wiki: _Wiki) -> None:
    response = wiki.client(RESTRICTED_USER).get(
        "/wiki/cidx-meta/_search", params={"q": "payments"}
    )
    assert response.status_code == 200
    paths = [item["path"] for item in response.json()]
    assert paths == [ALPHA, "dependency-map/payments"]


CATALOG_COPY = "catalog-copy"
_USER = f"/wiki/u/{RESTRICTED_USER}/{CATALOG_COPY}"
_ROUTE_REQUESTS = {
    "/u/{username}/{alias}/_assets/{asset_path:path}": [
        f"{_USER}/_assets/xray-patterns/{CANARY}/diagram.png"
    ],
    "/u/{username}/{alias}/_search": [f"{_USER}/_search?q=payments"],
    "/u/{username}/{alias}/": [f"{_USER}/"],
    "/u/{username}/{alias}/{path:path}": [
        f"{_USER}/{CANARY}",
        f"{_USER}/dependency-map/payments",
    ],
    "/{repo_alias}/_assets/{asset_path:path}": [
        f"/wiki/cidx-meta/_assets/xray-patterns/{CANARY}/diagram.png"
    ],
    "/{repo_alias}/": ["/wiki/cidx-meta/"],
    "/{repo_alias}/_search": ["/wiki/cidx-meta/_search?q=payments"],
    "/{repo_alias}/{path:path}": [
        f"/wiki/cidx-meta/{CANARY}",
        "/wiki/cidx-meta/dependency-map/payments",
        f"/wiki/cidx-meta/{ALPHA}",
    ],
}


def _activate_catalog_copy(wiki: _Wiki, fx: CidxMetaFixture, tmp_path: Path) -> None:
    import json

    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    data_dir = tmp_path / "data"
    user_dir = data_dir / "activated-repos" / RESTRICTED_USER
    shutil.copytree(fx.root, user_dir / CATALOG_COPY, symlinks=True)
    (user_dir / f"{CATALOG_COPY}_metadata.json").write_text(
        json.dumps(
            {
                "user_alias": CATALOG_COPY,
                "golden_repo_alias": "cidx-meta",
                "current_branch": "main",
                "wiki_enabled": True,
                "activated_at": "2026-01-01T00:00:00+00:00",
                "last_accessed": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    wiki.app.state.activated_repo_manager = ActivatedRepoManager(data_dir=str(data_dir))


def test_every_wiki_route_applies_the_catalog_gate(
    wiki: _Wiki, fx: CidxMetaFixture, tmp_path: Path
) -> None:
    from fastapi.routing import APIRoute

    templates = {
        route.path
        for route in wiki_routes.wiki_router.routes
        if isinstance(route, APIRoute)
    }
    assert templates == set(_ROUTE_REQUESTS), "list every wiki route here"
    _activate_catalog_copy(wiki, fx, tmp_path)
    client = wiki.client(RESTRICTED_USER)
    for template, urls in _ROUTE_REQUESTS.items():
        for url in urls:
            response = client.get(url)
            assert CANARY not in response.text, (template, url, response.text)
            assert GAMMA not in response.text, (template, url, response.text)


def test_copy_of_catalog_under_another_alias_is_unrestricted_wiki(
    wiki: _Wiki, fx: CidxMetaFixture, tmp_path: Path
) -> None:
    # Control: an ordinary repository's wiki is never projected.
    plain = tmp_path / "plain"
    shutil.copytree(fx.root, plain, ignore=shutil.ignore_patterns(".git"))
    AliasManager(
        str(Path(wiki.app.state.golden_repo_manager.golden_repos_dir) / "aliases")
    ).create_alias(f"{ALPHA}-global", str(plain))
    response = wiki.client(RESTRICTED_USER).get(f"/wiki/{ALPHA}/{CANARY}")
    assert response.status_code == 200
    assert CANARY in response.text
