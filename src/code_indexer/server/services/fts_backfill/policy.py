"""EVO-66445: the FTS index-backfill policy + its cross-pod (POD_PULL) build executor.

The FTS backfill rides the DRY :class:`GoldenIndexBackfillReconciler`: the leader
scans + enqueues ONE ``fts_backfill_build`` job per missing repo, and — unlike the
SCIP reconcile, whose delegated sandbox build is a shared serialized bottleneck —
each job is a POD_PULL cross-pod-claimable row, so every pod claims DISTINCT repos
and runs the (cheap, local) tantivy build in PARALLEL. No collisions (DB-level
claim), full cluster utilization.
"""

from __future__ import annotations

import logging
from pathlib import Path

from code_indexer.server.services.index_freshness import build_commit_is_current
from typing import Any, Callable, Dict, Optional

from code_indexer.server.repositories.background_jobs import DuplicateJobError
from code_indexer.utils.subprocess_env import build_cidx_subprocess_env

from ..fts_index_builder import (
    ensure_fts_index,
    fts_backfill_skip_present,
    fts_index_present,
    FTS_INDEX_DIRNAME,
)

logger = logging.getLogger(__name__)

#: POD_PULL operation type for a single-repo FTS build (must match the dispatch key
#: and the POD_PULL_OPS membership — see background_jobs.POD_PULL_OPS).
FTS_BACKFILL_BUILD_OP = "fts_backfill_build"


def fts_backfill_build_executor(
    metadata: Dict[str, Any], _progress_callback: Optional[Callable[..., Any]] = None
) -> Dict[str, Any]:
    """Dispatch executor: build ONE golden repo's FTS index. Reconstructs the work
    purely from the pod-pull ``metadata`` row (repo_path + alias), so any pod that
    claims the job can run it. Reads its own subprocess env + timeout from config, so
    the executor is self-contained on the claiming pod.

    The heavy lifting (present-check, init-guard, SKIPPED-on-no-files marker, honest
    error detail) all live in :func:`ensure_fts_index` — unchanged by this refactor.
    """
    from code_indexer.server.services.config_service import get_config_service

    repo_path = Path(metadata["repo_path"])
    alias = metadata.get("alias") or repo_path.name
    timeout: Optional[int] = None
    try:
        cfg = get_config_service().get_config().fts_backfill_sweep_config
        timeout = int(cfg.build_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — a config glitch must not block the build
        logger.warning("fts_backfill_build: config read failed, no timeout: %s", exc)

    outcome = ensure_fts_index(
        repo_path,
        alias=alias,
        subprocess_env=build_cidx_subprocess_env(),
        timeout=timeout,
    )
    return {
        "status": outcome.status.value,
        "alias": outcome.alias,
        "detail": outcome.detail,
    }


class FtsBackfillPolicy:
    """:class:`IndexBackfillPolicy` for FTS. Enqueues per-repo POD_PULL build jobs."""

    name = "fts"
    attempt_marker = ".fts_backfill_attempt"

    def __init__(self, background_job_manager: Any) -> None:
        self._bjm = background_job_manager

    def is_present(self, repo_path: Path) -> bool:
        return fts_index_present(repo_path)

    def is_fresh(self, repo_path: Path) -> bool:
        # The shared .build_commit convention; SCIP uses the same marker with extra
        # rules of its own. An FTS rebuild is a cheap local tantivy build, so drift
        # is worth acting on as soon as it is observed.
        return build_commit_is_current(
            repo_path, repo_path / ".code-indexer" / FTS_INDEX_DIRNAME
        )

    def eligibility(self, repo_path: Path) -> Optional[Dict[str, Any]]:
        # A repo marked as having no indexable files (config/infra mirror) is a permanent
        # skip — not eligible. Everything else is eligible to attempt; whether it has files
        # is decided by the build itself (which re-marks it SKIPPED if not).
        if fts_backfill_skip_present(repo_path):
            return None
        return {"repo_path": str(repo_path), "alias": repo_path.name}

    def enqueue(self, alias: str, repo_path: Path, meta: Dict[str, Any]) -> None:
        # metadata= makes the row pod-pull work-stealable; the callable is the
        # local-pool fallback (solo/CLI mode) — both run the same executor.
        try:
            self._bjm.submit_job(
                FTS_BACKFILL_BUILD_OP,
                lambda: fts_backfill_build_executor(meta),
                submitter_username="system",
                is_admin=True,
                repo_alias=alias,
                metadata=meta,
            )
        except DuplicateJobError:
            # A build for this repo is already pending/running (a prior cycle) — benign.
            logger.debug("fts_backfill: build for '%s' already queued; skipping", alias)
