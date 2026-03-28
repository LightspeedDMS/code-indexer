"""
Unit tests for RefreshScheduler error logging quality.

Bug #171: Verify that error logs include exception type names, repository aliases,
and stack traces even when exception messages are empty.

Tests ensure all error logging statements in refresh_scheduler.py follow the pattern:
    logger.error(f"Operation failed for {alias}: {type(e).__name__}: {e}", exc_info=True)
"""

import logging
import subprocess
from unittest.mock import Mock, patch
import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.services.progress_subprocess_runner import IndexingSubprocessError


@pytest.fixture
def mock_golden_repos_dir(tmp_path):
    """Create temporary golden repos directory."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir()
    return str(golden_dir)


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.get_global_repo.return_value = {
        "alias_name": "test-repo-global",
        "repo_url": "git@github.com:test/repo.git",
        "enable_temporal": False,
        "enable_scip": False,
    }
    return registry


@pytest.fixture
def scheduler(
    mock_golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    return RefreshScheduler(
        golden_repos_dir=mock_golden_repos_dir,
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


class TestSemanticFtsIndexingErrorLogging:
    def test_logs_exception_type_when_stderr_empty(
        self, scheduler, caplog, mock_registry, tmp_path
    ):
        alias_name = "test-repo-global"
        source_path = tmp_path / "repo"
        source_path.mkdir()
        error_text = (
            f"Failed to indexing on source for {alias_name}: CalledProcessError: "
        )
        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=IndexingSubprocessError(error_text),
            ):
                with caplog.at_level(logging.ERROR):
                    with pytest.raises(RuntimeError):
                        scheduler._index_source(alias_name, str(source_path))

        error_logs = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_logs) > 0, "Expected at least one ERROR log"
        indexing_logs = [
            r
            for r in error_logs
            if "indexing on source failed for" in r.message and alias_name in r.message
        ]
        assert len(indexing_logs) >= 1, (
            f"Expected at least one indexing error log, got {len(indexing_logs)}. "
            f"Records: {[r.message for r in error_logs]}"
        )
        log_message = indexing_logs[0].message
        assert "CalledProcessError" in log_message, (
            f"Log must contain 'CalledProcessError', got: {log_message}"
        )
        assert alias_name in log_message
        assert indexing_logs[0].exc_info is not None

    def test_logs_exception_type_when_stderr_has_message(
        self, scheduler, caplog, mock_registry, tmp_path
    ):
        alias_name = "test-repo-global"
        error_message = "Index creation failed: out of memory"
        source_path = tmp_path / "repo"
        source_path.mkdir()
        error_text = f"Failed to indexing on source for {alias_name}: {error_message}"
        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=IndexingSubprocessError(error_text),
            ):
                with caplog.at_level(logging.ERROR):
                    with pytest.raises(RuntimeError):
                        scheduler._index_source(alias_name, str(source_path))

        indexing_logs = [
            r
            for r in caplog.records
            if "indexing on source failed for" in r.message and alias_name in r.message
        ]
        assert len(indexing_logs) >= 1, "Expected at least one indexing error log"
        log_message = indexing_logs[0].message
        assert error_message in log_message
        assert alias_name in log_message


class TestTemporalIndexingErrorLogging:
    def test_logs_exception_type_when_stderr_empty(
        self, scheduler, caplog, mock_registry, tmp_path
    ):
        alias_name = "test-repo-global"
        source_path = tmp_path / "repo"
        source_path.mkdir()
        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:test/repo.git",
            "enable_temporal": True,
            "enable_scip": False,
        }
        call_count = [0]

        def popen_side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return 50
            raise IndexingSubprocessError(
                f"Failed to temporal indexing on source for {alias_name}: CalledProcessError: "
            )

        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=popen_side_effect,
            ):
                with caplog.at_level(logging.ERROR):
                    with pytest.raises(RuntimeError):
                        scheduler._index_source(alias_name, str(source_path))

        temporal_logs = [
            r
            for r in caplog.records
            if "temporal indexing on source failed for" in r.message
        ]
        assert len(temporal_logs) >= 1, (
            f"Expected temporal indexing error log. "
            f"Records: {[r.message for r in caplog.records if r.levelname == 'ERROR']}"
        )
        log_message = temporal_logs[0].message
        assert "CalledProcessError" in log_message
        assert alias_name in log_message
        assert temporal_logs[0].exc_info is not None


class TestScipIndexingErrorLogging:
    def test_logs_exception_type_when_stderr_empty(
        self, scheduler, caplog, mock_registry, tmp_path
    ):
        alias_name = "test-repo-global"
        source_path = tmp_path / "repo"
        source_path.mkdir()
        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:test/repo.git",
            "enable_temporal": False,
            "enable_scip": True,
        }
        with patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                return_value=50,
            ):
                with patch("subprocess.run") as mock_run:
                    mock_run.side_effect = subprocess.CalledProcessError(
                        1, "cidx scip generate", stderr=""
                    )
                    with caplog.at_level(logging.ERROR):
                        with pytest.raises(RuntimeError):
                            scheduler._index_source(alias_name, str(source_path))

        scip_logs = [
            r
            for r in caplog.records
            if "SCIP indexing on source failed for" in r.message
        ]
        assert len(scip_logs) >= 1, (
            f"Expected SCIP indexing error log. "
            f"Records: {[r.message for r in caplog.records if r.levelname == 'ERROR']}"
        )
        log_message = scip_logs[0].message
        assert "CalledProcessError" in log_message
        assert alias_name in log_message
        assert scip_logs[0].exc_info is not None


class TestCleanupErrorLogging:
    def test_logs_exception_type_when_cleanup_triggered(
        self, scheduler, caplog, mock_registry
    ):
        alias_name = "test-repo-global"
        with patch.object(scheduler, "_index_source"):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = RuntimeError("")
                with caplog.at_level(logging.ERROR):
                    with pytest.raises(RuntimeError):
                        scheduler._create_new_index(alias_name, "/path/to/repo")

            cleanup_logs = [
                r
                for r in caplog.records
                if "Failed to create snapshot for" in r.message
                and "cleaning up" in r.message
            ]
            assert len(cleanup_logs) >= 1, "Expected at least one cleanup error log"
            log_message = cleanup_logs[0].message
            assert "RuntimeError" in log_message, (
                f"Log must contain 'RuntimeError', got: {log_message}"
            )
            assert alias_name in log_message, (
                f"Log must contain alias '{alias_name}', got: {log_message}"
            )
