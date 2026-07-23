"""The actual BGM temporal-lane worker -- Story #1400 Phase 8.

CRITICAL 2 (FINAL LOCKED DESIGN): this worker explicitly declares
job_id/progress_callback/cancel_check BY NAME so BGM's
inspect.signature-based injection (background_jobs.py._execute_job) routes
it through the hard-bound direct-call branch -- the pool slot is held
until this function actually returns, making temporal_lane_concurrency a
HARD bound, not a soft one susceptible to abandoned-thread overrun.

CRITICAL 4 (honest no-auto-resubmit contract): BackgroundJob persists
neither the worker callable nor its TemporalWorkerInput anywhere. If the
node running this worker restarts mid-flight, the job is orphaned and (per
the node-scoped cleanup this story also adds) eventually marked FAILED
with error_code TEMPORAL_NODE_RESTART. There is NO durable-resumption
mechanism -- the client must re-issue the original query as a brand-new
request. This worker does not attempt, and must never attempt, to persist
enough state to auto-resubmit itself.

Checkpoint debounce (Bug #1181 not reopened): on_shard_complete writes are
time-debounced (first checkpoint always writes immediately since
_last_write starts at 0.0; subsequent checkpoints only write once
CHECKPOINT_MIN_GAP_SECONDS has elapsed). The FINAL write is unconditional
and always attempted regardless of debounce state. Intermediate write
failures are logged and skipped (the debounce marker is NOT advanced on
failure, so the next tick retries) -- only the FINAL write's persistence
failure is job-fatal.
"""

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from code_indexer.server.cache.payload_cache import PayloadCache
from code_indexer.server.query.semantic_query_manager import (
    convert_temporal_result_to_query_result,
    reconstruct_temporal_backend,
)
from code_indexer.server.services.temporal_snapshot_store import (
    store_temporal_snapshot,
)
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput

logger = logging.getLogger(__name__)

CHECKPOINT_MIN_GAP_SECONDS = 2.0


def _build_ctx(worker_input: TemporalWorkerInput) -> Dict[str, Any]:
    return {
        "requested_limit": worker_input.requested_limit,
        "fusion_fetch_limit": worker_input.fusion_fetch_limit,
        "rerank_query": worker_input.rerank_query,
        "rerank_instruction": worker_input.rerank_instruction,
        "repository_alias": worker_input.repository_alias,
    }


def _to_dicts(temporal_results: Any, repository_alias: str) -> List[Dict[str, Any]]:
    return [
        convert_temporal_result_to_query_result(t, repository_alias).to_dict()
        for t in temporal_results
    ]


def _snapshot_payload(
    results: List[Dict[str, Any]],
    shards_completed: Any,
    shards_total: Any,
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "results": results,
        "shards_completed": shards_completed,
        "shards_total": shards_total,
        "ctx": ctx,
    }


class _TemporalWorkerCheckpointer:
    """Bundles the mutable checkpoint state + the two fusion callbacks so
    run_temporal_worker's body stays a short, linear sequence."""

    def __init__(
        self,
        payload_cache: PayloadCache,
        job_id: str,
        repository_alias: str,
        ctx: Dict[str, Any],
    ) -> None:
        self._payload_cache = payload_cache
        self._job_id = job_id
        self._repository_alias = repository_alias
        self._ctx = ctx
        self.shards_total: Optional[int] = None
        self._last_write = 0.0

    def on_shards_discovered(self, total: int) -> None:
        self.shards_total = total
        try:
            store_temporal_snapshot(
                self._payload_cache,
                self._job_id,
                _snapshot_payload([], 0, total, self._ctx),
                terminal=False,
            )
        except Exception:
            logger.warning(
                "temporal worker %s: on_shards_discovered snapshot write "
                "failed (isolated, non-fatal)",
                self._job_id,
                exc_info=True,
            )

    def on_shard_complete(
        self, attempted: int, succeeded: int, cumulative: list
    ) -> None:
        now = time.monotonic()
        if self._last_write != 0.0 and (
            now - self._last_write < CHECKPOINT_MIN_GAP_SECONDS
        ):
            return
        try:
            qr = _to_dicts(cumulative, self._repository_alias)
            store_temporal_snapshot(
                self._payload_cache,
                self._job_id,
                _snapshot_payload(qr, attempted, self.shards_total, self._ctx),
                terminal=False,
            )
            # Only advance the debounce marker on a VERIFIED successful
            # write -- a failure leaves it alone so the next tick retries.
            self._last_write = now
        except Exception:
            logger.warning(
                "temporal worker %s: checkpoint write failed (isolated, "
                "non-fatal; debounce marker not advanced, will retry)",
                self._job_id,
                exc_info=True,
            )


def run_temporal_worker(
    worker_input: TemporalWorkerInput,
    payload_cache: PayloadCache,
    job_id: str,
    progress_callback: Optional[Callable[..., None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """BGM temporal-lane worker entry point.

    Submitted via BackgroundJobManager.submit_job(lane="temporal", ...);
    job_id/cancel_check are BGM-injected (CRITICAL 2); progress_callback is
    accepted (declared, not actively driven -- its PRESENCE alone routes
    this worker through BGM's hard-bound direct-call branch).

    Returns:
        {"result_ready": True} on success.

    Raises:
        ValueError: worker_input or job_id is missing.
        TemporalSnapshotPersistenceError: the FINAL snapshot write could not
            be verified by read-back -- job-fatal, BGM records the job
            FAILED with error_code TEMPORAL_SNAPSHOT_PERSISTENCE_FAILED.
        InterruptedError: cancel_check() returned True during fusion
            (propagates from execute_temporal_query_with_fusion).
    """
    if worker_input is None:
        raise ValueError("run_temporal_worker: worker_input is required")
    if not job_id:
        raise ValueError("run_temporal_worker: job_id is required")

    ctx = _build_ctx(worker_input)
    config, index_path, vector_store = reconstruct_temporal_backend(
        Path(worker_input.repo_path), worker_input.repository_alias
    )

    # INITIAL empty snapshot -- written before fusion starts so an early
    # poll (Scenario 14: zero-shard/PENDING) sees a real, empty snapshot
    # rather than a missing key.
    store_temporal_snapshot(
        payload_cache,
        job_id,
        _snapshot_payload([], 0, None, ctx),
        terminal=False,
    )

    checkpointer = _TemporalWorkerCheckpointer(
        payload_cache, job_id, worker_input.repository_alias, ctx
    )

    final = execute_temporal_query_with_fusion(
        config,
        index_path,
        vector_store,
        worker_input.query_text,
        worker_input.fusion_fetch_limit,
        time_range=worker_input.time_range,
        file_path_filter=worker_input.file_path_filter,
        provider_filter=worker_input.provider_filter,
        at_commit=worker_input.at_commit,
        language=worker_input.language,
        exclude_language=worker_input.exclude_language,
        exclude_path=worker_input.exclude_path,
        diff_types=(list(worker_input.diff_types) if worker_input.diff_types else None),
        author=worker_input.author,
        chunk_type=worker_input.chunk_type,
        no_embedding_cache_shortcut=worker_input.no_embedding_cache_shortcut,
        temporal_embedder=worker_input.temporal_embedder,
        on_shards_discovered=checkpointer.on_shards_discovered,
        on_shard_complete=checkpointer.on_shard_complete,
        cancel_check=cancel_check,
    )

    qr_final = _to_dicts(final.results, worker_input.repository_alias)
    # FINAL write: unconditional (always attempted regardless of debounce
    # state) and job-fatal on verification failure -- never report
    # completed without a durably-verified final snapshot.
    store_temporal_snapshot(
        payload_cache,
        job_id,
        _snapshot_payload(qr_final, final.shards_total, final.shards_total, ctx),
        terminal=True,
    )
    return {"result_ready": True}
