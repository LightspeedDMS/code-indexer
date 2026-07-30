"""
Unit tests for the fleet-migration permanent-unrecoverable-corruption
escalation surface on the /health endpoint (Bug #1486 High Finding 4).

Before this fix, a repo whose migration was classified as PERMANENTLY
unrecoverable (chunks.db corrupt, legacy source already gone --
UNRECOVERABLE_FAILURE_CAUSE in quarantine.py) was invisible on /health --
an operator would never learn a golden repo needs manual data recovery
unless they went log-searching. This test module covers the new
HealthCheckService._collect_fleet_migration_unrecoverable_failures()
method, which reads the SAME fleet_migration_quarantine_state table
quarantine.py's record_unrecoverable_corruption()/is_permanently_
unrecoverable() already write/read, and surfaces any row with
failure_cause == 'unrecoverable_corruption' as a DEGRADED health
failure_reason -- reusing the EXISTING /health failure_reasons surface,
mirroring _collect_golden_repo_reconcile_breaker_failures() (Bug #1382).

Fail-open discipline: any error reading the table (including "the table
does not exist yet" on a fresh install) must NEVER produce a false
health alarm.
"""

import os
import sqlite3
import tempfile

from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.models.api_models import HealthStatus, SystemHealthInfo


def _make_service_with_temp_db(create_table: bool = False, rows=None):
    """Build a HealthCheckService pointed at a temp SQLite DB, optionally
    pre-populated with fleet_migration_quarantine_state rows."""
    service = HealthCheckService()
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "cidx_server.db")
    service.database_url = f"sqlite:///{db_path}"

    if create_table:
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
            for row in rows or []:
                conn.execute(
                    "INSERT INTO fleet_migration_quarantine_state "
                    "(golden_alias, consecutive_failure_count, failure_cause) "
                    "VALUES (?, ?, ?)",
                    row,
                )
            conn.commit()
        finally:
            conn.close()

    return service


class TestFleetMigrationUnrecoverableHealthCheckFailOpen:
    def test_no_table_returns_no_failure(self):
        """Fresh install / no fleet migration has ever run -- table
        doesn't exist yet. Must be fail-open: no failure, no crash."""
        service = _make_service_with_temp_db(create_table=False)

        has_warning, has_error, reasons = (
            service._collect_fleet_migration_unrecoverable_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []

    def test_table_exists_with_no_unrecoverable_rows_returns_no_failure(self):
        """Table exists (ordinary quarantine bookkeeping has run) but no
        row is classified as permanently unrecoverable -- no failure."""
        service = _make_service_with_temp_db(
            create_table=True, rows=[("click", 3, "generic")]
        )

        has_warning, has_error, reasons = (
            service._collect_fleet_migration_unrecoverable_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []


class TestFleetMigrationUnrecoverableHealthCheckReports:
    def test_unrecoverable_row_reports_degraded_warning_naming_the_alias(self):
        """A repo permanently classified as unrecoverable-corrupt must
        surface as a DEGRADED (warning, not full outage) failure_reason
        naming the affected golden alias -- the core Bug #1486 High
        Finding 4 fix: visible on /health, not just in a bookkeeping
        table nobody queries."""
        service = _make_service_with_temp_db(
            create_table=True,
            rows=[("evolution", 1, "unrecoverable_corruption")],
        )

        has_warning, has_error, reasons = (
            service._collect_fleet_migration_unrecoverable_failures()
        )

        assert has_warning is True
        assert has_error is False
        assert len(reasons) == 1
        assert "evolution" in reasons[0]

    def test_multiple_unrecoverable_rows_all_named(self):
        service = _make_service_with_temp_db(
            create_table=True,
            rows=[
                ("evolution", 1, "unrecoverable_corruption"),
                ("other-repo", 1, "unrecoverable_corruption"),
                ("click", 3, "generic"),
            ],
        )

        has_warning, has_error, reasons = (
            service._collect_fleet_migration_unrecoverable_failures()
        )

        assert has_warning is True
        assert has_error is False
        combined = " ".join(reasons)
        assert "evolution" in combined
        assert "other-repo" in combined
        assert "click" not in combined


class TestFleetMigrationUnrecoverableWiredIntoOverallStatus:
    def test_unrecoverable_repo_degrades_overall_health_status(self):
        """_calculate_overall_status() must actually call
        _collect_fleet_migration_unrecoverable_failures() and fold a
        permanently-unrecoverable repo into the returned (status,
        failure_reasons) -- wiring it, not just implementing it as dead
        code (Messi Rule #12: anti-orphan-code)."""
        service = _make_service_with_temp_db(
            create_table=True,
            rows=[("evolution", 1, "unrecoverable_corruption")],
        )
        system_info = SystemHealthInfo(
            memory_usage_percent=20.0,
            cpu_usage_percent=20.0,
            active_jobs=0,
            disk_free_space_gb=200.0,
            disk_read_kb_s=0.0,
            disk_write_kb_s=0.0,
            net_rx_kb_s=0.0,
            net_tx_kb_s=0.0,
        )

        status, failure_reasons = service._calculate_overall_status({}, system_info, [])

        assert status == HealthStatus.DEGRADED
        assert any("evolution" in reason for reason in failure_reasons)
