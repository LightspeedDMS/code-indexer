"""The catalog wiki follows the caller's catalog view on a mount-less pod.

``test_wiki_meta_visibility`` serves the catalog from this disk. Here
(``content_backend: delegated``) the ``cidx-meta-global`` alias points at a
pod-local snapshot holding ``.code-indexer`` only and every route reads the
catalog through a NON-local backend over its base clone: articles, the root
index, the sidebar and assets must be judged exactly as locally, and a
restricted page must never be written to the shared cache.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.services.cidx_meta_read_mirror import reset_mirrors
from code_indexer.server.startup import content_backend_wiring as wiring
from code_indexer.server.startup import content_writer_wiring as writer_wiring
from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.server.storage.shared.content_backend import LocalContentBackend
from cidx_wiki.server import routes as wiki_routes
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

try:
    from .test_wiki_meta_visibility import PNG, _Wiki
except (ImportError, ModuleNotFoundError):
    pytest.skip("v12.103.0 snapshot: code_indexer.server.wiki moved to cidx_wiki", allow_module_level=True)

pytestmark = pytest.mark.xfail(reason="v12.103.0 snapshot: upstream API changes", strict=False)


SNAPSHOT_LEAF = "v_1760000000"


class _DelegatedBackend(LocalContentBackend):
    """A NON-local backend over the very same tree (workspace-mcp's view)."""

    @property
    def is_local(self) -> bool:
        return False


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path / "catalog")
    (fixture.root / "xray-patterns" / CANARY / "diagram.png").write_bytes(PNG)
    (fixture.root / "xray-patterns" / "__any__" / "diagram.png").write_bytes(PNG)
    return fixture


@pytest.fixture
def wiki(
    fx: CidxMetaFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Wiki]:
    wiki_routes._reset_wiki_cache()
    w = _Wiki(fx, tmp_path)
    golden = Path(w.app.state.golden_repo_manager.golden_repos_dir)
    snapshot = golden / ".versioned" / "cidx-meta" / SNAPSHOT_LEAF
    (snapshot / ".code-indexer").mkdir(parents=True)
    aliases = AliasManager(str(golden / "aliases"))
    aliases.delete_alias("cidx-meta-global")
    aliases.create_alias("cidx-meta-global", str(snapshot))

    def record(name: str) -> Optional[Any]:
        if name != "cidx-meta":
            return None
        return SimpleNamespace(clone_path=str(fx.root), repo_url="local://cidx-meta")

    backend = _DelegatedBackend()
    monkeypatch.setattr(wiring, "server_content_backend", lambda: backend)
    monkeypatch.setattr(wiring, "_golden_repo_record", record)
    monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: object())
    reset_mirrors()
    fs_mod.clear_manifest_cache()
    from tests.unit.server._wiki_feature import wiki_context_installed

    try:
        with wiki_context_installed(w.app):
            yield w
    finally:
        reset_mirrors()
        fs_mod.clear_manifest_cache()
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
        f"/wiki/cidx-meta/_assets/xray-patterns/{CANARY}/diagram.png",
    ],
)
def test_denied_catalog_paths_answer_not_found_through_the_seam(
    wiki: _Wiki, path: str
) -> None:
    client = wiki.client(RESTRICTED_USER)
    response = client.get(path)
    missing = client.get("/wiki/cidx-meta/no-such-article")
    assert response.status_code == 404
    assert response.text == missing.text


def test_visible_asset_is_served_through_the_seam(wiki: _Wiki) -> None:
    client = wiki.client(RESTRICTED_USER)
    response = client.get("/wiki/cidx-meta/_assets/xray-patterns/__any__/diagram.png")
    assert response.status_code == 200
    assert response.content == PNG


@pytest.mark.parametrize("article", [ALPHA, "dependency-map/payments"])
def test_visible_articles_are_projected_through_the_seam(
    wiki: _Wiki, article: str
) -> None:
    response = wiki.client(RESTRICTED_USER).get(f"/wiki/cidx-meta/{article}")
    assert response.status_code == 200, response.text
    _hidden_absent(response.text)
    assert ALPHA in response.text
    assert "[restricted repository]" in response.text


def test_root_index_and_sidebar_list_no_denied_entry_through_the_seam(
    wiki: _Wiki,
) -> None:
    response = wiki.client(RESTRICTED_USER).get("/wiki/cidx-meta/")
    assert response.status_code == 200
    _hidden_absent(response.text)
    assert ALPHA in response.text
    admin = wiki.client(ADMIN_USER).get("/wiki/cidx-meta/")
    assert CANARY in admin.text


def test_restricted_page_is_not_cached_and_admin_sees_the_raw_page(
    wiki: _Wiki,
) -> None:
    restricted = wiki.client(RESTRICTED_USER).get(
        "/wiki/cidx-meta/dependency-map/payments"
    )
    admin = wiki.client(ADMIN_USER).get("/wiki/cidx-meta/dependency-map/payments")
    again = wiki.client(RESTRICTED_USER).get("/wiki/cidx-meta/dependency-map/payments")

    assert restricted.status_code == admin.status_code == 200
    assert CANARY in admin.text
    _hidden_absent(again.text)
