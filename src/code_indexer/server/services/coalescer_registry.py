"""Coalescer registry — lazy, digest-keyed per-lane EmbeddingCoalescer registry.

Story #1079 Phase E. Bug #1112 fix: the registry is now LAZY and keyed by
(lane, config-digest). On the first request for a (lane, digest) pair the
registry constructs a fresh coalescer from the CALLER's provider (which
carries the per-repo config). Subsequent requests for the same (lane, digest)
reuse that coalescer.

The old design built ONE coalescer per lane from a BARE DEFAULT config
(VoyageAIConfig() / CohereConfig()), ignoring per-repo api_endpoint, model,
and api_key overrides. All repos shared the same coalescer regardless of their
config — producing wrong query vectors for any repo using a non-default
endpoint or model.

The fix: lazy + digest-keyed. ``_digest_for_provider(provider)`` extracts the
provider's (model, endpoint, key-fingerprint, timeouts) and returns a stable
digest. ``CoalescerRegistry.get_or_create(lane, digest, provider)`` returns the
cached coalescer for (lane, digest), building one from the caller's provider on
a miss. Per-lane cap (default 64) prevents unbounded growth.

Server-gating contract
----------------------
``get_coalescer_registry()`` returns ``None`` until ``set_coalescer_registry()``
is called from lifespan. The CLI / solo / daemon paths NEVER build a registry,
so it stays ``None`` there. ``_compute_live`` reads this accessor:
no registry -> direct ``governed_query_embedding`` single call (no batching, no
accumulation window). This is an explicit registry/None check (Messi #2
anti-fallback: the absence of a registry is a first-class, documented branch,
not a silent fallback) — the gating lives entirely here, never at the call site.

A lane whose coalescer encounters cap overflow returns None from get_or_create,
and ``_compute_live`` falls back to the direct governed call for that request
(explicit, not silent).
"""

import logging
import threading
from typing import Any, Callable, Dict, Optional

from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer

logger = logging.getLogger(__name__)

# Default maximum number of distinct digest-keyed coalescers per lane.
# Protects against unbounded growth from misconfigured / rapidly-changing
# per-repo configs. When the cap is hit, get_or_create returns None and the
# caller falls back to the direct governed call.
_DEFAULT_MAX_PER_LANE: int = 64

# Stable sentinel digest returned when _digest_for_provider cannot extract
# config attributes from the provider (AttributeError safety net).
_FALLBACK_DIGEST: str = "fallback-no-config"


def is_fallback_digest(digest: str) -> bool:
    """Return True when *digest* is the sentinel produced by a failed extraction.

    Use this predicate (rather than comparing against the string literal) so the
    sentinel value is defined in exactly ONE place.  Callers that need to guard
    against sentinel-collapse (two providers both returning the sentinel and
    therefore appearing config-identical) should call this instead of
    ``digest == "fallback-no-config"``.
    """
    return digest == _FALLBACK_DIGEST


def _digest_for_provider(provider: Any) -> str:
    """Compute a stable digest over the provider's behavior-affecting config.

    Extracts (model, api_endpoint, api_key-fingerprint, connect_timeout,
    timeout, max_retries, retry_delay, exponential_backoff) from
    ``provider.config`` and delegates to ``provider_config_digest`` from
    query_path_cache. The provider type (voyage / cohere) is also embedded so
    two providers from different lanes with identical config still produce
    distinct digests.

    Defensive: catches AttributeError so it NEVER raises into the hot path.
    Returns ``_FALLBACK_DIGEST`` when the provider lacks a .config attribute.

    Args:
        provider: Any EmbeddingProvider (VoyageAIClient, CohereEmbeddingProvider).

    Returns:
        A stable hex-digest string.
    """
    try:
        from code_indexer.server.services.query_path_cache import provider_config_digest

        cfg = provider.config
        # Determine provider type name from the provider's class for lane separation.
        provider_type = type(provider).__name__

        return provider_config_digest(  # type: ignore[no-any-return]
            provider=provider_type,
            model=str(getattr(cfg, "model", "")),
            api_key=getattr(cfg, "api_key", None),
            api_endpoint=str(getattr(cfg, "api_endpoint", "")),
            connect_timeout=float(getattr(cfg, "connect_timeout", 0.0)),
            timeout=float(getattr(cfg, "timeout", 0.0)),
            max_retries=getattr(cfg, "max_retries", None),
            retry_delay=getattr(cfg, "retry_delay", None),
            exponential_backoff=getattr(cfg, "exponential_backoff", None),
        )
    except AttributeError:
        # Provider has no .config attribute (tests, stub providers). Return a
        # stable sentinel so the call never raises into the hot path.
        return _FALLBACK_DIGEST
    except Exception as exc:  # noqa: BLE001
        # Any other unexpected error: log and return sentinel (fail-open).
        logger.debug("_digest_for_provider: unexpected error (%s); using sentinel", exc)
        return _FALLBACK_DIGEST


def _lane_to_provider_name(lane: str) -> str:
    """Derive the provider name string from a lane key.

    ``"voyage:embed"`` / ``"voyage:rerank"``  -> ``"voyage-ai"``
    ``"cohere:embed"`` / ``"cohere:rerank"``  -> ``"cohere"``
    Unknown prefix -> empty string (anchor_tokens_for falls back to default 2).
    """
    prefix = lane.split(":")[0]
    if prefix == "voyage":
        return "voyage-ai"
    if prefix == "cohere":
        return "cohere"
    return ""


class CoalescerRegistry:
    """Lazy, digest-keyed holder of per-(lane, digest) EmbeddingCoalescers.

    Built ONCE in server lifespan startup. Coalescers are constructed on demand
    (first request for a (lane, digest) pair) from the CALLER's provider, so the
    per-repo api_endpoint/model/key are used, not a stale default.

    Thread-safe via a single lock. Per-lane cap (``max_per_lane``) prevents
    unbounded growth; overflow returns None so the caller falls back to the
    direct governed call.

    Backward compat: ``get(lane)`` returns None (the lazy registry has no
    pre-built coalescer). Callers should use ``get_or_create`` instead.
    """

    def __init__(
        self,
        *,
        max_per_lane: int = _DEFAULT_MAX_PER_LANE,
        http_client_factory: Any = None,
        ceiling_provider: Optional[Callable[[], int]] = None,
        # FOLD IN #4: per-lane callable ``(lane) -> anchor_depth`` so the coalescer
        # uses the SAME live anchor depth as the cache (build_key_for_provider).
        # When None, coalescers use build_key's own default (2) — unchanged.
        anchor_depth_provider: Optional[Callable[[str], int]] = None,
        # Legacy keyword accepted but ignored (old tests pass coalescers= dict).
        coalescers: Optional[Dict[str, EmbeddingCoalescer]] = None,
    ) -> None:
        self._max_per_lane = max(0, max_per_lane)
        self._http_client_factory = http_client_factory
        self._ceiling_provider = ceiling_provider
        self._anchor_depth_provider = anchor_depth_provider
        # {lane: {digest: EmbeddingCoalescer}}
        self._coalescers: Dict[str, Dict[str, EmbeddingCoalescer]] = {}
        self._lock = threading.Lock()

        # Legacy compat: if coalescers dict provided, pre-seed under a synthetic
        # digest so old tests that call reg.get(lane) still work.
        if coalescers:
            for lane, coalescer in coalescers.items():
                self._coalescers.setdefault(lane, {})["__legacy__"] = coalescer

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, lane: str) -> Optional[EmbeddingCoalescer]:
        """Return a coalescer for ``lane`` or ``None``.

        Legacy accessor: returns None on a lazy (non-pre-seeded) registry.
        Use ``get_or_create`` for the digest-keyed lazy path.
        """
        with self._lock:
            lane_map = self._coalescers.get(lane)
            if not lane_map:
                return None
            # Return the first coalescer (legacy pre-seeded path).
            return next(iter(lane_map.values()))

    def get_or_create(
        self, lane: str, digest: str, provider: Any
    ) -> Optional[EmbeddingCoalescer]:
        """Return the cached coalescer for (lane, digest); build one on a miss.

        Under the lock: returns the cached coalescer on a hit. On a miss,
        checks the per-lane cap: if at or above cap, logs ONE structured
        WARNING and returns None (caller falls back to direct). Otherwise
        constructs a new EmbeddingCoalescer from the caller's provider (so
        its per-repo config is used) and caches it.

        Args:
            lane: Governor lane key (e.g. "voyage:embed").
            digest: Config digest from ``_digest_for_provider``.
            provider: The per-repo embedding provider (its config drives the coalescer).

        Returns:
            An EmbeddingCoalescer, or None if the per-lane cap is exceeded.
        """
        with self._lock:
            lane_map = self._coalescers.setdefault(lane, {})
            if digest in lane_map:
                return lane_map[digest]

            # Cap check: if the per-lane map is already at max, return None.
            if len(lane_map) >= self._max_per_lane:
                logger.warning(
                    "coalescer_registry: per-lane cap (%d) reached for lane=%s; "
                    "new digest=%s will use the direct governed call (cap exceeded)",
                    self._max_per_lane,
                    lane,
                    digest[:12],
                )
                return None

            # Build a new coalescer from the caller's provider.
            # Pass digest so the coalescer's build_key uses the same identity as
            # the registry key (Story #1146 dedup-key namespacing).
            coalescer = self._build_coalescer(lane, provider, digest=digest)
            lane_map[digest] = coalescer
            logger.debug(
                "coalescer_registry: built new coalescer lane=%s digest=%s "
                "(total in lane: %d)",
                lane,
                digest[:12],
                len(lane_map),
            )
            return coalescer

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_coalescer(
        self, lane: str, provider: Any, digest: str = ""
    ) -> EmbeddingCoalescer:
        """Construct an EmbeddingCoalescer from the given provider + factory/ceiling.

        Args:
            lane: Governor lane key (e.g. "voyage:embed").
            provider: The per-repo embedding provider (its config drives the coalescer).
            digest: Config digest for Story #1146 dedup-key namespacing. Passed as
                config_digest so build_key produces ``s:<digest>:<normalized>`` keys
                that are scoped to this provider config — same identity as the
                coalescer-registry key.
        """
        # Determine ceiling: use the live ceiling_provider if wired, else default 96.
        if self._ceiling_provider is not None:
            ceiling = int(self._ceiling_provider())
        else:
            ceiling = 96  # Safe default (Cohere texts-cap minimum)

        # FOLD IN #4: build a per-lane anchor_depth_provider closure so the coalescer
        # uses the SAME live anchor depth as QueryEmbeddingCache.build_key_for_provider.
        # The closure captures the provider_name (derived from lane once) and delegates
        # to self._anchor_depth_provider(provider_name) — a callable injected at
        # registry construction time (build_coalescer_registry wires it from the cache).
        # When self._anchor_depth_provider is None (tests / CLI), no closure is built
        # and the coalescer falls back to build_key's own default (2).
        coalescer_anchor_provider: Optional[Callable[[], int]] = None
        if self._anchor_depth_provider is not None:
            _provider_name = _lane_to_provider_name(lane)
            _registry_anchor = self._anchor_depth_provider

            def _make_anchor_fn(pname: str) -> Callable[[], int]:
                def _anchor_fn() -> int:
                    return _registry_anchor(pname)

                return _anchor_fn

            coalescer_anchor_provider = _make_anchor_fn(_provider_name)

        return EmbeddingCoalescer(
            lane,
            provider,
            coalesce_max_batch_size=ceiling,
            ceiling_provider=self._ceiling_provider,
            config_digest=digest,
            anchor_depth_provider=coalescer_anchor_provider,
        )

    def metrics(self) -> Dict[str, int]:
        """Return aggregated coalescer counters across all registered coalescers.

        Sums texts_coalesced, batches_dispatched, dedup_savings, and
        provider_embed_calls across every (lane, digest) coalescer held in this
        registry. Returns per-node in-memory tallies (not persisted to DB).

        Used by the front-door cache-metrics partial (dashboard) so cluster E2E
        can read them without DB access.

        Returns:
            Dict with keys: texts_coalesced, batches_dispatched, dedup_savings,
            provider_embed_calls.
        """
        totals: Dict[str, int] = {
            "texts_coalesced": 0,
            "batches_dispatched": 0,
            "dedup_savings": 0,
            "provider_embed_calls": 0,
        }
        with self._lock:
            for lane_map in self._coalescers.values():
                for coalescer in lane_map.values():
                    totals["texts_coalesced"] += coalescer.texts_coalesced
                    totals["batches_dispatched"] += coalescer.batches_dispatched
                    totals["dedup_savings"] += coalescer.dedup_savings
                    totals["provider_embed_calls"] += coalescer.provider_embed_calls
        return totals


# Process-level singleton. None until lifespan sets it (CLI/solo never does).
_registry: Optional[CoalescerRegistry] = None
_registry_lock = threading.Lock()


def get_coalescer_registry() -> Optional[CoalescerRegistry]:
    """Return the process-level registry, or ``None`` if none was built.

    ``None`` is the CLI/solo case and the pre-lifespan case — the caller
    (``_compute_live``) treats it as "no coalescing, direct governed single call".
    """
    with _registry_lock:
        return _registry


def set_coalescer_registry(registry: CoalescerRegistry) -> None:
    """Install the process-level registry (called once in lifespan startup)."""
    global _registry
    with _registry_lock:
        _registry = registry


def clear_coalescer_registry() -> None:
    """Clear the process-level registry (lifespan shutdown / test isolation)."""
    global _registry
    with _registry_lock:
        _registry = None


def build_coalescer_registry(
    config: Any,
    http_client_factory: Any = None,
) -> CoalescerRegistry:
    """Build an EMPTY lazy coalescer registry wired with the factory + live ceiling.

    Called ONCE in server lifespan startup (after providers + runtime config are
    available). The registry is LAZY: no providers are constructed at build time.
    Coalescers are built on demand by ``get_or_create`` from the caller's per-repo
    provider.

    The ``_live_ceiling`` closure reads ``coalesce_max_batch_size`` LIVE from the
    config service so a runtime change hot-reloads without rebuilding the registry.

    Args:
        config: A config object with ``coalesce_max_batch_size`` (used as the
            fallback ceiling at construction time).
        http_client_factory: Optional HttpClientFactory for pooled HTTP clients
            (passed through to the registry, available for future use).

    Returns:
        An empty CoalescerRegistry ready for lazy coalescer construction.
    """
    ceiling = int(getattr(config, "coalesce_max_batch_size", 96))

    def _live_ceiling() -> int:
        """Read coalesce_max_batch_size LIVE so runtime changes hot-reload.

        Falls back to the build-time ``ceiling`` if the config is momentarily
        unreadable (never raises into the coalescer's seal path).
        """
        try:
            value = getattr(
                get_config_service().get_config(), "coalesce_max_batch_size", None
            )
            if isinstance(value, int) and value > 0:
                return value
        except Exception:  # noqa: BLE001 — best-effort live read
            pass
        return ceiling

    registry = CoalescerRegistry(
        http_client_factory=http_client_factory,
        ceiling_provider=_live_ceiling,
    )
    logger.info(
        "build_coalescer_registry: empty lazy registry built (ceiling=%d); "
        "coalescers will be built on demand per config-digest",
        ceiling,
    )
    return registry
