"""Wiki pages run no inline script and are served under the strict wiki CSP."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.middleware.security_headers import (
    WIKI_CSP,
    SecurityHeadersMiddleware,
)
from tests.unit.server.wiki.test_wiki_root_template import _make_app

_SCRIPT_TAG = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)
_THEME_SCRIPT_SRC = "/wiki/_static/wiki-theme.js"
_WIKI_STATIC_DIR = (
    Path(__file__).resolve().parents[4] / "src/code_indexer/server/wiki/static"
)


@pytest.fixture
def wiki_page(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "page.md").write_text("# Page\nBody")
    app = _make_app(str(repo), {"test-repo"})
    app.add_middleware(SecurityHeadersMiddleware)
    resp = TestClient(app).get("/wiki/test-repo/")
    assert resp.status_code == 200
    return resp


def test_wiki_page_has_no_inline_script_and_strict_csp(wiki_page):
    assert wiki_page.headers.get_list("content-security-policy") == [WIKI_CSP]
    tags = _SCRIPT_TAG.findall(wiki_page.text)
    assert tags, "wiki page should load its scripts"
    for attrs, body in tags:
        assert "src=" in attrs, f"inline script found: {body[:80]!r}"
        assert body.strip() == "", f"script tag with src also has a body: {body!r}"


def test_theme_script_runs_synchronously_in_head(wiki_page):
    head = wiki_page.text.split("</head>", 1)[0]
    theme_tags = [
        attrs for attrs, _ in _SCRIPT_TAG.findall(head) if _THEME_SCRIPT_SRC in attrs
    ]
    assert len(theme_tags) == 1, head
    assert "defer" not in theme_tags[0] and "async" not in theme_tags[0]


def test_theme_script_file_applies_saved_theme():
    source = (_WIKI_STATIC_DIR / "wiki-theme.js").read_text()
    assert "localStorage.getItem('wiki_theme')" in source
    assert "setAttribute('data-theme'" in source
