"""Wiki pages pass repository-derived HTML through an allowlist before rendering.

Driven through the real wiki router, real WikiService, real SQLite WikiCache and
real Jinja templates. Only the golden-repo manager and access service (external
collaborators of the router) are stand-ins, as in the other wiki route tests.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Iterator, List
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.utils.html_sanitizer import HtmlAllowlist
from code_indexer.server.wiki.wiki_cache import WikiCache

from code_indexer.server.wiki.routes import (
    _reset_wiki_cache,
    get_current_user_hybrid,
    get_wiki_user_hybrid,
    wiki_router,
)
from tests.unit.server.wiki.wiki_test_helpers import make_aliases_dir

ALIAS = "example-repo"
BODY_START = "BODYSTARTMARKER"
BODY_END = "BODYENDMARKER"


OWNER = "example-user"
USER_WIKI = f"/wiki/u/{OWNER}/{ALIAS}"


def _make_app(repo_dir: Path, *, admin_viewer: bool = False) -> FastAPI:
    """Golden wiki of ``repo_dir`` as ALIAS; also OWNER's user wiki of it,
    readable by an admin viewer."""
    _reset_wiki_cache()
    user = MagicMock()
    user.username = "example-admin" if admin_viewer else OWNER
    app = FastAPI()
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    app.dependency_overrides[get_wiki_user_hybrid] = lambda: user
    app.include_router(wiki_router, prefix="/wiki")

    db_fd, db_path = tempfile.mkstemp(suffix=".db", dir=str(repo_dir.parent))
    os.close(db_fd)
    golden_dir = repo_dir.parent / "golden-repos"
    make_aliases_dir(str(golden_dir), ALIAS, str(repo_dir))

    app.state.golden_repo_manager = MagicMock()
    app.state.golden_repo_manager.get_wiki_enabled.return_value = True
    app.state.golden_repo_manager.db_path = db_path
    app.state.golden_repo_manager.golden_repos_dir = str(golden_dir)
    app.state.access_filtering_service = MagicMock()
    app.state.access_filtering_service.is_admin_user.return_value = admin_viewer
    app.state.access_filtering_service.get_accessible_repos.return_value = {ALIAS}
    app.state.activated_repo_manager = MagicMock()
    app.state.activated_repo_manager.get_wiki_enabled.return_value = True
    app.state.activated_repo_manager.get_activated_repo_path.return_value = str(
        repo_dir
    )
    return app


@pytest.fixture
def repo_dir(tmp_path: Path) -> Iterator[Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    yield repo
    _reset_wiki_cache()


def _article_body(html: str) -> str:
    """The rendered Markdown region, between the two marker paragraphs."""
    assert BODY_START in html and BODY_END in html, html
    return html.split(BODY_START, 1)[1].split(BODY_END, 1)[0]


ACTIVE_CONTENT_ARTICLE = f"""# Page

{BODY_START}

<script>alert(1)</script>

<img src=x onerror="alert(1)">

<a href="javascript:alert(1)">click</a>

<iframe src="https://example.com/frame"></iframe>

<svg onload="alert(1)"><circle r="1"/></svg>

<style>p {{ color: red; }}</style>

<p style="color: red" onclick="alert(1)">styled</p>

<form action="https://example.com/post"><input name="q"></form>

<img src="data:image/png;base64,AAAA" alt="inline">

{BODY_END}
"""

# Markup that must never survive in the article region (lower-cased match).
FORBIDDEN_FRAGMENTS = (
    "<script",
    "onerror",
    "javascript:",
    "<iframe",
    "<svg",
    "onload",
    "<style",
    "style=",
    "onclick",
    "<form",
    "<input",
    "data:",
)


def _assert_inert(body: str) -> None:
    lowered = body.lower()
    present = [f for f in FORBIDDEN_FRAGMENTS if f in lowered]
    assert present == [], f"active markup survived: {present}\n{body}"


# (file written, URL, admin viewer): golden and user wiki, article and root.
ARTICLE_ROUTES = [
    ("page.md", f"/wiki/{ALIAS}/page", False),
    ("home.md", f"/wiki/{ALIAS}/", False),
    ("page.md", f"{USER_WIKI}/page", True),
    ("home.md", f"{USER_WIKI}/", True),
]


class TestArticleRendersInert:
    @pytest.mark.parametrize("file_name,url,admin_viewer", ARTICLE_ROUTES)
    def test_article_active_content_renders_inert_fresh_and_cached(
        self, repo_dir: Path, file_name: str, url: str, admin_viewer: bool
    ) -> None:
        (repo_dir / file_name).write_text(ACTIVE_CONTENT_ARTICLE)
        client = TestClient(_make_app(repo_dir, admin_viewer=admin_viewer))

        fresh = client.get(url)
        assert fresh.status_code == 200
        _assert_inert(_article_body(fresh.text))

        cached = client.get(url)
        assert cached.status_code == 200
        _assert_inert(_article_body(cached.text))

    def test_unsanitised_cached_row_is_sanitised_on_serve(self, repo_dir: Path) -> None:
        """A cache row holding raw HTML (rendered by a server that did not
        sanitise) is still served inert."""
        from code_indexer.server.wiki import routes as wiki_routes

        page = repo_dir / "page.md"
        page.write_text(f"# Page\n\n{BODY_START}\n\nplain\n\n{BODY_END}\n")
        client = TestClient(_make_app(repo_dir))
        assert client.get(f"/wiki/{ALIAS}/page").status_code == 200
        cache = wiki_routes._wiki_cache
        assert cache is not None
        raw_html = (
            f"<p>{BODY_START}</p><script>alert(1)</script>"
            f'<img src=x onerror="alert(1)"><p>{BODY_END}</p>'
        )
        cache.put_article(ALIAS, "page", raw_html, "Page", page)

        served = client.get(f"/wiki/{ALIAS}/page")

        assert served.status_code == 200
        _assert_inert(_article_body(served.text))


NORMAL_ARTICLE = f"""# Guide

{BODY_START}

## Section One

See [Other](other.md), [Jump](#section-one), [Ext](https://example.com/doc)
and [Mail](mailto:team@example.com).

```python
print("hi")
```

| A | B |
|---|---|
| 1 | 2 |

![Diagram](images/diagram.png)

<details><summary>More</summary>

Hidden text

</details>

3. three
4. four

~~gone~~ <kbd>Ctrl</kbd> H<sub>2</sub>O x<sup>2</sup>

{BODY_END}
"""


class TestNormalArticleMarkupKept:
    def test_normal_article_markup_survives(self, repo_dir: Path) -> None:
        (repo_dir / "guide.md").write_text(NORMAL_ARTICLE)
        client = TestClient(_make_app(repo_dir))

        resp = client.get(f"/wiki/{ALIAS}/guide")

        assert resp.status_code == 200
        body = _article_body(resp.text)
        for expected in (
            '<h2 id="section-one">Section One</h2>',
            f'<a href="/wiki/{ALIAS}/other.md">Other</a>',
            '<a href="#section-one">Jump</a>',
            '<a href="https://example.com/doc" target="_blank" rel="noopener noreferrer">Ext</a>',
            '<a href="mailto:team@example.com">Mail</a>',
            '<pre><code class="language-python">',
            "<th>A</th>",
            "<td>1</td>",
            f'<img src="/wiki/{ALIAS}/_assets/images/diagram.png" alt="Diagram">',
            "<details><summary>More</summary>",
            '<ol start="3">',
            "<s>gone</s>",
            "<kbd>Ctrl</kbd>",
            "<sub>2</sub>",
            "<sup>2</sup>",
        ):
            assert expected in body, f"missing {expected!r} in:\n{body}"


MARKUP_FILE_STEM = 'x<img src=y onerror=z>"q'
# Jinja ``urlencode`` keeps "/" and percent-encodes everything else unsafe.
ENCODED_MARKUP_STEM = "x%3Cimg%20src%3Dy%20onerror%3Dz%3E%22q"


class TestIndexPageEscapesFileNames:
    def test_golden_index_file_name_markup_renders_as_text(
        self, repo_dir: Path
    ) -> None:
        (repo_dir / f"{MARKUP_FILE_STEM}.md").write_text("# Article\n")
        client = TestClient(_make_app(repo_dir))

        resp = client.get(f"/wiki/{ALIAS}/")

        assert resp.status_code == 200
        assert "<img" not in resp.text.lower()
        assert "&lt;img src=y onerror=z&gt;" in resp.text.lower()
        assert f'href="/wiki/{ALIAS}/{ENCODED_MARKUP_STEM}"' in resp.text

    def test_user_index_file_name_markup_renders_as_text(self, repo_dir: Path) -> None:
        (repo_dir / f"{MARKUP_FILE_STEM}.md").write_text("# Article\n")
        client = TestClient(_make_app(repo_dir, admin_viewer=True))

        resp = client.get(f"{USER_WIKI}/")

        assert resp.status_code == 200
        assert "<img" not in resp.text.lower()
        assert "&lt;img src=y onerror=z&gt;" in resp.text.lower()
        assert f'href="{USER_WIKI}/{ENCODED_MARKUP_STEM}"' in resp.text


# The plain Markdown link gets target="_blank" from WikiService.rewrite_links.
TARGET_LINKS_ARTICLE = f"""# Links

{BODY_START}

<a href="https://example.com/a" target="_blank" rel="opener">ext</a>

<a href="other.md" target="_blank">int</a>

[md](https://example.com/b)

{BODY_END}
"""


class TestTargetLinksCarryNoopener:
    def test_target_links_get_noopener_noreferrer_fresh_and_cached(
        self, repo_dir: Path
    ) -> None:
        (repo_dir / "links.md").write_text(TARGET_LINKS_ARTICLE)
        client = TestClient(_make_app(repo_dir))

        for _view in ("fresh", "cached"):
            resp = client.get(f"/wiki/{ALIAS}/links")
            assert resp.status_code == 200
            anchors = re.findall(r"<a\b[^>]*>", _article_body(resp.text))
            assert len(anchors) == 3, anchors
            for tag in anchors:
                assert "target=" in tag, tag
                assert re.findall(r'\brel="([^"]*)"', tag) == ["noopener noreferrer"], (
                    tag
                )


@pytest.fixture
def sanitiser_calls(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    """Records the size of every HTML string the wiki sanitises, delegating
    to the real sanitiser."""
    from code_indexer.server.wiki import wiki_service

    real = wiki_service.sanitize_html
    calls: List[int] = []

    def recording(html: str, allowlist: HtmlAllowlist) -> str:
        calls.append(len(html))
        return real(html, allowlist)

    monkeypatch.setattr(wiki_service, "sanitize_html", recording)
    return calls


class TestSanitisedPageCache:
    @pytest.mark.parametrize("file_name,url,admin_viewer", ARTICLE_ROUTES)
    def test_cached_view_does_not_run_sanitiser(
        self,
        repo_dir: Path,
        sanitiser_calls: List[int],
        file_name: str,
        url: str,
        admin_viewer: bool,
    ) -> None:
        (repo_dir / file_name).write_text(ACTIVE_CONTENT_ARTICLE)
        client = TestClient(_make_app(repo_dir, admin_viewer=admin_viewer))

        fresh = client.get(url)
        cached = client.get(url)

        assert fresh.status_code == cached.status_code == 200
        assert len(sanitiser_calls) == 1
        assert _article_body(cached.text) == _article_body(fresh.text)
        _assert_inert(_article_body(cached.text))

    @pytest.mark.parametrize("file_name,url,admin_viewer", ARTICLE_ROUTES)
    def test_undecodable_article_is_not_found(
        self,
        repo_dir: Path,
        sanitiser_calls: List[int],
        file_name: str,
        url: str,
        admin_viewer: bool,
    ) -> None:
        (repo_dir / file_name).write_bytes(b"# Page\n\n\xff\xfe not utf-8\n")
        client = TestClient(_make_app(repo_dir, admin_viewer=admin_viewer))

        assert client.get(url).status_code == 404
        assert sanitiser_calls == []

    @pytest.mark.parametrize("user_wiki", [False, True])
    def test_invalidation_re_renders(
        self, repo_dir: Path, sanitiser_calls: List[int], user_wiki: bool
    ) -> None:
        from code_indexer.server.wiki import routes as wiki_routes

        (repo_dir / "page.md").write_text(ACTIVE_CONTENT_ARTICLE)
        client = TestClient(_make_app(repo_dir, admin_viewer=user_wiki))
        url = f"{USER_WIKI}/page" if user_wiki else f"/wiki/{ALIAS}/page"
        assert client.get(url).status_code == 200
        cache = wiki_routes._wiki_cache
        assert cache is not None

        if user_wiki:
            cache.invalidate_user_wiki(OWNER, ALIAS)
        else:
            cache.invalidate_repo(ALIAS)
        resp = client.get(url)

        assert resp.status_code == 200
        assert len(sanitiser_calls) == 2
        _assert_inert(_article_body(resp.text))

    def test_changed_source_re_renders(
        self, repo_dir: Path, sanitiser_calls: List[int]
    ) -> None:
        page = repo_dir / "page.md"
        page.write_text(f"# Page\n\n{BODY_START}\n\nfirst\n\n{BODY_END}\n")
        client = TestClient(_make_app(repo_dir))
        assert "first" in client.get(f"/wiki/{ALIAS}/page").text

        page.write_text(f"# Page\n\n{BODY_START}\n\nsecond version\n\n{BODY_END}\n")
        resp = client.get(f"/wiki/{ALIAS}/page")

        assert "second version" in _article_body(resp.text)
        assert len(sanitiser_calls) == 2

    def test_old_location_row_is_re_rendered_not_served(
        self, repo_dir: Path, sanitiser_calls: List[int]
    ) -> None:
        """A row only in the bare-key location (as cached by a server that
        did not sanitise) is never served; the page is rendered afresh."""
        page = repo_dir / "page.md"
        page.write_text(f"# Page\n\n{BODY_START}\n\nplain\n\n{BODY_END}\n")
        app = _make_app(repo_dir)
        old = WikiCache(app.state.golden_repo_manager.db_path)
        old.ensure_tables()
        raw_html = f"<p>{BODY_START}</p><script>alert(1)</script><p>{BODY_END}</p>"
        old.put_article(ALIAS, "page", raw_html, "Page", page)

        resp = TestClient(app).get(f"/wiki/{ALIAS}/page")

        assert resp.status_code == 200
        assert len(sanitiser_calls) == 1
        body = _article_body(resp.text)
        _assert_inert(body)
        assert "plain" in body

    def test_large_article_cached_view_skips_sanitiser(
        self, repo_dir: Path, sanitiser_calls: List[int]
    ) -> None:
        paragraphs = "\n\n".join(f"Paragraph {i} " + "text " * 40 for i in range(2200))
        (repo_dir / "big.md").write_text(
            f"# Big\n\n{BODY_START}\n\n{paragraphs}\n\n{BODY_END}\n"
        )
        client = TestClient(_make_app(repo_dir))

        assert client.get(f"/wiki/{ALIAS}/big").status_code == 200
        assert len(sanitiser_calls) == 1 and sanitiser_calls[0] >= 480_000
        assert client.get(f"/wiki/{ALIAS}/big").status_code == 200

        assert len(sanitiser_calls) == 1

    def test_page_keys_isolated_from_bare_keys(self, tmp_path: Path) -> None:
        source = tmp_path / "page.md"
        source.write_text("# Page\n")
        cache = WikiCache(str(tmp_path / "cache.db"))
        cache.ensure_tables()

        cache.put_article(ALIAS, "page", "<p>bare</p>", "Bare", source)
        assert cache.get_page(ALIAS, "page", source) is None

        cache.put_page(ALIAS, "page", "<p>final</p>", "Final", source)
        page = cache.get_page(ALIAS, "page", source)
        assert page is not None and page["html"] == "<p>final</p>"
        bare = cache.get_article(ALIAS, "page", source)
        assert bare is not None and bare["html"] == "<p>bare</p>"

        cache.invalidate_repo(ALIAS)
        assert cache.get_page(ALIAS, "page", source) is None


EXPECTED_ASSET_CSP = (
    "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
)
ASSET_BASES = [(f"/wiki/{ALIAS}/_assets", False), (f"{USER_WIKI}/_assets", True)]
SVG_WITH_SCRIPT = (
    '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script>'
    '<circle r="1"/></svg>'
)


class TestWikiAssetsNeverActive:
    """Repository assets are served as passive content only."""

    @pytest.mark.parametrize("base,admin_viewer", ASSET_BASES)
    def test_svg_asset_served_sandboxed(
        self, repo_dir: Path, base: str, admin_viewer: bool
    ) -> None:
        (repo_dir / "diagram.svg").write_text(SVG_WITH_SCRIPT)
        client = TestClient(_make_app(repo_dir, admin_viewer=admin_viewer))

        resp = client.get(f"{base}/diagram.svg")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert resp.headers.get_list("content-security-policy") == [EXPECTED_ASSET_CSP]
        assert resp.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize("base,admin_viewer", ASSET_BASES)
    @pytest.mark.parametrize("ext", [".js", ".mjs", ".html", ".htm", ".xhtml", ".xml"])
    def test_script_and_document_assets_refused(
        self, repo_dir: Path, base: str, admin_viewer: bool, ext: str
    ) -> None:
        (repo_dir / f"payload{ext}").write_text("alert(1)")
        client = TestClient(_make_app(repo_dir, admin_viewer=admin_viewer))

        assert client.get(f"{base}/payload{ext}").status_code == 404

    @pytest.mark.parametrize("base,admin_viewer", ASSET_BASES)
    def test_png_asset_still_served(
        self, repo_dir: Path, base: str, admin_viewer: bool
    ) -> None:
        (repo_dir / "images").mkdir()
        (repo_dir / "images" / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        client = TestClient(_make_app(repo_dir, admin_viewer=admin_viewer))

        resp = client.get(f"{base}/images/diagram.png")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == b"\x89PNG\r\n\x1a\n"
        assert resp.headers["content-security-policy"] == EXPECTED_ASSET_CSP

    @pytest.mark.parametrize("base,admin_viewer", ASSET_BASES)
    def test_asset_type_ignores_host_mime_table(
        self, repo_dir: Path, base: str, admin_viewer: bool
    ) -> None:
        import mimetypes

        (repo_dir / "DIAGRAM.SVG").write_text(SVG_WITH_SCRIPT)
        client = TestClient(_make_app(repo_dir, admin_viewer=admin_viewer))
        mimetypes.add_type("text/html", ".svg")
        try:
            resp = client.get(f"{base}/DIAGRAM.SVG")
        finally:
            mimetypes.add_type("image/svg+xml", ".svg")

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert resp.headers["content-security-policy"] == EXPECTED_ASSET_CSP

    def test_security_middleware_keeps_route_asset_policy(self, repo_dir: Path) -> None:
        from code_indexer.server.middleware.security_headers import (
            SecurityHeadersMiddleware,
        )

        (repo_dir / "diagram.svg").write_text(SVG_WITH_SCRIPT)
        app = _make_app(repo_dir)
        app.add_middleware(SecurityHeadersMiddleware)

        resp = TestClient(app).get(f"/wiki/{ALIAS}/_assets/diagram.svg")

        assert resp.headers.get_list("content-security-policy") == [EXPECTED_ASSET_CSP]
        assert resp.headers.get_list("x-content-type-options") == ["nosniff"]
