"""
Unit tests for the cidx-meta backup conflict-resolution quarantine
escalation surface on the /health endpoint (Bug #1555 Defect B).

Bug #1539's quarantine circuit-breaker (see
global_repos/refresh_scheduler.py) is deliberately conservative and
working as designed -- but before this fix, a quarantine that persisted
for hours was visible ONLY as a repeating ERROR log line, invisible on
any admin surface. This test module covers
HealthCheckService._collect_cidx_meta_conflict_quarantine_failures(),
which reads the SAME cidx_meta_conflict_quarantine_state table Bug
#1539's bookkeeping already writes/reads, and surfaces any row that is
BOTH at/above the quarantine threshold AND has persisted beyond
CIDX_META_CONFLICT_QUARANTINE_HEALTH_THRESHOLD_SECONDS as a DEGRADED
health failure_reason naming the affected golden alias -- reusing the
EXISTING /health failure_reasons surface, mirroring
_collect_golden_repo_reconcile_breaker_failures() (Bug #1382) exactly,
per this bug's own suggested direction.

Fail-open discipline: any error reading the table (including "the table
does not exist yet") must NEVER produce a false health alarm.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from code_indexer.global_repos.refresh_scheduler import (
    _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD,
)
from code_indexer.server.models.api_models import HealthStatus, SystemHealthInfo
from code_indexer.server.services.health_service import (
    HealthCheckService,
    CIDX_META_CONFLICT_QUARANTINE_HEALTH_THRESHOLD_SECONDS,
)

# Margin added past the escalation threshold so the seeded row is
# unambiguously "beyond" it, tolerant of test execution jitter.
PERSISTENCE_MARGIN_SECONDS = 60


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_service_with_temp_db(tmp_path, create_table: bool = False, rows=None):
    """Build a HealthCheckService pointed at a temp SQLite DB (under
    pytest's auto-cleaned tmp_path), optionally pre-populated with
    cidx_meta_conflict_quarantine_state rows. Each row tuple is
    (golden_alias, consecutive_failure_count, last_target_sha,
    first_failed_at_iso_string_or_none)."""
    service = HealthCheckService()
    db_path = tmp_path / "cidx_server.db"
    service.database_url = f"sqlite:///{db_path}"

    if create_table:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE cidx_meta_conflict_quarantine_state (
                    golden_alias TEXT PRIMARY KEY NOT NULL,
                    consecutive_failure_count INTEGER NOT NULL DEFAULT 0,
                    last_target_sha TEXT,
                    last_detail TEXT,
                    first_failed_at TEXT,
                    last_failed_at TEXT,
                    updated_at TEXT
                )
                """
            )
            for row in rows or []:
                conn.execute(
                    "INSERT INTO cidx_meta_conflict_quarantine_state "
                    "(golden_alias, consecutive_failure_count, last_target_sha, "
                    "first_failed_at) VALUES (?, ?, ?, ?)",
                    row,
                )
            conn.commit()
        finally:
            conn.close()

    return service


class TestCidxMetaConflictQuarantineHealthCheckFailOpen:
    def test_no_table_returns_no_failure(self, tmp_path):
        """Fresh install / cidx-meta backup has never failed -- table
        doesn't exist yet. Must be fail-open: no failure, no crash."""
        service = _make_service_with_temp_db(tmp_path, create_table=False)

        has_warning, has_error, reasons = (
            service._collect_cidx_meta_conflict_quarantine_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []


class TestCidxMetaConflictQuarantineHealthCheckReports:
    def test_persistent_quarantine_beyond_threshold_reports_degraded_naming_alias(
        self, tmp_path
    ):
        """The core Bug #1555 fix: a quarantine that has persisted beyond
        the health-escalation threshold surfaces as a DEGRADED (warning,
        not full outage) failure_reason naming the affected golden
        alias."""
        old_enough = datetime.now(timezone.utc) - timedelta(
            seconds=CIDX_META_CONFLICT_QUARANTINE_HEALTH_THRESHOLD_SECONDS
            + PERSISTENCE_MARGIN_SECONDS
        )
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                (
                    "cidx-meta-global",
                    _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD,
                    "b9aff48",
                    _iso(old_enough),
                )
            ],
        )

        has_warning, has_error, reasons = (
            service._collect_cidx_meta_conflict_quarantine_failures()
        )

        assert has_warning is True
        assert has_error is False
        assert len(reasons) == 1
        assert "cidx-meta-global" in reasons[0]

    def test_below_threshold_row_not_reported_even_when_old(self, tmp_path):
        """A row that has NOT reached Bug #1539's quarantine threshold is
        not actually quarantined -- it must never be reported here, no
        matter how old first_failed_at is."""
        very_old = datetime.now(timezone.utc) - timedelta(days=30)
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                (
                    "cidx-meta-global",
                    _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD - 1,
                    "b9aff48",
                    _iso(very_old),
                )
            ],
        )

        has_warning, has_error, reasons = (
            service._collect_cidx_meta_conflict_quarantine_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []

    def test_at_threshold_but_too_recent_not_reported(self, tmp_path):
        """A quarantine that just tripped (still within the health-
        escalation grace window) must not yet surface on /health -- only
        a PERSISTENT quarantine escalates."""
        just_now = datetime.now(timezone.utc)
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                (
                    "cidx-meta-global",
                    _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD,
                    "b9aff48",
                    _iso(just_now),
                )
            ],
        )

        has_warning, has_error, reasons = (
            service._collect_cidx_meta_conflict_quarantine_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []


class TestCidxMetaConflictQuarantineWiring:
    def test_wired_into_calculate_overall_status(self, tmp_path):
        """_calculate_overall_status() must actually call
        _collect_cidx_meta_conflict_quarantine_failures() and fold a
        persistently-quarantined alias into the returned (status,
        failure_reasons) -- wiring it, not just implementing it as dead
        code (Messi Rule #12: anti-orphan-code)."""
        old_enough = datetime.now(timezone.utc) - timedelta(
            seconds=CIDX_META_CONFLICT_QUARANTINE_HEALTH_THRESHOLD_SECONDS
            + PERSISTENCE_MARGIN_SECONDS
        )
        service = _make_service_with_temp_db(
            tmp_path,
            create_table=True,
            rows=[
                (
                    "cidx-meta-global",
                    _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD,
                    "b9aff48",
                    _iso(old_enough),
                )
            ],
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
        assert any("cidx-meta-global" in reason for reason in failure_reasons)
