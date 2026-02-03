"""E2E tests for mixed valid/invalid images - Story #66 AC9."""

import pytest

from src.code_indexer.indexing.image_extractor import MarkdownImageExtractor


@pytest.mark.e2e
class TestMixedImages:
    """Test comprehensive handling of mixed valid and invalid images."""

    def test_mixed_valid_invalid_extraction(self, multimodal_repo_path):
        """Verify 4 valid images extracted and 3 invalid images skipped."""
        md_file = (
            multimodal_repo_path / "docs" / "edge-cases" / "mixed-valid-invalid.md"
        )
        assert md_file.exists(), f"Test fixture missing: {md_file}"

        extractor = MarkdownImageExtractor()
        content = md_file.read_text()

        # Extract with validation
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )

        # Verify 4 valid images (PNG, JPG, WebP, GIF)
        assert (
            len(valid_images) == 4
        ), f"Expected 4 valid images, got {len(valid_images)}"

        # Check each supported format is present
        png_images = [img for img in valid_images if img.endswith(".png")]
        jpg_images = [img for img in valid_images if img.endswith(".jpg")]
        webp_images = [img for img in valid_images if img.endswith(".webp")]
        gif_images = [img for img in valid_images if img.endswith(".gif")]

        assert len(png_images) == 1, "Expected 1 PNG image"
        assert len(jpg_images) == 1, "Expected 1 JPG image"
        assert len(webp_images) == 1, "Expected 1 WebP image"
        assert len(gif_images) == 1, "Expected 1 GIF image"

        # Verify specific images
        assert any("database-schema.png" in img for img in valid_images)
        assert any("api-flow.jpg" in img for img in valid_images)
        assert any("config-options.webp" in img for img in valid_images)
        assert any("error-codes.gif" in img for img in valid_images)

    def test_invalid_images_have_correct_skip_reasons(self, multimodal_repo_path):
        """Verify 3 invalid images have correct skip_reasons."""
        md_file = (
            multimodal_repo_path / "docs" / "edge-cases" / "mixed-valid-invalid.md"
        )
        content = md_file.read_text()

        extractor = MarkdownImageExtractor()
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )

        # Get invalid results
        invalid_results = [r for r in all_results if not r.is_valid]
        assert (
            len(invalid_results) == 3
        ), f"Expected 3 invalid images, got {len(invalid_results)}"

        # Check skip reasons
        missing_results = [r for r in invalid_results if r.skip_reason == "missing"]
        remote_results = [r for r in invalid_results if r.skip_reason == "remote_url"]
        unsupported_results = [
            r for r in invalid_results if r.skip_reason == "unsupported_format"
        ]

        assert (
            len(missing_results) == 1
        ), f"Expected 1 missing image, got {len(missing_results)}"
        assert (
            len(remote_results) == 1
        ), f"Expected 1 remote URL, got {len(remote_results)}"
        assert (
            len(unsupported_results) == 1
        ), f"Expected 1 unsupported format, got {len(unsupported_results)}"

        # Verify specific invalid images
        assert any("does-not-exist.png" in r.path for r in missing_results)
        assert any("example.com" in r.path for r in remote_results)
        assert any("unsupported.bmp" in r.path for r in unsupported_results)

    def test_all_images_in_validation_results(self, multimodal_repo_path):
        """Verify all_results contains ALL images (valid + invalid)."""
        md_file = (
            multimodal_repo_path / "docs" / "edge-cases" / "mixed-valid-invalid.md"
        )
        content = md_file.read_text()

        extractor = MarkdownImageExtractor()
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )

        # Total should be 7 images (4 valid + 3 invalid)
        assert len(all_results) == 7, f"Expected 7 total images, got {len(all_results)}"

        # Verify counts
        valid_count = sum(1 for r in all_results if r.is_valid)
        invalid_count = sum(1 for r in all_results if not r.is_valid)

        assert valid_count == 4, f"Expected 4 valid in all_results, got {valid_count}"
        assert (
            invalid_count == 3
        ), f"Expected 3 invalid in all_results, got {invalid_count}"

    def test_text_content_indexed_despite_invalid_images(self, multimodal_repo_path):
        """Verify document text is accessible even with invalid images."""
        md_file = (
            multimodal_repo_path / "docs" / "edge-cases" / "mixed-valid-invalid.md"
        )
        content = md_file.read_text()

        # Verify searchable text content exists
        assert "Valid Images" in content
        assert "Invalid Images" in content
        assert "should be processed" in content
        assert "should be skipped" in content

        # Document should still be processed for text indexing
        extractor = MarkdownImageExtractor()
        valid_images, all_results = extractor.extract_images_with_validation(
            content, md_file, multimodal_repo_path
        )

        # Even with 3 invalid images, we get 4 valid images
        assert len(valid_images) == 4, "Valid images should still be extracted"
        assert len(content) > 0, "Document content should be available"
