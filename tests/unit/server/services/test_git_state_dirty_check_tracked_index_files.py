"""The pre-refresh dirty check ignores only UNTRACKED cidx working paths.

Invariant: cidx's own working paths (``.code-indexer/`` and
``.code-indexer-override.yaml``) are ignorable only while untracked. A
TRACKED modification to either path is a local change like any other: the
pre-refresh clear sees the repository as dirty and resets it.

Every test runs real git commands in a real git repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

from code_indexer.server.services.git_state_manager import GitStateManager

OVERRIDE = ".code-indexer-override.yaml"
INDEX_CONFIG = Path(".code-indexer") / "config.json"
COMMITTED = "add_extensions: []\n"


def _git(args: List[str], repo: Path) -> str:
    return subprocess.run(
        ["git"] + args, cwd=str(repo), check=True, capture_output=True, text=True
    ).stdout


def _repo(tmp_path: Path, tracked: List[Path]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("readme\n")
    for path in tracked:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(COMMITTED)
    _git(["add", "-f", "README.md"] + [str(p) for p in tracked], repo)
    _git(["commit", "-q", "-m", "first"], repo)
    return repo


def test_tracked_modified_override_file_is_dirty_and_reset(tmp_path: Path) -> None:
    repo = _repo(tmp_path, [Path(OVERRIDE)])
    (repo / OVERRIDE).write_text("add_extensions: [local]\n")

    result = GitStateManager(config=None).clear_repo_before_refresh(repo_path=repo)

    assert result.was_dirty is True
    assert (repo / OVERRIDE).read_text() == COMMITTED


def test_tracked_modified_file_under_index_dir_is_dirty_and_reset(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, [INDEX_CONFIG])
    (repo / INDEX_CONFIG).write_text("{}\n")

    result = GitStateManager(config=None).clear_repo_before_refresh(repo_path=repo)

    assert result.was_dirty is True
    assert (repo / INDEX_CONFIG).read_text() == COMMITTED


def test_untracked_index_dir_alone_is_not_dirty(tmp_path: Path) -> None:
    repo = _repo(tmp_path, [])
    (repo / INDEX_CONFIG).parent.mkdir()
    (repo / INDEX_CONFIG).write_text("{}\n")
    (repo / OVERRIDE).write_text(COMMITTED)

    result = GitStateManager(config=None).clear_repo_before_refresh(repo_path=repo)

    assert result.was_dirty is False
    assert (repo / INDEX_CONFIG).read_text() == "{}\n"
    assert (repo / OVERRIDE).read_text() == COMMITTED
