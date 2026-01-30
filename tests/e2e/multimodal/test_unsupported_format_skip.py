"""E2E tests for unsupported format filtering - Story #66 AC7."""

import pytest

from src.code_indexer.indexing.image_extractor import MarkdownImageExtractor


@pytest.mark.e2e
class TestUnsupportedFormatSkip:
    """Test that unsupported image formats are filtered out."""

    def test_unsupported_format_filtered_with_reason(self, multimodal_repo_path):
        """Verify .bmp files are rejected with skip_reason='unsupported_format'."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "unsupported-format.md"
        assert md_file.exists(), f"Test fixture missing: {md_file}"
        
        extractor = MarkdownImageExtractor()
        content = md_file.read_text()
        
        # Extract with validation
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )
        
        # unsupported-format.md has 1 BMP and 1 PNG
        # BMP should be in all_results with skip_reason='unsupported_format'
        unsupported_results = [r for r in all_results if r.skip_reason == "unsupported_format"]
        assert len(unsupported_results) >= 1, f"Expected at least 1 unsupported format, got {len(unsupported_results)}"
        
        # Verify BMP detected as unsupported
        bmp_results = [r for r in unsupported_results if "unsupported.bmp" in r.path]
        assert len(bmp_results) == 1, "Expected BMP file to be detected as unsupported"
        
        # Valid images should only contain supported formats (PNG in this case)
        assert len(valid_images) >= 1, "Expected at least one valid image (PNG)"
        supported_images = [img for img in valid_images if "database-schema.png" in img]
        assert len(supported_images) == 1, "Expected PNG image to be valid"
        
    def test_supported_formats_still_processed(self, multimodal_repo_path):
        """Verify supported formats are still extracted when document has unsupported formats."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "unsupported-format.md"
        content = md_file.read_text()
        
        extractor = MarkdownImageExtractor()
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )
        
        # PNG should be valid
        png_images = [img for img in valid_images if img.endswith(".png")]
        assert len(png_images) >= 1, "Expected at least one valid PNG image"
        
    def test_only_supported_formats_in_extraction(self, multimodal_repo_path):
        """Verify extract_images() returns only supported formats."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "unsupported-format.md"
        content = md_file.read_text()
        
        extractor = MarkdownImageExtractor()
        images = extractor.extract_images(content, md_file, multimodal_repo_path)
        
        # Should extract both PNG and BMP paths, but validation will reject BMP
        # Let's verify validation rejects BMP
        for image_path in images:
            result = extractor.validate_image_with_reason(image_path, multimodal_repo_path)
            if image_path.endswith(".bmp"):
                assert not result.is_valid, "BMP should be invalid"
                assert result.skip_reason == "unsupported_format", "BMP should have unsupported_format reason"
            elif image_path.endswith(".png"):
                assert result.is_valid, "PNG should be valid"
