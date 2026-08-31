"""VoyageAI Multimodal API client for embeddings generation."""

import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Union, cast
import httpx
from rich.console import Console

from ..config import VoyageAIConfig
from .multimodal_utils import encode_image_to_base64
from .voyage_ai import SyncClientFactory


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

    def __init__(
        self,
        config: VoyageAIConfig,
        console: Optional[Console] = None,
        http_client_factory: Optional[SyncClientFactory] = None,
    ):
        """Initialize VoyageMultimodalClient.

        Args:
            config: VoyageAI configuration (model, endpoint, timeouts, retries)
            console: Optional Rich console for logging
            http_client_factory: An object satisfying the SyncClientFactory
                Protocol (typically HttpClientFactory or NullFaultFactory),
                mirroring VoyageAIClient's own constructor. Normalized to
                NullFaultFactory() when omitted so call sites never need an
                if-None branch.

        Raises:
            ValueError: If VOYAGE_API_KEY environment variable is not set
        """
        self.config = config
        self.console = console or Console()

        if http_client_factory is None:
            from code_indexer.server.fault_injection.null_factory import (
                NullFaultFactory,
            )

            http_client_factory = NullFaultFactory()
        self._http_client_factory: SyncClientFactory = http_client_factory

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
            image_data_url = encode_image_to_base64(image_path)
            content.append({"type": "image_base64", "image_base64": image_data_url})

        # Build API request payload
        payload: Dict[str, Any] = {
            "inputs": [{"content": content}],
            "model": self.config.model,
        }

        if input_type is not None:
            payload["input_type"] = input_type

        def _do_post_and_validate() -> httpx.Response:
            """The smallest unit including BOTH the network call and its
            status validation -- a vendor 4xx/5xx here must be recorded as
            success=False, never success=True (Story #1418)."""
            with self._http_client_factory.create_sync_client(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.config.timeout,
            ) as client:
                _response = client.post(self.config.api_endpoint, json=payload)
                _response.raise_for_status()
            return cast(httpx.Response, _response)

        def _count_single_text_tokens() -> int:
            from .embedded_voyage_tokenizer import VoyageTokenizer

            return VoyageTokenizer.count_tokens([text], self.config.model)

        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )
        from code_indexer.services.embedding_metrics_telemetry import (
            time_and_record_embedding_call,
        )

        # Story #1586 AC2: cidx.embedding.* OTEL metrics -- no internal
        # retry loop here, so wrapping this method boundary is per-attempt.
        response = time_and_record_embedding_call(
            model=self.config.model,
            count_tokens=_count_single_text_tokens,
            call_fn=lambda: instrument_call(
                provider="voyageai",
                call_type="embed_multimodal",
                model=self.config.model,
                item_count=1,
                token_count=0,
                batch_size=1,
                purpose="query" if input_type == "query" else "index",
                fn=_do_post_and_validate,
            ),
        )

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

    def get_embeddings_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
        retry: bool = True,
    ) -> List[List[float]]:
        """Standard EmbeddingProvider batch contract (Bug #1480 follow-up).

        The server-side embedding path (EmbeddingCoalescer) calls this method;
        without it a server-side multimodal query raised AttributeError and
        zeroed the whole result set. Embeds TEXT-ONLY queries (no images) in
        the multimodal vector space by delegating to
        get_multimodal_embeddings_batch, so the returned vectors match this
        client's multimodal collection dimension (voyage-multimodal-3 = 1024).

        ``model`` and ``retry`` are accepted for signature-compatibility with
        the base contract; the multimodal batch path manages its own model and
        retries.
        """
        if not texts:
            return []
        input_type = "query" if embedding_purpose == "query" else "document"
        items: List[Dict[str, Any]] = [{"text": t, "image_paths": []} for t in texts]
        return self.get_multimodal_embeddings_batch(items, input_type=input_type)

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
                image_data_url = encode_image_to_base64(image_path)
                content.append({"type": "image_base64", "image_base64": image_data_url})

            inputs.append({"content": content})

        # Build API request payload
        payload: Dict[str, Any] = {
            "inputs": inputs,
            "model": self.config.model,
        }

        if input_type is not None:
            payload["input_type"] = input_type

        def _do_post_and_validate() -> httpx.Response:
            """The smallest unit including BOTH the network call and its
            status validation -- a vendor 4xx/5xx here must be recorded as
            success=False, never success=True (Story #1418)."""
            with self._http_client_factory.create_sync_client(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.config.timeout,
            ) as client:
                _response = client.post(self.config.api_endpoint, json=payload)
                _response.raise_for_status()
            return cast(httpx.Response, _response)

        from code_indexer.server.services.embedding_call_instrumentation import (
            instrument_call,
        )
        from code_indexer.services.embedding_metrics_telemetry import (
            time_and_record_embedding_call,
        )

        # Story #1586 AC2: cidx.embedding.* OTEL metrics -- no internal
        # retry loop here, so wrapping this method boundary is per-attempt.
        response = time_and_record_embedding_call(
            model=self.config.model,
            count_tokens=lambda: sum(
                self._count_tokens_accurately(item["text"]) for item in items
            ),
            call_fn=lambda: instrument_call(
                provider="voyageai",
                call_type="embed_multimodal",
                model=self.config.model,
                item_count=len(items),
                token_count=0,
                batch_size=len(items),
                purpose="query" if input_type == "query" else "index",
                fn=_do_post_and_validate,
            ),
        )

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

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        """Generate text-only embedding for query purposes.

        This method enables VoyageMultimodalClient to be used as a standard
        embedding provider compatible with vector_store.search().

        Uses the multimodal API with text-only input and input_type="query"
        to generate embeddings in the same vector space as documents indexed
        with voyage-multimodal-3.

        Args:
            text: Query text to embed
            model: Accepted for EmbeddingProvider contract compliance — ignored
                (multimodal client uses the model it was initialized with)
            embedding_purpose: Accepted for EmbeddingProvider contract compliance —
                ignored (multimodal client always uses input_type="query")

        Returns:
            1024-dimensional embedding vector as list of floats
        """
        return self.get_multimodal_embedding(
            text=text, image_paths=[], input_type="query"
        )

    def get_provider_name(self) -> str:
        """Get the name of this embedding provider.

        Returns "voyage-ai" to match VoyageAIClient.get_provider_name() and satisfy
        the EmbeddingProvider contract called by filesystem_vector_store.py.

        The consumer (_write_embed_meta_to_event_ctx) checks whether the name contains
        "cohere" to route telemetry; anything else is treated as voyage.  Returning
        "voyage-ai" correctly routes multimodal telemetry to the voyage branch.
        """
        return "voyage-ai"

    def get_current_model(self) -> str:
        """Get the current active model name.

        Bug #1480 remediation: required by the server-side
        ``QueryEmbeddingCache.qualifier()`` contract (see
        ``server/services/query_embedding_cache.py``), which every embedding
        provider driven through ``governed_call.coalesced_query_embedding``
        must satisfy once the query-embedding cache is enabled. Mirrors
        ``VoyageAIClient.get_current_model()`` exactly.
        """
        return str(self.config.model)

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model.

        Bug #1480 remediation: required by the server-side
        ``QueryEmbeddingCache.qualifier()`` contract, which reads the
        ``"dimensions"`` key. Reuses the SAME ``_VOYAGE_MODEL_DIMENSIONS``
        table ``VoyageAIClient.get_model_info()`` uses -- never a fabricated
        value. Unlike the text client, this raises loudly (no silent
        default) when the configured model has no known dimension, since a
        wrong/undeclared dimension here would corrupt the cache qualifier.

        Raises:
            ValueError: If ``self.config.model`` has no entry in
                ``_VOYAGE_MODEL_DIMENSIONS``.
        """
        from .voyage_ai import _VOYAGE_MODEL_DIMENSIONS

        model_name = self.config.model
        dimensions = _VOYAGE_MODEL_DIMENSIONS.get(model_name)
        if dimensions is None:
            raise ValueError(
                f"Unknown VoyageAI multimodal model '{model_name}': no "
                "dimension entry in _VOYAGE_MODEL_DIMENSIONS (voyage_ai.py)."
            )

        return {
            "name": model_name,
            "provider": "voyage-ai",
            "dimensions": dimensions,
            "max_tokens": self._get_model_token_limit(),
            "supports_batch": True,
            "api_endpoint": self.config.api_endpoint,
        }
