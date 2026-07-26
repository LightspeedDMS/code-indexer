"""GlobalReposLifecycleManager wires CleanupManager's min_retention_age_getter
to the runtime-configurable snapshot_min_retention_age_seconds setting
(Story #1457 AC13 PT-13 follow-up).

Anti-orphan-code guard (Rule 12): CleanupManager.min_retention_age_getter
exists only to be wired here. If this wiring regresses, the AC13 floor
silently reverts to the hardcoded 900s constant and the Web UI Config Screen
setting becomes a dead knob nobody reads.

Mirrors the EXISTING, accepted test pattern for
snapshot_retention_keep_last (test_refresh_scheduler_retention_bug1084.py):
patch get_config_service() at the DEFINING module's imported namespace
(never let a real ConfigService() construct in a unit test).
"""

from __future__ import annotations

from unittest.mock import patch

from code_indexer.server.lifecycle.global_repos_lifecycle import (
    GlobalReposLifecycleManager,
)


def test_lifecycle_wires_min_retention_age_getter_reading_live_config(tmp_path):
    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
    )

    getter = lifecycle.cleanup_manager._min_retention_age_getter
    assert getter is not None, (
        "GlobalReposLifecycleManager must wire a min_retention_age_getter "
        "into its CleanupManager so the AC13 floor is runtime-configurable"
    )

    with patch(
        "code_indexer.server.lifecycle.global_repos_lifecycle.get_config_service"
    ) as gcs:
        gcs.return_value.get_config.return_value.snapshot_min_retention_age_seconds = (
            42.0
        )
        assert getter() == 42.0

        # Live re-read: a changed config value is reflected on the next call,
        # with no CleanupManager reconstruction.
        gcs.return_value.get_config.return_value.snapshot_min_retention_age_seconds = (
            7.0
        )
        assert getter() == 7.0
