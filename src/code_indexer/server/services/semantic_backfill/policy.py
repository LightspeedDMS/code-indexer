"""Semantic (vector) index backfill policy + its POD_PULL re-embed executor.

Rides the DRY :class:`GoldenIndexBackfillReconciler` exactly like FTS/SCIP: the leader
scans golden repos, and for any that is missing its semantic vector index (and is a
registered golden repo) enqueues ONE ``semantic_backfill_reindex`` job. That op is
POD_PULL + memory-heavy (a re-embed is as heavy as an add), so pods claim DISTINCT
repos under the same memory-admission gate as add_golden_repo.

COST: a semantic re-embed calls VoyageAI ($) — unlike the free local FTS build. So the
policy is inert unless explicitly enabled (``semantic_backfill_sweep_config.enabled``,
DEFAULT FALSE): eligibility returns None when disabled, so the engine enqueues nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from code_indexer.server.repositories.background_jobs import DuplicateJobError

logger = logging.getLogger(__name__)

#: POD_PULL operation type for a single-repo semantic re-embed (must match the dispatch
#: key + POD_PULL_OPS/_MEMORY_HEAVY_OPS membership — see background_jobs).
SEMANTIC_BACKFILL_REINDEX_OP = "semantic_backfill_reindex"

#: Where detect_semantic_index looks (a collection dir holding hnsw_index.bin).
_SEMANTIC_INDEX_SUBDIR = ".code-indexer/index"


def _index_base(repo_path: Path) -> Path:
    return repo_path / _SEMANTIC_INDEX_SUBDIR


def semantic_index_present(repo_path: Path) -> bool:
    """True when the repo has a built semantic vector index (a non-multimodal/temporal/
    tantivy collection with hnsw_index.bin). Reuses the SAME detector the registry
    reconcile gates on, so 'present' means exactly 'the query path can serve it'."""
    from code_indexer.server.routers.repository_health import detect_semantic_index

    return detect_semantic_index(_index_base(repo_path))


def semantic_backfill_reindex_executor(
    golden_repo_manager: Any,
    metadata: Dict[str, Any],
    progress_callback: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Re-embed ONE golden repo's semantic index from the pod-pull ``metadata`` row
    (clone_path + alias), so any pod that claims the job can run it.

    Uses ``_execute_post_clone_workflow(force_init=False)`` — the SAME incremental-refresh
    path the user-facing golden refresh uses: it SKIPS ``cidx init`` when the repo's
    config already exists and runs ``cidx index`` (which rebuilds the missing semantic
    vectors). force_init=False (no ``--force``, no ``--clear``) is deliberate: it must
    NOT reset config or clear the co-located FTS/SCIP indexes — only fill the semantic gap.
    """
    clone_path = metadata["clone_path"]
    alias = metadata.get("alias")
    golden_repo_manager._execute_post_clone_workflow(
        clone_path,
        force_init=False,
        golden_repo_alias=alias,
        job_id=metadata.get("job_id"),
        progress_callback=progress_callback,
    )
    return {"status": "reindexed", "alias": alias, "clone_path": clone_path}


class SemanticBackfillPolicy:
    """:class:`IndexBackfillPolicy` for the semantic vector index. Enqueues per-repo
    POD_PULL re-embed jobs — but ONLY when explicitly enabled (cost gate)."""

    name = "semantic"
    attempt_marker = ".semantic_backfill_attempt"

    def __init__(
        self, golden_repo_manager: Any, background_job_manager: Any, *, enabled: bool
    ) -> None:
        self._grm = golden_repo_manager
        self._bjm = background_job_manager
        self._enabled = enabled

    def is_present(self, repo_path: Path) -> bool:
        return semantic_index_present(repo_path)

    def eligibility(self, repo_path: Path) -> Optional[Dict[str, Any]]:
        # COST GATE: inert unless the operator opts in — a semantic re-embed spends
        # VoyageAI $, so a wiped index is reported but never auto-refilled by default.
        if not self._enabled:
            return None
        alias = repo_path.name
        # Only re-embed REGISTERED golden repos — a stray/partial dir on the shared
        # mount is not ours to (expensively) re-embed. get_golden_repo returns None
        # when unregistered.
        try:
            if self._grm.get_golden_repo(alias) is None:
                return None
        except Exception:  # noqa: BLE001 — a registry glitch must not enqueue a spurious re-embed
            return None
        return {"clone_path": str(repo_path), "alias": alias}

    def enqueue(self, alias: str, repo_path: Path, meta: Dict[str, Any]) -> None:
        # metadata= makes the row pod-pull work-stealable (reconstructed by the
        # lifespan dispatch closure, which supplies grm); the callable is the local-pool
        # fallback (solo/CLI mode). Both run the same executor.
        try:
            self._bjm.submit_job(
                SEMANTIC_BACKFILL_REINDEX_OP,
                lambda progress_callback=None: semantic_backfill_reindex_executor(
                    self._grm, meta, progress_callback
                ),
                submitter_username="system",
                is_admin=True,
                repo_alias=alias,
                metadata=meta,
            )
        except DuplicateJobError:
            logger.debug(
                "semantic_backfill: re-embed for '%s' already queued; skipping", alias
            )
