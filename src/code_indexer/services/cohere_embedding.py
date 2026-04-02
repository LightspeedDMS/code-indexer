"""Cohere Embed v4 provider for CIDX.

Story #486: Implements EmbeddingProvider ABC for Cohere.
All imports lazy (no module-level imports of cohere SDK).
"""

import logging
import os
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console

from code_indexer.services.embedding_provider import EmbeddingProvider

logger = logging.getLogger(__name__)

# Number of embedding values shown in error messages when validating None values
_EMBED_PREVIEW_LEN = 10

# Maximum sleep duration for any retry path to prevent indefinite thread blocking (#602)
_MAX_RETRY_SLEEP_SECONDS = 300.0


class CohereEmbeddingProvider(EmbeddingProvider):
    """Cohere Embed v4 embedding provider."""

    def __init__(self, config: Any, console: Optional[Console] = None):
        """Initialize with CohereConfig.

        Args:
            config: Configuration object with api_key, model, api_endpoint,
                    max_retries, retry_delay, timeout attributes.
            console: Optional Rich console for output.

        Raises:
            ValueError: If no API key is available from config or environment.
        """
        super().__init__(console)
        self.config = config
        self.console = console or Console()

        # API key: config first, then env var
        self.api_key = config.api_key or os.getenv("CO_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Cohere API key required. Set via config or CO_API_KEY env var."
            )

        self._load_model_specs()

    def _load_model_specs(self) -> None:
        """Load model specifications from cohere_models.yaml.

        Falls back to hardcoded embed-v4.0 spec if YAML is missing or unreadable.
        """
        import yaml

        try:
            module_dir = Path(__file__).parent.parent
            yaml_path = module_dir / "data" / "cohere_models.yaml"
            with open(yaml_path) as f:
                self.model_specs = yaml.safe_load(f)
        except Exception as exc:
            logger.warning(
                "Failed to load cohere_models.yaml (%s), using hardcoded fallback for embed-v4.0",
                exc,
            )
            self.model_specs = {
                "cohere_models": {
                    "embed-v4.0": {
                        "default_dimension": 1536,
                        "dimensions": [256, 512, 1024, 1536],
                        "token_limit": 128000,
                        "texts_per_request": 96,
                    }
                },
                "api_constraints": {"safety_margin_percentage": 90},
            }

    def _count_tokens(self, text: str) -> int:
        """Count tokens using embedded tokenizer."""
        from code_indexer.services.embedded_cohere_tokenizer import count_tokens_single

        return int(count_tokens_single(text, model=self.config.model))

    def _get_model_token_limit(self) -> int:
        """Get token limit for current model."""
        specs = self.model_specs.get("cohere_models", {}).get(self.config.model, {})
        return int(specs.get("token_limit", 128000))

    def _get_texts_per_request(self) -> int:
        """Get max texts per request for current model."""
        specs = self.model_specs.get("cohere_models", {}).get(self.config.model, {})
        return int(specs.get("texts_per_request", 96))

    def _map_embedding_purpose(self, purpose: str) -> str:
        """Map embedding_purpose to Cohere input_type.

        Args:
            purpose: Internal purpose string ("query" or "document").

        Returns:
            Cohere API input_type string.
        """
        if purpose == "query":
            return "search_query"
        return "search_document"

    def _make_sync_request(
        self, texts: List[str], input_type: str = "search_document"
    ) -> Dict[str, Any]:
        """Make synchronous HTTP request to Cohere Embed API.

        Args:
            texts: List of text strings to embed.
            input_type: Cohere input_type parameter.

        Returns:
            Parsed JSON response from the API.

        Raises:
            ValueError: If the API key is invalid (401 Unauthorized).
            RuntimeError: If all retry attempts are exhausted.
        """
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "texts": texts,
            "model": self.config.model,
            "input_type": input_type,
            "embedding_types": ["float"],
        }

        from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

        last_error: Optional[Exception] = None
        max_attempts = self.config.max_retries + 1
        _start = time.time()

        for attempt in range(max_attempts):
            try:
                _start = time.time()
                with httpx.Client(timeout=self.config.timeout) as client:
                    response = client.post(
                        self.config.api_endpoint,
                        headers=headers,
                        json=payload,
                    )

                if response.status_code == 429:
                    # Rate limited - wait and retry
                    retry_after = float(
                        response.headers.get("retry-after", self.config.retry_delay)
                    )
                    capped_delay = min(retry_after, _MAX_RETRY_SLEEP_SECONDS)
                    logger.warning(
                        "Cohere API rate limited (attempt %d/%d), retrying after %.1fs",
                        attempt + 1,
                        max_attempts,
                        capped_delay,
                    )
                    time.sleep(capped_delay)
                    continue

                if response.status_code >= 500:
                    delay = self.config.retry_delay * (
                        2**attempt if self.config.exponential_backoff else 1
                    )
                    capped_delay = min(delay, _MAX_RETRY_SLEEP_SECONDS)
                    logger.warning(
                        "Cohere API server error %d (attempt %d/%d), retrying after %.1fs",
                        response.status_code,
                        attempt + 1,
                        max_attempts,
                        capped_delay,
                    )
                    time.sleep(capped_delay)
                    continue

                response.raise_for_status()
                latency_ms = (time.time() - _start) * 1000
                ProviderHealthMonitor.get_instance().record_call(
                    "cohere", latency_ms, success=True
                )
                return dict(response.json())

            except Exception as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    delay = self.config.retry_delay * (
                        2**attempt if self.config.exponential_backoff else 1
                    )
                    capped_delay = min(delay, _MAX_RETRY_SLEEP_SECONDS)
                    logger.warning(
                        "Cohere API request failed (attempt %d/%d): %s, retrying after %.1fs",
                        attempt + 1,
                        max_attempts,
                        exc,
                        capped_delay,
                    )
                    time.sleep(capped_delay)
                    continue
                latency_ms = (time.time() - _start) * 1000
                ProviderHealthMonitor.get_instance().record_call(
                    "cohere", latency_ms, success=False
                )
                break

        latency_ms = (time.time() - _start) * 1000
        ProviderHealthMonitor.get_instance().record_call(
            "cohere", latency_ms, success=False
        )
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
            f"Cohere API request failed after {max_attempts} attempts: {last_error}"
        )

    # --- ABC Implementation ---

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
    ) -> List[float]:
        """Get single text embedding."""
        result = self.get_embeddings_batch(
            [text], model, embedding_purpose=embedding_purpose
        )
        return result[0]

    def get_embeddings_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
    ) -> List[List[float]]:
        """Get batch embeddings with dual-constraint splitting (tokens + texts/request)."""
        if not texts:
            return []

        input_type = self._map_embedding_purpose(embedding_purpose)
        model_token_limit = self._get_model_token_limit()
        max_texts = self._get_texts_per_request()
        safety_pct = self.model_specs.get("api_constraints", {}).get(
            "safety_margin_percentage", 90
        )
        safety_limit = int(model_token_limit * safety_pct / 100)

        all_embeddings: List[List[float]] = []
        current_batch: List[str] = []
        current_tokens = 0

        for text in texts:
            chunk_tokens = self._count_tokens(text)

            # Check dual constraint: token limit OR texts limit
            if current_batch and (
                current_tokens + chunk_tokens > safety_limit
                or len(current_batch) >= max_texts
            ):
                # Submit current batch
                response = self._make_sync_request(current_batch, input_type)
                embeddings = response.get("embeddings", {}).get("float", [])
                # Validate response
                if len(embeddings) != len(current_batch):
                    raise RuntimeError(
                        f"Cohere returned {len(embeddings)} embeddings "
                        f"but expected {len(current_batch)}"
                    )
                for idx, emb in enumerate(embeddings):
                    if emb is None or not emb:
                        raise RuntimeError(
                            f"Cohere returned None/empty embedding at index {idx}"
                        )
                    if any(v is None for v in emb):
                        raise RuntimeError(
                            f"Cohere returned embedding with None values at index {idx}: "
                            f"{list(emb)[:_EMBED_PREVIEW_LEN]}..."
                        )
                all_embeddings.extend(embeddings)
                current_batch = []
                current_tokens = 0

            current_batch.append(text)
            current_tokens += chunk_tokens

        # Submit final batch
        if current_batch:
            response = self._make_sync_request(current_batch, input_type)
            embeddings = response.get("embeddings", {}).get("float", [])
            # Validate response
            if len(embeddings) != len(current_batch):
                raise RuntimeError(
                    f"Cohere returned {len(embeddings)} embeddings "
                    f"but expected {len(current_batch)}"
                )
            for idx, emb in enumerate(embeddings):
                if emb is None or not emb:
                    raise RuntimeError(
                        f"Cohere returned None/empty embedding at index {idx}"
                    )
                if any(v is None for v in emb):
                    raise RuntimeError(
                        f"Cohere returned embedding with None values at index {idx}: "
                        f"{list(emb)[:_EMBED_PREVIEW_LEN]}..."
                    )
            all_embeddings.extend(embeddings)

        return all_embeddings

    def get_embedding_with_metadata(
        self,
        text: str,
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
    ):
        """Get single embedding with metadata."""
        from code_indexer.services.embedding_provider import EmbeddingResult

        embedding = self.get_embedding(text, model, embedding_purpose=embedding_purpose)
        tokens = self._count_tokens(text)
        return EmbeddingResult(
            embedding=embedding,
            model=self.config.model,
            tokens_used=tokens,
            provider="cohere",
        )

    def get_embeddings_batch_with_metadata(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
    ):
        """Get batch embeddings with metadata."""
        from code_indexer.services.embedding_provider import BatchEmbeddingResult
        from code_indexer.services.embedded_cohere_tokenizer import count_tokens

        embeddings = self.get_embeddings_batch(
            texts, model, embedding_purpose=embedding_purpose
        )
        total_tokens = count_tokens(texts, model=self.config.model)
        return BatchEmbeddingResult(
            embeddings=embeddings,
            model=self.config.model,
            total_tokens_used=total_tokens,
            provider="cohere",
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        """Check provider health.

        Args:
            test_api: If False, only check config validity (shallow).
                      If True, make a real API call (deep).

        Returns:
            True if provider is healthy, False otherwise.
        """
        if not self.api_key:
            return False
        if not test_api:
            return True
        try:
            self._make_sync_request(["health check"], "search_document")
            return True
        except Exception as exc:
            logger.warning("Cohere health check failed: %s", exc)
            return False

    def get_model_info(self) -> Dict[str, Any]:
        """Return model capabilities."""
        specs = self.model_specs.get("cohere_models", {}).get(self.config.model, {})
        return {
            "name": self.config.model,
            "provider": "cohere",
            "dimensions": int(specs.get("default_dimension", 1536)),
            "available_dimensions": specs.get("dimensions", [1536]),
            "max_tokens": specs.get("token_limit", 128000),
            "max_texts_per_request": specs.get("texts_per_request", 96),
            "supports_batch": True,
            "api_endpoint": self.config.api_endpoint,
        }

    def close(self) -> None:
        """Clean up resources (no-op, matches VoyageAI pattern)."""
        pass

    def __enter__(self):
        """Support context manager protocol."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Support context manager protocol."""
        self.close()

    def get_provider_name(self) -> str:
        """Get the name of this embedding provider."""
        return "cohere"

    def get_current_model(self) -> str:
        """Get the current active model name."""
        return str(self.config.model)

    def supports_batch_processing(self) -> bool:
        """Check if provider supports efficient batch processing."""
        return True
