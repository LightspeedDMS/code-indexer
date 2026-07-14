"""
Unit tests for the golden-repo registry-reconcile circuit-breaker
escalation surface on the /health endpoint (Bug #1382, part 2).

Bug #1382's live staging incident showed the reconcile circuit-breaker
aborting silently for ~2 months: only a log-only WARNING was emitted on
every restart, with no admin-visible signal. This test module covers the
new HealthCheckService._collect_golden_repo_reconcile_breaker_failures()
method, which reads the breaker's persisted state (golden_repo_reconcile_
breaker_state, written by golden_repo_reconciler.py) and surfaces a
currently-tripped breaker as a DEGRADED health failure_reason -- reusing
the project's EXISTING /health failure_reasons surface (health_service.py)
rather than inventing a new alerting mechanism, per this project's
established pattern for other "something has been wrong for a while"
conditions (_collect_database_failures, _collect_volume_failures, etc.).

Fail-open discipline: any error reading the breaker state (including "the
table does not exist yet" on a fresh install) must NEVER produce a false
health alarm -- this is a best-effort visibility aid.
"""

import os
import sqlite3
import tempfile
from typing import Optional, Tuple
from unittest.mock import patch

from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.models.api_models import HealthStatus, SystemHealthInfo


def _make_service_with_temp_db(create_table: bool = False, row: Optional[Tuple] = None):
    """
    Build a HealthCheckService pointed at a temp SQLite DB, optionally
    pre-populated with a golden_repo_reconcile_breaker_state row.
    """
    service = HealthCheckService()
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "cidx_server.db")
    service.database_url = f"sqlite:///{db_path}"

    if create_table:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE golden_repo_reconcile_breaker_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                orphan_fingerprint TEXT,
                consecutive_count INTEGER NOT NULL DEFAULT 0,
                first_observed_at TEXT,
                last_observed_at TEXT,
                updated_at TEXT
            )
            """
        )
        if row is not None:
            conn.execute(
                "INSERT INTO golden_repo_reconcile_breaker_state "
                "(id, orphan_fingerprint, consecutive_count) VALUES (1, ?, ?)",
                row,
            )
        conn.commit()
        conn.close()

    return service


class TestGoldenRepoReconcileBreakerHealthCheckFailOpen:
    def test_no_table_returns_no_failure(self):
        """Fresh install / breaker never tripped -- table doesn't exist yet.
        Must be fail-open: no failure reported, no crash."""
        service = _make_service_with_temp_db(create_table=False)

        has_warning, has_error, reasons = (
            service._collect_golden_repo_reconcile_breaker_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []

    def test_table_exists_but_no_row_returns_no_failure(self):
        """Table exists (reconciler has run) but breaker was never tripped
        (or was reset after a resolved incident) -- no row, no failure."""
        service = _make_service_with_temp_db(create_table=True, row=None)

        has_warning, has_error, reasons = (
            service._collect_golden_repo_reconcile_breaker_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []


class TestGoldenRepoReconcileBreakerHealthCheckTripped:
    def test_tripped_breaker_reports_degraded_warning(self):
        """A currently-tripped breaker (consecutive_count > 0) must surface
        as a DEGRADED (warning, not full outage) failure_reason mentioning
        the count -- this is the core Bug #1382 fix: visible immediately,
        not buried in log-only WARNINGs across months of restarts."""
        service = _make_service_with_temp_db(create_table=True, row=("a,b,c", 2))

        has_warning, has_error, reasons = (
            service._collect_golden_repo_reconcile_breaker_failures()
        )

        assert has_warning is True
        assert has_error is False
        assert len(reasons) == 1
        assert "2" in reasons[0]
        assert "Bug #1382" in reasons[0]

    def test_zero_count_row_reports_no_failure(self):
        """Defensive: a row with consecutive_count == 0 (should not
        normally persist, but defensively handled) must not report a
        failure."""
        service = _make_service_with_temp_db(create_table=True, row=("x", 0))

        has_warning, has_error, reasons = (
            service._collect_golden_repo_reconcile_breaker_failures()
        )

        assert has_warning is False
        assert has_error is False
        assert reasons == []


class TestGoldenRepoReconcileBreakerWiredIntoOverallStatus:
    def test_tripped_breaker_degrades_overall_health_status(self):
        """_calculate_overall_status() must actually call
        _collect_golden_repo_reconcile_breaker_failures() and fold a
        tripped breaker into the returned (status, failure_reasons) --
        wiring it, not just implementing it as dead code (Messi Rule #12:
        anti-orphan-code)."""
        service = HealthCheckService()
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

        with patch.object(
            service,
            "_collect_golden_repo_reconcile_breaker_failures",
            return_value=(True, False, ["Golden-repo reconcile: fake reason"]),
        ):
            status, failure_reasons = service._calculate_overall_status(
                {}, system_info, []
            )

        assert status == HealthStatus.DEGRADED
        assert "Golden-repo reconcile: fake reason" in failure_reasons
