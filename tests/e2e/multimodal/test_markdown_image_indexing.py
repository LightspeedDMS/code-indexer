"""E2E tests for Markdown image extraction - Story #66 AC2."""

import pytest

from src.code_indexer.indexing.image_extractor import (
    MarkdownImageExtractor,
    ImageExtractorFactory,
)


@pytest.mark.e2e
class TestMarkdownImageIndexing:
    """Test markdown image extraction from real test fixtures."""

    def test_extract_images_from_database_guide(self, multimodal_repo_path):
        """Verify database-guide.md has correct image extraction."""
        md_file = multimodal_repo_path / "docs" / "database-guide.md"
        assert md_file.exists(), f"Test fixture missing: {md_file}"

        extractor = MarkdownImageExtractor()
        content = md_file.read_text()

        images = extractor.extract_images(content, md_file, multimodal_repo_path)

        # database-guide.md has 1 image: ![Database Schema](../images/database-schema.png)
        assert len(images) == 1, f"Expected 1 image, got {len(images)}"
        assert "images/database-schema.png" in images[0]

    def test_factory_selects_markdown_extractor_for_md(self):
        """Verify ImageExtractorFactory returns MarkdownImageExtractor for .md files."""
        extractor = ImageExtractorFactory.get_extractor(".md")

        assert extractor is not None, "Factory returned None for .md extension"
        assert isinstance(
            extractor, MarkdownImageExtractor
        ), f"Expected MarkdownImageExtractor, got {type(extractor)}"

    def test_extract_images_from_api_reference(self, multimodal_repo_path):
        """Verify api-reference.md image extraction."""
        md_file = multimodal_repo_path / "docs" / "api-reference.md"
        assert md_file.exists(), f"Test fixture missing: {md_file}"

        extractor = MarkdownImageExtractor()
        content = md_file.read_text()

        images = extractor.extract_images(content, md_file, multimodal_repo_path)

        # api-reference.md has 1 image: ![API Flow Diagram](../images/api-flow.jpg)
        assert len(images) == 1, f"Expected 1 image, got {len(images)}"
        assert "images/api-flow.jpg" in images[0]
