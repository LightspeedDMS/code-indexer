"""Tests for WikiService (Stories #281, #282)."""

import tempfile
from pathlib import Path

import pytest

from code_indexer.server.wiki.wiki_service import WikiService


@pytest.fixture
def svc():
    return WikiService()


class TestWikiServiceFrontMatter:
    def test_strips_yaml_front_matter(self, svc):
        content = "---\ntitle: My Title\n---\n# Body"
        metadata, body = svc._strip_front_matter(content)
        assert metadata.get("title") == "My Title"
        assert "# Body" in body

    def test_returns_empty_metadata_when_no_front_matter(self, svc):
        content = "# Just content\nNo front matter here"
        metadata, body = svc._strip_front_matter(content)
        assert metadata == {}
        assert "# Just content" in body

    def test_front_matter_category_field(self, svc):
        content = "---\ncategory: Guides\ntitle: Test\n---\nBody"
        metadata, _ = svc._strip_front_matter(content)
        assert metadata.get("category") == "Guides"

    def test_handles_malformed_front_matter(self, svc):
        content = "---\ntitle: [unclosed bracket\n---\n# Body"
        metadata, body = svc._strip_front_matter(content)
        assert metadata == {}
        assert content == body


class TestWikiServiceHeaderBlock:
    def test_strips_header_block_with_separator(self, svc):
        content = "Article Number: 001\nTitle: My Article\nPublication Status: Draft\nSummary: Test\n---\n# Real Content"
        result = svc._strip_header_block(content)
        assert "# Real Content" in result
        assert "Article Number" not in result

    def test_no_header_block_unchanged(self, svc):
        content = "# Normal Content\nJust text"
        result = svc._strip_header_block(content)
        assert "# Normal Content" in result
        assert "Just text" in result

    def test_strips_leading_blank_lines_before_header(self, svc):
        content = "\n\nTitle: Test\nSummary: brief\n---\n# Body"
        result = svc._strip_header_block(content)
        assert "# Body" in result
        assert "Title: Test" not in result


class TestWikiServiceMarkdownRendering:
    def test_renders_heading(self, svc):
        html = svc._render_markdown("# Hello World")
        assert "<h1" in html
        assert "Hello World" in html

    def test_renders_paragraph(self, svc):
        html = svc._render_markdown("Just a paragraph")
        assert "<p>" in html
        assert "Just a paragraph" in html

    def test_renders_table(self, svc):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        html = svc._render_markdown(md)
        assert "<table" in html

    def test_renders_strikethrough(self, svc):
        html = svc._render_markdown("~~deleted~~")
        assert "<del>" in html or "<s>" in html

    def test_renders_code_block(self, svc):
        html = svc._render_markdown("```python\nprint('hi')\n```")
        assert "<code" in html


class TestWikiServiceTitleExtraction:
    def test_title_from_metadata(self, svc):
        title = svc._extract_title({"title": "My Article"}, Path("some/file.md"))
        assert title == "My Article"

    def test_title_from_filename_when_no_metadata(self, svc):
        title = svc._extract_title({}, Path("my-article-name.md"))
        assert title == "My Article Name"

    def test_title_replaces_underscores(self, svc):
        title = svc._extract_title({}, Path("hello_world.md"))
        assert title == "Hello World"

    def test_title_empty_metadata_key(self, svc):
        title = svc._extract_title({"title": ""}, Path("fallback-name.md"))
        assert title == "Fallback Name"


class TestWikiServiceImageRewriting:
    def test_rewrites_relative_image_src(self, svc):
        html = '<img src="images/photo.png" alt="test">'
        result = svc._rewrite_image_paths(html, "my-repo")
        assert "/wiki/my-repo/_assets/images/photo.png" in result

    def test_leaves_absolute_http_urls_unchanged(self, svc):
        html = '<img src="https://example.com/img.png">'
        result = svc._rewrite_image_paths(html, "my-repo")
        assert "https://example.com/img.png" in result
        assert "_assets" not in result

    def test_leaves_wiki_paths_unchanged(self, svc):
        html = '<img src="/wiki/other-repo/_assets/img.png">'
        result = svc._rewrite_image_paths(html, "my-repo")
        assert "/wiki/other-repo/_assets/img.png" in result

    def test_strips_relative_parent_traversal(self, svc):
        html = '<img src="../images/photo.png">'
        result = svc._rewrite_image_paths(html, "my-repo")
        assert "../" not in result
        assert "_assets/images/photo.png" in result

    def test_rewrites_double_quoted_src(self, svc):
        html = '<img src="logo.svg">'
        result = svc._rewrite_image_paths(html, "repo")
        assert "/wiki/repo/_assets/logo.svg" in result

    def test_rewrites_single_quoted_src(self, svc):
        html = "<img src='logo.svg'>"
        result = svc._rewrite_image_paths(html, "repo")
        assert "/wiki/repo/_assets/logo.svg" in result


class TestWikiServiceHeadingIds:
    def test_adds_id_to_h1(self, svc):
        html = "<h1>Hello World</h1>"
        result = svc._add_heading_ids(html)
        assert 'id="hello-world"' in result

    def test_adds_id_to_h2(self, svc):
        html = "<h2>Section Title</h2>"
        result = svc._add_heading_ids(html)
        assert 'id="section-title"' in result

    def test_strips_special_chars_from_id(self, svc):
        html = "<h2>Hello, World! (2024)</h2>"
        result = svc._add_heading_ids(html)
        assert 'id="hello-world-2024"' in result

    def test_collapses_spaces_to_dashes(self, svc):
        html = "<h3>Multiple   Words   Here</h3>"
        result = svc._add_heading_ids(html)
        assert 'id="multiple-words-here"' in result


class TestWikiServiceRenderArticle:
    def test_render_article_returns_html_and_title(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.md"
            f.write_text("# My Title\nSome content")
            result = svc.render_article(f, "test-repo")
            assert "html" in result
            assert "title" in result
            assert "Some content" in result["html"]

    def test_render_article_rewrites_images(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.md"
            f.write_text("![img](images/photo.png)")
            result = svc.render_article(f, "my-repo")
            assert "/wiki/my-repo/_assets/images/photo.png" in result["html"]

    def test_render_article_with_front_matter(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.md"
            f.write_text("---\ntitle: Custom Title\n---\n# Body content")
            result = svc.render_article(f, "test-repo")
            assert result["title"] == "Custom Title"
            assert "Body content" in result["html"]

    def test_render_article_strips_header_block(self, svc):
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "test.md"
            f.write_text("Title: Doc Title\nSummary: Brief\n---\n# Actual Content")
            result = svc.render_article(f, "test-repo")
            assert "Actual Content" in result["html"]
            assert "Doc Title" not in result["html"]
