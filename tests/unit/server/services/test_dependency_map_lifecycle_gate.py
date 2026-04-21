"""
Tests for Story #876 Phase B-1 Deliverables 2 and 3.

Verifies two wiring points in DependencyMapService:

  Deliverable 2: run_full_analysis
  Deliverable 3: run_delta_analysis

Each wiring point must:
  1. Replace the two-call TOCTOU pattern (check_operation_conflict +
     register_job) with a single atomic register_job_if_no_conflict call
     that re-raises DuplicateJobError on conflict.
  2. Before doing any dependency-map work, run a lifecycle pre-flight:
       a. LifecycleFleetScanner.find_broken_or_missing() -> list of aliases
       b. If non-empty, LifecycleBatchRunner.run(aliases, parent_job_id=...)
     When the scanner returns [], no runner is constructed.

Mock boundaries (per Story #876 prompt):
  * golden_repos_manager, config_manager, tracking_backend, analyzer,
    refresh_scheduler, job_tracker, description_refresh_tracking_backend,
    lifecycle_invoker are the LEGITIMATE mock boundaries.
  * LifecycleFleetScanner and LifecycleBatchRunner are patched at their USE
    site inside dependency_map_service so the test verifies the wiring
    contract without relying on their real constructors.
  * No private service methods (_setup_analysis, _execute_analysis_passes,
    etc.) are patched — when analysis fails downstream of the gate, that is
    acceptable; the test only asserts gate + pre-flight behavior.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.job_tracker import JobTracker, DuplicateJobError


# ---------------------------------------------------------------------------
# DB fixture — creates background_jobs table with the partial unique index
# that enforces single-active-job-per-(operation_type, repo_alias), matching
# the real production schema so DuplicateJobError fires on conflict.
# ---------------------------------------------------------------------------


@pytest.fixture
def atomic_db_path(tmp_path):
    db = tmp_path / "test_lifecycle_gate.db"
    with closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS background_jobs (
            job_id TEXT PRIMARY KEY NOT NULL,
            operation_type TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            result TEXT,
            error TEXT,
            progress INTEGER NOT NULL DEFAULT 0,
            username TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0,
            cancelled INTEGER NOT NULL DEFAULT 0,
            repo_alias TEXT,
            resolution_attempts INTEGER NOT NULL DEFAULT 0,
            claude_actions TEXT,
            failure_reason TEXT,
            extended_error TEXT,
            language_resolution_status TEXT,
            progress_info TEXT,
            metadata TEXT
        )"""
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
            ON background_jobs(operation_type, repo_alias)
            WHERE status IN ('pending', 'running')
              AND repo_alias IS NOT NULL
            """
        )
        conn.commit()
    return str(db)


@pytest.fixture
def real_job_tracker(atomic_db_path):
    return JobTracker(atomic_db_path)


# ---------------------------------------------------------------------------
# Collaborator fixtures — everything else is a MagicMock per the prompt's
# sanctioned mock boundaries.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_golden_repos_manager(tmp_path):
    m = MagicMock()
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir()
    m.golden_repos_dir = str(golden_dir)
    m.list_golden_repos.return_value = []
    return m


@pytest.fixture
def mock_config_manager():
    m = MagicMock()
    # Force early return from _setup_analysis so run_full_analysis
    # exits cleanly after the gate + pre-flight and we can assert on them.
    cfg = MagicMock()
    cfg.dependency_map_enabled = False
    m.get_claude_integration_config.return_value = cfg
    return m


@pytest.fixture
def mock_tracking_backend():
    return MagicMock()


@pytest.fixture
def mock_analyzer():
    return MagicMock()


@pytest.fixture
def mock_refresh_scheduler():
    m = MagicMock()
    m.acquire_write_lock.return_value = True
    return m


@pytest.fixture
def mock_lifecycle_invoker():
    return MagicMock()


@pytest.fixture
def mock_lifecycle_debouncer():
    # CidxMetaRefreshDebouncer surrogate — pre-flight gate requires non-None.
    return MagicMock()


@pytest.fixture
def service(
    mock_golden_repos_manager,
    mock_config_manager,
    mock_tracking_backend,
    mock_analyzer,
    mock_refresh_scheduler,
    real_job_tracker,
    mock_lifecycle_invoker,
    mock_lifecycle_debouncer,
):
    return DependencyMapService(
        golden_repos_manager=mock_golden_repos_manager,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking_backend,
        analyzer=mock_analyzer,
        refresh_scheduler=mock_refresh_scheduler,
        job_tracker=real_job_tracker,
        lifecycle_invoker=mock_lifecycle_invoker,
        lifecycle_debouncer=mock_lifecycle_debouncer,
    )


# ---------------------------------------------------------------------------
# Deliverable 2 — run_full_analysis lifecycle gate
# ---------------------------------------------------------------------------


class TestRunFullAnalysisLifecycleGate:
    """
    Story #876 Phase B-1 Deliverable 2 — run_full_analysis must run the
    atomic lifecycle gate plus fleet pre-flight before touching the
    dependency-map lock.
    """

    @patch("code_indexer.server.services.dependency_map_service.LifecycleBatchRunner")
    @patch("code_indexer.server.services.dependency_map_service.LifecycleFleetScanner")
    def test_run_full_analysis_repairs_broken_aliases_before_dep_map_work(
        self, scanner_cls, runner_cls, service
    ):
        scanner_cls.return_value.find_broken_or_missing.return_value = [
            "alias-a",
            "alias-b",
        ]
        runner_cls.return_value.run = MagicMock()

        service.run_full_analysis()

        runner_cls.return_value.run.assert_called_once()
        args, kwargs = runner_cls.return_value.run.call_args
        # First positional arg (or 'repo_aliases' kwarg) is the alias list
        aliases = args[0] if args else kwargs["repo_aliases"]
        assert aliases == ["alias-a", "alias-b"]
        # parent_job_id is the tracked job id this run registered
        assert kwargs["parent_job_id"].startswith("dep-map-full-")

    @patch("code_indexer.server.services.dependency_map_service.LifecycleBatchRunner")
    @patch("code_indexer.server.services.dependency_map_service.LifecycleFleetScanner")
    def test_run_full_analysis_skips_pre_flight_when_nothing_broken(
        self, scanner_cls, runner_cls, service
    ):
        scanner_cls.return_value.find_broken_or_missing.return_value = []

        service.run_full_analysis()

        runner_cls.assert_not_called()

    @patch("code_indexer.server.services.dependency_map_service.LifecycleBatchRunner")
    @patch("code_indexer.server.services.dependency_map_service.LifecycleFleetScanner")
    def test_run_full_analysis_propagates_duplicate_job_error(
        self, scanner_cls, runner_cls, service, real_job_tracker
    ):
        # Seed the DB with a conflicting active row so the atomic INSERT fails.
        real_job_tracker.register_job_if_no_conflict(
            job_id="preexisting-full",
            operation_type="dependency_map_full",
            username="system",
            repo_alias="server",
        )

        with pytest.raises(DuplicateJobError):
            service.run_full_analysis()

        runner_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Deliverable 3 — run_delta_analysis lifecycle gate
# ---------------------------------------------------------------------------


class TestRunDeltaAnalysisLifecycleGate:
    """
    Story #876 Phase B-1 Deliverable 3 — run_delta_analysis must run the
    same atomic lifecycle gate plus fleet pre-flight before touching the
    dependency-map lock.
    """

    @patch("code_indexer.server.services.dependency_map_service.LifecycleBatchRunner")
    @patch("code_indexer.server.services.dependency_map_service.LifecycleFleetScanner")
    def test_run_delta_analysis_repairs_broken_aliases_before_dep_map_work(
        self, scanner_cls, runner_cls, service
    ):
        scanner_cls.return_value.find_broken_or_missing.return_value = [
            "alias-x",
            "alias-y",
        ]
        runner_cls.return_value.run = MagicMock()

        service.run_delta_analysis()

        runner_cls.return_value.run.assert_called_once()
        args, kwargs = runner_cls.return_value.run.call_args
        aliases = args[0] if args else kwargs["repo_aliases"]
        assert aliases == ["alias-x", "alias-y"]
        assert kwargs["parent_job_id"].startswith("dep-map-delta-")

    @patch("code_indexer.server.services.dependency_map_service.LifecycleBatchRunner")
    @patch("code_indexer.server.services.dependency_map_service.LifecycleFleetScanner")
    def test_run_delta_analysis_skips_pre_flight_when_nothing_broken(
        self, scanner_cls, runner_cls, service
    ):
        scanner_cls.return_value.find_broken_or_missing.return_value = []

        service.run_delta_analysis()

        runner_cls.assert_not_called()

    @patch("code_indexer.server.services.dependency_map_service.LifecycleBatchRunner")
    @patch("code_indexer.server.services.dependency_map_service.LifecycleFleetScanner")
    def test_run_delta_analysis_propagates_duplicate_job_error(
        self, scanner_cls, runner_cls, service, real_job_tracker
    ):
        real_job_tracker.register_job_if_no_conflict(
            job_id="preexisting-delta",
            operation_type="dependency_map_delta",
            username="system",
            repo_alias="server",
        )

        with pytest.raises(DuplicateJobError):
            service.run_delta_analysis()

        runner_cls.assert_not_called()
