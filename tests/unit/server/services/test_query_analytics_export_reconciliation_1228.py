"""Tests for orphaned export reconciliation (Bug #1228).

Verifies that stuck pending/running exports are reconciled to failed on startup,
while completed/failed exports are not touched and recently-started exports are
preserved (cluster-safety predicate).

Cluster-safety design:
  Predicate: status IN ('pending', 'running') AND created_at < (time.time() - threshold)
  Default threshold: 300 seconds (5 minutes).
  Rationale: no real export takes more than a few minutes; a legitimately-running
  export on another cluster node started recently will NOT be within the orphan
  window.  The 3 staging orphans (created hours ago during the NFS outage) are
  definitively caught by any threshold > a few minutes.
"""

import time
from pathlib import Path

import pytest

from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportService,
    QueryAnalyticsExportSqliteBackend,
)

# Use the same default the implementation uses, but keep tests independent of
# the exact constant value by expressing ages relative to this threshold.
_THRESHOLD = 300.0  # seconds (5 minutes)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def backend(tmp_db: str) -> QueryAnalyticsExportSqliteBackend:
    return QueryAnalyticsExportSqliteBackend(tmp_db)


@pytest.fixture
def service(
    tmp_path: Path, backend: QueryAnalyticsExportSqliteBackend
) -> QueryAnalyticsExportService:
    return QueryAnalyticsExportService(backend=backend, golden_repos_dir=str(tmp_path))


def _rec(export_id: str, status: str, created_at: float, **extra) -> dict:
    """Build a minimal export record dict."""
    return {
        "id": export_id,
        "initiated_by": "tester",
        "created_at": created_at,
        "status": status,
        "filter_summary": "All searches",
        **extra,
    }


def _old(offset: float = 60.0) -> float:
    """Return a timestamp older than the orphan threshold."""
    return time.time() - _THRESHOLD - offset


def _recent(offset: float = 60.0) -> float:
    """Return a timestamp well within the threshold (i.e. recent)."""
    return time.time() - offset  # 60s old, threshold is 300s


# ---------------------------------------------------------------------------
# Backend-level tests
# ---------------------------------------------------------------------------


class TestReconcileOrphanedExportsSqliteBackend:
    def test_orphaned_pending_transitioned_to_failed(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """A pending export older than threshold must be failed."""
        backend.create_export(_rec("p1", "pending", _old()))

        count = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count == 1
        row = backend.list_exports(export_id="p1")[0]
        assert row["status"] == "failed"
        assert row["error_message"] == "interrupted by worker restart"

    def test_orphaned_running_transitioned_to_failed(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """A running export older than threshold must be failed."""
        backend.create_export(_rec("r1", "running", _old()))

        count = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count == 1
        row = backend.list_exports(export_id="r1")[0]
        assert row["status"] == "failed"
        assert row["error_message"] == "interrupted by worker restart"

    def test_completed_export_not_touched(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """Terminal state 'completed' must never be overwritten."""
        backend.create_export(_rec("c1", "completed", _old()))

        count = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count == 0
        row = backend.list_exports(export_id="c1")[0]
        assert row["status"] == "completed"

    def test_failed_export_not_touched(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """Terminal state 'failed' must not be overwritten (original message preserved)."""
        backend.create_export(
            _rec("f1", "failed", _old(), error_message="original failure")
        )

        count = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count == 0
        row = backend.list_exports(export_id="f1")[0]
        assert row["error_message"] == "original failure"

    def test_recent_pending_not_touched_cluster_safety(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """A pending export WITHIN the threshold window must NOT be failed.

        This simulates an export legitimately running on another cluster node.
        The staleness predicate (created_at < now - threshold) must protect it.
        """
        backend.create_export(_rec("recent", "pending", _recent()))

        count = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count == 0
        row = backend.list_exports(export_id="recent")[0]
        assert row["status"] == "pending"

    def test_recent_running_not_touched_cluster_safety(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """A running export WITHIN the threshold window must NOT be failed."""
        backend.create_export(_rec("recent-r", "running", _recent()))

        count = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count == 0
        row = backend.list_exports(export_id="recent-r")[0]
        assert row["status"] == "running"

    def test_idempotent_second_run_is_noop(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """Running reconciliation twice must be safe: second run returns 0."""
        backend.create_export(_rec("orphan", "pending", _old()))

        count1 = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )
        count2 = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count1 == 1
        assert count2 == 0

    def test_empty_table_returns_zero(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """Reconciliation on an empty table must return 0 without error."""
        count = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )
        assert count == 0

    def test_mixed_statuses_only_orphaned_affected(
        self, backend: QueryAnalyticsExportSqliteBackend
    ) -> None:
        """With mixed rows, only old pending/running rows are transitioned."""
        backend.create_export(_rec("old-pending", "pending", _old()))
        backend.create_export(_rec("old-running", "running", _old()))
        backend.create_export(_rec("recent-pending", "pending", _recent()))
        backend.create_export(_rec("old-completed", "completed", _old()))
        backend.create_export(_rec("old-failed", "failed", _old()))

        count = backend.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count == 2  # only old-pending + old-running
        assert backend.list_exports(export_id="old-pending")[0]["status"] == "failed"
        assert backend.list_exports(export_id="old-running")[0]["status"] == "failed"
        assert (
            backend.list_exports(export_id="recent-pending")[0]["status"] == "pending"
        )
        assert (
            backend.list_exports(export_id="old-completed")[0]["status"] == "completed"
        )
        assert backend.list_exports(export_id="old-failed")[0]["status"] == "failed"


# ---------------------------------------------------------------------------
# Service-level delegation test
# ---------------------------------------------------------------------------


class TestReconcileViaService:
    def test_service_has_reconcile_orphaned_exports_method(
        self, service: QueryAnalyticsExportService
    ) -> None:
        """QueryAnalyticsExportService must expose reconcile_orphaned_exports."""
        assert hasattr(service, "reconcile_orphaned_exports"), (
            "QueryAnalyticsExportService must expose reconcile_orphaned_exports "
            "so lifespan.py can call it via app.state.query_analytics_export_service"
        )

    def test_service_delegates_to_backend(
        self, service: QueryAnalyticsExportService
    ) -> None:
        """Service.reconcile_orphaned_exports must delegate to backend and return count."""
        service._backend.create_export(_rec("orphan", "pending", _old()))

        count = service.reconcile_orphaned_exports(
            threshold_seconds=_THRESHOLD,
            error="interrupted by worker restart",
        )

        assert count == 1
        exports = service.list_exports(export_id="orphan")
        assert exports[0]["status"] == "failed"

    def test_service_reconcile_default_args_work(
        self, service: QueryAnalyticsExportService
    ) -> None:
        """reconcile_orphaned_exports must work with no arguments (uses defaults)."""
        service._backend.create_export(_rec("orphan2", "pending", _old()))

        # Must not raise; must return an int
        count = service.reconcile_orphaned_exports()
        assert isinstance(count, int)
        assert count >= 1


# ---------------------------------------------------------------------------
# Lifespan source-text guard
# ---------------------------------------------------------------------------

_LIFESPAN_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "startup"
    / "lifespan.py"
)


class TestLifespanReconciliationWiring:
    def test_lifespan_calls_reconcile_orphaned_exports(self) -> None:
        """lifespan.py must call reconcile_orphaned_exports on startup (Bug #1228)."""
        source = _LIFESPAN_PATH.read_text()
        assert "reconcile_orphaned_exports" in source, (
            "lifespan.py must call reconcile_orphaned_exports on startup "
            "to clear stuck pending/running exports (Bug #1228). "
            "Wire it next to the fail_orphaned_jobs block."
        )

    def test_lifespan_reconcile_is_near_fail_orphaned_jobs(self) -> None:
        """reconcile_orphaned_exports must appear in the same startup region as fail_orphaned_jobs."""
        source = _LIFESPAN_PATH.read_text()
        fail_pos = source.find("fail_orphaned_jobs")
        reconcile_pos = source.find("reconcile_orphaned_exports")
        assert fail_pos != -1, "fail_orphaned_jobs not found in lifespan.py"
        assert reconcile_pos != -1, (
            "reconcile_orphaned_exports not found in lifespan.py"
        )
        # Both calls should be within 80 lines of each other (same startup region)
        fail_line = source[:fail_pos].count("\n")
        reconcile_line = source[:reconcile_pos].count("\n")
        assert abs(fail_line - reconcile_line) <= 80, (
            f"reconcile_orphaned_exports (line {reconcile_line}) is too far from "
            f"fail_orphaned_jobs (line {fail_line}) -- wire them in the same "
            f"startup region (within ~80 lines)."
        )
