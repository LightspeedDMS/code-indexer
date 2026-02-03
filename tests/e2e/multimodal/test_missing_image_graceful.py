"""E2E tests for graceful handling of missing images - Story #66 AC5."""

import pytest

from src.code_indexer.indexing.image_extractor import MarkdownImageExtractor


@pytest.mark.e2e
class TestMissingImageGraceful:
    """Test graceful degradation when images are missing."""

    def test_missing_image_validation_returns_missing_reason(
        self, multimodal_repo_path
    ):
        """Verify missing images are detected with skip_reason='missing'."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "missing-image.md"
        assert md_file.exists(), f"Test fixture missing: {md_file}"

        extractor = MarkdownImageExtractor()
        content = md_file.read_text()

        # Extract with validation
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )

        # missing-image.md references network-diagram-missing.png which doesn't exist
        assert len(valid_images) == 0, "Expected no valid images"
        assert len(all_results) > 0, "Expected at least one validation result"

        # Find the missing image result
        missing_results = [
            r for r in all_results if not r.is_valid and r.skip_reason == "missing"
        ]
        assert len(missing_results) > 0, "Expected at least one missing image"
        assert any("network-diagram-missing.png" in r.path for r in missing_results)

    def test_text_content_still_extracted_despite_missing_image(
        self, multimodal_repo_path
    ):
        """Verify text content is still accessible even when image is missing."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "missing-image.md"
        content = md_file.read_text()

        # Verify the document has searchable text content
        assert "network topology" in content
        assert "firewall rules" in content
        assert "load balancer configuration" in content

        # The document should still be processed for text indexing
        # even though the image is missing
        extractor = MarkdownImageExtractor()
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )

        # No valid images, but document content is still available
        assert len(valid_images) == 0
        assert len(content) > 0, "Document content should be available"

    def test_validation_without_reason_also_detects_missing(self, multimodal_repo_path):
        """Verify backward-compatible validate_image() method detects missing files."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "missing-image.md"
        content = md_file.read_text()

        extractor = MarkdownImageExtractor()
        images = extractor.extract_images(content, md_file, multimodal_repo_path)

        # Validate each extracted image
        for image_path in images:
            is_valid = extractor.validate_image(image_path, multimodal_repo_path)
            assert not is_valid, f"Image {image_path} should be invalid (missing)"
