"""Read-only discovery of legacy and fixed temporal roots."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)


@dataclass(frozen=True)
class TemporalMigrationCandidate:
    alias: str
    legacy_root: Path
    fixed_root: Path


def discover_candidates(
    golden_repo_manager: Any,
) -> Iterator[TemporalMigrationCandidate]:
    """Yield mutable golden-repo roots in stable alias order."""
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
        yield TemporalMigrationCandidate(
            alias=alias,
            legacy_root=repo / ".code-indexer" / "index",
            fixed_root=server_temporal_index_root(repo.parent, alias),
        )
