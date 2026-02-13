"""
Unit tests for DescriptionRefreshTrackingBackend (Story #190, Component 1).

Tests the SQLite CRUD backend for description refresh tracking records.
Uses real SQLite database (anti-mock methodology).
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Create temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def backend(db_path: str) -> DescriptionRefreshTrackingBackend:
    """Create backend instance with initialized schema."""
    # Initialize schema (assumes schema already created by DatabaseManager)
    conn_manager = DatabaseConnectionManager(db_path)
    conn = conn_manager.get_connection()

    # Create schema directly for testing
    conn.execute("""
        CREATE TABLE IF NOT EXISTS description_refresh_tracking (
            repo_alias TEXT PRIMARY KEY,
            last_run TEXT,
            next_run TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            last_known_commit TEXT,
            last_known_files_processed INTEGER,
            last_known_indexed_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn_manager.close_all()

    return DescriptionRefreshTrackingBackend(db_path)


def test_upsert_tracking_insert_new_record(backend: DescriptionRefreshTrackingBackend):
    """Test upserting a new tracking record."""
    now = datetime.now(timezone.utc).isoformat()
    next_run = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    backend.upsert_tracking(
        repo_alias="test-repo",
        next_run=next_run,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["repo_alias"] == "test-repo"
    assert record["next_run"] == next_run
    assert record["status"] == "pending"
    assert record["last_run"] is None
    assert record["error"] is None


def test_upsert_tracking_update_existing_record(
    backend: DescriptionRefreshTrackingBackend,
):
    """Test upserting updates an existing tracking record."""
    now = datetime.now(timezone.utc).isoformat()
    next_run = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    # Insert initial record
    backend.upsert_tracking(
        repo_alias="test-repo",
        next_run=next_run,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    # Update with new status
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    backend.upsert_tracking(
        repo_alias="test-repo",
        status="completed",
        last_run=later,
        updated_at=later,
    )

    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["status"] == "completed"
    assert record["last_run"] == later
    assert record["updated_at"] == later


def test_get_tracking_record_nonexistent(backend: DescriptionRefreshTrackingBackend):
    """Test getting a nonexistent tracking record returns None."""
    record = backend.get_tracking_record("nonexistent-repo")
    assert record is None


def test_get_stale_repos_returns_due_repos(backend: DescriptionRefreshTrackingBackend):
    """Test get_stale_repos returns repos where next_run <= now and status != queued."""
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()
    now_iso = now.isoformat()

    # Repo due for refresh (next_run in past, status=pending)
    backend.upsert_tracking(
        repo_alias="stale-repo",
        next_run=past,
        status="pending",
        created_at=past,
        updated_at=past,
    )

    # Repo not due yet (next_run in future)
    backend.upsert_tracking(
        repo_alias="future-repo",
        next_run=future,
        status="pending",
        created_at=now_iso,
        updated_at=now_iso,
    )

    # Repo due but already queued (should be excluded)
    backend.upsert_tracking(
        repo_alias="queued-repo",
        next_run=past,
        status="queued",
        created_at=past,
        updated_at=past,
    )

    stale_repos = backend.get_stale_repos(now_iso)

    assert len(stale_repos) == 1
    assert stale_repos[0]["repo_alias"] == "stale-repo"


def test_get_stale_repos_exact_boundary(backend: DescriptionRefreshTrackingBackend):
    """Test get_stale_repos includes repos where next_run equals now (boundary condition)."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # Repo with next_run exactly equal to now
    backend.upsert_tracking(
        repo_alias="boundary-repo",
        next_run=now_iso,
        status="pending",
        created_at=now_iso,
        updated_at=now_iso,
    )

    stale_repos = backend.get_stale_repos(now_iso)

    assert len(stale_repos) == 1
    assert stale_repos[0]["repo_alias"] == "boundary-repo"


def test_delete_tracking_removes_record(backend: DescriptionRefreshTrackingBackend):
    """Test deleting a tracking record."""
    now = datetime.now(timezone.utc).isoformat()

    backend.upsert_tracking(
        repo_alias="test-repo",
        next_run=now,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    deleted = backend.delete_tracking("test-repo")
    assert deleted is True

    record = backend.get_tracking_record("test-repo")
    assert record is None


def test_delete_tracking_nonexistent_returns_false(
    backend: DescriptionRefreshTrackingBackend,
):
    """Test deleting a nonexistent record returns False."""
    deleted = backend.delete_tracking("nonexistent-repo")
    assert deleted is False


def test_get_all_tracking_returns_all_records(
    backend: DescriptionRefreshTrackingBackend,
):
    """Test get_all_tracking returns all tracking records."""
    now = datetime.now(timezone.utc).isoformat()

    backend.upsert_tracking(
        repo_alias="repo1",
        next_run=now,
        status="pending",
        created_at=now,
        updated_at=now,
    )
    backend.upsert_tracking(
        repo_alias="repo2",
        next_run=now,
        status="completed",
        created_at=now,
        updated_at=now,
    )

    all_records = backend.get_all_tracking()

    assert len(all_records) == 2
    aliases = {r["repo_alias"] for r in all_records}
    assert aliases == {"repo1", "repo2"}


def test_upsert_tracking_preserves_change_markers(
    backend: DescriptionRefreshTrackingBackend,
):
    """Test upserting preserves change markers (last_known_commit, etc.)."""
    now = datetime.now(timezone.utc).isoformat()
    next_run = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()

    backend.upsert_tracking(
        repo_alias="test-repo",
        next_run=next_run,
        status="completed",
        last_known_commit="abc123",
        last_known_files_processed=100,
        last_known_indexed_at=now,
        created_at=now,
        updated_at=now,
    )

    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["last_known_commit"] == "abc123"
    assert record["last_known_files_processed"] == 100
    assert record["last_known_indexed_at"] == now


def test_upsert_tracking_stores_error_message(
    backend: DescriptionRefreshTrackingBackend,
):
    """Test upserting stores error messages for failed refreshes."""
    now = datetime.now(timezone.utc).isoformat()
    error_msg = "Claude CLI timeout after 60s"

    backend.upsert_tracking(
        repo_alias="test-repo",
        status="failed",
        error=error_msg,
        updated_at=now,
        created_at=now,
        next_run=now,
    )

    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["status"] == "failed"
    assert record["error"] == error_msg


def test_close_closes_connections(backend: DescriptionRefreshTrackingBackend):
    """Test close method closes database connections."""
    # Perform an operation to ensure connection is created
    now = datetime.now(timezone.utc).isoformat()
    backend.upsert_tracking(
        repo_alias="test-repo",
        next_run=now,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    # Close should not raise
    backend.close()

    # After close, operations should still work (connection manager handles reopening)
    record = backend.get_tracking_record("test-repo")
    assert record is not None
