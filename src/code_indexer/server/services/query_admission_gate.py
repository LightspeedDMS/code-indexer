"""Query-path memory-pressure admission gate (Story #1600).

Fifteen memory/CPU-unbounded query-path MCP handlers (browse_directory,
list_files, get_file_content, directory_tree, regex_search, search_code,
xray_search, xray_explore, xray_search_batch, scip_impact,
depmap_get_cross_domain_graph, depmap_get_hub_domains, git_search_commits,
git_search_diffs, get_all_repositories_status) call check_query_admission()
before doing any real work, so a burst of concurrent expensive queries
against large repos gets cleanly rejected with a retry hint instead of
driving the server into an OOM/swap death spiral.

This is the FIRST "reject-the-live-caller" consumer of
MemoryGovernor.admission_allowed(). The two existing consumers
(server/repositories/background_jobs.py, server/services/
distributed_job_claimer.py) use *defer* semantics: they leave a background
job's DB row PENDING for a later poll. An inline MCP/REST request has no
polling loop or persistent row to fall back on -- it must be served now or
fail now.

Fail-open contract (deliberately louder than the sibling background-job
gate):
  - No MemoryGovernor installed (CLI/solo/pre-lifespan-init) -> admit.
  - Any exception raised while consulting the governor (including the
    get_memory_governor() lookup itself) -> admit, with a WARNING logged.
    background_jobs.py's _admission_blocked() logs the equivalent failure
    at logging.debug (a silent background-job deferral is low-stakes); a
    live-caller-facing rejection failure is more visible to operators, so
    this gate deliberately logs louder. This is an intentional deviation,
    not a level-match with that sibling.

On the allow path this performs ONLY O(1) lock-free reads:
get_config_service().get_config().background_jobs_config is a verified
in-RAM dataclass return, and governor.admission_allowed(...) is a verified
O(1) cached-state read (never touches psutil or /sys/fs/cgroup directly).
On deny, retry_after_seconds is derived from
governor.last_red_min_dwell_seconds -- also an O(1) cached attribute read,
never get_snapshot() (which does a real psutil.swap_memory() syscall plus a
live config re-read and builds a ~20-field dict on every call).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MEMORY_PRESSURE_ERROR_CODE = "memory_pressure"
MEMORY_PRESSURE_MESSAGE = "Server is under high memory pressure; please retry shortly."


@dataclass(frozen=True)
class AdmissionDecision:
    """Result of a check_query_admission() call.

    allowed=True:  proceed with the handler's real work.
    allowed=False: reject; retry_after_seconds is the operator-facing hint
                   (ceil'd RED min-dwell seconds).
    """

    allowed: bool
    retry_after_seconds: Optional[int] = None


def check_query_admission() -> AdmissionDecision:
    """Should this query-path request be admitted now, given current
    memory pressure?

    See module docstring for the full fail-open contract. Never raises:
    the entire body (including the get_memory_governor() lookup) runs
    under one try/except so ANY unexpected failure fails open.
    """
    try:
        from code_indexer.server.services.memory_governor import (
            get_memory_governor,
        )

        governor = get_memory_governor()
        if governor is None:
            # CLI / solo / pre-lifespan-init: no governor to consult.
            return AdmissionDecision(allowed=True, retry_after_seconds=None)

        from code_indexer.server.services.config_service import get_config_service

        cfg = get_config_service().get_config().background_jobs_config
        if not cfg.job_admission_memory_gate_enabled:
            # Master kill switch (mirrors background_jobs.py's
            # _admission_blocked() check) -- an operator escape hatch if
            # this gate ever misfires in production. Checked BEFORE
            # consulting the governor so a disabled gate never increments
            # query_admissions_denied either.
            return AdmissionDecision(allowed=True, retry_after_seconds=None)
        if governor.admission_allowed(cfg.job_admission_memory_max_used_pct):
            return AdmissionDecision(allowed=True, retry_after_seconds=None)

        governor.increment_query_admissions_denied()
        retry_after = math.ceil(governor.last_red_min_dwell_seconds)
        return AdmissionDecision(allowed=False, retry_after_seconds=retry_after)
    except Exception as exc:  # noqa: BLE001 -- fail-open contract, see module docstring
        logger.warning(
            "query admission check raised; failing open: %s", exc, exc_info=True
        )
        return AdmissionDecision(allowed=True, retry_after_seconds=None)


def memory_pressure_mcp_payload(decision: AdmissionDecision) -> dict:
    """Build the MCP rejection envelope body for a denied AdmissionDecision.

    Callers wrap this dict in the module's own _mcp_response() helper.
    HTTP status stays 200 regardless -- MCP JSON-RPC convention: logical
    errors are never surfaced as a non-200 status.
    """
    return {
        "success": False,
        "error_code": MEMORY_PRESSURE_ERROR_CODE,
        "error": MEMORY_PRESSURE_MESSAGE,
        "retry_after_seconds": decision.retry_after_seconds,
    }


def raise_memory_pressure_http_error(decision: AdmissionDecision) -> None:
    """Raise the REST-facing HTTP 503 translation of a denied AdmissionDecision.

    Used by the REST routes backing search_code/regex_search (currently
    the single /api/query endpoint in routers/inline_query.py, which
    covers semantic, FTS, and hybrid modes). Never returns normally --
    always raises fastapi.HTTPException. Callers must only invoke this
    when decision.allowed is False (retry_after_seconds is always set on
    that path by check_query_admission()).
    """
    from fastapi import HTTPException

    raise HTTPException(
        status_code=503,
        detail={
            "success": False,
            "error_code": MEMORY_PRESSURE_ERROR_CODE,
            "error": MEMORY_PRESSURE_MESSAGE,
            "retry_after_seconds": decision.retry_after_seconds,
        },
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )
