"""
Reranker client abstractions and implementations.

Story #650: RerankerClient ABC + VoyageRerankerClient
Part of Epic #649: Voyage AI + Cohere Reranker Integration

Provides:
  - RerankResult      — result dataclass (index + relevance_score)
  - RerankerClient    — abstract base class for all reranker implementations
  - VoyageRerankerClient — sync httpx client for Voyage AI rerank-2.5
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

from code_indexer.server.services.config_service import get_config_service

logger = logging.getLogger(__name__)

# Default Voyage AI rerank endpoint.  Exposed as a module-level constant so
# callers and tests have a single source of truth for the URL string.
VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"

# Default model sent in every rerank request.
_DEFAULT_MODEL = "rerank-2.5"

# Provider name registered with ProviderHealthMonitor.
_PROVIDER_NAME = "voyage-reranker"


@dataclass
class RerankResult:
    """Single reranked result with its original document index and relevance score."""

    index: int
    relevance_score: float


class RerankerClient(ABC):
    """Abstract base class for cross-encoder reranking clients."""

    @abstractmethod
    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
        instruction: Optional[str] = None,
    ) -> List[RerankResult]:
        """
        Rerank documents against query and return scored results.

        Args:
            query: Search query string. Must be non-empty.
            documents: List of document strings to rerank. Must be non-empty.
            top_k: Return only the top-k results. None means return all scored.
                   When provided must be a positive integer.
            instruction: Optional instruction prepended to the query.

        Returns:
            List of RerankResult ordered by relevance_score descending.

        Raises:
            ValueError: If query is empty, documents list is empty, or top_k <= 0.
            httpx.HTTPStatusError: On HTTP 4xx/5xx responses.
            httpx.TimeoutException: On request timeout.
        """


class VoyageRerankerClient(RerankerClient):
    """
    Sync httpx client for Voyage AI rerank-2.5.

    API key is read exclusively from the config service
    (code_indexer.server.services.config_service.get_config_service).
    Reading VOYAGE_API_KEY from the environment is explicitly prohibited.

    Registers a health probe with ProviderHealthMonitor on construction.
    All API errors propagate to the caller — no exception swallowing.
    """

    def __init__(
        self,
        timeout: float = 5.0,
        max_chars: int = 4000,
        base_url: Optional[str] = None,
    ) -> None:
        """
        Args:
            timeout: HTTP request timeout in seconds. Must be positive (default 5.0).
            max_chars: Maximum characters per document before client-side truncation.
                       Must be positive (default 4000).
            base_url: Override the Voyage AI rerank endpoint. Defaults to
                      VOYAGE_RERANK_URL. Primarily used in tests to redirect
                      traffic to a local mock server.
        """
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        if max_chars <= 0:
            raise ValueError(f"max_chars must be positive, got {max_chars}")

        self.timeout = timeout
        self.max_chars = max_chars
        self._base_url = base_url if base_url is not None else VOYAGE_RERANK_URL

        # Register lightweight health probe with ProviderHealthMonitor.
        try:
            from code_indexer.services.provider_health_monitor import (
                ProviderHealthMonitor,
            )

            ProviderHealthMonitor.get_instance().register_probe(
                _PROVIDER_NAME, self._health_probe
            )
        except Exception as exc:  # pragma: no cover — monitor unavailable is non-fatal
            logger.debug(
                "Probe registration failed for %s (non-fatal): %s", _PROVIDER_NAME, exc
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
        instruction: Optional[str] = None,
    ) -> List[RerankResult]:
        """
        Call Voyage AI rerank API and return results ordered by score descending.

        Instruction prepending:
          If instruction is non-empty, the effective query sent to the API is
          f"{instruction}\\n{query}".

        Document truncation:
          Each document is truncated to self.max_chars before sending.
          Empty documents are sent as-is.

        Args:
            query: Search query string. Must be non-empty.
            documents: List of document strings to rerank. Must be non-empty.
            top_k: Return only the top-k results. None means return all.
                   When provided must be a positive integer.
            instruction: Optional instruction prepended to the query.

        Returns:
            List of RerankResult ordered by relevance_score descending.

        Raises:
            ValueError: If query is empty, documents list is empty, top_k <= 0,
                        or the configured API key is missing/empty.
            httpx.HTTPStatusError: On HTTP 4xx/5xx responses.
            httpx.TimeoutException: On request timeout.
        """
        if not query:
            raise ValueError("query must be a non-empty string")
        if not documents:
            raise ValueError("documents must be a non-empty list")
        if top_k is not None and top_k <= 0:
            raise ValueError(f"top_k must be a positive integer, got {top_k}")

        effective_query = self._build_query(query, instruction)
        truncated_docs = self._truncate_documents(documents)
        body = self._build_request_body(effective_query, truncated_docs, top_k)

        start_ms = time.monotonic() * 1000
        try:
            response = self._post(body)
            response.raise_for_status()
            latency_ms = time.monotonic() * 1000 - start_ms
            self._record_health(latency_ms=latency_ms, success=True)
        except Exception:
            latency_ms = time.monotonic() * 1000 - start_ms
            self._record_health(latency_ms=latency_ms, success=False)
            raise

        return self._parse_response(response)

    def _get_api_key(self) -> Optional[str]:
        """Return voyageai_api_key from the config service only (never from env vars).

        The return type annotation is Optional[str].  The ignore below suppresses
        a mypy "Returning Any" warning caused by the dynamically-typed config
        dataclass attribute; the runtime type is always str | None.
        """
        config = get_config_service().get_config()
        return config.claude_integration_config.voyageai_api_key  # type: ignore[no-any-return]

    def _get_model(self) -> str:
        """Return the Voyage reranker model name."""
        return _DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_query(self, query: str, instruction: Optional[str]) -> str:
        """Prepend instruction to query when instruction is non-empty."""
        if instruction:
            return f"{instruction}\n{query}"
        return query

    def _truncate_documents(self, documents: List[str]) -> List[str]:
        """Truncate each document to max_chars; empty docs are sent as-is."""
        return [doc[: self.max_chars] if doc else doc for doc in documents]

    def _build_request_body(
        self, query: str, documents: List[str], top_k: Optional[int]
    ) -> Dict:
        """Assemble the JSON body for the Voyage rerank API call."""
        body: Dict = {
            "model": self._get_model(),
            "query": query,
            "documents": documents,
            "truncation": True,
        }
        if top_k is not None:
            body["top_k"] = top_k
        return body

    def _post(self, body: Dict) -> httpx.Response:
        """Execute a synchronous POST to the configured rerank endpoint.

        Raises:
            ValueError: If the API key is missing or empty.
        """
        api_key = self._get_api_key()
        if not api_key or not api_key.strip():
            raise ValueError(
                "VoyageAI API key is missing or empty. "
                "Configure it via the server Web UI under API Keys."
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            return client.post(self._base_url, json=body, headers=headers)

    def _parse_response(self, response: httpx.Response) -> List[RerankResult]:
        """Parse Voyage API response into a list of RerankResult sorted descending."""
        data = response.json().get("data", [])
        results = [
            RerankResult(
                index=item["index"],
                relevance_score=float(item["relevance_score"]),
            )
            for item in data
        ]
        return sorted(results, key=lambda r: r.relevance_score, reverse=True)

    def _record_health(self, latency_ms: float, success: bool) -> None:
        """Record a call result with ProviderHealthMonitor (non-fatal if unavailable)."""
        try:
            from code_indexer.services.provider_health_monitor import (
                ProviderHealthMonitor,
            )

            ProviderHealthMonitor.get_instance().record_call(
                _PROVIDER_NAME, latency_ms=latency_ms, success=success
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("Health recording failed (non-fatal): %s", exc)

    def _health_probe(self) -> bool:
        """Lightweight probe for ProviderHealthMonitor recovery detection."""
        return True  # Connectivity verified by actual rerank calls


# ---------------------------------------------------------------------------
# Cohere reranker constants
# ---------------------------------------------------------------------------

# Default Cohere rerank endpoint (v2 API).
COHERE_RERANK_URL = "https://api.cohere.com/v2/rerank"

# Default Cohere reranker model.
_COHERE_DEFAULT_MODEL = "rerank-v3.5"

# Provider name registered with ProviderHealthMonitor for Cohere.
_COHERE_PROVIDER_NAME = "cohere-reranker"

# Maximum documents accepted by the Cohere rerank API.
_COHERE_MAX_DOCUMENTS = 1000


class CohereRerankerClient(RerankerClient):
    """
    Sync httpx client for Cohere rerank-v3.5.

    API key is read exclusively from the config service
    (code_indexer.server.services.config_service.get_config_service).
    Reading CO_API_KEY from the environment is explicitly prohibited.

    Key differences from VoyageRerankerClient:
      - Endpoint: https://api.cohere.com/v2/rerank
      - Request body uses ``top_n`` (not ``top_k``) and omits ``truncation`` flag.
      - Response uses ``results`` key (not ``data``).
      - Instruction concatenated with a SPACE separator (not newline).
      - Pre-flight document count validation: max 1000 documents.
      - Registers health probe as "cohere-reranker".

    All API errors propagate to the caller — no exception swallowing.
    """

    def __init__(
        self,
        timeout: float = 5.0,
        max_chars: int = 4000,
        base_url: Optional[str] = None,
    ) -> None:
        """
        Args:
            timeout: HTTP request timeout in seconds. Must be positive (default 5.0).
            max_chars: Maximum characters per document before client-side truncation.
                       Must be positive (default 4000).
            base_url: Override the Cohere rerank endpoint. Defaults to
                      COHERE_RERANK_URL. Primarily used in tests to redirect
                      traffic to a local mock server.
        """
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        if max_chars <= 0:
            raise ValueError(f"max_chars must be positive, got {max_chars}")

        self.timeout = timeout
        self.max_chars = max_chars
        self._base_url = base_url if base_url is not None else COHERE_RERANK_URL

        # Register lightweight health probe with ProviderHealthMonitor.
        try:
            from code_indexer.services.provider_health_monitor import (
                ProviderHealthMonitor,
            )

            ProviderHealthMonitor.get_instance().register_probe(
                _COHERE_PROVIDER_NAME, self._health_probe
            )
        except Exception as exc:  # pragma: no cover — monitor unavailable is non-fatal
            logger.debug(
                "Probe registration failed for %s (non-fatal): %s",
                _COHERE_PROVIDER_NAME,
                exc,
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def rerank(
        self,
        query: str,
        documents: List[str],
        top_k: Optional[int] = None,
        instruction: Optional[str] = None,
    ) -> List[RerankResult]:
        """
        Call Cohere rerank API and return results ordered by score descending.

        Instruction prepending:
          If instruction is non-empty, the effective query sent to the API is
          f"{instruction} {query}".strip() (SPACE separator — not newline).

        Document count validation:
          Raises ValueError if len(documents) > 1000 BEFORE any API call.

        Document truncation:
          Each document is truncated to self.max_chars before sending.

        Args:
            query: Search query string. Must be non-empty.
            documents: List of document strings to rerank. Must be non-empty.
                       Maximum 1000 documents.
            top_k: Return only the top-k results. None means return all.
                   When provided must be a positive integer.
            instruction: Optional instruction prepended to the query with a space.

        Returns:
            List of RerankResult ordered by relevance_score descending.

        Raises:
            ValueError: If query is empty, documents list is empty, top_k <= 0,
                        document count exceeds 1000, or API key is missing/empty.
            httpx.HTTPStatusError: On HTTP 4xx/5xx responses.
            httpx.TimeoutException: On request timeout.
        """
        if not query:
            raise ValueError("query must be a non-empty string")
        if not documents:
            raise ValueError("documents must be a non-empty list")
        if top_k is not None and top_k <= 0:
            raise ValueError(f"top_k must be a positive integer, got {top_k}")

        self._validate_document_count(documents)

        effective_query = self._build_query(query, instruction)
        truncated_docs = self._truncate_documents(documents)
        body = self._build_request_body(effective_query, truncated_docs, top_k)

        start_ms = time.monotonic() * 1000
        try:
            response = self._post(body)
            response.raise_for_status()
            latency_ms = time.monotonic() * 1000 - start_ms
            self._record_health(latency_ms=latency_ms, success=True)
        except Exception:
            latency_ms = time.monotonic() * 1000 - start_ms
            self._record_health(latency_ms=latency_ms, success=False)
            raise

        return self._parse_response(response)

    def _get_api_key(self) -> Optional[str]:
        """Return cohere_api_key from the config service only (never from env vars).

        The return type annotation is Optional[str].  The ignore below suppresses
        a mypy "Returning Any" warning caused by the dynamically-typed config
        dataclass attribute; the runtime type is always str | None.
        """
        config = get_config_service().get_config()
        return config.claude_integration_config.cohere_api_key  # type: ignore[no-any-return]

    def _get_model(self) -> str:
        """Return the Cohere reranker model name."""
        return _COHERE_DEFAULT_MODEL

    def _validate_document_count(self, documents: List[str]) -> None:
        """Raise ValueError if document count exceeds the Cohere API limit.

        Args:
            documents: List of document strings to validate.

        Raises:
            ValueError: If len(documents) > 1000.
        """
        if len(documents) > _COHERE_MAX_DOCUMENTS:
            raise ValueError(
                f"Cohere rerank API accepts at most {_COHERE_MAX_DOCUMENTS} documents, "
                f"got {len(documents)}. Split into smaller batches."
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_query(self, query: str, instruction: Optional[str]) -> str:
        """Prepend instruction to query with a SPACE separator when instruction is non-empty.

        Cohere uses a space separator (not a newline like Voyage).
        Leading/trailing whitespace is stripped from the result.
        """
        if instruction:
            return f"{instruction} {query}".strip()
        return query

    def _truncate_documents(self, documents: List[str]) -> List[str]:
        """Truncate each document to max_chars; empty docs are sent as-is."""
        return [doc[: self.max_chars] if doc else doc for doc in documents]

    def _build_request_body(
        self, query: str, documents: List[str], top_k: Optional[int]
    ) -> Dict:
        """Assemble the JSON body for the Cohere rerank API call.

        Note: Cohere uses ``top_n`` (not ``top_k``) and does not accept a
        ``truncation`` flag at this endpoint.
        """
        body: Dict = {
            "model": self._get_model(),
            "query": query,
            "documents": documents,
        }
        if top_k is not None:
            body["top_n"] = top_k
        return body

    def _post(self, body: Dict) -> httpx.Response:
        """Execute a synchronous POST to the configured Cohere rerank endpoint.

        Raises:
            ValueError: If the API key is missing or empty.
        """
        api_key = self._get_api_key()
        if not api_key or not api_key.strip():
            raise ValueError(
                "Cohere API key is missing or empty. "
                "Configure it via the server Web UI under API Keys."
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            return client.post(self._base_url, json=body, headers=headers)

    def _parse_response(self, response: httpx.Response) -> List[RerankResult]:
        """Parse Cohere API response into a list of RerankResult sorted descending.

        Cohere uses the ``results`` key (not ``data`` like Voyage).
        """
        data = response.json().get("results", [])
        results = [
            RerankResult(
                index=item["index"],
                relevance_score=float(item["relevance_score"]),
            )
            for item in data
        ]
        return sorted(results, key=lambda r: r.relevance_score, reverse=True)

    def _record_health(self, latency_ms: float, success: bool) -> None:
        """Record a call result with ProviderHealthMonitor (non-fatal if unavailable)."""
        try:
            from code_indexer.services.provider_health_monitor import (
                ProviderHealthMonitor,
            )

            ProviderHealthMonitor.get_instance().record_call(
                _COHERE_PROVIDER_NAME, latency_ms=latency_ms, success=success
            )
        except Exception as exc:  # pragma: no cover
            logger.debug("Health recording failed (non-fatal): %s", exc)

    def _health_probe(self) -> bool:
        """Lightweight probe for ProviderHealthMonitor recovery detection."""
        return True  # Connectivity verified by actual rerank calls
