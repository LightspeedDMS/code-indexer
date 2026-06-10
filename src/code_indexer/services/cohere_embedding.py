"""Cohere Embed v4 provider for CIDX.

Story #486: Implements EmbeddingProvider ABC for Cohere.
All imports lazy (no module-level imports of cohere SDK).
"""

import logging
import math
import os
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any, Dict, List, Optional
from typing import Protocol, runtime_checkable

import httpx
import yaml  # type: ignore[import-untyped]
from rich.console import Console

from code_indexer.services.embedding_provider import EmbeddingProvider

logger = logging.getLogger(__name__)


# Story #1082: the Cohere model-spec YAML is a STATIC package asset (zero drift).
# Parse it ONCE per process instead of re-opening + re-parsing on every
# CohereEmbeddingProvider construction (per query on the server hot path).
# NO TTL: deploy-invalidated packaged file only.
_COHERE_MODEL_SPECS_FALLBACK: Dict[str, Any] = {
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

_cohere_model_specs_cache: Optional[Dict[str, Any]] = None
_cohere_model_specs_lock = threading.Lock()


def _get_cohere_model_specs() -> Dict[str, Any]:
    """Return the process-wide parsed Cohere model specs (parsed once).

    Thread-safe single-flight; on parse failure the hardcoded fallback is cached
    so the cost is paid once.
    """
    global _cohere_model_specs_cache
    if _cohere_model_specs_cache is not None:
        return _cohere_model_specs_cache

    with _cohere_model_specs_lock:
        if _cohere_model_specs_cache is not None:
            return _cohere_model_specs_cache
        try:
            module_dir = Path(__file__).parent.parent
            yaml_path = module_dir / "data" / "cohere_models.yaml"
            with open(yaml_path) as f:
                _cohere_model_specs_cache = yaml.safe_load(f)
        except Exception as exc:
            logger.warning(
                "Failed to load cohere_models.yaml (%s), using hardcoded fallback for embed-v4.0",
                exc,
            )
            _cohere_model_specs_cache = _COHERE_MODEL_SPECS_FALLBACK
        return _cohere_model_specs_cache


def _reset_model_specs_cache_for_tests() -> None:
    """Clear the process-level Cohere model-spec memo (test-only hook)."""
    global _cohere_model_specs_cache
    with _cohere_model_specs_lock:
        _cohere_model_specs_cache = None


@runtime_checkable
class SyncClientFactory(Protocol):
    """Protocol satisfied by HttpClientFactory for sync HTTP client creation.

    Defined here (CLI layer) so that cohere_embedding.py can accept the
    server-side HttpClientFactory without importing it directly (which would
    create a CLI->server layer violation).  Any object with a
    create_sync_client() method that returns httpx.Client satisfies this
    protocol.
    """

    def create_sync_client(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        pooled: bool = False,
        **kwargs: Any,
    ) -> Any: ...


# Number of embedding values shown in error messages when validating None values
_EMBED_PREVIEW_LEN = 10

# Maximum sleep duration for any retry path to prevent indefinite thread blocking (#602)
_MAX_RETRY_SLEEP_SECONDS = 300.0

# Timeout (seconds) used by the lightweight health probe (Story #619 HIGH-2)
_PROBE_TIMEOUT_S: float = 5.0


class CohereEmbeddingProvider(EmbeddingProvider):
    """Cohere Embed v4 embedding provider."""

    def __init__(
        self,
        config: Any,
        console: Optional[Console] = None,
        http_client_factory: Optional[SyncClientFactory] = None,
    ):
        """Initialize with CohereConfig.

        Args:
            config: Configuration object with api_key, model, api_endpoint,
                    max_retries, retry_delay, timeout attributes.
            console: Optional Rich console for output.
            http_client_factory: An object satisfying the SyncClientFactory
                Protocol (typically HttpClientFactory or NullFaultFactory).
                Use NullFaultFactory() if you do not need fault injection;
                there is no fallback to direct httpx.Client construction.

        Raises:
            ValueError: If no API key is available from config or environment.
        """
        super().__init__(console)
        self.config = config
        self.console = console or Console()
        # Factory for outbound HTTP clients (Story #746 CRITICAL fix).
        # Normalized to NullFaultFactory at construction so self._http_client_factory
        # is always a concrete factory — no if-None branches needed at call sites.
        if http_client_factory is None:
            from code_indexer.server.fault_injection.null_factory import (
                NullFaultFactory,
            )

            http_client_factory = NullFaultFactory()
        self._http_client_factory: SyncClientFactory = http_client_factory

        # API key: config first, then env var
        self.api_key = config.api_key or os.getenv("CO_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "Cohere API key required. Set via config or CO_API_KEY env var."
            )

        self._load_model_specs()

        # Register lightweight connectivity probe with health monitor (Story #619 HIGH-2)
        try:
            from code_indexer.services.provider_health_monitor import (
                ProviderHealthMonitor,
            )
        except ImportError:
            logger.debug(
                "Health monitor unavailable; skipping probe registration for cohere."
            )
        else:
            try:
                ProviderHealthMonitor.get_instance().register_probe(
                    "cohere", self._health_probe
                )
            except Exception as exc:
                logger.debug(
                    "Probe registration failed for cohere (non-fatal): %s", exc
                )

    def _load_model_specs(self) -> None:
        """Bind this provider to the process-wide parsed Cohere model specs.

        Story #1082: the static model-spec YAML is parsed ONCE per process by
        ``_get_cohere_model_specs`` and shared across all providers, removing the
        per-construction (per-query) YAML open + ``yaml.safe_load`` from the
        server hot path while preserving identical behavior and the fallback.
        """
        self.model_specs = _get_cohere_model_specs()

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
        self,
        texts: List[str],
        input_type: str = "search_document",
        *,
        retry: bool = True,
    ) -> Dict[str, Any]:
        """Make synchronous HTTP request to Cohere Embed API.

        Args:
            texts: List of text strings to embed.
            input_type: Cohere input_type parameter.
            retry: When True (default, INDEXING path), retries on 500/network errors
                with exponential back-off and time.sleep inside this method.
                When False (QUERY path), makes exactly ONE HTTP attempt and raises
                immediately on any error — sleep and retry are handled OUTSIDE the
                governor slot by execute_with_backoff in the caller (Bug #1078 C2).

        Returns:
            Parsed JSON response from the API.

        Raises:
            ValueError: If the API key is invalid (401 Unauthorized).
            RuntimeError: If all retry attempts are exhausted (retry=True) or
                the single attempt fails (retry=False).
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

        def _single_attempt() -> Dict[str, Any]:
            """Execute ONE HTTP call and return the parsed JSON dict."""
            _start = time.time()
            try:
                from code_indexer.server.services.latency_tracking_httpx_transport import (
                    build_latency_transport,
                )

                _latency_transport = build_latency_transport()
            except ImportError:
                # Server module not available in CLI-only deployments.
                _latency_transport = None
            _timeout = httpx.Timeout(
                connect=self.config.connect_timeout,
                read=self.config.timeout,
                write=self.config.timeout,
                pool=self.config.timeout,
            )
            # Story #1083: pooled=True borrows the factory's ONE long-lived
            # keep-alive client (reused SSLContext + connection pool) instead of
            # building+closing a fresh client (TLS handshake) per query.  Auth
            # already travels on the per-request .post() call below, so the pooled
            # client is auth-agnostic.  Under fault injection the factory ignores
            # pooled and still returns a fresh per-call fault-intercepted client.
            _client_ctx = self._http_client_factory.create_sync_client(
                transport=_latency_transport,
                timeout=_timeout,
                pooled=True,
            )
            with _client_ctx as client:
                response = client.post(
                    self.config.api_endpoint,
                    headers=headers,
                    json=payload,
                )
            response.raise_for_status()
            latency_ms = (time.time() - _start) * 1000
            ProviderHealthMonitor.get_instance().record_call(
                "cohere", latency_ms, success=True
            )
            return dict(response.json())

        # --- Single-attempt path (QUERY, retry=False) ---
        # One call, no sleep. All errors propagate immediately to the caller
        # (execute_with_backoff handles 429 retries OUTSIDE the governor slot).
        if not retry:
            _start_single = time.time()
            try:
                return _single_attempt()
            except Exception:
                latency_ms = (time.time() - _start_single) * 1000
                ProviderHealthMonitor.get_instance().record_call(
                    "cohere", latency_ms, success=False
                )
                raise

        # --- Retry loop (INDEXING path, retry=True) ---
        # Keeps original behaviour: retries on 500/network errors with back-off sleep.
        # 429 still propagates immediately (handled by execute_with_backoff in query path).
        last_error: Optional[Exception] = None
        max_attempts = self.config.max_retries + 1
        _start = time.time()

        for attempt in range(max_attempts):
            try:
                _start = time.time()
                return _single_attempt()
            except Exception as exc:
                last_error = exc
                response_obj = getattr(exc, "response", None)
                status = getattr(response_obj, "status_code", None)
                if status == 429:
                    if not retry:
                        # Query path: propagate immediately so execute_with_backoff
                        # can sleep OUTSIDE the governor slot.
                        raise
                    # Indexing path (retry=True): back off and retry.
                    retry_after = getattr(response_obj, "headers", {}).get(
                        "retry-after"
                    )
                    if retry_after:
                        wait_time = min(float(retry_after), _MAX_RETRY_SLEEP_SECONDS)
                    else:
                        wait_time = self.config.retry_delay * (
                            2**attempt if self.config.exponential_backoff else 1
                        )
                    if attempt < self.config.max_retries:
                        time.sleep(wait_time)
                        continue
                    latency_ms = (time.time() - _start) * 1000
                    ProviderHealthMonitor.get_instance().record_call(
                        "cohere", latency_ms, success=False
                    )
                    raise
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

    def _validate_embeddings(self, embeddings: List[List[float]], model: str) -> None:
        """Validate embedding dimensions and check for NaN/Inf values (Story #619 Gap 6).

        NaN/Inf validation runs unconditionally. Dimension check runs when model
        dimensions are known.

        Args:
            embeddings: List of embedding vectors to validate.
            model: Model name used to determine expected dimensions.

        Raises:
            RuntimeError: If any embedding has wrong dimensions or contains NaN/Inf.
        """
        specs = self.model_specs.get("cohere_models", {}).get(model, {})
        expected_dims: Optional[int] = specs.get("default_dimension")
        for i, emb in enumerate(embeddings):
            if any(not math.isfinite(v) for v in emb):
                raise RuntimeError(f"Embedding[{i}] contains NaN or Inf values")
            if expected_dims is not None and len(emb) != expected_dims:
                raise RuntimeError(
                    f"Embedding[{i}]: got {len(emb)} dims, expected {expected_dims} for model {model}"
                )

    def _health_probe(self) -> bool:
        """Lightweight connectivity probe for recovery detection (Story #619 HIGH-2).

        Makes an OPTIONS request to the configured API endpoint. Returns True if
        the server responds with any status below 500 (reachable), False otherwise.
        """
        import httpx

        probe_timeout = httpx.Timeout(
            connect=_PROBE_TIMEOUT_S,
            read=_PROBE_TIMEOUT_S,
            write=_PROBE_TIMEOUT_S,
            pool=_PROBE_TIMEOUT_S,
        )
        try:
            client_ctx = self._http_client_factory.create_sync_client(
                timeout=probe_timeout
            )
            with client_ctx as client:
                response = client.options(self.config.api_endpoint)
                return bool(response.status_code < HTTPStatus.INTERNAL_SERVER_ERROR)
        except httpx.HTTPError as exc:
            logger.debug("Cohere health probe HTTP error: %s", exc, exc_info=True)
            return False
        except Exception as exc:
            logger.debug("Cohere health probe failed: %s", exc, exc_info=True)
            return False

    # --- ABC Implementation ---

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
    ) -> List[float]:
        """Get single text embedding (QUERY path).

        Uses retry=False so that exactly one HTTP attempt is made per
        governor slot acquisition. The execute_with_backoff wrapper in the
        caller handles 429 retries OUTSIDE the governor slot (Bug #1078 C2).
        """
        result = self.get_embeddings_batch(
            [text], model, embedding_purpose=embedding_purpose, retry=False
        )
        return result[0]

    def get_embeddings_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
        retry: bool = True,
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
                response = self._make_sync_request(
                    current_batch, input_type, retry=retry
                )
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
                self._validate_embeddings(embeddings, self.config.model)
                all_embeddings.extend(embeddings)
                current_batch = []
                current_tokens = 0

            current_batch.append(text)
            current_tokens += chunk_tokens

        # Submit final batch
        if current_batch:
            response = self._make_sync_request(current_batch, input_type, retry=retry)
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
            self._validate_embeddings(embeddings, self.config.model)
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
