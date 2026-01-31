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
            enabled=True,
            cadence_minutes=30,
            job_manager=Mock()
        )

        assert service.enabled is True
        assert service.cadence_minutes == 30
        assert service.is_running is False

    def test_service_does_not_start_when_disabled(self):
        """Test service does not start when disabled."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=False,
            cadence_minutes=60,
            job_manager=Mock()
        )

        service.start()

        assert service.is_running is False

    def test_service_starts_background_thread_when_enabled(self):
        """Test service starts background thread when enabled."""
        from code_indexer.server.self_monitoring.service import SelfMonitoringService

        service = SelfMonitoringService(
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock()
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
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock()
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
            enabled=True,
            cadence_minutes=60,
            job_manager=Mock()
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
            model="sonnet"
        )

        assert service._db_path == "/path/to/cidx_server.db"
        assert service._log_db_path == "/path/to/logs.db"
        assert service._github_repo == "owner/repo"
        assert service._prompt_template == "Custom prompt with {last_scan_log_id} and {dedup_context}"
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
            model="opus"
        )

        # Mock LogScanner to capture constructor args
        with patch('code_indexer.server.self_monitoring.scanner.LogScanner') as mock_scanner_class:
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
            model="opus"
        )

        # Mock LogScanner to verify constructor calls
        with patch('code_indexer.server.self_monitoring.scanner.LogScanner') as mock_scanner_class:
            mock_scanner_instance = MagicMock()
            mock_scanner_instance.execute_scan.return_value = {
                "status": "SUCCESS",
                "issues_created": 0,
                "duplicates_skipped": 0,
                "potential_duplicates_commented": 0,
                "max_log_id_processed": 100
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
            assert call_kwargs["prompt_template"] == "Custom prompt: {last_scan_log_id}, {dedup_context}"
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
            model="opus"
        )

        result = service._execute_scan()

        assert result["status"] == "FAILURE"
        assert "not configured" in result["error"].lower() or "missing" in result["error"].lower()

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
            github_repo="owner/repo"
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
            github_repo="owner/repo"
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
            server_name="Production CIDX Server"
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
            server_name="Test Server"
        )

        with patch('code_indexer.server.self_monitoring.scanner.LogScanner') as mock_scanner_class:
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
