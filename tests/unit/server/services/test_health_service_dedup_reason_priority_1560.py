"""
Codex review Finding F8 (rated HIGH by the coordinator, live-proven on
staging): HealthCheckService._calculate_overall_status() truncates
`failure_reasons` to MAX_FAILURE_REASONS (3), replacing the overflow
with a bare "+N more" string. Staging's CURRENT /api/system/health
already carries exactly 3 failure_reasons (2 volume-usage warnings + 1
fleet-migration-unrecoverable-corruption entry) BEFORE Story #1560's
dedup reason is ever added -- so the dedup reason (naming the affected
alias, dropped-record count, and loss ratio) lands 4th and is silently
collapsed into "+1 more", never actually visible on /health. This
breaks AC15 and makes AC32's staging verification unobservable.

Fix: whenever active fleet-migration dedup-state rows exist, their
reasons are RESERVED/PRIORITIZED ahead of every other category so they
always survive truncation.

Mirrors test_health_service_fleet_migration_unrecoverable_1486.py's and
test_health_service_dedup_state_1560.py's exact real-SQLite fixture
conventions -- no mocking of the service under test.
"""

import sqlite3

from code_indexer.server.services.health_service import (
    MAX_FAILURE_REASONS,
    HealthCheckService,
)
from code_indexer.server.models.api_models import SystemHealthInfo, VolumeInfo

# Dedup-row fixture values (a realistic outcome from repairing one
# legacy collection with 33 duplicate id_index groups) -- same shape
# already used by test_health_service_dedup_state_1560.py.
_DEDUP_ROW_ALIAS = "click"
_DEDUP_ROW_DUPLICATE_GROUPS = 33
_DEDUP_ROW_RECORDS_BEFORE = 343604
_DEDUP_ROW_RECORDS_DELETED = 43
_DEDUP_ROW_WINNER_KEPT_GROUPS = 23
_DEDUP_ROW_WHOLE_GROUP_DELETED_GROUPS = 10
_DEDUP_ROW_COLLECTION_TOTAL = 343604
_DEDUP_ROW_DROPPED_AT = "2026-08-11T00:00:00+00:00"

# Low-disk-volume fixture values: comfortably above
# DISK_WARNING_THRESHOLD_PERCENT (80%) so each volume produces exactly
# one failure_reasons entry.
_LOW_DISK_TOTAL_GB = 100.0
_LOW_DISK_USED_GB = 99.0
_LOW_DISK_FREE_GB = 1.0
_LOW_DISK_USED_PERCENT = 99.0
_LOW_DISK_FREE_PERCENT = 1.0

# Baseline (non-triggering) resource values for SystemHealthInfo.
_NORMAL_MEMORY_PERCENT = 20.0
_NORMAL_CPU_PERCENT = 20.0
_NORMAL_DISK_FREE_SPACE_GB = 200.0


def _create_unrecoverable_quarantine_row(db_path: str) -> None:
    """Shared setup: the pre-existing fleet_migration_quarantine_state
    table with one 'unrecoverable_corruption' row -- staging's real
    co-occurring shape, reused by every test in this module regardless
    of whether it also adds a dedup-state row."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE fleet_migration_quarantine_state (
                golden_alias TEXT PRIMARY KEY NOT NULL,
                consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
                state_signature TEXT,
                first_failed_at TEXT,
                last_failed_at TEXT,
                updated_at TEXT,
                signature_checked_at TEXT,
                failure_cause TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO fleet_migration_quarantine_state "
            "(golden_alias, consecutive_failure_count, failure_cause) "
            "VALUES (?, ?, ?)",
            ("evolution", 1, "unrecoverable_corruption"),
        )
        conn.commit()
    finally:
        conn.close()


def _add_active_dedup_row(db_path: str) -> None:
    """Adds one active (uncleared) fleet_migration_dedup_state row --
    the SAME fixture shape test_health_service_dedup_state_1560.py
    already uses."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE fleet_migration_dedup_state (
                golden_alias TEXT PRIMARY KEY NOT NULL,
                duplicate_groups INTEGER NOT NULL DEFAULT 0,
                records_before INTEGER NOT NULL DEFAULT 0,
                records_deleted INTEGER NOT NULL DEFAULT 0,
                winner_kept_groups INTEGER NOT NULL DEFAULT 0,
                whole_group_deleted_groups INTEGER NOT NULL DEFAULT 0,
                collection_total INTEGER NOT NULL DEFAULT 0,
                first_dropped_at TEXT,
                dropped_at TEXT,
                cleared_at TEXT,
                cleared_reason TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO fleet_migration_dedup_state "
            "(golden_alias, duplicate_groups, records_before, "
            "records_deleted, winner_kept_groups, "
            "whole_group_deleted_groups, collection_total, "
            "first_dropped_at, dropped_at, cleared_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                _DEDUP_ROW_ALIAS,
                _DEDUP_ROW_DUPLICATE_GROUPS,
                _DEDUP_ROW_RECORDS_BEFORE,
                _DEDUP_ROW_RECORDS_DELETED,
                _DEDUP_ROW_WINNER_KEPT_GROUPS,
                _DEDUP_ROW_WHOLE_GROUP_DELETED_GROUPS,
                _DEDUP_ROW_COLLECTION_TOTAL,
                _DEDUP_ROW_DROPPED_AT,
                _DEDUP_ROW_DROPPED_AT,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _make_service_with_temp_db(tmp_path, *, with_dedup_row: bool) -> HealthCheckService:
    service = HealthCheckService()
    db_path = str(tmp_path / "cidx_server.db")
    service.database_url = f"sqlite:///{db_path}"
    _create_unrecoverable_quarantine_row(db_path)
    if with_dedup_row:
        _add_active_dedup_row(db_path)
    return service


def _low_disk_volume(mount_point: str) -> VolumeInfo:
    """A volume crossing DISK_WARNING_THRESHOLD_PERCENT (80%) -- produces
    exactly one failure_reasons entry per call, matching staging's own
    observed "2 volume-usage" reasons."""
    return VolumeInfo(
        mount_point=mount_point,
        device="/dev/sda1",
        fstype="ext4",
        total_gb=_LOW_DISK_TOTAL_GB,
        used_gb=_LOW_DISK_USED_GB,
        free_gb=_LOW_DISK_FREE_GB,
        used_percent=_LOW_DISK_USED_PERCENT,
        free_percent=_LOW_DISK_FREE_PERCENT,
    )


def _system_info_with_two_low_disk_volumes() -> SystemHealthInfo:
    return SystemHealthInfo(
        memory_usage_percent=_NORMAL_MEMORY_PERCENT,
        cpu_usage_percent=_NORMAL_CPU_PERCENT,
        active_jobs=0,
        disk_free_space_gb=_NORMAL_DISK_FREE_SPACE_GB,
        disk_read_kb_s=0.0,
        disk_write_kb_s=0.0,
        net_rx_kb_s=0.0,
        net_tx_kb_s=0.0,
        volumes=[_low_disk_volume("/"), _low_disk_volume("/home")],
    )


def _system_info_with_three_low_disk_volumes() -> SystemHealthInfo:
    """3 volume reasons + 1 unrecoverable-corruption reason = 4 total
    'other' reasons on its own -- genuinely exercises truncation even
    with NO dedup row present."""
    return SystemHealthInfo(
        memory_usage_percent=_NORMAL_MEMORY_PERCENT,
        cpu_usage_percent=_NORMAL_CPU_PERCENT,
        active_jobs=0,
        disk_free_space_gb=_NORMAL_DISK_FREE_SPACE_GB,
        disk_read_kb_s=0.0,
        disk_write_kb_s=0.0,
        net_rx_kb_s=0.0,
        net_tx_kb_s=0.0,
        volumes=[
            _low_disk_volume("/"),
            _low_disk_volume("/home"),
            _low_disk_volume("/var"),
        ],
    )


class TestDedupReasonSurvivesTruncation:
    def test_dedup_reason_is_never_swallowed_by_plus_n_more(self, tmp_path):
        """Reproduces staging's exact live shape: 2 volume reasons + 1
        unrecoverable-corruption reason (3, == MAX_FAILURE_REASONS)
        ALREADY present before the 4th (dedup) reason is even
        considered. Before the fix, positional truncation drops the
        dedup reason into '+1 more'; after the fix it must survive,
        naming the affected alias."""
        service = _make_service_with_temp_db(tmp_path, with_dedup_row=True)
        system_info = _system_info_with_two_low_disk_volumes()

        _, failure_reasons = service._calculate_overall_status({}, system_info, [])

        assert len(failure_reasons) == MAX_FAILURE_REASONS + 1
        assert any(_DEDUP_ROW_ALIAS in reason for reason in failure_reasons), (
            f"dedup reason for {_DEDUP_ROW_ALIAS!r} was swallowed by "
            f"truncation: {failure_reasons}"
        )

    def test_no_dedup_state_leaves_truncation_unchanged(self, tmp_path):
        """Regression: with NO active dedup rows, truncation still fires
        (3 volume reasons + 1 unrecoverable reason = 4, > MAX) exactly
        as it did before this fix -- 3 kept, '+1 more' appended."""
        service = _make_service_with_temp_db(tmp_path, with_dedup_row=False)
        system_info = _system_info_with_three_low_disk_volumes()

        _, failure_reasons = service._calculate_overall_status({}, system_info, [])

        assert len(failure_reasons) == MAX_FAILURE_REASONS + 1
        assert failure_reasons[-1] == "+1 more"
