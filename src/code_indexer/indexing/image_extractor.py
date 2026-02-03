"""Image extraction for multimodal indexing - Stories #62 and #63."""

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Union, Optional


@dataclass
class ImageValidationResult:
    """Result of image validation with skip reason tracking - Story #64 AC1.

    Attributes:
        path: Image path or reference that was validated
        is_valid: True if image passes all validation checks
        skip_reason: Reason for skipping if invalid (None if valid)
                    Valid reasons: "missing", "remote_url", "oversized",
                                  "unsupported_format", "data_uri"
    """

    path: str
    is_valid: bool
    skip_reason: Optional[str] = None


class ImageExtractor:
    """Base class for image extractors with shared validation logic."""

    # Supported image formats (case-insensitive)
    SUPPORTED_FORMATS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    # Maximum image size in bytes (10MB) - Story #64 AC3
    MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024

    def _resolve_image_path(
        self, image_path: str, base_dir: Path, repo_root: Path
    ) -> str:
        """Resolve image path to be relative to repo_root.

        Args:
            image_path: Raw image path (may be relative or absolute)
            base_dir: Directory containing the source file
            repo_root: Repository root directory

        Returns:
            Path relative to repo_root, or empty string if path escapes repo
        """
        image_path_obj = Path(image_path)

        # Handle absolute paths (starting with /)
        if image_path.startswith("/"):
            # Remove leading slash and resolve relative to repo_root
            relative_path = image_path.lstrip("/")
            full_path = repo_root / relative_path
        else:
            # Relative path - resolve from base_dir
            full_path = (base_dir / image_path_obj).resolve()

        # Ensure resolved path is within repo_root
        try:
            # Get relative path from repo_root
            relative_to_repo = full_path.relative_to(repo_root.resolve())
            return str(relative_to_repo)
        except ValueError:
            # Path is outside repository - reject it
            return ""

    def validate_image_with_reason(
        self, image_path: str, repo_root: Union[str, Path]
    ) -> ImageValidationResult:
        """Validate image and return detailed result with skip reason - Story #64 AC2.

        Args:
            image_path: Image path relative to repo_root
            repo_root: Repository root directory

        Returns:
            ImageValidationResult with is_valid and skip_reason

        Validation checks:
        - File exists within repository
        - File extension is supported (PNG, JPG, JPEG, WebP, GIF)
        - File size is under MAX_IMAGE_SIZE_BYTES (10MB)
        - Path does not escape repository boundaries
        """
        repo_root = Path(repo_root)
        full_path = repo_root / image_path

        # Check 1: File must exist
        if not full_path.exists() or not full_path.is_file():
            return ImageValidationResult(
                path=image_path, is_valid=False, skip_reason="missing"
            )

        # Check 2: File extension must be supported (case-insensitive)
        extension = full_path.suffix.lower()
        if extension not in self.SUPPORTED_FORMATS:
            return ImageValidationResult(
                path=image_path, is_valid=False, skip_reason="unsupported_format"
            )

        # Check 3: File size must be under limit (Story #64 AC3)
        file_size = full_path.stat().st_size
        if file_size > self.MAX_IMAGE_SIZE_BYTES:
            return ImageValidationResult(
                path=image_path, is_valid=False, skip_reason="oversized"
            )

        # Check 4: Path must be within repository (no directory traversal)
        try:
            full_path.resolve().relative_to(repo_root.resolve())
            return ImageValidationResult(
                path=image_path, is_valid=True, skip_reason=None
            )
        except ValueError:
            # Path escapes repository
            return ImageValidationResult(
                path=image_path,
                is_valid=False,
                skip_reason="missing",  # Treat as missing since it's inaccessible
            )

    def validate_image(self, image_path: str, repo_root: Union[str, Path]) -> bool:
        """Validate that an image meets all requirements.

        Args:
            image_path: Image path relative to repo_root
            repo_root: Repository root directory

        Returns:
            True if image is valid, False otherwise

        Validation checks:
        - File exists within repository
        - File extension is supported (PNG, JPG, JPEG, WebP, GIF)
        - File size is under MAX_IMAGE_SIZE_BYTES (10MB)
        - Path does not escape repository boundaries

        Note:
            This method maintains backward compatibility. Use validate_image_with_reason()
            for detailed skip reasons.
        """
        result = self.validate_image_with_reason(image_path, repo_root)
        return result.is_valid


class MarkdownImageExtractor(ImageExtractor):
    """Extract and validate image references from markdown content.

    Supports:
    - Standard markdown syntax: ![alt](path)
    - Relative and absolute paths
    - Image format validation (PNG, JPEG, WebP, GIF)
    - Filtering of remote URLs (http://, https://)
    - Repository boundary enforcement
    """

    def __init__(self):
        """Initialize image extractor."""
        # Regex pattern for markdown images: ![alt text](path)
        # Matches: ![anything](path) but not URLs
        self.md_image_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)", re.MULTILINE)

    def extract_images(
        self, content: str, base_path: Union[str, Path], repo_root: Union[str, Path]
    ) -> List[str]:
        """Extract image paths from markdown content.

        Args:
            content: Markdown content as string
            base_path: Path to the markdown file (for resolving relative paths)
            repo_root: Root directory of the repository

        Returns:
            List of relative image paths (relative to repo_root), filtered for:
            - Local files only (no http:// or https:// URLs)
            - All extracted images (validation happens separately)

        Note:
            - Paths starting with / are resolved relative to repo_root
            - Relative paths are resolved relative to base_path's directory
            - Remote URLs (http://, https://) are filtered out
            - Returns paths relative to repo_root for consistency
        """
        base_path = Path(base_path)
        repo_root = Path(repo_root)

        # base_path is always treated as a file path (markdown file)
        # Use its parent directory for resolving relative image paths
        base_dir = base_path.parent

        extracted_images: List[str] = []

        # Find all markdown image references
        for match in self.md_image_pattern.finditer(content):
            # alt_text = match.group(1)  # Not needed for extraction
            image_path = match.group(2).strip()

            # Filter out remote URLs
            if image_path.startswith(("http://", "https://")):
                continue

            # Resolve the image path
            resolved_path = self._resolve_image_path(image_path, base_dir, repo_root)

            if resolved_path:
                extracted_images.append(resolved_path)

        return extracted_images

    def extract_images_with_validation(
        self, content: str, base_path: Union[str, Path], repo_root: Union[str, Path]
    ) -> tuple[List[str], List[ImageValidationResult]]:
        """Extract images and return validation results for all - Story #64 AC2.

        Args:
            content: Markdown content as string
            base_path: Path to the markdown file (for resolving relative paths)
            repo_root: Root directory of the repository

        Returns:
            Tuple of (valid_image_paths, all_validation_results):
            - valid_image_paths: List of paths that passed validation
            - all_validation_results: List of ImageValidationResult for ALL images,
                                     including remote URLs and invalid ones

        Note:
            This method provides detailed skip reasons for logging purposes.
            Use extract_images() for backward compatibility without validation details.
        """
        base_path = Path(base_path)
        repo_root = Path(repo_root)
        base_dir = base_path.parent

        valid_paths: List[str] = []
        all_results: List[ImageValidationResult] = []

        # Find all markdown image references
        for match in self.md_image_pattern.finditer(content):
            image_path = match.group(2).strip()

            # Check for remote URLs first
            if image_path.startswith(("http://", "https://")):
                all_results.append(
                    ImageValidationResult(
                        path=image_path, is_valid=False, skip_reason="remote_url"
                    )
                )
                continue

            # Resolve the image path
            resolved_path = self._resolve_image_path(image_path, base_dir, repo_root)

            if not resolved_path:
                # Path escapes repository or couldn't be resolved
                all_results.append(
                    ImageValidationResult(
                        path=image_path, is_valid=False, skip_reason="missing"
                    )
                )
                continue

            # Validate the resolved path
            validation_result = self.validate_image_with_reason(
                resolved_path, repo_root
            )
            all_results.append(validation_result)

            if validation_result.is_valid:
                valid_paths.append(resolved_path)

        return valid_paths, all_results


class HtmlImageExtractor(ImageExtractor):
    """Extract and validate image references from HTML content - Story #63 AC1.

    Supports:
    - HTML img tags: <img src="path">
    - Double quotes, single quotes, no quotes
    - Self-closing and non-self-closing tags
    - Extraction of src attribute only (ignores srcset, data-src)
    - Filtering of remote URLs (http://, https://)
    - Filtering of data URIs
    - Repository boundary enforcement
    """

    def __init__(self):
        """Initialize HTML image extractor."""
        pass

    def extract_images(
        self, content: str, base_path: Union[str, Path], repo_root: Union[str, Path]
    ) -> List[str]:
        """Extract image paths from HTML content.

        Args:
            content: HTML content as string
            base_path: Path to the HTML file (for resolving relative paths)
            repo_root: Root directory of the repository

        Returns:
            List of relative image paths (relative to repo_root), filtered for:
            - Local files only (no http:// or https:// URLs)
            - No data URIs
            - Only src attribute (srcset and data-src are ignored)

        Note:
            - Paths starting with / are resolved relative to repo_root
            - Relative paths are resolved relative to base_path's directory
            - Remote URLs (http://, https://) are filtered out
            - Data URIs (data:...) are filtered out
            - Returns paths relative to repo_root for consistency
        """
        base_path = Path(base_path)
        repo_root = Path(repo_root)

        # base_path is always treated as a file path (HTML file)
        # Use its parent directory for resolving relative image paths
        base_dir = base_path.parent

        # Parse HTML and extract img src attributes
        parser = _ImgTagParser()
        parser.feed(content)

        extracted_images: List[str] = []

        for image_path in parser.img_sources:
            # Filter out remote URLs
            if image_path.startswith(("http://", "https://")):
                continue

            # Filter out data URIs
            if image_path.startswith("data:"):
                continue

            # Resolve the image path (inherited from ImageExtractor base class)
            resolved_path = self._resolve_image_path(image_path, base_dir, repo_root)

            if resolved_path:
                extracted_images.append(resolved_path)

        return extracted_images

    def extract_images_with_validation(
        self, content: str, base_path: Union[str, Path], repo_root: Union[str, Path]
    ) -> tuple[List[str], List[ImageValidationResult]]:
        """Extract images and return validation results for all - Story #64 AC2.

        Args:
            content: HTML content as string
            base_path: Path to the HTML file (for resolving relative paths)
            repo_root: Root directory of the repository

        Returns:
            Tuple of (valid_image_paths, all_validation_results):
            - valid_image_paths: List of paths that passed validation
            - all_validation_results: List of ImageValidationResult for ALL images,
                                     including remote URLs, data URIs, and invalid ones

        Note:
            This method provides detailed skip reasons for logging purposes.
            Use extract_images() for backward compatibility without validation details.
        """
        base_path = Path(base_path)
        repo_root = Path(repo_root)
        base_dir = base_path.parent

        valid_paths: List[str] = []
        all_results: List[ImageValidationResult] = []

        # Parse HTML and extract img src attributes
        parser = _ImgTagParser()
        parser.feed(content)

        for image_path in parser.img_sources:
            # Check for remote URLs
            if image_path.startswith(("http://", "https://")):
                all_results.append(
                    ImageValidationResult(
                        path=image_path, is_valid=False, skip_reason="remote_url"
                    )
                )
                continue

            # Check for data URIs
            if image_path.startswith("data:"):
                all_results.append(
                    ImageValidationResult(
                        path=image_path, is_valid=False, skip_reason="data_uri"
                    )
                )
                continue

            # Resolve the image path
            resolved_path = self._resolve_image_path(image_path, base_dir, repo_root)

            if not resolved_path:
                # Path escapes repository or couldn't be resolved
                all_results.append(
                    ImageValidationResult(
                        path=image_path, is_valid=False, skip_reason="missing"
                    )
                )
                continue

            # Validate the resolved path
            validation_result = self.validate_image_with_reason(
                resolved_path, repo_root
            )
            all_results.append(validation_result)

            if validation_result.is_valid:
                valid_paths.append(resolved_path)

        return valid_paths, all_results


class _ImgTagParser(HTMLParser):
    """Internal HTML parser to extract img src attributes."""

    def __init__(self):
        """Initialize parser."""
        super().__init__()
        self.img_sources: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        """Handle start tags - extract src from img tags.

        Args:
            tag: HTML tag name
            attrs: List of (attribute, value) tuples
        """
        if tag == "img":
            # Extract only the 'src' attribute (ignore srcset, data-src, etc.)
            for attr_name, attr_value in attrs:
                if attr_name == "src" and attr_value:
                    self.img_sources.append(attr_value.strip())
                    break  # Only extract the first src attribute


class ImageExtractorFactory:
    """Factory for creating appropriate image extractors based on file extension - Story #63 AC3."""

    EXTRACTORS = {
        ".md": MarkdownImageExtractor,
        ".html": HtmlImageExtractor,
        ".htm": HtmlImageExtractor,
        ".htmx": HtmlImageExtractor,
    }

    @classmethod
    def get_extractor(cls, file_extension: str):
        """Return appropriate extractor for file extension or None if unsupported.

        Args:
            file_extension: File extension (e.g., '.md', '.html', '.htmx')

        Returns:
            Instance of appropriate extractor class, or None if extension not supported
        """
        extractor_class = cls.EXTRACTORS.get(file_extension)
        if extractor_class:
            return extractor_class()
        return None
