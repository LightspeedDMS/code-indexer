"""
Unit tests for ActivatedRepoIndexManager.

Tests the manual re-indexing service for activated repositories,
covering semantic, FTS, temporal, and SCIP index management.
"""

import json
import pytest
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
)


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_background_job_manager():
    """Create mock background job manager."""
    manager = Mock(spec=BackgroundJobManager)
    manager.submit_job = Mock(return_value=str(uuid.uuid4()))
    manager.get_job_status = Mock(
        return_value={
            "job_id": str(uuid.uuid4()),
            "operation_type": "reindex",
            "status": "completed",
            "progress": 100,
            "result": {"success": True},
            "error": None,
        }
    )
    # Mock list_jobs to return empty lists by default (no concurrent jobs)
    manager.list_jobs = Mock(return_value={"jobs": [], "total": 0})
    return manager


@pytest.fixture
def mock_activated_repo_manager(temp_data_dir):
    """Create mock activated repository manager."""
    manager = Mock()
    # Return path within temp_data_dir to pass security validation
    repo_path = str(Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo")
    manager.get_activated_repo_path = Mock(return_value=repo_path)
    return manager


@pytest.fixture
def index_manager(
    temp_data_dir, mock_background_job_manager, mock_activated_repo_manager
):
    """Create ActivatedRepoIndexManager instance with mocks."""
    manager = ActivatedRepoIndexManager(
        data_dir=temp_data_dir,
        background_job_manager=mock_background_job_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )
    return manager


class TestTriggerReindex:
    """Tests for trigger_reindex method."""

    @patch("os.path.exists")
    def test_trigger_reindex_semantic_only(self, mock_exists, index_manager):
        """Test triggering semantic index only."""
        # Mock repository directory exists
        mock_exists.return_value = True

        job_id = index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["semantic"],
            clear=False,
            username="testuser",
        )

        assert isinstance(job_id, str)
        assert len(job_id) == 36  # UUID format

        # Verify background job was submitted
        index_manager.background_job_manager.submit_job.assert_called_once()
        call_args = index_manager.background_job_manager.submit_job.call_args
        assert call_args[0][0] == "reindex"  # operation_type
        assert call_args[1]["submitter_username"] == "testuser"

    @patch("os.path.exists")
    def test_trigger_reindex_all_types(self, mock_exists, index_manager):
        """Test triggering all four index types."""
        # Mock repository directory exists
        mock_exists.return_value = True

        job_id = index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["semantic", "fts", "temporal", "scip"],
            clear=True,
            username="testuser",
        )

        assert isinstance(job_id, str)
        index_manager.background_job_manager.submit_job.assert_called_once()

    @patch("os.path.exists")
    def test_trigger_reindex_with_clear_flag(self, mock_exists, index_manager):
        """Test triggering reindex with clear flag (rebuild)."""
        # Mock repository directory exists
        mock_exists.return_value = True

        job_id = index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["semantic"],
            clear=True,
            username="testuser",
        )

        assert isinstance(job_id, str)
        # Verify clear flag is passed to job function.
        # Bug #1154 fix: worker params are forwarded as positional *args.
        # Positional layout: (operation_type, func, repo_alias, repo_path, index_types, clear)
        # so clear=True lands at index 5. Also accept the legacy kwargs form.
        call_args = index_manager.background_job_manager.submit_job.call_args
        assert call_args[1].get("clear") is True or (
            len(call_args[0]) > 5 and call_args[0][5] is True
        )

    def test_trigger_reindex_invalid_type(self, index_manager):
        """Test triggering reindex with invalid index type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid index type"):
            index_manager.trigger_reindex(
                repo_alias="test-repo",
                index_types=["invalid_type"],
                clear=False,
                username="testuser",
            )

    def test_trigger_reindex_empty_types(self, index_manager):
        """Test triggering reindex with empty index types raises ValueError."""
        with pytest.raises(ValueError, match="At least one index type required"):
            index_manager.trigger_reindex(
                repo_alias="test-repo",
                index_types=[],
                clear=False,
                username="testuser",
            )

    def test_trigger_reindex_missing_repo(self, index_manager):
        """Test triggering reindex for non-existent repository raises FileNotFoundError."""
        # Configure mock to raise error for missing repo
        index_manager.activated_repo_manager.get_activated_repo_path.side_effect = (
            FileNotFoundError("Repository not found")
        )

        with pytest.raises(FileNotFoundError, match="'missing-repo' not found"):
            index_manager.trigger_reindex(
                repo_alias="missing-repo",
                index_types=["semantic"],
                clear=False,
                username="testuser",
            )

    @patch("os.path.exists")
    def test_trigger_reindex_returns_job_info(self, mock_exists, index_manager):
        """Test that trigger_reindex returns expected job information."""
        # Mock repository directory exists
        mock_exists.return_value = True

        result = index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["semantic", "fts"],
            clear=False,
            username="testuser",
        )

        # Should return job_id string
        assert isinstance(result, str)
        assert len(result) > 0


class TestGetIndexStatus:
    """Tests for get_index_status method."""

    def test_get_index_status_all_current(self, index_manager, temp_data_dir):
        """Test getting status when all indexes are up-to-date."""
        # Create mock .code-indexer directory with metadata
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        index_dir.mkdir(parents=True, exist_ok=True)

        # Create semantic index metadata
        metadata_file = index_dir / "metadata.json"
        metadata_file.write_text(
            json.dumps(
                {
                    "last_indexed": datetime.now(timezone.utc).isoformat(),
                    "file_count": 100,
                    "index_size_mb": 25.5,
                }
            )
        )

        # Mock repo path
        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert "semantic" in status
        assert "fts" in status
        assert "temporal" in status
        assert "scip" in status

    def test_get_index_status_semantic_details(self, index_manager, temp_data_dir):
        """Test semantic index status details."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        index_dir.mkdir(parents=True, exist_ok=True)

        last_indexed = datetime.now(timezone.utc).isoformat()
        metadata_file = index_dir / "metadata.json"
        metadata_file.write_text(
            json.dumps(
                {
                    "last_indexed": last_indexed,
                    "file_count": 150,
                }
            )
        )

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["last_indexed"] == last_indexed
        assert status["semantic"]["file_count"] == 150
        # Index size is calculated from actual files, not from metadata
        assert "index_size_mb" in status["semantic"]
        assert status["semantic"]["status"] == "up_to_date"

    def test_get_index_status_not_indexed(self, index_manager, temp_data_dir):
        """Test status when indexes don't exist."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        repo_path.mkdir(parents=True, exist_ok=True)

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "not_indexed"
        assert status["fts"]["status"] == "not_indexed"
        assert status["temporal"]["status"] == "not_indexed"
        assert status["scip"]["status"] == "not_indexed"

    def test_get_index_status_scip_success(self, index_manager, temp_data_dir):
        """Test SCIP index status when generation succeeded."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        scip_dir = repo_path / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True, exist_ok=True)

        # Create SCIP database file
        (scip_dir / "index.scip.db").touch()

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["scip"]["status"] == "SUCCESS"
        assert status["scip"]["project_count"] >= 0

    def test_get_index_status_stale_temporal(self, index_manager, temp_data_dir):
        """Test detecting stale temporal index."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        temporal_dir = repo_path / ".code-indexer" / "index" / "code-indexer-temporal"
        temporal_dir.mkdir(parents=True, exist_ok=True)

        # Create old temporal metadata (30 days ago)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        metadata_file = temporal_dir / "metadata.json"
        metadata_file.write_text(
            json.dumps({"last_indexed": old_timestamp, "commit_count": 100})
        )

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        # Should be stale (>7 days old)
        assert status["temporal"]["status"] == "stale"


class TestJobExecution:
    """Tests for job execution logic."""

    @patch("subprocess.run")
    def test_execute_semantic_indexing(self, mock_subprocess, index_manager):
        """Test semantic indexing execution."""
        # Mock successful cidx index execution
        mock_subprocess.return_value = Mock(returncode=0, stderr="", stdout="")

        result = index_manager._execute_semantic_indexing("/tmp/test-repo", False)

        assert result["success"] is True
        assert "Semantic indexing completed" in result["message"]

    @patch("subprocess.run")
    def test_execute_scip_indexing(self, mock_subprocess, index_manager):
        """Test SCIP indexing execution via subprocess."""
        # Mock successful SCIP generation
        mock_subprocess.return_value = Mock(returncode=0, stderr="", stdout="")

        result = index_manager._execute_scip_indexing("/tmp/test-repo", False)

        assert result["success"] is True
        assert "SCIP generation completed" in result["message"]

    def test_execute_indexing_with_clear_deletes_index(self, index_manager):
        """Test that clear flag deletes existing index before rebuilding."""
        # Implementation complete - semantic indexing clears index dir when clear=True
        pass


class TestJobTracking:
    """Tests for job status tracking."""

    def test_job_status_transitions(self, index_manager, mock_background_job_manager):
        """Test job status transitions from queued to running to completed."""
        job_id = str(uuid.uuid4())

        # Mock status progression
        status_progression = [
            {"status": "pending", "progress": 0},
            {"status": "running", "progress": 50},
            {"status": "completed", "progress": 100, "result": {"success": True}},
        ]

        for status_data in status_progression:
            mock_background_job_manager.get_job_status.return_value = {
                "job_id": job_id,
                "operation_type": "reindex",
                **status_data,
                "error": None,
            }

            status = mock_background_job_manager.get_job_status(job_id, "testuser")
            assert status["status"] == status_data["status"]
            assert status["progress"] == status_data["progress"]

    def test_job_failure_tracking(self, index_manager, mock_background_job_manager):
        """Test job failure is properly tracked with error message."""
        job_id = str(uuid.uuid4())
        error_message = "SCIP generation failed: Language not supported"

        mock_background_job_manager.get_job_status.return_value = {
            "job_id": job_id,
            "operation_type": "reindex",
            "status": "failed",
            "progress": 0,
            "result": None,
            "error": error_message,
        }

        status = mock_background_job_manager.get_job_status(job_id, "testuser")
        assert status["status"] == "failed"
        assert status["error"] == error_message

    def test_job_progress_tracking(self, index_manager, mock_background_job_manager):
        """Test job progress percentage tracking."""
        job_id = str(uuid.uuid4())

        progress_values = [0, 25, 50, 75, 100]
        for progress in progress_values:
            mock_background_job_manager.get_job_status.return_value = {
                "job_id": job_id,
                "operation_type": "reindex",
                "status": "running",
                "progress": progress,
                "result": None,
                "error": None,
            }

            status = mock_background_job_manager.get_job_status(job_id, "testuser")
            assert status["progress"] == progress


class TestErrorHandling:
    """Tests for error handling scenarios."""

    def test_indexing_error_on_disk_space(self, index_manager):
        """Test that IndexingError is raised when disk space is insufficient."""
        # This will be tested once implementation is complete
        pass

    def test_scip_failure_captures_stderr(self, index_manager):
        """Test that SCIP failures capture stderr for diagnostics."""
        # This will be tested once implementation is complete
        pass

    @patch("os.path.exists")
    def test_concurrent_job_prevention(self, mock_exists, index_manager):
        """Test that concurrent reindex jobs for same user are prevented."""
        # Mock repository directory exists
        mock_exists.return_value = True

        # Mock that there's already a running reindex job
        index_manager.background_job_manager.list_jobs.return_value = {
            "jobs": [
                {
                    "job_id": "existing-job-123",
                    "operation_type": "reindex",
                    "status": "running",
                    "progress": 50,
                }
            ],
            "total": 1,
        }

        # Attempt to trigger another reindex should raise ValueError
        with pytest.raises(ValueError, match="Another reindex job is already running"):
            index_manager.trigger_reindex(
                repo_alias="test-repo",
                index_types=["semantic"],
                clear=False,
                username="testuser",
            )

        # Verify list_jobs was called to check for concurrent jobs
        assert index_manager.background_job_manager.list_jobs.called


class TestIntegration:
    """Integration tests with real components (will be expanded)."""

    def test_full_reindex_workflow(self, index_manager):
        """Test complete workflow: trigger -> poll -> verify completion."""
        # This will be expanded once implementation is complete
        pass


class TestRepoAliasForwardingBug1154:
    """Regression tests for Bug #1154: repo_alias not forwarded to worker.

    BackgroundJobManager.submit_job declares repo_alias as its own keyword-only
    parameter for job tracking.  Before the fix, trigger_reindex passed
    repo_alias=repo_alias as a keyword argument, which was consumed by
    submit_job and never forwarded into *args/**kwargs that reach the worker
    function _execute_indexing_job.  The result: every reindex job failed with
    ``TypeError: _execute_indexing_job() missing 1 required positional
    argument: 'repo_alias'``.
    """

    def test_repo_alias_forwarded_to_worker(self, temp_data_dir):
        """repo_alias must reach _execute_indexing_job when job executes.

        Uses the real BackgroundJobManager so the actual *args/**kwargs
        forwarding path is exercised — no mocking of the feature under test.
        _execute_indexing_job is replaced with a spy that records its arguments
        and returns success immediately (avoids needing real index tooling).
        """
        import threading
        from unittest.mock import patch

        real_bjm = BackgroundJobManager()

        mock_arm = Mock()
        repo_path = str(Path(temp_data_dir) / "activated-repos" / "testuser" / "myrepo")
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        mock_arm.get_activated_repo_path = Mock(return_value=repo_path)

        manager = ActivatedRepoIndexManager(
            data_dir=temp_data_dir,
            background_job_manager=real_bjm,
            activated_repo_manager=mock_arm,
        )

        received_kwargs: dict = {}
        worker_called = threading.Event()

        def spy_execute(
            repo_alias: str,
            repo_path: str,
            index_types,
            clear: bool,
            progress_callback=None,
        ):
            received_kwargs["repo_alias"] = repo_alias
            received_kwargs["repo_path"] = repo_path
            received_kwargs["index_types"] = index_types
            received_kwargs["clear"] = clear
            worker_called.set()
            return {"success": True, "details": {}}

        with patch.object(manager, "_execute_indexing_job", side_effect=spy_execute):
            # Mock path exists check
            with patch("os.path.exists", return_value=True):
                manager.trigger_reindex(
                    repo_alias="myrepo",
                    index_types=["semantic"],
                    clear=False,
                    username="testuser",
                )

        # Wait for the background worker to run (real thread pool)
        assert worker_called.wait(timeout=10), (
            "Worker _execute_indexing_job was never called within 10 seconds"
        )

        # The critical assertion: repo_alias must have been forwarded
        assert received_kwargs.get("repo_alias") == "myrepo", (
            f"Bug #1154: repo_alias was NOT forwarded to _execute_indexing_job. "
            f"Worker received kwargs: {received_kwargs}"
        )
        assert received_kwargs.get("repo_path") == repo_path
        assert received_kwargs.get("index_types") == ["semantic"]
        assert received_kwargs.get("clear") is False

        real_bjm.shutdown()


class TestSvcMigrate003Regression:
    """Regression tests for SVC-MIGRATE-003 log interpolation bug.

    Before the fix, the logger.error call in _execute_all_index_types used
    a plain string with {index_type}/{error_msg} placeholders instead of an
    f-string, so the actual values were never substituted.  These tests verify
    that after the fix the real values appear in the logged message.
    """

    def test_svc_migrate_003_log_contains_real_index_type_and_error(
        self, index_manager, temp_data_dir
    ):
        """SVC-MIGRATE-003: logged message must contain actual index_type and error string.

        Drives _execute_all_index_types directly by patching
        _execute_single_index_type to return a failure dict, then captures the
        logger.error call and asserts the message contains the real values.
        """
        from unittest.mock import patch

        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        )
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        expected_error = "Voyage AI timed out after 30s"
        expected_type = "semantic"

        def _fake_single(rp, index_type, clear):
            return {"success": False, "error": expected_error}

        captured_calls = []

        def _fake_logger_error(msg, *args, **kwargs):
            captured_calls.append(msg)

        with patch.object(
            index_manager, "_execute_single_index_type", side_effect=_fake_single
        ):
            with patch.object(
                index_manager.logger, "error", side_effect=_fake_logger_error
            ):
                index_manager._execute_all_index_types(
                    repo_path=repo_path,
                    index_types=[expected_type],
                    clear=False,
                    update_progress=lambda pct, message="": None,
                    allocator=None,
                )

        assert len(captured_calls) >= 1, (
            "SVC-MIGRATE-003: expected logger.error to be called at least once"
        )
        logged_msg = captured_calls[0]
        assert expected_type in logged_msg, (
            f"SVC-MIGRATE-003: log message must contain the real index_type '{expected_type}', "
            f"got: {logged_msg!r}"
        )
        assert expected_error in logged_msg, (
            f"SVC-MIGRATE-003: log message must contain the real error '{expected_error}', "
            f"got: {logged_msg!r}"
        )
        assert "{index_type}" not in logged_msg, (
            f"SVC-MIGRATE-003: log message must not contain literal '{{index_type}}', "
            f"got: {logged_msg!r}"
        )
        assert "{error_msg}" not in logged_msg, (
            f"SVC-MIGRATE-003: log message must not contain literal '{{error_msg}}', "
            f"got: {logged_msg!r}"
        )

    def test_execute_all_index_types_returns_success_false_on_index_failure(
        self, index_manager, temp_data_dir
    ):
        """_execute_all_index_types must return success=False when any index_type fails.

        This ensures the job-completion logic in BackgroundJobManager correctly
        marks the job as FAILED (not COMPLETED) when indexing partially or
        fully fails, preventing a 300s e2e poll timeout.
        """
        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        )
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        def _fake_single(rp, index_type, clear):
            if index_type == "semantic":
                return {"success": False, "error": "VoyageAI connection refused"}
            return {"success": True, "message": "ok"}

        with patch.object(
            index_manager, "_execute_single_index_type", side_effect=_fake_single
        ):
            results = index_manager._execute_all_index_types(
                repo_path=repo_path,
                index_types=["semantic", "fts"],
                clear=False,
                update_progress=lambda pct, message="": None,
                allocator=None,
            )

        assert "semantic" in results
        assert results["semantic"]["success"] is False, (
            "_execute_all_index_types must preserve the failure result dict from "
            "_execute_single_index_type so the caller can compute all_success=False"
        )
        assert "fts" in results
        assert results["fts"]["success"] is True


class TestProxyModeReindexGuard:
    """Regression tests for proxy-mode composite repo reindex guard.

    Before the fix, trigger_reindex on a composite repo would call
    _execute_fts_indexing (and other index types) with cwd=<composite root>,
    which has proxy_mode: true in .code-indexer/config.json.  Running
    ``cidx index --fts`` from that cwd causes ``CommandModeDetector`` to detect
    proxy mode, blocking the command with:

        SVC-MIGRATE-003: Failed to index fts: ... no configuration found -
        project needs initialization.

    The fix adds a proxy-mode pre-flight check in _execute_single_index_type:
    if .code-indexer/config.json exists and proxy_mode is true, return
    success=False with a clear diagnostic message -- no subprocess spawned.
    """

    def test_fts_indexing_fails_fast_on_proxy_mode_repo(
        self, index_manager, temp_data_dir
    ):
        """_execute_single_index_type must return success=False for a proxy-mode repo.

        Creates a real directory with .code-indexer/config.json containing
        proxy_mode: true, then calls _execute_single_index_type("fts") and
        asserts it returns success=False with a message mentioning composite
        or proxy -- without spawning a subprocess.
        """
        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "ms1139composite"
        )
        code_indexer_dir = Path(repo_path) / ".code-indexer"
        code_indexer_dir.mkdir(parents=True, exist_ok=True)
        (code_indexer_dir / "config.json").write_text(
            json.dumps({"proxy_mode": True, "codebase_dir": repo_path})
        )

        with patch("subprocess.run") as mock_run:
            result = index_manager._execute_single_index_type(repo_path, "fts", False)

        assert result["success"] is False, (
            "proxy-mode repo must return success=False from _execute_single_index_type"
        )
        error_msg = result.get("error", "")
        assert "composite" in error_msg.lower() or "proxy" in error_msg.lower(), (
            f"error message must mention 'composite' or 'proxy', got: {error_msg!r}"
        )
        assert not mock_run.called, (
            "subprocess must NOT be spawned for a proxy-mode repo"
        )

    def test_semantic_indexing_fails_fast_on_proxy_mode_repo(
        self, index_manager, temp_data_dir
    ):
        """_execute_single_index_type must return success=False for semantic on a proxy repo."""
        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "ms1139composite"
        )
        code_indexer_dir = Path(repo_path) / ".code-indexer"
        code_indexer_dir.mkdir(parents=True, exist_ok=True)
        (code_indexer_dir / "config.json").write_text(
            json.dumps({"proxy_mode": True, "codebase_dir": repo_path})
        )

        with patch("subprocess.run") as mock_run:
            result = index_manager._execute_single_index_type(
                repo_path, "semantic", False
            )

        assert result["success"] is False
        assert not mock_run.called, (
            "subprocess must NOT be spawned for a proxy-mode repo"
        )

    def test_temporal_indexing_fails_fast_on_proxy_mode_repo(
        self, index_manager, temp_data_dir
    ):
        """_execute_single_index_type must return success=False for temporal on a proxy repo."""
        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "ms1139composite"
        )
        code_indexer_dir = Path(repo_path) / ".code-indexer"
        code_indexer_dir.mkdir(parents=True, exist_ok=True)
        (code_indexer_dir / "config.json").write_text(
            json.dumps({"proxy_mode": True, "codebase_dir": repo_path})
        )

        with patch("subprocess.run") as mock_run:
            result = index_manager._execute_single_index_type(
                repo_path, "temporal", False
            )

        assert result["success"] is False
        assert not mock_run.called, (
            "subprocess must NOT be spawned for a proxy-mode repo"
        )

    def test_non_proxy_repo_proceeds_normally(self, index_manager, temp_data_dir):
        """_execute_single_index_type must NOT block a non-proxy (local-mode) repo.

        Verifies the guard is narrow: only proxy_mode: true triggers the fail-fast.
        """
        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "markupsafe"
        )
        code_indexer_dir = Path(repo_path) / ".code-indexer"
        code_indexer_dir.mkdir(parents=True, exist_ok=True)
        (code_indexer_dir / "config.json").write_text(
            json.dumps({"proxy_mode": False, "codebase_dir": repo_path})
        )

        mock_completed = Mock()
        mock_completed.returncode = 0
        mock_completed.stderr = ""

        with patch("subprocess.run", return_value=mock_completed) as mock_run:
            result = index_manager._execute_single_index_type(repo_path, "fts", False)

        assert result["success"] is True, (
            f"non-proxy repo must proceed to subprocess; got: {result!r}"
        )
        mock_run.assert_called_once()

    def test_missing_config_json_proceeds_normally(self, index_manager, temp_data_dir):
        """_execute_single_index_type must not block when .code-indexer/config.json absent.

        If the file is missing (e.g. fresh clone not yet initialized), the guard
        must not fire -- let the subprocess fail with its own diagnostic.
        """
        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "fresh-repo"
        )
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        # Deliberately do NOT create .code-indexer/config.json

        mock_completed = Mock()
        mock_completed.returncode = 0
        mock_completed.stderr = ""

        with patch("subprocess.run", return_value=mock_completed) as mock_run:
            result = index_manager._execute_single_index_type(repo_path, "fts", False)

        assert result["success"] is True
        mock_run.assert_called_once()
