"""Issue #1548 blocker 5: discovery must skip immutable .versioned/ paths."""

from pathlib import Path
from typing import Any, Dict, List

from code_indexer.server.services.temporal_legacy_migration.discovery import (
    discover_candidates,
)


class _FakeGoldenRepoManager:
    def __init__(self, entries: List[Dict[str, Any]], paths: Dict[str, Path]):
        self._entries = entries
        self._paths = paths

    def list_golden_repos(self):
        return self._entries

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._paths[alias])


def test_versioned_snapshot_path_is_skipped(tmp_path: Path) -> None:
    golden_repos_dir = tmp_path / "golden-repos"
    versioned_repo = golden_repos_dir / ".versioned" / "demo" / "v_1000000000"
    versioned_repo.mkdir(parents=True)

    mutable_repo = golden_repos_dir / "other"
    mutable_repo.mkdir(parents=True)

    manager = _FakeGoldenRepoManager(
        entries=[{"alias": "demo"}, {"alias": "other"}],
        paths={"demo": versioned_repo, "other": mutable_repo},
    )

    candidates = list(discover_candidates(manager))

    assert [c.alias for c in candidates] == ["other"]
