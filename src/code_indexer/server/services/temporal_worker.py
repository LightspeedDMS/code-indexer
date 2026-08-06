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

SNAPSHOT WRITES
---------------
An INITIAL empty snapshot is written before fusion starts so an early poll
(Scenario 14: zero-shard/PENDING) sees a real, empty snapshot rather than a
missing key. Checkpoint debounce (Bug #1181 not reopened): on_shard_complete
writes are time-debounced (the first checkpoint always writes immediately
since _last_write starts at 0.0; later ones only once
CHECKPOINT_MIN_GAP_SECONDS has elapsed). Intermediate write failures are
logged and skipped, and do NOT advance the debounce marker, so the next tick
retries. The FINAL write is unconditional (always attempted regardless of
debounce state) and job-fatal on verification failure -- never report
completed without a durably-verified final snapshot.

WORKER KWARGS
-------------
progress_callback is accepted but never driven -- its PRESENCE alone is what
routes this worker through BGM's hard-bound direct-call branch (CRITICAL 2).
query_tracker (Bug #1482) and activated_repo_manager (Bug #1533) are plain
forwarded kwargs, named explicitly rather than BGM-injected, passed by
execute_live_temporal_search's submit_job(). Both are deliberately NOT part
of TemporalWorkerInput: neither must ever enter the dedup signature.
activated_repo_manager is the server's DI-wired ActivatedRepoManager and is
REQUIRED in postgres/cluster mode (see LINEAGE STORE SELECTION below).

query_tracker is CURRENTLY UNUSED (Bug #1529): its only consumer was the
retired TemporalShardResolver -- a shard's path is fixed from first creation,
so there is no pointer swap to pin against. It is retained only because
callers still pass it, and is slated for removal with the Story #1457
modules. Do NOT reintroduce a resolver here to "use" it.

FIXED TEMPORAL LOCATION (Bug #1529)
-----------------------------------
Temporal data lives at ONE fixed path derived from the GOLDEN alias, outside
any repo's own cloned tree, so the store that performs the search must be
rooted there at CONSTRUCTION time -- which is why the golden lineage is
resolved BEFORE the backend is built. Rooting it at worker_input.repo_path
(an ACTIVATION's CoW clone) is the defect #1529 was filed for, and this
worker is the LIVE MCP temporal front door (Story #1400 replaced
_execute_temporal_query for that path), so leaving it in-repo half-wires the
primary read path. Only GENUINE ABSENCE of golden lineage yields
temporal_index_dir=None (the legacy in-repo derivation); every failure --
lineage lookup, store selection, fixed-root derivation -- propagates and
fails the job instead of quietly reading the clone. An all-None context is
exactly what sends the caller to the clone, so it must never be produced by
an error (finding #2).

LINEAGE STORE SELECTION (Bug #1533)
-----------------------------------
Resolving that golden lineage means reading activated-repo metadata, and
WHICH STORE is read is itself a correctness decision. This worker used to
construct its own standalone ``ActivatedRepoManager(data_dir=...)``: outside
the DI chain, so never wired to the shared PostgreSQL backend, so every read
hit NODE-LOCAL metadata. On a clustered deployment the real activation row
lives in the shared ``activated_repos`` table and the node-local store is
empty, so the lookup returned None -- which legitimately means "no golden
lineage" -- producing the all-None context, reading the activation's CoW
clone, which (correctly, per #1529) holds no temporal data. Net effect: HTTP
200, ZERO results, nothing logged as wrong, and invisible on solo/SQLite
where the node-local store IS the shared store. The same standalone
manager's ``.golden_repo_manager`` likewise read node-local golden metadata,
raising GoldenRepoNotFoundError on every temporal query and silently
degrading embedder selection to the clone's stale config.

``_resolve_lineage_repo_manager`` fixes both: prefer the injected DI-wired
manager, and in postgres/cluster mode REFUSE to read a node-local store at
all. "I looked in the wrong store" is not an answer -- it fails loudly, the
same principle #1529 finding #2 established for a lookup that raises.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

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

_UNWIRED_MANAGER_MESSAGE = (
    "temporal lineage lookup: storage_mode=postgres but the provided "
    "ActivatedRepoManager has no shared (PostgreSQL) metadata store wired -- "
    "refusing to read node-local metadata, which would misreport a "
    "cluster-wide activation as absent and silently return zero temporal "
    "results"
)

_NO_MANAGER_MESSAGE = (
    "temporal lineage lookup: storage_mode=postgres but no DI-wired "
    "ActivatedRepoManager was provided -- a standalone instance reads "
    "NODE-LOCAL metadata, which is empty for repos activated on any other "
    "node and would silently return zero temporal results"
)


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
    loading/refresh genuinely fails. This returns None ONLY for real absence
    and lets a failure propagate; laundering the exception into None
    reintroduces the whole hazard (module docstring).

    Bug #1533: that distinction only means anything if the store being read
    is the RIGHT one, which is the caller's decision -- a manager reading
    node-local metadata on a cluster node answers None for a repo that
    genuinely IS activated, and nothing here can tell that apart from real
    absence.

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


class TemporalLineageStoreUnavailableError(RuntimeError):
    """No SHARED metadata store is available for the lineage lookup.

    Bug #1533 -- raised instead of returning an all-None context, which would
    silently read the activation's own CoW clone and answer zero results. See
    the module docstring's LINEAGE STORE SELECTION section.
    """


def _standalone_lineage_repo_manager() -> Any:
    """The legacy standalone manager, for solo/SQLite and pure-CLI callers.

    ``CIDX_SERVER_DATA_DIR`` is honored because a bare
    ``ActivatedRepoManager()`` resolves its data dir purely from
    ``Path.home()`` and would look in the wrong directory on any deployment
    whose data dir is not the OS default (Bug #1517).
    """
    from code_indexer.server.repositories.activated_repo_manager import (
        ActivatedRepoManager,
    )

    env_server_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
    data_dir = (
        str(Path(env_server_dir) / "data")
        if env_server_dir
        else str(Path.home() / ".cidx-server" / "data")
    )
    return ActivatedRepoManager(data_dir=data_dir)


def _resolve_lineage_repo_manager(activated_repo_manager: Optional[Any]) -> Any:
    """Select the manager whose metadata store the lineage lookup may read.

    Bug #1533, deliberate ordering: an INJECTED (DI-wired) manager wins, but
    only after confirming it really reads the shared store on a cluster node;
    otherwise postgres/cluster mode RAISES; otherwise -- solo/SQLite or pure
    CLI, where the node-local store IS the real store -- the legacy
    standalone construction.

    Raises:
        TemporalLineageStoreUnavailableError: cluster mode with no
            shared-store-backed manager available to read.
    """
    from code_indexer.server.utils.registry_factory import is_postgres_storage_mode

    postgres_mode = is_postgres_storage_mode()

    if activated_repo_manager is not None:
        if postgres_mode and not activated_repo_manager.uses_shared_metadata_store():
            raise TemporalLineageStoreUnavailableError(_UNWIRED_MANAGER_MESSAGE)
        return activated_repo_manager

    if postgres_mode:
        raise TemporalLineageStoreUnavailableError(_NO_MANAGER_MESSAGE)

    return _standalone_lineage_repo_manager()


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
    worker_input: TemporalWorkerInput,
    job_id: str,
    activated_repo_manager: Optional[Any] = None,
) -> _GoldenTemporalContext:
    """Resolve golden alias + the fixed temporal index dir. Not fail-open.

    An all-None context is returned ONLY for GENUINE ABSENCE of golden
    lineage; see the module docstring for both halves of the contract.
    ``activated_repo_manager`` may be None only outside postgres/cluster mode
    (``_resolve_lineage_repo_manager``).

    Raises:
        TemporalLineageStoreUnavailableError: cluster mode, no shared store.
        ValueError: the alias is known but its fixed root cannot be derived.
        Exception: any lineage-lookup failure propagates unchanged.
    """
    from code_indexer.services.temporal.temporal_server_paths import (
        server_temporal_index_root,
    )

    manager = _resolve_lineage_repo_manager(activated_repo_manager)
    golden_repo_alias = _resolve_golden_repo_alias(
        worker_input.username, worker_input.repository_alias, manager
    )

    if not golden_repo_alias:
        return _GoldenTemporalContext(None, manager, None)

    golden_repos_dir = Path(manager.activated_repos_dir).parent / "golden-repos"
    return _GoldenTemporalContext(
        golden_repo_alias,
        manager,
        server_temporal_index_root(golden_repos_dir, golden_repo_alias),
    )


def _apply_golden_temporal_config(
    config: Any,
    golden_repo_alias: Optional[str],
    activated_repo_manager: Optional[Any],
    job_id: str,
) -> Any:
    """Overlay the GOLDEN repo's own current `temporal` config onto `config`.

    Story #1461 salvage item 4 (MCP-path analog of the REST fix in
    semantic_query_manager._execute_temporal_query): embedder SELECTION must
    use the golden repo's OWN, CURRENT config -- never the activated CoW
    clone's point-in-time config.json snapshot. Entirely fail-open: any
    resolution failure returns `config` unchanged.

    Bug #1533: `load_golden_temporal_config` reads `.golden_repo_manager` off
    the passed manager, so this inherits that manager's store correctness.
    """
    if not golden_repo_alias or activated_repo_manager is None:
        return config
    try:
        golden_config = load_golden_temporal_config(
            golden_repo_alias, activated_repo_manager
        )
        if golden_config is None:
            return config
        return config.model_copy(update={"temporal": golden_config.temporal})
    except Exception:
        logger.warning(
            "temporal worker %s: golden-repo temporal config wiring failed "
            "(isolated, non-fatal); using clone-derived config as-is",
            job_id,
            exc_info=True,
        )
        return config


def _build_temporal_backend(
    worker_input: TemporalWorkerInput,
    golden_ctx: "_GoldenTemporalContext",
    job_id: str,
) -> Tuple[Any, Any, Any]:
    """Reconstruct the temporal backend rooted at the resolved location, with
    the golden repo's own temporal config applied."""
    config, index_path, vector_store = reconstruct_temporal_backend(
        Path(worker_input.repo_path),
        worker_input.repository_alias,
        temporal_index_dir=golden_ctx.temporal_index_dir,
    )
    config = _apply_golden_temporal_config(
        config, golden_ctx.alias, golden_ctx.activated_repo_manager, job_id
    )
    return config, index_path, vector_store


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


def _run_fusion(
    config: Any,
    index_path: Any,
    vector_store: Any,
    worker_input: TemporalWorkerInput,
    checkpointer: "_TemporalWorkerCheckpointer",
    cancel_check: Optional[Callable[[], bool]],
) -> Any:
    """Forward the worker's query parameters into the real fusion dispatch.

    Pure parameter forwarding -- no decisions of its own.
    """
    return execute_temporal_query_with_fusion(
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


def run_temporal_worker(
    worker_input: TemporalWorkerInput,
    payload_cache: PayloadCache,
    job_id: str,
    progress_callback: Optional[Callable[..., None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    query_tracker: Optional[Any] = None,
    activated_repo_manager: Optional[Any] = None,
) -> Dict[str, Any]:
    """BGM temporal-lane worker entry point (see the module docstring for the
    kwarg contract, snapshot-write rules, temporal-location and
    lineage-store rules).

    Returns:
        {"result_ready": True} on success.

    Raises:
        ValueError: worker_input/job_id missing, or the golden alias is known
            but its fixed temporal root cannot be derived.
        TemporalLineageStoreUnavailableError: cluster mode, no shared store.
        ActivatedRepoError: the lineage lookup itself failed (propagated).
        TemporalSnapshotPersistenceError: unverified FINAL snapshot write.
        InterruptedError: cancel_check() returned True during fusion.
    """
    if worker_input is None:
        raise ValueError("run_temporal_worker: worker_input is required")
    if not job_id:
        raise ValueError("run_temporal_worker: job_id is required")

    ctx = _build_ctx(worker_input)
    golden_ctx = _resolve_golden_temporal_context(
        worker_input, job_id, activated_repo_manager=activated_repo_manager
    )
    config, index_path, vector_store = _build_temporal_backend(
        worker_input, golden_ctx, job_id
    )

    store_temporal_snapshot(
        payload_cache, job_id, _snapshot_payload([], 0, None, ctx), terminal=False
    )

    checkpointer = _TemporalWorkerCheckpointer(
        payload_cache, job_id, worker_input.repository_alias, ctx
    )
    final = _run_fusion(
        config, index_path, vector_store, worker_input, checkpointer, cancel_check
    )

    qr_final = _to_dicts(final.results, worker_input.repository_alias)
    store_temporal_snapshot(
        payload_cache,
        job_id,
        _snapshot_payload(qr_final, final.shards_total, final.shards_total, ctx),
        terminal=True,
    )
    return {"result_ready": True}
