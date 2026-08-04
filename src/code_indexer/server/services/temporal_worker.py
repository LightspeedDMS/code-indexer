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
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from code_indexer.server.cache.payload_cache import PayloadCache
from code_indexer.server.query.semantic_query_manager import (
    convert_temporal_result_to_query_result,
    load_golden_temporal_config,
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


def _resolve_golden_repo_alias(
    username: str, repository_alias: str, activated_repo_manager: Any
) -> Optional[str]:
    """Resolve the underlying GOLDEN repo alias for a temporal worker query
    (Story #1461 salvage item 4, MCP-path analog of
    SemanticQueryManager._search_single_repository's own is_global/
    activated-repo distinction).

    An is_global repository_alias (ends with '-global') IS its own golden
    alias. A regular activated repo's golden alias is looked up via
    ActivatedRepoManager.get_repository. Fail-open: any lookup failure
    (repo not found, backend error) returns None, preserving today's
    clone-config behavior for that query.
    """
    if repository_alias.endswith("-global"):
        return repository_alias
    try:
        repo_info = activated_repo_manager.get_repository(
            username, repository_alias, touch=False
        )
    except Exception:
        logger.warning(
            "temporal worker: failed to resolve golden_repo_alias for "
            "activated repo '%s' (user=%s); using clone config as-is",
            repository_alias,
            username,
            exc_info=True,
        )
        return None
    if not repo_info:
        return None
    golden_alias = repo_info.get("golden_repo_alias")
    return golden_alias if isinstance(golden_alias, str) and golden_alias else None


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
    query_tracker: Optional[Any] = None,
) -> Dict[str, Any]:
    """BGM temporal-lane worker entry point.

    Submitted via BackgroundJobManager.submit_job(lane="temporal", ...);
    job_id/cancel_check are BGM-injected (CRITICAL 2); progress_callback is
    accepted (declared, not actively driven -- its PRESENCE alone routes
    this worker through BGM's hard-bound direct-call branch). query_tracker
    (Bug #1482) is a plain forwarded kwarg -- named explicitly, not BGM-
    injected -- passed by execute_live_temporal_search's submit_job() call
    so this worker can construct a resolution-scope-safe
    TemporalShardResolver. Deliberately NOT part of TemporalWorkerInput: it
    must never enter the dedup signature.

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

    # Story #1461 salvage item 4 (MCP-path analog of the REST fix in
    # semantic_query_manager._execute_temporal_query): embedder SELECTION
    # must use the GOLDEN repo's OWN, CURRENT config -- never the activated
    # CoW clone's point-in-time config.json snapshot. Entirely fail-open:
    # any resolution failure leaves `config` (and thus behavior) unchanged.
    #
    # Bug #1482: this try block ALSO constructs a TemporalShardResolver
    # (mirroring semantic_query_manager.py's now-retired
    # _execute_temporal_query wiring verbatim -- Story #1400 replaced that
    # path with this live worker, which never got the equivalent wiring,
    # so the live MCP temporal front door could only ever read the
    # in-repo legacy location, empty once Story #1457's AC1 relocation
    # trigger succeeds). Failure here is likewise entirely fail-open:
    # resolver stays None and behavior is unchanged (legacy-only
    # resolution, today's status quo).
    resolver: Optional[Any] = None
    try:
        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )

        # Bug #1517: a bare, no-arg ActivatedRepoManager() resolves its
        # OWN default data_dir purely from Path.home(), never consulting
        # CIDX_SERVER_DATA_DIR -- the env var this codebase otherwise
        # treats as the canonical way to locate the server's configured
        # data directory for a standalone construction outside the normal
        # DI chain (see ActivatedRepoIndexManager.__init__). On any real
        # deployment where the server's data dir differs from the OS
        # default, that mismatch made this worker's internally-
        # constructed GoldenRepoManager look in the wrong metadata store,
        # so get_actual_repo_path() raised GoldenRepoNotFoundError for a
        # golden repo that genuinely exists elsewhere -- fail-open (caught
        # below by load_golden_temporal_config), but permanently losing
        # Story #1461's "use the golden repo's own current config for
        # embedder selection" correctness fix and logging noise on every
        # temporal query.
        _env_server_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
        _worker_data_dir = (
            str(Path(_env_server_dir) / "data")
            if _env_server_dir
            else str(Path.home() / ".cidx-server" / "data")
        )
        _activated_repo_manager = ActivatedRepoManager(data_dir=_worker_data_dir)
        golden_repo_alias = _resolve_golden_repo_alias(
            worker_input.username,
            worker_input.repository_alias,
            _activated_repo_manager,
        )
        if golden_repo_alias:
            golden_config = load_golden_temporal_config(
                golden_repo_alias, _activated_repo_manager
            )
            if golden_config is not None:
                config = config.model_copy(update={"temporal": golden_config.temporal})

        # Gated on BOTH a known golden_repo_alias AND a real query_tracker
        # (exactly like semantic_query_manager.py:2649-2691) -- without a
        # tracker, pin() is a silent no-op, so constructing a resolver
        # anyway would reintroduce the mid-read deletion hazard AC8 Step 6
        # exists to prevent.
        if golden_repo_alias and query_tracker is not None:
            from code_indexer.global_repos.alias_manager import AliasManager
            from code_indexer.services.temporal.temporal_shard_resolver import (
                TemporalShardResolver,
            )

            golden_repos_dir = (
                Path(_activated_repo_manager.activated_repos_dir).parent
                / "golden-repos"
            )
            # Golden repo directories are never named with a '-global'
            # suffix -- that suffix is purely a query-facing alias-
            # registry convention (an is_global query passes its full
            # '-global'-suffixed alias as golden_repo_alias). Strip
            # exactly one trailing '-global' so the resolver's namespace
            # matches what the relocation trigger actually published
            # under.
            normalized_repo_alias = golden_repo_alias.removesuffix("-global")
            resolver = TemporalShardResolver(
                alias_manager=AliasManager(str(golden_repos_dir / "aliases")),
                repo_alias=normalized_repo_alias,
                sister_root=golden_repos_dir,
                legacy_index_path=index_path,
                query_tracker=query_tracker,
            )
            # "Disconnected reader" lesson (semantic_query_manager.py
            # code review CRITICAL #1): a resolver threaded ONLY into
            # execute_temporal_query_with_fusion's discovery/pin
            # bookkeeping never reaches the vector_store instance that
            # actually PERFORMS the search -- _get_collection_path()
            # would silently fall back to the legacy base_path/
            # collection_name path even after relocation moved the data.
            # Attach the resolver to the SAME store instance used for
            # search, preserving its existing caching/governor wiring.
            vector_store._temporal_shard_resolver = resolver
    except Exception:
        logger.warning(
            "temporal worker %s: golden-repo temporal config/resolver "
            "wiring failed (isolated, non-fatal); using clone-derived "
            "config and legacy-only resolution as-is",
            job_id,
            exc_info=True,
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
        resolver=resolver,
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
