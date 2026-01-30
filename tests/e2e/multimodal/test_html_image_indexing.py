"""E2E tests for HTML image extraction - Story #66 AC3."""

import pytest

from src.code_indexer.indexing.image_extractor import (
    HtmlImageExtractor,
    ImageExtractorFactory,
)


@pytest.mark.e2e
class TestHtmlImageIndexing:
    """Test HTML image extraction from real test fixtures."""

    def test_extract_images_from_configuration_html(self, multimodal_repo_path):
        """Verify configuration.html has correct image extraction."""
        html_file = multimodal_repo_path / "docs" / "configuration.html"
        assert html_file.exists(), f"Test fixture missing: {html_file}"
        
        extractor = HtmlImageExtractor()
        content = html_file.read_text()
        
        images = extractor.extract_images(content, html_file, multimodal_repo_path)
        
        # configuration.html has 1 image: <img src="../images/config-options.webp" alt="Configuration Options">
        assert len(images) == 1, f"Expected 1 image, got {len(images)}"
        assert "images/config-options.webp" in images[0]
        
    def test_factory_selects_html_extractor_for_html(self):
        """Verify ImageExtractorFactory returns HtmlImageExtractor for .html files."""
        extractor = ImageExtractorFactory.get_extractor(".html")
        
        assert extractor is not None, "Factory returned None for .html extension"
        assert isinstance(extractor, HtmlImageExtractor), (
            f"Expected HtmlImageExtractor, got {type(extractor)}"
        )
        
    def test_html_extractor_handles_img_src_attribute(self, multimodal_repo_path):
        """Verify HTML extractor correctly parses img src attributes."""
        html_file = multimodal_repo_path / "docs" / "configuration.html"
        content = html_file.read_text()
        
        extractor = HtmlImageExtractor()
        images = extractor.extract_images(content, html_file, multimodal_repo_path)
        
        # Verify the image path is correctly resolved
        assert len(images) > 0, "Expected at least one image"
        assert all(not img.startswith("http") for img in images), (
            "HTML extractor should filter out remote URLs"
        )
