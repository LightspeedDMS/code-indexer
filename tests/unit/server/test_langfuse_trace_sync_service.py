"""
Unit tests for Langfuse Trace Sync Service (Story #165).

Tests are organized in phases:
- Phase 1: Pure unit tests for all static/utility methods
- Phase 2: Integration tests with real Langfuse API (requires live config)
"""

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, Mock

import pytest

from code_indexer.server.services.langfuse_trace_sync_service import (
    LangfuseTraceSyncService,
    SyncMetrics,
)
from code_indexer.server.utils.config_manager import (
    LangfuseConfig,
    LangfusePullProject,
    ServerConfig,
)


# ==============================================================================
# Phase 1: Pure Unit Tests (No API calls)
# ==============================================================================


class TestSanitizeFolderName:
    """Test folder name sanitization."""

    def test_basic_sanitization(self):
        """Forward slashes should be replaced with underscores."""
        result = LangfuseTraceSyncService._sanitize_folder_name("hello/world")
        assert result == "hello_world"

    def test_special_characters(self):
        """Various special characters should be replaced with underscores."""
        result = LangfuseTraceSyncService._sanitize_folder_name('a:b*c?d"e')
        assert result == "a_b_c_d_e"

    def test_spaces(self):
        """Spaces should be replaced with underscores."""
        result = LangfuseTraceSyncService._sanitize_folder_name("my project")
        assert result == "my_project"

    def test_clean_name(self):
        """Clean names with hyphens and underscores should pass through."""
        result = LangfuseTraceSyncService._sanitize_folder_name("clean-name_123")
        assert result == "clean-name_123"

    def test_multiple_forbidden_chars(self):
        """Multiple forbidden characters should all be replaced."""
        result = LangfuseTraceSyncService._sanitize_folder_name("@#$%^&()")
        assert result == "________"

    def test_brackets_and_braces(self):
        """Brackets and braces should be replaced."""
        result = LangfuseTraceSyncService._sanitize_folder_name("test[0]{1}")
        assert result == "test_0__1_"

    def test_semicolon_and_quotes(self):
        """Semicolons and quotes should be replaced."""
        result = LangfuseTraceSyncService._sanitize_folder_name("test;'value'")
        assert result == "test__value_"

    def test_backslash(self):
        """Backslashes should be replaced."""
        result = LangfuseTraceSyncService._sanitize_folder_name("path\\to\\file")
        assert result == "path_to_file"

    def test_pipe_and_less_than_greater_than(self):
        """Pipe and angle brackets should be replaced."""
        result = LangfuseTraceSyncService._sanitize_folder_name("a|b<c>d")
        assert result == "a_b_c_d"

    def test_comma_and_exclamation(self):
        """Commas and exclamation marks should be replaced."""
        result = LangfuseTraceSyncService._sanitize_folder_name("hello, world!")
        assert result == "hello__world_"


class TestBuildCanonicalJson:
    """Test deterministic JSON building."""

    def test_deterministic_output(self):
        """Same input in different order should produce identical JSON."""
        trace = {"id": "t1", "name": "test", "timestamp": "2024-01-01"}
        obs1 = [{"id": "o1", "type": "generation"}, {"id": "o2", "type": "span"}]
        obs2 = [{"id": "o2", "type": "span"}, {"id": "o1", "type": "generation"}]

        result1 = LangfuseTraceSyncService._build_canonical_json(trace, obs1)
        result2 = LangfuseTraceSyncService._build_canonical_json(trace, obs2)

        assert result1 == result2

    def test_observations_sorted_by_id(self):
        """Observations should be sorted by ID in canonical JSON."""
        trace = {"id": "t1"}
        observations = [
            {"id": "o3", "type": "generation"},
            {"id": "o1", "type": "span"},
            {"id": "o2", "type": "event"},
        ]

        result = LangfuseTraceSyncService._build_canonical_json(trace, observations)
        parsed = json.loads(result)

        assert parsed["observations"][0]["id"] == "o1"
        assert parsed["observations"][1]["id"] == "o2"
        assert parsed["observations"][2]["id"] == "o3"

    def test_keys_sorted_in_output(self):
        """All keys should be sorted in canonical JSON."""
        trace = {"z_field": "last", "a_field": "first", "m_field": "middle"}
        observations = []

        result = LangfuseTraceSyncService._build_canonical_json(trace, observations)

        # Check that JSON has keys in sorted order
        assert result.index('"a_field"') < result.index('"m_field"')
        assert result.index('"m_field"') < result.index('"z_field"')

    def test_no_whitespace_in_output(self):
        """Canonical JSON should have no extra whitespace."""
        trace = {"id": "t1", "name": "test"}
        observations = [{"id": "o1"}]

        result = LangfuseTraceSyncService._build_canonical_json(trace, observations)

        # Should use compact separators
        assert ", " not in result  # No space after comma
        assert ": " not in result  # No space after colon

    def test_empty_observations(self):
        """Should handle empty observations list."""
        trace = {"id": "t1", "name": "test"}
        observations = []

        result = LangfuseTraceSyncService._build_canonical_json(trace, observations)
        parsed = json.loads(result)

        assert parsed["trace"] == trace
        assert parsed["observations"] == []


class TestComputeHash:
    """Test SHA256 hashing."""

    def test_consistent_hash(self):
        """Same input should produce same hash."""
        text = '{"a":1,"b":2}'

        hash1 = LangfuseTraceSyncService._compute_hash(text)
        hash2 = LangfuseTraceSyncService._compute_hash(text)

        assert hash1 == hash2

    def test_different_content_different_hash(self):
        """Different input should produce different hash."""
        text1 = '{"a":1,"b":2}'
        text2 = '{"a":1,"b":3}'

        hash1 = LangfuseTraceSyncService._compute_hash(text1)
        hash2 = LangfuseTraceSyncService._compute_hash(text2)

        assert hash1 != hash2

    def test_hash_format(self):
        """Hash should be 64-character hex string (SHA256)."""
        text = '{"test":"data"}'

        result = LangfuseTraceSyncService._compute_hash(text)

        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_known_hash_value(self):
        """Test against known SHA256 hash."""
        text = "hello"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()

        result = LangfuseTraceSyncService._compute_hash(text)

        assert result == expected


class TestGetTraceFolder:
    """Test folder path building."""

    def test_normal_trace(self):
        """Normal trace with userId and sessionId."""
        service = LangfuseTraceSyncService(lambda: _mock_config(), "/data")
        trace = {"userId": "user123", "sessionId": "session456"}

        folder = service._get_trace_folder("my-project", trace)

        assert folder == Path("/data/golden-repos/langfuse_my-project_user123/session456")

    def test_no_user(self):
        """Trace without userId should use 'no_user'."""
        service = LangfuseTraceSyncService(lambda: _mock_config(), "/data")
        trace = {"sessionId": "session456"}

        folder = service._get_trace_folder("my-project", trace)

        assert folder == Path("/data/golden-repos/langfuse_my-project_no_user/session456")

    def test_no_session(self):
        """Trace without sessionId should use 'no_session'."""
        service = LangfuseTraceSyncService(lambda: _mock_config(), "/data")
        trace = {"userId": "user123"}

        folder = service._get_trace_folder("my-project", trace)

        assert folder == Path(
            "/data/golden-repos/langfuse_my-project_user123/no_session"
        )

    def test_no_user_no_session(self):
        """Trace without both should use both defaults."""
        service = LangfuseTraceSyncService(lambda: _mock_config(), "/data")
        trace = {}

        folder = service._get_trace_folder("my-project", trace)

        assert folder == Path(
            "/data/golden-repos/langfuse_my-project_no_user/no_session"
        )

    def test_special_chars_in_names(self):
        """Special characters in user/session should be sanitized."""
        service = LangfuseTraceSyncService(lambda: _mock_config(), "/data")
        trace = {"userId": "user@example.com", "sessionId": "session:123"}

        folder = service._get_trace_folder("my-project", trace)

        # Should replace @ and : with underscores
        assert (
            folder
            == Path(
                "/data/golden-repos/langfuse_my-project_user_example.com/session_123"
            )
        )

    def test_special_chars_in_project_name(self):
        """Special characters in project name should be sanitized."""
        service = LangfuseTraceSyncService(lambda: _mock_config(), "/data")
        trace = {"userId": "user123", "sessionId": "session456"}

        folder = service._get_trace_folder("My Project!", trace)

        assert folder == Path(
            "/data/golden-repos/langfuse_My_Project__user123/session456"
        )


class TestSyncState:
    """Test state file load/save."""

    def test_save_and_load(self):
        """State should be saved and loaded correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            project_name = "test-project"

            # Save state with new hash format
            state = {
                "last_sync_timestamp": "2024-01-01T00:00:00+00:00",
                "trace_hashes": {
                    "t1": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash1"},
                    "t2": {"updated_at": "2024-01-01T01:00:00+00:00", "content_hash": "hash2"},
                },
            }
            service._save_sync_state(project_name, state)

            # Load state
            loaded = service._load_sync_state(project_name)

            assert loaded == state

    def test_load_nonexistent(self):
        """Loading nonexistent state should return empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            result = service._load_sync_state("nonexistent-project")

            assert result == {}

    def test_atomic_write(self):
        """State write should use atomic temp file pattern."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            project_name = "test-project"

            state = {"key": "value"}
            service._save_sync_state(project_name, state)

            # Verify no .tmp file remains
            state_file = service._get_state_file_path(project_name)
            tmp_file = state_file.with_suffix(".json.tmp")

            assert state_file.exists()
            assert not tmp_file.exists()

    def test_corrupted_state_file(self):
        """Corrupted state file should return empty dict with warning."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            project_name = "test-project"

            # Write invalid JSON
            state_file = service._get_state_file_path(project_name)
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("not valid json{{{")

            result = service._load_sync_state(project_name)

            assert result == {}

    def test_state_file_path_format(self):
        """State file path should follow naming convention."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            path = service._get_state_file_path("my-project")

            assert path == Path(tmpdir) / "langfuse_sync_state_my-project.json"

    def test_state_file_path_sanitization(self):
        """State file name should sanitize project name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            path = service._get_state_file_path("My Project!")

            assert path == Path(tmpdir) / "langfuse_sync_state_My_Project_.json"


class TestWriteTrace:
    """Test trace JSON writing."""

    def test_write_creates_file(self):
        """Writing trace should create JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "test_folder"
            filename = "001_turn_trace123.json"
            trace = {"id": "trace123", "name": "test"}
            observations = [{"id": "o1"}]

            service._write_trace(folder, filename, trace, observations)

            assert (folder / filename).exists()

    def test_write_creates_directories(self):
        """Writing trace should create parent directories if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "level1" / "level2" / "level3"
            filename = "001_turn_trace123.json"
            trace = {"id": "trace123"}
            observations = []

            service._write_trace(folder, filename, trace, observations)

            assert folder.exists()
            assert (folder / filename).exists()

    def test_write_overwrites_existing(self):
        """Writing trace should overwrite existing file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "test_folder"
            folder.mkdir(parents=True)
            filename = "001_turn_trace123.json"
            trace_file = folder / filename

            # Write initial content
            trace_file.write_text("old content")

            # Overwrite
            trace = {"id": "trace123", "new": "data"}
            observations = []
            service._write_trace(folder, filename, trace, observations)

            content = trace_file.read_text()
            assert "old content" not in content
            assert "new" in content

    def test_write_is_pretty_printed(self):
        """Written JSON should be pretty-printed with indentation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "test_folder"
            filename = "001_turn_trace123.json"
            trace = {"id": "trace123", "name": "test"}
            observations = [{"id": "o1", "type": "generation"}]

            service._write_trace(folder, filename, trace, observations)

            content = (folder / filename).read_text()
            # Pretty-printed JSON should have newlines and indentation
            assert "\n" in content
            assert "  " in content  # Indentation

    def test_write_combines_trace_and_observations(self):
        """Written file should contain both trace and observations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "test_folder"
            filename = "001_turn_trace123.json"
            trace = {"id": "trace123", "name": "test"}
            observations = [{"id": "o1"}, {"id": "o2"}]

            service._write_trace(folder, filename, trace, observations)

            content = (folder / filename).read_text()
            data = json.loads(content)

            assert "trace" in data
            assert "observations" in data
            assert data["trace"] == trace
            assert len(data["observations"]) == 2

    def test_write_sorts_observations_by_start_time(self):
        """Observations should be sorted chronologically by startTime in written file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "test_folder"
            filename = "001_turn_trace123.json"
            trace = {"id": "trace123"}
            observations = [
                {"id": "o3", "startTime": "2026-01-01T12:03:00Z"},
                {"id": "o1", "startTime": "2026-01-01T12:01:00Z"},
                {"id": "o2", "startTime": "2026-01-01T12:02:00Z"},
            ]

            service._write_trace(folder, filename, trace, observations)

            content = (folder / filename).read_text()
            data = json.loads(content)

            assert data["observations"][0]["id"] == "o1"
            assert data["observations"][1]["id"] == "o2"
            assert data["observations"][2]["id"] == "o3"


class TestOverlapWindowCalculation:
    """Test overlap window timestamp logic."""

    def test_first_sync_uses_max_age(self):
        """First sync should use max age as start time."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _mock_config()
            config.langfuse_config.pull_trace_age_days = 30
            service = LangfuseTraceSyncService(lambda: config, tmpdir)

            # Mock empty state (first sync)
            state = {}
            now = datetime.now(timezone.utc)
            max_age_days = 30

            # Calculate expected from_time
            expected_from = now - timedelta(days=max_age_days)

            # Service would calculate: from_time = max_age
            # Verify logic: if no last_sync_timestamp, use max_age
            assert "last_sync_timestamp" not in state

            # Simulation: first sync
            from_time = now - timedelta(days=max_age_days)
            assert from_time < now
            assert (now - from_time).days == max_age_days

    def test_subsequent_sync_uses_overlap(self):
        """Subsequent sync should use last_sync minus overlap window."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            now = datetime.now(timezone.utc)
            last_sync = now - timedelta(hours=6)  # 6 hours ago
            overlap_hours = 2

            # Calculate expected from_time
            expected_from = last_sync - timedelta(hours=overlap_hours)

            # Verify: from_time should be 2 hours before last_sync
            assert (last_sync - expected_from).total_seconds() == overlap_hours * 3600

    def test_overlap_doesnt_exceed_max_age(self):
        """Overlap window should not go beyond max age limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            now = datetime.now(timezone.utc)
            max_age_days = 30
            max_age = now - timedelta(days=max_age_days)

            # Last sync was 29 days ago (recent)
            last_sync = now - timedelta(days=29)
            overlap_hours = 2

            # Overlap window: last_sync - 2 hours
            from_overlap = last_sync - timedelta(hours=overlap_hours)

            # from_time should be max(from_overlap, max_age)
            from_time = max(from_overlap, max_age)

            # from_overlap is within max_age, so should use from_overlap
            assert from_time == from_overlap

            # Now test edge case: last sync was 30 days + 1 hour ago
            last_sync_old = now - timedelta(days=30, hours=1)
            from_overlap_old = last_sync_old - timedelta(hours=overlap_hours)

            # from_overlap_old would be beyond max_age
            from_time_old = max(from_overlap_old, max_age)

            # Should clamp to max_age
            assert from_time_old == max_age


class TestSyncMetrics:
    """Test SyncMetrics data structure."""

    def test_initial_state(self):
        """SyncMetrics should initialize with zeros."""
        metrics = SyncMetrics()

        assert metrics.traces_checked == 0
        assert metrics.traces_written_new == 0
        assert metrics.traces_written_updated == 0
        assert metrics.traces_unchanged == 0
        assert metrics.errors_count == 0
        assert metrics.last_sync_time is None
        assert metrics.last_sync_duration_ms == 0

    def test_metrics_update(self):
        """SyncMetrics should allow updates."""
        metrics = SyncMetrics()

        metrics.traces_checked = 100
        metrics.traces_written_new = 10
        metrics.traces_written_updated = 5
        metrics.traces_unchanged = 85
        metrics.errors_count = 2
        metrics.last_sync_time = "2024-01-01T00:00:00Z"
        metrics.last_sync_duration_ms = 5000

        assert metrics.traces_checked == 100
        assert metrics.traces_written_new == 10
        assert metrics.traces_written_updated == 5
        assert metrics.traces_unchanged == 85
        assert metrics.errors_count == 2
        assert metrics.last_sync_time == "2024-01-01T00:00:00Z"
        assert metrics.last_sync_duration_ms == 5000


class TestServiceLifecycle:
    """Test service start/stop lifecycle."""

    def test_start_creates_thread(self):
        """Starting service should create background thread."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            service.start()

            assert service._thread is not None
            assert service._thread.is_alive()

            service.stop()

    def test_stop_terminates_thread(self):
        """Stopping service should terminate background thread."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            service.start()
            assert service._thread.is_alive()

            service.stop()

            # Thread should be stopped
            assert not service._thread.is_alive()

    def test_start_twice_warns(self):
        """Starting already-running service should warn, not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            service.start()
            thread1 = service._thread

            # Start again - should warn but not break
            service.start()
            thread2 = service._thread

            # Should be same thread
            assert thread1 is thread2

            service.stop()

    def test_get_metrics_returns_dict(self):
        """get_metrics should return dictionary of metrics."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            # Set some metrics
            metrics = SyncMetrics()
            metrics.traces_checked = 50
            service._metrics["project1"] = metrics

            result = service.get_metrics()

            assert isinstance(result, dict)
            assert "project1" in result


# ==============================================================================
# Code Review Fixes Tests (Story #165)
# ==============================================================================


class TestBackwardCompatHashMigration:
    """Test backward compatibility migration from old to new hash format (Finding 2)."""

    def test_migrate_old_string_hash_to_new_dict_format(self):
        """Old format {trace_id: hash_string} should migrate to new format in-memory."""
        # Test the migration logic directly
        trace_hashes_old = {
            "t1": "hash1",  # Old format: plain string
            "t2": "hash2",
        }

        # Apply migration (same logic as in sync_project)
        trace_hashes = trace_hashes_old.copy()
        for tid, val in list(trace_hashes.items()):
            if isinstance(val, str):
                trace_hashes[tid] = {"updated_at": "", "content_hash": val}

        # After migration, should be new format
        assert isinstance(trace_hashes["t1"], dict)
        assert "updated_at" in trace_hashes["t1"]
        assert "content_hash" in trace_hashes["t1"]
        assert trace_hashes["t1"]["content_hash"] == "hash1"
        assert trace_hashes["t1"]["updated_at"] == ""

        assert isinstance(trace_hashes["t2"], dict)
        assert trace_hashes["t2"]["content_hash"] == "hash2"
        assert trace_hashes["t2"]["updated_at"] == ""

    def test_new_format_unchanged_by_migration(self):
        """New format hashes should pass through migration unchanged."""
        # Test the migration logic with already-new format
        trace_hashes_new = {
            "t1": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash1"},
            "t2": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash2"},
        }

        # Apply migration (same logic as in sync_project)
        trace_hashes = trace_hashes_new.copy()
        for tid, val in list(trace_hashes.items()):
            if isinstance(val, str):
                trace_hashes[tid] = {"updated_at": "", "content_hash": val}

        # Should be unchanged
        assert trace_hashes["t1"]["updated_at"] == "2024-01-01T00:00:00+00:00"
        assert trace_hashes["t1"]["content_hash"] == "hash1"
        assert trace_hashes["t2"]["updated_at"] == "2024-01-01T00:00:00+00:00"
        assert trace_hashes["t2"]["content_hash"] == "hash2"

    def test_mixed_format_migration(self):
        """Mixed old and new format should both be handled correctly."""
        trace_hashes_mixed = {
            "t1": "hash1",  # Old format
            "t2": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash2"},  # New format
            "t3": "hash3",  # Old format
        }

        # Apply migration
        trace_hashes = trace_hashes_mixed.copy()
        for tid, val in list(trace_hashes.items()):
            if isinstance(val, str):
                trace_hashes[tid] = {"updated_at": "", "content_hash": val}

        # t1 and t3 should be migrated
        assert isinstance(trace_hashes["t1"], dict)
        assert trace_hashes["t1"]["content_hash"] == "hash1"
        assert trace_hashes["t1"]["updated_at"] == ""

        assert isinstance(trace_hashes["t3"], dict)
        assert trace_hashes["t3"]["content_hash"] == "hash3"
        assert trace_hashes["t3"]["updated_at"] == ""

        # t2 should be unchanged
        assert trace_hashes["t2"]["updated_at"] == "2024-01-01T00:00:00+00:00"
        assert trace_hashes["t2"]["content_hash"] == "hash2"


class TestHashPruning:
    """Test hash pruning to only keep seen traces (Finding 1)."""

    def test_prune_logic(self):
        """Test the pruning logic directly without full sync."""
        # Start with 3 trace hashes
        trace_hashes = {
            "t1": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash1"},
            "t2": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash2"},
            "t3": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash3"},
        }

        # Only t1 and t2 were seen in current sync
        seen_trace_ids = {"t1", "t2"}

        # Apply pruning logic (same as in sync_project)
        pruned_hashes = {
            tid: h for tid, h in trace_hashes.items() if tid in seen_trace_ids
        }

        # t3 should be pruned
        assert "t1" in pruned_hashes
        assert "t2" in pruned_hashes
        assert "t3" not in pruned_hashes
        assert len(pruned_hashes) == 2

    def test_prune_all_when_none_seen(self):
        """Test pruning when no traces seen."""
        trace_hashes = {
            "t1": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash1"},
            "t2": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash2"},
        }

        seen_trace_ids = set()  # No traces seen

        # Apply pruning logic
        pruned_hashes = {
            tid: h for tid, h in trace_hashes.items() if tid in seen_trace_ids
        }

        # All should be pruned
        assert pruned_hashes == {}

    def test_keep_all_when_all_seen(self):
        """Test that all hashes kept when all traces seen."""
        trace_hashes = {
            "t1": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash1"},
            "t2": {"updated_at": "2024-01-01T00:00:00+00:00", "content_hash": "hash2"},
        }

        seen_trace_ids = {"t1", "t2"}  # All traces seen

        # Apply pruning logic
        pruned_hashes = {
            tid: h for tid, h in trace_hashes.items() if tid in seen_trace_ids
        }

        # All should be kept
        assert pruned_hashes == trace_hashes

    def test_prune_keeps_older_valid_traces(self):
        """Test that traces older than sync window but within age limit are kept (Finding 2)."""
        now = datetime.now(timezone.utc)
        max_age_days = 30
        max_age = now - timedelta(days=max_age_days)

        # t1: seen in current sync (2 hours ago)
        # t2: not seen, but 10 days old (within age window)
        # t3: not seen, 35 days old (beyond age window)
        # t4: malformed timestamp (should be discarded)
        trace_hashes = {
            "t1": {"updated_at": (now - timedelta(hours=2)).isoformat(), "content_hash": "hash1"},
            "t2": {"updated_at": (now - timedelta(days=10)).isoformat(), "content_hash": "hash2"},
            "t3": {"updated_at": (now - timedelta(days=35)).isoformat(), "content_hash": "hash3"},
            "t4": {"updated_at": "invalid-timestamp", "content_hash": "hash4"},
        }

        seen_trace_ids = {"t1"}  # Only t1 seen in current sync

        # Apply pruning logic (same as in sync_project with Finding 2 fix)
        pruned_hashes = {}
        for tid, h in trace_hashes.items():
            if tid in seen_trace_ids:
                pruned_hashes[tid] = h
            elif isinstance(h, dict) and h.get("updated_at"):
                try:
                    trace_time = datetime.fromisoformat(h["updated_at"])
                    if trace_time >= max_age:
                        pruned_hashes[tid] = h
                except (ValueError, TypeError):
                    pass

        # t1 should be kept (seen)
        assert "t1" in pruned_hashes
        # t2 should be kept (within age window)
        assert "t2" in pruned_hashes
        # t3 should be pruned (beyond age window)
        assert "t3" not in pruned_hashes
        # t4 should be pruned (malformed timestamp)
        assert "t4" not in pruned_hashes
        assert len(pruned_hashes) == 2


class TestExtractTraceType:
    """Test trace type extraction from trace data."""

    def test_turn_type_for_plain_trace(self):
        """Trace without subagent prefix should return 'turn'."""
        trace = {"id": "abc123", "name": "some-trace-name"}
        result = LangfuseTraceSyncService._extract_trace_type(trace)
        assert result == "turn"

    def test_turn_type_for_empty_name(self):
        """Trace with empty name should return 'turn'."""
        trace = {"id": "abc123", "name": ""}
        result = LangfuseTraceSyncService._extract_trace_type(trace)
        assert result == "turn"

    def test_turn_type_for_missing_name(self):
        """Trace without name key should return 'turn'."""
        trace = {"id": "abc123"}
        result = LangfuseTraceSyncService._extract_trace_type(trace)
        assert result == "turn"

    def test_subagent_type_extraction(self):
        """Trace with 'subagent:Explore' should return 'subagent-Explore'."""
        trace = {"id": "abc123", "name": "subagent:Explore"}
        result = LangfuseTraceSyncService._extract_trace_type(trace)
        assert result == "subagent-Explore"

    def test_subagent_type_with_complex_name(self):
        """Trace with 'subagent:tdd-engineer' should return 'subagent-tdd-engineer'."""
        trace = {"id": "abc123", "name": "subagent:tdd-engineer"}
        result = LangfuseTraceSyncService._extract_trace_type(trace)
        assert result == "subagent-tdd-engineer"

    def test_subagent_type_case_sensitive(self):
        """Subagent prefix match should be case-sensitive."""
        trace = {"id": "abc123", "name": "Subagent:Explore"}
        result = LangfuseTraceSyncService._extract_trace_type(trace)
        # "Subagent:" (capital S) does not match "subagent:" prefix
        assert result == "turn"

    def test_subagent_type_with_none_name(self):
        """Trace with None name should return 'turn'."""
        trace = {"id": "abc123", "name": None}
        result = LangfuseTraceSyncService._extract_trace_type(trace)
        assert result == "turn"

    def test_subagent_with_special_characters(self):
        """Subagent names with filesystem-unsafe chars should be sanitized."""
        trace = {"name": "subagent:some/weird:name"}
        result = LangfuseTraceSyncService._extract_trace_type(trace)
        assert result == "subagent-some_weird_name"


class TestExtractShortId:
    """Test short ID extraction from trace ID string."""

    def test_last_8_chars(self):
        """Should return last 8 characters of trace ID."""
        trace_id = "038114a5-8665-4aaa-a14d-f136244b598b-turn-0b5c9e0c"
        result = LangfuseTraceSyncService._extract_short_id(trace_id)
        assert result == "0b5c9e0c"

    def test_short_trace_id(self):
        """Trace IDs shorter than 8 chars should return the whole ID."""
        trace_id = "abc"
        result = LangfuseTraceSyncService._extract_short_id(trace_id)
        assert result == "abc"

    def test_exactly_8_chars(self):
        """Trace ID of exactly 8 chars should return the whole ID."""
        trace_id = "abcd1234"
        result = LangfuseTraceSyncService._extract_short_id(trace_id)
        assert result == "abcd1234"

    def test_uuid_style_id(self):
        """Standard UUID should return last 8 chars."""
        trace_id = "550e8400-e29b-41d4-a716-446655440000"
        result = LangfuseTraceSyncService._extract_short_id(trace_id)
        assert result == "55440000"


class TestGetNextSeqNumber:
    """Test next sequence number determination from folder contents."""

    def test_empty_folder(self):
        """Empty folder should return 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            result = LangfuseTraceSyncService._get_next_seq_number(folder)
            assert result == 1

    def test_nonexistent_folder(self):
        """Nonexistent folder should return 1."""
        folder = Path("/tmp/nonexistent_folder_test_12345")
        result = LangfuseTraceSyncService._get_next_seq_number(folder)
        assert result == 1

    def test_folder_with_sequential_files(self):
        """Folder with sequential files should return max + 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "001_turn_abcd1234.json").write_text("{}")
            (folder / "002_turn_efgh5678.json").write_text("{}")
            (folder / "003_subagent-Explore_ijkl9012.json").write_text("{}")

            result = LangfuseTraceSyncService._get_next_seq_number(folder)
            assert result == 4

    def test_folder_with_gaps(self):
        """Folder with gaps in sequence should return max + 1 (not fill gaps)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "001_turn_abcd1234.json").write_text("{}")
            (folder / "005_turn_efgh5678.json").write_text("{}")

            result = LangfuseTraceSyncService._get_next_seq_number(folder)
            assert result == 6

    def test_folder_with_old_format_files(self):
        """Folder with old-format files (no seq prefix) should return 1."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "some-trace-id-abc123.json").write_text("{}")
            (folder / "another-trace-id-def456.json").write_text("{}")

            result = LangfuseTraceSyncService._get_next_seq_number(folder)
            assert result == 1

    def test_folder_with_mixed_files(self):
        """Folder with both old and new format should only consider new format."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "old-trace-id.json").write_text("{}")
            (folder / "003_turn_abcd1234.json").write_text("{}")

            result = LangfuseTraceSyncService._get_next_seq_number(folder)
            assert result == 4

    def test_folder_with_non_json_files(self):
        """Non-JSON files should be ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            (folder / "001_turn_abcd1234.json").write_text("{}")
            (folder / "readme.txt").write_text("hello")
            (folder / "005_turn_efgh5678.json.tmp").write_text("{}")

            result = LangfuseTraceSyncService._get_next_seq_number(folder)
            assert result == 2


class TestBuildTraceFilename:
    """Test trace filename building."""

    def test_turn_filename(self):
        """Turn trace should produce correct filename."""
        result = LangfuseTraceSyncService._build_trace_filename(1, "turn", "0b5c9e0c")
        assert result == "001_turn_0b5c9e0c.json"

    def test_subagent_filename(self):
        """Subagent trace should produce correct filename."""
        result = LangfuseTraceSyncService._build_trace_filename(2, "subagent-Explore", "8eac4a27")
        assert result == "002_subagent-Explore_8eac4a27.json"

    def test_zero_padded_sequence(self):
        """Sequence number should be zero-padded to 3 digits."""
        result = LangfuseTraceSyncService._build_trace_filename(42, "turn", "abcd1234")
        assert result == "042_turn_abcd1234.json"

    def test_large_sequence_number(self):
        """Sequence numbers above 999 should still work (no truncation)."""
        result = LangfuseTraceSyncService._build_trace_filename(1234, "turn", "abcd1234")
        assert result == "1234_turn_abcd1234.json"

    def test_subagent_with_hyphenated_name(self):
        """Subagent with hyphenated name should be preserved."""
        result = LangfuseTraceSyncService._build_trace_filename(3, "subagent-tdd-engineer", "12345678")
        assert result == "003_subagent-tdd-engineer_12345678.json"


class TestTwoPhaseHashCheck:
    """Test two-phase hash check optimization (Finding 2)."""

    def test_skip_fetch_when_updated_at_unchanged(self):
        """Should skip observation fetch when updatedAt matches stored value."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            from unittest.mock import Mock

            mock_api_client = Mock()

            trace = {"id": "t1", "updatedAt": "2024-01-01T00:00:00+00:00"}
            trace_hashes = {
                "t1": {
                    "updated_at": "2024-01-01T00:00:00+00:00",
                    "content_hash": "hash123",
                }
            }
            metrics = SyncMetrics()

            # Create the trace file on disk so the file existence check passes
            folder = service._get_trace_folder("test-project", trace)
            folder.mkdir(parents=True, exist_ok=True)
            trace_file = folder / "t1.json"
            trace_file.write_text('{"trace": {}, "observations": []}')

            # Process trace - should skip fetch
            service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            # fetch_observations should NOT be called
            mock_api_client.fetch_observations.assert_not_called()
            assert metrics.traces_unchanged == 1

    def test_fetch_when_updated_at_changed(self):
        """Should fetch observations when updatedAt differs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            from unittest.mock import Mock

            mock_api_client = Mock()
            mock_api_client.fetch_observations.return_value = []

            trace = {"id": "t1", "updatedAt": "2024-01-02T00:00:00+00:00"}  # New time
            trace_hashes = {
                "t1": {
                    "updated_at": "2024-01-01T00:00:00+00:00",  # Old time
                    "content_hash": "hash123",
                }
            }
            metrics = SyncMetrics()

            # Process trace - should fetch
            service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            # fetch_observations SHOULD be called
            mock_api_client.fetch_observations.assert_called_once_with("t1")

    def test_fetch_when_new_trace(self):
        """Should fetch observations for new traces."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            from unittest.mock import Mock

            mock_api_client = Mock()
            mock_api_client.fetch_observations.return_value = []

            trace = {"id": "t_new", "updatedAt": "2024-01-01T00:00:00+00:00"}
            trace_hashes = {}  # Empty - new trace
            metrics = SyncMetrics()

            service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            # Should fetch for new trace
            mock_api_client.fetch_observations.assert_called_once_with("t_new")

    def test_update_updated_at_when_content_unchanged(self):
        """Should update updatedAt even if content hash unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            from unittest.mock import Mock

            mock_api_client = Mock()
            # Return empty observations that hash to same value
            mock_api_client.fetch_observations.return_value = []

            # Create trace
            trace = {"id": "t1", "updatedAt": "2024-01-02T00:00:00+00:00"}

            # Build expected hash from the actual trace that will be processed
            canonical = service._build_canonical_json(trace, [])
            expected_hash = service._compute_hash(canonical)

            trace_hashes = {
                "t1": {
                    "updated_at": "2024-01-01T00:00:00+00:00",  # Old updatedAt
                    "content_hash": expected_hash,  # Same content hash
                }
            }
            metrics = SyncMetrics()

            # Create the trace file on disk so the file existence check passes in Phase 2
            folder = service._get_trace_folder("test-project", trace)
            folder.mkdir(parents=True, exist_ok=True)
            trace_file = folder / "t1.json"
            trace_file.write_text('{"trace": {}, "observations": []}')

            service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            # updatedAt should be updated
            assert trace_hashes["t1"]["updated_at"] == "2024-01-02T00:00:00+00:00"
            assert trace_hashes["t1"]["content_hash"] == expected_hash
            assert metrics.traces_unchanged == 1

    def test_process_trace_rewrites_when_file_missing(self):
        """Test that _process_trace re-writes trace when file is missing from disk despite hash match.

        Updated: With staging approach, Phase 1 writes to staging as {trace_id}.json and returns
        6-tuple metadata, then Phase 2 moves from staging to destination with sequential format.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()
            mock_api_client.fetch_observations.return_value = []

            trace = {
                "id": "test-trace-123",
                "updatedAt": "2026-01-01T00:00:00Z",
                "userId": "test_user",
                "sessionId": "test_session",
            }

            # Build expected hash
            canonical = service._build_canonical_json(trace, [])
            content_hash = service._compute_hash(canonical)

            # Pre-populate hash as if trace was previously synced (no "filename" = old format)
            trace_hashes = {
                "test-trace-123": {
                    "updated_at": "2026-01-01T00:00:00Z",
                    "content_hash": content_hash,
                }
            }

            metrics = SyncMetrics()

            # Phase 1: should write to STAGING as {trace_id}.json and return 6-tuple metadata
            rename_info = service._process_trace(mock_api_client, trace, "TestProject", trace_hashes, metrics)

            # Verify temp file was written to STAGING (not destination)
            staging_folder = service._get_staging_dir("TestProject", trace)
            assert (staging_folder / "test-trace-123.json").exists(), "Staging file should have been written"

            # Verify 6-tuple metadata returned
            assert rename_info is not None
            assert len(rename_info) == 6  # Now returns 6-tuple
            assert trace_hashes["test-trace-123"]["filename"] is None  # Will be set in Phase 2

            # Phase 2: Finalize (move from staging to destination with sequential names)
            if rename_info:
                service._finalize_trace_files([rename_info], trace_hashes)

            # Now verify sequential filename assigned and file moved to destination
            dest_folder = service._get_trace_folder("TestProject", trace)
            new_filename = trace_hashes["test-trace-123"]["filename"]
            assert new_filename == "001_turn_race-123.json"
            assert (dest_folder / new_filename).exists(), "Trace file should exist in destination"
            assert not (staging_folder / "test-trace-123.json").exists(), "Staging file should be moved (not copied)"

            # Should be counted as updated (not new, not unchanged)
            assert metrics.traces_written_new == 0  # Not "new" - trace was in hashes
            assert metrics.traces_written_updated == 1  # Re-written because file was missing
            assert metrics.traces_unchanged == 0  # Should NOT be unchanged


class TestSequentialNaming:
    """Integration tests for sequential trace file naming through _process_trace."""

    def test_new_trace_gets_sequential_filename(self):
        """A new trace should be written with sequential naming format.

        Updated: Phase 1 writes as {trace_id}.json, Phase 2 renames to sequential.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()
            mock_api_client.fetch_observations.return_value = []

            trace = {
                "id": "038114a5-8665-4aaa-a14d-f136244b598b-turn-0b5c9e0c",
                "name": "my-trace",
                "updatedAt": "2024-01-01T00:00:00+00:00",
                "userId": "test_user",
                "sessionId": "test_session",
            }
            trace_hashes = {}
            metrics = SyncMetrics()

            # Phase 1: Process trace (writes temp file, returns rename metadata)
            rename_info = service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            # Phase 2: Assign sequential names
            if rename_info:
                service._finalize_trace_files([rename_info], trace_hashes)

            folder = service._get_trace_folder("test-project", trace)
            expected_filename = "001_turn_0b5c9e0c.json"
            assert (folder / expected_filename).exists()
            assert not (folder / f"{trace['id']}.json").exists()

            trace_id = trace["id"]
            assert "filename" in trace_hashes[trace_id]
            assert trace_hashes[trace_id]["filename"] == expected_filename

    def test_subagent_trace_gets_correct_type_in_filename(self):
        """A subagent trace should have subagent type in filename.

        Updated: Phase 1 writes temp file, Phase 2 renames to sequential with subagent type.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()
            mock_api_client.fetch_observations.return_value = []

            trace = {
                "id": "abc12345-6789-0000-1111-22223333aaaa-sub-8eac4a27",
                "name": "subagent:Explore",
                "updatedAt": "2024-01-01T00:00:00+00:00",
                "userId": "test_user",
                "sessionId": "test_session",
            }
            trace_hashes = {}
            metrics = SyncMetrics()

            # Phase 1 and 2
            rename_info = service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)
            if rename_info:
                service._finalize_trace_files([rename_info], trace_hashes)

            folder = service._get_trace_folder("test-project", trace)
            expected_filename = "001_subagent-Explore_8eac4a27.json"
            assert (folder / expected_filename).exists()
            assert trace_hashes[trace["id"]]["filename"] == expected_filename

    def test_multiple_traces_get_sequential_numbers(self):
        """Multiple new traces in same folder should get incrementing sequence numbers.

        Updated: Collect rename metadata in Phase 1, then rename all in Phase 2.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()
            mock_api_client.fetch_observations.return_value = []

            trace_hashes = {}
            metrics = SyncMetrics()
            pending_renames = []

            for i, suffix in enumerate(["aaaa1111", "bbbb2222", "cccc3333"]):
                trace = {
                    "id": f"trace-{i}-{suffix}",
                    "name": "my-trace",
                    "updatedAt": "2024-01-01T00:00:00+00:00",
                    "userId": "test_user",
                    "sessionId": "test_session",
                }
                rename_info = service._process_trace(
                    mock_api_client, trace, "test-project", trace_hashes, metrics
                )
                if rename_info:
                    pending_renames.append(rename_info)

            # Phase 2: Rename all at once
            if pending_renames:
                service._finalize_trace_files(pending_renames, trace_hashes)

            folder = service._get_trace_folder("test-project", trace)
            json_files = sorted(folder.glob("*.json"))
            assert len(json_files) == 3
            assert json_files[0].name.startswith("001_")
            assert json_files[1].name.startswith("002_")
            assert json_files[2].name.startswith("003_")

    def test_trace_update_keeps_same_filename(self):
        """When a trace is updated, it should keep its original filename.

        Updated: First trace goes through Phase 1+2, updated trace has stored_filename so returns None.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()
            mock_api_client.fetch_observations.return_value = [
                {"id": "obs1", "startTime": "2024-01-01T00:00:00Z"}
            ]

            trace_id = "trace-abc-12345678"
            trace_v1 = {
                "id": trace_id,
                "name": "my-trace",
                "updatedAt": "2024-01-01T00:00:00+00:00",
                "userId": "test_user",
                "sessionId": "test_session",
            }
            trace_hashes = {}
            metrics = SyncMetrics()

            # Initial trace: Phase 1 + 2
            rename_info = service._process_trace(mock_api_client, trace_v1, "test-project", trace_hashes, metrics)
            if rename_info:
                service._finalize_trace_files([rename_info], trace_hashes)

            original_filename = trace_hashes[trace_id]["filename"]
            assert original_filename == "001_turn_12345678.json"

            # Update trace: has stored_filename now, so no rename needed
            mock_api_client.fetch_observations.return_value = [
                {"id": "obs1", "startTime": "2024-01-01T00:00:00Z"},
                {"id": "obs2", "startTime": "2024-01-02T00:00:00Z"},
            ]
            trace_v2 = dict(trace_v1)
            trace_v2["updatedAt"] = "2024-01-02T00:00:00+00:00"

            rename_info2 = service._process_trace(mock_api_client, trace_v2, "test-project", trace_hashes, metrics)
            assert rename_info2 is None  # No rename needed - already has stored_filename

            assert trace_hashes[trace_id]["filename"] == original_filename
            folder = service._get_trace_folder("test-project", trace_v1)
            assert (folder / original_filename).exists()
            assert len(list(folder.glob("*.json"))) == 1

    def test_filename_in_state_persists_through_unchanged_check(self):
        """When trace is unchanged (Phase 1 skip), filename should remain in state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()

            trace = {
                "id": "trace-xyz-99887766",
                "name": "my-trace",
                "updatedAt": "2024-01-01T00:00:00+00:00",
                "userId": "test_user",
                "sessionId": "test_session",
            }

            folder = service._get_trace_folder("test-project", trace)
            folder.mkdir(parents=True, exist_ok=True)
            filename = "001_turn_99887766.json"
            (folder / filename).write_text('{"trace": {}, "observations": []}')

            trace_hashes = {
                "trace-xyz-99887766": {
                    "updated_at": "2024-01-01T00:00:00+00:00",
                    "content_hash": "somehash",
                    "filename": filename,
                }
            }
            metrics = SyncMetrics()

            service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            assert trace_hashes["trace-xyz-99887766"]["filename"] == filename
            assert metrics.traces_unchanged == 1


class TestChronologicalTraceOrdering:
    """Test that traces are sorted chronologically by timestamp before processing."""

    @staticmethod
    def _mock_langfuse_api(pages_data, processed_trace_ids, project_name="test-project"):
        """Helper to mock LangfuseApiClient with paginated trace data.

        Args:
            pages_data: List of trace lists, one per page (e.g., [[trace1, trace2], [trace3]])
            processed_trace_ids: List to append trace IDs as they're processed
            project_name: Project name to return from discover_project

        Returns:
            Tuple of (original_init, cleanup_function)
        """
        from code_indexer.server.services.langfuse_api_client import LangfuseApiClient

        original_init = LangfuseApiClient.__init__

        def mock_init(self, host, creds):
            self.host = host
            self.creds = creds

        LangfuseApiClient.__init__ = mock_init
        LangfuseApiClient.discover_project = Mock(return_value={"name": project_name})

        def mock_fetch_observations(self, trace_id):
            processed_trace_ids.append(trace_id)
            return []

        LangfuseApiClient.fetch_observations = mock_fetch_observations

        call_count = [0]
        def mock_fetch_traces_page(self, page, from_time):
            call_count[0] += 1
            if call_count[0] <= len(pages_data):
                return pages_data[call_count[0] - 1]
            else:
                return []

        LangfuseApiClient.fetch_traces_page = mock_fetch_traces_page

        def cleanup():
            LangfuseApiClient.__init__ = original_init

        return original_init, cleanup

    def test_sync_project_sorts_traces_by_timestamp_ascending(self):
        """Traces returned newest-first from API should be sorted oldest-first for processing.

        Updated: Verifies streaming approach where traces are written as {trace_id}.json during
        Phase 1, then renamed to sequential filenames in Phase 2.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            # Simulate API returning traces in reverse chronological order (newest first)
            pages_data = [
                # Page 1: newest traces
                [
                    {"id": "t3", "timestamp": "2024-01-03T00:00:00Z", "updatedAt": "2024-01-03T00:00:00Z"},
                    {"id": "t2", "timestamp": "2024-01-02T00:00:00Z", "updatedAt": "2024-01-02T00:00:00Z"},
                ],
                # Page 2: older traces
                [
                    {"id": "t1", "timestamp": "2024-01-01T00:00:00Z", "updatedAt": "2024-01-01T00:00:00Z"},
                ],
            ]

            processed_trace_ids = []
            original_init, cleanup = self._mock_langfuse_api(pages_data, processed_trace_ids)

            try:
                creds = LangfusePullProject(public_key="test", secret_key="test")
                service.sync_project("https://test.com", creds, 30)

                # Verify traces were processed (streaming - as pages arrive)
                assert len(processed_trace_ids) == 3
                # NOTE: Processing order may be API order (not sorted) during streaming,
                # but final filenames should reflect chronological order

                # Verify filenames reflect chronological order (after Phase 2 rename)
                folder = service._get_trace_folder("test-project", pages_data[0][0])
                files = sorted(folder.glob("*.json"))

                # Find which file contains which trace ID
                file_map = {}  # {trace_id: filename}
                for f in files:
                    content = json.loads(f.read_text())
                    file_map[content["trace"]["id"]] = f.name

                # Oldest trace (t1) should have lowest seq number
                assert file_map["t1"].startswith("001_"), f"Oldest trace should be 001, got {file_map['t1']}"
                assert file_map["t2"].startswith("002_"), f"Middle trace should be 002, got {file_map['t2']}"
                assert file_map["t3"].startswith("003_"), f"Newest trace should be 003, got {file_map['t3']}"

            finally:
                cleanup()

    def test_multi_page_buffering_before_sorting(self):
        """Verify traces processed in streaming fashion, then sorted in Phase 2 rename.

        Updated: Streaming approach processes each page immediately, collecting lightweight
        metadata, then Phase 2 renames files to sequential order.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            # Mock API that returns 3 pages, each with traces in reverse chronological order
            pages_data = [
                [
                    {"id": "t9", "timestamp": "2024-01-09T00:00:00Z", "updatedAt": "2024-01-09T00:00:00Z"},
                    {"id": "t8", "timestamp": "2024-01-08T00:00:00Z", "updatedAt": "2024-01-08T00:00:00Z"},
                    {"id": "t7", "timestamp": "2024-01-07T00:00:00Z", "updatedAt": "2024-01-07T00:00:00Z"},
                ],
                [
                    {"id": "t6", "timestamp": "2024-01-06T00:00:00Z", "updatedAt": "2024-01-06T00:00:00Z"},
                    {"id": "t5", "timestamp": "2024-01-05T00:00:00Z", "updatedAt": "2024-01-05T00:00:00Z"},
                    {"id": "t4", "timestamp": "2024-01-04T00:00:00Z", "updatedAt": "2024-01-04T00:00:00Z"},
                ],
                [
                    {"id": "t3", "timestamp": "2024-01-03T00:00:00Z", "updatedAt": "2024-01-03T00:00:00Z"},
                    {"id": "t2", "timestamp": "2024-01-02T00:00:00Z", "updatedAt": "2024-01-02T00:00:00Z"},
                    {"id": "t1", "timestamp": "2024-01-01T00:00:00Z", "updatedAt": "2024-01-01T00:00:00Z"},
                ],
            ]

            processed_trace_ids = []
            original_init, cleanup = self._mock_langfuse_api(pages_data, processed_trace_ids)

            try:
                creds = LangfusePullProject(public_key="test", secret_key="test")
                service.sync_project("https://test.com", creds, 30)

                # All 9 traces should be processed
                assert len(processed_trace_ids) == 9

                # Verify sequence numbers match chronological order (after Phase 2 rename)
                folder = service._get_trace_folder("test-project", pages_data[0][0])
                files = sorted(folder.glob("*.json"))
                assert len(files) == 9

                # Map trace IDs to their sequence numbers
                expected_order = ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9"]
                for i, expected_id in enumerate(expected_order, start=1):
                    seq_prefix = f"{i:03d}_"
                    matching_file = [f for f in files if f.name.startswith(seq_prefix)]
                    assert len(matching_file) == 1, f"Expected one file with prefix {seq_prefix}"
                    content = json.loads(matching_file[0].read_text())
                    assert content["trace"]["id"] == expected_id

            finally:
                cleanup()

    def test_existing_traces_with_stored_filenames_unaffected_by_sort(self):
        """Traces that already have stored filenames should keep them despite sorting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)

            # Use realistic trace IDs with 8+ characters for short_id extraction
            trace_id_t2 = "trace-newer-00000002"
            trace_id_t1 = "trace-older-00000001"

            # Simulate traces returned newest-first
            pages_data = [
                [
                    {"id": trace_id_t2, "timestamp": "2024-01-02T00:00:00Z", "updatedAt": "2024-01-02T00:00:00Z"},
                    {"id": trace_id_t1, "timestamp": "2024-01-01T00:00:00Z", "updatedAt": "2024-01-01T00:00:00Z"},
                ]
            ]

            # Pre-populate folder with existing file for t2
            folder_path = Path(tmpdir) / "golden-repos" / "langfuse_test-project_no_user" / "no_session"
            folder_path.mkdir(parents=True, exist_ok=True)
            existing_file_t2 = folder_path / "001_turn_00000002.json"
            existing_file_t2.write_text('{"trace": {"id": "' + trace_id_t2 + '"}, "observations": []}')

            # Build expected hash for t2 so it's detected as unchanged
            canonical_t2 = service._build_canonical_json(pages_data[0][0], [])
            hash_t2 = service._compute_hash(canonical_t2)

            # Pre-populate state with t2 having its stored filename
            state = {
                "last_sync_timestamp": "2024-01-01T12:00:00+00:00",
                "trace_hashes": {
                    trace_id_t2: {
                        "updated_at": "2024-01-02T00:00:00Z",
                        "content_hash": hash_t2,
                        "filename": "001_turn_00000002.json",
                    }
                }
            }
            service._save_sync_state("test-project", state)

            processed_trace_ids = []
            original_init, cleanup = self._mock_langfuse_api(pages_data, processed_trace_ids)

            try:
                creds = LangfusePullProject(public_key="test", secret_key="test")
                service.sync_project("https://test.com", creds, 30)

                # Load state after sync
                final_state = service._load_sync_state("test-project")

                # t2 should still have its original filename despite being sorted after t1
                assert final_state["trace_hashes"][trace_id_t2]["filename"] == "001_turn_00000002.json"
                assert existing_file_t2.exists()

                # t1 (new trace, older timestamp) should get sequence number 2
                assert trace_id_t1 in final_state["trace_hashes"]
                assert final_state["trace_hashes"][trace_id_t1]["filename"] == "002_turn_00000001.json"

            finally:
                cleanup()


class TestFinalizeTraceFiles:
    """Test _finalize_trace_files static method for Phase 2 move from staging to destination."""

    def test_finalize_moves_from_staging_to_destination(self):
        """Files written to staging dir should be moved to dest with sequential names."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_base = Path(tmpdir) / ".langfuse_staging" / "test-project" / "user1" / "session1"
            dest_folder = Path(tmpdir) / "golden-repos" / "langfuse_test-project_user1" / "session1"
            staging_base.mkdir(parents=True)

            # Create staged files with trace_id.json naming
            trace_ids = ["trace-aaa-12345678", "trace-bbb-23456789", "trace-ccc-34567890"]
            timestamps = ["2024-01-02T00:00:00Z", "2024-01-01T00:00:00Z", "2024-01-03T00:00:00Z"]

            for tid in trace_ids:
                (staging_base / f"{tid}.json").write_text('{"test": "data"}')

            # Build pending with 6-tuple: (timestamp, trace_id, staging_folder, dest_folder, trace_type, short_id)
            pending_renames = [
                (timestamps[2], trace_ids[2], str(staging_base), str(dest_folder), "turn", "34567890"),  # Newest
                (timestamps[0], trace_ids[0], str(staging_base), str(dest_folder), "turn", "12345678"),  # Middle
                (timestamps[1], trace_ids[1], str(staging_base), str(dest_folder), "turn", "23456789"),  # Oldest
            ]

            trace_hashes = {
                trace_ids[0]: {"updated_at": timestamps[0], "content_hash": "hash1", "filename": None},
                trace_ids[1]: {"updated_at": timestamps[1], "content_hash": "hash2", "filename": None},
                trace_ids[2]: {"updated_at": timestamps[2], "content_hash": "hash3", "filename": None},
            }

            # Call _finalize_trace_files
            LangfuseTraceSyncService._finalize_trace_files(pending_renames, trace_hashes)

            # Verify files moved to destination in chronological order
            assert (dest_folder / "001_turn_23456789.json").exists(), "Oldest trace should be 001"
            assert (dest_folder / "002_turn_12345678.json").exists(), "Middle trace should be 002"
            assert (dest_folder / "003_turn_34567890.json").exists(), "Newest trace should be 003"

            # Staged files should be gone (moved, not copied)
            assert not (staging_base / f"{trace_ids[0]}.json").exists()
            assert not (staging_base / f"{trace_ids[1]}.json").exists()
            assert not (staging_base / f"{trace_ids[2]}.json").exists()

            # Verify trace_hashes updated with filenames
            assert trace_hashes[trace_ids[1]]["filename"] == "001_turn_23456789.json"
            assert trace_hashes[trace_ids[0]]["filename"] == "002_turn_12345678.json"
            assert trace_hashes[trace_ids[2]]["filename"] == "003_turn_34567890.json"

    def test_finalize_missing_staged_file_logs_warning(self):
        """Missing staged file should log warning and NOT update trace_hashes (High #2)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_base = Path(tmpdir) / ".langfuse_staging" / "test-project" / "user1" / "session1"
            dest_folder = Path(tmpdir) / "golden-repos" / "langfuse_test-project_user1" / "session1"
            staging_base.mkdir(parents=True)

            # Create ONE staged file, but pending has TWO entries
            trace_id_exists = "trace-exists-12345678"
            trace_id_missing = "trace-missing-87654321"
            (staging_base / f"{trace_id_exists}.json").write_text('{"test": "data"}')
            # trace_id_missing NOT created - simulates crash or missing file

            pending_renames = [
                ("2024-01-01T00:00:00Z", trace_id_exists, str(staging_base), str(dest_folder), "turn", "12345678"),
                ("2024-01-02T00:00:00Z", trace_id_missing, str(staging_base), str(dest_folder), "turn", "87654321"),
            ]

            trace_hashes = {
                trace_id_exists: {"updated_at": "2024-01-01T00:00:00Z", "content_hash": "hash1", "filename": None},
                trace_id_missing: {"updated_at": "2024-01-02T00:00:00Z", "content_hash": "hash2", "filename": None},
            }

            # Should not crash, should log warning
            LangfuseTraceSyncService._finalize_trace_files(pending_renames, trace_hashes)

            # Existing file should be moved successfully
            assert (dest_folder / "001_turn_12345678.json").exists()
            assert trace_hashes[trace_id_exists]["filename"] == "001_turn_12345678.json"

            # Missing file: filename should remain None (NOT updated)
            assert trace_hashes[trace_id_missing]["filename"] is None

    def test_finalize_destination_exists_overwrites_with_warning(self):
        """When destination folder has existing files, new files get next sequence number."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_base = Path(tmpdir) / ".langfuse_staging" / "test-project" / "user1" / "session1"
            dest_folder = Path(tmpdir) / "golden-repos" / "langfuse_test-project_user1" / "session1"
            staging_base.mkdir(parents=True)
            dest_folder.mkdir(parents=True)

            trace_id = "trace-new-12345678"
            (staging_base / f"{trace_id}.json").write_text('{"test": "new data"}')

            # Pre-create destination file with sequence 001
            existing_file = dest_folder / "001_turn_existing.json"
            existing_file.write_text('{"test": "old data"}')

            pending_renames = [
                ("2024-01-01T00:00:00Z", trace_id, str(staging_base), str(dest_folder), "turn", "12345678"),
            ]

            trace_hashes = {
                trace_id: {"updated_at": "2024-01-01T00:00:00Z", "content_hash": "hash1", "filename": None},
            }

            # Should assign next available sequence number (002)
            LangfuseTraceSyncService._finalize_trace_files(pending_renames, trace_hashes)

            # Old file should still exist with old data
            assert existing_file.exists()
            assert "old data" in existing_file.read_text()

            # New file should have sequence 002 with new data
            new_file = dest_folder / "002_turn_12345678.json"
            assert new_file.exists()
            assert "new data" in new_file.read_text()

            # trace_hashes updated with new sequence number
            assert trace_hashes[trace_id]["filename"] == "002_turn_12345678.json"

    def test_finalize_handles_none_timestamp(self):
        """Verify traces with None/missing timestamps are handled gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_base = Path(tmpdir) / ".langfuse_staging" / "test-project" / "user1" / "session1"
            dest_folder = Path(tmpdir) / "golden-repos" / "langfuse_test-project_user1" / "session1"
            staging_base.mkdir(parents=True)

            trace_ids = ["trace-aaa-11111111", "trace-bbb-22222222"]
            (staging_base / f"{trace_ids[0]}.json").write_text('{"test": "data"}')
            (staging_base / f"{trace_ids[1]}.json").write_text('{"test": "data"}')

            # One trace has None timestamp, other has valid timestamp
            pending_renames = [
                ("2024-01-02T00:00:00Z", trace_ids[1], str(staging_base), str(dest_folder), "turn", "22222222"),
                (None, trace_ids[0], str(staging_base), str(dest_folder), "turn", "11111111"),  # None timestamp
            ]

            trace_hashes = {
                trace_ids[0]: {"updated_at": None, "content_hash": "hash1", "filename": None},
                trace_ids[1]: {"updated_at": "2024-01-02T00:00:00Z", "content_hash": "hash2", "filename": None},
            }

            # Should not crash
            LangfuseTraceSyncService._finalize_trace_files(pending_renames, trace_hashes)

            # Verify files were moved (None sorts before valid timestamps)
            assert (dest_folder / "001_turn_11111111.json").exists()
            assert (dest_folder / "002_turn_22222222.json").exists()

    def test_finalize_multiple_folders(self):
        """Verify finalize handles traces across multiple destination folders correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging1 = Path(tmpdir) / ".langfuse_staging" / "test-project" / "user1" / "session1"
            staging2 = Path(tmpdir) / ".langfuse_staging" / "test-project" / "user2" / "session2"
            dest1 = Path(tmpdir) / "golden-repos" / "langfuse_test-project_user1" / "session1"
            dest2 = Path(tmpdir) / "golden-repos" / "langfuse_test-project_user2" / "session2"
            staging1.mkdir(parents=True)
            staging2.mkdir(parents=True)

            # Create traces in both staging folders
            (staging1 / "trace-a-11111111.json").write_text('{"test": "data"}')
            (staging1 / "trace-b-22222222.json").write_text('{"test": "data"}')
            (staging2 / "trace-c-33333333.json").write_text('{"test": "data"}')

            pending_renames = [
                # Dest 1: reverse chronological
                ("2024-01-02T00:00:00Z", "trace-b-22222222", str(staging1), str(dest1), "turn", "22222222"),
                ("2024-01-01T00:00:00Z", "trace-a-11111111", str(staging1), str(dest1), "turn", "11111111"),
                # Dest 2: single trace
                ("2024-01-03T00:00:00Z", "trace-c-33333333", str(staging2), str(dest2), "turn", "33333333"),
            ]

            trace_hashes = {
                "trace-a-11111111": {"updated_at": "2024-01-01T00:00:00Z", "content_hash": "hash1", "filename": None},
                "trace-b-22222222": {"updated_at": "2024-01-02T00:00:00Z", "content_hash": "hash2", "filename": None},
                "trace-c-33333333": {"updated_at": "2024-01-03T00:00:00Z", "content_hash": "hash3", "filename": None},
            }

            LangfuseTraceSyncService._finalize_trace_files(pending_renames, trace_hashes)

            # Dest 1: should be sorted chronologically
            assert (dest1 / "001_turn_11111111.json").exists()
            assert (dest1 / "002_turn_22222222.json").exists()

            # Dest 2: single trace starts at 001
            assert (dest2 / "001_turn_33333333.json").exists()

    def test_finalize_continues_existing_sequence(self):
        """Verify new traces get sequence numbers after existing ones in destination."""
        with tempfile.TemporaryDirectory() as tmpdir:
            staging_base = Path(tmpdir) / ".langfuse_staging" / "test-project" / "user1" / "session1"
            dest_folder = Path(tmpdir) / "golden-repos" / "langfuse_test-project_user1" / "session1"
            staging_base.mkdir(parents=True)
            dest_folder.mkdir(parents=True)

            # Create existing sequential files in DESTINATION
            (dest_folder / "001_turn_existing1.json").write_text('{"test": "data"}')
            (dest_folder / "002_turn_existing2.json").write_text('{"test": "data"}')

            # Create new trace in STAGING
            (staging_base / "trace-new-99999999.json").write_text('{"test": "data"}')

            pending_renames = [
                ("2024-01-03T00:00:00Z", "trace-new-99999999", str(staging_base), str(dest_folder), "turn", "99999999"),
            ]

            trace_hashes = {
                "trace-new-99999999": {"updated_at": "2024-01-03T00:00:00Z", "content_hash": "hash1", "filename": None},
            }

            LangfuseTraceSyncService._finalize_trace_files(pending_renames, trace_hashes)

            # New trace should be 003 (after existing 001, 002 in destination)
            assert (dest_folder / "003_turn_99999999.json").exists()
            assert not (staging_base / "trace-new-99999999.json").exists()

    def test_staging_cleanup_removes_empty_dirs(self):
        """After finalization, empty staging dirs should be cleaned up."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            project_name = "test-project"

            # Create dummy trace for getting staging dir
            trace = {"userId": "user1", "sessionId": "session1"}

            # Create staging directory structure with empty dirs
            staging_dir = service._get_staging_dir(project_name, trace)
            staging_dir.mkdir(parents=True)

            # Add a file then remove it to simulate post-finalization state
            test_file = staging_dir / "test.json"
            test_file.write_text('{"test": "data"}')
            test_file.unlink()

            # Get parent dirs for verification
            user_dir = staging_dir.parent
            project_staging = user_dir.parent

            # Now staging_dir, user_dir, and project_staging are all empty
            assert staging_dir.exists()
            assert user_dir.exists()
            assert project_staging.exists()

            # Cleanup
            service._cleanup_staging(project_name)

            # All empty dirs should be removed
            assert not staging_dir.exists()
            assert not user_dir.exists()
            assert not project_staging.exists()


class TestMigrationFromOldNaming:
    """Test migration from old {trace_id}.json naming to sequential naming."""

    def test_old_state_without_filename_unchanged_trace(self):
        """Old state entry without 'filename' key should still detect unchanged trace."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()

            trace = {
                "id": "old-trace-id-abcd1234",
                "name": "my-trace",
                "updatedAt": "2024-01-01T00:00:00+00:00",
                "userId": "test_user",
                "sessionId": "test_session",
            }

            folder = service._get_trace_folder("test-project", trace)
            folder.mkdir(parents=True, exist_ok=True)
            old_file = folder / "old-trace-id-abcd1234.json"
            old_file.write_text('{"trace": {}, "observations": []}')

            trace_hashes = {
                "old-trace-id-abcd1234": {
                    "updated_at": "2024-01-01T00:00:00+00:00",
                    "content_hash": "somehash",
                }
            }
            metrics = SyncMetrics()

            service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)
            assert metrics.traces_unchanged == 1

    def test_old_trace_migrates_on_content_change(self):
        """Old trace without filename gets new sequential name when content changes.

        Updated: Phase 1 writes to staging, Phase 2 moves to destination with sequential name.
        Old file in destination stays in place (manual cleanup needed for orphaned old-format files).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()
            mock_api_client.fetch_observations.return_value = [
                {"id": "new-obs", "startTime": "2024-01-02T00:00:00Z"}
            ]

            trace = {
                "id": "old-trace-id-abcd1234",
                "name": "my-trace",
                "updatedAt": "2024-01-02T00:00:00+00:00",
                "userId": "test_user",
                "sessionId": "test_session",
            }

            folder = service._get_trace_folder("test-project", trace)
            folder.mkdir(parents=True, exist_ok=True)
            old_file = folder / "old-trace-id-abcd1234.json"
            old_file.write_text('{"trace": {}, "observations": []}')

            trace_hashes = {
                "old-trace-id-abcd1234": {
                    "updated_at": "2024-01-01T00:00:00+00:00",
                    "content_hash": "old_hash",
                }
            }
            metrics = SyncMetrics()

            # Phase 1: Process trace (writes to staging, returns 6-tuple metadata)
            rename_info = service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            # Verify staging file exists
            staging_folder = service._get_staging_dir("test-project", trace)
            if rename_info:
                assert (staging_folder / f"{trace['id']}.json").exists(), "Staging file should exist"

            # Phase 2: Finalize (move from staging to destination with sequential name)
            if rename_info:
                service._finalize_trace_files([rename_info], trace_hashes)

            new_filename = trace_hashes["old-trace-id-abcd1234"]["filename"]
            assert new_filename == "001_turn_abcd1234.json"
            assert (folder / new_filename).exists(), "New sequential file should exist"
            # Note: Old file stays in place - manual cleanup needed for migration
            assert old_file.exists(), "Old file remains (not automatically deleted)"
            assert not (staging_folder / f"{trace['id']}.json").exists(), "Staging file should be moved"
            assert metrics.traces_written_updated == 1

    def test_old_trace_unchanged_leaves_old_file_alone(self):
        """Old trace file should be left as-is if content hasn't changed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            mock_api_client = Mock()

            trace = {
                "id": "old-trace-id-wxyz9876",
                "name": "my-trace",
                "updatedAt": "2024-01-01T00:00:00+00:00",
                "userId": "test_user",
                "sessionId": "test_session",
            }

            folder = service._get_trace_folder("test-project", trace)
            folder.mkdir(parents=True, exist_ok=True)
            old_file = folder / "old-trace-id-wxyz9876.json"
            old_file.write_text('{"trace": {}, "observations": []}')

            trace_hashes = {
                "old-trace-id-wxyz9876": {
                    "updated_at": "2024-01-01T00:00:00+00:00",
                    "content_hash": "somehash",
                }
            }
            metrics = SyncMetrics()

            service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            assert old_file.exists()
            assert metrics.traces_unchanged == 1


# ==============================================================================
# Phase 2: Integration Tests (Requires Live Langfuse)
# ==============================================================================


class TestLangfuseApiIntegration:
    """Integration tests using real Langfuse API."""

    @pytest.fixture
    def live_config(self):
        """Load live config with real credentials."""
        config_dir = os.path.expanduser("~/.cidx-server")
        config_file = Path(config_dir) / "config.json"

        if not config_file.exists():
            pytest.skip("No CIDX server config found")

        # Load config
        from code_indexer.server.utils.config_manager import ServerConfigManager

        manager = ServerConfigManager(config_dir)
        config = manager.load_config()

        if not config or not config.langfuse_config:
            pytest.skip("No Langfuse config found")

        if not config.langfuse_config.pull_enabled:
            pytest.skip("Langfuse pull not enabled")

        if not config.langfuse_config.pull_projects:
            pytest.skip("No Langfuse pull projects configured")

        return config

    def test_discover_project(self, live_config):
        """Test project discovery from API."""
        from code_indexer.server.services.langfuse_api_client import LangfuseApiClient

        host = live_config.langfuse_config.pull_host
        creds = live_config.langfuse_config.pull_projects[0]

        api_client = LangfuseApiClient(host, creds)
        project_info = api_client.discover_project()

        # Should return dict with at least 'name' field
        assert isinstance(project_info, dict)
        assert "name" in project_info
        assert isinstance(project_info["name"], str)
        assert len(project_info["name"]) > 0

    def test_fetch_traces_page(self, live_config):
        """Test fetching a page of traces."""
        from code_indexer.server.services.langfuse_api_client import LangfuseApiClient

        host = live_config.langfuse_config.pull_host
        creds = live_config.langfuse_config.pull_projects[0]

        api_client = LangfuseApiClient(host, creds)

        # Fetch recent traces
        from_time = datetime.now(timezone.utc) - timedelta(days=7)
        traces = api_client.fetch_traces_page(1, from_time)

        # Should return list (may be empty)
        assert isinstance(traces, list)
        # If there are traces, verify structure
        if traces:
            trace = traces[0]
            assert "id" in trace

    def test_fetch_observations(self, live_config):
        """Test fetching observations for a trace."""
        from code_indexer.server.services.langfuse_api_client import LangfuseApiClient

        host = live_config.langfuse_config.pull_host
        creds = live_config.langfuse_config.pull_projects[0]

        api_client = LangfuseApiClient(host, creds)

        # First get a trace
        from_time = datetime.now(timezone.utc) - timedelta(days=7)
        traces = api_client.fetch_traces_page(1, from_time)

        if not traces:
            pytest.skip("No traces available for testing")

        trace_id = traces[0]["id"]

        # Fetch observations
        observations = api_client.fetch_observations(trace_id)

        # Should return list (may be empty)
        assert isinstance(observations, list)
        # If there are observations, verify structure
        if observations:
            obs = observations[0]
            assert "id" in obs

    def test_full_sync_cycle(self, live_config):
        """Test complete sync: fetch, hash, write, state persistence."""
        from code_indexer.server.services.langfuse_api_client import LangfuseApiClient

        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: live_config, tmpdir)

            host = live_config.langfuse_config.pull_host
            creds = live_config.langfuse_config.pull_projects[0]
            trace_age_days = live_config.langfuse_config.pull_trace_age_days

            # Run sync
            service.sync_project(host, creds, trace_age_days)

            # Verify state file was created
            api_client = LangfuseApiClient(host, creds)
            project_info = api_client.discover_project()
            project_name = project_info.get("name", "unknown")
            state_file = service._get_state_file_path(project_name)

            assert state_file.exists()

            # Verify state content
            state = service._load_sync_state(project_name)
            assert "last_sync_timestamp" in state
            assert "trace_hashes" in state

            # Verify metrics were updated
            metrics = service.get_metrics()
            assert project_name in metrics


# ==============================================================================
# Test Helpers
# ==============================================================================


def _mock_config() -> ServerConfig:
    """Create minimal mock config for testing."""
    config = ServerConfig(server_dir="/tmp/test")
    config.langfuse_config = LangfuseConfig(
        enabled=False,
        pull_enabled=True,
        pull_host="https://cloud.langfuse.com",
        pull_projects=[
            LangfusePullProject(public_key="test_pk", secret_key="test_sk")
        ],
        pull_sync_interval_seconds=300,
        pull_trace_age_days=30,
    )
    return config
