"""Shared keep-last-N versioned-snapshot retention primitive (Story #1457
MEDIUM #14, 2026-07-23 code review).

`enforce_snapshot_retention` is extracted from
`RefreshScheduler._enforce_retention` (Bug #1084 Phase A6) so temporal
sister-location aliases can reuse the EXACT SAME keep-last-N logic instead
of a reimplementation -- "reuse, don't reinvent" per the review finding.

`discover_and_enforce_temporal_retention` closes the actual gap: temporal
sister aliases (`{repo_alias}-temporal-{embedder_slug}[-{quarter}]`) are
published directly via AliasManager.create_alias/swap_alias, NOT as
golden_repos registry rows, so they are structurally invisible to
RefreshScheduler's per-repo enumeration loop that feeds
`enforce_snapshot_retention` today. This discovers them by globbing the
alias directory (the same on-disk convention AliasManager itself uses:
one `{alias_name}.json` file per alias) and enforces the SAME retention
primitive per discovered alias.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.snapshot_retention import (
    discover_and_enforce_temporal_retention,
    enforce_snapshot_retention,
)


def _snapshot_manager_with(snaps):
    sm = MagicMock()
    sm.list_snapshots.return_value = sorted(snaps, key=lambda x: x[1])
    return sm


def test_enforce_snapshot_retention_keeps_n_newest_schedules_rest(tmp_path):
    cm = MagicMock(spec=CleanupManager)
    alias_manager = AliasManager(str(tmp_path / "aliases"))
    snaps = [
        ("/mnt/cow/.versioned/my-repo/v_100", 100),
        ("/mnt/cow/.versioned/my-repo/v_200", 200),
        ("/mnt/cow/.versioned/my-repo/v_300", 300),
    ]
    sm = _snapshot_manager_with(snaps)
    alias_manager.create_alias(
        "my-repo-global", "/mnt/cow/.versioned/my-repo/v_300", repo_name="my-repo"
    )

    with patch(
        "code_indexer.global_repos.snapshot_retention.get_config_service"
    ) as gcs:
        gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 1
        enforce_snapshot_retention(
            "my-repo-global",
            "/mnt/cow/.versioned/my-repo/v_300",
            snapshot_manager=sm,
            alias_manager=alias_manager,
            cleanup_manager=cm,
        )

    scheduled = {c.args[0] for c in cm.schedule_cleanup.call_args_list}
    # keep_last=1: only v_300 (current target, also newest) is protected --
    # v_100 and v_200 both fall outside the keep window.
    assert scheduled == {
        "/mnt/cow/.versioned/my-repo/v_100",
        "/mnt/cow/.versioned/my-repo/v_200",
    }


def test_discover_and_enforce_temporal_retention_finds_temporal_aliases_only(
    tmp_path,
):
    """A repo's semantic golden alias and OTHER repos' temporal aliases must
    NOT be swept -- only THIS repo's `{repo_alias}-temporal-*` aliases."""
    cm = MagicMock(spec=CleanupManager)
    alias_manager = AliasManager(str(tmp_path / "aliases"))

    alias_manager.create_alias(
        "evolution-global", "/mnt/cow/.versioned/evolution/v_999", repo_name="evolution"
    )
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1",
        "/mnt/cow/.versioned/evolution-temporal-voyage_code_3-2024Q1/v_300",
        repo_name="evolution-temporal-voyage_code_3-2024Q1",
    )
    alias_manager.create_alias(
        "other-repo-temporal-voyage_code_3-2024Q1",
        "/mnt/cow/.versioned/other-repo-temporal-voyage_code_3-2024Q1/v_300",
        repo_name="other-repo-temporal-voyage_code_3-2024Q1",
    )

    snaps = [
        ("/mnt/cow/.versioned/evolution-temporal-voyage_code_3-2024Q1/v_100", 100),
        ("/mnt/cow/.versioned/evolution-temporal-voyage_code_3-2024Q1/v_200", 200),
        ("/mnt/cow/.versioned/evolution-temporal-voyage_code_3-2024Q1/v_300", 300),
    ]
    sm = _snapshot_manager_with(snaps)

    with patch(
        "code_indexer.global_repos.snapshot_retention.get_config_service"
    ) as gcs:
        gcs.return_value.get_config.return_value.snapshot_retention_keep_last = 1
        discover_and_enforce_temporal_retention(
            "evolution",
            snapshot_manager=sm,
            alias_manager=alias_manager,
            cleanup_manager=cm,
        )

    scheduled = {c.args[0] for c in cm.schedule_cleanup.call_args_list}
    assert scheduled == {
        "/mnt/cow/.versioned/evolution-temporal-voyage_code_3-2024Q1/v_100",
        "/mnt/cow/.versioned/evolution-temporal-voyage_code_3-2024Q1/v_200",
    }
    # list_snapshots must have been called ONLY for the temporal alias,
    # never for the semantic golden alias or the other repo's temporal alias.
    called_aliases = {c.args[0] for c in sm.list_snapshots.call_args_list}
    assert called_aliases == {"evolution-temporal-voyage_code_3-2024Q1"}


def test_discover_and_enforce_temporal_retention_no_op_when_no_temporal_aliases(
    tmp_path,
):
    cm = MagicMock(spec=CleanupManager)
    alias_manager = AliasManager(str(tmp_path / "aliases"))
    alias_manager.create_alias(
        "evolution-global", "/mnt/cow/.versioned/evolution/v_999", repo_name="evolution"
    )
    sm = _snapshot_manager_with([])

    discover_and_enforce_temporal_retention(
        "evolution",
        snapshot_manager=sm,
        alias_manager=alias_manager,
        cleanup_manager=cm,
    )

    cm.schedule_cleanup.assert_not_called()
    sm.list_snapshots.assert_not_called()


def test_discover_and_enforce_temporal_retention_no_op_when_snapshot_manager_none(
    tmp_path,
):
    """Mirrors enforce_snapshot_retention's own None-snapshot_manager no-op
    (ONTAP / not-yet-wired case) -- must not raise."""
    cm = MagicMock(spec=CleanupManager)
    alias_manager = AliasManager(str(tmp_path / "aliases"))
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1",
        "/mnt/cow/.versioned/evolution-temporal-voyage_code_3-2024Q1/v_300",
        repo_name="evolution-temporal-voyage_code_3-2024Q1",
    )

    discover_and_enforce_temporal_retention(
        "evolution",
        snapshot_manager=None,
        alias_manager=alias_manager,
        cleanup_manager=cm,
    )

    cm.schedule_cleanup.assert_not_called()
