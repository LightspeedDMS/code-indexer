"""E2E tests for parser robustness - Story #66 AC8."""

import pytest

from src.code_indexer.indexing.image_extractor import (
    MarkdownImageExtractor,
    HtmlImageExtractor,
)


@pytest.mark.e2e
class TestStrictParser:
    """Test that parsers handle malformed syntax gracefully."""

    def test_markdown_parser_handles_malformed_syntax(self, multimodal_repo_path):
        """Verify markdown parser doesn't crash on malformed image syntax."""
        # Test various malformed markdown image syntaxes
        malformed_content = """
# Test Document

Missing closing bracket:
![Alt text(image.png)

Missing opening bracket:
!Alt text](image.png)

Empty alt text:
![](image.png)

No path:
![Alt text]()

Multiple images with some malformed:
![Valid](valid.png)
![Malformed(incomplete.png)
![Another Valid](another.png)
"""

        extractor = MarkdownImageExtractor()
        test_file = multimodal_repo_path / "docs" / "database-guide.md"

        # Should not crash, even with malformed syntax
        images = extractor.extract_images(
            malformed_content, test_file, multimodal_repo_path
        )

        # Should extract the valid images
        assert isinstance(images, list), "Should return a list"

    def test_html_parser_handles_malformed_tags(self, multimodal_repo_path):
        """Verify HTML parser doesn't crash on malformed img tags."""
        # Test various malformed HTML img tags
        malformed_content = """
<html>
<body>
    <!-- Missing closing angle bracket -->
    <img src="incomplete.png"
    
    <!-- Missing src attribute -->
    <img alt="No source">
    
    <!-- Empty src -->
    <img src="">
    
    <!-- Valid tag -->
    <img src="valid.png">
    
    <!-- Malformed nested tags -->
    <img src="<broken.png>">
</body>
</html>
"""

        extractor = HtmlImageExtractor()
        test_file = multimodal_repo_path / "docs" / "configuration.html"

        # Should not crash, even with malformed syntax
        images = extractor.extract_images(
            malformed_content, test_file, multimodal_repo_path
        )

        # Should return a list (possibly empty)
        assert isinstance(images, list), "Should return a list"

    def test_edge_case_files_dont_crash_parsers(self, multimodal_repo_path):
        """Verify all edge case files can be parsed without crashes."""
        edge_cases_dir = multimodal_repo_path / "docs" / "edge-cases"

        markdown_extractor = MarkdownImageExtractor()
        html_extractor = HtmlImageExtractor()

        for edge_file in edge_cases_dir.glob("*"):
            if not edge_file.is_file():
                continue

            content = edge_file.read_text()

            # Try appropriate extractor based on file extension
            if edge_file.suffix in [".md", ".markdown"]:
                images = markdown_extractor.extract_images(
                    content, edge_file, multimodal_repo_path
                )
                assert isinstance(
                    images, list
                ), f"Markdown parser crashed on {edge_file.name}"

            elif edge_file.suffix in [".html", ".htmx", ".htm"]:
                images = html_extractor.extract_images(
                    content, edge_file, multimodal_repo_path
                )
                assert isinstance(
                    images, list
                ), f"HTML parser crashed on {edge_file.name}"

    def test_text_that_looks_like_image_syntax(self, multimodal_repo_path):
        """Verify parser handles text that looks like image syntax in code blocks."""
        md_file = (
            multimodal_repo_path / "docs" / "edge-cases" / "text-looks-like-image.md"
        )

        if not md_file.exists():
            pytest.skip("text-looks-like-image.md not found in test fixtures")

        content = md_file.read_text()
        extractor = MarkdownImageExtractor()

        # Should not crash on code blocks containing image-like syntax
        images = extractor.extract_images(content, md_file, multimodal_repo_path)
        assert isinstance(images, list), "Should return a list"
