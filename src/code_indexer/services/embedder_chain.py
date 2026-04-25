"""Embedder Provider Chain (Story #903 of Epic #689).

Sequential primary->secondary embedder failover. Mirrors the reranker chain
pattern from server/mcp/reranking.py:132-170 for embedder use.

Caller boundary: Story #904 wires _run_embedder_chain() into the CLI query
path (cli.py:6247 and cli.py:6309). Per story spec, no callers are wired in
this story — this module is the standalone chain implementation only.
EmbedderUnavailableError is exported here for Story #904 to import and raise
at the call site when the chain returns a total failure.
"""

import logging
import time
from typing import List, Optional, Tuple

from code_indexer.services.embedding_provider import EmbeddingProvider
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

logger = logging.getLogger(__name__)

# Health-monitor keys for embedder providers — distinct from reranker keys
# ('voyage-reranker', 'cohere-reranker') verified in provider_health_monitor.py:343,345.
EMBEDDER_HEALTH_KEYS = {
    "voyage": "voyage-embedder",
    "cohere": "cohere-embedder",
}


class EmbedderUnavailableError(Exception):
    """All configured embedder providers failed or were sin-binned.

    Exported for Story #904 to import and raise at the CLI call site when
    _run_embedder_chain returns (None, None, reason, elapsed_ms).
    Carries structured metadata about which providers were attempted.
    """

    def __init__(
        self,
        message: str,
        providers_attempted: List[
            Tuple[str, str]
        ],  # [(provider_name, failure_reason), ...]
    ) -> None:
        super().__init__(message)
        self.providers_attempted = providers_attempted


def _attempt_provider_embed(
    name: str,
    health_key: str,
    provider: EmbeddingProvider,
    text: str,
    embedding_purpose: str,
    health_monitor: ProviderHealthMonitor,
) -> Tuple[Optional[List[float]], Optional[str]]:
    """Try one embedder provider; return (vector, failure_reason).

    Gates on both get_health() (skip if status 'down') and is_sinbinned(),
    mirroring _attempt_provider_rerank() in server/mcp/reranking.py:112-117.

    Returns:
        (vector, None)      -- success; record_call(success=True) called
        (None, "skipped")   -- provider health=down or sin-binned, not attempted
        (None, "failed")    -- provider raised an exception; record_call(success=False) called
    """
    health = health_monitor.get_health(health_key)
    status = health.get(health_key)
    if status is not None and status.status == "down":
        return None, "skipped"
    if health_monitor.is_sinbinned(health_key):
        return None, "skipped"

    t_start = time.monotonic()
    try:
        vector = provider.get_embedding(text, embedding_purpose=embedding_purpose)
        latency_ms = (time.monotonic() - t_start) * 1000
        health_monitor.record_call(health_key, latency_ms, success=True)
        return vector, None
    except Exception as exc:
        latency_ms = (time.monotonic() - t_start) * 1000
        health_monitor.record_call(health_key, latency_ms, success=False)
        logger.warning("%s embedder failed: %s", name.capitalize(), exc)
        return None, "failed"


def _run_embedder_chain(
    text: str,
    embedding_purpose: str,
    primary_provider: Optional[EmbeddingProvider],
    secondary_provider: Optional[EmbeddingProvider],
    health_monitor: ProviderHealthMonitor,
) -> Tuple[Optional[List[float]], Optional[str], Optional[str], int]:
    """Run primary->secondary embedder chain for a single text.

    Mirrors _run_provider_chain() from server/mcp/reranking.py:132-170.

    Returns: (embedding_vector, provider_name, worst_failure_reason, elapsed_ms).
    On full success: (vector, "voyage" or "cohere", None, ms).
    On total failure: (None, None, reason, ms).

    Terminal failure_reason values:
      "no-providers-configured" -- primary and secondary are both None
      "all-sinbinned"           -- all configured providers gated (health=down or sinbinned)
      "failed"                  -- at least one provider raised an exception
    """
    t_start = time.monotonic()

    # Build ordered provider list — skip None entries.
    providers: List[Tuple[str, str, EmbeddingProvider]] = []
    if primary_provider is not None:
        providers.append(("voyage", EMBEDDER_HEALTH_KEYS["voyage"], primary_provider))
    if secondary_provider is not None:
        providers.append(("cohere", EMBEDDER_HEALTH_KEYS["cohere"], secondary_provider))

    if not providers:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        return None, None, "no-providers-configured", elapsed_ms

    worst_failure: Optional[str] = None
    any_attempted = False

    for name, health_key, provider in providers:
        vector, failure_reason = _attempt_provider_embed(
            name, health_key, provider, text, embedding_purpose, health_monitor
        )
        if vector is not None:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            return vector, name, None, elapsed_ms
        # Track worst failure: "failed" outranks "skipped" (mirrors reranking.py:167).
        if worst_failure != "failed":
            worst_failure = failure_reason
        if failure_reason != "skipped":
            any_attempted = True

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    # All configured providers were gated out — none attempted an actual call.
    if not any_attempted:
        return None, None, "all-sinbinned", elapsed_ms

    return None, None, worst_failure, elapsed_ms
