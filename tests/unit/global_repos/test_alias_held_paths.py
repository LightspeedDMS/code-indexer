"""AliasManager.alias_held_paths() -- the method CleanupManager._held_by_an_alias
calls to decide whether a path is still reachable through an alias.

Regression coverage: CleanupManager._held_by_an_alias (cleanup_manager.py) was
wired to call self._alias_manager.alias_held_paths() but AliasManager never
defined that method -- every call raised AttributeError, caught by the
broad except and logged at WARNING with no dedup, on the cleanup thread's
poll loop. These tests use a REAL AliasManager (never a Mock/MagicMock,
which would silently auto-create the missing attribute and hide exactly
this bug) so a future regression fails loudly again.
"""

import os

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker


def test_alias_held_paths_empty_when_no_aliases(tmp_path):
    manager = AliasManager(str(tmp_path / "aliases"))
    assert manager.alias_held_paths() == set()


def test_alias_held_paths_includes_current_and_previous_targets(tmp_path):
    manager = AliasManager(str(tmp_path / "aliases"))
    manager.create_alias("repo-global", "/data/snapshots/v1")
    manager.swap_alias(
        "repo-global", new_target="/data/snapshots/v2", old_target="/data/snapshots/v1"
    )

    held = manager.alias_held_paths()

    assert held == {
        os.path.normpath("/data/snapshots/v1"),
        os.path.normpath("/data/snapshots/v2"),
    }


def test_alias_held_paths_excludes_paths_no_alias_points_at(tmp_path):
    manager = AliasManager(str(tmp_path / "aliases"))
    manager.create_alias("repo-global", "/data/snapshots/v1")

    held = manager.alias_held_paths()

    assert "/data/snapshots/unrelated" not in held
    assert os.path.normpath("/data/snapshots/v1") in held


def test_held_by_an_alias_defers_held_path_without_raising(tmp_path):
    """Regression for the missing-method bug: calling through the real
    CleanupManager._held_by_an_alias with a real AliasManager must not raise,
    and must correctly report a currently-held path as held."""
    manager = AliasManager(str(tmp_path / "aliases"))
    manager.create_alias("repo-global", "/data/snapshots/v1")

    cleanup_manager = CleanupManager(query_tracker=QueryTracker())
    cleanup_manager.set_alias_manager(manager)

    assert cleanup_manager._held_by_an_alias("/data/snapshots/v1") is True


def test_held_by_an_alias_allows_unheld_path_without_raising(tmp_path):
    manager = AliasManager(str(tmp_path / "aliases"))
    manager.create_alias("repo-global", "/data/snapshots/v1")

    cleanup_manager = CleanupManager(query_tracker=QueryTracker())
    cleanup_manager.set_alias_manager(manager)

    assert cleanup_manager._held_by_an_alias("/data/snapshots/orphaned") is False
