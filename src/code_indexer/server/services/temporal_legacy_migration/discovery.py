"""Read-only discovery of legacy and fixed temporal roots."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from code_indexer.server.storage.shared.snapshot_paths import is_versioned_snapshot
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TemporalMigrationCandidate:
    alias: str
    legacy_root: Path
    fixed_root: Path


def discover_candidates(
    golden_repo_manager: Any,
) -> Iterator[TemporalMigrationCandidate]:
    """Yield mutable golden-repo roots in stable alias order.

    Issue #1548 blocker 5: ``get_actual_repo_path`` can resolve to a
    ``.versioned/{alias}/v_*/`` immutable snapshot for repos using
    versioned topology -- this project's CLAUDE.md absolutely prohibits
    writing/deleting inside ``.versioned/``. Any such candidate is skipped
    entirely, using ``is_versioned_snapshot()`` (the designated authority
    for this check), never a hand-rolled substring test.
    """
    entries = sorted(
        golden_repo_manager.list_golden_repos(),
        key=lambda entry: entry.get("alias") or entry.get("alias_name") or "",
    )
    for entry in entries:
        alias = entry.get("alias") or entry.get("alias_name")
        if not alias:
            continue
        repo = Path(golden_repo_manager.get_actual_repo_path(alias))
        if repo.is_symlink() or not repo.is_dir():
            continue
        resolved = str(repo.resolve())
        if is_versioned_snapshot(resolved):
            logger.warning(
                "skipping temporal legacy migration for %r: resolved path "
                "%s is an immutable .versioned/ snapshot",
                alias,
                resolved,
            )
            continue
        yield TemporalMigrationCandidate(
            alias=alias,
            legacy_root=repo / ".code-indexer" / "index",
            fixed_root=server_temporal_index_root(repo.parent, alias),
        )
