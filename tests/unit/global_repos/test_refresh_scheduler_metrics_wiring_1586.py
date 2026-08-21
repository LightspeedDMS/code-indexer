"""Story #1586 AC4: cidx.repos.refresh.duration OTEL metric wired into
RefreshScheduler._execute_refresh completion.

Proves the WIRING -- a real call into RefreshScheduler._execute_refresh
emits a real cidx.repos.refresh.duration OTEL metric via JobMetrics -- not
just that JobMetrics.record_repository_refresh works standalone (already
covered in tests/unit/server/telemetry/test_job_metrics.py).

Reuses the exact same real-scheduler fixture recipe as the sibling Bug #935
wiring test (test_refresh_scheduler_job_tracker_935.py): a real
RefreshScheduler wired to a real, SQLite-backed JobTracker, with only
alias_manager.read_alias patched to force the cheapest real early-return
paths (success: "alias not found, skipped"; failure: a raised exception) --
MESSI Rule #1: no mocks of the code under test.
"""

import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager

from tests.unit.server.telemetry.otel_test_support import (
    active_job_metrics_singleton,
    find_metric,
)

_BACKGROUND_JOBS_DDL = """
    CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY,
        operation_type TEXT,
        status TEXT,
        created_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER DEFAULT 0,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        cancelled INTEGER DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER DEFAULT 0,
        progress_info TEXT,
        metadata TEXT,
        actor_username TEXT
    )
"""


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / ".code-indexer" / "golden_repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def job_tracker_db(tmp_path):
    """Real, SQLite-backed JobTracker (same recipe as Bug #935's test)."""
    db_path = str(tmp_path / "tracker.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_BACKGROUND_JOBS_DDL)
        conn.commit()
    finally:
        conn.close()

    from code_indexer.server.services.job_tracker import JobTracker

    return JobTracker(db_path)


@pytest.fixture
def scheduler(
    golden_repos_dir, config_mgr, query_tracker, cleanup_manager, job_tracker_db
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        job_tracker=job_tracker_db,
    )


class TestRefreshDurationMetricSuccess:
    def test_execute_refresh_records_duration_metric_on_success(self, scheduler):
        with active_job_metrics_singleton() as (_metrics, reader):
            with patch.object(scheduler.alias_manager, "read_alias", return_value=None):
                result = scheduler._execute_refresh("metrics-ok-repo-global")

            assert result["success"] is True

            metric = find_metric(reader, "cidx.repos.refresh.duration")
            assert metric is not None, "cidx.repos.refresh.duration not emitted"
            dp = list(metric.data.data_points)[0]
            assert dp.attributes["repository"] == "metrics-ok-repo-global"
            assert dp.attributes["status"] == "success"


class TestRefreshDurationMetricFailure:
    def test_execute_refresh_records_duration_metric_on_failure(self, scheduler):
        with active_job_metrics_singleton() as (_metrics, reader):
            with patch.object(
                scheduler.alias_manager,
                "read_alias",
                side_effect=RuntimeError("disk read error"),
            ):
                with pytest.raises(RuntimeError):
                    scheduler._execute_refresh("metrics-fail-repo-global")

            metric = find_metric(reader, "cidx.repos.refresh.duration")
            assert metric is not None, "cidx.repos.refresh.duration not emitted"
            dp = list(metric.data.data_points)[0]
            assert dp.attributes["repository"] == "metrics-fail-repo-global"
            assert dp.attributes["status"] == "error"


@pytest.fixture
def repair_failure_scheduler(tmp_path):
    """Real RefreshScheduler + a real on-disk local repo whose
    .code-indexer/ directory exists but has no config.json (Bug #1253's
    repair-failure trigger condition). registry is a data-only MagicMock
    stand-in for the DB-backed registry; the alias pointer is published via
    the scheduler's own real alias_manager.create_alias() API -- no mocking
    of alias resolution. Yields (scheduler, alias_name).
    """
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True)

    mock_registry = MagicMock()
    mock_registry.list_global_repos.return_value = []
    mock_registry.update_refresh_timestamp = MagicMock()

    alias_name = "langfuse-user-global"
    source_dir = golden_repos_dir / "langfuse-user"
    source_dir.mkdir(parents=True)
    (source_dir / ".code-indexer").mkdir()  # exists, but no config.json

    mock_registry.get_global_repo.return_value = {
        "alias_name": alias_name,
        "repo_url": "local://langfuse-user",
        "enable_temporal": False,
        "enable_scip": False,
    }

    mock_config_source = MagicMock()
    mock_config_source.get_global_refresh_interval.return_value = 3600

    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
    )
    scheduler.alias_manager.create_alias(alias_name, str(source_dir))
    return scheduler, alias_name


class TestRefreshDurationMetricSuccessFalseReturn:
    """Story #1586 Finding 2: a real 'success': False RETURN (not a raised
    exception) -- e.g. the Bug #1253 local-repo repair-failure path -- must
    record status='error' on cidx.repos.refresh.duration, not 'success'.
    Only the external subprocess boundary (subprocess.run) is patched --
    MESSI Rule #1: no mocks of the code under test itself.
    """

    def test_local_repo_repair_failure_records_error_status(
        self, repair_failure_scheduler
    ):
        scheduler, alias_name = repair_failure_scheduler

        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, output="", stderr="disk full"
            )

        with active_job_metrics_singleton() as (_metrics, reader):
            with patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                side_effect=fake_run,
            ):
                result = scheduler._execute_refresh(alias_name)

            assert result["success"] is False, (
                f"repair failure must surface as a real success=False "
                f"RETURN, not an exception. Got: {result}"
            )

            metric = find_metric(reader, "cidx.repos.refresh.duration")
            assert metric is not None, "cidx.repos.refresh.duration not emitted"
            dp = list(metric.data.data_points)[0]
            assert dp.attributes["status"] == "error", (
                "a success=False RETURN (not a raised exception) must "
                "record status='error', not 'success' (Story #1586 Finding 2)"
            )
