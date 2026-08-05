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
from typing import Any, Callable, Dict, List, NamedTuple, Optional

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
    alias, resolved without any lookup.

    Bug #1529 finding #2: ABSENCE and FAILURE are NOT the same answer, and
    ``get_repository`` already distinguishes them -- it returns None when
    there is no activated-repo record, and raises only when metadata
    loading/refresh genuinely fails. This function therefore returns None
    ONLY for real absence, and lets a failure propagate. Swallowing the
    exception into None was enough to reintroduce the entire hazard: None
    produces an all-None temporal context, which sends the caller to
    ``reconstruct_temporal_backend(repo_path, ...)`` -- the ACTIVATION'S own
    CoW clone -- silently serving frozen-at-clone-time data. "I could not
    determine the lineage" must never be reported as "there is no lineage".

    Raises:
        Whatever ``get_repository`` raises (e.g. ActivatedRepoError) when the
        lookup genuinely fails.
    """
    if repository_alias.endswith("-global"):
        return repository_alias
    repo_info = activated_repo_manager.get_repository(
        username, repository_alias, touch=False
    )
    if not repo_info:
        return None
    golden_alias = repo_info.get("golden_repo_alias")
    return golden_alias if isinstance(golden_alias, str) and golden_alias else None


class _GoldenTemporalContext(NamedTuple):
    """The golden-repo lineage this worker needs BEFORE it builds a backend.

    Bug #1529: `temporal_index_dir` must be known at backend-construction
    time, because that is the only point at which the store performing the
    search can be rooted at the fixed, outside-the-repo temporal location.
    """

    alias: Optional[str]
    activated_repo_manager: Optional[Any]
    temporal_index_dir: Optional[Path]




def _resolve_golden_temporal_context(
    worker_input: TemporalWorkerInput, job_id: str
) -> _GoldenTemporalContext:
    """Resolve golden alias + the fixed temporal index dir.

    Fail-open ONLY for the lineage LOOKUP: if the golden alias cannot be
    determined (composite repo, no activated-repo record, backend error) the
    context is all-None and the caller keeps the legacy in-repo derivation.

    Bug #1529 finding #2: once the alias IS known, deriving the fixed root is
    NOT fail-open. It happens OUTSIDE the try below, so a failure propagates
    and fails the job. Swallowing it returned an all-None context, which sent
    the caller on to `reconstruct_temporal_backend(repo_path, ...)` -- the
    ACTIVATION'S own CoW clone -- silently serving the frozen-at-clone-time
    duplicate this bug exists to eliminate. This seam is the LIVE MCP
    temporal front door (Story #1400), so a silent local fallback here is the
    primary read path being wrong.

    The discriminator deliberately is NOT `CIDX_SERVER_REFRESH_CONTEXT`: that
    marker is injected only into the temporal CHILD SUBPROCESS and is absent
    from this (server-side) process, so gating on it would be inert.

    Raises:
        ValueError: when the golden alias is known but its fixed temporal
            root cannot be derived.
    """
    # Bug #1529 finding #2: NO fail-open wrapper here. A failure in any of
    # these steps (import, data-dir derivation, manager construction, lineage
    # lookup) is "I could not determine the lineage", NOT "there is no
    # lineage" -- and returning an all-None context for it is precisely what
    # sends the caller to the activation's own CoW clone.
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )
    from code_indexer.services.temporal.temporal_server_paths import (
        server_temporal_index_root,
    )

    # Bug #1517: a bare, no-arg ActivatedRepoManager() resolves its OWN
    # default data_dir purely from Path.home(), never consulting
    # CIDX_SERVER_DATA_DIR -- the env var this codebase otherwise treats
    # as the canonical way to locate the server's configured data
    # directory for a standalone construction outside the normal DI chain
    # (see ActivatedRepoIndexManager.__init__). On any real deployment
    # where the server's data dir differs from the OS default, that
    # mismatch made this worker's internally-constructed
    # GoldenRepoManager look in the wrong metadata store, so
    # get_actual_repo_path() raised GoldenRepoNotFoundError for a golden
    # repo that genuinely exists elsewhere -- and would now ALSO resolve
    # the wrong temporal location.
    _env_server_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
    _worker_data_dir = (
        str(Path(_env_server_dir) / "data")
        if _env_server_dir
        else str(Path.home() / ".cidx-server" / "data")
    )
    activated_repo_manager = ActivatedRepoManager(data_dir=_worker_data_dir)
    golden_repo_alias = _resolve_golden_repo_alias(
        worker_input.username,
        worker_input.repository_alias,
        activated_repo_manager,
    )

    if not golden_repo_alias:
        return _GoldenTemporalContext(None, activated_repo_manager, None)

    # Bug #1529 finding #2: deliberately OUTSIDE the fail-open try above. The
    # alias is known, so the fixed root is the only correct location -- a
    # failure here must fail the job, never silently degrade to the
    # activation's own clone.
    golden_repos_dir = (
        Path(activated_repo_manager.activated_repos_dir).parent / "golden-repos"
    )
    return _GoldenTemporalContext(
        golden_repo_alias,
        activated_repo_manager,
        server_temporal_index_root(golden_repos_dir, golden_repo_alias),
    )


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
    injected -- passed by execute_live_temporal_search's submit_job() call.
    Deliberately NOT part of TemporalWorkerInput: it must never enter the
    dedup signature.

    query_tracker is CURRENTLY UNUSED (Bug #1529): its only consumer was the
    TemporalShardResolver this worker used to construct, and that resolver is
    retired -- a temporal shard's path is now fixed from first creation, so
    there is no pointer swap to pin against. The parameter is retained only
    because callers still pass it; it is slated for removal together with the
    Story #1457 sister-location modules (see CLAUDE.md's Bug #1529 section,
    "NOT YET DONE"). Do NOT reintroduce a resolver here to "use" it.

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

    # Bug #1529: the golden repo's identity must be resolved BEFORE the
    # backend is reconstructed. Temporal data lives at ONE fixed path derived
    # from the golden alias, outside any repo's own cloned tree, so the store
    # has to be rooted there at construction time -- rooting it at
    # worker_input.repo_path (an ACTIVATION's CoW clone) is precisely the
    # defect #1529 was filed for, and this worker is the LIVE MCP temporal
    # front door (Story #1400 replaced _execute_temporal_query for that
    # path), so leaving it in-repo would half-wire the primary read path.
    # Fail-open throughout: no golden lineage -> temporal_index_dir is None
    # and the legacy in-repo derivation is used, unchanged.
    _golden_alias_ctx = _resolve_golden_temporal_context(worker_input, job_id)
    golden_repo_alias = _golden_alias_ctx.alias
    _activated_repo_manager = _golden_alias_ctx.activated_repo_manager

    config, index_path, vector_store = reconstruct_temporal_backend(
        Path(worker_input.repo_path),
        worker_input.repository_alias,
        temporal_index_dir=_golden_alias_ctx.temporal_index_dir,
    )

    # Story #1461 salvage item 4 (MCP-path analog of the REST fix in
    # semantic_query_manager._execute_temporal_query): embedder SELECTION
    # must use the GOLDEN repo's OWN, CURRENT config -- never the activated
    # CoW clone's point-in-time config.json snapshot. Entirely fail-open:
    # any resolution failure leaves `config` (and thus behavior) unchanged.
    #
    if golden_repo_alias and _activated_repo_manager is not None:
        try:
            golden_config = load_golden_temporal_config(
                golden_repo_alias, _activated_repo_manager
            )
            if golden_config is not None:
                config = config.model_copy(update={"temporal": golden_config.temporal})
        except Exception:
            logger.warning(
                "temporal worker %s: golden-repo temporal config wiring "
                "failed (isolated, non-fatal); using clone-derived config "
                "as-is",
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
