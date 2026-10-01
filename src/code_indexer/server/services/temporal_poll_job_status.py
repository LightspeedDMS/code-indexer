"""DEFERRED POLL algorithm core logic -- Story #1400 Phase 8.

Shared by the (not-yet-registered) poll_search_job MCP tool and the REST
GET /api/query/result/{job_id} endpoint -- both are thin readers around
this pure function. Ownership/ authorization is the CALLER's
responsibility: it must have already called get_job_status(job_id, user)
(which returns None for BOTH not-found AND unauthorized by design) and
pass the result (or None) in as `job_status`.

    status_of(job_id,user): d=get_job_status(job_id,user); RETURN d["status"] if d else None
    IF st is None: RETURN {status:"not_found", continue_polling:False}
    IF st in ("pending","running"):
      IF has_key: partial (unranked=True always -- rerank is terminal-only)
      ELSE:       empty partial (Scenario 14: zero-shard/PENDING, no error)
    IF st in ("failed","cancelled"): RETURN {status:"failed", ...}
    IF NOT has_key:
      IF job_status["result"] is a dict: RETURN {**result, status:"completed", continue_polling:False}  # Bug #1499
      RETURN {status:"not_found", error:"result expired -- resubmit"}  # AC10
    RETURN {status:"completed", results:..., unranked:...}

MEDIUM item (terminal-status completeness): "completed_partial" is treated
identically to "completed" in the terminal branch -- unreachable in v1 as
the worker always writes a plain completed snapshot, but included
defensively since JobStatus.COMPLETED_PARTIAL genuinely exists in BGM and
is named as a shared-enum v2/Bug #679 follow-up.

Bug #1499: this function is also polled for jobs that are NOT temporal
snapshot jobs at all (e.g. operation_type=xray_search) via the exact same
poll_search_job / GET .../result/{job_id} front doors. Such a job never had
a temporal snapshot written for it, so read_snapshot_fn() correctly returns
None -- but the terminal-completed branch used to unconditionally treat
"no snapshot" as TTL expiry, producing a misleading "result expired --
resubmit" for a job whose real result was sitting, unread, in
background_jobs.result the whole time. The fix checks job_status["result"]
before declaring expiry and returns it unmodified (never routed through
postprocess_temporal_snapshot, whose results/shards_* shape assumptions
are specific to a temporal snapshot and do not apply to an arbitrary job
result).
"""

from typing import Any, Callable, Dict, Optional

from code_indexer.server.services.temporal_poll_postprocessor import (
    postprocess_temporal_snapshot,
)

_PENDING_STATUSES = {"pending", "running"}
_FAILED_STATUSES = {"failed", "cancelled"}
_COMPLETED_STATUSES = {"completed", "completed_partial"}

# CRITICAL (regression fix on the #1499 fix, caught by code review): the
# ONE operation_type string every real temporal search job is submitted
# under -- verified against services/temporal_live_dispatch.py, whose
# module-level TEMPORAL_OPERATION_TYPE = "temporal_query" is the sole
# literal passed to background_job_manager.submit_job() for
# execute_live_temporal_search (the only production submitter of a
# temporal job). A temporal job's job_status["result"] is NOT a reliable
# "has a real persisted result" signal: run_temporal_worker
# (temporal_worker.py) returns {"result_ready": True} on success, which
# background_jobs.py persists verbatim as job.result and get_job_status
# surfaces as-is -- that IS a dict, so a bare isinstance(..., dict) check
# (the original #1499 discriminator) incorrectly treated it as a genuine
# persisted result, making the AC10 expiry branch below unreachable for
# every temporal job whose snapshot expired. Only a NON-temporal
# operation_type (e.g. xray_search, xray_search_batch, xray_explore) may
# take the persisted-result passthrough; a temporal_query job always
# falls through to the snapshot/AC10-expiry logic, regardless of its
# result shape.
_TEMPORAL_OPERATION_TYPE = "temporal_query"

# CRITICAL 4/6 (code review): substrings of the two real, distinct failure
# messages a temporal job's stored `error` field can carry. Matched against
# the ACTUAL wording used by every orphan-cleanup path (background_jobs.py
# / background_jobs_backend.py: "Job interrupted by server restart",
# "Orphaned by server restart") and by TemporalSnapshotPersistenceError
# (temporal_snapshot_store.py: "Temporal snapshot write verification
# failed for job ..."). No new storage layer needed -- both are already
# stored verbatim in job_status["error"] by BGM's existing job-failure path.
_NODE_RESTART_ERROR_MARKER = "restart"
_SNAPSHOT_PERSISTENCE_ERROR_MARKER = "snapshot write verification failed"


def _empty_partial_response() -> Dict[str, Any]:
    return {
        "status": "waiting",
        "continue_polling": True,
        "partial_results": [],
        "shards_completed": 0,
        "shards_total": None,
        "unranked": True,
    }


def poll_temporal_job_status(
    job_status: Optional[Dict[str, Any]],
    read_snapshot_fn: Callable[[], Optional[Dict[str, Any]]],
    access_filtering_service: Any,
    username: str,
    is_admin: bool,
    config_service: Optional[Any] = None,
    deadline_monotonic: Optional[float] = None,
) -> Dict[str, Any]:
    """Resolve a single poll_search_job / GET .../result/{job_id} response.

    Args:
        job_status: the dict returned by BackgroundJobManager.get_job_status,
            or None (not found / unauthorized -- indistinguishable, matching
            get_job_status's own contract).
        read_snapshot_fn: zero-arg callable returning the parsed temporal
            snapshot (via read_temporal_snapshot), or None if absent.
        access_filtering_service: real AccessFilteringService instance.
        username: the requesting user.
        is_admin: whether the requester is an admin.
        config_service: real ConfigService instance, forwarded to the
            terminal-completed postprocess call ONLY (rerank is
            terminal-only) so a real rerank_query in the snapshot's ctx is
            actually honored. None (default) -- used for every non-terminal
            call -- keeps that read conservatively unranked=True.
        deadline_monotonic: Story #1400 CRITICAL 5, forwarded alongside
            config_service to cap the reranker's HTTP timeout/backoff.

    Returns:
        A response dict with at minimum "status" and "continue_polling".
    """
    if job_status is None:
        return {"status": "not_found", "continue_polling": False}

    status = job_status.get("status")

    if status in _PENDING_STATUSES:
        snapshot = read_snapshot_fn()
        if snapshot is None:
            return _empty_partial_response()
        results, k, n, _unranked = postprocess_temporal_snapshot(
            snapshot, access_filtering_service, username, is_admin, terminal=False
        )
        return {
            "status": "waiting",
            "continue_polling": True,
            "partial_results": results,
            "shards_completed": k,
            "shards_total": n,
            "unranked": True,
        }

    if status in _FAILED_STATUSES:
        error_text = job_status.get("error") or "job failed"
        response: Dict[str, Any] = {
            "status": "failed",
            "continue_polling": False,
            "error": error_text,
        }
        error_lower = error_text.lower()
        if _SNAPSHOT_PERSISTENCE_ERROR_MARKER in error_lower:
            # CRITICAL 6: distinguishable from plain TTL expiry (a
            # separate "not_found" branch below) -- this is a genuine
            # storage failure, the client must resubmit.
            response["error_code"] = "TEMPORAL_SNAPSHOT_PERSISTENCE_FAILED"
            response["resubmit_required"] = True
        elif _NODE_RESTART_ERROR_MARKER in error_lower:
            # CRITICAL 4: no auto-resubmission mechanism exists -- the
            # client must re-issue the original query as a new request.
            response["error_code"] = "TEMPORAL_NODE_RESTART"
            response["resubmit_required"] = True
        return response

    # Terminal-completed (including the defensively-handled
    # completed_partial, unreachable in v1).
    snapshot = read_snapshot_fn()
    if snapshot is None:
        # Bug #1499: a non-temporal completed job (e.g. operation_type=
        # xray_search) polled through this SAME deferred-poll path never
        # had a temporal snapshot written for it -- read_snapshot_fn()
        # correctly returns None. Its real result lives in
        # background_jobs.result (job_status["result"]), NOT the
        # PayloadCache-backed temporal snapshot store. Return it as-is --
        # never route it through postprocess_temporal_snapshot, whose
        # results/shards_* shape assumptions do not apply to it.
        job_result = job_status.get("result")
        is_temporal_job = job_status.get("operation_type") == _TEMPORAL_OPERATION_TYPE
        if isinstance(job_result, dict) and not is_temporal_job:
            response = dict(job_result)
            response["status"] = "completed"
            response["continue_polling"] = False
            return response

        # AC10: a genuine COMPLETED temporal job whose snapshot expired
        # past TTL, with no persisted job result either -- honest expiry,
        # never stale/empty-as-success.
        return {
            "status": "not_found",
            "continue_polling": False,
            "error": "result expired -- resubmit",
        }

    results, k, n, unranked = postprocess_temporal_snapshot(
        snapshot,
        access_filtering_service,
        username,
        is_admin,
        terminal=True,
        config_service=config_service,
        deadline_monotonic=deadline_monotonic,
    )
    return {
        "status": "completed",
        "continue_polling": False,
        "results": results,
        "total_results": len(results),
        "shards_completed": k,
        "shards_total": n,
        "unranked": unranked,
    }
