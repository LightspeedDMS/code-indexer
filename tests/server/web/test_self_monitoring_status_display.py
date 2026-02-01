"""
Tests for Self-Monitoring Status Display (Bug #129).

Tests the /admin/self-monitoring page's status section to ensure:
1. Last Scan displays actual timestamp from database
2. Next Scan calculates correctly based on last scan + cadence
3. Scan Status reflects actual background job state
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


class TestLastScanDisplay:
    """Tests for Last Scan field display (Bug #129 Problem 1)."""

    def test_last_scan_shows_actual_timestamp_when_scans_exist(
        self,
        authenticated_client: TestClient,
        web_infrastructure,
    ):
        """
        Test Last Scan displays actual timestamp from most recent scan.

        Bug #129 Problem 1: Currently always shows "Never" even when scans exist.
        Expected: Display started_at from most recent scan.
        """
        import os

        # Get database path
        server_dir = Path(os.environ["CIDX_SERVER_DATA_DIR"])
        db_path = server_dir / "data" / "cidx_server.db"

        # Insert test scan data (most recent scan)
        scan_time = "2026-01-30T15:30:00"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO self_monitoring_scans
                (scan_id, started_at, completed_at, status, log_id_start, log_id_end, issues_created)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-scan-latest",
                    scan_time,
                    "2026-01-30T15:35:00",
                    "SUCCESS",
                    1,
                    50,
                    0,
                ),
            )
            # Insert older scan to verify we get the most recent
            conn.execute(
                """
                INSERT INTO self_monitoring_scans
                (scan_id, started_at, completed_at, status, log_id_start, log_id_end, issues_created)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-scan-older",
                    "2026-01-30T14:00:00",
                    "2026-01-30T14:05:00",
                    "SUCCESS",
                    1,
                    30,
                    0,
                ),
            )
            conn.commit()

        # Request the page
        response = authenticated_client.get("/admin/self-monitoring")

        assert response.status_code == 200
        html = response.text

        # BUG: Currently shows "Never" - should show actual timestamp
        # Looking for the scan_time somewhere in the Last Scan section
        assert scan_time in html or "2026-01-30" in html
        # Should NOT show "Never" when scans exist
        # Note: This will fail until bug is fixed
        assert '<span id="last-scan">Never</span>' not in html


class TestNextScanCalculation:
    """Tests for Next Scan calculation (Bug #129 Problem 2)."""

    def test_next_scan_shows_na_when_no_scans_exist(
        self,
        authenticated_client: TestClient,
    ):
        """Test Next Scan shows 'N/A' when no scans exist."""
        response = authenticated_client.get("/admin/self-monitoring")

        assert response.status_code == 200
        html = response.text

        # Should show "N/A" when no scans exist
        assert 'id="next-scan"' in html
        assert "N/A" in html

    def test_next_scan_calculates_correctly_based_on_cadence(
        self,
        authenticated_client: TestClient,
        web_infrastructure,
    ):
        """
        Test Next Scan calculates last_scan + cadence_minutes.

        Bug #129 Problem 2: Currently always shows "N/A".
        Expected: Display (last_scan_started_at + cadence_minutes) as future timestamp.
        """
        import os

        # Get database path
        server_dir = Path(os.environ["CIDX_SERVER_DATA_DIR"])
        db_path = server_dir / "data" / "cidx_server.db"

        # Use default cadence for testing (60 minutes is standard default)
        cadence_minutes = 60

        # Insert test scan
        last_scan_time = datetime(2026, 1, 30, 15, 30, 0)
        last_scan_str = last_scan_time.isoformat()

        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO self_monitoring_scans
                (scan_id, started_at, completed_at, status, log_id_start, log_id_end, issues_created)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-scan-for-next",
                    last_scan_str,
                    (last_scan_time + timedelta(minutes=5)).isoformat(),
                    "SUCCESS",
                    1,
                    50,
                    0,
                ),
            )
            conn.commit()

        # Calculate expected next scan time
        next_scan_time = last_scan_time + timedelta(minutes=cadence_minutes)

        # Request the page
        response = authenticated_client.get("/admin/self-monitoring")

        assert response.status_code == 200
        html = response.text

        # BUG: Currently shows "N/A" - should show calculated next scan time
        # The exact format may vary, but should contain date/time components
        # Note: This will fail until bug is fixed
        assert '<span id="next-scan">N/A</span>' not in html
        # Should show some representation of the next scan time
        assert "2026-01-30" in html or str(next_scan_time.hour) in html


class TestScanStatusLifecycle:
    """Tests for Scan Status lifecycle (Bug #129 Problem 3)."""

    def test_scan_status_shows_idle_when_no_jobs_running(
        self,
        authenticated_client: TestClient,
    ):
        """Test Scan Status shows 'Idle' when no background jobs are running."""
        response = authenticated_client.get("/admin/self-monitoring")

        assert response.status_code == 200
        html = response.text

        # Should show "Idle" when no jobs running
        assert 'id="scan-status"' in html
        assert "Idle" in html

    def test_scan_status_shows_running_when_job_executing(
        self,
        authenticated_client: TestClient,
        web_infrastructure,
    ):
        """
        Test Scan Status shows 'Running...' when scan is in progress.

        Bug #129 Problem 3: Status lifecycle incorrect.
        Expected: Show "Running..." while scan has started but not completed (completed_at IS NULL).
        Actual: Shows "Running..." briefly, then "Completed" while still running.
        """
        import os

        # Get database path
        server_dir = Path(os.environ["CIDX_SERVER_DATA_DIR"])
        db_path = server_dir / "data" / "cidx_server.db"

        # Insert running scan (completed_at IS NULL = still running)
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO self_monitoring_scans
                (scan_id, started_at, completed_at, status, log_id_start, log_id_end, issues_created)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-scan-running",
                    datetime.now().isoformat(),
                    None,
                    "RUNNING",
                    1,
                    None,
                    0,
                ),
            )
            conn.commit()

        # Request the page
        response = authenticated_client.get("/admin/self-monitoring")

        assert response.status_code == 200
        html = response.text

        # Should show "Running..." when scan has completed_at IS NULL
        assert "Running" in html or "running" in html.lower()
        # Should NOT show "Idle" when scan is actually running
        assert '<span id="scan-status">Idle</span>' not in html

    def test_scan_status_shows_idle_after_job_completes(
        self,
        authenticated_client: TestClient,
        web_infrastructure,
    ):
        """Test Scan Status shows 'Idle' after background job completes."""
        import os

        # Get database path
        server_dir = Path(os.environ["CIDX_SERVER_DATA_DIR"])
        db_path = server_dir / "data" / "cidx_server.db"

        # Insert completed background job
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                INSERT INTO background_jobs
                (job_id, operation_type, status, created_at, started_at, completed_at, progress, username)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test-job-completed",
                    "self_monitoring",
                    "completed",
                    (datetime.now() - timedelta(minutes=10)).isoformat(),
                    (datetime.now() - timedelta(minutes=10)).isoformat(),
                    (datetime.now() - timedelta(minutes=5)).isoformat(),
                    100,
                    "admin",
                ),
            )
            conn.commit()

        # Request the page
        response = authenticated_client.get("/admin/self-monitoring")

        assert response.status_code == 200
        html = response.text

        # Should show "Idle" when no running jobs exist
        assert "Idle" in html
