"""Embedding/reranker call instrumentation wrapper -- Story #1418.

instrument_call() / instrument_call_async() are the single shared wrappers
used at every embedding-provider / reranker-client HTTP call site (10
injection points, see CLAUDE.md and
<<<<<<< HEAD
docs/architecture-invariants.md#embedding--reranker-call-tracking).
=======
docs/architecture/invariants/query-path.md#embedding-and-reranker-call-tracking).
>>>>>>> upstream/master
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
<<<<<<< HEAD
import time
from contextlib import contextmanager
from typing import Awaitable, Callable, Iterator, Optional, TypeVar

from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord
=======
import threading
import time
from contextlib import contextmanager
from typing import Awaitable, Callable, Iterator, Optional, Sequence, TypeVar

from code_indexer.server.services.embedding_call_stats import EmbeddingCallRecord

# Child-side module (src/code_indexer/services/run_spend_meter.py): the meter
# belongs to the `cidx index` child process, not to a server service.
from code_indexer.services.run_spend_meter import get_run_spend_meter
from code_indexer.server.services.embedding_stats_attribution import StatsAttribution
>>>>>>> upstream/master
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

<<<<<<< HEAD
# EVO-66113: process-level fallback tags for golden_repo_alias, job_id,
# node_id.  Set ONCE at child-process startup (via
# set_process_embed_stats_tags) from env vars threaded by the parent; read
# as defaults when instrument_call()/instrument_call_async() callers pass
# None (which is ALL existing embed-provider call sites -- they have no
# per-job context to thread).
_stats_golden_repo_alias: "contextvars.ContextVar[Optional[str]]" = (
    contextvars.ContextVar("_stats_golden_repo_alias", default=None)
)
_stats_job_id: "contextvars.ContextVar[Optional[str]]" = (
    contextvars.ContextVar("_stats_job_id", default=None)
)
_stats_node_id: "contextvars.ContextVar[Optional[str]]" = (
    contextvars.ContextVar("_stats_node_id", default=None)
)


def set_process_embed_stats_tags(
    *,
    golden_repo_alias: Optional[str] = None,
    job_id: Optional[str] = None,
    node_id: Optional[str] = None,
) -> None:
    """Set process-level fallback tags for all subsequent embed-stats records.

    Called once at child-process startup from env vars threaded by the parent
    server process (see embedding_stats_child_wiring.py).  Values persist for
    the child's entire lifetime -- a `cidx index` subprocess handles exactly
    one repo/job, so per-process scope is correct.
    """
    if golden_repo_alias:
        _stats_golden_repo_alias.set(golden_repo_alias)
    if job_id:
        _stats_job_id.set(job_id)
    if node_id:
        _stats_node_id.set(node_id)
=======

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


def _current_attribution() -> Optional[StatsAttribution]:
    with _process_default_attribution_lock:
        return _process_default_attribution
>>>>>>> upstream/master


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
<<<<<<< HEAD
        effective_purpose = _stats_purpose_override.get() or purpose
        # EVO-66113: fall back to process-level tags when the caller did not
        # pass explicit values (which is the normal case for all embed-
        # provider call sites -- they have no per-job context to thread).
        effective_alias = golden_repo_alias or _stats_golden_repo_alias.get()
        effective_job_id = job_id or _stats_job_id.get()
        effective_node_id = node_id or _stats_node_id.get()
=======
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
>>>>>>> upstream/master
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
<<<<<<< HEAD
            golden_repo_alias=effective_alias,
            job_id=effective_job_id,
            node_id=effective_node_id,
=======
            golden_repo_alias=golden_repo_alias,
            job_id=job_id,
            node_id=node_id,
>>>>>>> upstream/master
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
<<<<<<< HEAD
=======
    inputs: Optional[Sequence[str]] = None,
>>>>>>> upstream/master
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
<<<<<<< HEAD
=======
        inputs: The exact texts this attempt sends (Story #2122). An armed
            ``cidx index`` child's run-spend meter checks them BEFORE fn();
            a non-query call without them is refused there.
>>>>>>> upstream/master

    Returns:
        Whatever ``fn()`` returns, unchanged.

    Raises:
<<<<<<< HEAD
        Whatever ``fn()`` raises, unchanged -- instrument_call never
        swallows or replaces the wrapped call's own exception.
    """
=======
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
>>>>>>> upstream/master
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
<<<<<<< HEAD
=======
    inputs: Optional[Sequence[str]] = None,
>>>>>>> upstream/master
) -> T:
    """Async twin of instrument_call() -- awaits ``fn()`` instead of calling
    it synchronously. Identical contract otherwise (see instrument_call).
    """
<<<<<<< HEAD
=======
    get_run_spend_meter().charge(purpose=purpose, inputs=inputs)
>>>>>>> upstream/master
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
