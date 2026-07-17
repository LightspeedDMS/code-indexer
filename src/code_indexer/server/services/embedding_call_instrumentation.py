"""Embedding/reranker call instrumentation wrapper -- Story #1418.

instrument_call() / instrument_call_async() are the single shared wrappers
used at every embedding-provider / reranker-client HTTP call site (10
injection points, see CLAUDE.md and
docs/architecture-invariants.md#embedding--reranker-call-tracking).
``fn`` MUST be the
smallest unit of work that includes BOTH the outbound network call AND its
status validation (``raise_for_status()`` or equivalent) -- never just the
bare transport call. Otherwise a vendor 4xx/5xx that raises AFTER the
wrapped call "returns" (e.g. because raise_for_status() is invoked by the
CALLER, not inside the callee) would be misrecorded as success=True.

Fail-open: a failure in the stats-recording path itself (writer.record()
raising) is caught and logged -- it NEVER masks or replaces the wrapped
call's own return value or exception. This is observability-only
side-channel code; it must never affect indexing/query/rerank behavior.

instrument_call_async() is the async twin needed by
api_key_management.py's async VoyageAI/Cohere connectivity-test methods
(injection points 9/10) -- identical contract, awaits an async ``fn``.
"""

from __future__ import annotations

import contextvars
import logging
import time
from contextlib import contextmanager
from typing import Awaitable, Callable, Iterator, Optional, TypeVar

from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord
from code_indexer.server.services.embedding_stats_writer import EmbeddingStatsWriter

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Optional stats-purpose override, scoped via context manager -- lets a
# caller several layers ABOVE instrument_call()/instrument_call_async()
# retag the recorded purpose without threading a new parameter through the
# provider call chain (voyage_ai.py / cohere_embedding.py / the abstract
# EmbeddingProvider interface). The single current use case (Story #1418
# LOW-2 finding): embedding_cache_audit.py's "on"-mode cache-shadow-audit
# re-embed deliberately reuses the ordinary query-embedding code path,
# which hardcodes purpose="query" from an internal retry flag -- with no
# override active, this stays byte-identical to plain purpose="query".
_stats_purpose_override: "contextvars.ContextVar[Optional[str]]" = (
    contextvars.ContextVar("_stats_purpose_override", default=None)
)


@contextmanager
def stats_purpose_override(purpose: str) -> Iterator[None]:
    """Temporarily override the ``purpose`` tag recorded by every
    instrument_call()/instrument_call_async() invocation made within this
    context, regardless of the caller-supplied ``purpose`` kwarg. Resets to
    the previous value (None outside any nesting) on exit, even on
    exception."""
    token = _stats_purpose_override.set(purpose)
    try:
        yield
    finally:
        _stats_purpose_override.reset(token)


def _record_call(
    *,
    provider: str,
    call_type: str,
    model: str,
    item_count: int,
    token_count: int,
    batch_size: int,
    purpose: str,
    success: bool,
    latency_ms: int,
    golden_repo_alias: Optional[str],
    job_id: Optional[str],
    node_id: Optional[str],
) -> None:
    """Build an EmbeddingCallRecord and hand it to the active writer.

    Fail-open: any exception here (invalid record shape, writer failure) is
    caught and logged -- callers rely on this NEVER raising.
    """
    try:
        effective_purpose = _stats_purpose_override.get() or purpose
        record = EmbeddingCallRecord(
            provider=provider,
            call_type=call_type,
            model=model,
            item_count=item_count,
            token_count=token_count,
            batch_size=batch_size,
            purpose=effective_purpose,
            success=success,
            latency_ms=latency_ms,
            occurred_at=time.time(),
            golden_repo_alias=golden_repo_alias,
            job_id=job_id,
            node_id=node_id,
        )
        EmbeddingStatsWriter.get_active().record(record)
    except Exception as exc:  # fail-open: never mask the real result/exception
        logger.warning("embedding stats recording failed: %s", exc)


def instrument_call(
    *,
    provider: str,
    call_type: str,
    model: str,
    item_count: int,
    token_count: int,
    batch_size: int,
    purpose: str,
    fn: Callable[[], T],
    golden_repo_alias: Optional[str] = None,
    job_id: Optional[str] = None,
    node_id: Optional[str] = None,
) -> T:
    """Execute ``fn`` and record exactly one EmbeddingCallRecord for it.

    Args:
        provider: "voyageai" | "cohere".
        call_type: "embed" | "embed_multimodal" | "rerank".
        model: Vendor model name actually used for the call.
        item_count: Number of items (texts/documents) in this call.
        token_count: Token count for this call (0 when not applicable/known).
        batch_size: Batch size sent to the vendor for this call.
        purpose: "index" | "refresh" | "query" | "temporal" | "key_test" |
            "cache_shadow_audit".
        fn: Zero-arg callable performing BOTH the network call and its
            status validation (e.g. ``response.raise_for_status()``) as one
            atomic unit -- this is what determines success/failure.
        golden_repo_alias: Optional correlation field.
        job_id: Optional correlation field.
        node_id: Optional correlation field (cluster node id).

    Returns:
        Whatever ``fn()`` returns, unchanged.

    Raises:
        Whatever ``fn()`` raises, unchanged -- instrument_call never
        swallows or replaces the wrapped call's own exception.
    """
    start = time.time()
    success = False
    try:
        result = fn()
        success = True
        return result
    finally:
        latency_ms = int((time.time() - start) * 1000)
        _record_call(
            provider=provider,
            call_type=call_type,
            model=model,
            item_count=item_count,
            token_count=token_count,
            batch_size=batch_size,
            purpose=purpose,
            success=success,
            latency_ms=latency_ms,
            golden_repo_alias=golden_repo_alias,
            job_id=job_id,
            node_id=node_id,
        )


async def instrument_call_async(
    *,
    provider: str,
    call_type: str,
    model: str,
    item_count: int,
    token_count: int,
    batch_size: int,
    purpose: str,
    fn: Callable[[], Awaitable[T]],
    golden_repo_alias: Optional[str] = None,
    job_id: Optional[str] = None,
    node_id: Optional[str] = None,
) -> T:
    """Async twin of instrument_call() -- awaits ``fn()`` instead of calling
    it synchronously. Identical contract otherwise (see instrument_call).
    """
    start = time.time()
    success = False
    try:
        result = await fn()
        success = True
        return result
    finally:
        latency_ms = int((time.time() - start) * 1000)
        _record_call(
            provider=provider,
            call_type=call_type,
            model=model,
            item_count=item_count,
            token_count=token_count,
            batch_size=batch_size,
            purpose=purpose,
            success=success,
            latency_ms=latency_ms,
            golden_repo_alias=golden_repo_alias,
            job_id=job_id,
            node_id=node_id,
        )
