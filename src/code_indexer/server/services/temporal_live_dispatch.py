"""Story #1400: live submit-side dispatch for async-hybrid temporal queries.

The ONE shared entry point BOTH search_code (MCP) and POST /api/query
(REST) call for the temporal branch -- replaces the old fully-synchronous
_execute_temporal_query call.

    build TemporalWorkerInput (caller's job -- via the adapters)
    -> compute dedup signature (canonical_signature over the query's
       logically-identifying fields, PLUS an index-freshness fingerprint --
       Bug #1547, see _compute_temporal_freshness_signal)
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
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.cache.hnsw_index_cache import _stat_index_fingerprint
from code_indexer.server.cache.payload_cache import PayloadCache
from code_indexer.server.services.temporal_dedup_cache import (
    TemporalDedupCache,
    TemporalDedupCapacityExhaustedError,
    canonical_signature,
    get_temporal_dedup_cache,
)
from code_indexer.server.services.temporal_freshness_cache import (
    TemporalFreshnessSignalCache,
    get_temporal_freshness_signal_cache,
)
from code_indexer.server.services.temporal_poll_job_status import (
    poll_temporal_job_status,
)
from code_indexer.server.services.temporal_snapshot_store import (
    read_temporal_snapshot,
)
from code_indexer.server.services.temporal_worker import (
    _resolve_golden_temporal_context,
    run_temporal_worker,
)
from code_indexer.services.temporal.temporal_status import (
    list_temporal_shard_dirs_under_fixed_root,
)
from code_indexer.services.temporal.temporal_worker_input import TemporalWorkerInput

logger = logging.getLogger(__name__)

TEMPORAL_OPERATION_TYPE = "temporal_query"

# Locked design: "~50ms polling" -- short enough for a responsive foreground
# wait, long enough not to busy-loop the executor thread.
_POLL_INTERVAL_SECONDS = 0.05

#: Bug #1547: literal filename, matching temporal_status.py's private
#: _HNSW_INDEX_FILENAME constant -- kept as a plain literal here rather than
#: importing that private module constant across the module boundary.
_HNSW_INDEX_FILENAME = "hnsw_index.bin"

#: Bug #1547 Finding 2: tag embedded in every DEGRADED (unverifiable)
#: freshness sub-result -- see _degraded_freshness_marker. Real temporal
#: shard directory names always match parse_physical_temporal_name's
#: naming convention and can therefore never collide with this literal.
_DEGRADED_MARKER_TAG = "__temporal_freshness_degraded__"


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


def _freshness_cache_key(worker_input: TemporalWorkerInput) -> str:
    """Bug #1547 Finding 1: cache key for the freshness-signal cache --
    everything _compute_temporal_freshness_signal's resolution depends on
    (username to resolve a non-global repository_alias's golden lineage,
    repository_alias itself).

    Bug #1547 round-2 FIX 2: length-PREFIXED, not a bare
    f"{username}:{repository_alias}" join. Neither validate_username
    (models/auth.py) nor validate_user_alias (models/repos.py) forbid a
    literal colon, so the bare join was not injective -- username="a",
    repository_alias="b:c" and username="a:b", repository_alias="c" both
    resolve DIFFERENT temporal roots but formatted to the identical string
    "a:b:c", letting one repo's freshness signal collide with (and be
    served for) a completely different repo/user pair. Prefixing with
    len(username) makes the username/repository_alias boundary depend only
    on the username's own length -- never on its content -- so it can
    never be confused with a colon appearing inside either field. This is
    the standard unambiguous "netstring-style" length-prefix encoding: for
    two pairs to collide, the numeric prefixes (canonical str(int)
    representations) must be equal, which forces the SAME length, which
    forces the SAME first `length` characters (the username) to be
    identical, which forces the same remaining suffix (the
    repository_alias) to be identical too.
    """
    username = worker_input.username
    return f"{len(username)}:{username}:{worker_input.repository_alias}"


def _degraded_freshness_marker(generation: int) -> List[List[Any]]:
    """Bug #1547 Finding 2: the TOP-LEVEL degraded-signal marker -- used
    when the freshness signal could not be positively verified AT ALL (no
    activated_repo_manager, a lineage-resolution failure, or an unexpected
    exception). Tagged with the CURRENT recompute generation
    (TemporalFreshnessSignalCache.get_or_compute) so it can never be
    mistaken for a degraded (or healthy) signal computed in ANY OTHER
    recompute pass -- concurrent identical dispatches within the SAME pass
    share the SAME cached generation (still dedup with each other), while a
    LATER pass always gets a strictly greater generation (can never
    collapse into an earlier, possibly pre-refresh, entry). See the module
    docstring's Finding 2 discussion in temporal_freshness_cache.py."""
    return [[_DEGRADED_MARKER_TAG, generation]]


# Bug #1547 defect 1 / Finding 1 / Finding 2: TemporalDedupCache re-served a
# COMPLETED prior job's stored result for up to its terminal TTL even after
# the golden repo's temporal index was refreshed, because the dedup
# signature carried no index-freshness information. _compute_temporal_
# freshness_signal below closes that by folding an on-disk identity
# fingerprint of every temporal shard's hnsw_index.bin into the signature,
# so a refresh (which atomically replaces that file -- a different inode)
# yields a DIFFERENT signature and the stale entry is simply never looked
# up again.
#
# Reuses Bug #1538's exact fingerprinting primitive
# (hnsw_index_cache._stat_index_fingerprint: st_mtime_ns/st_size/st_ino/
# st_dev) rather than a second mechanism, and Bug #1529/#1533's existing
# golden-lineage resolver (_resolve_golden_temporal_context) rather than
# re-deriving the fixed temporal root independently. Per-node self-healing
# off shared on-disk truth -- no new cross-node signalling.
#
# This function is now called EXCLUSIVELY through
# TemporalFreshnessSignalCache.get_or_compute (Finding 1) -- never directly
# on every dispatch -- which supplies `generation`, a strictly-increasing
# per-recompute-pass counter, threaded through to every UNVERIFIABLE
# sub-result (Finding 2) so a degraded signal from one pass can never
# collapse with a degraded signal from a different pass. A POSITIVELY
# VERIFIED "no data" result (no golden lineage, or zero shards found) is a
# STABLE fact, not a degraded state -- it is deliberately NOT
# generation-tagged, and stays the plain empty list `[]`.
def _compute_temporal_freshness_signal(
    worker_input: TemporalWorkerInput,
    activated_repo_manager: Optional[Any],
    generation: int,
) -> List[List[Any]]:
    """Returns a generation-tagged degraded marker (see
    _degraded_freshness_marker) when activated_repo_manager is None (every
    production door wires this per Bug #1533; existing dispatch tests that
    omit it get a degraded, generation-bound signal rather than a constant
    sentinel), when resolution/fingerprinting fails for any other reason,
    or per-shard when an individual stat fails. Returns the plain, stable
    empty list `[]` when no golden lineage resolves for this alias
    (positively verified: nothing to be stale against). This signal is a
    best-effort dedup-key refinement, never a correctness gate on the
    query result itself, so a failure here degrades to a generation-tagged
    marker (logged) rather than failing the whole dispatch. Mirrors this
    module's own _apply_golden_temporal_config precedent (broad try/except
    + WARNING + fallback for a similarly best-effort golden-repo lookup).
    """
    if activated_repo_manager is None:
        logger.debug(
            "Bug #1547: temporal dedup freshness signal computation for "
            "repository_alias=%s skipped (no activated_repo_manager "
            "provided) -- using a generation-tagged degraded marker "
            "(generation=%d) for this recompute pass",
            worker_input.repository_alias,
            generation,
        )
        return _degraded_freshness_marker(generation)

    try:
        golden_ctx = _resolve_golden_temporal_context(
            worker_input, "dedup-freshness-signal", activated_repo_manager
        )
        if golden_ctx.temporal_index_dir is None:
            # Positively verified: no golden lineage -> no temporal shards
            # to be stale against. A STABLE fact, not a degraded/
            # unverifiable state -- deliberately NOT generation-tagged.
            return []

        shard_dirs = list_temporal_shard_dirs_under_fixed_root(
            golden_ctx.temporal_index_dir
        )
        result: List[List[Any]] = []
        for shard_dir in shard_dirs:
            fingerprint = _stat_index_fingerprint(shard_dir / _HNSW_INDEX_FILENAME)
            if fingerprint is None:
                # Bug #1547 Finding 2: an unstat-able shard is
                # UNVERIFIABLE, never "no data" -- tag with the current
                # generation so it can never collapse with a degraded
                # result from a different recompute pass.
                logger.debug(
                    "Bug #1547: could not stat %s for freshness tracking "
                    "(shard %s) -- using a generation-tagged degraded "
                    "marker (generation=%d) for this shard",
                    shard_dir / _HNSW_INDEX_FILENAME,
                    shard_dir.name,
                    generation,
                )
                result.append([shard_dir.name, [_DEGRADED_MARKER_TAG, generation]])
            else:
                result.append([shard_dir.name, list(fingerprint)])
        return result
    except Exception:
        logger.warning(
            "Bug #1547: temporal dedup freshness signal computation failed "
            "(isolated, non-fatal) for repository_alias=%s -- using a "
            "generation-tagged degraded marker (generation=%d, never a "
            "constant sentinel) for this recompute pass",
            worker_input.repository_alias,
            generation,
            exc_info=True,
        )
        return _degraded_freshness_marker(generation)


def _parse_completed_at_epoch(completed_at_iso: Optional[str]) -> Optional[float]:
    """Bug #1547 Finding 3: parse BackgroundJob's completed_at ISO string
    (wall-clock, time.time()-equivalent) into epoch seconds, for anchoring
    TemporalDedupCache's terminal window on the job's ACTUAL completion
    time rather than whenever a later request first observes it as
    terminal. Returns None on any missing/unparseable value -- the dedup
    cache's own fallback (anchor to "now", matching this cache's pre-fix
    behavior) is a reasonable degrade for this rare case, since real
    BackgroundJob records always populate completed_at before flipping to
    a terminal status."""
    if not completed_at_iso:
        logger.debug(
            "Bug #1547: status_check observed no completed_at yet -- "
            "TemporalDedupCache will anchor this entry's terminal window "
            "on 'now' if it turns out to be terminal"
        )
        return None
    try:
        return datetime.fromisoformat(completed_at_iso).timestamp()
    except (ValueError, TypeError) as exc:
        logger.debug(
            "Bug #1547: could not parse completed_at=%r as an ISO "
            "timestamp (%s) -- TemporalDedupCache will anchor this "
            "entry's terminal window on 'now' instead",
            completed_at_iso,
            exc,
        )
        return None


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
    query_tracker: Optional[Any] = None,
    activated_repo_manager: Optional[Any] = None,
    freshness_cache: Optional[TemporalFreshnessSignalCache] = None,
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
        query_tracker: Bug #1482 -- forwarded into the submitted worker's
            kwargs (never into TemporalWorkerInput/the dedup signature) so
            run_temporal_worker can construct a resolution-scope-safe
            TemporalShardResolver and consult the golden-owned sister
            location. None (default) preserves today's legacy-only
            resolution behavior exactly.
        activated_repo_manager: Bug #1533 -- the caller's DI-wired
            ActivatedRepoManager (app.state.activated_repo_manager),
            forwarded into the worker's kwargs the same way query_tracker
            is. The worker needs it to resolve golden lineage from the
            SHARED metadata store; a manager it constructs itself reads
            node-local metadata, which is empty on a cluster node and
            silently yields zero temporal results. In postgres/cluster mode
            the worker RAISES rather than degrade, so a door that omits
            this kwarg fails loudly instead of quietly answering nothing.
            Bug #1547: also consulted (read-only) to compute an
            index-freshness signal folded into the dedup signature -- see
            _compute_temporal_freshness_signal. Still never enters
            TemporalWorkerInput itself.
        freshness_cache: Bug #1547 Finding 1 -- injected for tests; a
            TemporalFreshnessSignalCache instance (production callers omit
            this and get the shared get_temporal_freshness_signal_cache()
            singleton). Bounds how often _compute_temporal_freshness_signal
            actually runs (including its blocking os.stat calls and golden-
            lineage metadata lookup) per (username, repository_alias) key,
            rather than recomputing on every dispatch.

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
    if freshness_cache is None:
        freshness_cache = get_temporal_freshness_signal_cache()

    signature_payload = _worker_input_signature_dict(worker_input)
    # Bug #1547: an index-freshness fingerprint, folded in as an EXTRA key
    # (never merged into _worker_input_signature_dict itself, which stays a
    # pure function of TemporalWorkerInput alone) so a temporal refresh
    # changes the computed signature instead of silently re-serving a
    # pre-refresh terminal dedup entry. Finding 1: routed through
    # freshness_cache.get_or_compute so the (potentially blocking) real
    # computation runs at most once per recheck interval per key, never on
    # every dispatch.
    freshness_key = _freshness_cache_key(worker_input)
    signature_payload["_temporal_freshness"] = freshness_cache.get_or_compute(
        freshness_key,
        lambda generation: _compute_temporal_freshness_signal(
            worker_input, activated_repo_manager, generation
        ),
    )
    signature = canonical_signature(signature_payload)

    def _status_check(job_id: str) -> Tuple[Optional[str], Optional[float]]:
        status_dict = background_job_manager.get_job_status(
            job_id, worker_input.username, is_admin
        )
        if status_dict is None:
            return None, None
        # Bug #1547 Finding 3: forward the job's REAL completion time
        # (wall-clock) so TemporalDedupCache can anchor its terminal
        # window on when the job actually finished, not on whenever this
        # (possibly much later) status check first observes it as
        # terminal.
        return status_dict.get("status"), _parse_completed_at_epoch(
            status_dict.get("completed_at")
        )

    def _submit() -> str:
        # Story #1400: repo_alias is deliberately OMITTED here. BGM's
        # register_job_if_no_conflict gate is a per-(operation_type,
        # repo_alias) uniqueness constraint -- passing repository_alias
        # would incorrectly reject a SECOND, entirely different temporal
        # query (different query_text/filters) against the same repo as a
        # "duplicate". Correct dedup granularity (full query signature) is
        # already enforced above by TemporalDedupCache; the BGM-level gate
        # is the wrong tool for this job type.
        #
        # query_tracker (Bug #1482) and activated_repo_manager (Bug #1533)
        # are forwarded as plain worker kwargs -- BGM passes them through by
        # name to run_temporal_worker.
        new_job_id: str = background_job_manager.submit_job(
            TEMPORAL_OPERATION_TYPE,
            worker_fn,
            submitter_username=worker_input.username,
            is_admin=is_admin,
            lane="temporal",
            worker_input=worker_input,
            payload_cache=payload_cache,
            query_tracker=query_tracker,
            activated_repo_manager=activated_repo_manager,
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
    logger.debug(
        "execute_live_temporal_search: job_id=%s inline_wait_seconds=%.6f "
        "waiter_budget_seconds=%.6f handler_deadline_present=%s",
        job_id,
        inline_wait_seconds,
        waiter_deadline - now,
        handler_deadline_monotonic is not None,
    )
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

    # Bug investigation (recurrence of the forced-deferral E2E race in
    # test_19_temporal_live_wiring_1400.py). Two DISTINCT, real defects in
    # the wait loop, not a probabilistic-timing issue:
    #
    # (1) temporal_inline_wait_seconds == 0.0 is already a valid, accepted
    #     config value (config_manager.py only rejects < 0.0), so it gets a
    #     well-defined, race-proof contract HERE: "always hand off
    #     immediately" -- return the deferred envelope WITHOUT ever
    #     consulting job status. No status check means no race to lose,
    #     regardless of how fast the underlying job happens to complete.
    if inline_wait_seconds <= 0.0:
        return {
            "status": "waiting",
            "continue_polling": True,
            "partial_results": [],
            "shards_completed": 0,
            "shards_total": None,
            "unranked": True,
            "job_id": job_id,
        }

    # (2) For a positive wait budget, the deadline must be checked BEFORE
    #     every status read (never read status once the deadline has
    #     already passed) and each sleep must be capped to the remaining
    #     budget -- an unconditional full-interval sleep can overshoot the
    #     deadline, after which the NEXT status read might see "completed"
    #     purely because extra, unbudgeted wall-clock time elapsed during
    #     that overshoot. `result` holds the last KNOWN status; it stays a
    #     "waiting" envelope if the deadline expires before any read ever
    #     ran (e.g. an already-tiny budget consumed by submission/setup).
    result: Dict[str, Any] = {
        "status": "waiting",
        "continue_polling": True,
        "partial_results": [],
        "shards_completed": 0,
        "shards_total": None,
        "unranked": True,
    }
    while time.monotonic() < waiter_deadline:
        job_status = background_job_manager.get_job_status(
            job_id, worker_input.username, is_admin
        )
        result = poll_temporal_job_status(
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
        remaining = waiter_deadline - time.monotonic()
        if remaining <= 0.0:
            break
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    result["job_id"] = job_id
    return result
