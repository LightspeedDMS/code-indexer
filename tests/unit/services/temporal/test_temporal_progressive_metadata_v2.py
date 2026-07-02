"""
Unit tests for TemporalProgressiveMetadata v2 — format_version 2, atomic writes,
file locking, legacy migration, per-provider progress tracking.
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from src.code_indexer.services.temporal.temporal_progressive_metadata import (
    FORMAT_VERSION,
    TemporalProgressiveMetadata,
)


class TestTemporalProgressiveMetadataV2(unittest.TestCase):
    """Test TemporalProgressiveMetadata with format_version 2 features."""

    def setUp(self):
        """Create temporary directory for testing."""
        self.temp_dir = tempfile.mkdtemp()
        self.temporal_dir = Path(self.temp_dir) / ".code-indexer/index/temporal"
        self.temporal_dir.mkdir(parents=True, exist_ok=True)
        self.metadata = TemporalProgressiveMetadata(self.temporal_dir)

    def tearDown(self):
        """Clean up temporary directory."""
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Test 1: mark_commit_indexed creates file with format_version 2
    # ------------------------------------------------------------------
    def test_mark_commit_indexed_creates_file(self):
        """mark_commit_indexed creates file with format_version 2."""
        self.metadata.mark_commit_indexed("abc123")

        self.assertTrue(self.metadata.progress_path.exists())
        with open(self.metadata.progress_path) as f:
            data = json.load(f)
        self.assertEqual(data["format_version"], FORMAT_VERSION)
        self.assertIn("abc123", data["completed_commits"])

    # ------------------------------------------------------------------
    # Test 2: mark_commit_indexed deduplicates
    # ------------------------------------------------------------------
    def test_mark_commit_indexed_deduplicates(self):
        """Marking same commit twice results in only one entry."""
        self.metadata.mark_commit_indexed("abc123")
        self.metadata.mark_commit_indexed("abc123")

        with open(self.metadata.progress_path) as f:
            data = json.load(f)
        self.assertEqual(data["completed_commits"].count("abc123"), 1)

    # ------------------------------------------------------------------
    # Test 3: load_completed returns Set[str]
    # ------------------------------------------------------------------
    def test_load_completed_returns_set(self):
        """load_completed returns a Set[str]."""
        self.metadata.mark_commit_indexed("aaa")
        self.metadata.mark_commit_indexed("bbb")

        result = self.metadata.load_completed()
        self.assertIsInstance(result, set)
        self.assertEqual(result, {"aaa", "bbb"})

    # ------------------------------------------------------------------
    # Test 4: save_completed backward compat
    # ------------------------------------------------------------------
    def test_save_completed_backward_compat(self):
        """save_completed (legacy API) still works and uses format_version 2."""
        self.metadata.save_completed("legacy_commit")

        completed = self.metadata.load_completed()
        self.assertIn("legacy_commit", completed)

        with open(self.metadata.progress_path) as f:
            data = json.load(f)
        self.assertEqual(data["format_version"], FORMAT_VERSION)

    # ------------------------------------------------------------------
    # Test 5: mark_completed batch
    # ------------------------------------------------------------------
    def test_mark_completed_batch(self):
        """mark_completed marks multiple commits at once."""
        self.metadata.mark_completed(["c1", "c2", "c3"])

        completed = self.metadata.load_completed()
        self.assertEqual(completed, {"c1", "c2", "c3"})

    # ------------------------------------------------------------------
    # Test 6: set_state valid values
    # ------------------------------------------------------------------
    def test_set_state_valid(self):
        """set_state accepts idle, building, failed."""
        for state in ("idle", "building", "failed"):
            self.metadata.set_state(state)
            self.assertEqual(self.metadata.get_state(), state)

    # ------------------------------------------------------------------
    # Test 7: set_state invalid raises ValueError
    # ------------------------------------------------------------------
    def test_set_state_invalid_raises(self):
        """set_state raises ValueError for unknown states."""
        with self.assertRaises(ValueError):
            self.metadata.set_state("in_progress")

        with self.assertRaises(ValueError):
            self.metadata.set_state("complete")

        with self.assertRaises(ValueError):
            self.metadata.set_state("")

    # ------------------------------------------------------------------
    # Test 8: get_state defaults to idle for new file
    # ------------------------------------------------------------------
    def test_get_state_default_idle(self):
        """New file defaults to state 'idle'."""
        self.assertEqual(self.metadata.get_state(), "idle")

    # ------------------------------------------------------------------
    # Test 9: format_version 2 in file
    # ------------------------------------------------------------------
    def test_format_version_2_in_file(self):
        """JSON file written by mark_commit_indexed contains format_version: 2."""
        self.metadata.mark_commit_indexed("x")
        with open(self.metadata.progress_path) as f:
            data = json.load(f)
        self.assertEqual(data["format_version"], 2)

    # ------------------------------------------------------------------
    # Test 10: completed_commits is sorted and deduplicated
    # ------------------------------------------------------------------
    def test_completed_commits_is_sorted_list(self):
        """completed_commits in JSON is a sorted, deduplicated list."""
        self.metadata.mark_completed(["zzz", "aaa", "mmm", "aaa"])

        with open(self.metadata.progress_path) as f:
            data = json.load(f)
        commits = data["completed_commits"]
        self.assertEqual(commits, sorted(set(commits)))
        self.assertEqual(len(commits), len(set(commits)))

    # ------------------------------------------------------------------
    # Test 11: last_updated is ISO 8601 timestamp
    # ------------------------------------------------------------------
    def test_last_updated_is_iso8601(self):
        """last_updated field is a valid ISO 8601 timestamp."""
        self.metadata.mark_commit_indexed("t1")
        with open(self.metadata.progress_path) as f:
            data = json.load(f)
        ts = data["last_updated"]
        # Must be parseable as ISO 8601 with timezone
        parsed = datetime.datetime.fromisoformat(ts)
        self.assertIsNotNone(parsed.tzinfo)

    # ------------------------------------------------------------------
    # Test 12: legacy migration adds format_version
    # ------------------------------------------------------------------
    def test_legacy_migration_adds_format_version(self):
        """Old file without format_version gets migrated to version 2."""
        legacy_data = {
            "completed_commits": ["old1", "old2"],
            "status": "complete",
        }
        with open(self.metadata.progress_path, "w") as f:
            json.dump(legacy_data, f)

        # Loading should trigger migration
        completed = self.metadata.load_completed()
        self.assertIn("old1", completed)
        self.assertIn("old2", completed)

        # File should now have format_version
        with open(self.metadata.progress_path) as f:
            data = json.load(f)
        self.assertEqual(data["format_version"], FORMAT_VERSION)

    # ------------------------------------------------------------------
    # Test 13: legacy migration maps status:in_progress → state:idle
    # ------------------------------------------------------------------
    def test_legacy_migration_maps_status_in_progress_to_idle(self):
        """Legacy status:in_progress maps to state:idle (old run is dead)."""
        legacy_data = {
            "completed_commits": ["c1"],
            "status": "in_progress",
        }
        with open(self.metadata.progress_path, "w") as f:
            json.dump(legacy_data, f)

        self.metadata.load_completed()  # trigger migration
        self.assertEqual(self.metadata.get_state(), "idle")

    # ------------------------------------------------------------------
    # Test 14: legacy migration maps status:failed → state:failed
    # ------------------------------------------------------------------
    def test_legacy_migration_maps_status_failed_to_failed(self):
        """Legacy status:failed maps to state:failed."""
        legacy_data = {
            "completed_commits": ["c1"],
            "status": "failed",
        }
        with open(self.metadata.progress_path, "w") as f:
            json.dump(legacy_data, f)

        self.metadata.load_completed()  # trigger migration
        self.assertEqual(self.metadata.get_state(), "failed")

    # ------------------------------------------------------------------
    # Test 15: legacy migration deduplicates commits
    # ------------------------------------------------------------------
    def test_legacy_migration_deduplicates_commits(self):
        """Legacy file with duplicate commits is deduplicated on migration."""
        legacy_data = {
            "completed_commits": ["dup", "dup", "unique"],
            "status": "complete",
        }
        with open(self.metadata.progress_path, "w") as f:
            json.dump(legacy_data, f)

        completed = self.metadata.load_completed()
        self.assertEqual(completed, {"dup", "unique"})

        with open(self.metadata.progress_path) as f:
            data = json.load(f)
        self.assertEqual(data["completed_commits"].count("dup"), 1)

    # ------------------------------------------------------------------
    # Test 16: atomic write via tmp file then replace
    # ------------------------------------------------------------------
    def test_atomic_write_via_replace(self):
        """Write uses tmp file that gets replaced atomically."""
        self.metadata.mark_commit_indexed("atomic_test")

        # The tmp file should not exist after successful write
        self.assertFalse(self.metadata._tmp_path.exists())
        # The actual file should exist
        self.assertTrue(self.metadata.progress_path.exists())

    # ------------------------------------------------------------------
    # Test 17: clear removes file
    # ------------------------------------------------------------------
    def test_clear_removes_file(self):
        """clear() deletes the progress file."""
        self.metadata.mark_commit_indexed("to_be_cleared")
        self.assertTrue(self.metadata.progress_path.exists())

        self.metadata.clear()
        self.assertFalse(self.metadata.progress_path.exists())

        # After clearing, load_completed returns empty set
        self.assertEqual(self.metadata.load_completed(), set())

    # ------------------------------------------------------------------
    # Test 18: corrupt JSON returns default data
    # ------------------------------------------------------------------
    def test_load_corrupt_json_returns_default(self):
        """Corrupt JSON file returns empty default (no exception)."""
        with open(self.metadata.progress_path, "w") as f:
            f.write("{invalid json!!!}")

        # Should not raise; returns empty set
        completed = self.metadata.load_completed()
        self.assertEqual(completed, set())

        state = self.metadata.get_state()
        self.assertEqual(state, "idle")


if __name__ == "__main__":
    unittest.main()
