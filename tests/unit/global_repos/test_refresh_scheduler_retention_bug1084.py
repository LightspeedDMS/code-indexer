"""Bug #1084 Phase A6: keep-last-N versioned-snapshot retention.

After a successful alias swap, RefreshScheduler._enforce_retention lists snapshots
via the discovery API and schedules deletion (through the refcount-gated
CleanupManager) of all but the N newest — NEVER the current target_path or
previous_path. N is the runtime config knob snapshot_retention_keep_last (default 3).
"""

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


def _make_scheduler(golden_repos_dir, snapshot_manager, cleanup_manager):
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=cleanup_manager,
        registry=MagicMock(),
        snapshot_manager=snapshot_manager,
    )


def _snapshot_manager_with(snaps):
    """snaps: list of (path, ts). list_snapshots returns them (ascending)."""
    sm = MagicMock()
    sm.list_snapshots.return_value = sorted(snaps, key=lambda x: x[1])
    return sm


def _seed_alias(scheduler, alias_name, target_path, previous_path=None):
    if previous_path is not None:
        # Create at previous_path, then swap to target_path so swap_alias records
        # previous_path correctly (its old_target must match the current target).
        scheduler.alias_manager.create_alias(
            alias_name, previous_path, repo_name="my-repo"
        )
        scheduler.alias_manager.swap_alias(
            alias_name=alias_name,
            new_target=target_path,
            old_target=previous_path,
        )
    else:
        scheduler.alias_manager.create_alias(
            alias_name, target_path, repo_name="my-repo"
        )


class TestKeepLastN:
    def test_keeps_n_newest_schedules_rest(self, golden_repos_dir):
        """With N=3 and 5 snapshots, the 2 oldest are scheduled; 3 newest kept."""
        cm = MagicMock(spec=CleanupManager)
        snaps = [
            ("/mnt/cow/.versioned/my-repo/v_100", 100),
            ("/mnt/cow/.versioned/my-repo/v_200", 200),
            ("/mnt/cow/.versioned/my-repo/v_300", 300),
            ("/mnt/cow/.versioned/my-repo/v_400", 400),
            ("/mnt/cow/.versioned/my-repo/v_500", 500),
        ]
        sm = _snapshot_manager_with(snaps)
        sched = _make_scheduler(golden_repos_dir, sm, cm)
        # current target is the newest; no previous.
        _seed_alias(sched, "my-repo-global", "/mnt/cow/.versioned/my-repo/v_500")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service"
        ) as gcs:
            gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 3
            sched._enforce_retention(
                "my-repo-global", "/mnt/cow/.versioned/my-repo/v_500"
            )

        scheduled = {c.args[0] for c in cm.schedule_cleanup.call_args_list}
        assert scheduled == {
            "/mnt/cow/.versioned/my-repo/v_100",
            "/mnt/cow/.versioned/my-repo/v_200",
        }

    def test_never_deletes_current_or_previous(self, golden_repos_dir):
        """current target_path and previous_path are force-kept even if they fall
        outside the N-newest window."""
        cm = MagicMock(spec=CleanupManager)
        # 5 snapshots; N=1 would normally keep only v_500, but current=v_300 and
        # previous=v_200 must also survive.
        snaps = [
            ("/mnt/cow/.versioned/my-repo/v_100", 100),
            ("/mnt/cow/.versioned/my-repo/v_200", 200),
            ("/mnt/cow/.versioned/my-repo/v_300", 300),
            ("/mnt/cow/.versioned/my-repo/v_400", 400),
            ("/mnt/cow/.versioned/my-repo/v_500", 500),
        ]
        sm = _snapshot_manager_with(snaps)
        sched = _make_scheduler(golden_repos_dir, sm, cm)
        _seed_alias(
            sched,
            "my-repo-global",
            "/mnt/cow/.versioned/my-repo/v_300",
            previous_path="/mnt/cow/.versioned/my-repo/v_200",
        )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service"
        ) as gcs:
            gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 1
            sched._enforce_retention(
                "my-repo-global", "/mnt/cow/.versioned/my-repo/v_300"
            )

        scheduled = {c.args[0] for c in cm.schedule_cleanup.call_args_list}
        # Kept: v_500 (N-newest=1), v_300 (current), v_200 (previous).
        # Scheduled: v_100, v_400.
        assert "/mnt/cow/.versioned/my-repo/v_300" not in scheduled
        assert "/mnt/cow/.versioned/my-repo/v_200" not in scheduled
        assert "/mnt/cow/.versioned/my-repo/v_500" not in scheduled
        assert scheduled == {
            "/mnt/cow/.versioned/my-repo/v_100",
            "/mnt/cow/.versioned/my-repo/v_400",
        }

    def test_no_op_when_at_or_below_n(self, golden_repos_dir):
        """When snapshot count <= N, nothing is scheduled."""
        cm = MagicMock(spec=CleanupManager)
        snaps = [
            ("/mnt/cow/.versioned/my-repo/v_100", 100),
            ("/mnt/cow/.versioned/my-repo/v_200", 200),
        ]
        sm = _snapshot_manager_with(snaps)
        sched = _make_scheduler(golden_repos_dir, sm, cm)
        _seed_alias(sched, "my-repo-global", "/mnt/cow/.versioned/my-repo/v_200")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service"
        ) as gcs:
            gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 3
            sched._enforce_retention(
                "my-repo-global", "/mnt/cow/.versioned/my-repo/v_200"
            )

        cm.schedule_cleanup.assert_not_called()

    def test_ontap_empty_discovery_is_inert(self, golden_repos_dir):
        """ONTAP discovery returns [] -> retention naturally does nothing."""
        cm = MagicMock(spec=CleanupManager)
        sm = _snapshot_manager_with([])
        sched = _make_scheduler(golden_repos_dir, sm, cm)
        _seed_alias(sched, "my-repo-global", "/mnt/fsx/v_500")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service"
        ) as gcs:
            gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 3
            sched._enforce_retention("my-repo-global", "/mnt/fsx/v_500")

        cm.schedule_cleanup.assert_not_called()

    def test_retention_failure_is_non_fatal(self, golden_repos_dir):
        """A discovery/list error must not raise out of _enforce_retention."""
        cm = MagicMock(spec=CleanupManager)
        sm = MagicMock()
        sm.list_snapshots.side_effect = RuntimeError("daemon down")
        sched = _make_scheduler(golden_repos_dir, sm, cm)
        _seed_alias(sched, "my-repo-global", "/mnt/cow/.versioned/my-repo/v_500")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service"
        ) as gcs:
            gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 3
            # Must not raise.
            sched._enforce_retention(
                "my-repo-global", "/mnt/cow/.versioned/my-repo/v_500"
            )

        cm.schedule_cleanup.assert_not_called()

    def test_invalid_keep_last_falls_back_to_default(self, golden_repos_dir):
        """A keep-last < 1 is treated as the default 3 (never delete everything)."""
        cm = MagicMock(spec=CleanupManager)
        snaps = [
            ("/mnt/cow/.versioned/my-repo/v_100", 100),
            ("/mnt/cow/.versioned/my-repo/v_200", 200),
            ("/mnt/cow/.versioned/my-repo/v_300", 300),
            ("/mnt/cow/.versioned/my-repo/v_400", 400),
        ]
        sm = _snapshot_manager_with(snaps)
        sched = _make_scheduler(golden_repos_dir, sm, cm)
        _seed_alias(sched, "my-repo-global", "/mnt/cow/.versioned/my-repo/v_400")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.get_config_service"
        ) as gcs:
            gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 0
            sched._enforce_retention(
                "my-repo-global", "/mnt/cow/.versioned/my-repo/v_400"
            )

        # With default 3: only v_100 scheduled.
        scheduled = {c.args[0] for c in cm.schedule_cleanup.call_args_list}
        assert scheduled == {"/mnt/cow/.versioned/my-repo/v_100"}
