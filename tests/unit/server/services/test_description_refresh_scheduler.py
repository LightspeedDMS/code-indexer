"""
Unit tests for DescriptionRefreshScheduler (Story #190, Component 3).

Tests the description refresh scheduler service that manages periodic
description regeneration for golden repositories.
"""

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.description_refresh_scheduler import (
    DescriptionRefreshScheduler,
)
from code_indexer.server.storage.database_manager import DatabaseConnectionManager
from code_indexer.server.utils.config_manager import (
    ServerConfig,
    ClaudeIntegrationConfig,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
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
def mock_config_manager():
    """Create mock config manager."""
    config = ServerConfig(server_dir="/tmp/test")
    config.claude_integration_config = ClaudeIntegrationConfig()
    config.claude_integration_config.description_refresh_enabled = True
    config.claude_integration_config.description_refresh_interval_hours = 24

    config_manager = MagicMock()
    config_manager.load_config.return_value = config
    return config_manager


@pytest.fixture
def scheduler(db_path: str, mock_config_manager):
    """Create scheduler instance."""
    return DescriptionRefreshScheduler(
        db_path=db_path, config_manager=mock_config_manager
    )


def test_calculate_next_run_deterministic_bucket(scheduler: DescriptionRefreshScheduler):
    """Test calculate_next_run assigns same alias to same bucket."""
    next_run_1 = scheduler.calculate_next_run("test-repo", interval_hours=24)
    next_run_2 = scheduler.calculate_next_run("test-repo", interval_hours=24)

    # Parse ISO timestamps
    dt1 = datetime.fromisoformat(next_run_1)
    dt2 = datetime.fromisoformat(next_run_2)

    # Should be within same hour bucket (allow jitter variation)
    assert abs((dt1 - dt2).total_seconds()) < 3600


def test_calculate_next_run_distributes_across_buckets(
    scheduler: DescriptionRefreshScheduler,
):
    """Test calculate_next_run distributes different aliases across buckets."""
    interval_hours = 24
    aliases = [f"repo-{i}" for i in range(50)]

    next_runs = []
    for alias in aliases:
        next_run = scheduler.calculate_next_run(alias, interval_hours=interval_hours)
        dt = datetime.fromisoformat(next_run)
        next_runs.append(dt)

    # Check that repos are distributed across different hours (not all in same bucket)
    unique_hours = set(dt.hour for dt in next_runs)
    # With 50 repos and 24 buckets, we should see multiple different hours
    assert len(unique_hours) > 1


def test_calculate_next_run_hash_based_bucketing(
    scheduler: DescriptionRefreshScheduler,
):
    """Test calculate_next_run uses hash-based bucketing."""
    interval_hours = 24
    alias = "test-repo"

    # Calculate expected bucket using hashlib (same as production code)
    expected_bucket = int(hashlib.md5(alias.encode()).hexdigest(), 16) % interval_hours

    next_run = scheduler.calculate_next_run(alias, interval_hours=interval_hours)
    dt = datetime.fromisoformat(next_run)

    # Calculate base time (next hour boundary)
    now = datetime.now(timezone.utc)
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    expected_base = next_hour + timedelta(hours=expected_bucket)

    # next_run should be within bucket + jitter (0-18 minutes)
    time_diff = (dt - expected_base).total_seconds()
    assert 0 <= time_diff <= 1080  # 18 minutes = 1080 seconds


def test_has_changes_since_last_run_no_metadata(
    scheduler: DescriptionRefreshScheduler, tmp_path: Path
):
    """Test has_changes_since_last_run returns True when metadata.json missing."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()

    tracking_record = {"last_known_commit": "abc123"}

    result = scheduler.has_changes_since_last_run(str(repo_path), tracking_record)
    assert result is True


def test_has_changes_since_last_run_git_repo_no_change(
    scheduler: DescriptionRefreshScheduler, tmp_path: Path
):
    """Test has_changes_since_last_run returns False when git commit unchanged."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()

    # Create metadata.json with git info
    metadata = {
        "current_commit": "abc123",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    (repo_path / ".code-indexer" / "metadata.json").parent.mkdir(parents=True)
    (repo_path / ".code-indexer" / "metadata.json").write_text(json.dumps(metadata))

    tracking_record = {"last_known_commit": "abc123"}

    result = scheduler.has_changes_since_last_run(str(repo_path), tracking_record)
    assert result is False


def test_has_changes_since_last_run_git_repo_changed(
    scheduler: DescriptionRefreshScheduler, tmp_path: Path
):
    """Test has_changes_since_last_run returns True when git commit changed."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()

    # Create metadata.json with new commit
    metadata = {
        "current_commit": "xyz789",
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    (repo_path / ".code-indexer" / "metadata.json").parent.mkdir(parents=True)
    (repo_path / ".code-indexer" / "metadata.json").write_text(json.dumps(metadata))

    tracking_record = {"last_known_commit": "abc123"}

    result = scheduler.has_changes_since_last_run(str(repo_path), tracking_record)
    assert result is True


def test_has_changes_since_last_run_langfuse_no_change(
    scheduler: DescriptionRefreshScheduler, tmp_path: Path
):
    """Test has_changes_since_last_run returns False when Langfuse files_processed unchanged."""
    repo_path = tmp_path / "langfuse-repo"
    repo_path.mkdir()

    # Create metadata.json with Langfuse info
    metadata = {
        "files_processed": 100,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    (repo_path / ".code-indexer" / "metadata.json").parent.mkdir(parents=True)
    (repo_path / ".code-indexer" / "metadata.json").write_text(json.dumps(metadata))

    tracking_record = {"last_known_files_processed": 100}

    result = scheduler.has_changes_since_last_run(str(repo_path), tracking_record)
    assert result is False


def test_has_changes_since_last_run_langfuse_changed(
    scheduler: DescriptionRefreshScheduler, tmp_path: Path
):
    """Test has_changes_since_last_run returns True when Langfuse files_processed changed."""
    repo_path = tmp_path / "langfuse-repo"
    repo_path.mkdir()

    # Create metadata.json with new files count
    metadata = {
        "files_processed": 150,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    (repo_path / ".code-indexer" / "metadata.json").parent.mkdir(parents=True)
    (repo_path / ".code-indexer" / "metadata.json").write_text(json.dumps(metadata))

    tracking_record = {"last_known_files_processed": 100}

    result = scheduler.has_changes_since_last_run(str(repo_path), tracking_record)
    assert result is True


def test_get_stale_repos_empty(scheduler: DescriptionRefreshScheduler):
    """Test get_stale_repos returns empty list when no stale repos."""
    stale_repos = scheduler.get_stale_repos()
    assert stale_repos == []


def test_get_stale_repos_with_tracking_records(
    scheduler: DescriptionRefreshScheduler, db_path: str, tmp_path: Path
):
    """Test get_stale_repos returns repos with next_run in past."""
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
    )

    backend = DescriptionRefreshTrackingBackend(db_path)

    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat()
    future = (now + timedelta(hours=1)).isoformat()

    # Add stale repo
    backend.upsert_tracking(
        repo_alias="stale-repo",
        next_run=past,
        status="pending",
        created_at=past,
        updated_at=past,
    )

    # Add future repo
    backend.upsert_tracking(
        repo_alias="future-repo",
        next_run=future,
        status="pending",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    # Create golden repos metadata
    from code_indexer.server.storage.sqlite_backends import (
        GoldenRepoMetadataSqliteBackend,
    )

    golden_backend = GoldenRepoMetadataSqliteBackend(db_path)
    conn_manager = DatabaseConnectionManager(db_path)
    conn = conn_manager.get_connection()

    # Create golden_repos_metadata table (matching production schema)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS golden_repos_metadata (
            alias TEXT PRIMARY KEY NOT NULL,
            repo_url TEXT NOT NULL,
            default_branch TEXT NOT NULL,
            clone_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            enable_temporal INTEGER NOT NULL DEFAULT 0,
            temporal_options TEXT,
            category_id INTEGER,
            category_auto_assigned INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn_manager.close_all()

    # Add golden repo record
    repo_path = tmp_path / "stale-repo"
    repo_path.mkdir()

    golden_backend.add_repo(
        alias="stale-repo",
        repo_url="https://example.com/stale.git",
        default_branch="main",
        clone_path=str(repo_path),
        created_at=past,
    )

    stale_repos = scheduler.get_stale_repos()
    assert len(stale_repos) == 1
    assert stale_repos[0]["repo_alias"] == "stale-repo"
    assert "clone_path" in stale_repos[0]


def test_on_refresh_complete_success(
    scheduler: DescriptionRefreshScheduler, db_path: str, tmp_path: Path
):
    """Test on_refresh_complete updates tracking record on success."""
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
    )

    backend = DescriptionRefreshTrackingBackend(db_path)
    now = datetime.now(timezone.utc).isoformat()

    # Create initial tracking record
    backend.upsert_tracking(
        repo_alias="test-repo",
        next_run=now,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    # Create repo path with metadata
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()
    metadata = {
        "current_commit": "abc123",
        "indexed_at": now,
    }
    (repo_path / ".code-indexer" / "metadata.json").parent.mkdir(parents=True)
    (repo_path / ".code-indexer" / "metadata.json").write_text(json.dumps(metadata))

    # Call callback
    scheduler.on_refresh_complete(
        repo_alias="test-repo", repo_path=str(repo_path), success=True, result=None
    )

    # Verify tracking record updated
    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["status"] == "completed"
    assert record["last_known_commit"] == "abc123"
    assert record["error"] is None


def test_on_refresh_complete_failure(
    scheduler: DescriptionRefreshScheduler, db_path: str, tmp_path: Path
):
    """Test on_refresh_complete updates tracking record on failure."""
    from code_indexer.server.storage.sqlite_backends import (
        DescriptionRefreshTrackingBackend,
    )

    backend = DescriptionRefreshTrackingBackend(db_path)
    now = datetime.now(timezone.utc).isoformat()

    # Create initial tracking record
    backend.upsert_tracking(
        repo_alias="test-repo",
        next_run=now,
        status="pending",
        created_at=now,
        updated_at=now,
    )

    # Call callback with failure
    scheduler.on_refresh_complete(
        repo_alias="test-repo",
        repo_path="/nonexistent",
        success=False,
        result={"error": "Claude CLI timeout"},
    )

    # Verify tracking record updated
    record = backend.get_tracking_record("test-repo")
    assert record is not None
    assert record["status"] == "failed"
    assert "Claude CLI timeout" in record["error"]


def test_validate_cli_output_valid_description(scheduler: DescriptionRefreshScheduler):
    """Test _validate_cli_output accepts valid description (200+ chars)."""
    valid_description = (
        "This is a comprehensive repository description that provides detailed "
        "information about the codebase. It explains the purpose, architecture, "
        "key components, and usage patterns. This description is sufficiently long "
        "to be considered valid and not an error message."
    )

    assert scheduler._validate_cli_output(valid_description) is True


def test_validate_cli_output_too_short(scheduler: DescriptionRefreshScheduler):
    """Test _validate_cli_output rejects output that is too short (50 chars)."""
    short_output = "This is too short to be a real description."

    assert scheduler._validate_cli_output(short_output) is False


def test_validate_cli_output_empty(scheduler: DescriptionRefreshScheduler):
    """Test _validate_cli_output rejects empty string."""
    assert scheduler._validate_cli_output("") is False


def test_validate_cli_output_error_pattern_api_key(scheduler: DescriptionRefreshScheduler):
    """Test _validate_cli_output detects 'Invalid API key' error message."""
    error_output = (
        "Error: Invalid API key. Please check your authentication credentials "
        "and try again. Visit the documentation for more information."
    )

    assert scheduler._validate_cli_output(error_output) is False


def test_validate_cli_output_error_pattern_nested_session(
    scheduler: DescriptionRefreshScheduler,
):
    """Test _validate_cli_output detects nested session error message."""
    error_output = (
        "Error: Claude Code cannot be launched inside another Claude Code session. "
        "Nested sessions share runtime state and can cause conflicts. "
        "Please exit the outer session first."
    )

    assert scheduler._validate_cli_output(error_output) is False


def test_invoke_cli_strips_bare_esc_bytes(
    scheduler: DescriptionRefreshScheduler, tmp_path: Path
):
    """Test _invoke_claude_cli strips trailing bare ESC bytes (0x1b)."""
    valid_description = (
        "---\nlast_analyzed: \"2026-01-01T00:00:00Z\"\n---\n"
        "# test-repo\n\nA comprehensive description of the repository "
        "that contains useful information about the codebase structure "
        "and purpose for developers.\x1b"
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = valid_description
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        scheduler._claude_cli_manager = None
        success, output = scheduler._invoke_claude_cli(str(tmp_path), "test prompt")

    assert success is True
    assert "\x1b" not in output


def test_invoke_cli_strips_chain_of_thought_before_frontmatter(
    scheduler: DescriptionRefreshScheduler, tmp_path: Path
):
    """Test _invoke_claude_cli strips reasoning text before YAML frontmatter."""
    output_with_reasoning = (
        "The initial commit was on 2025-12-11, which is after the last "
        "analyzed date of 2025-01-01. So this is a material change that "
        "warrants updating the description.\n\n"
        "---\n"
        "last_analyzed: \"2026-02-13T00:00:00Z\"\n"
        "---\n"
        "# test-repo\n\n"
        "A comprehensive description of the repository that contains "
        "useful information about the codebase structure and purpose "
        "for developers working with this project."
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = output_with_reasoning
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        scheduler._claude_cli_manager = None
        success, output = scheduler._invoke_claude_cli(str(tmp_path), "test prompt")

    assert success is True
    assert output.startswith("---")
    assert "material change" not in output
    assert "last_analyzed" in output
    assert "test-repo" in output


def test_invoke_cli_preserves_output_without_frontmatter(
    scheduler: DescriptionRefreshScheduler, tmp_path: Path
):
    """Test _invoke_claude_cli preserves output that has no YAML frontmatter."""
    plain_description = (
        "# test-repo\n\n"
        "A comprehensive description of the repository that contains "
        "useful information about the codebase structure and purpose "
        "for developers working with this project. It includes details "
        "about architecture, patterns, and key components."
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = plain_description
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        scheduler._claude_cli_manager = None
        success, output = scheduler._invoke_claude_cli(str(tmp_path), "test prompt")

    assert success is True
    assert output.startswith("# test-repo")
    assert "comprehensive description" in output
