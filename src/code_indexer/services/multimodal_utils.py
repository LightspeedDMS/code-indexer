"""Shared utilities for multimodal embedding support.

Provides common image encoding functions used by both VoyageMultimodalClient
and CohereMultimodalClient to avoid code duplication.
"""

import base64
from pathlib import Path
from typing import Union

# Supported image media types, keyed by lowercase file extension
SUPPORTED_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def encode_image_to_base64(image_path: Union[Path, str]) -> str:
    """Encode image file to base64 data URL.

    Args:
        image_path: Path to image file (PNG, JPEG, WebP, GIF)

    Returns:
        Base64-encoded data URL with proper media type.
        Format: data:image/[mediatype];base64,[encoded-data]

    Raises:
        FileNotFoundError: If image file does not exist.
        ValueError: If image format is not supported.
    """
    image_path = Path(image_path)

    if not image_path.exists():
        raise FileNotFoundError(f"Image file not found: {image_path}")

    extension = image_path.suffix.lower()
    if extension not in SUPPORTED_MEDIA_TYPES:
        raise ValueError(
            f"Unsupported image format: {extension}. "
            f"Supported formats: PNG, JPEG, WebP, GIF"
        )

    media_type = SUPPORTED_MEDIA_TYPES[extension]

    with open(image_path, "rb") as f:
        image_data = f.read()

    encoded_data = base64.b64encode(image_data).decode("utf-8")
    return f"data:{media_type};base64,{encoded_data}"
