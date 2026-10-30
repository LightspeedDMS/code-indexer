"""#2323: remove_watch_leftover removes only what a server watch leaves."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict

import pytest

from code_indexer.server.repositories.watch_leftover import remove_watch_leftover


def _leftover(workspace: Path) -> None:
    (workspace / ".code-indexer").mkdir(parents=True)
    (workspace / ".code-indexer" / "watch_metadata.json").write_text("{}")
    (workspace / ".code-indexer" / "indexing_heartbeat.json").write_text("{}")


def _with_save_temp(workspace: Path) -> None:
    _leftover(workspace)
    (workspace / ".code-indexer" / ".watch_metadata.json.abc123.tmp").write_text("")


def _empty_root(workspace: Path) -> None:
    # ^ hazard removing an empty root => a concurrent activation's reserved clone destination is deleted
    workspace.mkdir(parents=True)


def _heartbeat_only(workspace: Path) -> None:
    (workspace / ".code-indexer").mkdir(parents=True)
    (workspace / ".code-indexer" / "indexing_heartbeat.json").write_text("{}")


def _symlinked_metadata_dir(workspace: Path) -> None:
    target = workspace.parent / "elsewhere"
    _leftover(target)
    (target / ".code-indexer").rename(workspace.parent / "elsewhere-ci")
    workspace.mkdir()
    (workspace / ".code-indexer").symlink_to(workspace.parent / "elsewhere-ci")


def _with_extra_file(workspace: Path) -> None:
    _leftover(workspace)
    (workspace / ".code-indexer" / "config.json").write_text("{}")


def _with_subdirectory(workspace: Path) -> None:
    _leftover(workspace)
    (workspace / ".code-indexer" / "index").mkdir()


def _with_root_file(workspace: Path) -> None:
    _leftover(workspace)
    (workspace / "module.py").write_text("x = 1\n")


def _symlinked_root(workspace: Path) -> None:
    target = workspace.parent / "elsewhere"
    _leftover(target)
    workspace.symlink_to(target)


def _symlinked_leftover_name(workspace: Path) -> None:
    (workspace / ".code-indexer").mkdir(parents=True)
    outside = workspace.parent / "outside.json"
    outside.write_text("{}")
    (workspace / ".code-indexer" / "watch_metadata.json").symlink_to(outside)


REMOVED: Dict[str, Callable[[Path], None]] = {
    "watch files": _leftover,
    "save temp file": _with_save_temp,
}
KEPT: Dict[str, Callable[[Path], None]] = {
    "empty root": _empty_root,
    "heartbeat only": _heartbeat_only,
    "symlinked metadata dir": _symlinked_metadata_dir,
    "extra file": _with_extra_file,
    "subdirectory": _with_subdirectory,
    "root file": _with_root_file,
    "symlinked root": _symlinked_root,
    "symlinked leftover name": _symlinked_leftover_name,
}


@pytest.mark.parametrize("shape", sorted(REMOVED))
def test_pure_watch_leftover_is_removed(tmp_path: Path, shape: str) -> None:
    workspace = tmp_path / "example-repo"
    REMOVED[shape](workspace)

    assert remove_watch_leftover(workspace, "example-repo") is True

    assert not workspace.exists()


@pytest.mark.parametrize("shape", sorted(KEPT))
def test_anything_else_is_kept_untouched(tmp_path: Path, shape: str) -> None:
    workspace = tmp_path / "example-repo"
    KEPT[shape](workspace)
    before = sorted(str(p) for p in tmp_path.rglob("*"))

    assert remove_watch_leftover(workspace, "example-repo") is False

    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


def test_absent_workspace_is_not_a_leftover(tmp_path: Path) -> None:
    assert remove_watch_leftover(tmp_path / "example-repo", "example-repo") is False
