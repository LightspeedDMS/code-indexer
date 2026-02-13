"""
Tests for meta_description_hook tracking integration (Story #190, Component 4).

Verifies that on_repo_added creates tracking records and on_repo_removed deletes them.
"""

import pytest
from pathlib import Path
from datetime import datetime, timezone
from code_indexer.global_repos.meta_description_hook import (
    on_repo_added,
    on_repo_removed,
)
from code_indexer.server.storage.sqlite_backends import (
    DescriptionRefreshTrackingBackend,
)


@pytest.fixture
def temp_db(tmp_path):
    """Create temporary SQLite database for tracking."""
    db_path = tmp_path / "test_tracking.db"

    # Initialize schema (matches actual schema from database_manager.py)
    import sqlite3
    conn = sqlite3.connect(str(db_path))
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
    conn.close()

    backend = DescriptionRefreshTrackingBackend(str(db_path))
    return backend, db_path


@pytest.fixture
def mock_golden_repos_dir(tmp_path):
    """Create temporary golden repos directory with cidx-meta."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir()

    cidx_meta = golden_dir / "cidx-meta"
    cidx_meta.mkdir()

    # Create a minimal .code-indexer directory for re-indexing
    (cidx_meta / ".code-indexer").mkdir()

    return str(golden_dir)


@pytest.fixture
def mock_repo(tmp_path):
    """Create a mock repository with README."""
    repo_dir = tmp_path / "test-repo"
    repo_dir.mkdir()

    # Create README
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n\nTest repository for tracking tests.")

    return str(repo_dir)


def test_on_repo_added_creates_tracking_record(
    temp_db, mock_golden_repos_dir, mock_repo, monkeypatch
):
    """Test that on_repo_added creates a tracking record with status=pending."""
    backend, db_path = temp_db

    # Mock get_claude_cli_manager to return None (use fallback path)
    monkeypatch.setattr(
        "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
        lambda: None,
    )

    # Mock the tracking backend module-level variable
    # We need to inject it into meta_description_hook
    import code_indexer.global_repos.meta_description_hook as hook_module
    hook_module._tracking_backend = backend

    # Call on_repo_added
    repo_alias = "test-repo"
    on_repo_added(
        repo_name=repo_alias,
        repo_url="https://github.com/test/test-repo.git",
        clone_path=mock_repo,
        golden_repos_dir=mock_golden_repos_dir,
    )

    # Verify tracking record was created
    record = backend.get_tracking_record(repo_alias)
    assert record is not None
    assert record["repo_alias"] == repo_alias
    assert record["status"] == "pending"
    assert record["next_run"] is not None
    assert record["created_at"] is not None
    assert record["updated_at"] is not None


def test_on_repo_removed_deletes_tracking_record(
    temp_db, mock_golden_repos_dir, monkeypatch
):
    """Test that on_repo_removed deletes the tracking record."""
    backend, db_path = temp_db

    # Mock the tracking backend
    import code_indexer.global_repos.meta_description_hook as hook_module
    hook_module._tracking_backend = backend

    # Pre-create a tracking record
    repo_alias = "test-repo"
    now_iso = datetime.now(timezone.utc).isoformat()
    backend.upsert_tracking(
        repo_alias=repo_alias,
        status="pending",
        next_run=now_iso,
        created_at=now_iso,
        updated_at=now_iso,
    )

    # Verify it exists
    assert backend.get_tracking_record(repo_alias) is not None

    # Call on_repo_removed
    on_repo_removed(
        repo_name=repo_alias,
        golden_repos_dir=mock_golden_repos_dir,
    )

    # Verify tracking record was deleted
    assert backend.get_tracking_record(repo_alias) is None


def test_on_repo_removed_no_tracking_record_no_error(
    temp_db, mock_golden_repos_dir, monkeypatch
):
    """Test that on_repo_removed doesn't error when no tracking record exists."""
    backend, db_path = temp_db

    # Mock the tracking backend
    import code_indexer.global_repos.meta_description_hook as hook_module
    hook_module._tracking_backend = backend

    # Call on_repo_removed for non-existent repo (should not raise)
    on_repo_removed(
        repo_name="nonexistent-repo",
        golden_repos_dir=mock_golden_repos_dir,
    )

    # Verify no error was raised (test passes if we get here)


def test_on_repo_added_skips_cidx_meta(
    temp_db, mock_golden_repos_dir, mock_repo, monkeypatch
):
    """Test that on_repo_added skips creating tracking record for cidx-meta itself."""
    backend, db_path = temp_db

    # Mock the tracking backend
    import code_indexer.global_repos.meta_description_hook as hook_module
    hook_module._tracking_backend = backend

    # Call on_repo_added for cidx-meta
    on_repo_added(
        repo_name="cidx-meta",
        repo_url="https://github.com/test/cidx-meta.git",
        clone_path=mock_repo,
        golden_repos_dir=mock_golden_repos_dir,
    )

    # Verify NO tracking record was created
    assert backend.get_tracking_record("cidx-meta") is None


def test_on_repo_added_handles_missing_backend_gracefully(
    mock_golden_repos_dir, mock_repo, monkeypatch
):
    """Test that on_repo_added doesn't crash if tracking backend is None."""
    # Mock get_claude_cli_manager to return None
    monkeypatch.setattr(
        "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
        lambda: None,
    )

    # Mock tracking backend as None
    import code_indexer.global_repos.meta_description_hook as hook_module
    hook_module._tracking_backend = None

    # Call on_repo_added (should not crash)
    on_repo_added(
        repo_name="test-repo",
        repo_url="https://github.com/test/test-repo.git",
        clone_path=mock_repo,
        golden_repos_dir=mock_golden_repos_dir,
    )

    # Test passes if no exception was raised
