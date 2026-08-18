"""
Story #1589 acceptance scenarios 2 and 3: /health status recovery after
the Diagnostics tab's "Clear All Dedup Warnings" action.

Mirrors test_health_service_dedup_state_1560.py's real-SQLite fixture
pattern for fleet_migration_dedup_state, and
test_health_service_fleet_migration_unrecoverable_1486.py's
fleet_migration_quarantine_state table for the "unrelated failure"
scenario. Exercises the REAL clear_all_dedup_states write path
(GoldenRepoMetadataSqliteBackend against the SAME db file
HealthCheckService reads) -- not a mock -- so this proves the write and
read sides genuinely compose, not merely that each was unit-tested in
isolation.
"""

import sqlite3
from contextlib import closing

from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.models.api_models import HealthStatus, SystemHealthInfo
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend

_CLEAR_ALL_REASON = "manually acknowledged via Diagnostics tab"


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


def _make_service_with_active_dedup_row(tmp_path, golden_alias: str = "click"):
    """Real SQLite db with a genuinely active fleet_migration_dedup_state
    row, created via the REAL backend (record_dedup_outcome), not a
    hand-crafted INSERT -- so the schema is exactly what production
    writes."""
    db_path = str(tmp_path / "cidx_server.db")
    with closing(GoldenRepoMetadataSqliteBackend(db_path)) as backend:
        backend.ensure_table_exists()
        backend.record_dedup_outcome(
            golden_alias,
            duplicate_groups=33,
            records_before=343604,
            records_deleted=43,
            winner_kept_groups=23,
            whole_group_deleted_groups=10,
            collection_total=343604,
        )

    service = HealthCheckService()
    service.database_url = f"sqlite:///{db_path}"
    return service, db_path


def _add_unrelated_unrecoverable_failure(db_path: str, golden_alias: str) -> None:
    """Insert an UNRELATED active failure into
    fleet_migration_quarantine_state (Bug #1486's escalation surface),
    matching test_health_service_fleet_migration_unrecoverable_1486.py's
    schema exactly."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fleet_migration_quarantine_state (
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
            (golden_alias, 3, "unrecoverable_corruption"),
        )
        conn.commit()
    finally:
        conn.close()


class TestHealthRecoversWhenDedupWasSoleDegradationSource:
    def test_status_is_degraded_before_clearing(self, tmp_path):
        service, _db_path = _make_service_with_active_dedup_row(tmp_path)

        status, failure_reasons = service._calculate_overall_status(
            {}, _system_info(), []
        )

        assert status == HealthStatus.DEGRADED
        assert any("click" in reason for reason in failure_reasons)

    def test_status_recovers_to_healthy_after_clear_all(self, tmp_path):
        service, db_path = _make_service_with_active_dedup_row(tmp_path)

        with closing(GoldenRepoMetadataSqliteBackend(db_path)) as clearing_backend:
            cleared_count = clearing_backend.clear_all_dedup_states(_CLEAR_ALL_REASON)
        assert cleared_count == 1

        status, failure_reasons = service._calculate_overall_status(
            {}, _system_info(), []
        )

        assert status == HealthStatus.HEALTHY
        assert not any("click" in reason for reason in failure_reasons)
        assert not any(
            "dropped duplicate record" in reason for reason in failure_reasons
        )


class TestHealthStaysDegradedWhenUnrelatedFailurePersists:
    def test_dedup_reason_removed_but_unrelated_failure_remains(self, tmp_path):
        service, db_path = _make_service_with_active_dedup_row(tmp_path)
        _add_unrelated_unrecoverable_failure(db_path, "evolution")

        status_before, reasons_before = service._calculate_overall_status(
            {}, _system_info(), []
        )
        assert status_before == HealthStatus.DEGRADED
        unrelated_reason = next(r for r in reasons_before if "evolution" in r)

        with closing(GoldenRepoMetadataSqliteBackend(db_path)) as clearing_backend:
            clearing_backend.clear_all_dedup_states(_CLEAR_ALL_REASON)

        status_after, reasons_after = service._calculate_overall_status(
            {}, _system_info(), []
        )

        assert status_after == HealthStatus.DEGRADED
        assert not any("click" in reason for reason in reasons_after)
        assert unrelated_reason in reasons_after
