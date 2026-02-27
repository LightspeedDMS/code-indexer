"""
Shared fixtures for Story 2 dependency map tracking tests.

Epic #261 Story 2 (#312): Dependency Map Analysis Job Tracking.
"""

import sqlite3
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.job_tracker import JobTracker


@pytest.fixture
def db_path(tmp_path):
    """Temporary SQLite database with background_jobs schema."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
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
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def job_tracker(db_path):
    """Real JobTracker connected to temp database."""
    return JobTracker(db_path)


@pytest.fixture
def mock_tracking_backend():
    """Mock DependencyMapTrackingBackend."""
    backend = MagicMock()
    backend.get_tracking.return_value = {}
    backend.update_tracking.return_value = None
    return backend


@pytest.fixture
def mock_config_manager():
    """Mock ConfigManager returning dependency map enabled."""
    config = MagicMock()
    config.dependency_map_enabled = True
    config.dependency_map_pass1_max_turns = 10
    config.dependency_map_pass2_max_turns = 10
    config.dependency_map_interval_hours = 24
    config.dependency_map_pass_timeout_seconds = 300

    manager = MagicMock()
    manager.get_claude_integration_config.return_value = config
    return manager


@pytest.fixture
def mock_config_manager_disabled():
    """Mock ConfigManager with dependency map disabled."""
    config = MagicMock()
    config.dependency_map_enabled = False

    manager = MagicMock()
    manager.get_claude_integration_config.return_value = config
    return manager


@pytest.fixture
def mock_golden_repos_manager(tmp_path):
    """Mock GoldenRepoManager."""
    manager = MagicMock()
    manager.golden_repos_dir = str(tmp_path / "golden-repos")
    (tmp_path / "golden-repos").mkdir(parents=True)
    return manager


@pytest.fixture
def mock_analyzer():
    """Mock DependencyMapAnalyzer that completes quickly."""
    analyzer = MagicMock()
    analyzer.generate_claude_md.return_value = None
    analyzer.run_pass_1_synthesis.return_value = [{"name": "domain-alpha"}]
    analyzer.run_pass_2_per_domain.return_value = None
    analyzer._reconcile_domains_json.side_effect = (
        lambda staging_dir, domain_list: domain_list
    )
    analyzer._generate_index_md.return_value = None
    return analyzer


def make_service(
    golden_repos_manager,
    config_manager,
    tracking_backend,
    analyzer,
    job_tracker=None,
    refresh_scheduler=None,
):
    """Helper to create DependencyMapService with all dependencies."""
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        refresh_scheduler=refresh_scheduler,
        job_tracker=job_tracker,
    )
