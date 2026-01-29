"""Unit tests for Image Extractors - Stories #62 and #63."""

import pytest
from pathlib import Path
import tempfile
import shutil

from src.code_indexer.indexing.image_extractor import (
    MarkdownImageExtractor,
    HtmlImageExtractor,
    ImageExtractorFactory,
    ImageValidationResult
)


class TestMarkdownImageExtractorParsing:
    """Test markdown image syntax parsing."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = MarkdownImageExtractor()

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_extract_single_image_standard_syntax(self):
        """Test extraction of single image with standard markdown syntax."""
        content = "# Title\n\n![Alt text](images/diagram.png)\n\nSome text."
        base_path = self.repo_root / "docs" / "guide.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/diagram.png"

    def test_extract_multiple_images(self):
        """Test extraction of multiple images from markdown."""
        content = """
# Documentation

![First](assets/first.jpg)

Some text here.

![Second](assets/second.png)

More text.

![Third](images/third.webp)
"""
        base_path = self.repo_root / "README.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 3
        assert "assets/first.jpg" in images
        assert "assets/second.png" in images
        assert "images/third.webp" in images

    def test_extract_images_with_complex_alt_text(self):
        """Test extraction with complex alt text containing special characters."""
        content = '![Complex (alt) text with "quotes"](path/to/image.png)'
        base_path = self.repo_root / "doc.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "path/to/image.png"

    def test_extract_no_images(self):
        """Test extraction returns empty list when no images present."""
        content = "# Just text\n\nNo images here."
        base_path = self.repo_root / "doc.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert images == []

    def test_filter_remote_urls_http(self):
        """Test that http:// URLs are filtered out."""
        content = """
![Local](local/image.png)
![Remote](http://example.com/image.png)
![Another local](assets/pic.jpg)
"""
        base_path = self.repo_root / "doc.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 2
        assert "local/image.png" in images
        assert "assets/pic.jpg" in images
        # http URL should be filtered out
        assert not any("http" in img for img in images)

    def test_filter_remote_urls_https(self):
        """Test that https:// URLs are filtered out."""
        content = """
![Local](images/diagram.png)
![Remote](https://cdn.example.com/image.jpg)
"""
        base_path = self.repo_root / "doc.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "images/diagram.png"

    def test_resolve_relative_paths_same_directory(self):
        """Test relative path resolution from same directory."""
        content = "![Image](image.png)"
        base_path = self.repo_root / "docs" / "guide.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/image.png"

    def test_resolve_relative_paths_parent_directory(self):
        """Test relative path resolution with ../ parent navigation."""
        content = "![Image](../assets/image.png)"
        base_path = self.repo_root / "docs" / "guide" / "intro.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/assets/image.png"

    def test_resolve_relative_paths_multiple_parent_levels(self):
        """Test relative path resolution with multiple ../ levels."""
        content = "![Image](../../images/diagram.png)"
        base_path = self.repo_root / "a" / "b" / "c" / "doc.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "a/images/diagram.png"

    def test_absolute_paths_within_repo(self):
        """Test absolute paths that start with / are resolved relative to repo root."""
        content = "![Image](/docs/images/pic.png)"
        base_path = self.repo_root / "anywhere" / "doc.md"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/pic.png"


class TestMarkdownImageExtractorValidation:
    """Test image validation logic."""

    def setup_method(self):
        """Set up test fixtures with real files."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = MarkdownImageExtractor()

        # Create test image files
        self.images_dir = self.repo_root / "images"
        self.images_dir.mkdir()

        # Create dummy image files
        (self.images_dir / "valid.png").write_bytes(b"fake png content")
        (self.images_dir / "valid.jpg").write_bytes(b"fake jpg content")
        (self.images_dir / "valid.jpeg").write_bytes(b"fake jpeg content")
        (self.images_dir / "valid.webp").write_bytes(b"fake webp content")
        (self.images_dir / "valid.gif").write_bytes(b"fake gif content")
        (self.images_dir / "invalid.bmp").write_bytes(b"fake bmp content")
        (self.images_dir / "invalid.svg").write_bytes(b"fake svg content")

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_validate_png_format(self):
        """Test PNG format is supported."""
        result = self.extractor.validate_image("images/valid.png", self.repo_root)
        assert result is True

    def test_validate_jpg_format(self):
        """Test JPG format is supported."""
        result = self.extractor.validate_image("images/valid.jpg", self.repo_root)
        assert result is True

    def test_validate_jpeg_format(self):
        """Test JPEG format is supported."""
        result = self.extractor.validate_image("images/valid.jpeg", self.repo_root)
        assert result is True

    def test_validate_webp_format(self):
        """Test WebP format is supported."""
        result = self.extractor.validate_image("images/valid.webp", self.repo_root)
        assert result is True

    def test_validate_gif_format(self):
        """Test GIF format is supported."""
        result = self.extractor.validate_image("images/valid.gif", self.repo_root)
        assert result is True

    def test_reject_unsupported_format_bmp(self):
        """Test BMP format is rejected."""
        result = self.extractor.validate_image("images/invalid.bmp", self.repo_root)
        assert result is False

    def test_reject_unsupported_format_svg(self):
        """Test SVG format is rejected."""
        result = self.extractor.validate_image("images/invalid.svg", self.repo_root)
        assert result is False

    def test_reject_nonexistent_file(self):
        """Test nonexistent file is rejected."""
        result = self.extractor.validate_image("images/nonexistent.png", self.repo_root)
        assert result is False

    def test_reject_path_outside_repo(self):
        """Test path outside repository is rejected."""
        # Create file outside repo
        outside_dir = self.temp_dir / "outside"
        outside_dir.mkdir()
        (outside_dir / "image.png").write_bytes(b"fake content")

        # Try to validate with path escaping repo
        result = self.extractor.validate_image("../outside/image.png", self.repo_root)
        assert result is False

    def test_case_insensitive_extension_matching(self):
        """Test extension matching is case-insensitive."""
        # Create file with uppercase extension
        (self.images_dir / "upper.PNG").write_bytes(b"fake content")

        result = self.extractor.validate_image("images/upper.PNG", self.repo_root)
        assert result is True


class TestMarkdownImageExtractorIntegration:
    """Integration tests combining extraction and validation."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = MarkdownImageExtractor()

        # Create test structure
        docs_dir = self.repo_root / "docs"
        docs_dir.mkdir()
        images_dir = docs_dir / "images"
        images_dir.mkdir()

        # Create valid images
        (images_dir / "diagram.png").write_bytes(b"fake png")
        (images_dir / "chart.jpg").write_bytes(b"fake jpg")

        # Create invalid images
        (images_dir / "unsupported.bmp").write_bytes(b"fake bmp")

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_extract_and_validate_filters_invalid_images(self):
        """Test that extraction + validation filters out invalid images."""
        content = """
![Valid PNG](images/diagram.png)
![Valid JPG](images/chart.jpg)
![Invalid BMP](images/unsupported.bmp)
![Nonexistent](images/missing.png)
"""
        base_path = self.repo_root / "docs" / "guide.md"

        # Extract images
        images = self.extractor.extract_images(content, base_path, self.repo_root)

        # Should extract all 4 references
        assert len(images) == 4

        # Now validate and filter
        valid_images = [
            img for img in images
            if self.extractor.validate_image(img, self.repo_root)
        ]

        # Only 2 should be valid
        assert len(valid_images) == 2
        assert "docs/images/diagram.png" in valid_images
        assert "docs/images/chart.jpg" in valid_images


# Story #63: HTML Image Extraction Tests


class TestHtmlImageExtractorParsing:
    """Test HTML image tag parsing - Story #63 AC1."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = HtmlImageExtractor()

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_extract_single_image_double_quotes(self):
        """Test extraction with standard double quotes."""
        content = '<img src="images/diagram.png" alt="Diagram">'
        base_path = self.repo_root / "docs" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/diagram.png"

    def test_extract_single_image_single_quotes(self):
        """Test extraction with single quotes."""
        content = "<img src='images/diagram.png' alt='Diagram'>"
        base_path = self.repo_root / "docs" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/diagram.png"

    def test_extract_single_image_no_quotes(self):
        """Test extraction with no quotes (valid HTML5)."""
        content = "<img src=images/diagram.png alt=Diagram>"
        base_path = self.repo_root / "docs" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/diagram.png"

    def test_extract_self_closing_tag(self):
        """Test extraction with self-closing tag."""
        content = '<img src="images/diagram.png" alt="Diagram" />'
        base_path = self.repo_root / "docs" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/diagram.png"

    def test_extract_with_multiple_attributes(self):
        """Test extraction with multiple attributes."""
        content = '<img src="images/diagram.png" alt="Schema" class="responsive" width="800" height="600" loading="lazy">'
        base_path = self.repo_root / "docs" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/diagram.png"

    def test_extract_src_ignores_srcset(self):
        """Test that srcset attribute is ignored, only src is extracted."""
        content = '''
<img src="images/api-flow.jpg"
     srcset="images/api-flow-2x.jpg 2x, images/api-flow-3x.jpg 3x"
     alt="API Flow">
'''
        base_path = self.repo_root / "docs" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/api-flow.jpg"
        # Verify srcset paths are NOT extracted
        assert "docs/images/api-flow-2x.jpg" not in images
        assert "docs/images/api-flow-3x.jpg" not in images

    def test_extract_src_ignores_data_src(self):
        """Test that data-src attribute is ignored, only src is extracted."""
        content = '''
<img src="images/config.webp"
     data-src="images/config-lazy.webp"
     alt="Lazy Loaded">
'''
        base_path = self.repo_root / "docs" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/config.webp"
        # Verify data-src is NOT extracted
        assert "docs/images/config-lazy.webp" not in images

    def test_filter_remote_url_https(self):
        """Test that https:// URLs are filtered out."""
        content = '''
<img src="images/local.png" alt="Local">
<img src="https://example.com/remote.png" alt="Remote">
<img src="assets/another.jpg" alt="Another">
'''
        base_path = self.repo_root / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 2
        assert "images/local.png" in images
        assert "assets/another.jpg" in images
        assert not any("https" in img for img in images)

    def test_filter_remote_url_http(self):
        """Test that http:// URLs are filtered out."""
        content = '''
<img src="images/local.png" alt="Local">
<img src="http://example.com/remote.png" alt="Remote">
'''
        base_path = self.repo_root / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "images/local.png"

    def test_filter_data_uri(self):
        """Test that data URIs are filtered out."""
        content = '''
<img src="images/real.png" alt="Real">
<img src="data:image/png;base64,iVBORw0KGgo..." alt="Data URI">
<img src="images/another.jpg" alt="Another">
'''
        base_path = self.repo_root / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 2
        assert "images/real.png" in images
        assert "images/another.jpg" in images
        assert not any("data:" in img for img in images)

    def test_resolve_relative_paths_same_directory(self):
        """Test relative path resolution from same directory."""
        content = '<img src="image.png" alt="Image">'
        base_path = self.repo_root / "docs" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/image.png"

    def test_resolve_relative_paths_parent_directory(self):
        """Test relative path resolution with ../ parent navigation."""
        content = '<img src="../images/diagram.png" alt="Diagram">'
        base_path = self.repo_root / "docs" / "guide" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/diagram.png"

    def test_resolve_absolute_paths_within_repo(self):
        """Test absolute paths starting with / are resolved relative to repo root."""
        content = '<img src="/docs/images/pic.png" alt="Picture">'
        base_path = self.repo_root / "anywhere" / "page.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        assert len(images) == 1
        assert images[0] == "docs/images/pic.png"


class TestHtmlImageExtractorValidation:
    """Test HTML image validation logic - Story #63 AC1."""

    def setup_method(self):
        """Set up test fixtures with real files."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = HtmlImageExtractor()

        # Create test image files
        self.images_dir = self.repo_root / "images"
        self.images_dir.mkdir()

        # Create dummy image files
        (self.images_dir / "valid.png").write_bytes(b"fake png content")
        (self.images_dir / "valid.jpg").write_bytes(b"fake jpg content")
        (self.images_dir / "valid.jpeg").write_bytes(b"fake jpeg content")
        (self.images_dir / "valid.webp").write_bytes(b"fake webp content")
        (self.images_dir / "valid.gif").write_bytes(b"fake gif content")
        (self.images_dir / "invalid.bmp").write_bytes(b"fake bmp content")

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_validate_supported_formats(self):
        """Test all supported formats validate correctly."""
        assert self.extractor.validate_image("images/valid.png", self.repo_root) is True
        assert self.extractor.validate_image("images/valid.jpg", self.repo_root) is True
        assert self.extractor.validate_image("images/valid.jpeg", self.repo_root) is True
        assert self.extractor.validate_image("images/valid.webp", self.repo_root) is True
        assert self.extractor.validate_image("images/valid.gif", self.repo_root) is True

    def test_reject_unsupported_format(self):
        """Test unsupported format is rejected."""
        result = self.extractor.validate_image("images/invalid.bmp", self.repo_root)
        assert result is False

    def test_reject_nonexistent_file(self):
        """Test nonexistent file is rejected."""
        result = self.extractor.validate_image("images/missing.png", self.repo_root)
        assert result is False


class TestHtmlImageExtractorIntegration:
    """Integration tests for HTML image extraction - Story #63 AC4."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = HtmlImageExtractor()

        # Create test structure matching html-variants.html
        docs_dir = self.repo_root / "docs" / "edge-cases"
        docs_dir.mkdir(parents=True)
        images_dir = self.repo_root / "docs" / "images"
        images_dir.mkdir(parents=True)

        # Create valid images referenced in html-variants.html
        (images_dir / "database-schema.png").write_bytes(b"fake png")
        (images_dir / "api-flow.jpg").write_bytes(b"fake jpg")
        (images_dir / "config-options.webp").write_bytes(b"fake webp")
        (images_dir / "error-codes.gif").write_bytes(b"fake gif")

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_html_variants_edge_cases(self):
        """Test all HTML variants from html-variants.html fixture."""
        # Read the html-variants.html file using relative path from test file
        test_file_dir = Path(__file__).parent
        project_root = test_file_dir.parent.parent.parent
        html_variants_path = project_root / "test-fixtures" / "multimodal-mock-repo" / "docs" / "edge-cases" / "html-variants.html"

        with open(html_variants_path, "r") as f:
            content = f.read()

        base_path = self.repo_root / "docs" / "edge-cases" / "html-variants.html"

        images = self.extractor.extract_images(content, base_path, self.repo_root)

        # Should extract local image references (not remote URLs or data URIs)
        # Expected images from html-variants.html:
        # - ../images/database-schema.png (appears 3 times)
        # - ../images/api-flow.jpg (appears 2 times)
        # - ../images/config-options.webp (appears 2 times)
        # - ../images/error-codes.gif (appears 2 times)

        # Verify we got the expected unique images
        unique_images = set(images)
        assert "docs/images/database-schema.png" in unique_images
        assert "docs/images/api-flow.jpg" in unique_images
        assert "docs/images/config-options.webp" in unique_images
        assert "docs/images/error-codes.gif" in unique_images

        # Verify remote URLs and data URIs were filtered
        assert not any("http" in img for img in images)
        assert not any("https" in img for img in images)
        assert not any("data:" in img for img in images)

        # Verify srcset and data-src were not extracted
        assert not any("2x" in img for img in images)
        assert not any("3x" in img for img in images)
        assert not any("lazy" in img for img in images)


# Story #63: ImageExtractorFactory Tests (AC3)


class TestImageExtractorFactory:
    """Test ImageExtractorFactory - Story #63 AC3."""

    def setup_method(self):
        """Set up test fixtures."""
        pass

    def test_get_extractor_for_markdown(self):
        """Test factory returns MarkdownImageExtractor for .md extension."""
        extractor = ImageExtractorFactory.get_extractor(".md")

        assert extractor is not None
        assert isinstance(extractor, MarkdownImageExtractor)

    def test_get_extractor_for_html(self):
        """Test factory returns HtmlImageExtractor for .html extension."""
        extractor = ImageExtractorFactory.get_extractor(".html")

        assert extractor is not None
        assert isinstance(extractor, HtmlImageExtractor)

    def test_get_extractor_for_htmx(self):
        """Test factory returns HtmlImageExtractor for .htmx extension."""
        extractor = ImageExtractorFactory.get_extractor(".htmx")

        assert extractor is not None
        assert isinstance(extractor, HtmlImageExtractor)

    def test_get_extractor_for_unsupported_extension(self):
        """Test factory returns None for unsupported extensions."""
        extractor = ImageExtractorFactory.get_extractor(".txt")

        assert extractor is None


# Story #64: Graceful Degradation with Validation Results


class TestMarkdownExtractImagesWithValidation:
    """Test extract_images_with_validation() for MarkdownImageExtractor - Story #64 AC2."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = MarkdownImageExtractor()

        # Create test structure
        self.images_dir = self.repo_root / "images"
        self.images_dir.mkdir()

        # Create valid images
        (self.images_dir / "valid.png").write_bytes(b"fake png")
        (self.images_dir / "valid.jpg").write_bytes(b"fake jpg")

        # Create invalid images
        (self.images_dir / "invalid.bmp").write_bytes(b"fake bmp")

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_returns_valid_images_and_all_results(self):
        """Test method returns both valid paths and all validation results."""
        content = """
![Valid PNG](images/valid.png)
![Missing](images/missing.png)
![Invalid BMP](images/invalid.bmp)
"""
        base_path = self.repo_root / "doc.md"

        valid_paths, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        # Should return 1 valid path
        assert len(valid_paths) == 1
        assert "images/valid.png" in valid_paths

        # Should return 3 validation results
        assert len(all_results) == 3

    def test_validation_results_have_correct_structure(self):
        """Test validation results contain path, is_valid, and skip_reason."""
        content = "![Valid](images/valid.png)\n![Missing](images/missing.png)"
        base_path = self.repo_root / "doc.md"

        _, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        # Check valid image result
        valid_result = next(r for r in all_results if r.path == "images/valid.png")
        assert isinstance(valid_result, ImageValidationResult)
        assert valid_result.is_valid is True
        assert valid_result.skip_reason is None

        # Check missing image result
        missing_result = next(r for r in all_results if r.path == "images/missing.png")
        assert isinstance(missing_result, ImageValidationResult)
        assert missing_result.is_valid is False
        assert missing_result.skip_reason == "missing"

    def test_skip_reason_for_unsupported_format(self):
        """Test skip_reason is 'unsupported_format' for invalid extensions."""
        content = "![BMP](images/invalid.bmp)"
        base_path = self.repo_root / "doc.md"

        _, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        assert len(all_results) == 1
        result = all_results[0]
        assert result.is_valid is False
        assert result.skip_reason == "unsupported_format"

    def test_skip_reason_for_remote_url(self):
        """Test skip_reason is 'remote_url' for http/https URLs."""
        content = "![Remote](https://example.com/image.png)"
        base_path = self.repo_root / "doc.md"

        _, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        assert len(all_results) == 1
        result = all_results[0]
        assert result.is_valid is False
        assert result.skip_reason == "remote_url"

    def test_multiple_images_mixed_validity(self):
        """Test extraction with mix of valid and invalid images."""
        content = """
![Valid 1](images/valid.png)
![Remote](https://example.com/remote.png)
![Valid 2](images/valid.jpg)
![Missing](images/missing.png)
![Invalid](images/invalid.bmp)
"""
        base_path = self.repo_root / "doc.md"

        valid_paths, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        # Should have 2 valid paths
        assert len(valid_paths) == 2
        assert "images/valid.png" in valid_paths
        assert "images/valid.jpg" in valid_paths

        # Should have 5 validation results
        assert len(all_results) == 5

        # Count by validity
        valid_results = [r for r in all_results if r.is_valid]
        invalid_results = [r for r in all_results if not r.is_valid]
        assert len(valid_results) == 2
        assert len(invalid_results) == 3

        # Check skip reasons
        skip_reasons = {r.skip_reason for r in invalid_results}
        assert "remote_url" in skip_reasons
        assert "missing" in skip_reasons
        assert "unsupported_format" in skip_reasons


class TestHtmlExtractImagesWithValidation:
    """Test extract_images_with_validation() for HtmlImageExtractor - Story #64 AC2."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = HtmlImageExtractor()

        # Create test structure
        self.images_dir = self.repo_root / "images"
        self.images_dir.mkdir()

        # Create valid images
        (self.images_dir / "valid.png").write_bytes(b"fake png")
        (self.images_dir / "valid.jpg").write_bytes(b"fake jpg")

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_returns_valid_images_and_all_results(self):
        """Test method returns both valid paths and all validation results."""
        content = """
<img src="images/valid.png" alt="Valid">
<img src="images/missing.png" alt="Missing">
<img src="https://example.com/remote.png" alt="Remote">
"""
        base_path = self.repo_root / "page.html"

        valid_paths, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        # Should return 1 valid path
        assert len(valid_paths) == 1
        assert "images/valid.png" in valid_paths

        # Should return 3 validation results
        assert len(all_results) == 3

    def test_skip_reason_for_data_uri(self):
        """Test skip_reason is 'data_uri' for data: URIs."""
        content = '<img src="data:image/png;base64,iVBORw0KGgo..." alt="Data">'
        base_path = self.repo_root / "page.html"

        _, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        assert len(all_results) == 1
        result = all_results[0]
        assert result.is_valid is False
        assert result.skip_reason == "data_uri"

    def test_skip_reason_for_remote_url(self):
        """Test skip_reason is 'remote_url' for http/https URLs."""
        content = '<img src="http://example.com/image.png" alt="Remote">'
        base_path = self.repo_root / "page.html"

        _, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        assert len(all_results) == 1
        result = all_results[0]
        assert result.is_valid is False
        assert result.skip_reason == "remote_url"

    def test_multiple_images_mixed_validity(self):
        """Test extraction with mix of valid and invalid images."""
        content = """
<img src="images/valid.png" alt="Valid 1">
<img src="https://example.com/remote.png" alt="Remote">
<img src="images/valid.jpg" alt="Valid 2">
<img src="data:image/png;base64,abc" alt="Data URI">
<img src="images/missing.png" alt="Missing">
"""
        base_path = self.repo_root / "page.html"

        valid_paths, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        # Should have 2 valid paths
        assert len(valid_paths) == 2
        assert "images/valid.png" in valid_paths
        assert "images/valid.jpg" in valid_paths

        # Should have 5 validation results
        assert len(all_results) == 5

        # Count by validity
        valid_results = [r for r in all_results if r.is_valid]
        invalid_results = [r for r in all_results if not r.is_valid]
        assert len(valid_results) == 2
        assert len(invalid_results) == 3

        # Check skip reasons
        skip_reasons = {r.skip_reason for r in invalid_results}
        assert "remote_url" in skip_reasons
        assert "data_uri" in skip_reasons
        assert "missing" in skip_reasons


class TestImageSizeValidation:
    """Test image size validation - Story #64 AC3."""

    def setup_method(self):
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.repo_root = self.temp_dir / "repo"
        self.repo_root.mkdir()
        self.extractor = MarkdownImageExtractor()

        # Create test structure
        self.images_dir = self.repo_root / "images"
        self.images_dir.mkdir()

    def teardown_method(self):
        """Clean up test fixtures."""
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def test_validate_image_accepts_small_file(self):
        """Test validation passes for files under 10MB."""
        # Create 1MB file
        small_file = self.images_dir / "small.png"
        small_file.write_bytes(b"x" * (1024 * 1024))  # 1MB

        result = self.extractor.validate_image("images/small.png", self.repo_root)
        assert result is True

    def test_validate_image_accepts_exactly_10mb(self):
        """Test validation passes for files exactly 10MB."""
        # Create exactly 10MB file
        exact_file = self.images_dir / "exact.png"
        exact_file.write_bytes(b"x" * (10 * 1024 * 1024))  # 10MB

        result = self.extractor.validate_image("images/exact.png", self.repo_root)
        assert result is True

    def test_validate_image_rejects_oversized_file(self):
        """Test validation fails for files over 10MB."""
        # Create 11MB file
        large_file = self.images_dir / "large.png"
        large_file.write_bytes(b"x" * (11 * 1024 * 1024))  # 11MB

        result = self.extractor.validate_image("images/large.png", self.repo_root)
        assert result is False

    def test_extract_with_validation_marks_oversized_correctly(self):
        """Test extract_images_with_validation() sets skip_reason='oversized' for large files."""
        # Create oversized file
        large_file = self.images_dir / "huge.png"
        large_file.write_bytes(b"x" * (15 * 1024 * 1024))  # 15MB

        content = "![Huge](images/huge.png)"
        base_path = self.repo_root / "doc.md"

        valid_paths, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        # Should have no valid paths
        assert len(valid_paths) == 0

        # Should have 1 result with oversized skip_reason
        assert len(all_results) == 1
        result = all_results[0]
        assert result.is_valid is False
        assert result.skip_reason == "oversized"

    def test_extract_with_validation_accepts_valid_sized_file(self):
        """Test extract_images_with_validation() accepts files under 10MB."""
        # Create valid sized file
        valid_file = self.images_dir / "valid.png"
        valid_file.write_bytes(b"x" * (5 * 1024 * 1024))  # 5MB

        content = "![Valid](images/valid.png)"
        base_path = self.repo_root / "doc.md"

        valid_paths, all_results = self.extractor.extract_images_with_validation(
            content, base_path, self.repo_root
        )

        # Should have 1 valid path
        assert len(valid_paths) == 1
        assert "images/valid.png" in valid_paths

        # Result should be valid
        assert len(all_results) == 1
        result = all_results[0]
        assert result.is_valid is True
        assert result.skip_reason is None
