"""
Unit tests for SelfMonitoringService (Story #72 - AC3).

Tests scheduled task behavior:
- Timer-based execution at configured cadence
- Job submission to background job queue
- Single-threaded execution (no concurrent scans)
- Proper start/stop lifecycle
"""

from unittest.mock import Mock


class TestSelfMonitoringService:
    """Test suite for SelfMonitoringService scheduled task."""

    def test_service_initializes_with_config(self):
        """Test SelfMonitoringService initializes with configuration."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=True, cadence_minutes=30, job_manager=Mock()
        )

        assert service.enabled is True
        assert service.cadence_minutes == 30
        assert service.is_running is False

    def test_service_does_not_start_when_disabled(self):
        """Test service does not start when disabled."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=False, cadence_minutes=60, job_manager=Mock()
        )

        service.start()

        assert service.is_running is False

    def test_service_starts_background_thread_when_enabled(self):
        """Test service starts background thread when enabled."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=True, cadence_minutes=60, job_manager=Mock()
        )

        service.start()

        try:
            assert service.is_running is True
            assert service._thread is not None
            assert service._thread.is_alive()
        finally:
            service.stop()

    def test_service_stops_cleanly(self):
        """Test service stops background thread cleanly."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=True, cadence_minutes=60, job_manager=Mock()
        )

        service.start()
        assert service.is_running is True

        service.stop()

        assert service.is_running is False
        if service._thread:
            assert not service._thread.is_alive()

    def test_service_can_restart_after_stop(self):
        """Test service can be restarted after being stopped."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=True, cadence_minutes=60, job_manager=Mock()
        )

        # First start/stop cycle
        service.start()
        assert service.is_running is True
        service.stop()
        assert service.is_running is False

        # Second start/stop cycle
        service.start()
        assert service.is_running is True
        service.stop()
        assert service.is_running is False


class TestSelfMonitoringServiceLogScannerIntegration:
    """Test suite for SelfMonitoringService integration with LogScanner (Bug #87)."""

    def test_service_accepts_database_and_config_parameters(self):
        """Test that service accepts db_path, log_db_path, github_repo, prompt_template, and model."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock(),
            db_path="/path/to/cidx_server.db",
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
            prompt_template="Custom prompt with {last_scan_log_id} and {dedup_context}",
            model="sonnet",
        )

        assert service._db_path == "/path/to/cidx_server.db"
        assert service._log_db_path == "/path/to/logs.db"
        assert service._github_repo == "owner/repo"
        assert (
            service._prompt_template
            == "Custom prompt with {last_scan_log_id} and {dedup_context}"
        )
        assert service._model == "sonnet"

    def test_service_uses_default_prompt_when_template_empty(self):
        """Test that service loads default prompt when prompt_template is empty."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import patch, MagicMock
        import tempfile

        # Create temporary database
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock(),
            db_path=temp_db.name,
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
            prompt_template="",  # Empty - should load default
            model="opus",
        )

        # Mock LogScanner to capture constructor args
        with patch(
            "code_indexer.server.self_monitoring.scanner.LogScanner"
        ) as mock_scanner_class:
            mock_scanner_instance = MagicMock()
            mock_scanner_instance.execute_scan.return_value = {"status": "SUCCESS"}
            mock_scanner_class.return_value = mock_scanner_instance

            # Execute scan (this should load default prompt)
            result = service._execute_scan()

            # Verify LogScanner was created with default prompt
            mock_scanner_class.assert_called_once()
            call_kwargs = mock_scanner_class.call_args[1]
            assert "{last_scan_log_id}" in call_kwargs["prompt_template"]
            assert "{dedup_context}" in call_kwargs["prompt_template"]
            assert "Three-Tier Deduplication" in call_kwargs["prompt_template"]

        # Cleanup
        import os

        os.unlink(temp_db.name)

    def test_service_execute_scan_creates_log_scanner_with_correct_params(self):
        """Test that _execute_scan creates LogScanner with correct parameters."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import patch, MagicMock
        import tempfile

        # Create temporary database
        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock(),
            db_path=temp_db.name,
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
            prompt_template="Custom prompt: {last_scan_log_id}, {dedup_context}",
            model="opus",
        )

        # Mock LogScanner to verify constructor calls
        with patch(
            "code_indexer.server.self_monitoring.scanner.LogScanner"
        ) as mock_scanner_class:
            mock_scanner_instance = MagicMock()
            mock_scanner_instance.execute_scan.return_value = {
                "status": "SUCCESS",
                "issues_created": 0,
                "duplicates_skipped": 0,
                "potential_duplicates_commented": 0,
                "max_log_id_processed": 100,
            }
            mock_scanner_class.return_value = mock_scanner_instance

            # Execute scan
            result = service._execute_scan()

            # Verify LogScanner was created with correct parameters
            mock_scanner_class.assert_called_once()
            call_kwargs = mock_scanner_class.call_args[1]

            assert call_kwargs["db_path"] == temp_db.name
            assert call_kwargs["log_db_path"] == "/path/to/logs.db"
            assert call_kwargs["github_repo"] == "owner/repo"
            assert (
                call_kwargs["prompt_template"]
                == "Custom prompt: {last_scan_log_id}, {dedup_context}"
            )
            assert call_kwargs["model"] == "opus"
            assert "scan_id" in call_kwargs

            # Verify scanner.execute_scan was called
            mock_scanner_instance.execute_scan.assert_called_once()

            # Verify result is returned
            assert result["status"] == "SUCCESS"

        # Cleanup
        import os

        os.unlink(temp_db.name)

    def test_service_skips_scan_if_db_paths_not_configured(self):
        """Test that service logs warning and returns error if db_path or log_db_path is None."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock(),
            db_path=None,  # Not configured
            log_db_path=None,
            github_repo="owner/repo",
            prompt_template="",
            model="opus",
        )

        result = service._execute_scan()

        assert result["status"] == "FAILURE"
        assert (
            "not configured" in result["error"].lower()
            or "missing" in result["error"].lower()
        )

    def test_submit_scan_job_includes_repo_alias(self):
        """Test _submit_scan_job passes repo_alias parameter to submit_job (Bug #87 - Issue #1)."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        mock_job_manager = Mock()
        mock_job_manager.submit_job.return_value = "job-123"

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=mock_job_manager,
            db_path="/path/to/db",
            log_db_path="/path/to/logs",
            github_repo="owner/repo",
        )

        # Call _submit_scan_job directly
        service._submit_scan_job()

        # Verify submit_job was called with repo_alias parameter
        mock_job_manager.submit_job.assert_called_once()
        call_kwargs = mock_job_manager.submit_job.call_args[1]
        assert "repo_alias" in call_kwargs
        assert call_kwargs["repo_alias"] == "owner/repo"

    def test_trigger_scan_includes_repo_alias(self):
        """Test trigger_scan passes repo_alias parameter to submit_job (Bug #87 - Issue #1)."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        mock_job_manager = Mock()
        mock_job_manager.submit_job.return_value = "job-456"

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=mock_job_manager,
            db_path="/path/to/db",
            log_db_path="/path/to/logs",
            github_repo="owner/repo",
        )

        # Call trigger_scan
        result = service.trigger_scan()

        # Verify submit_job was called with repo_alias parameter
        assert result["status"] == "queued"
        mock_job_manager.submit_job.assert_called_once()
        call_kwargs = mock_job_manager.submit_job.call_args[1]
        assert "repo_alias" in call_kwargs
        assert call_kwargs["repo_alias"] == "owner/repo"

    def test_service_accepts_github_token_and_server_name(self):
        """Test service accepts github_token and server_name parameters (Bug #87 - Issue #2)."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock(),
            db_path="/path/to/db",
            log_db_path="/path/to/logs",
            github_repo="owner/repo",
            github_token="ghp_test_token",
            server_name="Production CIDX Server",
        )

        assert service._github_token == "ghp_test_token"
        assert service._server_name == "Production CIDX Server"

    def test_service_passes_github_token_to_scanner(self):
        """Test service passes github_token to LogScanner (Bug #87 - Issue #2)."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import patch, MagicMock
        import tempfile

        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock(),
            db_path=temp_db.name,
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
            github_token="ghp_test_token_123",
            server_name="Test Server",
        )

        with patch(
            "code_indexer.server.self_monitoring.scanner.LogScanner"
        ) as mock_scanner_class:
            mock_scanner_instance = MagicMock()
            mock_scanner_instance.execute_scan.return_value = {"status": "SUCCESS"}
            mock_scanner_class.return_value = mock_scanner_instance

            service._execute_scan()

            # Verify LogScanner was created with github_token and server_name
            mock_scanner_class.assert_called_once()
            call_kwargs = mock_scanner_class.call_args[1]
            assert "github_token" in call_kwargs
            assert call_kwargs["github_token"] == "ghp_test_token_123"
            assert "server_name" in call_kwargs
            assert call_kwargs["server_name"] == "Test Server"

        import os

        os.unlink(temp_db.name)


class TestSelfMonitoringServiceStartupCadence:
    """Test suite for Bug #127 - Service respects last scan timestamp on startup."""

    # Issue #4 Code Review Fix: Extract magic number to named constant
    TIMESTAMP_TOLERANCE_SECONDS = 120  # Tolerance for test execution time

    def _create_test_db(self, last_scan_timestamp=None):
        """
        Create a temporary test database with self_monitoring_scans table.

        Args:
            last_scan_timestamp: Optional ISO format timestamp for last scan.
                               If None, creates empty scans table.

        Returns:
            Path to temporary database file (caller must delete)
        """
        import tempfile
        import sqlite3

        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()

        conn = sqlite3.connect(temp_db.name)
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE self_monitoring_scans (
                scan_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                log_id_start INTEGER NOT NULL,
                log_id_end INTEGER,
                issues_created INTEGER DEFAULT 0,
                error_message TEXT
            )
        """
        )

        if last_scan_timestamp:
            cursor.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, status, log_id_start, log_id_end, issues_created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"scan-{id(last_scan_timestamp)}",
                    last_scan_timestamp,
                    "COMPLETED",
                    1,
                    100,
                    0,
                ),
            )

        conn.commit()
        conn.close()

        return temp_db.name

    def test_service_waits_when_last_scan_was_recent(self):
        """
        Bug #127: Service should wait remaining time when last scan was recent.

        Scenario: Last scan was 1 hour ago, cadence is 6 hours (360 minutes)
        Expected: Service waits ~5 hours (300 minutes) before first scan
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import Mock
        from datetime import datetime, timedelta

        # Create database with last scan 1 hour ago
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        temp_db_path = self._create_test_db(one_hour_ago.isoformat())

        mock_job_manager = Mock()

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=360,  # 6 hours
            job_manager=mock_job_manager,
            db_path=temp_db_path,
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
        )

        # Mock _stop_event.wait to capture timeout
        wait_timeouts = []

        def capture_wait(timeout=None):
            wait_timeouts.append(timeout)
            service._running = False
            return True

        service._stop_event.wait = capture_wait

        service.start()

        import time

        time.sleep(0.5)

        service.stop()

        # Verify: First wait should be ~300 minutes (5 hours remaining)
        # Allow 2 minute tolerance for test execution time
        assert len(wait_timeouts) > 0, "Expected at least one wait call"
        first_wait = wait_timeouts[0]
        expected_wait = 300 * 60  # 5 hours in seconds
        assert first_wait is not None, "Expected initial wait timeout"
        assert (
            abs(first_wait - expected_wait) < self.TIMESTAMP_TOLERANCE_SECONDS
        ), f"Expected ~{expected_wait}s wait, got {first_wait}s"

        # Verify: No scan was submitted during initial wait
        mock_job_manager.submit_job.assert_not_called()

        # Cleanup
        import os

        os.unlink(temp_db_path)

    def test_service_runs_immediately_when_no_previous_scans(self):
        """
        Bug #127: Service should run immediately when no previous scans exist.

        Scenario: Database has no scans (fresh install or first run)
        Expected: Service submits scan immediately without waiting
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import Mock

        # Create database with empty scans table
        temp_db_path = self._create_test_db(last_scan_timestamp=None)

        mock_job_manager = Mock()
        mock_job_manager.submit_job.return_value = "job-999"

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=360,  # 6 hours
            job_manager=mock_job_manager,
            db_path=temp_db_path,
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
        )

        # Mock _stop_event.wait to immediately stop after first iteration
        def stop_after_first(timeout=None):
            service._running = False
            return True

        service._stop_event.wait = stop_after_first

        service.start()

        import time

        time.sleep(0.5)

        service.stop()

        # Verify: Scan was submitted immediately (no initial wait)
        mock_job_manager.submit_job.assert_called_once()

        # Cleanup
        import os

        os.unlink(temp_db_path)

    def test_service_runs_immediately_when_elapsed_exceeds_cadence(self):
        """
        Bug #127: Service should run immediately when elapsed time >= cadence.

        Scenario: Last scan was 7 hours ago, cadence is 6 hours
        Expected: Service submits scan immediately (no wait)
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import Mock
        from datetime import datetime, timedelta

        # Create database with last scan 7 hours ago (exceeds 6-hour cadence)
        seven_hours_ago = datetime.utcnow() - timedelta(hours=7)
        temp_db_path = self._create_test_db(seven_hours_ago.isoformat())

        mock_job_manager = Mock()
        mock_job_manager.submit_job.return_value = "job-888"

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=360,  # 6 hours
            job_manager=mock_job_manager,
            db_path=temp_db_path,
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
        )

        # Mock _stop_event.wait to immediately stop after first iteration
        def stop_after_first(timeout=None):
            service._running = False
            return True

        service._stop_event.wait = stop_after_first

        service.start()

        import time

        time.sleep(0.5)

        service.stop()

        # Verify: Scan was submitted immediately (no wait since elapsed > cadence)
        mock_job_manager.submit_job.assert_called_once()

        # Cleanup
        import os

        os.unlink(temp_db_path)

    def test_trigger_scan_after_enabled_via_toggle(self):
        """
        Bug #128 Code Review Fix: Verify trigger_scan() works after enabling via toggle.

        Critical Issue: When service is started via toggle, the service's internal
        _enabled flag remains False (initialized with old config value). This causes
        trigger_scan() to fail with "Self-monitoring is not enabled".

        Scenario:
        1. Service initialized with enabled=False
        2. Service._enabled flag updated to True (simulating toggle)
        3. Service.start() called
        4. trigger_scan() invoked manually

        Expected: trigger_scan() succeeds (returns "queued" status)
        Bug: trigger_scan() fails because _enabled still False
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import Mock

        mock_job_manager = Mock()
        mock_job_manager.submit_job.return_value = "job-trigger-999"

        # Step 1: Initialize service with enabled=False (old config state)
        service = SelfMonitoringService(
            enabled=False,
            cadence_minutes=60,
            job_manager=mock_job_manager,
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
        )

        assert service.enabled is False
        assert service.is_running is False

        # Step 2: Update _enabled flag (simulating toggle fix)
        service._enabled = True

        # Step 3: Start service
        service.start()

        # Wait for service to start
        import time

        time.sleep(0.5)

        assert service.is_running is True

        # Stop the background thread to prevent automatic scans
        service.stop()

        # Reset mock to isolate manual trigger
        mock_job_manager.reset_mock()

        # Step 4: Trigger manual scan (service stopped but _enabled is True)
        result = service.trigger_scan()

        # Verify: trigger_scan() succeeds because _enabled was updated
        assert result["status"] == "queued", f"Expected 'queued' status, got: {result}"
        assert "scan_id" in result, f"Expected 'scan_id' in result, got: {result}"
        assert result["scan_id"] == "job-trigger-999"

        # Verify job was submitted
        mock_job_manager.submit_job.assert_called_once()

    def test_trigger_scan_fails_without_enabled_sync(self):
        """
        Bug #128 Code Review: Demonstrate the bug when _enabled is not synchronized.

        This test shows the failure mode when routes.py starts the service
        without updating the _enabled flag first.

        Scenario:
        1. Service initialized with enabled=False
        2. Service.start() called WITHOUT updating _enabled (BUG)
        3. trigger_scan() invoked manually

        Expected (bug behavior): trigger_scan() returns error "Self-monitoring is not enabled"
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import Mock

        mock_job_manager = Mock()
        mock_job_manager.submit_job.return_value = "job-trigger-999"

        # Step 1: Initialize service with enabled=False (old config state)
        service = SelfMonitoringService(
            enabled=False,
            cadence_minutes=60,
            job_manager=mock_job_manager,
            log_db_path="/path/to/logs.db",
            github_repo="owner/repo",
        )

        assert service.enabled is False
        assert service.is_running is False

        # Step 2: Start service WITHOUT updating _enabled (simulating bug in routes.py)
        # The start() method will not actually start because _enabled is False
        service.start()

        # Wait for service to start (it won't because _enabled is False)
        import time

        time.sleep(0.5)

        # Service did not start because _enabled is False
        assert service.is_running is False

        # Step 3: Trigger manual scan
        result = service.trigger_scan()

        # Verify: trigger_scan() fails with "not enabled" error (BUG DEMONSTRATED)
        assert result["status"] == "error", f"Expected 'error' status, got: {result}"
        assert (
            "not enabled" in result["error"].lower()
        ), f"Expected 'not enabled' error, got: {result['error']}"

        # Verify job was NOT submitted
        mock_job_manager.submit_job.assert_not_called()


class TestOrphanedScanCleanup:
    """Test suite for orphaned scan cleanup (Feature request - automated cleanup)."""

    def _create_test_db_with_scans(self, scans_data):
        """
        Create a temporary test database with self_monitoring_scans table and test data.

        Args:
            scans_data: List of tuples (scan_id, started_at_iso, completed_at_iso, status)

        Returns:
            Path to temporary database file (caller must delete)
        """
        import tempfile
        import sqlite3

        temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        temp_db.close()

        conn = sqlite3.connect(temp_db.name)
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE self_monitoring_scans (
                scan_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                log_id_start INTEGER NOT NULL,
                log_id_end INTEGER,
                issues_created INTEGER DEFAULT 0,
                error_message TEXT
            )
        """
        )

        for scan_data in scans_data:
            scan_id, started_at, completed_at, status = scan_data
            cursor.execute(
                "INSERT INTO self_monitoring_scans "
                "(scan_id, started_at, completed_at, status, log_id_start, log_id_end, issues_created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scan_id, started_at, completed_at, status, 1, 100, 0),
            )

        conn.commit()
        conn.close()

        return temp_db.name

    def test_cleanup_marks_orphaned_scans_as_failure(self):
        """
        Test cleanup finds and marks orphaned scans (>2 hours old) as FAILURE.

        Scenario: Database has orphaned scan (completed_at=NULL, started 3 hours ago)
        Expected: Cleanup marks scan as FAILURE with error message
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from datetime import datetime, timedelta
        import sqlite3

        # Create database with orphaned scan (3 hours ago, completed_at=NULL)
        three_hours_ago = (datetime.utcnow() - timedelta(hours=3)).isoformat()
        scans_data = [("orphaned-scan-1", three_hours_ago, None, "RUNNING")]
        temp_db_path = self._create_test_db_with_scans(scans_data)

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=None,  # Not needed for cleanup test
            db_path=temp_db_path,
        )

        # Call cleanup method (will be implemented)
        service._cleanup_orphaned_scans()

        # Verify: Scan is marked as FAILURE with correct error message
        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT completed_at, status, error_message FROM self_monitoring_scans WHERE scan_id = ?",
            ("orphaned-scan-1",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        completed_at, status, error_message = row
        assert completed_at is not None, "Expected completed_at to be set"
        assert status == "FAILURE", f"Expected status FAILURE, got {status}"
        assert (
            "orphaned" in error_message.lower()
        ), f"Expected 'orphaned' in error message, got: {error_message}"
        assert (
            "2 hours" in error_message.lower()
        ), f"Expected '2 hours' in error message, got: {error_message}"

        # Cleanup
        import os

        os.unlink(temp_db_path)

    def test_cleanup_ignores_recent_scans(self):
        """
        Test cleanup ignores recent scans (<2 hours old).

        Scenario: Database has recent scan (completed_at=NULL, started 1 hour ago)
        Expected: Cleanup does NOT mark scan as FAILURE (still running legitimately)
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from datetime import datetime, timedelta
        import sqlite3

        # Create database with recent scan (1 hour ago, completed_at=NULL)
        one_hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        scans_data = [("recent-scan-1", one_hour_ago, None, "RUNNING")]
        temp_db_path = self._create_test_db_with_scans(scans_data)

        service = SelfMonitoringService(
            enabled=True, cadence_minutes=60, job_manager=None, db_path=temp_db_path
        )

        # Call cleanup method
        service._cleanup_orphaned_scans()

        # Verify: Scan is still RUNNING (not marked as FAILURE)
        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT completed_at, status FROM self_monitoring_scans WHERE scan_id = ?",
            ("recent-scan-1",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        completed_at, status = row
        assert completed_at is None, "Expected completed_at to still be NULL"
        assert status == "RUNNING", f"Expected status RUNNING, got {status}"

        # Cleanup
        import os

        os.unlink(temp_db_path)

    def test_cleanup_ignores_already_completed_scans(self):
        """
        Test cleanup ignores already completed scans.

        Scenario: Database has completed scan (completed_at set, status=SUCCESS)
        Expected: Cleanup does NOT modify the scan
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from datetime import datetime, timedelta
        import sqlite3

        # Create database with completed scan (3 hours ago, completed_at set)
        three_hours_ago = (datetime.utcnow() - timedelta(hours=3)).isoformat()
        scans_data = [("completed-scan-1", three_hours_ago, three_hours_ago, "SUCCESS")]
        temp_db_path = self._create_test_db_with_scans(scans_data)

        service = SelfMonitoringService(
            enabled=True, cadence_minutes=60, job_manager=None, db_path=temp_db_path
        )

        # Call cleanup method
        service._cleanup_orphaned_scans()

        # Verify: Scan is still SUCCESS (not modified)
        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT completed_at, status, error_message FROM self_monitoring_scans WHERE scan_id = ?",
            ("completed-scan-1",),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        completed_at, status, error_message = row
        assert completed_at == three_hours_ago, "Expected completed_at unchanged"
        assert status == "SUCCESS", f"Expected status SUCCESS, got {status}"
        assert error_message is None, f"Expected no error_message, got: {error_message}"

        # Cleanup
        import os

        os.unlink(temp_db_path)

    def test_cleanup_logs_count_of_orphaned_scans(self):
        """
        Test cleanup logs count of orphaned scans cleaned.

        Scenario: Database has 2 orphaned scans and 1 recent scan
        Expected: Cleanup logs INFO message with count=2
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from datetime import datetime, timedelta
        from unittest.mock import patch
        import logging

        # Create database with 2 orphaned scans and 1 recent scan
        three_hours_ago = (datetime.utcnow() - timedelta(hours=3)).isoformat()
        one_hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        scans_data = [
            ("orphaned-1", three_hours_ago, None, "RUNNING"),
            ("orphaned-2", three_hours_ago, None, "RUNNING"),
            ("recent-1", one_hour_ago, None, "RUNNING"),
        ]
        temp_db_path = self._create_test_db_with_scans(scans_data)

        service = SelfMonitoringService(
            enabled=True, cadence_minutes=60, job_manager=None, db_path=temp_db_path
        )

        # Capture log messages
        with patch("code_indexer.server.self_monitoring.service.logger") as mock_logger:
            service._cleanup_orphaned_scans()

            # Verify: INFO log message contains count=2
            info_calls = [call for call in mock_logger.info.call_args_list]
            assert len(info_calls) > 0, "Expected at least one INFO log call"

            # Find the cleanup log message
            cleanup_log = None
            for call in info_calls:
                msg = call[0][0]
                if "orphaned" in msg.lower() and "2" in msg:
                    cleanup_log = msg
                    break

            assert (
                cleanup_log is not None
            ), f"Expected cleanup log with count=2, got: {[call[0][0] for call in info_calls]}"
            assert "2" in cleanup_log, f"Expected count 2 in log message: {cleanup_log}"

        # Cleanup
        import os

        os.unlink(temp_db_path)

    def test_cleanup_runs_before_each_scan(self):
        """
        Test cleanup runs before each scheduled scan.

        Scenario: Service starts and runs scheduled scan
        Expected: _cleanup_orphaned_scans() is called before _submit_scan_job()
        """
        from code_indexer.server.self_monitoring.service import SelfMonitoringService
        from unittest.mock import Mock
        import time

        mock_job_manager = Mock()
        mock_job_manager.submit_job.return_value = "job-777"

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=mock_job_manager,
            db_path="/tmp/test.db",
            log_db_path="/tmp/logs.db",
            github_repo="owner/repo",
        )

        # Track call order using shared list
        call_order = []

        def track_cleanup():
            call_order.append("cleanup")
            # Don't call original to avoid database operations

        def track_submit():
            call_order.append("submit")
            # Don't call original to avoid job submission

        service._cleanup_orphaned_scans = track_cleanup
        service._submit_scan_job = track_submit

        # Mock _stop_event.wait to stop after first iteration
        def stop_after_first(timeout=None):
            service._running = False
            return True

        service._stop_event.wait = stop_after_first

        service.start()
        time.sleep(0.5)
        service.stop()

        # Verify: Both cleanup and submit were called
        assert (
            len(call_order) >= 2
        ), f"Expected at least 2 calls (cleanup + submit), got: {call_order}"
        assert "cleanup" in call_order, "Expected cleanup to be called"
        assert "submit" in call_order, "Expected submit to be called"

        # Verify: Cleanup was called before submit
        assert call_order.index("cleanup") < call_order.index(
            "submit"
        ), f"Expected cleanup before submit, got order: {call_order}"
