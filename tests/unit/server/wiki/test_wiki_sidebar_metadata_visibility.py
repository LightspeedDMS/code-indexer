"""Catalog wiki metadata follows the same redaction as catalog prose.

An allowed article's sidebar title and category, and its page title, never
name a repository the caller may not see. Real fixture tree, real
WikiService sidebar and renderer, real projector; no mocks.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from code_indexer.server.services.cidx_meta_access import (
    MetaProjector,
    resolve_meta_visibility,
)
from code_indexer.server.services.cidx_meta_access.catalog_articles import (
    catalog_decider,
)
from code_indexer.server.services.cidx_meta_access.depmap_projection import (
    RESTRICTED_PLACEHOLDER,
)
from code_indexer.server.services.cidx_meta_access.universe_provider import (
    process_alias_universe,
)
from code_indexer.server.wiki.catalog_wiki import (
    CatalogWikiView,
    filter_sidebar,
    render_projected_page,
)
from code_indexer.server.wiki.wiki_service import WikiService
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path)
    (fixture.root / f"{ALPHA}.md").write_text(
        f"---\ntitle: {CANARY} architecture\ncategory: {CANARY}\n---\n\n"
        f"# {ALPHA}\n\n{ALPHA} body.\n",
        encoding="utf-8",
    )
    return fixture


def _view(fx: CidxMetaFixture) -> CatalogWikiView:
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    projector = MetaProjector(
        vis, dep_map_dir=fx.dep_map_dir, universe=process_alias_universe(fx.registry)
    )
    return CatalogWikiView(
        vis, catalog_decider(fx.root, vis, fx.access_service), projector, fx.root
    )


def test_sidebar_title_and_category_of_an_allowed_article_are_redacted(
    fx: CidxMetaFixture,
) -> None:
    shared = WikiService().build_sidebar_tree(fx.root, "cidx-meta")
    before = copy.deepcopy(shared)
    filtered = filter_sidebar(shared, _view(fx))
    text = json.dumps(filtered)
    assert CANARY not in text
    root = next(g for g in filtered if g["name"] == "Root")
    entries = [e for cat in root["categories"].values() for e in cat]
    alpha = next(e for e in entries if e["path"] == ALPHA)
    assert alpha["title"] == f"{RESTRICTED_PLACEHOLDER} architecture"
    assert RESTRICTED_PLACEHOLDER in root["categories"]
    # The shared (cached) sidebar is never modified.
    assert shared == before
    assert CANARY in json.dumps(shared)


def test_page_title_of_an_allowed_article_is_redacted(fx: CidxMetaFixture) -> None:
    page = render_projected_page(
        _view(fx),
        WikiService(),
        fx.root / f"{ALPHA}.md",
        f"{ALPHA}.md",
        "cidx-meta",
        "",
        None,
    )
    assert CANARY not in page["title"]
    assert page["title"] == f"{RESTRICTED_PLACEHOLDER} architecture"
    assert CANARY not in json.dumps(page["metadata"], default=str)
