"""Model-aware fixed-size chunker with optimized chunk sizes.

This module implements model-aware fixed-size chunking algorithm:
- Dynamic chunk size: optimized per embedding model (voyage-code-3: 4096, nomic-embed-text: 2048)
- Fixed overlap: 15% of chunk size between adjacent chunks
- Pure arithmetic: no parsing, no regex, no string analysis
- Pattern: next_start = current_start + (chunk_size - overlap_size)
- Multimodal image detection: extracts images from .md, .html, .htm, .htmx files for multimodal embeddings
"""

from typing import List, Dict, Any, Optional, Union
from pathlib import Path

from ..config import IndexingConfig, Config
from .image_extractor import ImageExtractorFactory


class FixedSizeChunker:
    """Model-aware fixed-size chunker optimized for different embedding providers.

    Algorithm:
    1. Determine optimal chunk size based on embedding model
    2. 15% overlap between adjacent chunks (consistent across models)
    3. Simple math: next_start = current_start + step_size
    4. No boundary detection - cuts at exact character positions
    5. No parsing - pure arithmetic operations
    6. Markdown image detection - extracts image references for multimodal embeddings

    Model-specific chunk sizes (VoyageAI only in v8.0+):
    - voyage-code-3: 4096 characters (1024 tokens, research optimal)
    - voyage-code-2: 4096 characters (1024 tokens, research optimal)
    - voyage-large-2: 4096 characters (1024 tokens, research optimal)
    - default: 1000 characters (conservative fallback)
    """

    # Model-aware chunk size mapping based on research and model capabilities
    MODEL_CHUNK_SIZES = {
        "voyage-code-3": 4096,  # 32K token capacity, 1024 tokens optimal
        "voyage-code-2": 4096,  # 16K token capacity, 1024 tokens optimal
        "voyage-large-2": 4096,  # Large context models, 1024 tokens optimal
        "voyage-3": 4096,  # General purpose, 1024 tokens optimal
        "voyage-3-large": 4096,  # Large model, 1024 tokens optimal
        "default": 1000,  # Conservative fallback for unknown models
    }

    # Fixed overlap percentage (15% of chunk size)
    OVERLAP_PERCENTAGE = 0.15

    def __init__(self, config: Union[IndexingConfig, Config]):
        """Initialize the model-aware fixed-size chunker.

        Args:
            config: Indexing configuration or full Config with embedding provider info
        """
        self.config = config

        # Determine chunk size based on embedding model
        if isinstance(config, Config):
            # Full Config passed - can determine model-aware chunk size
            # Only VoyageAI supported in v8.0+
            embedding_provider = config.embedding_provider
            if embedding_provider == "voyage-ai":
                # Get specific VoyageAI model
                model_name = config.voyage_ai.model
                self.chunk_size = self.MODEL_CHUNK_SIZES.get(
                    model_name, self.MODEL_CHUNK_SIZES["default"]
                )
            else:
                self.chunk_size = self.MODEL_CHUNK_SIZES["default"]
        else:
            # IndexingConfig only - use default chunk size
            self.chunk_size = self.MODEL_CHUNK_SIZES["default"]

        # Calculate derived values
        self.overlap_size = int(self.chunk_size * self.OVERLAP_PERCENTAGE)
        self.step_size = self.chunk_size - self.overlap_size

    def _extract_images(
        self, text: str, file_path: Path, repo_root: Optional[Path] = None
    ) -> List[Dict[str, str]]:
        """Extract image references from text using appropriate extractor.

        Uses ImageExtractorFactory to select the correct extractor based on file extension.
        Supports: .md, .html, .htm, .htmx

        Args:
            text: Text to search for image references
            file_path: Path to the file being chunked
            repo_root: Repository root directory (for image path resolution and validation)

        Returns:
            List of dictionaries with 'alt_text' and 'path' keys for each image found.
            Returns empty list if:
            - File extension not supported for image extraction
            - repo_root not provided (can't validate images)
            - No images found
        """
        # Can't extract images without repo_root (needed for validation)
        if not repo_root:
            return []

        # Get file extension
        file_extension = file_path.suffix.lower() if file_path.suffix else ""

        # Get appropriate extractor from factory
        extractor = ImageExtractorFactory.get_extractor(file_extension)
        if not extractor:
            # File type not supported for image extraction
            return []

        # Extract images (extractor returns list of relative paths as strings)
        image_paths = extractor.extract_images(text, file_path, repo_root)

        # Convert to dict format for backward compatibility with existing code
        # HtmlImageExtractor returns list[str], MarkdownImageExtractor returns list[str]
        images = []
        for path in image_paths:
            images.append({
                "alt_text": "",  # HTML extractor doesn't extract alt text
                "path": path
            })

        return images

    def _calculate_line_numbers(
        self, text: str, start_pos: int, end_pos: int
    ) -> tuple[int, int]:
        """Calculate line start and end numbers for a chunk.

        Args:
            text: The full text
            start_pos: Character start position
            end_pos: Character end position

        Returns:
            Tuple of (line_start, line_end) as 1-based line numbers
        """
        if not text or start_pos >= len(text):
            return 1, 1

        # Count newlines up to start position
        line_start = text[:start_pos].count("\n") + 1

        # Count newlines up to end position (but not beyond text length)
        actual_end = min(end_pos, len(text))
        line_end = text[:actual_end].count("\n") + 1

        return line_start, line_end

    def chunk_text(
        self, text: str, file_path: Optional[Path] = None, repo_root: Optional[Path] = None
    ) -> List[Dict[str, Any]]:
        """Split text into fixed-size chunks using ultra-simple algorithm.

        Args:
            text: Text to chunk
            file_path: Path to the file (for metadata and image extraction)
            repo_root: Repository root directory (for image path resolution and validation)

        Returns:
            List of chunk dictionaries with metadata
        """
        if not text or not text.strip():
            return []

        # Determine file extension
        file_extension = ""
        if file_path:
            file_extension = file_path.suffix.lstrip(".")

        chunks = []
        current_start = 0
        chunk_index = 0

        # Process text using fixed-size algorithm
        while current_start < len(text):
            # Calculate chunk boundaries
            chunk_end = current_start + self.chunk_size

            # Extract chunk text
            if chunk_end >= len(text):
                # Last chunk - take remaining text
                chunk_text = text[current_start:]
            else:
                # Regular chunk - exactly chunk_size characters
                chunk_text = text[current_start:chunk_end]

            # Calculate line numbers for this chunk
            line_start, line_end = self._calculate_line_numbers(
                text, current_start, current_start + len(chunk_text)
            )

            # Extract images from supported file types (.md, .html, .htm, .htmx)
            images = []
            if file_path and repo_root:
                images = self._extract_images(chunk_text, file_path, repo_root)

            # Create chunk metadata
            chunk = {
                "text": chunk_text,
                "chunk_index": chunk_index,
                "total_chunks": 0,  # Will be updated after processing all chunks
                "size": len(chunk_text),
                "file_path": str(file_path) if file_path else None,
                "file_extension": file_extension,
                "line_start": line_start,
                "line_end": line_end,
                "images": images,  # Populated for markdown files with image references
            }
            chunks.append(chunk)

            # If this chunk contains all remaining text, we're done
            if chunk_end >= len(text):
                break

            # Move to next chunk start position
            # Pattern: next_start = current_start + step_size
            current_start += self.step_size
            chunk_index += 1

        # Update total_chunks in all chunk metadata
        total_chunks = len(chunks)
        for chunk in chunks:
            chunk["total_chunks"] = total_chunks

        return chunks

    def chunk_file(self, file_path: Path, repo_root: Optional[Path] = None) -> List[Dict[str, Any]]:
        """Read and chunk a file using fixed-size algorithm.

        Args:
            file_path: Path to file to chunk
            repo_root: Repository root directory (for image path resolution and validation)

        Returns:
            List of chunk dictionaries with metadata

        Raises:
            ValueError: If file cannot be read or processed
        """
        try:
            return self._chunk_file_standard(file_path, repo_root)
        except Exception as e:
            raise ValueError(f"Failed to process file {file_path}: {e}")

    def _chunk_file_standard(self, file_path: Path, repo_root: Optional[Path] = None) -> List[Dict[str, Any]]:
        """Standard file chunking - reads entire file into memory."""
        # Try different encodings
        encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
        text = None

        for encoding in encodings:
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    text = f.read()
                break
            except UnicodeDecodeError:
                continue

        if text is None:
            raise ValueError(f"Could not decode file {file_path}")

        return self.chunk_text(text, file_path, repo_root)

    def estimate_chunks(self, text: str) -> int:
        """Estimate number of chunks for given text using fixed-size algorithm.

        Args:
            text: Text to estimate chunks for

        Returns:
            Estimated number of chunks
        """
        if not text:
            return 0

        # Simple arithmetic estimation
        # First chunk: chunk_size characters
        # Each additional chunk: step_size new characters (accounting for overlap)
        if len(text) <= self.chunk_size:
            return 1

        remaining_after_first = len(text) - self.chunk_size
        additional_chunks = (
            remaining_after_first + self.step_size - 1
        ) // self.step_size  # Ceiling division
        return 1 + additional_chunks
