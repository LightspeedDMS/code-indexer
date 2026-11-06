"""Shared utilities for multimodal embedding support.

Provides common image encoding functions used by both VoyageMultimodalClient
and CohereMultimodalClient to avoid code duplication.
"""

import base64
import os
from pathlib import Path
from typing import Optional, Protocol, Union

# Supported image media types, keyed by lowercase file extension
SUPPORTED_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ImageSource(Protocol):
    """Where an indexing pass gets image bytes when they are not on this disk.

    None everywhere means "this container's disk", exactly as before. A source
    is passed only on a pass whose content backend is delegated: the repository
    is not mounted, so an image's existence, size and bytes come from the
    content seam instead of ``exists``/``stat``/``open``.
    """

    def has(self, image_path: Path) -> bool:
        """Whether the repository holds this path (the ``exists()`` stand-in)."""
        ...

    def size(self, image_path: Path) -> int:
        """The file's size in bytes (the ``getsize`` stand-in)."""
        ...

    def read(self, image_path: Path) -> bytes:
        """The file's bytes. Raises FileNotFoundError when it is not there."""
        ...


def image_size(
    image_path: Union[Path, str], image_source: Optional[ImageSource]
) -> int:
    """An image's size: from ``image_source`` when given, else ``os.path.getsize``."""
    if image_source is None:
        return os.path.getsize(str(image_path))
    return image_source.size(Path(image_path))


def encode_image_to_base64(
    image_path: Union[Path, str], image_source: Optional[ImageSource] = None
) -> str:
    """Encode image file to base64 data URL.

    Args:
        image_path: Path to image file (PNG, JPEG, WebP, GIF)
        image_source: Where the bytes come from. None (the default) reads this
            container's disk, exactly as before. A source is consulted only
            AFTER the format check, so an unsupported reference never costs a
            fetch.

    Returns:
        Base64-encoded data URL with proper media type.
        Format: data:image/[mediatype];base64,[encoded-data]

    Raises:
        FileNotFoundError: If image file does not exist.
        ValueError: If image format is not supported.
    """
    image_path = Path(image_path)

    if image_source is None and not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    extension = image_path.suffix.lower()
    if extension not in SUPPORTED_MEDIA_TYPES:
        raise ValueError(
            f"Unsupported image format: {extension}. "
            f"Supported formats: PNG, JPEG, WebP, GIF"
        )

    media_type = SUPPORTED_MEDIA_TYPES[extension]

    if image_source is None:
        with open(image_path, "rb") as f:
            image_data = f.read()
    else:
        image_data = image_source.read(image_path)

    encoded_data = base64.b64encode(image_data).decode("utf-8")
    return f"data:{media_type};base64,{encoded_data}"
