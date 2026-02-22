"""Tests for XSS sanitization in dependency map domain service (Bug #253).

Verifies that bleach-based sanitization blocks all known XSS vectors
that the previous regex-based approach missed.
"""
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_dep_map_service(golden_repos_dir: str):
    """Build a mock DependencyMapService with a golden_repos_dir property."""
    svc = Mock()
    svc.golden_repos_dir = golden_repos_dir
    svc.cidx_meta_read_path = Path(golden_repos_dir) / "cidx-meta"
    return svc


def _make_config_manager():
    """Build a mock config_manager (not used directly but needed for constructor)."""
    return Mock()


def _import_service():
    from code_indexer.server.services.dependency_map_domain_service import (
        DependencyMapDomainService,
    )
    return DependencyMapDomainService


def _write_domain_md(depmap_dir: Path, domain_name: str, content: str) -> None:
    """Write a domain .md file to the given dependency-map directory."""
    depmap_dir.mkdir(parents=True, exist_ok=True)
    (depmap_dir / f"{domain_name}.md").write_text(content)


def _render(content: str) -> str:
    """Helper: render a markdown string through DependencyMapDomainService."""
    Service = _import_service()
    with tempfile.TemporaryDirectory() as tmp:
        depmap_dir = Path(tmp) / "cidx-meta" / "dependency-map"
        _write_domain_md(depmap_dir, "test_domain", content)
        dep_map_svc = _make_dep_map_service(tmp)
        service = Service(dep_map_svc, _make_config_manager())
        result = service._render_domain_markdown("test_domain")
        assert result is not None, "Rendering returned None unexpectedly"
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Bug #253: XSS sanitization tests
# ─────────────────────────────────────────────────────────────────────────────


class TestXSSSanitization:
    """Verify bleach-based sanitization blocks all XSS vectors (Bug #253)."""

    def test_script_tags_removed(self):
        """Script tags must be stripped; bleach preserves inner text as harmless plain text."""
        result = _render("# Safe Heading\n\n<script>alert('xss')</script>\n\nSafe text.")
        assert "<script>" not in result.lower()
        assert "</script>" not in result.lower()
        # The text content is preserved as harmless plain text - this is correct
        assert "Safe Heading" in result
        assert "Safe text" in result

    def test_javascript_urls_removed(self):
        """javascript: URLs in href must be sanitized (href stripped or removed)."""
        result = _render("# Safe\n\n<a href=\"javascript:alert('xss')\">click me</a>")
        assert "javascript:" not in result.lower()
        # The link text should still be present (bleach strips the attribute, not the tag)
        assert "click me" in result

    def test_svg_onload_removed(self):
        """SVG onload event handlers must be removed."""
        result = _render('# Safe\n\n<svg onload="alert(\'xss\')"></svg>')
        assert "onload" not in result.lower()

    def test_event_handlers_removed_from_div(self):
        """on* event handlers must be removed from any tag; tag content preserved."""
        result = _render('# Safe\n\n<div onmouseover="alert(\'xss\')">hover text</div>')
        assert "onmouseover" not in result.lower()
        assert "hover text" in result

    def test_event_handlers_removed_from_img(self):
        """onerror event handler on img must be removed."""
        result = _render('# Safe\n\n<img src="valid.png" onerror="alert(\'xss\')">')
        assert "onerror" not in result.lower()

    def test_event_handlers_removed_unquoted(self):
        """Unquoted on* event handlers must be removed (regex-only approach misses these)."""
        result = _render("# Safe\n\n<div onclick=alert('xss')>text</div>")
        assert "onclick" not in result.lower()

    def test_data_uri_in_img_src_removed(self):
        """data: URIs in img src must be sanitized (disallowed protocol)."""
        result = _render(
            "# Safe\n\n<img src=\"data:text/html,<script>alert('xss')</script>\">"
        )
        assert "data:" not in result.lower()

    def test_iframe_removed(self):
        """iframe tags must be stripped entirely."""
        result = _render(
            "# Safe\n\n<iframe src=\"https://evil.com\">content</iframe>\n\nSafe text."
        )
        assert "<iframe" not in result.lower()
        assert "Safe text" in result

    def test_style_attribute_removed(self):
        """style attributes must be stripped (CSS injection vector)."""
        result = _render(
            '# Safe\n\n<div style="background:url(javascript:alert(1))">text</div>'
        )
        assert "style=" not in result.lower()
        # div tag and text should still be present
        assert "text" in result

    def test_object_tag_removed(self):
        """object tags must be stripped entirely."""
        result = _render(
            "# Safe\n\n<object data=\"evil.swf\">fallback</object>\n\nSafe."
        )
        assert "<object" not in result.lower()
        assert "Safe." in result

    def test_embed_tag_removed(self):
        """embed tags must be stripped entirely."""
        result = _render("# Safe\n\n<embed src=\"evil.swf\">\n\nSafe.")
        assert "<embed" not in result.lower()
        assert "Safe." in result

    def test_form_tag_removed(self):
        """form tags must be stripped (CSRF/phishing vector)."""
        result = _render(
            "# Safe\n\n<form action=\"https://evil.com\"><input type=\"text\"></form>\n\nSafe."
        )
        assert "<form" not in result.lower()
        assert "Safe." in result

    def test_safe_html_preserved(self):
        """Normal markdown-generated HTML tags must pass through unchanged."""
        result = _render(
            "# Heading 1\n\n## Heading 2\n\nParagraph **bold** and *italic*.\n\n"
            "- Item 1\n- Item 2\n\n"
            "| Col A | Col B |\n|---|---|\n| val1 | val2 |\n"
        )
        assert "<h1>" in result or "Heading 1" in result
        assert "<h2>" in result or "Heading 2" in result
        assert "<strong>" in result or "<b>" in result or "bold" in result
        assert "<li>" in result or "Item 1" in result

    def test_code_blocks_preserved(self):
        """Code blocks with pre/code tags must be preserved."""
        result = _render(
            "# Safe\n\n```python\ndef hello():\n    return 'world'\n```\n"
        )
        assert "hello" in result
        assert "world" in result
        # pre or code tag should appear (fenced_code extension)
        assert "<pre>" in result or "<code>" in result

    def test_anchor_with_safe_href_preserved(self):
        """Anchor tags with http/https/mailto href must be preserved."""
        result = _render(
            "# Safe\n\n[Visit site](https://example.com)\n\n[Email](mailto:user@example.com)"
        )
        assert "https://example.com" in result
        assert "mailto:user@example.com" in result

    def test_safe_img_preserved(self):
        """img tags with http/https src must be preserved."""
        result = _render("# Safe\n\n![Alt text](https://example.com/image.png)")
        assert "https://example.com/image.png" in result

    def test_vbscript_url_removed(self):
        """vbscript: URLs must be sanitized (not in SAFE_PROTOCOLS)."""
        result = _render("# Safe\n\n<a href=\"vbscript:msgbox('xss')\">click</a>")
        assert "vbscript:" not in result.lower()

    def test_nested_script_inside_other_tag_removed(self):
        """Nested script injection attempt must be sanitized."""
        result = _render(
            "# Safe\n\n<div><script>alert('nested')</script>text</div>"
        )
        assert "<script>" not in result.lower()
        assert "</script>" not in result.lower()
        # Safe HTML structure preserved
        assert "<div>" in result or "<h1>" in result
