"""EVO-66445: the SCIP index-backfill policy for the shared
:class:`GoldenIndexBackfillReconciler`.

The SCIP reconcile (EVO-65484) predates the DRY engine; this policy expresses its
present/eligible/enqueue hooks so it reuses the one shared scan/cooldown/bound loop.
Unlike FTS (which enqueues cheap LOCAL builds as POD_PULL work-stealable jobs), SCIP
funnels to its serialized DELEGATED sandbox queue via ``funnel_golden_scip`` — the
engine is agnostic to that.

It also tracks the granular ``not_discoverable`` vs ``language_skipped`` sub-counts the
SCIP reconcile has always reported (the generic engine only counts a single
``not_eligible``), so the reconcile's observable count contract is unchanged.
"""

from __future__ import annotations

from code_indexer.scip.paths import scip_index_dir
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ScipBackfillPolicy:
    """:class:`IndexBackfillPolicy` for delegated golden-repo SCIP backfill."""

    name = "scip"
    #: kept as the pre-engine SCIP marker so adopting the engine never resets cooldowns.
    attempt_marker = ".scip_reconcile_attempt"

    def __init__(
        self, golden_repo_manager: Any, enabled_langs: set[str], config: Any
    ) -> None:
        self._grm = golden_repo_manager
        self._enabled_langs = enabled_langs
        self._config = config
        # granular reasons the engine's single not_eligible bucket can't distinguish.
        self.not_discoverable = 0
        self.language_skipped = 0

    def is_present(self, repo_path: Path) -> bool:
        return bool(self._grm._scip_present(repo_path))

    def is_fresh(self, repo_path: Path) -> bool:
        # SCIP's own freshness rules, not the generic comparison: a build where
        # every project was an unsupported language is fresh WITHOUT a DB on disk,
        # and a root-buildable repo requires the top-level DB so a stale
        # per-subproject layout does not read as current.
        from code_indexer.server.services.scip_lifecycle import scip_db_is_fresh

        return scip_db_is_fresh(repo_path, scip_index_dir(repo_path))

    def eligibility(self, repo_path: Path) -> Optional[dict[str, Any]]:
        # A discovery error propagates -> the engine counts it as an error (matching the
        # pre-engine behaviour: no marker written, cycle continues).
        from code_indexer.scip.discovery import ProjectDiscovery

        projects = ProjectDiscovery(repo_path).discover()
        if not projects:
            self.not_discoverable += 1
            return None
        if not ({p.language.lower() for p in projects} & self._enabled_langs):
            self.language_skipped += 1
            return None
        return {"alias": repo_path.name}

    def enqueue(self, alias: str, repo_path: Path, meta: dict[str, Any]) -> None:
        from code_indexer.server.services.scip_lifecycle import funnel_golden_scip

        outcome = funnel_golden_scip(alias, str(repo_path), self._config)
        logger.info(
            "EVO-65484: enqueued SCIP build for '%s' (outcome=%s)",
            alias,
            getattr(outcome, "value", outcome),
        )
