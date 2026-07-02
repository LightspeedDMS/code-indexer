"""
Unit tests for GoldenRepoMetadataSqliteBackend.update_enable_temporal method.

Bug #131: Temporal index status inconsistency - enable_temporal flag incorrect.

Tests verify that the update_enable_temporal method correctly updates the
enable_temporal flag both in-memory and in the SQLite database after
successful temporal index creation.
"""

import tempfile
from pathlib import Path

import pytest

from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test.db")
        # Initialize the database schema
        db_schema = DatabaseSchema(db_path)
        db_schema.initialize_database()
        yield db_path


@pytest.fixture
def backend(temp_db):
    """Create a GoldenRepoMetadataSqliteBackend instance."""
    return GoldenRepoMetadataSqliteBackend(temp_db)


def test_update_enable_temporal_success(backend):
    """Test that update_enable_temporal successfully updates the flag."""
    # AC1: Add a repo with enable_temporal=False
    backend.add_repo(
        alias="test-repo",
        repo_url="https://github.com/test/repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
        enable_temporal=False,
    )

    # AC2: Verify initial state
    repo = backend.get_repo("test-repo")
    assert repo is not None
    assert repo["enable_temporal"] is False

    # AC3: Update enable_temporal to True
    result = backend.update_enable_temporal("test-repo", True)

    # AC4: Verify update succeeded
    assert result is True

    # AC5: Verify flag was updated in database
    repo = backend.get_repo("test-repo")
    assert repo is not None
    assert repo["enable_temporal"] is True


def test_update_enable_temporal_not_found(backend):
    """Test that update_enable_temporal returns False for non-existent alias."""
    # AC1: Try to update a non-existent repo
    result = backend.update_enable_temporal("non-existent", True)

    # AC2: Verify operation returned False
    assert result is False


def test_update_enable_temporal_false_to_true(backend):
    """Test transition from False to True."""
    # AC1: Add repo with enable_temporal=False
    backend.add_repo(
        alias="test-repo",
        repo_url="https://github.com/test/repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
        enable_temporal=False,
    )

    # AC2: Update to True
    result = backend.update_enable_temporal("test-repo", True)
    assert result is True

    # AC3: Verify flag is True
    repo = backend.get_repo("test-repo")
    assert repo["enable_temporal"] is True


def test_update_enable_temporal_persists(backend):
    """Test that update persists across backend instances."""
    # AC1: Add repo with enable_temporal=False
    backend.add_repo(
        alias="test-repo",
        repo_url="https://github.com/test/repo.git",
        default_branch="main",
        clone_path="/tmp/test-repo",
        created_at="2024-01-01T00:00:00Z",
        enable_temporal=False,
    )

    # AC2: Update to True
    backend.update_enable_temporal("test-repo", True)

    # AC3: Close backend
    backend.close()

    # AC4: Create new backend instance (simulates server restart)
    new_backend = GoldenRepoMetadataSqliteBackend(backend._conn_manager.db_path)

    # AC5: Verify flag persisted
    repo = new_backend.get_repo("test-repo")
    assert repo is not None
    assert repo["enable_temporal"] is True

    new_backend.close()
