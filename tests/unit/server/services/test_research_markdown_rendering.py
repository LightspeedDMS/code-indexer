"""
Unit tests for Research Assistant markdown rendering.

Story #XXX: Markdown rendering for Claude responses in Research Assistant.

Tests markdown-to-HTML conversion with:
- Code blocks with syntax highlighting
- Headers (h1-h6)
- Lists (ordered and unordered)
- Links
- Bold, italic, inline code
- XSS prevention
"""


from code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
)


class TestMarkdownRendering:
    """Test markdown-to-HTML conversion functionality."""

    def test_code_block_basic(self):
        """AC1: Code blocks render with proper formatting."""
        service = ResearchAssistantService()

        markdown_text = """
Here's some code:

```python
def hello():
    return "world"
```
"""
        html = service.render_markdown(markdown_text)

        # Should contain pre and code tags
        assert "<pre>" in html
        assert "<code>" in html
        # Content should be present (may be wrapped in syntax highlighting spans)
        assert "hello" in html
        assert "world" in html

    def test_code_block_with_language(self):
        """AC1: Code blocks with language specified render with class."""
        service = ResearchAssistantService()

        markdown_text = """
```python
print("hello")
```
"""
        html = service.render_markdown(markdown_text)

        # Should have codehilite class for syntax highlighting
        assert "codehilite" in html
        assert "hello" in html

    def test_inline_code(self):
        """AC1: Inline code renders with code tag."""
        service = ResearchAssistantService()

        markdown_text = "Use the `print()` function to output text."
        html = service.render_markdown(markdown_text)

        assert "<code>" in html
        assert "print()" in html
        assert "</code>" in html

    def test_headers_render(self):
        """AC2: Headers (h1-h6) render with appropriate sizing."""
        service = ResearchAssistantService()

        markdown_text = """
# Header 1
## Header 2
### Header 3
#### Header 4
##### Header 5
###### Header 6
"""
        html = service.render_markdown(markdown_text)

        assert "<h1>" in html
        assert "<h2>" in html
        assert "<h3>" in html
        assert "<h4>" in html
        assert "<h5>" in html
        assert "<h6>" in html

    def test_unordered_list(self):
        """AC3: Unordered lists render correctly."""
        service = ResearchAssistantService()

        markdown_text = """
- Item 1
- Item 2
- Item 3
"""
        html = service.render_markdown(markdown_text)

        assert "<ul>" in html
        assert "<li>" in html
        assert "Item 1" in html
        assert "Item 2" in html
        assert "Item 3" in html

    def test_ordered_list(self):
        """AC3: Ordered lists render correctly."""
        service = ResearchAssistantService()

        markdown_text = """
1. First
2. Second
3. Third
"""
        html = service.render_markdown(markdown_text)

        assert "<ol>" in html
        assert "<li>" in html
        assert "First" in html
        assert "Second" in html
        assert "Third" in html

    def test_links_render(self):
        """AC4: Links are clickable."""
        service = ResearchAssistantService()

        markdown_text = "Check out [Google](https://google.com) for more info."
        html = service.render_markdown(markdown_text)

        assert '<a href="https://google.com"' in html
        assert "Google" in html
        assert "</a>" in html

    def test_bold_text(self):
        """Bold text renders with strong tag."""
        service = ResearchAssistantService()

        markdown_text = "This is **bold text**."
        html = service.render_markdown(markdown_text)

        assert "<strong>" in html
        assert "bold text" in html
        assert "</strong>" in html

    def test_italic_text(self):
        """Italic text renders with em tag."""
        service = ResearchAssistantService()

        markdown_text = "This is *italic text*."
        html = service.render_markdown(markdown_text)

        assert "<em>" in html
        assert "italic text" in html
        assert "</em>" in html

    def test_xss_script_tag_stripped(self):
        """AC5: Script tags are sanitized to prevent XSS."""
        service = ResearchAssistantService()

        markdown_text = '<script>alert("XSS")</script>'
        html = service.render_markdown(markdown_text)

        # Script tag should be stripped
        assert "<script>" not in html
        assert 'alert("XSS")' not in html or "&lt;script&gt;" in html

    def test_xss_onclick_stripped(self):
        """AC5: onclick and other event handlers are stripped."""
        service = ResearchAssistantService()

        markdown_text = '<a href="#" onclick="alert(\'XSS\')">Click me</a>'
        html = service.render_markdown(markdown_text)

        # onclick should be stripped
        assert "onclick" not in html

    def test_xss_iframe_stripped(self):
        """AC5: iframe tags are stripped."""
        service = ResearchAssistantService()

        markdown_text = '<iframe src="http://evil.com"></iframe>'
        html = service.render_markdown(markdown_text)

        # iframe should be stripped
        assert "<iframe" not in html

    def test_empty_input(self):
        """Empty markdown returns empty HTML."""
        service = ResearchAssistantService()

        html = service.render_markdown("")
        assert html == ""

    def test_plain_text(self):
        """Plain text without markdown renders in paragraph tag."""
        service = ResearchAssistantService()

        markdown_text = "Just plain text."
        html = service.render_markdown(markdown_text)

        assert "<p>" in html
        assert "Just plain text." in html
        assert "</p>" in html

    def test_complex_document(self):
        """Complex document with multiple markdown elements."""
        service = ResearchAssistantService()

        markdown_text = """
# Analysis Report

## Summary

The code has **3 issues**:

1. Missing error handling
2. *Inefficient* algorithm
3. No tests

### Code Example

```python
def buggy_function():
    return 1 / 0  # ZeroDivisionError!
```

For more info, see [documentation](https://example.com).
"""
        html = service.render_markdown(markdown_text)

        # Should contain all elements
        assert "<h1>" in html
        assert "<h2>" in html
        assert "<h3>" in html
        assert "<strong>" in html
        assert "<em>" in html
        assert "<ol>" in html
        assert "<li>" in html
        assert "<pre>" in html
        assert "<code>" in html
        assert "<a href=" in html
        assert "Analysis Report" in html
        assert "buggy_function" in html
        assert "ZeroDivisionError" in html

    def test_newlines_preserved_in_code(self):
        """Newlines in code blocks should be preserved."""
        service = ResearchAssistantService()

        markdown_text = """
```python
line1
line2
line3
```
"""
        html = service.render_markdown(markdown_text)

        # Should contain all lines
        assert "line1" in html
        assert "line2" in html
        assert "line3" in html

    def test_special_characters_escaped(self):
        """Special HTML characters should be escaped in text."""
        service = ResearchAssistantService()

        markdown_text = "Use < and > symbols carefully."
        html = service.render_markdown(markdown_text)

        # Should escape < and >
        assert "&lt;" in html or "<" not in html.replace("<p>", "").replace("</p>", "")
        assert "&gt;" in html or ">" not in html.replace("<p>", "").replace("</p>", "")
