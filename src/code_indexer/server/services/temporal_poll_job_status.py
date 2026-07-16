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
    IF NOT has_key: RETURN {status:"not_found", error:"result expired -- resubmit"}  # AC10
    RETURN {status:"completed", results:..., unranked:...}

MEDIUM item (terminal-status completeness): "completed_partial" is treated
identically to "completed" in the terminal branch -- unreachable in v1 as
the worker always writes a plain completed snapshot, but included
defensively since JobStatus.COMPLETED_PARTIAL genuinely exists in BGM and
is named as a shared-enum v2/Bug #679 follow-up.
"""

from typing import Any, Callable, Dict, Optional

from code_indexer.server.services.temporal_poll_postprocessor import (
    postprocess_temporal_snapshot,
)

_PENDING_STATUSES = {"pending", "running"}
_FAILED_STATUSES = {"failed", "cancelled"}
_COMPLETED_STATUSES = {"completed", "completed_partial"}

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
        # AC10: a COMPLETED job whose snapshot expired past TTL -- honest
        # expiry, never stale/empty-as-success.
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
        "shards_completed": k,
        "shards_total": n,
        "unranked": unranked,
    }
