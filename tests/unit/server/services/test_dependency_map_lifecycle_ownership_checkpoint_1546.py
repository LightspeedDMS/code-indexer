"""DependencyMapService lifecycle pre-flight ownership-loss checkpoint
(Issue #1546 Fix 3, Codex round review).

Codex finding: ``run_full_analysis()``/``run_delta_analysis()`` acquire
the ``cidx-meta`` write lock, then immediately invoke
``LifecycleBatchRunner.run()`` (repair writes to
``cidx-meta/<alias>.md`` files) WITHOUT an ownership checkpoint in
between. If the lock's ownership was lost between acquisition and this
point (DB connection death in DB-backed mode; an external lock-file
eviction in file mode), the repair writes would previously proceed as if
the lock were still legitimately held.

Fixture/mocking boundary mirrors the established, already-merged
``test_dependency_map_lifecycle_gate.py`` exactly (same sanctioned mock
boundary: golden_repos_manager, config_manager, tracking_backend,
analyzer, refresh_scheduler, job_tracker, lifecycle_invoker,
lifecycle_debouncer; LifecycleFleetScanner/LifecycleBatchRunner patched
at their use site).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)
from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.services.job_tracker import JobTracker


@pytest.fixture
def atomic_db_path(tmp_path):
    db = tmp_path / "test_lifecycle_ownership_checkpoint.db"
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
            metadata TEXT,
            actor_username TEXT
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


class TestLifecyclePreFlightOwnershipCheckpoint:
    """Covers both run_full_analysis() and run_delta_analysis(): each
    acquires the cidx-meta write lock, then runs the lifecycle pre-flight
    scan+repair. Ownership must be re-checked between those two steps."""

    @pytest.mark.parametrize(
        "method_name,broken_alias",
        [
            ("run_full_analysis", "alias-a"),
            ("run_delta_analysis", "alias-x"),
        ],
    )
    @patch("code_indexer.server.services.dependency_map_service.LifecycleBatchRunner")
    @patch("code_indexer.server.services.dependency_map_service.LifecycleFleetScanner")
    def test_ownership_loss_aborts_before_lifecycle_repair_writes(
        self,
        scanner_cls,
        runner_cls,
        method_name,
        broken_alias,
        service,
        mock_refresh_scheduler,
    ):
        scanner_cls.return_value.find_broken_or_missing.return_value = [broken_alias]
        mock_refresh_scheduler.raise_if_write_lock_ownership_lost.side_effect = (
            AliasLockOwnershipLostError("ownership lost")
        )

        with pytest.raises(AliasLockOwnershipLostError):
            getattr(service, method_name)()

        runner_cls.return_value.run.assert_not_called()
        mock_refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
