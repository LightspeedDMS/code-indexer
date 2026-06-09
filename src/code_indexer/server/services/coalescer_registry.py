"""Coalescer registry — process-level holder of per-lane EmbeddingCoalescers.

Story #1079 Phase E. The registry holds ONE ``EmbeddingCoalescer`` per
configured ``:embed`` lane (``voyage:embed`` / ``cohere:embed``) and is
constructed ONLY in the server lifespan startup (after providers + runtime
config are available), then cleared on shutdown.

Server-gating contract
----------------------
``get_coalescer_registry()`` returns ``None`` until ``set_coalescer_registry()``
is called from lifespan. The CLI / solo / daemon paths NEVER build a registry,
so it stays ``None`` there. ``coalesced_query_embedding`` reads this accessor:
no registry -> direct ``governed_query_embedding`` single call (no batching, no
accumulation window). This is an explicit registry/None check (Messi #2
anti-fallback: the absence of a registry is a first-class, documented branch,
not a silent fallback) — the gating lives entirely here, never at the call site.

A lane whose provider key is absent simply has no coalescer in the dict, and
``get(lane)`` returns ``None``, so ``coalesced_query_embedding`` falls back to
the direct governed call for that lane (explicit, not silent).
"""

import logging
import threading
from typing import Any, Callable, Dict, Optional

from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.services.embedding_coalescer import EmbeddingCoalescer

logger = logging.getLogger(__name__)


def _build_voyage_provider(http_client_factory: Any) -> Any:
    """Construct a VoyageAIClient (raises if VOYAGE_API_KEY is absent)."""
    from code_indexer.config import VoyageAIConfig
    from code_indexer.services.voyage_ai import VoyageAIClient

    return VoyageAIClient(VoyageAIConfig(), http_client_factory=http_client_factory)


def _build_cohere_provider(http_client_factory: Any) -> Any:
    """Construct a CohereEmbeddingProvider (raises if CO_API_KEY is absent)."""
    from code_indexer.config import CohereConfig
    from code_indexer.services.cohere_embedding import CohereEmbeddingProvider

    return CohereEmbeddingProvider(
        CohereConfig(), http_client_factory=http_client_factory
    )


# ``:embed`` lane -> direct provider constructor. Mirrors how the serving sites
# build providers (e.g. _compute_memory_query_vector builds VoyageAIClient
# directly). A missing API-key env var makes the constructor raise, which the
# builder treats as "lane absent" (non-fatal).
_LANE_PROVIDER_BUILDERS: Dict[str, Callable[[Any], Any]] = {
    "voyage:embed": _build_voyage_provider,
    "cohere:embed": _build_cohere_provider,
}


class CoalescerRegistry:
    """Immutable holder of ``{lane: EmbeddingCoalescer}`` built once in lifespan."""

    def __init__(self, coalescers: Dict[str, EmbeddingCoalescer]) -> None:
        # Copy so the caller can't mutate the registry after construction.
        self._coalescers: Dict[str, EmbeddingCoalescer] = dict(coalescers)

    def get(self, lane: str) -> Optional[EmbeddingCoalescer]:
        """Return the coalescer for ``lane`` or ``None`` if that lane is absent."""
        return self._coalescers.get(lane)


# Process-level singleton. None until lifespan sets it (CLI/solo never does).
_registry: Optional[CoalescerRegistry] = None
_registry_lock = threading.Lock()


def get_coalescer_registry() -> Optional[CoalescerRegistry]:
    """Return the process-level registry, or ``None`` if none was built.

    ``None`` is the CLI/solo case and the pre-lifespan case — the caller
    (``coalesced_query_embedding``) treats it as "no coalescing, direct
    governed single call".
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
    """Build a registry with one EmbeddingCoalescer per CONFIGURED ``:embed`` lane.

    Called ONCE in server lifespan startup (after providers + runtime config are
    available). For each provider that has a valid API key
    (``EmbeddingProviderFactory.get_configured_providers``), constructs the
    embedding provider (passing ``http_client_factory`` so fault injection
    intercepts) and an ``EmbeddingCoalescer`` on that provider's ``:embed`` lane
    with ``config.coalesce_max_batch_size`` as the texts ceiling.

    A provider whose API-key env var is absent makes its constructor raise; that
    lane is then simply absent, so ``coalesced_query_embedding`` falls back to the
    direct governed call for it. A per-lane construction error is logged and
    skipped (non-fatal): the lane is never half-built.
    """
    ceiling = int(getattr(config, "coalesce_max_batch_size", 96))
    coalescers: Dict[str, EmbeddingCoalescer] = {}

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

    for lane, build_provider in _LANE_PROVIDER_BUILDERS.items():
        try:
            provider = build_provider(http_client_factory)
            coalescers[lane] = EmbeddingCoalescer(
                lane,
                provider,
                coalesce_max_batch_size=ceiling,
                ceiling_provider=_live_ceiling,
            )
            logger.info(
                "build_coalescer_registry: built coalescer for lane=%s (ceiling=%d)",
                lane,
                ceiling,
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal per-lane build
            logger.warning(
                "build_coalescer_registry: no coalescer for lane=%s (%s); "
                "that lane will use the direct governed call",
                lane,
                exc,
            )

    return CoalescerRegistry(coalescers=coalescers)
