"""E2E tests for HTMX image extraction - Story #66 AC4."""

import pytest

from src.code_indexer.indexing.image_extractor import (
    HtmlImageExtractor,
    ImageExtractorFactory,
)


@pytest.mark.e2e
class TestHtmxImageIndexing:
    """Test HTMX image extraction from real test fixtures."""

    def test_extract_images_from_troubleshooting_htmx(self, multimodal_repo_path):
        """Verify troubleshooting.htmx has correct image extraction."""
        htmx_file = multimodal_repo_path / "docs" / "troubleshooting.htmx"
        assert htmx_file.exists(), f"Test fixture missing: {htmx_file}"
        
        extractor = HtmlImageExtractor()
        content = htmx_file.read_text()
        
        images = extractor.extract_images(content, htmx_file, multimodal_repo_path)
        
        # troubleshooting.htmx has 1 image: <img src="../images/error-codes.gif" alt="HTTP Error Codes Reference">
        assert len(images) == 1, f"Expected 1 image, got {len(images)}"
        assert "images/error-codes.gif" in images[0]
        
    def test_factory_selects_html_extractor_for_htmx(self):
        """Verify ImageExtractorFactory returns HtmlImageExtractor for .htmx files."""
        extractor = ImageExtractorFactory.get_extractor(".htmx")
        
        assert extractor is not None, "Factory returned None for .htmx extension"
        assert isinstance(extractor, HtmlImageExtractor), (
            f"Expected HtmlImageExtractor, got {type(extractor)}"
        )
        
    def test_htmx_extractor_ignores_htmx_attributes(self, multimodal_repo_path):
        """Verify HTMX-specific attributes don't interfere with image extraction."""
        htmx_file = multimodal_repo_path / "docs" / "troubleshooting.htmx"
        content = htmx_file.read_text()
        
        # Verify file contains htmx attributes
        assert "hx-get" in content, "Test fixture should contain htmx attributes"
        assert "hx-trigger" in content, "Test fixture should contain htmx attributes"
        
        extractor = HtmlImageExtractor()
        images = extractor.extract_images(content, htmx_file, multimodal_repo_path)
        
        # Verify images are extracted despite htmx attributes
        assert len(images) > 0, "HTMX extractor should extract images despite htmx attributes"
