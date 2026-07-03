"""VoyageAI API client for embeddings generation."""

import logging
import math
import os
import threading
import time
from http import HTTPStatus
from typing import List, Dict, Any, Optional
from typing import Protocol, runtime_checkable
import httpx
from rich.console import Console
import yaml  # type: ignore[import-untyped]
from pathlib import Path

from ..config import VoyageAIConfig
from .embedding_provider import EmbeddingProvider, EmbeddingResult, BatchEmbeddingResult
from .provider_backoff import is_rate_limited

logger = logging.getLogger(__name__)


@runtime_checkable
class SyncClientFactory(Protocol):
    """Protocol satisfied by HttpClientFactory for sync HTTP client creation.

    Defined here (CLI layer) so that voyage_ai.py can accept the server-side
    HttpClientFactory without importing it directly (which would create a
    CLI→server layer violation).  Any object with a create_sync_client() method
    that returns httpx.Client satisfies this protocol.
    """

    def create_sync_client(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        pooled: bool = False,
        **kwargs: Any,
    ) -> Any: ...


# Timeout (seconds) used by the lightweight health probe (Story #619 HIGH-2)
_PROBE_TIMEOUT_S: float = 5.0

# Suppress tokenizers parallelism warning
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# NOTE: VoyageTokenizer is imported lazily inside _count_tokens_accurately()
# to avoid triggering the import chain at module load time

# Dimensions per model (Story #619 Gap 6 — used for embedding validation)
_VOYAGE_MODEL_DIMENSIONS: Dict[str, int] = {
    "voyage-code-3": 1024,
    "voyage-multimodal-3": 1024,
    "voyage-large-2": 1536,
    "voyage-2": 1024,
    "voyage-code-2": 1536,
    "voyage-law-2": 1024,
    "voyage-context-4": 1024,
}

# Story #1290 (AC14): models that MUST embed queries via the contextualized
# endpoint (POST /v1/contextualizedembeddings, input_type="query") instead of
# the ordinary /v1/embeddings endpoint. get_embedding() branches on this set
# so any provider instance pinned to one of these models (e.g. the temporal
# per-commit voyage-context-4 recall path) transparently reuses the existing
# governor/cache/lane plumbing (get_provider_name/get_current_model are
# unaffected — only the outbound HTTP call changes).
_CONTEXTUAL_QUERY_MODELS = frozenset({"voyage-context-4"})

# Story #1290: contextualized embeddings endpoint used by the per-commit
# contextual temporal embedder (voyage-context-4). Distinct from
# VoyageAIConfig.api_endpoint (the ordinary /v1/embeddings endpoint used by
# regular semantic indexing) — this is a fixed provider URL, not configurable
# per repo, matching the story's "POST /v1/contextualizedembeddings" anchor.
CONTEXTUALIZED_EMBEDDINGS_ENDPOINT = (
    "https://api.voyageai.com/v1/contextualizedembeddings"
)


# Story #1082: the Voyage model-spec YAML is a STATIC package asset (zero drift).
# Parse it ONCE per process instead of re-opening + re-parsing it on every
# VoyageAIClient construction (which happens per query on the server hot path).
# NO TTL: the source is an unchanging packaged file, deploy-invalidated only.
_MODEL_SPECS_FALLBACK: Dict[str, Any] = {
    "voyage_models": {
        "voyage-code-3": {"token_limit": 120000},
        "voyage-large-2": {"token_limit": 120000},
        "voyage-2": {"token_limit": 320000},
    }
}

_model_specs_cache: Optional[Dict[str, Any]] = None
_model_specs_lock = threading.Lock()


def _get_voyage_model_specs(console: Optional[Console] = None) -> Dict[str, Any]:
    """Return the process-wide parsed Voyage model specs (parsed once).

    Thread-safe single-flight: the first caller parses the static YAML under a
    lock; all subsequent callers reuse the same parsed dict. On parse failure
    the hardcoded fallback specs are cached so the cost is still paid once.
    """
    global _model_specs_cache
    if _model_specs_cache is not None:
        return _model_specs_cache

    with _model_specs_lock:
        if _model_specs_cache is not None:
            return _model_specs_cache
        try:
            module_dir = Path(__file__).parent.parent
            yaml_path = module_dir / "data" / "voyage_models.yaml"
            with open(yaml_path, "r", encoding="utf-8") as f:
                _model_specs_cache = yaml.safe_load(f)
        except Exception as e:
            (console or Console()).print(
                f"[yellow]Warning: Could not load model specs: {e}[/yellow]"
            )
            _model_specs_cache = _MODEL_SPECS_FALLBACK
        return _model_specs_cache


def _reset_model_specs_cache_for_tests() -> None:
    """Clear the process-level model-spec memo (test-only hook)."""
    global _model_specs_cache
    with _model_specs_lock:
        _model_specs_cache = None


class VoyageAIClient(EmbeddingProvider):
    """Client for interacting with VoyageAI API."""

    def __init__(
        self,
        config: VoyageAIConfig,
        console: Optional[Console] = None,
        http_client_factory: Optional[SyncClientFactory] = None,
    ):
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

        # Get API key from environment
        self.api_key = os.getenv("VOYAGE_API_KEY")
        if not self.api_key:
            raise ValueError(
                "VOYAGE_API_KEY environment variable is required for VoyageAI. "
                "Set it with: export VOYAGE_API_KEY=your_api_key_here"
            )

        # Load model specifications from YAML
        self._load_model_specs()

        # HTTP client will be created per request to avoid threading issues
        # ThreadPoolExecutor removed - parallel processing handled by VectorCalculationManager

        # Register lightweight connectivity probe with health monitor (Story #619 HIGH-2)
        try:
            from .provider_health_monitor import ProviderHealthMonitor
        except ImportError:
            logger.debug(
                "Health monitor unavailable; skipping probe registration for voyage-ai."
            )
        else:
            try:
                ProviderHealthMonitor.get_instance().register_probe(
                    "voyage-ai", self._health_probe
                )
            except Exception as exc:
                logger.debug(
                    "Probe registration failed for voyage-ai (non-fatal): %s", exc
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
        expected_dims = _VOYAGE_MODEL_DIMENSIONS.get(model)
        for i, emb in enumerate(embeddings):
            if any(not math.isfinite(v) for v in emb):
                raise RuntimeError(f"Embedding[{i}] contains NaN or Inf values")
            if expected_dims is not None and len(emb) != expected_dims:
                raise RuntimeError(
                    f"Embedding[{i}]: got {len(emb)} dims, expected {expected_dims} for model {model}"
                )

    def _load_model_specs(self):
        """Bind this client to the process-wide parsed model specs.

        Story #1082: the static model-spec YAML is parsed ONCE per process by
        ``_get_voyage_model_specs`` and shared across all clients. This removes
        the per-construction (per-query) YAML open + ``yaml.safe_load`` from the
        server hot path while preserving identical behavior and the fallback.
        """
        self.model_specs = _get_voyage_model_specs(self.console)

    def _count_tokens_accurately(self, text: str) -> int:
        """Count tokens accurately using VoyageAI's embedded tokenizer."""
        # Lazy import to avoid loading tokenizer at module import time
        from .embedded_voyage_tokenizer import VoyageTokenizer

        return VoyageTokenizer.count_tokens([text], model=self.config.model)

    def _get_model_token_limit(self) -> int:
        """Get token limit for current model."""
        try:
            limit = self.model_specs["voyage_models"][self.config.model]["token_limit"]
            return int(limit)  # Ensure integer return type
        except (KeyError, TypeError):
            # Fallback for unknown models
            return 120000  # Conservative default

    def _get_model_context_length(self) -> int:
        """Get the per-TEXT context length for current model (Story #1290 AC23).

        Distinct from ``_get_model_token_limit()`` (a per-BATCH/request token
        budget, e.g. 120000): ``context_length`` is the maximum tokens a
        SINGLE input text may contain -- the correct basis for a per-chunk
        token cap. Read from voyage_models.yaml; unknown models fall back to
        the conservative 32000 shared by every model currently in the spec.
        """
        try:
            limit = self.model_specs["voyage_models"][self.config.model][
                "context_length"
            ]
            return int(limit)
        except (KeyError, TypeError):
            return 32000  # Conservative default

    def _health_probe(self) -> bool:
        """Lightweight connectivity probe for recovery detection (Story #619 HIGH-2).

        Makes an OPTIONS request to the configured API endpoint. Returns True if
        the server responds with any status below 500 (reachable), False otherwise.
        """
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
            logger.debug("VoyageAI health probe HTTP error: %s", exc, exc_info=True)
            return False
        except Exception as exc:
            logger.debug("VoyageAI health probe failed: %s", exc, exc_info=True)
            return False

    def health_check(self, *, test_api: bool = False) -> bool:
        """Check if VoyageAI service is configured correctly.

        Args:
            test_api: If True, make an actual API call to test connectivity.
                     If False, only check configuration validity.
        """
        try:
            # First check configuration validity
            config_valid = bool(
                self.api_key  # API key is available
                and self.config.model  # Model is configured
                and self.config.api_endpoint  # Endpoint is configured
            )

            if not config_valid:
                return False

            # If API testing is requested, make a simple API call
            if test_api:
                try:
                    # Make a minimal API call with a single character
                    self._make_sync_request(["test"])
                    return True
                except Exception:
                    return False

            # For normal health checks, only verify configuration
            # Making actual API calls during startup causes hanging due to rate limits/timeouts
            return True
        except Exception:
            return False

    def _make_sync_request(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        retry: bool = True,
    ) -> Dict[str, Any]:
        """Make synchronous request to VoyageAI API.

        Args:
            texts: Text strings to embed.
            model: Override model name; defaults to self.config.model.
            retry: When True (default, INDEXING path), retries on 500/network errors
                with exponential back-off and time.sleep inside this method.
                When False (QUERY path), makes exactly ONE HTTP attempt and raises
                immediately on any error — sleep and retry are handled OUTSIDE the
                governor slot by execute_with_backoff in the caller.
        """
        from .provider_health_monitor import ProviderHealthMonitor

        model_name = model or self.config.model

        # Prepare request payload
        payload = {"input": texts, "model": model_name}

        def _single_attempt() -> Dict[str, Any]:
            """Execute ONE HTTP call and return the parsed JSON dict."""
            _start = time.time()
            _timeout = httpx.Timeout(
                connect=self.config.connect_timeout,
                read=self.config.timeout,
                write=self.config.timeout,
                pool=self.config.timeout,
            )
            # Story #1083: auth header travels on the per-request .post() call so
            # the pooled keep-alive client stays auth-agnostic — API-key rotation
            # is transparent (no client invalidation/rebuild needed).
            _headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            # Story #1083 (+ residual): pooled=True borrows the factory's ONE
            # long-lived keep-alive client (reused SSLContext + connection pool)
            # instead of building+closing a fresh client per query.  The latency
            # transport is OWNED by the factory and baked into the pooled client
            # ONCE — the provider no longer constructs build_latency_transport()
            # (and its SSLContext) per call, which was the residual per-query
            # churn.  Under fault injection the factory ignores pooled and still
            # returns a fresh per-call client (building the latency transport then).
            _client_ctx = self._http_client_factory.create_sync_client(
                timeout=_timeout,
                pooled=True,
            )
            with _client_ctx as client:
                response = client.post(
                    self.config.api_endpoint, json=payload, headers=_headers
                )
            response.raise_for_status()

            result = response.json()
            if not isinstance(result, dict):
                raise ValueError(f"Unexpected response format: {type(result)}")

            latency_ms = (time.time() - _start) * 1000
            ProviderHealthMonitor.get_instance().record_call(
                "voyage-ai", latency_ms, success=True
            )
            return result

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
                    "voyage-ai", latency_ms, success=False
                )
                raise

        # --- Retry loop (INDEXING path, retry=True) ---
        # Keeps original behaviour: retries on 500/network errors with back-off sleep.
        # 429 still propagates immediately (handled by execute_with_backoff in query path).
        last_exception: Optional[Exception] = None
        _start = time.time()
        for attempt in range(self.config.max_retries + 1):
            try:
                _start = time.time()
                return _single_attempt()
            except httpx.HTTPStatusError as e:
                last_exception = e
                if e.response.status_code == 429:
                    if not retry:
                        # Query path: propagate immediately so execute_with_backoff
                        # can sleep OUTSIDE the governor slot.
                        raise
                    # Indexing path (retry=True): back off and retry as before.
                    retry_after = e.response.headers.get("retry-after")
                    if retry_after:
                        wait_time = min(float(retry_after), 300.0)
                    else:
                        wait_time = self.config.retry_delay * (
                            2**attempt if self.config.exponential_backoff else 1
                        )
                    if attempt < self.config.max_retries:
                        time.sleep(wait_time)
                        continue
                    raise
                elif e.response.status_code >= 500:  # Server error
                    wait_time = self.config.retry_delay * (
                        2**attempt if self.config.exponential_backoff else 1
                    )
                    if attempt < self.config.max_retries:
                        time.sleep(wait_time)
                        continue
                else:
                    # Client error, don't retry
                    break
            except Exception as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    time.sleep(self.config.retry_delay)
                    continue
                else:
                    break

        # All retries exhausted — record failure before raising
        latency_ms = (time.time() - _start) * 1000
        ProviderHealthMonitor.get_instance().record_call(
            "voyage-ai", latency_ms, success=False
        )
        if isinstance(last_exception, httpx.HTTPStatusError):
            if last_exception.response.status_code == 401:
                raise ValueError(
                    "Invalid VoyageAI API key. Check VOYAGE_API_KEY environment variable."
                )
            elif last_exception.response.status_code == 429:
                raise RuntimeError(
                    "VoyageAI rate limit exceeded. Try reducing parallel_requests or requests_per_minute."
                )
            else:
                # Include more detailed error information for debugging
                try:
                    response_text = last_exception.response.text
                except Exception:
                    response_text = "Unable to read response"
                raise RuntimeError(
                    f"VoyageAI API error (HTTP {last_exception.response.status_code}): {last_exception}. "
                    f"Response: {response_text}"
                )
        else:
            raise ConnectionError(f"Failed to connect to VoyageAI: {last_exception}")

    def _make_sync_contextualized_request(
        self,
        documents: List[List[str]],
        *,
        input_type: str,
        output_dimension: int = 1024,
        model: Optional[str] = None,
        retry: bool = True,
    ) -> Dict[str, Any]:
        """Make synchronous request to VoyageAI POST /v1/contextualizedembeddings.

        Story #1290: mirrors _make_sync_request's retry/backoff/health-probe
        structure, targeting the contextualized-embeddings endpoint instead of
        the ordinary embeddings endpoint. The client MUST NOT re-chunk: each
        inner list in `documents` is sent as-is (already fixed-size-chunked by
        the caller with 0% overlap).

        Args:
            documents: Ordered list of documents; each document is an ordered
                list of chunk texts sharing context.
            input_type: "document" (indexing) or "query" (recall).
            output_dimension: Requested embedding dimensionality.
            model: Override model name; defaults to self.config.model.
            retry: See _make_sync_request.
        """
        from .provider_health_monitor import ProviderHealthMonitor

        model_name = model or self.config.model
        payload = {
            "inputs": documents,
            "model": model_name,
            "input_type": input_type,
            "output_dimension": output_dimension,
        }

        def _single_attempt() -> Dict[str, Any]:
            _start = time.time()
            _timeout = httpx.Timeout(
                connect=self.config.connect_timeout,
                read=self.config.timeout,
                write=self.config.timeout,
                pool=self.config.timeout,
            )
            _headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            _client_ctx = self._http_client_factory.create_sync_client(
                timeout=_timeout,
                pooled=True,
            )
            with _client_ctx as client:
                response = client.post(
                    CONTEXTUALIZED_EMBEDDINGS_ENDPOINT, json=payload, headers=_headers
                )
            response.raise_for_status()

            result = response.json()
            if not isinstance(result, dict):
                raise ValueError(f"Unexpected response format: {type(result)}")

            latency_ms = (time.time() - _start) * 1000
            ProviderHealthMonitor.get_instance().record_call(
                "voyage-ai", latency_ms, success=True
            )
            return result

        if not retry:
            _start_single = time.time()
            try:
                return _single_attempt()
            except Exception:
                latency_ms = (time.time() - _start_single) * 1000
                ProviderHealthMonitor.get_instance().record_call(
                    "voyage-ai", latency_ms, success=False
                )
                raise

        last_exception: Optional[Exception] = None
        _start = time.time()
        for attempt in range(self.config.max_retries + 1):
            try:
                _start = time.time()
                return _single_attempt()
            except httpx.HTTPStatusError as e:
                last_exception = e
                if e.response.status_code == 429:
                    retry_after = e.response.headers.get("retry-after")
                    wait_time = (
                        min(float(retry_after), 300.0)
                        if retry_after
                        else self.config.retry_delay
                        * (2**attempt if self.config.exponential_backoff else 1)
                    )
                    if attempt < self.config.max_retries:
                        time.sleep(wait_time)
                        continue
                    raise
                elif e.response.status_code >= 500:
                    wait_time = self.config.retry_delay * (
                        2**attempt if self.config.exponential_backoff else 1
                    )
                    if attempt < self.config.max_retries:
                        time.sleep(wait_time)
                        continue
                else:
                    break
            except Exception as e:
                last_exception = e
                if attempt < self.config.max_retries:
                    time.sleep(self.config.retry_delay)
                    continue
                else:
                    break

        latency_ms = (time.time() - _start) * 1000
        ProviderHealthMonitor.get_instance().record_call(
            "voyage-ai", latency_ms, success=False
        )
        if isinstance(last_exception, httpx.HTTPStatusError):
            if last_exception.response.status_code == 401:
                raise ValueError(
                    "Invalid VoyageAI API key. Check VOYAGE_API_KEY environment variable."
                )
            elif last_exception.response.status_code == 429:
                raise RuntimeError(
                    "VoyageAI rate limit exceeded on contextualized embeddings. "
                    "Try reducing parallel_requests or requests_per_minute."
                )
            else:
                try:
                    response_text = last_exception.response.text
                except Exception:
                    response_text = "Unable to read response"
                raise RuntimeError(
                    f"VoyageAI contextualized-embeddings API error "
                    f"(HTTP {last_exception.response.status_code}): {last_exception}. "
                    f"Response: {response_text}"
                )
        else:
            raise ConnectionError(
                f"Failed to connect to VoyageAI contextualized embeddings: {last_exception}"
            )

    def _validate_contextualized_embeddings(
        self, embeddings: List[List[float]], expected_dimension: int
    ) -> None:
        """Validate contextualized-embedding vectors (dims + NaN/Inf), Story #1290."""
        for i, emb in enumerate(embeddings):
            if any(not math.isfinite(v) for v in emb):
                raise RuntimeError(
                    f"Contextualized embedding[{i}] contains NaN or Inf values"
                )
            if len(emb) != expected_dimension:
                raise RuntimeError(
                    f"Contextualized embedding[{i}]: got {len(emb)} dims, "
                    f"expected {expected_dimension}"
                )

    def get_contextualized_embeddings(
        self,
        documents: List[List[str]],
        input_type: str,
        output_dimension: int = 1024,
        model: Optional[str] = None,
        *,
        retry: bool = True,
    ) -> List[List[List[float]]]:
        """Embed documents' ordered chunk lists via the contextualized endpoint.

        Story #1290: used by ContextualTemporalEmbedder (voyage-context-4) to
        embed a per-commit aggregated document's already-chunked (0% overlap)
        text. Each document's chunks share context within that document; the
        client performs fixed-size chunking BEFORE this call — the provider
        MUST NOT re-chunk.

        Args:
            documents: Ordered list of documents; each document is an ordered
                list of chunk texts.
            input_type: "document" (indexing) or "query" (recall).
            output_dimension: Requested embedding dimensionality (1024 for
                voyage-context-4 per Story #1290).
            model: Override model name; defaults to self.config.model.
            retry: See _make_sync_request.

        Returns:
            Per-document list of per-chunk embedding vectors, in the same
            order as `documents`.

        Raises:
            RuntimeError: If the response's document or per-document chunk
                count does not match the request (fail-loud, AC21 — no
                partial/silent index on a contextualized-response mismatch).
        """
        if not documents:
            return []

        result = self._make_sync_contextualized_request(
            documents,
            input_type=input_type,
            output_dimension=output_dimension,
            model=model,
            retry=retry,
        )

        groups = result.get("data", [])
        if len(groups) != len(documents):
            raise RuntimeError(
                f"VoyageAI contextualized embeddings document count mismatch: "
                f"sent {len(documents)} documents, got {len(groups)} in response."
            )

        # The API's `index` field is authoritative for group ordering.
        groups_by_index = {g["index"]: g for g in groups}

        ordered_results: List[List[List[float]]] = []
        for doc_idx, doc_chunks in enumerate(documents):
            group = groups_by_index.get(doc_idx)
            if group is None:
                raise RuntimeError(
                    "VoyageAI contextualized embeddings response missing "
                    f"document index {doc_idx}."
                )
            inner = group.get("data", [])
            if len(inner) != len(doc_chunks):
                raise RuntimeError(
                    "VoyageAI contextualized embeddings chunk count mismatch "
                    f"for document index {doc_idx}: sent {len(doc_chunks)} "
                    f"chunks, got {len(inner)} in response."
                )
            inner_sorted = sorted(inner, key=lambda item: item["index"])
            embeddings = [list(item["embedding"]) for item in inner_sorted]
            self._validate_contextualized_embeddings(embeddings, output_dimension)
            ordered_results.append(embeddings)

        return ordered_results

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        """Generate embedding for given text (QUERY path).

        Uses retry=False so that exactly one HTTP attempt is made per
        governor slot acquisition. The execute_with_backoff wrapper in the
        caller handles 429 retries OUTSIDE the governor slot.

        Story #1290 (AC14): when the effective model is a contextual model
        (voyage-context-4), the query is embedded via the contextualized
        endpoint with input_type="query" instead of the standard
        /v1/embeddings endpoint -- required for correct recall against
        per-commit contextual temporal shards.
        """
        model_to_use = model or self.config.model
        if model_to_use in _CONTEXTUAL_QUERY_MODELS:
            output_dimension = _VOYAGE_MODEL_DIMENSIONS.get(model_to_use, 1024)
            result = self.get_contextualized_embeddings(
                [[text]],
                input_type="query",
                output_dimension=output_dimension,
                model=model_to_use,
                retry=False,
            )
            return result[0][0]

        # Use get_embeddings_batch internally with single-item array and retry=False
        # so no time.sleep() runs while the governor slot is held (Bug #1078 C2).
        batch_result = self.get_embeddings_batch([text], model, retry=False)

        # Extract first result from batch response
        return batch_result[0]

    def get_embeddings_batch(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
        retry: bool = True,
    ) -> List[List[float]]:
        """Generate embeddings with dynamic token-aware batching (90% safety margin).

        Args:
            texts: Texts to embed.
            model: Override model name.
            embedding_purpose: "document" (indexing) or "query".
            retry: When True (default, INDEXING path), each _make_sync_request call
                retries on 500/network errors with back-off sleep.
                When False (QUERY path, set by get_embedding), exactly one HTTP
                attempt is made — sleep is handled OUTSIDE the governor slot by
                execute_with_backoff (Bug #1078 C2).
        """
        if not texts:
            return []

        # Get model-specific token limit with 90% safety margin
        model_token_limit = self._get_model_token_limit()
        safety_limit = int(model_token_limit * 0.9)  # 90% safety margin as requested

        # Dynamic batching: process chunks until approaching token limit
        all_embeddings: List[List[float]] = []
        current_batch: List[str] = []
        current_tokens: int = 0

        for text in texts:
            # Count tokens accurately for this chunk
            chunk_tokens = self._count_tokens_accurately(text)

            # Check if adding this chunk would exceed 90% safety limit
            if current_tokens + chunk_tokens > safety_limit and current_batch:
                # Submit current batch before it gets too large
                try:
                    result = self._make_sync_request(current_batch, model, retry=retry)

                    # LAYER 3 VALIDATION: Validate all embeddings from API before processing
                    for idx, item in enumerate(result["data"]):
                        emb = item["embedding"]
                        if emb is None:
                            raise RuntimeError(
                                f"VoyageAI returned None embedding at index {idx} in batch. "
                                f"API response is corrupt."
                            )
                        if not emb:  # Empty list
                            raise RuntimeError(
                                f"VoyageAI returned empty embedding at index {idx} in batch"
                            )
                        # Check for None values inside embedding
                        if any(v is None for v in emb):
                            raise RuntimeError(
                                f"VoyageAI returned embedding with None values at index {idx}: {emb[:10]}..."
                            )

                    batch_embeddings = [
                        list(item["embedding"]) for item in result["data"]
                    ]

                    # VALIDATION: Ensure embeddings match input count
                    if len(batch_embeddings) != len(current_batch):
                        raise RuntimeError(
                            f"VoyageAI returned {len(batch_embeddings)} embeddings "
                            f"but expected {len(current_batch)}. Partial response detected."
                        )

                    model_to_use = model or self.config.model
                    self._validate_embeddings(batch_embeddings, model_to_use)
                    all_embeddings.extend(batch_embeddings)
                except Exception as e:
                    # Re-raise rate-limit (429) signals intact so the
                    # execute_with_backoff wrapper (and future AIMD signal) can
                    # classify and retry them; only non-429 errors are wrapped
                    # in a generic RuntimeError (Story #1079 Phase A).
                    if is_rate_limited(e):
                        raise
                    raise RuntimeError(f"Batch embedding request failed: {e}")

                # Reset for next batch
                current_batch = []
                current_tokens = 0

            # Add chunk to current batch
            current_batch.append(text)
            current_tokens += chunk_tokens

        # Process final batch if not empty
        if current_batch:
            try:
                result = self._make_sync_request(current_batch, model, retry=retry)

                # LAYER 3 VALIDATION: Validate all embeddings from API before processing
                for idx, item in enumerate(result["data"]):
                    emb = item["embedding"]
                    if emb is None:
                        raise RuntimeError(
                            f"VoyageAI returned None embedding at index {idx} in batch. "
                            f"API response is corrupt."
                        )
                    if not emb:  # Empty list
                        raise RuntimeError(
                            f"VoyageAI returned empty embedding at index {idx} in batch"
                        )
                    # Check for None values inside embedding
                    if any(v is None for v in emb):
                        raise RuntimeError(
                            f"VoyageAI returned embedding with None values at index {idx}: {emb[:10]}..."
                        )

                batch_embeddings = [list(item["embedding"]) for item in result["data"]]

                # VALIDATION: Ensure embeddings match input count
                if len(batch_embeddings) != len(current_batch):
                    raise RuntimeError(
                        f"VoyageAI returned {len(batch_embeddings)} embeddings "
                        f"but expected {len(current_batch)}. Partial response detected."
                    )

                model_to_use = model or self.config.model
                self._validate_embeddings(batch_embeddings, model_to_use)
                all_embeddings.extend(batch_embeddings)
            except Exception as e:
                # Re-raise rate-limit (429) signals intact so the
                # execute_with_backoff wrapper (and future AIMD signal) can
                # classify and retry them; only non-429 errors are wrapped
                # in a generic RuntimeError (Story #1079 Phase A).
                if is_rate_limited(e):
                    raise
                raise RuntimeError(f"Batch embedding request failed: {e}")

        return all_embeddings

    def get_embedding_with_metadata(
        self,
        text: str,
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
    ) -> EmbeddingResult:
        """Generate embedding with metadata."""
        # Use batch processing internally for consistency
        batch_result = self.get_embeddings_batch_with_metadata([text], model)

        if not batch_result.embeddings:
            raise ValueError("No embedding returned from batch processing")

        # Extract single embedding from batch result
        return EmbeddingResult(
            embedding=batch_result.embeddings[0],
            model=batch_result.model,
            tokens_used=batch_result.total_tokens_used,
            provider=batch_result.provider,
        )

    def get_embeddings_batch_with_metadata(
        self,
        texts: List[str],
        model: Optional[str] = None,
        *,
        embedding_purpose: str = "document",
    ) -> BatchEmbeddingResult:
        """Generate batch embeddings with metadata."""
        if not texts:
            return BatchEmbeddingResult(
                embeddings=[], model=model or self.config.model, provider="voyage-ai"
            )

        # Use conservative sub-batching by delegating to get_embeddings_batch()
        embeddings = self.get_embeddings_batch(texts, model)

        # Create metadata result (note: token usage not available from batched calls)
        model_name = model or self.config.model

        return BatchEmbeddingResult(
            embeddings=embeddings,
            model=model_name,
            total_tokens_used=None,  # Token usage not available from batched processing
            provider="voyage-ai",
        )

    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the current model."""
        model_name = self.config.model

        return {
            "name": model_name,
            "provider": "voyage-ai",
            "dimensions": _VOYAGE_MODEL_DIMENSIONS.get(
                model_name, 1024
            ),  # Default to 1024
            "max_tokens": 16000,  # VoyageAI typical context limit
            "supports_batch": True,
            "api_endpoint": self.config.api_endpoint,
        }

    def get_provider_name(self) -> str:
        """Get the name of this embedding provider."""
        return "voyage-ai"

    def get_current_model(self) -> str:
        """Get the current active model name."""
        return self.config.model

    def supports_batch_processing(self) -> bool:
        """Check if provider supports efficient batch processing."""
        return True

    def close(self) -> None:
        """Clean up resources (no executor to close after refactoring)."""
        # ThreadPoolExecutor removed - no cleanup needed
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
