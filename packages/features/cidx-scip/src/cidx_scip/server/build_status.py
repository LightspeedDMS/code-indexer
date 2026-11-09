"""the state of a delegated SCIP build, visible on every pod.

A delegated build (``scip_backend='delegated-plugin'``) runs asynchronously
on one pod's serialized queue, after the job that asked for it has returned.
Its outcome used to be a log line on that pod, so ``get_index_status`` kept
saying ``not_indexed`` and ``scip_definition`` returned 0 results with no
reason why.

Each enqueued build is now tracked as a job-tracker row
(:data:`OPERATION`, ``repo_alias`` = the bare golden alias) in the shared
``background_jobs`` table. It moves pending -> running -> completed
(with a project summary) or failed (with the reason). :func:`latest_build`
reads the most recent one for an alias, so the status and the query tools
can say what happened. Tracking is best-effort: a tracker failure never
affects the build.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from cidx_scip.server import _core

logger = logging.getLogger(__name__)

OPERATION = "delegated_scip_build"
SUBMITTER = "system-scip"
_REASON_MAX_CHARS = 500


def server_job_tracker() -> Optional[Any]:
    """The running server's job tracker (None outside a server)."""
    return getattr(_core.running_app_state(), "job_tracker", None)


def failure_reason(result: Any) -> str:
    """Why a build produced no index, from its per-project results."""
    if result is None:
        return "the build returned no result"
    reasons: List[str] = []
    for project in getattr(result, "project_results", None) or []:
        indexer = getattr(project, "indexer_result", None)
        if indexer is None or (
            callable(getattr(indexer, "is_success", None)) and indexer.is_success()
        ):
            continue
        name = getattr(getattr(project, "project", None), "relative_path", None) or "?"
        detail = (
            getattr(indexer, "stderr", "") or getattr(indexer, "stdout", "") or ""
        ).strip()
        detail = (
            detail.splitlines()[-1]
            if detail
            else f"exit {getattr(indexer, 'exit_code', '?')}"
        )
        reasons.append(f"{name}: {detail}")
    summary = "; ".join(reasons) or (
        f"{getattr(result, 'failed_projects', 0)} of "
        f"{getattr(result, 'total_projects', 0)} project(s) failed"
    )
    return summary[:_REASON_MAX_CHARS]


def result_summary(result: Any) -> Dict[str, Any]:
    return {
        "total_projects": int(getattr(result, "total_projects", 0) or 0),
        "successful_projects": int(getattr(result, "successful_projects", 0) or 0),
        "failed_projects": int(getattr(result, "failed_projects", 0) or 0),
    }


@dataclass(frozen=True)
class BuildClaim:
    """The result of claiming the right to build an alias.

    ``claimed`` is False only when another build of the alias is active:
    ``blocking_job_id`` / ``blocking_dedupe_key`` then name it. A tracker
    that is missing or failing still reports ``claimed=True`` -- tracking
    is best-effort and never stops a build."""

    claimed: bool
    blocking_job_id: Optional[str] = None
    blocking_dedupe_key: Optional[str] = None


class BuildTracker:
    """The tracker row of one delegated build, and the cluster-wide claim.

    ``claim`` atomically registers the row (one active row per alias, enforced
    by the shared table), so the registration is the single-flight arbiter
    across pods. The queue then reports ``running`` / ``succeeded`` /
    ``failed`` against that row. A registration is attempted once: a lost
    claim or a tracker error is remembered, never retried, and never touches
    another build's row. Never raises."""

    def __init__(self, job_tracker: Optional[Any], alias: str, dedupe_key: str) -> None:
        self._tracker = job_tracker
        self._alias = alias
        self._dedupe_key = dedupe_key
        self._job_id: Optional[str] = None
        self._claim: Optional[BuildClaim] = None
        self._lock = threading.Lock()

    @property
    def job_id(self) -> Optional[str]:
        return self._job_id

    def claim(self) -> BuildClaim:
        with self._lock:
            if self._claim is None:
                self._claim = self._register()
            return self._claim

    def _register(self) -> BuildClaim:
        if self._tracker is None:
            return BuildClaim(claimed=True)
        job_id = uuid.uuid4().hex
        try:
            self._tracker.register_job_if_no_conflict(
                job_id,
                OPERATION,
                SUBMITTER,
                repo_alias=self._alias,
                metadata={"dedupe_key": self._dedupe_key},
            )
        # The tracker raises its own DuplicateJobError; the jobs backend
        # raises another with the same shape. Either one means "another build
        # is active".
        except _core.duplicate_job_errors() as exc:
            blocking = getattr(exc, "existing_job_id", None)
            return BuildClaim(
                claimed=False,
                blocking_job_id=blocking,
                blocking_dedupe_key=self._dedupe_key_of(blocking),
            )
        except Exception as exc:  # tracking never touches the build
            logger.warning(
                "could not track the delegated SCIP build of '%s': %s",
                self._alias,
                exc,
            )
            return BuildClaim(claimed=True)
        self._job_id = job_id
        return BuildClaim(claimed=True)

    def _dedupe_key_of(self, job_id: Optional[str]) -> Optional[str]:
        if not job_id or self._tracker is None:
            return None
        try:
            job = self._tracker.get_job(job_id)
            metadata = getattr(job, "metadata", None) or {}
            key = metadata.get("dedupe_key")
            return key if isinstance(key, str) else None
        except Exception:  # only used to word a log line
            return None

    def queued(self) -> None:
        self._safely(self.claim)

    def release(self, reason: str) -> None:
        """Give the claim back: the build was never queued."""

        def cancel() -> None:
            if self._job_id is not None and self._tracker is not None:
                self._tracker.cancel_job(self._job_id)

        logger.info(
            "released the SCIP build claim of '%s': %s", self._alias, reason
        )
        self._safely(cancel)

    def on_state(
        self, state: str, reason: Optional[str], result: Optional[Any]
    ) -> None:
        def record() -> None:
            self.claim()
            job_id = self._job_id
            if job_id is None or self._tracker is None:
                return
            if state == "running":
                self._tracker.update_status(job_id, status="running")
            elif state == "succeeded":
                self._tracker.complete_job(job_id, result=result_summary(result))
            elif state == "failed":
                self._tracker.fail_job(job_id, error=reason or "build failed")

        self._safely(record)

    def _safely(self, action: Any) -> None:
        try:
            action()
        except Exception as exc:  # tracking never touches the build
            logger.warning(
                "could not track the delegated SCIP build of '%s': %s",
                self._alias,
                exc,
            )


def latest_build(job_tracker: Optional[Any], alias: str) -> Optional[Dict[str, Any]]:
    """The most recent delegated build of *alias* (bare or ``-global``):
    ``{job_id, state, error?, result?, created_at, completed_at}``, or None
    (no tracker, none recorded, or the tracker cannot be read)."""
    if job_tracker is None:
        return None
    bare = alias.removesuffix("-global")
    try:
        rows = job_tracker.query_jobs(
            operation_type=OPERATION, repo_alias=bare, limit=1
        )
    except Exception as exc:
        logger.warning("delegated SCIP build state unreadable: %s", exc)
        return None
    if not rows:
        return None
    row = rows[0]
    build: Dict[str, Any] = {
        "job_id": row.get("job_id"),
        "state": row.get("status"),
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }
    if row.get("error"):
        build["error"] = row["error"]
    if row.get("result"):
        build["result"] = row["result"]
    return build


def explain_empty_results(alias: Optional[str]) -> Optional[str]:
    """Why a SCIP query on *alias* found nothing, when a delegated build
    says so: still running, or failed (with its reason). None otherwise."""
    if not alias:
        return None
    build = latest_build(server_job_tracker(), alias)
    if build is None:
        return None
    state = build.get("state")
    if state in ("pending", "running"):
        return (
            f"The SCIP index of '{alias}' is still being built (job "
            f"{build.get('job_id')}, {state}); try again when it completes"
        )
    if state == "failed":
        return (
            f"The last SCIP build of '{alias}' failed (job {build.get('job_id')}): "
            f"{build.get('error')}"
        )
    return None


#: Build state -> the SCIP status a repo whose index is not readable here shows.
_STATUS_OF_BUILD = {"pending": "BUILDING", "running": "BUILDING", "failed": "FAILED"}


def with_build_state(scip_status: Any, alias: str) -> Dict[str, Any]:
    """*scip_status* (``get_index_status``'s scip entry) with the alias's
    latest delegated build under ``delegated_build``. While no index is
    readable here, a queued/running build shows ``BUILDING`` and a failed
    one ``FAILED`` with its reason."""
    status: Dict[str, Any] = dict(scip_status or {})
    build = latest_build(server_job_tracker(), alias)
    if build is None:
        return status
    status["delegated_build"] = build
    shown = _STATUS_OF_BUILD.get(str(build.get("state")))
    if shown and status.get("status") != "SUCCESS":
        status["status"] = shown
        if shown == "FAILED":
            status["error"] = build.get("error")
    return status
