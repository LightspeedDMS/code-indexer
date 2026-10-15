"""The golden repo wiki on a pod without the repositories (content seam).

With a delegated content backend the golden wiki lists the repository through
the backend's manifest and reads it through ``fetch``. These tests pin:

* parity -- the same repository served locally and through the backend gives
  the same status, HTML and asset bytes, route by route;
* no local IO -- under delegation nothing touches the pod's copy of the repo;
* fail loud -- a backend failure is a 503 carrying its message, never an
  empty wiki or a 404 for a file the manifest lists;
* sane call counts -- one manifest per cache window, batched sidebar fetches,
  and the render cache validated from manifest stamps (no refetch on a hit).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterator, Sequence, Union
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.services import file_service
from code_indexer.server.storage.shared.content_backend import FetchedFile
from code_indexer.server.wiki import routes as wiki_routes
from code_indexer.server.wiki.routes import (
    get_current_user_hybrid,
    get_wiki_user_hybrid,
    wiki_router,
)
from code_indexer.server.wiki.wiki_cache import FileStamp, WikiCache
from code_indexer.server.wiki.wiki_content_seam import (
    WikiContentUnavailable,
    _mtime_from_ns,
)
from code_indexer.server.wiki.wiki_service import WikiService
from tests.unit.server.cidx_meta_seam_fakes import MetaWorld
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir

ALIAS = "kb"

#: A wiki exercising ordering, categories, hidden paths, nesting, CRLF,
#: non-UTF-8, an image and an article whose name sorts differently by string
#: than by path component ("a-b.md" vs "a/b.md").
FILES: Dict[str, Union[str, bytes]] = {
    "home.md": "---\ntitle: Welcome\n---\n# Home\nSee [guide](guide/intro.md)\n"
    '<img src="img/logo.png">\n',
    "guide/intro.md": "---\ncategory: Basics\nviews: 7\n---\n# Intro\nBody\n",
    "guide/advanced.md": "---\ncategory: Basics\ncreated: 2024-03-15\n---\nAdv\n",
    "a-b.md": "# Dash\n",
    "a/b.md": "---\ncategory: Zed\nviews: 3\n---\n# Slash\n",
    "crlf.md": "# Crlf\r\nline one\r\nline two\r\n",
    "notes.txt": "plain text article\n",
    ".hidden/secret.md": "# Secret\n",
    "img/logo.png": b"\x89PNG\r\n\x1a\nfake-png-bytes",
    "binary.md": b"\xff\xfe not utf-8 \x80",
    "script.sh": "#!/bin/sh\n",
}

ROUTES = [
    "/wiki/kb/",
    "/wiki/kb/guide/intro",
    "/wiki/kb/guide/intro.md",
    "/wiki/kb/guide/advanced",
    "/wiki/kb/a-b",
    "/wiki/kb/a/b",
    "/wiki/kb/crlf",
    "/wiki/kb/notes.txt",
    "/wiki/kb/binary",
    "/wiki/kb/missing",
    "/wiki/kb/script.sh",
    "/wiki/kb/guide",
    "/wiki/kb/../outside",
    "/wiki/kb/%2E%2E/outside",
    "/wiki/kb/_assets/img/logo.png",
    "/wiki/kb/_assets/img/missing.png",
    "/wiki/kb/_assets/script.sh",
    "/wiki/kb/_assets/%2E%2E/escape.png",
]


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    file_service._collect_cache.clear()
    wiki_routes._reset_wiki_cache()
    yield
    file_service._collect_cache.clear()
    wiki_routes._reset_wiki_cache()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    w = MetaWorld(tmp_path, monkeypatch)
    for rel, data in FILES.items():
        w.seed(rel, data)
    return w


def _client(tmp_path: Path, name: str, target: Path) -> TestClient:
    """A wiki app whose golden alias ``kb`` points at *target*."""
    wiki_routes._reset_wiki_cache()
    user = MagicMock()
    user.username = "admin"
    app = FastAPI()
    app.dependency_overrides[get_wiki_user_hybrid] = lambda: user
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    app.include_router(wiki_router, prefix="/wiki")
    state_dir = tmp_path / f"cidx-state-{name}"
    state_dir.mkdir()
    fd, db_path = tempfile.mkstemp(suffix=".db", dir=state_dir)
    os.close(fd)
    manager = MagicMock()
    manager.get_wiki_enabled.return_value = True
    manager.db_path = db_path
    manager.golden_repos_dir = str(state_dir)
    make_aliases_dir(str(state_dir), ALIAS, str(target))
    app.state.golden_repo_manager = manager
    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = True
    return TestClient(app)


def _snapshot(client: TestClient) -> dict:
    out = {}
    for url in ROUTES:
        resp = client.get(url)
        out[url] = (
            resp.status_code,
            resp.headers.get("content-type"),
            resp.content,
        )
    return out


class TestParity:
    def test_every_route_matches_the_local_wiki(
        self, world: MetaWorld, tmp_path: Path
    ) -> None:
        world.local_mode()
        local = _snapshot(_client(tmp_path, "local", world.local_meta))
        world.delegated()
        with world.forbid_local_io():
            seam = _snapshot(_client(tmp_path, "seam", world.pod_meta))
        assert seam == local
        # The fixture exercises what it claims to.
        assert local["/wiki/kb/"][0] == 200
        assert local["/wiki/kb/missing"][0] == 404
        assert local["/wiki/kb/binary"][0] == 404
        assert local["/wiki/kb/_assets/img/logo.png"][2] == FILES["img/logo.png"]

    def test_index_page_without_home_matches(
        self, world: MetaWorld, tmp_path: Path
    ) -> None:
        for root in (world.local_meta, world.remote_meta):
            (root / "home.md").unlink()
        world.local_mode()
        local = _client(tmp_path, "local", world.local_meta).get("/wiki/kb/")
        world.delegated()
        with world.forbid_local_io():
            seam = _client(tmp_path, "seam", world.pod_meta).get("/wiki/kb/")
        assert local.status_code == seam.status_code == 200
        assert seam.text == local.text
        assert "Secret" not in seam.text
        assert seam.text.index("/wiki/kb/a/b") < seam.text.index("/wiki/kb/a-b")

    def test_sidebar_tree_from_texts_matches_local_walk(self, world: MetaWorld) -> None:
        from code_indexer.server.wiki.wiki_content_seam import SeamWikiTree

        world.delegated()
        source = file_service.content_seam_source(str(world.pod_meta))
        assert source is not None
        tree = SeamWikiTree.load(source)
        svc = WikiService()
        local = svc.build_sidebar_tree(world.local_meta, ALIAS)
        with world.forbid_local_io():
            seam = svc.build_sidebar_tree_from_texts(tree.read_visible_md_texts())
        assert seam == local

    def test_stamp_mtime_is_exactly_st_mtime(self, tmp_path: Path) -> None:
        target = tmp_path / "f.md"
        target.write_text("x")
        os.utime(target, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
        st = target.stat()
        assert _mtime_from_ns(st.st_mtime_ns) == st.st_mtime
        assert _mtime_from_ns(None) is None


class _FailingFetch:
    """Wraps a backend: per-file error records for the named paths."""

    def __init__(self, inner: Any, broken: Sequence[str]) -> None:
        self._inner = inner
        self._broken = set(broken)

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path: Any, **kwargs: Any) -> Any:
        return self._inner.manifest(repo_path, **kwargs)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        for record in self._inner.fetch(repo_path, paths):
            if record.path in self._broken:
                yield FetchedFile(record.path, None, b"", error="EACCES")
            else:
                yield record


class TestFailLoud:
    @pytest.mark.parametrize(
        "url", ["/wiki/kb/", "/wiki/kb/guide/intro", "/wiki/kb/_assets/img/logo.png"]
    )
    def test_backend_down_is_503_with_message(
        self, world: MetaWorld, tmp_path: Path, url: str
    ) -> None:
        world.delegated()
        world.backend.fail = RuntimeError("workspace-mcp unreachable")
        with world.forbid_local_io():
            resp = _client(tmp_path, "seam", world.pod_meta).get(url)
        assert resp.status_code == 503
        assert "workspace-mcp unreachable" in resp.json()["detail"]

    def test_fetch_down_after_listing_is_503(
        self, world: MetaWorld, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        world.delegated()
        client = _client(tmp_path, "seam", world.pod_meta)

        def failing_fetch(repo_path: Any, paths: Sequence[str]) -> Any:
            raise RuntimeError("fetch timed out")

        monkeypatch.setattr(world.backend, "fetch", failing_fetch)
        with world.forbid_local_io():
            resp = client.get("/wiki/kb/guide/intro")
        assert resp.status_code == 503
        assert "fetch timed out" in resp.json()["detail"]

    @pytest.mark.parametrize(
        "url,broken",
        [
            ("/wiki/kb/guide/intro", "guide/intro.md"),
            ("/wiki/kb/", "home.md"),
            ("/wiki/kb/_assets/img/logo.png", "img/logo.png"),
        ],
    )
    def test_listed_file_that_cannot_be_fetched_is_503_not_404(
        self,
        world: MetaWorld,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        url: str,
        broken: str,
    ) -> None:
        from code_indexer.server.startup import content_backend_wiring

        failing = _FailingFetch(world.backend, [broken])
        monkeypatch.setattr(
            content_backend_wiring, "server_content_backend", lambda: failing
        )
        with world.forbid_local_io():
            resp = _client(tmp_path, "seam", world.pod_meta).get(url)
        assert resp.status_code == 503
        assert broken in resp.json()["detail"]
        assert "EACCES" in resp.json()["detail"]


class TestCallCounts:
    def test_one_manifest_per_window_and_cache_hits_fetch_nothing(
        self, world: MetaWorld, tmp_path: Path
    ) -> None:
        world.delegated()
        client = _client(tmp_path, "seam", world.pod_meta)
        with world.forbid_local_io():
            assert client.get("/wiki/kb/guide/intro").status_code == 200
            first_fetches = list(world.backend.fetch_calls)
            assert client.get("/wiki/kb/guide/intro").status_code == 200
            assert client.get("/wiki/kb/").status_code == 200
        assert world.backend.manifest_calls == 1
        # Sidebar: ONE batched fetch of every visible .md; then the article.
        visible = [p for p in first_fetches[0]]
        assert "guide/intro.md" in visible and ".hidden/secret.md" not in visible
        assert first_fetches[1:] == [["guide/intro.md"]]
        # Second article request is a render-cache hit: nothing fetched; the
        # root then fetches only home.md.
        assert world.backend.fetch_calls[len(first_fetches) :] == [["home.md"]]

    def test_changed_mtime_in_manifest_invalidates_render_cache(
        self, world: MetaWorld, tmp_path: Path
    ) -> None:
        world.delegated()
        client = _client(tmp_path, "seam", world.pod_meta)
        assert "Body" in client.get("/wiki/kb/guide/intro").text
        target = world.remote_meta / "guide" / "intro.md"
        target.write_text("---\ncategory: Basics\n---\n# Intro\nChanged\n")
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        file_service._collect_cache.clear()  # the manifest window has passed
        with world.forbid_local_io():
            assert "Changed" in client.get("/wiki/kb/guide/intro").text


class TestWikiCacheStamps:
    def test_unknown_stamp_never_hits_and_is_never_stored(self, tmp_path: Path) -> None:
        cache = WikiCache(str(tmp_path / "w.db"))
        cache.ensure_tables()
        cache.put_article("r", "a", "<p/>", "A", FileStamp(mtime=None, size=3))
        assert cache.get_article("r", "a", FileStamp(mtime=None, size=3)) is None
        assert cache.get_article("r", "a", FileStamp(mtime=0.0, size=3)) is None
        cache.put_article("r", "a", "<p/>", "A", FileStamp(mtime=1.5, size=3))
        assert cache.get_article("r", "a", FileStamp(mtime=None, size=3)) is None
        assert cache.get_article("r", "a", FileStamp(mtime=1.5, size=4)) is None
        hit = cache.get_article("r", "a", FileStamp(mtime=1.5, size=3))
        assert hit is not None and hit["html"] == "<p/>"

    def test_local_row_validates_against_manifest_stamp(self, tmp_path: Path) -> None:
        cache = WikiCache(str(tmp_path / "w.db"))
        cache.ensure_tables()
        f = tmp_path / "a.md"
        f.write_text("abc")
        cache.put_article("r", "a", "<p/>", "A", f)
        st = f.stat()
        stamp = FileStamp(mtime=_mtime_from_ns(st.st_mtime_ns), size=st.st_size)
        assert cache.get_article("r", "a", stamp) is not None

    def test_put_sidebar_with_max_mtime_does_not_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from code_indexer.server.wiki import wiki_cache

        def no_walk(repo_dir: Path) -> float:
            raise AssertionError("walked the repository")

        monkeypatch.setattr(wiki_cache, "_max_mtime_of_md_files", no_walk)
        cache = WikiCache(str(tmp_path / "w.db"))
        cache.ensure_tables()
        cache.put_sidebar("r", [{"x": 1}], tmp_path / "absent", max_mtime=2.0)
        assert cache.get_sidebar("r", tmp_path / "absent") == [{"x": 1}]


class TestPopulateViews:
    def _views(self, cache: WikiCache) -> dict:
        return {
            row["article_path"]: row["real_views"]
            for row in cache.get_all_view_counts(ALIAS)
        }

    def test_delegated_seeding_matches_local(
        self, world: MetaWorld, tmp_path: Path
    ) -> None:
        svc = WikiService()
        world.local_mode()
        local_cache = WikiCache(str(tmp_path / "local.db"))
        local_cache.ensure_tables()
        svc.populate_views_from_front_matter(ALIAS, world.local_meta, local_cache)

        world.delegated()
        seam_cache = WikiCache(str(tmp_path / "seam.db"))
        seam_cache.ensure_tables()
        with world.forbid_local_io():
            svc.populate_views_from_front_matter(ALIAS, world.pod_meta, seam_cache)
        assert self._views(seam_cache) == self._views(local_cache)
        assert self._views(seam_cache) == {"guide/intro": 7, "a/b": 3}

    def test_backend_failure_raises(self, world: MetaWorld, tmp_path: Path) -> None:
        world.delegated()
        world.backend.fail = RuntimeError("workspace-mcp unreachable")
        cache = WikiCache(str(tmp_path / "seam.db"))
        cache.ensure_tables()
        with world.forbid_local_io(), pytest.raises(WikiContentUnavailable):
            WikiService().populate_views_from_front_matter(ALIAS, world.pod_meta, cache)
        assert self._views(cache) == {}


def test_md_paths_follow_rglob_order(world: MetaWorld) -> None:
    from code_indexer.server.wiki.wiki_content_seam import SeamWikiTree

    world.delegated()
    source = file_service.content_seam_source(str(world.pod_meta))
    assert source is not None
    tree = SeamWikiTree.load(source)
    expected = [
        str(PurePosixPath(p.relative_to(world.local_meta)))
        for p in sorted(world.local_meta.rglob("*.md"))
    ]
    assert tree.md_paths() == expected
