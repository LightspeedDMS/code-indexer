"""Shared helper for wiring cidx.embedding.* OTEL metrics into embedding
provider clients (Story #1586 AC2).

Each provider client (VoyageAIClient, CohereEmbeddingProvider,
CohereMultimodalClient, VoyageMultimodalClient) calls
record_embedding_provider_call() exactly once per REAL outbound HTTP
embedding request -- never around a cache-hit short-circuit (the
query-embedding cache lives in server/services/governed_call.py, a layer
ABOVE these clients, and never calls into them on a hit) and never at a
delegating public wrapper whose own call chain lands on an
already-instrumented boundary (that would double-count one real request).

time_and_record_embedding_call() composes the timing + try/except +
recording boilerplate around a single real call in one line, for call
sites with no internal retry loop of their own.

Lazily imports the server-side telemetry package inside the function body
(never at module level) -- this module is used by CLI-layer embedding
clients (voyage_ai.py explicitly documents that CLI-layer files must not
import server-layer modules at module scope; the existing
embedding_call_instrumentation import at each provider's HTTP call site
already establishes the same lazy-import precedent for exactly this reason).
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def record_embedding_provider_call(
    model: str,
    duration_seconds: float,
    status: str,
    count_tokens: Callable[[], int],
) -> None:
    """Record one cidx.embedding.* metrics event for a real provider call.

    Args:
        model: Embedding model name to attribute the metric to.
        duration_seconds: Wall-clock duration of the real outbound call.
        status: "success" or "error".
        count_tokens: Zero-arg callable returning the token count for this
            request. Only invoked when ApplicationMetrics is actually
            active, so a telemetry-disabled deployment never pays the
            tokenizer cost purely for metrics purposes.

    Never raises: telemetry failures must never break the embedding path
    (mirrors this project's documented fail-open contract for the
    query-embedding cache and every ApplicationMetrics.record_* call site).
    """
    try:
        from code_indexer.server.telemetry.manager import peek_telemetry_manager
        from code_indexer.server.telemetry.metrics_instrumentation import (
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        app_metrics = get_application_metrics(telemetry_manager)
        if not app_metrics.is_active:
            return
        tokens_count = count_tokens()
        app_metrics.record_embedding_request(
            model=model,
            tokens_count=tokens_count,
            duration_seconds=duration_seconds,
            status=status,
        )
    except Exception as exc:  # never break the embedding call path
        logger.debug(f"Failed to record embedding metrics: {exc}")


def time_and_record_embedding_call(
    model: str,
    count_tokens: Callable[[], int],
    call_fn: Callable[[], T],
) -> T:
    """Call call_fn(), record a cidx.embedding.* metric, return its result.

    Args:
        model: Embedding model name to attribute the metric to.
        count_tokens: Zero-arg callable returning the token count for a
            SUCCESSFUL call. Never invoked on the error path.
        call_fn: Zero-arg callable performing the real outbound HTTP
            request (and its status validation) as one atomic unit.

    Returns:
        Whatever call_fn() returns, unchanged.

    Raises:
        Whatever call_fn() raises, unchanged -- this function never
        swallows or replaces the wrapped call's own exception.
    """
    start = time.monotonic()
    try:
        result = call_fn()
    except Exception:
        record_embedding_provider_call(
            model=model,
            duration_seconds=time.monotonic() - start,
            status="error",
            count_tokens=lambda: 0,
        )
        raise
    record_embedding_provider_call(
        model=model,
        duration_seconds=time.monotonic() - start,
        status="success",
        count_tokens=count_tokens,
    )
    return result
