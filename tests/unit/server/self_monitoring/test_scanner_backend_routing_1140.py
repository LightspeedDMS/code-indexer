"""
Regression tests for Bug #1140: LogScanner writes to SQLite while SelfMonitoringBackend
reads from PG (cluster mode) → split-brain → Scan History always empty.

The fix: LogScanner must accept a `backend: SelfMonitoringBackend` and route all
scan-record and fingerprint persistence through it, so writes land in the SAME
store the dashboard reads.

Three test classes:
1. TestScannerBackendRoutingRegression1140 — the split-brain regression
2. TestScannerBackendRoutingSQLite — real SQLite backend, end-to-end
3. TestScannerDeltaTrackingViaBackend — get_last_scan_log_id via backend
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from code_indexer.server.self_monitoring.scanner import LogScanner
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import SelfMonitoringSqliteBackend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_sqlite_db() -> str:
    """Return path to a fresh SQLite DB with the full server schema."""
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "test.db"
    schema = DatabaseSchema(db_path=str(db_path))
    schema.initialize_database()
    return str(db_path)


def _cleanup_db(db_path: str) -> None:
    p = Path(db_path)
    shutil.rmtree(p.parent, ignore_errors=True)


class _RecordingBackend:
    """
    A minimal in-memory SelfMonitoringBackend that records every call.

    Used as the backend injected into LogScanner to prove the scanner delegates
    its persistence calls to the backend (NOT to a separate SQLite file).
    """

    def __init__(self) -> None:
        self._scans: List[Dict[str, Any]] = []
        self._fingerprints: List[Tuple[str, str, str, str, str]] = []
        self.calls: List[str] = []

    # --- SelfMonitoringBackend protocol methods ---

    def create_scan_record(
        self,
        scan_id: str,
        started_at: str,
        log_id_start: int,
    ) -> None:
        self.calls.append("create_scan_record")
        self._scans.append(
            {
                "scan_id": scan_id,
                "started_at": started_at,
                "status": "RUNNING",
                "log_id_start": log_id_start,
                "log_id_end": log_id_start,
                "issues_created": 0,
                "completed_at": None,
                "error_message": None,
            }
        )

    def get_last_scan_log_id(self) -> int:
        self.calls.append("get_last_scan_log_id")
        success = [
            s
            for s in self._scans
            if s["status"] == "SUCCESS" and s.get("log_id_end") is not None
        ]
        if not success:
            return 0
        return int(max(s["log_id_end"] for s in success))

    def update_scan_record(
        self,
        scan_id: str,
        status: str,
        completed_at: str,
        log_id_end: Optional[int] = None,
        issues_created: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        self.calls.append("update_scan_record")
        for s in self._scans:
            if s["scan_id"] == scan_id:
                s["status"] = status
                s["completed_at"] = completed_at
                if log_id_end is not None:
                    s["log_id_end"] = log_id_end
                if issues_created is not None:
                    s["issues_created"] = issues_created
                if error_message is not None:
                    s["error_message"] = error_message
                break

    def cleanup_orphaned_scans(self, cutoff_iso: str) -> int:
        return 0

    def get_last_started_at(self) -> Optional[str]:
        return None

    def fetch_stored_fingerprints(
        self, retention_days: int
    ) -> List[Tuple[str, str, str, str, str]]:
        self.calls.append("fetch_stored_fingerprints")
        return self._fingerprints

    def store_issue_metadata(
        self,
        scan_id: str,
        github_issue_number: Optional[int],
        github_issue_url: Optional[str],
        classification: str,
        title: str,
        error_codes: str,
        fingerprint: str,
        source_log_ids: str,
        source_files: str,
        created_at: str,
    ) -> None:
        self.calls.append("store_issue_metadata")

    def list_scans(self, limit: int = 50) -> List[Dict[str, Any]]:
        self.calls.append("list_scans")
        return list(reversed(self._scans[-limit:]))

    def list_issues(self, limit: int = 100) -> List[Dict[str, Any]]:
        return []

    def get_running_scan_count(self) -> int:
        return 0


# ---------------------------------------------------------------------------
# Class 1: Split-brain regression (Bug #1140)
# ---------------------------------------------------------------------------


class TestScannerBackendRoutingRegression1140:
    """
    Regression test that directly reproduces Bug #1140.

    Before the fix: LogScanner ignores the backend and writes to its own
    DatabaseConnectionManager(db_path).  The _RecordingBackend never sees
    the scan record → list_scans() returns empty → dashboard shows nothing.

    After the fix: LogScanner routes create_scan_record / update_scan_record
    through the injected backend → _RecordingBackend.list_scans() returns
    the record → dashboard works.
    """

    def test_scan_record_visible_via_injected_backend(self) -> None:
        """
        REGRESSION: A scan record written by LogScanner must be readable through
        the injected backend's list_scans(), not a separate SQLite file.

        This is the exact split-brain that causes Scan History to be empty in
        cluster (PG) mode: the scanner writes to node-local SQLite while the
        dashboard reads from PG.
        """
        backend = _RecordingBackend()

        # LogScanner with the backend injected
        scanner = LogScanner(
            db_path=":memory:",  # vestigial — must NOT be used for scan records
            scan_id="scan-regression-1140",
            github_repo="org/repo",
            log_db_path="/dev/null",
            prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
            backend=backend,
        )

        # Simulate the scan-record lifecycle that execute_scan() calls
        scanner.create_scan_record(log_id_start=0)
        scanner.update_scan_record(
            status="SUCCESS",
            log_id_end=100,
            issues_created=0,
        )

        # The dashboard reads via backend.list_scans() — must NOT be empty
        scans = backend.list_scans()
        assert len(scans) == 1, (
            "Bug #1140: scan record written by LogScanner not visible via backend.list_scans(). "
            f"Got {scans!r}"
        )
        scan = scans[0]
        assert scan["scan_id"] == "scan-regression-1140"
        assert scan["status"] == "SUCCESS"
        assert scan["log_id_end"] == 100

    def test_create_scan_record_delegates_to_backend(self) -> None:
        """create_scan_record must call backend.create_scan_record, not raw sqlite."""
        backend = _RecordingBackend()
        scanner = LogScanner(
            db_path=":memory:",
            scan_id="scan-abc",
            github_repo="org/repo",
            log_db_path="/dev/null",
            prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
            backend=backend,
        )

        scanner.create_scan_record(log_id_start=42)

        assert "create_scan_record" in backend.calls
        assert any(s["scan_id"] == "scan-abc" for s in backend._scans)

    def test_update_scan_record_delegates_to_backend(self) -> None:
        """update_scan_record must call backend.update_scan_record."""
        backend = _RecordingBackend()
        scanner = LogScanner(
            db_path=":memory:",
            scan_id="scan-upd",
            github_repo="org/repo",
            log_db_path="/dev/null",
            prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
            backend=backend,
        )

        scanner.create_scan_record(log_id_start=0)
        scanner.update_scan_record(status="FAILURE", error_message="oops")

        assert "update_scan_record" in backend.calls
        scan = next(s for s in backend._scans if s["scan_id"] == "scan-upd")
        assert scan["status"] == "FAILURE"
        assert scan["error_message"] == "oops"

    def test_get_last_scan_log_id_delegates_to_backend(self) -> None:
        """get_last_scan_log_id must query backend, not raw sqlite."""
        backend = _RecordingBackend()
        # Pre-seed backend with a SUCCESS scan
        backend._scans.append(
            {
                "scan_id": "old-scan",
                "started_at": "2026-01-01T00:00:00",
                "status": "SUCCESS",
                "log_id_start": 0,
                "log_id_end": 77,
                "issues_created": 0,
                "completed_at": "2026-01-01T00:01:00",
                "error_message": None,
            }
        )

        scanner = LogScanner(
            db_path=":memory:",
            scan_id="scan-new",
            github_repo="org/repo",
            log_db_path="/dev/null",
            prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
            backend=backend,
        )

        result = scanner.get_last_scan_log_id()

        assert "get_last_scan_log_id" in backend.calls
        assert result == 77

    def test_fetch_stored_fingerprints_delegates_to_backend(self) -> None:
        """_fetch_stored_fingerprints must read from backend, not raw sqlite."""
        backend = _RecordingBackend()
        backend._fingerprints = [
            ("fp123", "server_bug", "ERR-001", "Title", "2026-01-01T00:00:00"),
        ]

        scanner = LogScanner(
            db_path=":memory:",
            scan_id="scan-fp",
            github_repo="org/repo",
            log_db_path="/dev/null",
            prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
            backend=backend,
        )

        # _fetch_stored_fingerprints is called by assemble_dedup_context
        context = scanner.assemble_dedup_context(existing_issues=[])

        assert "fetch_stored_fingerprints" in backend.calls
        assert "fp123" in context


# ---------------------------------------------------------------------------
# Class 2: Real SQLite backend — end-to-end
# ---------------------------------------------------------------------------


class TestScannerBackendRoutingSQLite:
    """
    End-to-end tests using a real SelfMonitoringSqliteBackend.

    Proves that after the fix, scan records written by LogScanner appear
    in the real SQLite backend's list_scans() — no mocks of the DB layer.
    """

    def setup_method(self) -> None:
        self._db_path = _make_temp_sqlite_db()
        self._backend = SelfMonitoringSqliteBackend(self._db_path)

    def teardown_method(self) -> None:
        _cleanup_db(self._db_path)

    def _make_scanner(self, scan_id: str) -> LogScanner:
        return LogScanner(
            db_path=self._db_path,
            scan_id=scan_id,
            github_repo="org/repo",
            log_db_path="/dev/null",
            prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
            backend=self._backend,
        )

    def test_created_scan_appears_in_backend_list_scans(self) -> None:
        """Scan created by scanner appears in backend.list_scans()."""
        scanner = self._make_scanner("scan-sqlite-001")

        scanner.create_scan_record(log_id_start=0)
        scanner.update_scan_record(status="SUCCESS", log_id_end=50, issues_created=0)

        scans = self._backend.list_scans()
        assert any(s["scan_id"] == "scan-sqlite-001" for s in scans), (
            f"Scan not found in backend. Got: {scans}"
        )
        found = next(s for s in scans if s["scan_id"] == "scan-sqlite-001")
        assert found["status"] == "SUCCESS"
        assert found["log_id_end"] == 50

    def test_running_scan_then_completed_visible_in_list_scans(self) -> None:
        """RUNNING → SUCCESS lifecycle visible through backend."""
        scanner = self._make_scanner("scan-sqlite-002")

        scanner.create_scan_record(log_id_start=10)

        # Verify RUNNING state visible
        running = self._backend.list_scans()
        assert any(
            s["scan_id"] == "scan-sqlite-002" and s["status"] == "RUNNING"
            for s in running
        )

        # Complete with SUCCESS
        scanner.update_scan_record(status="SUCCESS", log_id_end=200, issues_created=3)

        completed = self._backend.list_scans()
        found = next(s for s in completed if s["scan_id"] == "scan-sqlite-002")
        assert found["status"] == "SUCCESS"
        assert found["issues_created"] == 3
        assert found["log_id_end"] == 200

    def test_failure_scan_visible_in_list_scans(self) -> None:
        """FAILURE scan visible through backend.list_scans()."""
        scanner = self._make_scanner("scan-sqlite-003")

        scanner.create_scan_record(log_id_start=0)
        scanner.update_scan_record(status="FAILURE", error_message="Claude timeout")

        scans = self._backend.list_scans()
        found = next((s for s in scans if s["scan_id"] == "scan-sqlite-003"), None)
        assert found is not None
        assert found["status"] == "FAILURE"
        assert found["error_message"] == "Claude timeout"

    def test_get_last_scan_log_id_reads_from_backend_store(self) -> None:
        """get_last_scan_log_id reads from the same store as backend.list_scans()."""
        scanner_a = self._make_scanner("scan-a")
        scanner_a.create_scan_record(log_id_start=0)
        scanner_a.update_scan_record(status="SUCCESS", log_id_end=99, issues_created=0)

        # A new scanner should pick up 99 from the backend
        scanner_b = self._make_scanner("scan-b")
        result = scanner_b.get_last_scan_log_id()
        assert result == 99

    def test_multiple_scans_ordered_most_recent_first(self) -> None:
        """list_scans returns most recent first after multiple scans."""
        for i, log_end in enumerate([10, 20, 30]):
            s = self._make_scanner(f"scan-order-{i}")
            s.create_scan_record(log_id_start=0)
            s.update_scan_record(status="SUCCESS", log_id_end=log_end, issues_created=0)

        scans = self._backend.list_scans()
        # Most recent first: scan-order-2 (log_end=30) should be first
        found_ids = [s["scan_id"] for s in scans]
        assert found_ids.index("scan-order-2") < found_ids.index("scan-order-0")


# ---------------------------------------------------------------------------
# Class 3: Delta tracking via backend
# ---------------------------------------------------------------------------


class TestScannerDeltaTrackingViaBackend:
    """
    Tests that delta tracking (get_last_scan_log_id) reads from the backend,
    ensuring the scanner picks up the correct watermark in both modes.
    """

    def test_no_previous_scans_returns_zero(self) -> None:
        """Empty backend yields 0 as initial watermark."""
        backend = _RecordingBackend()
        scanner = LogScanner(
            db_path=":memory:",
            scan_id="scan-delta-1",
            github_repo="org/repo",
            log_db_path="/dev/null",
            prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
            backend=backend,
        )

        assert scanner.get_last_scan_log_id() == 0

    def test_only_success_scan_counts_for_delta(self) -> None:
        """FAILURE scans must NOT advance the watermark."""
        backend = _RecordingBackend()

        # Insert one SUCCESS, one FAILURE (more recent)
        backend._scans.extend(
            [
                {
                    "scan_id": "scan-s",
                    "started_at": "2026-01-01T10:00:00",
                    "status": "SUCCESS",
                    "log_id_start": 0,
                    "log_id_end": 55,
                    "issues_created": 0,
                    "completed_at": "2026-01-01T10:01:00",
                    "error_message": None,
                },
                {
                    "scan_id": "scan-f",
                    "started_at": "2026-01-01T11:00:00",
                    "status": "FAILURE",
                    "log_id_start": 55,
                    "log_id_end": None,
                    "issues_created": None,
                    "completed_at": "2026-01-01T11:01:00",
                    "error_message": "timeout",
                },
            ]
        )

        scanner = LogScanner(
            db_path=":memory:",
            scan_id="scan-new",
            github_repo="org/repo",
            log_db_path="/dev/null",
            prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
            backend=backend,
        )

        # Must return 55 from SUCCESS, not None from FAILURE
        assert scanner.get_last_scan_log_id() == 55

    def test_sqlite_backend_delta_tracking_end_to_end(self) -> None:
        """Real SQLite: watermark advances after SUCCESS, stays after FAILURE."""
        db_path = _make_temp_sqlite_db()
        try:
            backend = SelfMonitoringSqliteBackend(db_path)

            scanner1 = LogScanner(
                db_path=db_path,
                scan_id="scan-dt-1",
                github_repo="org/repo",
                log_db_path="/dev/null",
                prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
                backend=backend,
            )
            scanner1.create_scan_record(log_id_start=0)
            scanner1.update_scan_record(
                status="SUCCESS", log_id_end=150, issues_created=0
            )

            scanner2 = LogScanner(
                db_path=db_path,
                scan_id="scan-dt-2",
                github_repo="org/repo",
                log_db_path="/dev/null",
                prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
                backend=backend,
            )
            assert scanner2.get_last_scan_log_id() == 150

            # A FAILURE scan should not move the watermark
            scanner2.create_scan_record(log_id_start=150)
            scanner2.update_scan_record(status="FAILURE", error_message="err")

            scanner3 = LogScanner(
                db_path=db_path,
                scan_id="scan-dt-3",
                github_repo="org/repo",
                log_db_path="/dev/null",
                prompt_template="p {log_db_path} {last_scan_log_id} {dedup_context}",
                backend=backend,
            )
            assert scanner3.get_last_scan_log_id() == 150  # unchanged
        finally:
            _cleanup_db(db_path)


# ---------------------------------------------------------------------------
# Class 4: IssueManager backend routing (B1)
# ---------------------------------------------------------------------------


class TestIssueManagerBackendRouting:
    """
    Regression tests for B1: IssueManager._store_metadata must route through
    the injected backend when one is present.

    Before the fix: _store_metadata always writes via DatabaseConnectionManager
    (SQLite) regardless of whether a backend was injected.  In cluster (PG)
    mode the dashboard reads issues via backend.list_issues() which reads PG —
    so created issues are invisible and fingerprint dedup sees an empty set.

    After the fix: when `backend` is provided, _store_metadata calls
    backend.store_issue_metadata(...) so write and read converge on one store.
    """

    def setup_method(self) -> None:
        self._db_path = _make_temp_sqlite_db()
        self._backend = SelfMonitoringSqliteBackend(self._db_path)

    def teardown_method(self) -> None:
        _cleanup_db(self._db_path)

    def _seed_scan(self, scan_id: str) -> None:
        """Insert a minimal scan record to satisfy the FK constraint on self_monitoring_issues."""
        self._backend.create_scan_record(
            scan_id=scan_id,
            started_at="2026-01-01T00:00:00",
            log_id_start=0,
        )

    def test_store_metadata_visible_via_backend_list_issues(self) -> None:
        """
        B1 REGRESSION: issue written by IssueManager._store_metadata must be
        visible via backend.list_issues().

        This would have caught the split-brain: before the fix, _store_metadata
        writes to node-local SQLite; backend.list_issues() reads the same store
        in SQLite mode — but in PG mode they diverge. By using a real
        SelfMonitoringSqliteBackend we prove the WRITE/READ convergence.
        """
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        scan_id = "scan-b1-001"
        self._seed_scan(scan_id)

        mgr = IssueManager(
            db_path=self._db_path,
            scan_id=scan_id,
            github_repo="org/repo",
            backend=self._backend,
        )

        # Directly call _store_metadata (bypasses GitHub API — unit test boundary)
        mgr._store_metadata(
            github_issue_number=42,
            github_issue_url="https://github.com/org/repo/issues/42",
            classification="server_bug",
            title="[BUG] Something broke",
            error_codes=["ERR-001"],
            fingerprint="deadbeef" * 8,
            source_log_ids=[1, 2, 3],
            source_files=["src/foo.py"],
        )

        issues = self._backend.list_issues()
        assert len(issues) == 1, (
            f"B1: issue written by IssueManager._store_metadata not visible via "
            f"backend.list_issues(). Got: {issues!r}"
        )
        issue = issues[0]
        assert issue["scan_id"] == scan_id
        assert issue["github_issue_number"] == 42
        assert issue["classification"] == "server_bug"
        assert issue["title"] == "[BUG] Something broke"

    def test_store_metadata_fingerprint_visible_via_fetch_stored_fingerprints(
        self,
    ) -> None:
        """
        B1 REGRESSION: fingerprint written by IssueManager._store_metadata must
        be readable via backend.fetch_stored_fingerprints().

        The dedup logic in LogScanner._fetch_stored_fingerprints reads fingerprints
        via the backend. If _store_metadata writes to a DIFFERENT store, dedup
        sees an empty set and duplicates GitHub issues every scan.
        """
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        scan_id = "scan-b1-002"
        self._seed_scan(scan_id)

        fingerprint = "cafebabe" * 8
        mgr = IssueManager(
            db_path=self._db_path,
            scan_id=scan_id,
            github_repo="org/repo",
            backend=self._backend,
        )

        mgr._store_metadata(
            github_issue_number=99,
            github_issue_url="https://github.com/org/repo/issues/99",
            classification="client_misuse",
            title="[CLIENT] Bad request pattern",
            error_codes=["CLI-002"],
            fingerprint=fingerprint,
            source_log_ids=[10],
            source_files=["src/bar.py"],
        )

        fps = self._backend.fetch_stored_fingerprints(retention_days=90)
        fp_values = [row[0] for row in fps]
        assert fingerprint in fp_values, (
            f"B1: fingerprint written by IssueManager._store_metadata not visible "
            f"via backend.fetch_stored_fingerprints(). Got: {fp_values!r}"
        )

    def test_store_metadata_without_backend_still_writes_sqlite(self) -> None:
        """
        Solo mode (no backend): _store_metadata falls back to raw SQLite.
        The issue must still be readable from the DB file directly.
        """
        import sqlite3
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Create the scan record first via the backend (same DB) to satisfy FK
        scan_id = "scan-b1-solo"
        self._seed_scan(scan_id)

        mgr = IssueManager(
            db_path=self._db_path,
            scan_id=scan_id,
            github_repo="org/repo",
            # No backend — solo mode: falls back to raw SQLite
        )

        mgr._store_metadata(
            github_issue_number=7,
            github_issue_url="https://github.com/org/repo/issues/7",
            classification="server_bug",
            title="[BUG] Solo mode issue",
            error_codes=[],
            fingerprint="a" * 64,
            source_log_ids=[],
            source_files=[],
        )

        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute(
            "SELECT scan_id, github_issue_number FROM self_monitoring_issues"
        )
        rows = cursor.fetchall()
        conn.close()
        assert any(r[1] == 7 for r in rows), (
            f"Solo fallback: issue not found in SQLite. Rows: {rows!r}"
        )


# ---------------------------------------------------------------------------
# Class 5: trigger_manual_scan backend wiring (B2)
# ---------------------------------------------------------------------------


class TestManualScanTriggerBackendWiring:
    """
    Source-level guard for B2: trigger_manual_scan must pass storage_backend=
    into SelfMonitoringService(...) so manual scans in cluster mode write to PG.

    Before the fix: SelfMonitoringService is constructed without storage_backend=,
    so it defaults to None and all writes go to node-local SQLite.

    This is a source-text guard (like test_lifespan_tracking_backend_wiring_1100.py)
    because trigger_manual_scan is an async route handler that requires a full
    ASGI test stack to exercise properly.  The source guard is sufficient to
    catch the regression (the exact mistake was omitting the kwarg).
    """

    def _get_routes_source(self) -> str:
        import inspect
        import importlib

        mod = importlib.import_module("code_indexer.server.web.routes")
        return inspect.getsource(mod)

    def test_trigger_manual_scan_passes_storage_backend(self) -> None:
        """
        B2 REGRESSION: trigger_manual_scan must pass storage_backend= to
        SelfMonitoringService constructor.

        Before the fix the kwarg was absent → backend=None → SQLite in cluster.
        """
        source = self._get_routes_source()

        # Find the trigger_manual_scan function body.  We look for the
        # storage_backend= kwarg AFTER the function definition marker so we
        # don't accidentally match some other call site.
        func_start = source.find("async def trigger_manual_scan(")
        assert func_start != -1, "trigger_manual_scan function not found in routes.py"

        # The next SelfMonitoringService(...) call after the function definition
        # must include storage_backend=
        service_call_start = source.find("SelfMonitoringService(", func_start)
        assert service_call_start != -1, (
            "SelfMonitoringService( not found inside trigger_manual_scan"
        )

        # Find the closing paren of this call
        paren_depth = 0
        i = service_call_start + len("SelfMonitoringService(")
        paren_depth = 1
        while i < len(source) and paren_depth > 0:
            if source[i] == "(":
                paren_depth += 1
            elif source[i] == ")":
                paren_depth -= 1
            i += 1
        call_body = source[service_call_start:i]

        assert "storage_backend=" in call_body, (
            "B2: SelfMonitoringService() in trigger_manual_scan is missing "
            f"storage_backend= kwarg.\n\nCall body found:\n{call_body}"
        )

    def test_trigger_manual_scan_derives_backend_from_registry(self) -> None:
        """
        B2 REGRESSION: trigger_manual_scan must derive the backend from
        request.app.state.backend_registry (same pattern as the scheduled path
        in lifespan.py).

        Checks that the function contains the backend_registry access idiom
        before constructing SelfMonitoringService.
        """
        source = self._get_routes_source()

        func_start = source.find("async def trigger_manual_scan(")
        assert func_start != -1

        func_body_end = source.find("\nasync def ", func_start + 1)
        if func_body_end == -1:
            func_body_end = len(source)
        func_body = source[func_start:func_body_end]

        assert "backend_registry" in func_body, (
            "B2: trigger_manual_scan does not access backend_registry — "
            "storage_backend cannot be derived from the registry."
        )
