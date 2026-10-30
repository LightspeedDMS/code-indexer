"""Cleanup never deletes a path an alias points at (current or previous
target), re-checked when it is about to delete: a fenced run may schedule
the very snapshot path another run then published (real AliasManager on
disk, real CleanupManager, real directories)."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker

ALIAS = "example-repo-global"


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    root = tmp_path / "golden-repos"
    (root / "aliases").mkdir(parents=True)
    return root


def _snapshot(golden: Path, name: str) -> str:
    path = golden / ".versioned" / "example-repo" / name
    path.mkdir(parents=True)
    (path / "chunks.db").write_text("index")
    return str(path)


def _cleanup(golden: Path) -> CleanupManager:
    manager = CleanupManager(QueryTracker(), min_retention_age_seconds=0)
    manager.set_alias_manager(AliasManager(str(golden / "aliases")))
    return manager


def test_snapshot_published_by_another_run_survives_its_cleanup(golden: Path):
    shared = _snapshot(golden, "v_1")
    fenced_run_cleanup = _cleanup(golden)
    new_owner_aliases = AliasManager(str(golden / "aliases"))
    fenced_run_cleanup.schedule_cleanup(shared)
    new_owner_aliases.create_alias(ALIAS, shared)

    fenced_run_cleanup._process_cleanup_queue()

    assert Path(shared).exists(), "the published snapshot was deleted"
    assert shared in fenced_run_cleanup.get_pending_cleanups(), (
        "it stays queued, not dropped"
    )


def test_previous_target_survives_until_a_later_swap_moves_it_out(golden: Path):
    aliases = AliasManager(str(golden / "aliases"))
    v1, v2, v3 = (_snapshot(golden, n) for n in ("v_1", "v_2", "v_3"))
    aliases.create_alias(ALIAS, v1)
    aliases.swap_alias(ALIAS, new_target=v2, old_target=v1)
    cleanup = _cleanup(golden)
    cleanup.schedule_cleanup(v1)

    cleanup._process_cleanup_queue()
    assert Path(v1).exists(), "a previous_path must not be deleted"

    aliases.swap_alias(ALIAS, new_target=v3, old_target=v2)
    cleanup._process_cleanup_queue()
    assert not Path(v1).exists(), "no longer current or previous: it is deleted"
    assert v1 not in cleanup.get_pending_cleanups()


def test_unreadable_alias_file_defers_every_deletion(golden: Path):
    orphan = _snapshot(golden, "v_9")
    (golden / "aliases" / "broken-global.json").write_text("{not json")
    cleanup = _cleanup(golden)
    cleanup.schedule_cleanup(orphan)

    cleanup._process_cleanup_queue()

    assert Path(orphan).exists(), "cannot prove the path unreferenced: keep it"
    assert orphan in cleanup.get_pending_cleanups()


def test_unreferenced_path_is_deleted(golden: Path):
    aliases = AliasManager(str(golden / "aliases"))
    current = _snapshot(golden, "v_2")
    aliases.create_alias(ALIAS, current)
    orphan = _snapshot(golden, "v_1")
    cleanup = _cleanup(golden)
    cleanup.schedule_cleanup(orphan)

    cleanup._process_cleanup_queue()

    assert not Path(orphan).exists()
    assert Path(current).exists()
