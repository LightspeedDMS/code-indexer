"""Story #1105 / #1106: QueryEmbeddingCache service — anchor-token embedding cache.

Provides:
- build_key(text, anchor_tokens=2) -> SHA-256 hex (case-preserved, anchor-token
  normalised).  Story #1106 generalises S1's exact-match key: the first
  ``anchor_tokens`` tokens are kept in original order; the remaining tokens are
  sorted alphabetically (duplicates kept as a sorted multiset).  anchor_tokens=0
  sorts ALL tokens; anchor_tokens >= token count degenerates to exact-match.
  CASE IS NEVER LOWERCASED at any step.
- CacheQualifier: named-tuple PK fields (provider, model, dimension)
- QueryEmbeddingCache: service wrapping a QueryEmbeddingCacheBackend with
  per-provider mode gating (off / shadow / on) and fail-open error handling.
  ``enabled_for()``, ``mode_for()``, and ``anchor_tokens_for()`` read LIVE from
  the config service on every call (mirror of ``coalesce_enabled`` in
  governed_call.py) so the master kill switch, per-provider mode, and
  anchor-depth take effect WITHOUT a restart.

Namespace-change observability (Story #1106):
  When the effective ``anchor_tokens`` for a provider changes at runtime, exactly
  ONE structured WARNING is emitted so operators understand the resulting
  hit-rate dip.  The process-local memo is per-provider so a change on one
  provider never suppresses a log for another.  Changing ``anchor_tokens``
  INTENTIONALLY fragments the keyspace: old rows keyed under the old normalisation
  no longer match new keys and age out via LRU.  Correctness is preserved —
  each row's key still correctly matches its own normalisation.  NOTE: a live
  model change is NOT tracked by this log because ``anchor_tokens_for()``
  receives only the provider-name string, not the provider object, so the model
  is genuinely unavailable at that call site.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Dict, List, NamedTuple, Optional, cast

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
# Default anchor depth
# ---------------------------------------------------------------------------

_DEFAULT_ANCHOR_TOKENS = 2

# ---------------------------------------------------------------------------
# Key building
# ---------------------------------------------------------------------------


def build_key(text: str, anchor_tokens: int = _DEFAULT_ANCHOR_TOKENS) -> str:
    """Return SHA-256 hex of the anchor-token-normalised representation of *text*.

    Normalisation algorithm (Story #1106):
    1. Tokenise: ``text.split()`` (no argument) — splits on any whitespace run,
       strips leading/trailing whitespace, discards empty tokens automatically.
       Punctuation is NOT stripped (attached to its token).
    2. Take the first ``anchor_tokens`` tokens in ORIGINAL order (the anchor prefix).
    3. Sort the REMAINING tokens ALPHABETICALLY (case-aware, i.e. lexicographic on
       the raw Unicode code points — NEVER lowercased).  Duplicates are kept as a
       sorted multiset.
    4. Normalised string = anchor prefix + sorted tail, joined by a single space.
    5. Return SHA-256 hex of the normalised string encoded as UTF-8.

    Boundary behaviours:
    - ``anchor_tokens == 0`` -> sort ALL tokens (no anchor prefix).
    - ``anchor_tokens >= token_count`` -> all tokens in original order; tail is
      empty; key equals exact-match SHA-256 of the joined tokens.
    - Empty / whitespace-only input -> empty token list -> SHA-256 of ``""``
      (stable, non-crashing).

    CASE PRESERVED throughout — never lowercased.  Two queries that differ only
    in case produce different keys.

    Args:
        text: The raw query string (any length, including empty).
        anchor_tokens: Number of leading tokens to keep in original order.
            Remaining tokens are sorted alphabetically.  Must be >= 0; negative
            values are treated as 0.  Default: 2.

    Returns:
        64-character lowercase hex string (SHA-256 digest).
    """
    # Clamp to >= 0 defensively (negative is not meaningful)
    n = max(0, anchor_tokens)

    tokens = text.split()  # collapses whitespace runs; case preserved
    if n >= len(tokens):
        # All tokens in original order — exact-match semantics
        normalised = " ".join(tokens)
    else:
        anchor = tokens[:n]
        tail = sorted(tokens[n:])  # lexicographic (case-aware); duplicates kept
        normalised = " ".join(anchor + tail)

    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


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
        # Process-local memo of last-seen anchor_tokens per provider (Story #1106).
        # Used to detect changes and emit exactly ONE structured WARNING per change.
        self._last_anchor_tokens: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Key / qualifier helpers
    # ------------------------------------------------------------------

    @staticmethod
    def build_key(text: str, anchor_tokens: int = _DEFAULT_ANCHOR_TOKENS) -> str:
        """Delegate to module-level :func:`build_key`.

        Args:
            text: The raw query string.
            anchor_tokens: Number of leading tokens to keep in original order.
                Remaining tokens are sorted alphabetically.  Default: 2.

        Returns:
            64-character SHA-256 hex string.
        """
        return build_key(text, anchor_tokens)

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
    # Live config read helper
    # ------------------------------------------------------------------

    def _live_qec_cfg(self) -> Optional[object]:
        """Return QueryEmbeddingCacheConfig from live config service, or None.

        Fail-open: any exception returns None so callers fall back to the
        construction-time defaults stored in ``self._enabled`` / ``self._modes``.
        """
        try:
            from code_indexer.server.services.config_service import get_config_service

            cfg = get_config_service().get_config()
            return getattr(cfg, "query_embedding_cache_config", None)
        except Exception:  # noqa: BLE001 — config read is best-effort
            logger.debug(
                "query_embedding_cache: could not read live config; using construction defaults",
            )
            return None

    # ------------------------------------------------------------------
    # Mode gating
    # ------------------------------------------------------------------

    def enabled_for(self, provider_name: str) -> bool:
        """Return True iff master switch is ON and provider mode is not 'off'.

        Reads LIVE from ``get_config_service().get_config().query_embedding_cache_config``
        on every call (mirror of ``coalesce_enabled`` in governed_call.py).
        Falls back to the construction-time ``enabled`` arg when config unavailable.

        Args:
            provider_name: e.g. "voyage-ai" or "cohere".

        Returns:
            bool — False means caller should skip all cache interaction.
        """
        qec_cfg = self._live_qec_cfg()
        if qec_cfg is not None:
            enabled = bool(
                getattr(qec_cfg, "query_embedding_cache_enabled", self._enabled)
            )
        else:
            enabled = self._enabled
        if not enabled:
            return False
        return self._live_mode_for(provider_name, qec_cfg) != _MODE_OFF

    def mode_for(self, provider_name: str) -> str:
        """Return the effective mode string for *provider_name*.

        Reads LIVE from the config service on every call.

        Returns:
            One of "off", "shadow", "on".  Unknown providers default to "shadow".
        """
        return self._live_mode_for(provider_name, self._live_qec_cfg())

    def _live_mode_for(self, provider_name: str, qec_cfg: Optional[object]) -> str:
        """Extract mode for *provider_name* from *qec_cfg* (live) or fallback."""
        if qec_cfg is None:
            return self._modes.get(provider_name, _MODE_SHADOW)
        if provider_name == "voyage-ai":
            raw = getattr(
                qec_cfg,
                "query_embedding_cache_voyage_mode",
                self._modes.get("voyage-ai", _MODE_SHADOW),
            )
        elif provider_name == "cohere":
            raw = getattr(
                qec_cfg,
                "query_embedding_cache_cohere_mode",
                self._modes.get("cohere", _MODE_SHADOW),
            )
        else:
            raw = self._modes.get(provider_name, _MODE_SHADOW)
        mode = str(raw) if raw in _VALID_MODES else _MODE_SHADOW
        return mode

    # ------------------------------------------------------------------
    # Anchor-token dial (Story #1106)
    # ------------------------------------------------------------------

    def anchor_tokens_for(self, provider_name: str) -> int:
        """Return the effective ``anchor_tokens`` for *provider_name*.

        Reads LIVE from the config service on every call — a value change takes
        effect immediately without a restart.

        Per-provider config keys (tried in order):
        - ``query_embedding_cache_voyage_anchor_tokens`` (when provider == "voyage-ai")
        - ``query_embedding_cache_cohere_anchor_tokens`` (when provider == "cohere")
        - ``query_embedding_cache_anchor_tokens`` (global fallback field)
        - :data:`_DEFAULT_ANCHOR_TOKENS` (construction-time hard default)

        Namespace-change observability: when the resolved ``anchor_tokens`` value
        differs from the last-seen value for this provider, ONE structured WARNING
        is emitted so operators understand the resulting hit-rate dip.  Subsequent
        calls with the same new value do NOT re-log.  NOTE: a live model change is
        NOT tracked here because this method receives only the provider-name string,
        not the provider object — the model is genuinely unavailable at this call
        site.

        The keyspace fragmentation is INTENTIONAL: old rows keyed under the old
        normalisation no longer match new keys and age out via LRU.  Correctness
        is preserved — each row's key still correctly matches its own
        normalisation.

        Args:
            provider_name: e.g. "voyage-ai" or "cohere".

        Returns:
            int >= 0.  Negative config values are clamped to 0.
        """
        qec_cfg = self._live_qec_cfg()
        raw: int = _DEFAULT_ANCHOR_TOKENS
        if qec_cfg is not None:
            # Try per-provider key first
            if provider_name == "voyage-ai":
                per_provider = getattr(
                    qec_cfg,
                    "query_embedding_cache_voyage_anchor_tokens",
                    None,
                )
            elif provider_name == "cohere":
                per_provider = getattr(
                    qec_cfg,
                    "query_embedding_cache_cohere_anchor_tokens",
                    None,
                )
            else:
                per_provider = None

            if per_provider is not None:
                raw = int(per_provider)
            else:
                # Global fallback
                raw = int(
                    getattr(
                        qec_cfg,
                        "query_embedding_cache_anchor_tokens",
                        _DEFAULT_ANCHOR_TOKENS,
                    )
                )

        effective = max(0, raw)  # clamp negative to sort-all

        # Namespace-change observability: emit ONE WARNING when value changes.
        last = self._last_anchor_tokens.get(provider_name)
        if last is not None and last != effective:
            logger.warning(
                "query_embedding_cache: anchor_tokens changed for provider '%s' "
                "(%d -> %d) — cache namespace fragmented; old keys age out via LRU; "
                "correctness preserved (each row matches its own normalisation).",
                provider_name,
                last,
                effective,
            )
        self._last_anchor_tokens[provider_name] = effective

        return effective

    def build_key_for_provider(self, text: str, provider_name: str) -> str:
        """Build a cache key using the LIVE ``anchor_tokens`` for *provider_name*.

        Convenience method that combines :meth:`anchor_tokens_for` and
        :func:`build_key` in one call.  Used by the cache-wrap layer so the
        active anchor depth is always up-to-date.

        Args:
            text: The raw query string.
            provider_name: e.g. "voyage-ai" or "cohere".

        Returns:
            64-character SHA-256 hex string.
        """
        return build_key(text, self.anchor_tokens_for(provider_name))

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

    def _resolve_max_entries(self) -> int:
        """Return the effective LRU cap, applying the >=100 safe floor.

        Reads the LIVE ``query_embedding_cache_max_entries`` from the config
        service on every call (mirror of how ``mode_for``/``anchor_tokens_for``
        read live config via ``_live_qec_cfg()``).  Applies the safe floor:
        ``return max(configured, 100)`` so values < 100 are raised to 100.

        The >=100 floor is the SOLE location of this floor logic — it must NOT
        be duplicated inside the backend primitives (which are pure).

        Returns:
            int >= 100.
        """
        qec_cfg = self._live_qec_cfg()
        if qec_cfg is not None:
            raw = int(
                getattr(
                    qec_cfg,
                    "query_embedding_cache_max_entries",
                    self._max_entries,
                )
            )
        else:
            raw = self._max_entries
        return max(raw, 100)

    def record_miss_or_shadow(
        self,
        cache_key: str,
        qualifier: CacheQualifier,
        embedding: List[float],
    ) -> None:
        """UPSERT the embedding bytes into the backend, then enforce the LRU cap.

        Converts the float list to float32 little-endian bytes before writing.
        After a successful upsert, calls ``prune_to_max`` with the LIVE resolved
        cap so the cap is NOT orphan code.  Prune failure is fail-open: logs a
        WARNING but does NOT roll back the write and does NOT raise.

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
            return

        # Enforce the LRU cap on every miss-write.  Workload is ~500 searches/day
        # so COUNT+delete-excess is trivial.  Prune failure is fail-open.
        try:
            self._backend.prune_to_max(self._resolve_max_entries())
        except Exception:
            logger.warning(
                "query_embedding_cache: prune_to_max failed (fail-open)",
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
