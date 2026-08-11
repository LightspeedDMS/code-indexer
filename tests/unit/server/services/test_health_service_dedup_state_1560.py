"""
Unit tests for Story #1560's /health surface wiring (AC13/AC14/AC15/
AC16/AC17): duplicate-point-id auto-resolution outcome state.

Mirrors test_health_service_fleet_migration_unrecoverable_1486.py's
exact fixture pattern -- a REAL SQLite database, no mocking of the
service under test. Uses pytest's `tmp_path` fixture (rather than a
bare `tempfile.mkdtemp()`) so the temp directory is cleaned up
automatically by pytest after each test.
"""

import sqlite3

from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.models.api_models import HealthStatus, SystemHealthInfo


def _make_service_with_temp_db(tmp_path, create_table: bool = False, rows=None):
    """Build a HealthCheckService pointed at a temp SQLite DB (under
    pytest's auto-cleaned tmp_path), optionally pre-populated with
    fleet_migration_dedup_state rows. Each row tuple:
    (golden_alias, duplicate_groups, records_before, records_deleted,
    winner_kept_groups, whole_group_deleted_groups, collection_total,
    dropped_at, cleared_at)."""
    service = HealthCheckService()
    db_path = str(tmp_path / "cidx_server.db")
    service.database_url = f"sqlite:///{db_path}"

    if create_table:
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
            for row in rows or []:
                (
                    alias,
                    dup_groups,
                    rec_before,
                    rec_deleted,
                    winner_kept,
                    whole_deleted,
                    coll_total,
                    dropped_at,
                    cleared_at,
                ) = row
                conn.execute(
                    "INSERT INTO fleet_migration_dedup_state "
                    "(golden_alias, duplicate_groups, records_before, "
                    "records_deleted, winner_kept_groups, "
                    "whole_group_deleted_groups, collection_total, "
                    "first_dropped_at, dropped_at, cleared_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        alias,
                        dup_groups,
                        rec_before,
                        rec_deleted,
                        winner_kept,
                        whole_deleted,
                        coll_total,
                        dropped_at,
                        dropped_at,
                        cleared_at,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    return service


def _system_info() -> SystemHealthInfo:
    return SystemHealthInfo(
        memory_usage_percent=20.0,
        cpu_usage_percent=20.0,
        active_jobs=0,
        disk_free_space_gb=200.0,
        disk_read_kb_s=0.0,
        disk_write_kb_s=0.0,
        net_rx_kb_s=0.0,
        net_tx_kb_s=0.0,
    )


class TestFailOpen:
    def test_no_table_returns_no_failure(self, tmp_path):
        service = _make_service_with_temp_db(tmp_path, create_table=False)
        has_warning, has_error, reasons = (
            service._collect_fleet_migration_dedup_failures()
        )
        assert (has_warning, has_error, reasons) == (False, False, [])

    def test_no_table_summary_returns_none(self, tmp_path):
        service = _make_service_with_temp_db(tmp_path, create_table=False)
        assert service.get_fleet_migration_dedup_state_summary() is None


class TestSummarySchemaAndBounding:
    def test_active_row_appears_with_loss_ratio_and_incomplete_flag(self, tmp_path):
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                (
                    "click",
                    33,
                    343604,
                    43,
                    23,
                    10,
                    343604,
                    "2026-08-11T00:00:00+00:00",
                    None,
                )
            ],
        )
        summary = service.get_fleet_migration_dedup_state_summary()
        assert summary is not None
        assert summary["affected_total"] == 1
        assert len(summary["repos"]) == 1
        entry = summary["repos"][0]
        assert entry["golden_alias"] == "click"
        assert entry["records_deleted"] == 43
        assert entry["collection_total"] == 343604
        assert entry["incomplete"] is True
        assert abs(entry["loss_ratio"] - (43 / 343604)) < 1e-9

    def test_cleared_row_is_excluded(self, tmp_path):
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                (
                    "click",
                    1,
                    10,
                    1,
                    1,
                    0,
                    10,
                    "2026-08-11T00:00:00+00:00",
                    "2026-08-12T00:00:00+00:00",
                )
            ],
        )
        assert service.get_fleet_migration_dedup_state_summary() is None

    def test_null_dropped_at_sorts_last(self, tmp_path):
        """AC16/R3: NULL dropped_at must sort LAST regardless of the
        DESC ordering on the timestamped rows."""
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                ("no-timestamp", 1, 10, 1, 1, 0, 10, None, None),
                (
                    "has-timestamp",
                    1,
                    10,
                    1,
                    1,
                    0,
                    10,
                    "2026-08-11T00:00:00+00:00",
                    None,
                ),
            ],
        )
        summary = service.get_fleet_migration_dedup_state_summary()
        assert summary is not None
        aliases_in_order = [r["golden_alias"] for r in summary["repos"]]
        assert aliases_in_order == ["has-timestamp", "no-timestamp"]

    def test_alias_ascending_tiebreak_for_equal_timestamps(self, tmp_path):
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                (
                    "zulu",
                    1,
                    10,
                    1,
                    1,
                    0,
                    10,
                    "2026-08-11T00:00:00+00:00",
                    None,
                ),
                (
                    "alpha",
                    1,
                    10,
                    1,
                    1,
                    0,
                    10,
                    "2026-08-11T00:00:00+00:00",
                    None,
                ),
            ],
        )
        summary = service.get_fleet_migration_dedup_state_summary()
        assert summary is not None
        aliases_in_order = [r["golden_alias"] for r in summary["repos"]]
        assert aliases_in_order == ["alpha", "zulu"]


class TestWiredIntoOverallStatus:
    def test_active_dedup_row_degrades_overall_health_status(self, tmp_path):
        """_calculate_overall_status() must actually call
        _collect_fleet_migration_dedup_failures() and fold an active
        dedup-state row into the returned (status, failure_reasons) --
        wiring it, not just implementing it as dead code (Messi Rule
        #12: anti-orphan-code)."""
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                (
                    "click",
                    33,
                    343604,
                    43,
                    23,
                    10,
                    343604,
                    "2026-08-11T00:00:00+00:00",
                    None,
                )
            ],
        )

        status, failure_reasons = service._calculate_overall_status(
            {}, _system_info(), []
        )

        assert status == HealthStatus.DEGRADED
        assert any("click" in reason for reason in failure_reasons)

    def test_no_active_rows_never_degrades(self, tmp_path):
        service = _make_service_with_temp_db(tmp_path, create_table=False)

        status, _ = service._calculate_overall_status({}, _system_info(), [])

        assert status == HealthStatus.HEALTHY
