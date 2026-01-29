"""E2E tests for remote URL filtering - Story #66 AC6."""

import pytest
from pathlib import Path

from src.code_indexer.indexing.image_extractor import MarkdownImageExtractor


@pytest.mark.e2e
class TestRemoteUrlSkip:
    """Test that remote URLs are filtered out."""

    def test_remote_urls_filtered_with_reason(self, multimodal_repo_path):
        """Verify remote URLs (http://, https://) are skipped with skip_reason='remote_url'."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "remote-url.md"
        assert md_file.exists(), f"Test fixture missing: {md_file}"
        
        extractor = MarkdownImageExtractor()
        content = md_file.read_text()
        
        # Extract with validation
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )
        
        # remote-url.md has 2 remote URLs and 1 local image
        # Remote URLs should be in all_results with skip_reason='remote_url'
        remote_results = [r for r in all_results if r.skip_reason == "remote_url"]
        assert len(remote_results) >= 2, f"Expected at least 2 remote URLs, got {len(remote_results)}"
        
        # Verify remote URLs detected
        remote_urls = [r.path for r in remote_results]
        assert any("http://example.com" in url or "https://example.com" in url for url in remote_urls)
        assert any("https://cdn.example.org" in url for url in remote_urls)
        
        # Valid images should only contain local images
        assert len(valid_images) > 0, "Expected at least one valid local image"
        assert all(not img.startswith("http") for img in valid_images)
        
    def test_local_image_still_processed_with_remote_urls(self, multimodal_repo_path):
        """Verify local images are still extracted when document has remote URLs."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "remote-url.md"
        content = md_file.read_text()
        
        extractor = MarkdownImageExtractor()
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )
        
        # remote-url.md has 1 local image: error-codes.gif
        local_images = [img for img in valid_images if "error-codes.gif" in img]
        assert len(local_images) == 1, "Expected 1 local image (error-codes.gif)"
        
    def test_backward_compatible_extraction_filters_remote_urls(self, multimodal_repo_path):
        """Verify extract_images() method filters remote URLs (backward compatibility)."""
        md_file = multimodal_repo_path / "docs" / "edge-cases" / "remote-url.md"
        content = md_file.read_text()
        
        extractor = MarkdownImageExtractor()
        images = extractor.extract_images(content, md_file, multimodal_repo_path)
        
        # Should only return local images, no remote URLs
        assert len(images) > 0, "Expected at least one local image"
        assert all(not img.startswith("http") for img in images), (
            "extract_images() should filter out remote URLs"
        )
