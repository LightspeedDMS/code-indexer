"""
Unit tests for DependencyMapTrackingBackend (Story #192, Component 2).

Tests the SQLite CRUD backend for dependency map tracking (singleton row).
Uses real SQLite database (anti-mock methodology).
"""

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.server.storage.sqlite_backends import (
    DependencyMapTrackingBackend,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    """Create temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def backend(db_path: str) -> DependencyMapTrackingBackend:
    """Create backend instance with initialized schema."""
    # Initialize schema (create table directly for testing)
    conn_manager = DatabaseConnectionManager(db_path)
    conn = conn_manager.get_connection()

    # Create schema directly for testing
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dependency_map_tracking (
            id INTEGER PRIMARY KEY,
            last_run TEXT,
            next_run TEXT,
            status TEXT DEFAULT 'pending',
            commit_hashes TEXT,
            error_message TEXT
        )
    """)
    conn.commit()
    conn_manager.close_all()

    return DependencyMapTrackingBackend(db_path)


def test_get_tracking_initializes_singleton_row(backend: DependencyMapTrackingBackend):
    """Test get_tracking initializes singleton row if not exists."""
    tracking = backend.get_tracking()

    assert tracking is not None
    assert tracking["id"] == 1
    assert tracking["last_run"] is None
    assert tracking["next_run"] is None
    assert tracking["status"] == "pending"
    assert tracking["commit_hashes"] is None
    assert tracking["error_message"] is None


def test_get_tracking_returns_existing_row(backend: DependencyMapTrackingBackend):
    """Test get_tracking returns existing singleton row."""
    # Initialize first
    backend.get_tracking()

    # Update the row
    now = datetime.now(timezone.utc).isoformat()
    backend.update_tracking(
        last_run=now,
        status="completed",
        commit_hashes='{"repo1": "abc123"}',
    )

    # Get should return updated row
    tracking = backend.get_tracking()
    assert tracking["last_run"] == now
    assert tracking["status"] == "completed"
    assert tracking["commit_hashes"] == '{"repo1": "abc123"}'


def test_update_tracking_updates_singleton_row(backend: DependencyMapTrackingBackend):
    """Test update_tracking updates the singleton row."""
    # Initialize
    backend.get_tracking()

    # Update
    now = datetime.now(timezone.utc).isoformat()
    next_run = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    commit_hashes = json.dumps({"repo1": "abc123", "repo2": "def456"})

    backend.update_tracking(
        last_run=now,
        next_run=next_run,
        status="completed",
        commit_hashes=commit_hashes,
        error_message=None,
    )

    tracking = backend.get_tracking()
    assert tracking["last_run"] == now
    assert tracking["next_run"] == next_run
    assert tracking["status"] == "completed"
    assert tracking["commit_hashes"] == commit_hashes
    assert tracking["error_message"] is None


def test_update_tracking_partial_update(backend: DependencyMapTrackingBackend):
    """Test update_tracking with partial field updates."""
    # Initialize
    backend.get_tracking()

    # First update
    now = datetime.now(timezone.utc).isoformat()
    backend.update_tracking(
        last_run=now,
        status="running",
    )

    # Partial update (only status)
    backend.update_tracking(status="completed")

    tracking = backend.get_tracking()
    assert tracking["last_run"] == now  # Preserved from first update
    assert tracking["status"] == "completed"  # Updated


def test_update_tracking_stores_error_message(backend: DependencyMapTrackingBackend):
    """Test update_tracking stores error messages for failed analysis."""
    # Initialize
    backend.get_tracking()

    # Update with error
    error_msg = "Pass 2 failed for domain 'auth': Claude CLI timeout"
    backend.update_tracking(
        status="failed",
        error_message=error_msg,
    )

    tracking = backend.get_tracking()
    assert tracking["status"] == "failed"
    assert tracking["error_message"] == error_msg


def test_update_tracking_commit_hashes_json(backend: DependencyMapTrackingBackend):
    """Test update_tracking stores commit_hashes as JSON string."""
    # Initialize
    backend.get_tracking()

    # Update with commit hashes
    commit_hashes = json.dumps({
        "code-indexer": "abc123def456",
        "web-app": "789abc012def",
        "cidx-meta": "local",
    })

    backend.update_tracking(
        status="completed",
        commit_hashes=commit_hashes,
    )

    tracking = backend.get_tracking()
    assert tracking["commit_hashes"] == commit_hashes

    # Verify it's valid JSON
    parsed = json.loads(tracking["commit_hashes"])
    assert parsed["code-indexer"] == "abc123def456"
    assert parsed["web-app"] == "789abc012def"


def test_update_tracking_clears_error_message(backend: DependencyMapTrackingBackend):
    """Test update_tracking can clear error_message by setting to None (FIX 5)."""
    # Initialize
    backend.get_tracking()

    # First update with error
    backend.update_tracking(
        status="failed",
        error_message="Something went wrong",
    )

    tracking = backend.get_tracking()
    assert tracking["error_message"] == "Something went wrong"

    # Second update to clear error (successful run)
    backend.update_tracking(
        status="completed",
        error_message=None,  # Explicitly clear the error
    )

    tracking = backend.get_tracking()
    assert tracking["status"] == "completed"
    assert tracking["error_message"] is None  # Error should be cleared


def test_close_closes_connections(backend: DependencyMapTrackingBackend):
    """Test close method closes database connections."""
    # Perform an operation to ensure connection is created
    backend.get_tracking()

    # Close should not raise
    backend.close()

    # After close, operations should still work (connection manager handles reopening)
    tracking = backend.get_tracking()
    assert tracking is not None
