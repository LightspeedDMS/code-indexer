"""Story #1105: QueryEmbeddingCache service — exact-match embedding cache.

Provides:
- build_key(text) -> SHA-256 hex (case-preserved, no normalisation)
- CacheQualifier: named-tuple PK fields (provider, model, dimension)
- QueryEmbeddingCache: service wrapping a QueryEmbeddingCacheBackend with
  per-provider mode gating (off / shadow / on) and fail-open error handling.

Chunk C wires this into governed_call and lifespan.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import List, NamedTuple, Optional, cast

from code_indexer.server.storage.protocols import QueryEmbeddingCacheBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mode constants
# ---------------------------------------------------------------------------

_MODE_OFF = "off"
_MODE_SHADOW = "shadow"
_MODE_ON = "on"

_VALID_MODES = {_MODE_OFF, _MODE_SHADOW, _MODE_ON}

# ---------------------------------------------------------------------------
# Key building
# ---------------------------------------------------------------------------


def build_key(text: str) -> str:
    """Return SHA-256 hex digest of *text* encoded as UTF-8.

    CASE PRESERVED — never lowercased.  Two queries that differ only in
    case produce different keys (exact-match semantics).

    Args:
        text: The raw query string (any length, including empty).

    Returns:
        64-character lowercase hex string (SHA-256 digest).
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Qualifier (composite PK fields)
# ---------------------------------------------------------------------------


class CacheQualifier(NamedTuple):
    """Identifies the (provider, model, dimension) axis of a cached embedding.

    Used as a typed carrier between ``QueryEmbeddingCache.qualifier()`` and
    the ``lookup`` / ``record_miss_or_shadow`` / ``record_hit`` methods so
    callers never pass three separate positional strings.
    """

    provider: str
    model: str
    dimension: int


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class QueryEmbeddingCache:
    """Fail-open cache service for query embeddings.

    Constructor is called with explicit arguments.  Tests instantiate directly
    with known values.  Chunk C will wire live ServerConfig reads.

    Args:
        backend: A QueryEmbeddingCacheBackend implementation.
        enabled: Master kill switch.  When False, all operations are no-ops.
        voyage_mode: "off" | "shadow" | "on" for voyage-ai provider.
        cohere_mode: "off" | "shadow" | "on" for cohere provider.
        max_entries: Soft cap stored for Chunk C pruning integration.
    """

    def __init__(
        self,
        backend: QueryEmbeddingCacheBackend,
        *,
        enabled: bool = True,
        voyage_mode: str = _MODE_SHADOW,
        cohere_mode: str = _MODE_SHADOW,
        max_entries: int = 10000,
    ) -> None:
        self._backend = backend
        self._enabled = enabled
        self._modes = {
            "voyage-ai": voyage_mode if voyage_mode in _VALID_MODES else _MODE_SHADOW,
            "cohere": cohere_mode if cohere_mode in _VALID_MODES else _MODE_SHADOW,
        }
        self._max_entries = max_entries

    # ------------------------------------------------------------------
    # Key / qualifier helpers
    # ------------------------------------------------------------------

    @staticmethod
    def build_key(text: str) -> str:
        """Delegate to module-level :func:`build_key`."""
        return build_key(text)

    def qualifier(self, provider: object) -> CacheQualifier:
        """Extract a :class:`CacheQualifier` from a provider object.

        The provider must expose:
        - ``get_provider_name() -> str``
        - ``get_current_model() -> str``
        - ``get_model_info() -> dict`` with key ``"dimensions"``

        Args:
            provider: An embedding-provider instance (duck-typed).

        Returns:
            CacheQualifier named tuple.
        """
        pname: str = provider.get_provider_name()  # type: ignore[attr-defined]
        model: str = provider.get_current_model()  # type: ignore[attr-defined]
        info: dict = provider.get_model_info()  # type: ignore[attr-defined]
        dimension: int = int(info["dimensions"])
        return CacheQualifier(provider=pname, model=model, dimension=dimension)

    # ------------------------------------------------------------------
    # Mode gating
    # ------------------------------------------------------------------

    def enabled_for(self, provider_name: str) -> bool:
        """Return True iff master switch is ON and provider mode is not 'off'.

        Args:
            provider_name: e.g. "voyage-ai" or "cohere".

        Returns:
            bool — False means caller should skip all cache interaction.
        """
        if not self._enabled:
            return False
        return self._modes.get(provider_name, _MODE_SHADOW) != _MODE_OFF

    def mode_for(self, provider_name: str) -> str:
        """Return the effective mode string for *provider_name*.

        Returns:
            One of "off", "shadow", "on".  Unknown providers default to "shadow".
        """
        return self._modes.get(provider_name, _MODE_SHADOW)

    # ------------------------------------------------------------------
    # Cache operations — all fail-open
    # ------------------------------------------------------------------

    def lookup(
        self,
        cache_key: str,
        qualifier: CacheQualifier,
    ) -> Optional[bytes]:
        """Look up a cached embedding.  Fail-open on any backend error.

        Args:
            cache_key: SHA-256 hex from :meth:`build_key`.
            qualifier: Provider / model / dimension tuple.

        Returns:
            Raw float32 LE bytes or None (miss OR backend error).
        """
        try:
            return cast(
                Optional[bytes],
                self._backend.lookup(
                    cache_key,
                    qualifier.provider,
                    qualifier.model,
                    qualifier.dimension,
                ),
            )
        except Exception:
            logger.warning(
                "query_embedding_cache: lookup failed (fail-open)",
                exc_info=True,
            )
            return None

    def record_miss_or_shadow(
        self,
        cache_key: str,
        qualifier: CacheQualifier,
        embedding: List[float],
    ) -> None:
        """UPSERT the embedding bytes into the backend.  Fail-open on error.

        Converts the float list to float32 little-endian bytes before writing.

        Args:
            cache_key: SHA-256 hex from :meth:`build_key`.
            qualifier: Provider / model / dimension tuple.
            embedding: List of floats (the live embedding result).
        """
        try:
            import numpy as np

            blob: bytes = np.asarray(embedding, dtype="<f4").tobytes()
            now = time.time()
            self._backend.upsert(
                cache_key,
                qualifier.provider,
                qualifier.model,
                qualifier.dimension,
                blob,
                now,
                now,
            )
        except Exception:
            logger.warning(
                "query_embedding_cache: upsert failed (fail-open)",
                exc_info=True,
            )

    def record_hit(
        self,
        cache_key: str,
        qualifier: CacheQualifier,
    ) -> None:
        """Touch last_used timestamp for an existing cache row.  Fail-open.

        Args:
            cache_key: SHA-256 hex from :meth:`build_key`.
            qualifier: Provider / model / dimension tuple.
        """
        try:
            self._backend.touch_last_used(
                cache_key,
                qualifier.provider,
                qualifier.model,
                qualifier.dimension,
                time.time(),
            )
        except Exception:
            logger.warning(
                "query_embedding_cache: touch_last_used failed (fail-open)",
                exc_info=True,
            )

    def total_entries(self) -> int:
        """Return total row count from the backend.

        Returns:
            int row count, or 0 on backend error.
        """
        try:
            return cast(int, self._backend.total_entries())
        except Exception:
            logger.warning(
                "query_embedding_cache: total_entries failed (fail-open)",
                exc_info=True,
            )
            return 0
