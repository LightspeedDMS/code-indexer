"""Unit tests for the shared allowlist HTML sanitiser and its presets."""

import bleach  # type: ignore[import-untyped]
import pytest

from code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
)
from code_indexer.server.utils.html_sanitizer import (
    RESEARCH_ASSISTANT_ALLOWLIST,
    WIKI_ARTICLE_ALLOWLIST,
    sanitize_html,
)

# The research assistant's allowlist as it stood before the shared helper;
# its rendered output must not change.
_PREVIOUS_RESEARCH_TAGS = [
    "p", "br", "strong", "em", "code", "pre", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "a",
    "table", "thead", "tbody", "tr", "th", "td", "div", "span", "hr",
]  # fmt: skip
_PREVIOUS_RESEARCH_ATTRS = {
    "a": ["href", "title"],
    "code": ["class"],
    "div": ["class"],
    "span": ["class"],
    "pre": ["class"],
}

RESEARCH_CORPUS = [
    "<p>plain <strong>bold</strong> <em>it</em></p>",
    '<a href="https://example.com/x" title="t" onclick="alert(1)">l</a>',
    '<a href="javascript:alert(1)">l</a>',
    "<script>alert(1)</script><p>after</p>",
    '<img src="x" onerror="alert(1)">',
    '<iframe src="https://example.com/f"></iframe>',
    '<div class="codehilite"><pre><span class="k">def</span></pre></div>',
    "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>",
    '<h2 id="x">Head</h2><hr><blockquote>q</blockquote>',
    '<p style="color:red">styled</p><!-- comment -->',
    '<a href="mailto:team@example.com">m</a> <a href="data:text/html,x">d</a>',
]


class TestResearchAssistantPreset:
    @pytest.mark.parametrize("html", RESEARCH_CORPUS)
    def test_research_preset_matches_previous_inline_allowlist(self, html: str) -> None:
        previous = bleach.clean(
            html,
            tags=_PREVIOUS_RESEARCH_TAGS,
            attributes=_PREVIOUS_RESEARCH_ATTRS,
            protocols=["http", "https", "mailto"],
        )
        assert sanitize_html(html, RESEARCH_ASSISTANT_ALLOWLIST) == previous

    def test_research_render_markdown_escapes_disallowed_elements(self) -> None:
        rendered = ResearchAssistantService().render_markdown(
            "Text <script>alert(1)</script> and <iframe></iframe>"
        )
        assert "<script" not in rendered and "<iframe" not in rendered
        assert "&lt;script&gt;" in rendered


class TestWikiArticlePreset:
    @pytest.mark.parametrize(
        "html,forbidden",
        [
            ("<script>alert(1)</script>", "<script"),
            ("<style>p{}</style>", "<style"),
            ('<img src="x" onerror="alert(1)">', "onerror"),
            ('<a href="javascript:alert(1)">l</a>', "javascript:"),
            ('<a href=" JaVaScRiPt:alert(1)">l</a>', "javascript:"),
            ('<a href="vbscript:x">l</a>', "vbscript:"),
            ('<img src="data:image/png;base64,AAAA">', "data:"),
            ('<a href="data:text/html,x">l</a>', "data:"),
            ('<p style="color:red">t</p>', "style="),
            ('<svg onload="alert(1)"><circle/></svg>', "<svg"),
            ("<object data='x'></object><embed src='x'>", "<object"),
            ('<base href="https://example.com/">', "<base"),
            ('<meta http-equiv="refresh" content="0">', "<meta"),
            ('<link rel="stylesheet" href="https://example.com/s.css">', "<link"),
            ("<form><input name='q'></form>", "<input"),
            ("<!-- hidden -->", "<!--"),
            ('<img src="a.png" width="9" onload="x">', "width="),
        ],
    )
    def test_wiki_preset_removes_active_content(
        self, html: str, forbidden: str
    ) -> None:
        assert forbidden not in sanitize_html(html, WIKI_ARTICLE_ALLOWLIST).lower()

    def test_wiki_preset_strips_unknown_elements_keeping_text(self) -> None:
        assert sanitize_html("<marquee>hello</marquee>", WIKI_ARTICLE_ALLOWLIST) == (
            "hello"
        )

    @pytest.mark.parametrize(
        "html",
        [
            '<h3 id="setup">Setup</h3>',
            '<a href="https://example.com/" target="_blank" rel="noopener noreferrer">e</a>',
            '<a href="/wiki/example-repo/page#x" title="t">p</a>',
            '<a href="mailto:team@example.com">m</a>',
            '<img src="/wiki/example-repo/_assets/a.png" alt="A" title="T">',
            '<pre><code class="language-python">x = 1</code></pre>',
            '<ol start="3"><li>three</li></ol>',
            "<details open><summary>S</summary>b</details>",
            "<p><del>a</del> <s>b</s> <kbd>k</kbd> <sup>1</sup> <sub>2</sub></p>",
            "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>",
        ],
    )
    def test_wiki_preset_keeps_markdown_output(self, html: str) -> None:
        assert sanitize_html(html, WIKI_ARTICLE_ALLOWLIST) == html

    @pytest.mark.parametrize(
        "html,expected",
        [
            (
                '<a href="https://example.com/" target="_blank" rel="opener">e</a>',
                '<a href="https://example.com/" target="_blank" rel="noopener noreferrer">e</a>',
            ),
            (
                '<a href="page" target="frame">p</a>',
                '<a href="page" target="frame" rel="noopener noreferrer">p</a>',
            ),
            ('<a href="page" rel="opener">p</a>', '<a href="page" rel="opener">p</a>'),
        ],
    )
    def test_wiki_preset_isolates_target_links(self, html: str, expected: str) -> None:
        assert sanitize_html(html, WIKI_ARTICLE_ALLOWLIST) == expected

    def test_research_preset_does_not_add_rel(self) -> None:
        html = '<a href="https://example.com/">e</a>'
        assert sanitize_html(html, RESEARCH_ASSISTANT_ALLOWLIST) == html
