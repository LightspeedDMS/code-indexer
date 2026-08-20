"""EVO-66445: discover golden repos missing a built Tantivy FTS index."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

from ..fts_index_builder import fts_index_present

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FtsBackfillCandidate:
    """A golden repo whose in-place root lacks a built FTS index."""

    alias: str
    repo_path: Path


def discover_repos_missing_fts(golden_repo_manager: Any) -> List[FtsBackfillCandidate]:
    """Enumerate golden repos whose in-place root lacks a built FTS index.

    Reuses the SAME ``list_golden_repos()`` / ``get_actual_repo_path()`` primitives
    the other schedulers reuse (never a third enumeration mechanism). Fail-open per
    repo: an entry with no alias, or one whose path cannot be resolved, is logged
    and skipped -- a single bad repo never aborts the sweep.
    """
    missing: List[FtsBackfillCandidate] = []
    for entry in golden_repo_manager.list_golden_repos():
        alias = entry.get("alias") or entry.get("alias_name")
        if not alias:
            logger.warning(
                "fts_backfill discovery: golden repo entry missing alias: %s", entry
            )
            continue
        try:
            repo_root = Path(golden_repo_manager.get_actual_repo_path(alias))
        except Exception as exc:  # noqa: BLE001 -- fail-open, never abort the sweep
            logger.warning(
                "fts_backfill discovery: cannot resolve path for alias=%s: %s",
                alias,
                exc,
            )
            continue
        if not fts_index_present(repo_root):
            missing.append(FtsBackfillCandidate(alias=alias, repo_path=repo_root))
    return missing
