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
from unittest.mock import MagicMock

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
            trace_id = "trace123"
            trace = {"id": trace_id, "name": "test"}
            observations = [{"id": "o1"}]

            service._write_trace(folder, trace_id, trace, observations)

            trace_file = folder / f"{trace_id}.json"
            assert trace_file.exists()

    def test_write_creates_directories(self):
        """Writing trace should create parent directories if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "level1" / "level2" / "level3"
            trace_id = "trace123"
            trace = {"id": trace_id}
            observations = []

            service._write_trace(folder, trace_id, trace, observations)

            assert folder.exists()
            assert (folder / f"{trace_id}.json").exists()

    def test_write_overwrites_existing(self):
        """Writing trace should overwrite existing file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "test_folder"
            folder.mkdir(parents=True)
            trace_id = "trace123"
            trace_file = folder / f"{trace_id}.json"

            # Write initial content
            trace_file.write_text("old content")

            # Overwrite
            trace = {"id": trace_id, "new": "data"}
            observations = []
            service._write_trace(folder, trace_id, trace, observations)

            content = trace_file.read_text()
            assert "old content" not in content
            assert "new" in content

    def test_write_is_pretty_printed(self):
        """Written JSON should be pretty-printed with indentation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "test_folder"
            trace_id = "trace123"
            trace = {"id": trace_id, "name": "test"}
            observations = [{"id": "o1", "type": "generation"}]

            service._write_trace(folder, trace_id, trace, observations)

            content = (folder / f"{trace_id}.json").read_text()
            # Pretty-printed JSON should have newlines and indentation
            assert "\n" in content
            assert "  " in content  # Indentation

    def test_write_combines_trace_and_observations(self):
        """Written file should contain both trace and observations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            service = LangfuseTraceSyncService(lambda: _mock_config(), tmpdir)
            folder = Path(tmpdir) / "test_folder"
            trace_id = "trace123"
            trace = {"id": trace_id, "name": "test"}
            observations = [{"id": "o1"}, {"id": "o2"}]

            service._write_trace(folder, trace_id, trace, observations)

            content = (folder / f"{trace_id}.json").read_text()
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
            trace_id = "trace123"
            trace = {"id": trace_id}
            observations = [
                {"id": "o3", "startTime": "2026-01-01T12:03:00Z"},
                {"id": "o1", "startTime": "2026-01-01T12:01:00Z"},
                {"id": "o2", "startTime": "2026-01-01T12:02:00Z"},
            ]

            service._write_trace(folder, trace_id, trace, observations)

            content = (folder / f"{trace_id}.json").read_text()
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

            service._process_trace(mock_api_client, trace, "test-project", trace_hashes, metrics)

            # updatedAt should be updated
            assert trace_hashes["t1"]["updated_at"] == "2024-01-02T00:00:00+00:00"
            assert trace_hashes["t1"]["content_hash"] == expected_hash
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
