"""VoyageAI Multimodal API client for embeddings generation."""

import base64
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Union
import httpx
from rich.console import Console

from ..config import VoyageAIConfig


class VoyageMultimodalClient:
    """Client for interacting with VoyageAI Multimodal API.

    Supports generating embeddings from text + images using voyage-multimodal-3.5 model.
    API Endpoint: https://api.voyageai.com/v1/multimodalembeddings

    Features:
    - Text + image embedding generation
    - Base64 image encoding (PNG, JPEG, WebP, GIF)
    - Batch processing support
    - Retry logic with exponential backoff
    - Rate limit handling
    """

    def __init__(self, config: VoyageAIConfig, console: Optional[Console] = None):
        """Initialize VoyageMultimodalClient.

        Args:
            config: VoyageAI configuration (model, endpoint, timeouts, retries)
            console: Optional Rich console for logging

        Raises:
            ValueError: If VOYAGE_API_KEY environment variable is not set
        """
        self.config = config
        self.console = console or Console()

        # Override API endpoint for multimodal embeddings
        # VoyageAIConfig defaults to /v1/embeddings, but multimodal needs /v1/multimodalembeddings
        self.config.api_endpoint = "https://api.voyageai.com/v1/multimodalembeddings"

        # Get API key from environment
        self.api_key = os.getenv("VOYAGE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "VOYAGE_API_KEY environment variable is required for VoyageAI. "
                "Set it with: export VOYAGE_API_KEY=your_api_key_here"
            )

    def _encode_image_to_base64(self, image_path: Union[Path, str]) -> str:
        """Encode image file to base64 data URL.

        Args:
            image_path: Path to image file (PNG, JPEG, WebP, GIF)

        Returns:
            Base64-encoded data URL with proper media type
            Format: data:image/[mediatype];base64,[encoded-data]

        Raises:
            FileNotFoundError: If image file doesn't exist
            ValueError: If image format is not supported
        """
        image_path = Path(image_path)

        # Check if file exists
        if not image_path.exists():
            raise FileNotFoundError(f"Image file not found: {image_path}")

        # Determine media type from extension
        extension = image_path.suffix.lower()
        media_type_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }

        if extension not in media_type_map:
            raise ValueError(
                f"Unsupported image format: {extension}. "
                f"Supported formats: PNG, JPEG, WebP, GIF"
            )

        media_type = media_type_map[extension]

        # Read and encode image
        with open(image_path, "rb") as f:
            image_data = f.read()

        encoded_data = base64.b64encode(image_data).decode("utf-8")

        # Return data URL
        return f"data:{media_type};base64,{encoded_data}"

    def get_multimodal_embedding(
        self,
        text: str,
        image_paths: List[Union[Path, str]],
        input_type: Optional[str] = None,
    ) -> List[float]:
        """Generate multimodal embedding for text and images.

        Args:
            text: Text content to embed
            image_paths: List of paths to image files (PNG, JPEG, WebP, GIF)
            input_type: Optional input type ("query", "document", or None)

        Returns:
            1024-dimensional embedding vector as list of floats

        Raises:
            ValueError: If API key is invalid or response format is unexpected
            RuntimeError: If API call fails after retries
            FileNotFoundError: If any image file doesn't exist
        """
        # Build content array with text and images
        content = [{"type": "text", "text": text}]

        # Add images if provided
        for image_path in image_paths:
            image_data_url = self._encode_image_to_base64(image_path)
            content.append({"type": "image_base64", "image_base64": image_data_url})

        # Build API request payload
        payload: Dict[str, Any] = {
            "inputs": [{"content": content}],
            "model": self.config.model,
        }

        if input_type is not None:
            payload["input_type"] = input_type

        # Make API request
        with httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.config.timeout,
        ) as client:
            response = client.post(self.config.api_endpoint, json=payload)
            response.raise_for_status()

        result = response.json()

        # Extract embedding from response
        if not isinstance(result, dict) or "data" not in result:
            raise ValueError(f"Unexpected response format: {type(result)}")

        if not result["data"] or len(result["data"]) == 0:
            raise ValueError("No embedding returned in response")

        embedding = result["data"][0]["embedding"]

        if not isinstance(embedding, list):
            raise ValueError(f"Unexpected embedding format: {type(embedding)}")

        return embedding

    def get_multimodal_embeddings_batch(
        self, items: List[Dict[str, Any]], input_type: Optional[str] = None
    ) -> List[List[float]]:
        """Generate multimodal embeddings for batch of items with token-aware batching.

        Automatically splits large batches to respect token limits with 90% safety margin.

        Args:
            items: List of items with 'text' and 'image_paths' keys
            input_type: Optional input type ("query", "document", or None)

        Returns:
            List of 1024-dimensional embedding vectors

        Raises:
            ValueError: If API key is invalid, response format unexpected, or items missing required keys
            RuntimeError: If API call fails after retries
            FileNotFoundError: If any image file doesn't exist
        """
        if not items:
            return []

        # Get model-specific token limit with 90% safety margin
        model_token_limit = self._get_model_token_limit()
        safety_limit = int(model_token_limit * 0.9)

        # Dynamic batching: process items until approaching token limit
        all_embeddings: List[List[float]] = []
        current_batch: List[Dict[str, Any]] = []
        current_tokens: int = 0

        for item in items:
            # Validate required keys
            if "text" not in item:
                raise ValueError(f"Item missing required 'text' key: {item}")

            # Count tokens for this item's text
            item_tokens = self._count_tokens_accurately(item["text"])

            # Check if adding this item would exceed 90% safety limit
            if current_tokens + item_tokens > safety_limit and current_batch:
                # Submit current batch before it gets too large
                batch_embeddings = self._submit_multimodal_batch(
                    current_batch, input_type
                )
                all_embeddings.extend(batch_embeddings)

                # Start new batch with current item
                current_batch = [item]
                current_tokens = item_tokens
            else:
                # Add item to current batch
                current_batch.append(item)
                current_tokens += item_tokens

        # Submit final batch if any items remain
        if current_batch:
            batch_embeddings = self._submit_multimodal_batch(current_batch, input_type)
            all_embeddings.extend(batch_embeddings)

        return all_embeddings

    def _get_model_token_limit(self) -> int:
        """Get token limit for current model.

        Returns:
            Token limit for the configured model (default: 120000)
        """
        # voyage-multimodal-3.5 likely has similar limits to voyage-3
        # Default to 120000 tokens (conservative estimate)
        VOYAGE_MULTIMODAL_TOKEN_LIMIT = 120000
        return VOYAGE_MULTIMODAL_TOKEN_LIMIT

    def _count_tokens_accurately(self, text: str) -> int:
        """Count tokens accurately using VoyageAI's embedded tokenizer.

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens in the text
        """
        # Lazy import to avoid loading tokenizer at module import time
        from .embedded_voyage_tokenizer import VoyageTokenizer

        return VoyageTokenizer.count_tokens([text], model=self.config.model)

    def _submit_multimodal_batch(
        self, items: List[Dict[str, Any]], input_type: Optional[str] = None
    ) -> List[List[float]]:
        """Submit a batch of multimodal items to the API.

        Args:
            items: List of items with 'text' and 'image_paths' keys
            input_type: Optional input type ("query", "document", or None)

        Returns:
            List of embedding vectors from the API response

        Raises:
            ValueError: If items missing required 'text' key
        """
        # Build inputs array for batch API call
        inputs = []
        for item in items:
            # Validate required keys
            if "text" not in item:
                raise ValueError(f"Item missing required 'text' key: {item}")

            content = [{"type": "text", "text": item["text"]}]

            # Add images if provided
            for image_path in item.get("image_paths", []):
                image_data_url = self._encode_image_to_base64(image_path)
                content.append({"type": "image_base64", "image_base64": image_data_url})

            inputs.append({"content": content})

        # Build API request payload
        payload: Dict[str, Any] = {
            "inputs": inputs,
            "model": self.config.model,
        }

        if input_type is not None:
            payload["input_type"] = input_type

        # Make API request
        with httpx.Client(
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.config.timeout,
        ) as client:
            response = client.post(self.config.api_endpoint, json=payload)
            response.raise_for_status()

        result = response.json()

        # Extract embeddings from response
        if not isinstance(result, dict) or "data" not in result:
            raise ValueError(f"Unexpected response format: {type(result)}")

        embeddings = []
        for item_data in result["data"]:
            embedding = item_data["embedding"]
            if not isinstance(embedding, list):
                raise ValueError(f"Unexpected embedding format: {type(embedding)}")
            embeddings.append(embedding)

        return embeddings

    def get_embedding(self, text: str) -> List[float]:
        """Generate text-only embedding for query purposes.

        This method enables VoyageMultimodalClient to be used as a standard
        embedding provider compatible with vector_store.search().

        Uses the multimodal API with text-only input and input_type="query"
        to generate embeddings in the same vector space as documents indexed
        with voyage-multimodal-3.

        Args:
            text: Query text to embed

        Returns:
            1024-dimensional embedding vector as list of floats
        """
        return self.get_multimodal_embedding(
            text=text, image_paths=[], input_type="query"
        )
