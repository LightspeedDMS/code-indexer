"""
Shared fixtures for Story #314: Remaining Untracked Operations Migration.

Provides real SQLite database and real JobTracker for all tests in this package.
No mocking of the database layer - uses real SQLite for authentic testing.
"""

import sqlite3
import pytest

from code_indexer.server.services.job_tracker import JobTracker


@pytest.fixture
def db_path(tmp_path):
    """Temporary SQLite database with background_jobs schema matching production."""
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
