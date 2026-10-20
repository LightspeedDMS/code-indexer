"""Embedding/reranker call instrumentation wrapper -- Story #1418.

instrument_call() / instrument_call_async() are the single shared wrappers
used at every embedding-provider / reranker-client HTTP call site (10
injection points, see CLAUDE.md and
docs/architecture/invariants/query-path.md#embedding-and-reranker-call-tracking).
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

token_count is what the vendor billed, read AFTER fn() succeeds by the call
site's ``tokens_from_result`` extractor -- one shared parser per vendor
(voyage_usage_tokens, cohere_billed_input_tokens, cohere_rerank_billed_tokens),
never the local tokenizer. A failed call records 0; a successful response
without a readable usage figure records 0 and logs a rate-limited WARNING.

instrument_call_async() is the async twin needed by
api_key_management.py's async VoyageAI/Cohere connectivity-test methods
(injection points 9/10) -- identical contract, awaits an async ``fn``.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Iterator,
    Optional,
    Sequence,
    TypeVar,
)

from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

if TYPE_CHECKING:
    import httpx

# Child-side module (src/code_indexer/services/run_spend_meter.py): the meter
# belongs to the `cidx index` child process, not to a server service.
from code_indexer.services.run_spend_meter import get_run_spend_meter
from code_indexer.server.services.embedding_stats_attribution import StatsAttribution
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


_process_default_attribution: Optional[StatsAttribution] = None
_process_default_attribution_lock = threading.Lock()


def set_process_default_attribution(attribution: StatsAttribution) -> None:
    """Install the PROCESS-WIDE default attribution (Story #2118).

    Called once by a `cidx index` child from its ``--stats-attribution``
    argument. Process-wide rather than a contextvar because the child's
    embedding calls run on VectorCalculationManager pool threads."""
    global _process_default_attribution
    with _process_default_attribution_lock:
        _process_default_attribution = attribution


def clear_process_default_attribution() -> None:
    """Remove the process-wide default attribution."""
    global _process_default_attribution
    with _process_default_attribution_lock:
        _process_default_attribution = None


#: The provider call sites' label for indexing work; it takes the
#: attribution purpose.
_GENERIC_INDEX_PURPOSE = "index"
#: The contextual temporal embedder's label; in a refresh child it becomes
#: _TEMPORAL_REFRESH_PURPOSE.
_TEMPORAL_PURPOSE = "temporal"
_REFRESH_PURPOSE = "refresh"
_TEMPORAL_REFRESH_PURPOSE = "temporal_refresh"


def _usage_int(body: Any, *path: str) -> Optional[int]:
    """The non-negative int at ``path`` in a vendor JSON body, else None."""
    value = body
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def voyage_usage_tokens(response: "httpx.Response") -> Optional[int]:
    """Voyage-reported ``usage.total_tokens`` of a successful response
    (embeddings, contextualized, multimodal and rerank endpoints); None when
    the response carries no readable figure."""
    return _usage_int(response.json(), "usage", "total_tokens")


def cohere_billed_input_tokens(response: "httpx.Response") -> Optional[int]:
    """Cohere-billed ``meta.billed_units.input_tokens`` of a successful
    ``/v2/embed`` response (text and multimodal); None when absent. Image
    tokens are billed separately and not counted here."""
    return _usage_int(response.json(), "meta", "billed_units", "input_tokens")


def cohere_rerank_billed_tokens(response: "httpx.Response") -> int:
    """Cohere ``/v2/rerank`` input tokens when reported, else 0.

    Rerank is billed in ``search_units``, which are not tokens and are never
    stored in token_count; an absent token figure is therefore expected and
    not a missing-usage case."""
    tokens = cohere_billed_input_tokens(response)
    return 0 if tokens is None else tokens


def _current_attribution() -> Optional[StatsAttribution]:
    with _process_default_attribution_lock:
        return _process_default_attribution


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
        attribution = _current_attribution()
        if attribution is not None:
            # Explicit correlation args always win; the attribution fills
            # only what the call site left unset. The generic indexing label
            # "index" takes the attribution purpose ("index" or "refresh");
            # in a REFRESH child the contextual temporal label "temporal"
            # becomes "temporal_refresh", so refresh-time temporal spend is
            # countable apart from registration-time temporal spend. Every
            # other label ("query", "key_test", ...) is kept.
            if golden_repo_alias is None:
                golden_repo_alias = attribution.golden_repo_alias
            if job_id is None:
                job_id = attribution.job_id
            if node_id is None:
                node_id = attribution.node_id
            if purpose == _GENERIC_INDEX_PURPOSE:
                purpose = attribution.purpose
            elif (
                purpose == _TEMPORAL_PURPOSE and attribution.purpose == _REFRESH_PURPOSE
            ):
                purpose = _TEMPORAL_REFRESH_PURPOSE
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


#: Minimum gap between two missing-usage WARNINGs for one (provider,
#: call_type, model, reason); the check runs on the indexing hot path.
_USAGE_WARNING_INTERVAL_SECONDS = 600.0
_usage_warning_last: "dict[tuple[str, str, str, str], float]" = {}
_usage_warning_lock = threading.Lock()


def _warn_usage_unavailable(
    provider: str, call_type: str, model: str, reason: str
) -> None:
    """Log that a successful call's vendor usage was not recorded, at most
    once per (provider, call_type, model, reason) per interval."""
    key = (provider, call_type, model, reason)
    now = time.monotonic()
    with _usage_warning_lock:
        last = _usage_warning_last.get(key)
        if last is not None and now - last < _USAGE_WARNING_INTERVAL_SECONDS:
            return
        _usage_warning_last[key] = now
    logger.warning(
        "embedding stats: vendor token usage %s for provider=%s call_type=%s "
        "model=%s; token_count recorded as 0",
        reason,
        provider,
        call_type,
        model,
    )


def _tokens_for_success(
    result: T,
    tokens_from_result: Callable[[T], Optional[int]],
    provider: str,
    call_type: str,
    model: str,
) -> int:
    """The vendor token count of a successful call; fail-open to 0.

    A None from the extractor means the response carried no readable usage
    figure; that, like an extractor error, records 0 and warns."""
    try:
        tokens = tokens_from_result(result)
    except Exception as exc:  # fail-open: never raise into indexing/query
        _warn_usage_unavailable(
            provider, call_type, model, f"unreadable ({type(exc).__name__})"
        )
        return 0
    if tokens is None:
        _warn_usage_unavailable(provider, call_type, model, "missing")
        return 0
    return tokens


def instrument_call(
    *,
    provider: str,
    call_type: str,
    model: str,
    item_count: int,
    tokens_from_result: Callable[[T], Optional[int]],
    batch_size: int,
    purpose: str,
    fn: Callable[[], T],
    golden_repo_alias: Optional[str] = None,
    job_id: Optional[str] = None,
    node_id: Optional[str] = None,
    inputs: Optional[Sequence[str]] = None,
) -> T:
    """Execute ``fn`` and record exactly one EmbeddingCallRecord for it.

    Args:
        provider: "voyageai" | "cohere".
        call_type: "embed" | "embed_multimodal" | "rerank".
        model: Vendor model name actually used for the call.
        item_count: Number of items (texts/documents) in this call.
        tokens_from_result: Called with ``fn()``'s return value, only after
            ``fn()`` succeeded; returns the vendor-reported token count
            recorded as ``token_count``. A failed call records 0 (not
            billed) and this is not called. A None return (no readable
            usage figure) or an exception records 0 and logs a rate-limited
            WARNING; neither ever reaches the caller.
        batch_size: Batch size sent to the vendor for this call.
        purpose: "index" | "refresh" | "query" | "temporal" | "key_test" |
            "cache_shadow_audit".
        fn: Zero-arg callable performing BOTH the network call and its
            status validation (e.g. ``response.raise_for_status()``) as one
            atomic unit -- this is what determines success/failure.
        golden_repo_alias: Optional correlation field.
        job_id: Optional correlation field.
        node_id: Optional correlation field (cluster node id).
        inputs: The exact texts this attempt sends (Story #2122). An armed
            ``cidx index`` child's run-spend meter checks them BEFORE fn();
            a non-query call without them is refused there.

    Returns:
        Whatever ``fn()`` returns, unchanged.

    Raises:
        RunSpendDefect: before fn(), when the armed run-spend meter refuses
            the attempt (Story #2122); fn() is never called.
        Whatever ``fn()`` raises, unchanged -- instrument_call never
        swallows or replaces the wrapped call's own exception.
    """
    # Story #2122: the indexing child's run-spend meter is charged BEFORE
    # fn(), so a refused input never reaches HTTP. Outside an armed child,
    # and for purpose "query", this is a no-op. Not fail-open: a refusal
    # raises RunSpendDefect and no stats record is written (no attempt).
    get_run_spend_meter().charge(purpose=purpose, inputs=inputs)
    start = time.time()
    success = False
    token_count = 0
    try:
        result = fn()
        success = True
        token_count = _tokens_for_success(
            result, tokens_from_result, provider, call_type, model
        )
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
    tokens_from_result: Callable[[T], Optional[int]],
    batch_size: int,
    purpose: str,
    fn: Callable[[], Awaitable[T]],
    golden_repo_alias: Optional[str] = None,
    job_id: Optional[str] = None,
    node_id: Optional[str] = None,
    inputs: Optional[Sequence[str]] = None,
) -> T:
    """Async twin of instrument_call() -- awaits ``fn()`` instead of calling
    it synchronously. Identical contract otherwise (see instrument_call).
    """
    get_run_spend_meter().charge(purpose=purpose, inputs=inputs)
    start = time.time()
    success = False
    token_count = 0
    try:
        result = await fn()
        success = True
        token_count = _tokens_for_success(
            result, tokens_from_result, provider, call_type, model
        )
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
