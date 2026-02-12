"""
Unit tests for RefreshScheduler error logging quality.

Bug #171: Verify that error logs include exception type names, repository aliases,
and stack traces even when exception messages are empty.

Tests ensure all error logging statements in refresh_scheduler.py follow the pattern:
    logger.error(f"Operation failed for {alias}: {type(e).__name__}: {e}", exc_info=True)
"""

import logging
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


@pytest.fixture
def mock_golden_repos_dir(tmp_path):
    """Create temporary golden repos directory."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir()
    return str(golden_dir)


@pytest.fixture
def mock_query_tracker():
    """Create mock QueryTracker."""
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    """Create mock CleanupManager."""
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    """Create mock config source."""
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    """Create mock registry."""
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
    """Create RefreshScheduler instance with mocked dependencies."""
    return RefreshScheduler(
        golden_repos_dir=mock_golden_repos_dir,
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


class TestSemanticFtsIndexingErrorLogging:
    """Test error logging when semantic+FTS indexing fails."""

    def test_logs_exception_type_when_stderr_empty(
        self, scheduler, caplog, mock_registry
    ):
        """
        GIVEN a CalledProcessError with empty stderr during semantic+FTS indexing
        WHEN the error is logged
        THEN the log contains the exception type name (CalledProcessError)
        AND the log contains the repository alias
        AND exc_info=True is used (stack trace captured)
        """
        alias_name = "test-repo-global"

        # Setup mocks
        with patch.object(scheduler.alias_manager, "read_alias", return_value="/path/to/repo"):
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    with patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_updater_cls:
                        mock_updater = Mock()
                        mock_updater.has_changes.return_value = True
                        mock_updater.get_source_path.return_value = "/path/to/repo"
                        mock_updater_cls.return_value = mock_updater

                        with patch("subprocess.run") as mock_run:
                            # CoW clone succeeds, then cidx index fails with empty stderr
                            mock_run.side_effect = [
                                Mock(returncode=0),  # cp --reflink
                                Mock(returncode=0),  # git update-index
                                Mock(returncode=0),  # git restore
                                Mock(returncode=0),  # cidx fix-config
                                # cidx index fails with empty stderr
                                subprocess.CalledProcessError(1, "cidx index", stderr=""),
                            ]

                            # Mock index directory validation to skip it
                            with patch("pathlib.Path.exists", return_value=True):
                                with caplog.at_level(logging.ERROR):
                                    with pytest.raises(RuntimeError):
                                        scheduler._create_new_index(alias_name, "/path/to/repo")

                            # Verify log contains exception type
                            error_logs = [r for r in caplog.records if r.levelname == "ERROR"]
                            assert len(error_logs) > 0, "Expected at least one ERROR log"

                            # Find the semantic+FTS indexing error log (first occurrence, not cleanup)
                            indexing_logs = [
                                r for r in error_logs
                                if "Indexing (semantic+FTS) failed for" in r.message
                            ]
                            assert len(indexing_logs) >= 1, f"Expected at least one semantic+FTS indexing error log, got {len(indexing_logs)}"

                            log_message = indexing_logs[0].message
                            assert "CalledProcessError" in log_message, \
                                f"Log must contain exception type 'CalledProcessError', got: {log_message}"
                            assert alias_name in log_message, \
                                f"Log must contain repository alias '{alias_name}', got: {log_message}"

                            # Verify exc_info=True was used (stack trace captured)
                            assert indexing_logs[0].exc_info is not None, \
                                "exc_info must be True to capture stack trace"

    def test_logs_exception_type_when_stderr_has_message(
        self, scheduler, caplog, mock_registry
    ):
        """
        GIVEN a CalledProcessError with non-empty stderr during semantic+FTS indexing
        WHEN the error is logged
        THEN the log contains both the exception type name and the stderr message
        AND the log contains the repository alias
        """
        alias_name = "test-repo-global"
        error_message = "Index creation failed: out of memory"

        with patch.object(scheduler.alias_manager, "read_alias", return_value="/path/to/repo"):
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    with patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_updater_cls:
                        mock_updater = Mock()
                        mock_updater.has_changes.return_value = True
                        mock_updater.get_source_path.return_value = "/path/to/repo"
                        mock_updater_cls.return_value = mock_updater

                        with patch("subprocess.run") as mock_run:
                            mock_run.side_effect = [
                                Mock(returncode=0),  # cp --reflink
                                Mock(returncode=0),  # git update-index
                                Mock(returncode=0),  # git restore
                                Mock(returncode=0),  # cidx fix-config
                                subprocess.CalledProcessError(1, "cidx index", stderr=error_message),
                            ]

                            # Mock index directory validation to skip it
                            with patch("pathlib.Path.exists", return_value=True):
                                with caplog.at_level(logging.ERROR):
                                    with pytest.raises(RuntimeError):
                                        scheduler._create_new_index(alias_name, "/path/to/repo")

                            indexing_logs = [
                                r for r in caplog.records
                                if "Indexing (semantic+FTS) failed for" in r.message
                            ]
                            assert len(indexing_logs) >= 1, f"Expected at least one semantic+FTS indexing error log"

                            log_message = indexing_logs[0].message
                            assert "CalledProcessError" in log_message
                            assert error_message in log_message
                            assert alias_name in log_message


class TestTemporalIndexingErrorLogging:
    """Test error logging when temporal indexing fails."""

    def test_logs_exception_type_when_stderr_empty(
        self, scheduler, caplog, mock_registry
    ):
        """
        GIVEN a CalledProcessError with empty stderr during temporal indexing
        WHEN the error is logged
        THEN the log contains the exception type name
        AND the repository alias
        AND exc_info=True is used
        """
        alias_name = "test-repo-global"

        # Enable temporal indexing
        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:test/repo.git",
            "enable_temporal": True,
            "enable_scip": False,
        }

        with patch.object(scheduler.alias_manager, "read_alias", return_value="/path/to/repo"):
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    with patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_updater_cls:
                        mock_updater = Mock()
                        mock_updater.has_changes.return_value = True
                        mock_updater.get_source_path.return_value = "/path/to/repo"
                        mock_updater_cls.return_value = mock_updater

                        with patch("subprocess.run") as mock_run:
                            mock_run.side_effect = [
                                Mock(returncode=0),  # cp --reflink
                                Mock(returncode=0),  # git update-index
                                Mock(returncode=0),  # git restore
                                Mock(returncode=0),  # cidx fix-config
                                Mock(returncode=0),  # cidx index (semantic+FTS)
                                # Temporal indexing fails with empty stderr
                                subprocess.CalledProcessError(1, "cidx index --index-commits", stderr=""),
                            ]

                            # Mock index directory validation to skip it
                            with patch("pathlib.Path.exists", return_value=True):
                                with caplog.at_level(logging.ERROR):
                                    with pytest.raises(RuntimeError):
                                        scheduler._create_new_index(alias_name, "/path/to/repo")

                            temporal_logs = [
                                r for r in caplog.records
                                if "Temporal indexing failed for" in r.message
                            ]
                            assert len(temporal_logs) >= 1, f"Expected at least one temporal indexing error log"

                            log_message = temporal_logs[0].message
                            assert "CalledProcessError" in log_message
                            assert alias_name in log_message
                            assert temporal_logs[0].exc_info is not None


class TestScipIndexingErrorLogging:
    """Test error logging when SCIP indexing fails."""

    def test_logs_exception_type_when_stderr_empty(
        self, scheduler, caplog, mock_registry
    ):
        """
        GIVEN a CalledProcessError with empty stderr during SCIP indexing
        WHEN the error is logged
        THEN the log contains the exception type name
        AND the repository alias
        AND exc_info=True is used
        """
        alias_name = "test-repo-global"

        # Enable SCIP indexing
        mock_registry.get_global_repo.return_value = {
            "alias_name": alias_name,
            "repo_url": "git@github.com:test/repo.git",
            "enable_temporal": False,
            "enable_scip": True,
        }

        with patch.object(scheduler.alias_manager, "read_alias", return_value="/path/to/repo"):
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    with patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_updater_cls:
                        mock_updater = Mock()
                        mock_updater.has_changes.return_value = True
                        mock_updater.get_source_path.return_value = "/path/to/repo"
                        mock_updater_cls.return_value = mock_updater

                        with patch("subprocess.run") as mock_run:
                            mock_run.side_effect = [
                                Mock(returncode=0),  # cp --reflink
                                Mock(returncode=0),  # git update-index
                                Mock(returncode=0),  # git restore
                                Mock(returncode=0),  # cidx fix-config
                                Mock(returncode=0),  # cidx index (semantic+FTS)
                                # SCIP indexing fails with empty stderr
                                subprocess.CalledProcessError(1, "cidx scip generate", stderr=""),
                            ]

                            # Mock index directory validation to skip it
                            with patch("pathlib.Path.exists", return_value=True):
                                with caplog.at_level(logging.ERROR):
                                    with pytest.raises(RuntimeError):
                                        scheduler._create_new_index(alias_name, "/path/to/repo")

                            scip_logs = [
                                r for r in caplog.records
                                if "SCIP indexing failed for" in r.message
                            ]
                            assert len(scip_logs) >= 1, f"Expected at least one SCIP indexing error log"

                            log_message = scip_logs[0].message
                            assert "CalledProcessError" in log_message
                            assert alias_name in log_message
                            assert scip_logs[0].exc_info is not None


class TestCleanupErrorLogging:
    """Test error logging during cleanup of failed index creation."""

    def test_logs_exception_type_when_cleanup_triggered(
        self, scheduler, caplog, mock_registry
    ):
        """
        GIVEN an exception during index creation that triggers cleanup
        WHEN the cleanup error is logged
        THEN the log contains the exception type name
        AND exc_info=True is used
        """
        alias_name = "test-repo-global"

        with patch.object(scheduler.alias_manager, "read_alias", return_value="/path/to/repo"):
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    with patch("code_indexer.global_repos.refresh_scheduler.GitPullUpdater") as mock_updater_cls:
                        mock_updater = Mock()
                        mock_updater.has_changes.return_value = True
                        mock_updater.get_source_path.return_value = "/path/to/repo"
                        mock_updater_cls.return_value = mock_updater

                        with patch("subprocess.run") as mock_run:
                            # Simulate RuntimeError with empty message (wrapped exception)
                            mock_run.side_effect = RuntimeError("")

                            with caplog.at_level(logging.ERROR):
                                with pytest.raises(RuntimeError):
                                    scheduler._create_new_index(alias_name, "/path/to/repo")

                            # Find cleanup error log
                            cleanup_logs = [
                                r for r in caplog.records
                                if "Failed to create new index for" in r.message and "cleaning up" in r.message
                            ]
                            assert len(cleanup_logs) >= 1, f"Expected at least one cleanup error log"

                            log_message = cleanup_logs[0].message
                            # Should contain exception type even if message is empty
                            assert "RuntimeError" in log_message, \
                                f"Log must contain exception type 'RuntimeError', got: {log_message}"
                            assert alias_name in log_message, \
                                f"Log must contain repository alias '{alias_name}', got: {log_message}"
