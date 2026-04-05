"""Cohere Multimodal API client for embeddings generation.

Story #637: Implements multimodal (text + image) embedding support for Cohere
embed-v4.0-multimodal model using the /v2/embed endpoint.

Key differences from VoyageMultimodalClient:
- Content block format: {"type": "image_url", "image_url": {"url": data_url}}
- Response format: response["embeddings"]["float"] (not response["data"][0]["embedding"])
- Dual-constraint batch splitting: token limit AND 96-image cap
- 5MB per-image size enforcement
- output_dimension parameter for configurable embedding size
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# Maximum image size: 5MB per Cohere API documentation
COHERE_MAX_IMAGE_SIZE = 5 * 1024 * 1024

# Maximum images per request per Cohere API documentation
MAX_IMAGES_PER_REQUEST = 96

# Token limit for embed-v4.0-multimodal
COHERE_MULTIMODAL_TOKEN_LIMIT = 128000

# Safety margin for token limit (90%)
_SAFETY_MARGIN = 0.9

# Maximum sleep duration for any retry path to prevent indefinite blocking
_MAX_RETRY_SLEEP_SECONDS = 300.0


class CohereMultimodalClient:
    """Client for Cohere embed-v4.0-multimodal API.

    Supports generating embeddings from text + images using Cohere's v2 embed
    endpoint with content blocks format.

    Features:
    - Text + image embedding generation using content blocks
    - Dual-constraint batch splitting (token limit + 96-image cap)
    - 5MB image size enforcement (skip with warning)
    - Configurable output dimensions (256, 512, 1024, 1536)
    - Retry logic with exponential backoff
    - Rate limit handling
    - Health monitor probe registration
    """

    def __init__(self, config: Any, console: Optional[Any] = None):
        """Initialize CohereMultimodalClient.

        Args:
            config: CohereConfig object with api_key, model, api_endpoint,
                    default_dimension, max_retries, retry_delay, timeout attrs.
            console: Optional Rich console for logging.

        Raises:
            ValueError: If no API key is available from config or environment.
        """
        self.config = config

        # API key: config first, then CO_API_KEY env var
        self.api_key = config.api_key or os.getenv("CO_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Cohere API key required. Set via config or CO_API_KEY env var."
            )

        # Register lightweight connectivity probe (defensive pattern from cohere_embedding.py)
        try:
            from code_indexer.services.provider_health_monitor import (
                ProviderHealthMonitor,
            )
        except ImportError:
            logger.debug(
                "Health monitor unavailable; skipping probe registration for cohere-multimodal."
            )
        else:
            try:
                ProviderHealthMonitor.get_instance().register_probe(
                    "cohere-multimodal", self._health_probe
                )
            except Exception as exc:
                logger.debug(
                    "Probe registration failed for cohere-multimodal (non-fatal): %s",
                    exc,
                )

    @property
    def collection_name(self) -> str:
        """Collection name for storing multimodal embeddings.

        Decoupled from config.model because Cohere's API model is 'embed-v4.0'
        but the collection is named 'embed-v4.0-multimodal' to separate
        multimodal vectors from text-only vectors.
        """
        from code_indexer.config import COHERE_MULTIMODAL_MODEL

        return str(COHERE_MULTIMODAL_MODEL)

    def _map_input_type(self, input_type: Optional[str]) -> str:
        """Map input type string to Cohere API input_type parameter.

        Args:
            input_type: Internal type string ("query", "document", None, or other).

        Returns:
            Cohere API input_type: "search_query" or "search_document".
        """
        if input_type == "query":
            return "search_query"
        return "search_document"

    def _count_tokens(self, text: str) -> int:
        """Count tokens using the embedded Cohere tokenizer (lazy import).

        Args:
            text: Text to count tokens for.

        Returns:
            Number of tokens in the text.
        """
        from code_indexer.services.embedded_cohere_tokenizer import count_tokens_single

        return int(count_tokens_single(text, model=self.config.model))

    def _check_image_size(self, image_path: Path) -> bool:
        """Check if image is within the 5MB size limit.

        Args:
            image_path: Path to image file.

        Returns:
            True if image is within size limit, False if oversized.
        """
        size = os.path.getsize(str(image_path))
        if size > COHERE_MAX_IMAGE_SIZE:
            logger.warning(
                "Image %s exceeds 5MB size limit (%d bytes), skipping image embedding",
                image_path,
                size,
            )
            return False
        return True

    def _build_content_blocks(
        self, text: str, image_paths: Sequence[Union[Path, str]]
    ) -> List[Dict[str, Any]]:
        """Build content blocks for the Cohere API request.

        Skips images that exceed 5MB size limit (with warning).

        Args:
            text: Text content.
            image_paths: List of image file paths.

        Returns:
            List of content block dicts with 'type' key.
        """
        from .multimodal_utils import encode_image_to_base64

        content: List[Dict[str, Any]] = [{"type": "text", "text": text}]

        for image_path in image_paths:
            image_path = Path(image_path)
            if not self._check_image_size(image_path):
                continue
            data_url = encode_image_to_base64(image_path)
            content.append({"type": "image_url", "image_url": {"url": data_url}})

        return content

    def _make_request(
        self,
        inputs: List[Dict[str, Any]],
        input_type: str = "search_document",
    ) -> Dict[str, Any]:
        """Make HTTP request to Cohere embed v2 API with retry logic.

        Args:
            inputs: List of input dicts with 'content' key (content blocks).
            input_type: Cohere input_type parameter.

        Returns:
            Parsed JSON response from the API.

        Raises:
            ValueError: If API key is invalid (401 Unauthorized).
            RuntimeError: If all retry attempts are exhausted.
        """
        import httpx
        from http import HTTPStatus

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "inputs": inputs,
            "model": self.config.model,
            "input_type": input_type,
            "embedding_types": ["float"],
            "output_dimension": self.config.default_dimension,
        }

        max_retries = getattr(self.config, "max_retries", 3)
        retry_delay = getattr(self.config, "retry_delay", 1.0)
        exponential_backoff = getattr(self.config, "exponential_backoff", True)
        timeout = getattr(self.config, "timeout", 30)
        connect_timeout = getattr(self.config, "connect_timeout", 5)
        api_endpoint = getattr(
            self.config, "api_endpoint", "https://api.cohere.com/v2/embed"
        )

        max_attempts = max_retries + 1
        last_error: Optional[Exception] = None
        _start = time.time()

        for attempt in range(max_attempts):
            try:
                _start = time.time()
                with httpx.Client(
                    timeout=httpx.Timeout(
                        connect=connect_timeout,
                        read=timeout,
                        write=timeout,
                        pool=timeout,
                    )
                ) as client:
                    response = client.post(
                        api_endpoint,
                        headers=headers,
                        json=payload,
                    )

                if response.status_code == 429:
                    retry_after = float(
                        response.headers.get("retry-after", retry_delay)
                    )
                    capped_delay = min(retry_after, _MAX_RETRY_SLEEP_SECONDS)
                    logger.warning(
                        "Cohere multimodal rate limited (attempt %d/%d), retrying after %.1fs",
                        attempt + 1,
                        max_attempts,
                        capped_delay,
                    )
                    time.sleep(capped_delay)
                    continue

                if response.status_code >= 500:
                    delay = retry_delay * (2**attempt if exponential_backoff else 1)
                    capped_delay = min(delay, _MAX_RETRY_SLEEP_SECONDS)
                    logger.warning(
                        "Cohere multimodal server error %d (attempt %d/%d), retrying after %.1fs",
                        response.status_code,
                        attempt + 1,
                        max_attempts,
                        capped_delay,
                    )
                    time.sleep(capped_delay)
                    continue

                response.raise_for_status()
                return dict(response.json())

            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    delay = retry_delay * (2**attempt if exponential_backoff else 1)
                    capped_delay = min(delay, _MAX_RETRY_SLEEP_SECONDS)
                    logger.warning(
                        "Cohere multimodal request failed (attempt %d/%d): %s, retrying after %.1fs",
                        attempt + 1,
                        max_attempts,
                        exc,
                        capped_delay,
                    )
                    time.sleep(capped_delay)
                    continue
                break

        if last_error is not None:
            response_obj = getattr(last_error, "response", None)
            if (
                response_obj is not None
                and getattr(response_obj, "status_code", None)
                == HTTPStatus.UNAUTHORIZED
            ):
                raise ValueError(
                    "Invalid Cohere API key. Check CO_API_KEY environment variable."
                )
        raise RuntimeError(
            f"Cohere multimodal API request failed after {max_attempts} attempts: {last_error}"
        )

    def _parse_response(self, response: Dict[str, Any]) -> List[List[float]]:
        """Parse Cohere embed v2 response to extract float embeddings.

        Args:
            response: Parsed JSON response from API.

        Returns:
            List of embedding vectors.

        Raises:
            ValueError: If response format is unexpected.
        """
        if "embeddings" not in response or "float" not in response["embeddings"]:
            raise ValueError(
                f"Unexpected Cohere response format: missing embeddings.float. "
                f"Got keys: {list(response.keys())}"
            )
        embeddings = response["embeddings"]["float"]
        if not isinstance(embeddings, list):
            raise ValueError(
                f"Unexpected embeddings format: expected list, got {type(embeddings)}"
            )
        return embeddings

    def get_multimodal_embedding(
        self,
        text: str,
        image_paths: List[Union[Path, str]],
        input_type: Optional[str] = None,
    ) -> List[float]:
        """Generate multimodal embedding for text and images.

        Args:
            text: Text content to embed.
            image_paths: List of paths to image files (PNG, JPEG, WebP, GIF).
            input_type: Optional input type ("query", "document", or None).

        Returns:
            Embedding vector as list of floats (dimension from config.default_dimension).

        Raises:
            ValueError: If API key is invalid or response format is unexpected.
            RuntimeError: If API call fails after retries.
            FileNotFoundError: If any image file doesn't exist.
        """
        cohere_input_type = self._map_input_type(input_type)
        content = self._build_content_blocks(text, image_paths)
        inputs = [{"content": content}]

        response = self._make_request(inputs, cohere_input_type)
        embeddings = self._parse_response(response)

        if not embeddings:
            raise ValueError("No embedding returned in Cohere response")

        return embeddings[0]

    def get_multimodal_embeddings_batch(
        self,
        items: List[Dict[str, Any]],
        input_type: Optional[str] = None,
    ) -> List[List[float]]:
        """Generate multimodal embeddings for a batch of items.

        Uses dual-constraint batch splitting: token limit AND 96-image cap.
        The safety limit is 90% of COHERE_MULTIMODAL_TOKEN_LIMIT (128000).

        Args:
            items: List of dicts with 'text' key and optional 'image_paths' key.
            input_type: Optional input type ("query", "document", or None).

        Returns:
            List of embedding vectors in the same order as input items.

        Raises:
            ValueError: If items are missing required 'text' key.
        """
        if not items:
            return []

        cohere_input_type = self._map_input_type(input_type)
        safety_limit = int(COHERE_MULTIMODAL_TOKEN_LIMIT * _SAFETY_MARGIN)

        all_embeddings: List[List[float]] = []
        current_batch_inputs: List[Dict[str, Any]] = []
        current_tokens = 0
        current_image_count = 0

        for item in items:
            if "text" not in item:
                raise ValueError(f"Item missing required 'text' key: {item}")

            text = item["text"]
            image_paths = [Path(p) for p in item.get("image_paths", [])]

            # Count images that will actually be included (passing size check)
            item_image_count = sum(1 for p in image_paths if self._check_image_size(p))
            item_tokens = self._count_tokens(text)

            # Check dual constraint: token limit OR image cap
            if current_batch_inputs and (
                current_tokens + item_tokens > safety_limit
                or current_image_count + item_image_count > MAX_IMAGES_PER_REQUEST
            ):
                # Submit current batch
                response = self._make_request(current_batch_inputs, cohere_input_type)
                embeddings = self._parse_response(response)
                all_embeddings.extend(embeddings)
                current_batch_inputs = []
                current_tokens = 0
                current_image_count = 0

            content = self._build_content_blocks(text, image_paths)
            current_batch_inputs.append({"content": content})
            current_tokens += item_tokens
            current_image_count += item_image_count

        # Submit final batch
        if current_batch_inputs:
            response = self._make_request(current_batch_inputs, cohere_input_type)
            embeddings = self._parse_response(response)
            all_embeddings.extend(embeddings)

        return all_embeddings

    def get_embedding(self, text: str, **kwargs) -> List[float]:
        """Generate text-only embedding for query purposes.

        Uses input_type="search_query" to match the query vector space.
        Accepts additional kwargs (e.g., embedding_purpose) for compatibility
        with the vector store search interface.

        Args:
            text: Query text to embed.
            **kwargs: Additional keyword arguments (ignored, for interface compat).

        Returns:
            Embedding vector as list of floats.
        """
        return self.get_multimodal_embedding(
            text=text, image_paths=[], input_type="query"
        )

    def _health_probe(self) -> bool:
        """Lightweight connectivity probe for health monitoring.

        Makes an OPTIONS request to the API endpoint. Returns True if reachable.
        """
        import httpx
        from http import HTTPStatus

        try:
            with httpx.Client(
                timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
            ) as client:
                api_endpoint = getattr(
                    self.config, "api_endpoint", "https://api.cohere.com/v2/embed"
                )
                response = client.options(api_endpoint)
                return bool(response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR)
        except httpx.HTTPError as exc:
            logger.debug("Cohere multimodal health probe HTTP error: %s", exc)
            return False
        except Exception as exc:
            logger.debug("Cohere multimodal health probe failed: %s", exc)
            return False
