"""Unit tests for trigger_post_consolidation_snapshot() (Story #1458 AC10).

Fires the SINGLE, exactly-once, per-repo post-consolidation snapshot by
calling the LOW-LEVEL publication primitives directly
(``VersionedSnapshotManager.create_snapshot()`` + ``AliasManager.
swap_alias()``), NOT the scheduler-level ``trigger_refresh_for_repo()``/
``_execute_refresh()`` wrapper -- which self-skips while migration holds
the write lock (``refresh_scheduler.py:1873``, `"Skipped, write lock
held"`) and would never publish. Then invokes the SAME retention method the
scheduler itself uses (``RefreshScheduler._enforce_retention``), reusing
the REAL ``RefreshScheduler`` reference migration already holds via AC2's
``acquire_write_lock``.

Real ``RefreshScheduler``, real ``VersionedSnapshotManager`` (local CoW
mode), real ``AliasManager``, real ``QueryTracker``/``CleanupManager`` --
no mocking of the storage layer under test.
"""

from pathlib import Path

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.fleet_migration.snapshot_trigger import (
    trigger_post_consolidation_snapshot,
)
from code_indexer.server.storage.shared.snapshot_manager import (
    VersionedSnapshotManager,
)


def _make_real_scheduler(tmp_path: Path) -> RefreshScheduler:
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    versioned_base = tmp_path / "versioned"
    versioned_base.mkdir(parents=True, exist_ok=True)

    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)
    snapshot_manager = VersionedSnapshotManager(versioned_base=str(versioned_base))

    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=ConfigManager(),
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        snapshot_manager=snapshot_manager,
    )


class TestTriggerPostConsolidationSnapshotFirstPublish:
    def test_creates_first_snapshot_and_publishes_alias_when_none_exists(
        self, tmp_path: Path
    ) -> None:
        scheduler = _make_real_scheduler(tmp_path)
        source_path = tmp_path / "base-clone"
        (source_path / ".code-indexer").mkdir(parents=True)
        (source_path / ".code-indexer" / "config.json").write_text("{}")

        new_target = trigger_post_consolidation_snapshot(
            scheduler, "evolution", str(source_path)
        )

        assert Path(new_target).exists()
        assert (Path(new_target) / ".code-indexer" / "config.json").exists()
        assert scheduler.alias_manager.read_alias("evolution-global") == new_target

    def test_accepts_alias_with_or_without_global_suffix_identically(
        self, tmp_path: Path
    ) -> None:
        scheduler = _make_real_scheduler(tmp_path)
        source_path = tmp_path / "base-clone"
        source_path.mkdir(parents=True)

        new_target = trigger_post_consolidation_snapshot(
            scheduler, "evolution-global", str(source_path)
        )

        assert scheduler.alias_manager.read_alias("evolution-global") == new_target


class TestTriggerPostConsolidationSnapshotRepublish:
    def test_swaps_existing_alias_to_new_snapshot(self, tmp_path: Path) -> None:
        scheduler = _make_real_scheduler(tmp_path)
        source_path = tmp_path / "base-clone"
        (source_path / ".code-indexer").mkdir(parents=True)
        (source_path / ".code-indexer" / "config.json").write_text("{}")

        first_target = trigger_post_consolidation_snapshot(
            scheduler, "evolution", str(source_path)
        )

        # Mutate the base clone (simulating consolidation having changed it)
        # and trigger a second post-consolidation snapshot. No wall-clock
        # sleep needed -- VersionedSnapshotManager._create_cow_snapshot
        # auto-increments the version timestamp on same-second collision.
        (source_path / ".code-indexer" / "marker.txt").write_text("consolidated")

        second_target = trigger_post_consolidation_snapshot(
            scheduler, "evolution", str(source_path)
        )

        assert second_target != first_target
        assert (Path(second_target) / ".code-indexer" / "marker.txt").exists()
        assert scheduler.alias_manager.read_alias("evolution-global") == second_target
        # AC10: previous_path is preserved for rollback (swap_alias's contract) --
        # this is the observable proof that the swap went through swap_alias(),
        # not create_alias() overwriting the pointer without history.
        assert (
            scheduler.alias_manager.get_previous_path("evolution-global")
            == first_target
        )
