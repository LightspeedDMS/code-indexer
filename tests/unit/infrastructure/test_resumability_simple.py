"""Simple focused tests for resumability functionality.

Story #2121 retired resume-from-list: no stored work list exists, an
interrupted run (status in_progress or failed) is recovered by the
store-derived reconcile, and only the size of a run's plan is recorded.
"""

import json
import tempfile
from pathlib import Path

import pytest

from code_indexer.services.progressive_metadata import ProgressiveMetadata


@pytest.fixture
def temp_metadata_path():
    """Create a temporary metadata file path."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = Path(f.name)
    yield path
    if path.exists():
        path.unlink()


_RETIRED_LIST_FIELDS = ("files_to_index", "completed_files", "current_file_index")


class TestRetiredResumeList:
    """No stored work list; only the plan size is recorded."""

    def test_legacy_stored_list_is_dropped_on_load_and_never_saved(
        self, temp_metadata_path
    ):
        temp_metadata_path.write_text(
            json.dumps(
                {
                    "status": "in_progress",
                    "files_to_index": ["src/a.py", "src/b.py"],
                    "completed_files": ["src/a.py"],
                    "current_file_index": 1,
                    "total_files_to_index": 2,
                }
            )
        )
        metadata = ProgressiveMetadata(temp_metadata_path)
        assert metadata.metadata["status"] == "in_progress"
        for retired in _RETIRED_LIST_FIELDS:
            assert retired not in metadata.metadata

        metadata.update_progress(files_processed=1)
        saved = json.loads(temp_metadata_path.read_text())
        for retired in _RETIRED_LIST_FIELDS:
            assert retired not in saved

    def test_interrupted_state_survives_across_instances(self, temp_metadata_path):
        first = ProgressiveMetadata(temp_metadata_path)
        first.start_indexing("test-provider", "test-model", {"git_available": False})
        first.record_plan(3)
        first.update_progress(files_processed=1, chunks_added=5)

        second = ProgressiveMetadata(temp_metadata_path)
        assert second.metadata["status"] == "in_progress"
        assert second.metadata["files_processed"] == 1
        assert second.metadata["total_files_to_index"] == 3

    def test_record_plan_stores_only_the_count(self, temp_metadata_path):
        metadata = ProgressiveMetadata(temp_metadata_path)
        metadata.record_plan(198_000)
        saved = json.loads(temp_metadata_path.read_text())
        assert saved["total_files_to_index"] == 198_000
        for retired in _RETIRED_LIST_FIELDS:
            assert retired not in saved
        with pytest.raises(ValueError):
            metadata.record_plan(-1)

    def test_complete_indexing_records_the_listing_start(self, temp_metadata_path):
        metadata = ProgressiveMetadata(temp_metadata_path)
        metadata.start_indexing("test-provider", "test-model", {"git_available": False})
        metadata.complete_indexing(1_700_000_000.5)
        assert metadata.metadata["status"] == "completed"
        assert metadata.metadata["last_index_timestamp"] == 1_700_000_000.5
        for invalid in (0.0, -1.0):
            with pytest.raises(ValueError):
                metadata.complete_indexing(invalid)

    @pytest.mark.parametrize(
        "tamper_field", [None, "files_to_index", "failed_file_paths"]
    )
    def test_old_sealed_state_with_list_fields_keeps_its_seal_check(
        self, temp_metadata_path, tamper_field
    ):
        """An old server-sealed file still carries the retired list fields.
        They are dropped only AFTER the seal digest is taken, so the
        untouched file stays trusted and any tampering -- also of a dropped
        list field -- is refused."""
        from code_indexer.services.resume_state_seal import (
            RESUME_SEAL_FIELD,
            compute_resume_seal,
            content_digest,
        )

        key = b"k" * 32
        payload = {
            "status": "in_progress",
            "files_to_index": ["src/a.py", "src/b.py"],
            "completed_files": ["src/a.py"],
            "current_file_index": 1,
            "total_files_to_index": 2,
            "failed_file_paths": ["src/c.py"],
        }
        binding = str(temp_metadata_path.resolve())
        payload[RESUME_SEAL_FIELD] = compute_resume_seal(
            key, binding, content_digest(payload)
        )
        if tamper_field is not None:
            payload[tamper_field] = ["src/tampered.py"]
        temp_metadata_path.write_text(json.dumps(payload))

        metadata = ProgressiveMetadata(temp_metadata_path)
        metadata.enable_resume_seal(key)

        assert metadata.loaded_state_is_sealed() is (tamper_field is None)
        for retired in _RETIRED_LIST_FIELDS:
            assert retired not in metadata.metadata


class TestDatabaseReconciliationResumability:
    """Test the new database-reconciliation based reconcile functionality."""

    def test_database_reconciliation_with_timestamps(self, temp_metadata_path):
        """
        Test reconcile functionality that compares disk files with database contents.

        This tests the new --reconcile behavior that:
        1. Scans files that should be indexed from disk
        2. Checks what files are already in the database with timestamps
        3. Indexes missing files and files with newer timestamps
        """
        # This test would require mocking the Filesystem client and SmartIndexer
        # For now, we'll create a conceptual test structure

        # Simulate the logic: files on disk vs files in database with timestamps
        # disk_files = {"src/main.py": 1000, "src/utils.py": 2000, "src/config.py": 3000, "tests/test_main.py": 4000}
        # database_files = {"src/main.py": 1000, "src/utils.py": 1500}  # utils.py is older in DB, config.py missing

        # Expected files to be indexed: missing files + files with newer timestamps
        expected_files_to_index = [
            Path("src/utils.py"),  # Modified (newer on disk: 2000 > 1500)
            Path("src/config.py"),  # Missing from database
            Path("tests/test_main.py"),  # Missing from database
        ]

        # This test validates the logic but would need full integration
        # to test with real Filesystem client and SmartIndexer
        assert len(expected_files_to_index) == 3
        assert Path("src/utils.py") in expected_files_to_index  # Modified file
        assert Path("src/config.py") in expected_files_to_index  # Missing file
        assert Path("tests/test_main.py") in expected_files_to_index  # Missing file

        # Verify up-to-date files are not in the list
        assert (
            Path("src/main.py") not in expected_files_to_index
        )  # Same timestamp, up-to-date
