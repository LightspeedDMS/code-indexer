"""
Unit tests for DescriptionRefreshTrackingBackend.upsert_tracking() (Story #190).

Tests verify that upsert_tracking() preserves untouched columns during partial updates.
"""

from datetime import datetime, timezone

import pytest

from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.server.storage.sqlite_backends import DescriptionRefreshTrackingBackend


@pytest.fixture
def db_path(tmp_path):
    """Create temporary database with initialized schema."""
    db_file = tmp_path / "test.db"
    conn_manager = DatabaseConnectionManager(str(db_file))
    conn = conn_manager.get_connection()

    # Create schema
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

    return str(db_file)


@pytest.fixture
def backend(db_path):
    """Create backend instance."""
    return DescriptionRefreshTrackingBackend(db_path)


def test_upsert_tracking_preserves_untouched_fields(backend, db_path):
    """Test that upsert_tracking preserves fields not included in update."""
    now = datetime.now(timezone.utc).isoformat()
    past = "2024-01-01T00:00:00+00:00"

    # Create initial record with all fields populated
    backend.upsert_tracking(
        repo_alias="test-repo",
        last_run=past,
        next_run=now,
        status="completed",
        error=None,
        last_known_commit="abc123",
        last_known_files_processed=100,
        last_known_indexed_at=past,
        created_at=past,
        updated_at=past,
    )

    # Verify initial state
    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["last_known_commit"] == "abc123"
    assert record["last_known_files_processed"] == 100
    assert record["last_known_indexed_at"] == past
    assert record["status"] == "completed"
    assert record["last_run"] == past

    # Partial update: only next_run and updated_at
    later = "2024-12-31T23:59:59+00:00"
    backend.upsert_tracking(
        repo_alias="test-repo",
        next_run=later,
        updated_at=now,
    )

    # Verify untouched fields are preserved
    updated_record = backend.get_tracking_record("test-repo")
    assert updated_record is not None
    assert updated_record["next_run"] == later  # Updated
    assert updated_record["updated_at"] == now  # Updated
    # All untouched fields should be preserved
    assert updated_record["last_known_commit"] == "abc123"
    assert updated_record["last_known_files_processed"] == 100
    assert updated_record["last_known_indexed_at"] == past
    assert updated_record["status"] == "completed"
    assert updated_record["last_run"] == past
    assert updated_record["error"] is None
    assert updated_record["created_at"] == past


def test_upsert_tracking_preserves_status_on_reschedule(backend, db_path):
    """Test that rescheduling preserves status and error fields."""
    now = datetime.now(timezone.utc).isoformat()

    # Create record with error
    backend.upsert_tracking(
        repo_alias="failed-repo",
        status="failed",
        error="Claude CLI timeout",
        last_run=now,
        next_run=now,
        created_at=now,
        updated_at=now,
    )

    # Reschedule (only update next_run)
    later = "2024-12-31T23:59:59+00:00"
    backend.upsert_tracking(
        repo_alias="failed-repo",
        next_run=later,
    )

    # Verify status and error are preserved
    record = backend.get_tracking_record("failed-repo")
    assert record is not None
    assert record["status"] == "failed"  # Preserved
    assert record["error"] == "Claude CLI timeout"  # Preserved
    assert record["next_run"] == later  # Updated


def test_upsert_tracking_updates_all_specified_fields(backend, db_path):
    """Test that upsert_tracking updates all fields specified in kwargs."""
    now = datetime.now(timezone.utc).isoformat()

    # Create initial record
    backend.upsert_tracking(
        repo_alias="test-repo",
        status="pending",
        created_at=now,
    )

    # Update multiple fields
    backend.upsert_tracking(
        repo_alias="test-repo",
        status="completed",
        last_run=now,
        last_known_commit="xyz789",
    )

    # Verify all specified fields updated
    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["status"] == "completed"
    assert record["last_run"] == now
    assert record["last_known_commit"] == "xyz789"


def test_upsert_tracking_creates_new_record_if_not_exists(backend, db_path):
    """Test that upsert_tracking creates new record if repo_alias doesn't exist."""
    now = datetime.now(timezone.utc).isoformat()

    # Verify record doesn't exist
    assert backend.get_tracking_record("new-repo") is None

    # Upsert creates new record
    backend.upsert_tracking(
        repo_alias="new-repo",
        status="pending",
        next_run=now,
        created_at=now,
    )

    # Verify record created
    record = backend.get_tracking_record("new-repo")
    assert record is not None
    assert record["status"] == "pending"
    assert record["next_run"] == now


def test_upsert_tracking_ignores_invalid_fields(backend, db_path):
    """Test that upsert_tracking ignores fields not in valid_fields set."""
    now = datetime.now(timezone.utc).isoformat()

    # Create record with invalid field
    backend.upsert_tracking(
        repo_alias="test-repo",
        status="pending",
        invalid_field="should_be_ignored",  # Not in valid_fields
        created_at=now,
    )

    # Verify only valid fields stored
    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["status"] == "pending"
    # No error should be raised for invalid field


def test_upsert_tracking_handles_null_values(backend, db_path):
    """Test that upsert_tracking can set fields to NULL."""
    now = datetime.now(timezone.utc).isoformat()

    # Create record with error
    backend.upsert_tracking(
        repo_alias="test-repo",
        error="Some error",
        created_at=now,
    )

    # Clear error by setting to None
    backend.upsert_tracking(
        repo_alias="test-repo",
        error=None,
    )

    # Verify error cleared
    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["error"] is None
