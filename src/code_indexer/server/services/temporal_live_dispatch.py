"""Story #1400: live submit-side dispatch for async-hybrid temporal queries.

The ONE shared entry point BOTH search_code (MCP) and POST /api/query
(REST) call for the temporal branch -- replaces the old fully-synchronous
_execute_temporal_query call.

    build TemporalWorkerInput (caller's job -- via the adapters)
    -> compute dedup signature (canonical_signature over the query's
       logically-identifying fields)
    -> single-flight join an in-flight identical query on THIS node via
       TemporalDedupCache.get_or_submit (or submit a new BGM lane="temporal"
       job)
    -> foreground-wait, deadline-aware: waiter_deadline =
       min(now + inline_wait_seconds, response_deadline), where
       response_deadline = handler_deadline_monotonic - response_reserve_seconds
       (CRITICAL 5) -- never polls past the outer protocol-level timeout
    -> return either the postprocessed inline "completed" result
       (Scenario 1/4/9) OR a "waiting" handoff dict (job_id +
       partial_results + continue_polling=True, Scenario 2/3/14)

Protocol-agnostic: returns a plain dict. The caller (search.py / REST route)
decides how to wrap it -- Scenario 1's "unchanged envelope, no job_id/
status/partial_results fields" only applies to the wire response the caller
builds, not to this function's own return contract (which always includes
job_id so the caller CAN build either shape).
"""

import logging
import time
from typing import Any, Callable, Dict, Optional

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.cache.payload_cache import PayloadCache
from code_indexer.server.services.temporal_dedup_cache import (
    TemporalDedupCache,
    TemporalDedupCapacityExhaustedError,
    canonical_signature,
    get_temporal_dedup_cache,
)
from code_indexer.server.services.temporal_poll_job_status import (
    poll_temporal_job_status,
)
from code_indexer.server.services.temporal_snapshot_store import (
    read_temporal_snapshot,
)
from code_indexer.server.services.temporal_worker import run_temporal_worker
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput

logger = logging.getLogger(__name__)

TEMPORAL_OPERATION_TYPE = "temporal_query"

# Locked design: "~50ms polling" -- short enough for a responsive foreground
# wait, long enough not to busy-loop the executor thread.
_POLL_INTERVAL_SECONDS = 0.05


def _worker_input_signature_dict(wi: TemporalWorkerInput) -> Dict[str, Any]:
    """The logically-identifying fields for dedup -- deliberately excludes
    repo_path (a resolution detail of repository_alias, not part of query
    identity) and provider_filter (always None today, no door exposes it).
    diff_types is already sorted/deduped by the adapter's canonicalization."""
    return {
        "username": wi.username,
        "repository_alias": wi.repository_alias,
        "query_text": wi.query_text,
        "requested_limit": wi.requested_limit,
        "fusion_fetch_limit": wi.fusion_fetch_limit,
        "time_range": list(wi.time_range),
        "time_range_all": wi.time_range_all,
        "file_path_filter": wi.file_path_filter,
        "at_commit": wi.at_commit,
        "language": wi.language,
        "exclude_language": wi.exclude_language,
        "exclude_path": wi.exclude_path,
        "diff_types": list(wi.diff_types) if wi.diff_types else None,
        "author": wi.author,
        "chunk_type": wi.chunk_type,
        "temporal_embedder": wi.temporal_embedder,
        "rerank_query": wi.rerank_query,
        "rerank_instruction": wi.rerank_instruction,
    }


def execute_live_temporal_search(
    worker_input: TemporalWorkerInput,
    background_job_manager: BackgroundJobManager,
    payload_cache: PayloadCache,
    access_filtering_service: Any,
    is_admin: bool,
    inline_wait_seconds: float,
    handler_deadline_monotonic: Optional[float],
    response_reserve_seconds: float,
    dedup_cache: Optional[TemporalDedupCache] = None,
    worker_fn: Callable[..., Dict[str, Any]] = run_temporal_worker,
    config_service: Optional[Any] = None,
) -> Dict[str, Any]:
    """Core protocol-agnostic async-hybrid temporal dispatch.

    Args:
        dedup_cache: injected for tests; production callers omit this and
            get the shared get_temporal_dedup_cache() singleton.
        worker_fn: injected for tests (a fast fake); production callers
            omit this and get the real run_temporal_worker.
        config_service: real ConfigService instance, forwarded to
            poll_temporal_job_status so a completed terminal read can
            actually invoke the real rerank wiring (postprocess_temporal_
            snapshot's terminal-only rerank step). None (default) keeps
            every read conservatively unranked=True.

    Returns:
        A dict with at least "status" ("completed"|"waiting"|"failed"|
        "not_found") and "job_id". "completed" carries "results"/
        "shards_completed"/"shards_total"/"unranked". "waiting" carries
        "partial_results"/"continue_polling"=True/"shards_completed"/
        "shards_total"/"unranked"=True. "failed"/"not_found" carry "error".

    Raises:
        Nothing -- TemporalDedupCapacityExhaustedError is caught and
        surfaced as a "capacity_exhausted" status dict instead.
    """
    if dedup_cache is None:
        dedup_cache = get_temporal_dedup_cache()

    signature = canonical_signature(_worker_input_signature_dict(worker_input))

    def _status_check(job_id: str) -> Optional[str]:
        status = background_job_manager.get_job_status(
            job_id, worker_input.username, is_admin
        )
        return status.get("status") if status else None

    def _submit() -> str:
        # Story #1400: repo_alias is deliberately OMITTED here. BGM's
        # register_job_if_no_conflict gate is a per-(operation_type,
        # repo_alias) uniqueness constraint -- passing repository_alias
        # would incorrectly reject a SECOND, entirely different temporal
        # query (different query_text/filters) against the same repo as a
        # "duplicate". Correct dedup granularity (full query signature) is
        # already enforced above by TemporalDedupCache; the BGM-level gate
        # is the wrong tool for this job type.
        new_job_id: str = background_job_manager.submit_job(
            TEMPORAL_OPERATION_TYPE,
            worker_fn,
            submitter_username=worker_input.username,
            is_admin=is_admin,
            lane="temporal",
            worker_input=worker_input,
            payload_cache=payload_cache,
        )
        return new_job_id

    try:
        job_id = dedup_cache.get_or_submit(signature, _status_check, _submit)
    except TemporalDedupCapacityExhaustedError as exc:
        return {
            "status": "capacity_exhausted",
            "job_id": None,
            "error": str(exc),
            "error_code": "TEMPORAL_DEDUP_CAPACITY_EXHAUSTED",
        }

    # CRITICAL 5: waiter_deadline = min(configured inline wait,
    # response_deadline). response_deadline reserves a grace budget for
    # everything AFTER the wait (snapshot read, post-processing,
    # serialization) so the waiter always returns before the outer
    # protocol-level asyncio.wait_for deadline fires with no job_id.
    now = time.monotonic()
    candidate_deadlines = [now + inline_wait_seconds]
    if handler_deadline_monotonic is not None:
        candidate_deadlines.append(
            handler_deadline_monotonic - response_reserve_seconds
        )
    waiter_deadline = min(candidate_deadlines)
    # The rerank deadline is the RESPONSE budget (handler deadline minus
    # the reserve), not the (possibly shorter) waiter_deadline -- a
    # completed read's post-processing still has the full reserve window
    # to work with, independent of how much of inline_wait_seconds the
    # wait loop itself consumed.
    response_deadline = (
        handler_deadline_monotonic - response_reserve_seconds
        if handler_deadline_monotonic is not None
        else None
    )

    def _read_snapshot() -> Optional[Dict[str, Any]]:
        snapshot: Optional[Dict[str, Any]] = read_temporal_snapshot(
            payload_cache, job_id
        )
        return snapshot

    while True:
        job_status = background_job_manager.get_job_status(
            job_id, worker_input.username, is_admin
        )
        result: Dict[str, Any] = poll_temporal_job_status(
            job_status,
            _read_snapshot,
            access_filtering_service,
            worker_input.username,
            is_admin,
            config_service=config_service,
            deadline_monotonic=response_deadline,
        )
        if result["status"] != "waiting":
            break
        if time.monotonic() >= waiter_deadline:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)

    result["job_id"] = job_id
    return result
